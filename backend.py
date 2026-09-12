#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StroyAI — бэкенд в ОДНОМ файле, база данных — PostgreSQL (Neon).

Продакшен-версия: тот же http.server + sqlite3 заменён на http.server +
psycopg2, подключение берётся из переменной окружения DATABASE_URL (Neon
даёт такую строку в своей панели). Запуск локально:

    DATABASE_URL=postgres://... python backend.py

На Railway PORT и DATABASE_URL подставляются автоматически (DATABASE_URL —
если вы привяжете Neon через Railway, либо впишите его вручную в Variables).
"""

import json
import gzip
import mimetypes
import os
import re
import secrets
import string
import subprocess
import sys
import atexit
import base64
import io
import collections
import concurrent.futures
import threading
import time
import urllib.request
import urllib.error
import psycopg2
import psycopg2.extras
from PIL import Image
import psycopg2.pool
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Простой загрузчик .env без внешних зависимостей (не тянем
    python-dotenv ради одного файла) — читает KEY=VALUE построчно и
    доливает в os.environ только те переменные, которых там ЕЩЁ нет.
    Так реальное окружение (например, Variables в Railway) всегда важнее
    содержимого .env — обычное и ожидаемое поведение для .env-файлов.
    Нужен только для локального запуска `python backend.py`; на Railway
    переменные и так приходят через Variables, файла .env там просто нет."""
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 8000))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
INDEX_HTML_PATH = os.path.join(BASE_DIR, 'index.html')
ADMIN_HTML_PATH = os.path.join(BASE_DIR, 'admin.html')
DELIVERY_FEE = 15000  # сум, фиксированная стоимость доставки по Ташкенту и области (легаси, не используется в новом расчёте)
PREPAY_THRESHOLD = 5_000_000  # сум: заказы от этой суммы требуют предоплату 50%, ниже — 30%

# ---------------------------------------------------------------------------
# РЕЖИМЫ ПОЛУЧЕНИЯ ЗАКАЗА: наценки на товар (пункт 1 ТЗ)
#
# Цена товара клиенту = закупочная цена (products.base_price) × коэффициент.
# Коэффициент зависит от выбранного в корзине режима, поэтому переключение
# режима мгновенно пересчитывает все цены — ничего не захардкожено.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# НАЦЕНКА НА ТОВАР: ОТКЛЮЧЕНА (по явному указанию заказчика)
#
# Все цены, которые присылает заказчик (кирпич, цемент, газоблок), — это уже
# ГОТОВЫЕ розничные цены с его собственной наценкой. Раньше здесь стояли
# коэффициенты 1.12/1.18, и система накручивала наценку ЕЩЁ РАЗ поверх уже
# посчитанной цены — это была ошибка, сейчас исправлено.
#
# products.base_price = именно та цена, которую прислал заказчик, и клиенту
# показывается СТРОГО она же, без каких-либо умножений, в обоих режимах
# получения. Если в будущем понадобится вернуть разницу в цене товара между
# доставкой и самовывозом — верните сюда коэффициенты ≠ 1.0.
# ---------------------------------------------------------------------------
MARKUP_DELIVERY = 1.0
MARKUP_PICKUP = 1.01  # самовывоз дороже на 1% — наценка за товар без учёта доставки

# ---------------------------------------------------------------------------
# СТОИМОСТЬ ДОСТАВКИ: реальная стоимость × 1.25 (пункт 1 ТЗ)
#
# «Реальная стоимость доставки» в этой простой версии считается по суммарному
# весу заказа (базовая стоимость рейса + доплата за вес сверх включённого).
# Это осознанная заглушка вместо интеграции с картами/логистикой — при
# необходимости замените calc_real_delivery_cost() на реальный тариф
# (фиксированные зоны, API транспортной компании и т.п.), остальная система
# (наценка +25%, отображение, скрытие блока при самовывозе) менять не нужно.
# ---------------------------------------------------------------------------
DELIVERY_BASE_COST = 20000    # сум — минимальная «реальная» стоимость одного рейса
DELIVERY_COST_PER_KG = 150    # сум за кг сверх бесплатного лимита веса
DELIVERY_FREE_KG = 300        # кг, включённые в базовую стоимость рейса
DELIVERY_MARKUP_PERCENT = 25  # +25% к реальной стоимости доставки, показываемое клиенту

# ---------------------------------------------------------------------------
# TELEGRAM-БОТ ОПЛАТЫ (пункт 3 ТЗ)
#
# Впишите сюда данные вашего бота (создаётся за 1 минуту у @BotFather) и id
# чата/канала администратора (узнать id можно, например, через @userinfobot
# или @getmyid_bot — добавьте бота в канал и перешлите оттуда сообщение).
# Пока TELEGRAM_BOT_USERNAME пустой — сайт создаёт заказы, но кнопка
# «Оплатить» не будет вести в бота (на это будет явная подсказка на сайте).
# ---------------------------------------------------------------------------
TELEGRAM_BOT_USERNAME = ''     # имя бота БЕЗ @, например 'stroyai_pay_bot'
TELEGRAM_ADMIN_CHAT_ID = ''    # id чата/канала администратора, например '-1001234567890'
PAYMENT_CARD_NUMBER = '0000 0000 0000 0000'   # реквизиты карты — показываются в бланке оплаты на сайте
PAYMENT_CARD_HOLDER = 'STROYAI OOO'

# Адрес и режим работы точки самовывоза — показываются клиенту на странице
# «Оформить заказ», когда выбран режим «Самовывоз».
PICKUP_ADDRESS = 'г. Ташкент, ул. Примерная, 10'
PICKUP_HOURS = 'Ежедневно 9:00–19:00'
PICKUP_READY_MINUTES = 60   # сколько обычно готовится заказ к самовывозу «как можно скорее»

# Пароль входа в панель администратора (отдельная страница /admin/ — там
# видно ВСЕ заказы всех клиентов и можно подтверждать каждый шаг вручную,
# а также есть полная очистка базы данных).
#
# ВАЖНО: раньше здесь был захардкожен пароль 'admin123' прямо в коде —
# любой, кто открывал этот файл (архив в чате, GitHub-репозиторий и т.п.),
# получал полный доступ к сайту, включая стирание базы данных. Теперь
# пароль обязательно берётся из переменной окружения на Railway:
#   ADMIN_PANEL_PASSWORD=длинный-случайный-пароль
# Если переменная не задана — используется старый пароль 'admin123' ТОЛЬКО
# чтобы не заблокировать вам доступ прямо сейчас, но при каждом старте
# сервера в логи печатается громкое предупреждение (см. main()) — задайте
# нормальный пароль на Railway и смените его как можно скорее.
ADMIN_PANEL_PASSWORD = os.environ.get('ADMIN_PANEL_PASSWORD', 'admin123')

# Простая защита от подбора пароля админки: считаем неудачные попытки входа
# за последние 15 минут (в памяти процесса — общий счётчик на весь сервер,
# не по IP, потому что на Railway обычные адреса подключений — это внутренние
# адреса прокси, а не реальные IP посетителей, см. обсуждение в чате). Не
# идеально (один настойчивый атакующий может временно затруднить вход и
# настоящему админу), но закрывает главную дыру — неограниченный перебор
# 'admin123'-подобных паролей без какого-либо предела попыток.
_admin_login_failures = collections.deque(maxlen=200)
_admin_login_lock = threading.Lock()
ADMIN_LOGIN_MAX_FAILURES = 10
ADMIN_LOGIN_WINDOW_SECONDS = 900  # 15 минут


def _check_admin_login_throttle():
    with _admin_login_lock:
        now = time.time()
        while _admin_login_failures and now - _admin_login_failures[0] > ADMIN_LOGIN_WINDOW_SECONDS:
            _admin_login_failures.popleft()
        if len(_admin_login_failures) >= ADMIN_LOGIN_MAX_FAILURES:
            raise ApiError(429, 'Слишком много неудачных попыток входа в админку. Попробуйте через 15 минут.')


def _record_admin_login_failure():
    with _admin_login_lock:
        _admin_login_failures.append(time.time())


# Общий секрет между backend.py и telegram_bot.py (чтобы бот мог читать и
# подтверждать заказы через служебные /api/internal/... эндпоинты). Секрет
# генерируется один раз и сохраняется рядом, в internal_secret.txt — не
# публикуйте этот файл вместе с кодом (как и пароль от почты).
INTERNAL_SECRET_PATH = os.path.join(BASE_DIR, 'internal_secret.txt')


def _load_or_create_internal_secret():
    env_secret = os.environ.get('INTERNAL_SECRET', '')
    if env_secret:
        return env_secret
    if os.path.exists(INTERNAL_SECRET_PATH):
        with open(INTERNAL_SECRET_PATH, 'r', encoding='utf-8') as f:
            value = f.read().strip()
            if value:
                return value
    value = secrets.token_hex(24)
    with open(INTERNAL_SECRET_PATH, 'w', encoding='utf-8') as f:
        f.write(value)
    return value


INTERNAL_API_SECRET = _load_or_create_internal_secret()
PREPAY_PERCENT_HIGH = 50
PREPAY_PERCENT_LOW = 30

# ---------------------------------------------------------------------------
# Настройки почты (Gmail App Password) — для реальной отправки OTP-кодов.
#
# Как получить пароль приложения в Gmail:
# ПОЧЕМУ НЕ SMTP: Railway блокирует исходящие SMTP-соединения (порты 25,
# 465, 587, 2525) на тарифах Free/Trial/Hobby — SMTP разрешён только на
# платном тарифе Pro. Поэтому прямая отправка через smtplib/Gmail здесь не
# работает в принципе (ошибка "[Errno 101] Network is unreachable" — это
# блокировка сети, а не проблема пароля). Обычный HTTPS никто не блокирует,
# поэтому письма теперь уходят через HTTP API сервиса Brevo (бывш. Sendinblue):
#   1. Зарегистрируйтесь бесплатно: https://www.brevo.com (300 писем/день
#      бесплатно навсегда, без привязки карты).
#   2. Settings → Senders, Domains, IPs → Senders → Add a sender → впишите
#      вашу почту (например ту же gjjnn05@gmail.com) → Brevo пришлёт на неё
#      письмо с 6-значным кодом → введите код. Домен/DNS настраивать не нужно.
#   3. Settings → SMTP & API → API Keys → Generate a new API key → скопируйте
#      ключ (вида xkeysib-...).
#   4. На Railway, во вкладке Variables у сервиса backend, добавьте:
#        BREVO_API_KEY=xkeysib-ваш-ключ
#        EMAIL_HOST_USER=та-самая-подтверждённая-в-Brevo-почта@gmail.com
#      (EMAIL_HOST_PASSWORD/EMAIL_HOST/EMAIL_PORT больше не нужны и не
#      используются.)
# Если BREVO_API_KEY не задан — коды по-прежнему печатаются в консоль (как
# раньше), сайт при этом продолжит работать.
# ---------------------------------------------------------------------------
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'gjjnn05@gmail.com')
EMAIL_FROM_NAME = os.environ.get('EMAIL_FROM_NAME', 'StroyAI')

# ---------------------------------------------------------------------------
# ИИ-Дизайн: по текстовому промпту пользователя генерируем через Google
# Gemini API (модель gemini-3.1-flash-image, она же "Nano Banana 2")
# несколько картинок дома — с разных ракурсов + отдельно эскиз планировки
# (НЕ настоящий чертёж со размерами, просто иллюстрация «как может
# выглядеть» — так и подписываем на сайте). Если пользователь приложил
# референс-фото участка/дома — оно передаётся модели вместе с промптом
# (Gemini понимает картинку+текст в одном запросе, отдельного "edit"
# эндпоинта как у OpenAI тут не нужно).
# Нужно на Railway задать:
#   GEMINI_API_KEY=AIza...   (получить в Google AI Studio: aistudio.google.com/apikey)
# Без ключа раздел ИИ-Дизайна возвращает понятную ошибку вместо падения.
# У Gemini есть бесплатный лимит запросов в день — для старта хватает,
# при росте нагрузки нужно будет включить платный биллинг в Google Cloud.
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_IMAGE_MODEL = os.environ.get('GEMINI_IMAGE_MODEL', 'gemini-3.1-flash-image-preview')
GEMINI_GENERATE_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent'
# Референс-фото от пользователя (участок / существующий дом) — ограничение
# на размер декодированного файла, чтобы не гонять гигантские фото в API
# и не давать положить сервер огромным телом запроса.
AI_DESIGN_MAX_PHOTO_BYTES = 6 * 1024 * 1024

# Стили, которые можно выбрать в дропдауне на фронте (см. index.html,
# #aiDesignStyle) — ключ должен совпадать со значением <option value="...">.
AI_DESIGN_STYLE_LABELS = {
    'scandi': 'скандинавский стиль (светлое дерево, простые формы, светлая отделка)',
    'minimal': 'минимализм (чистые линии, простые геометрические формы, нейтральные цвета)',
    'classic': 'классический стиль (симметричный фасад, традиционные материалы и декор)',
    'loft': 'лофт (кирпичная кладка, металл, индустриальные детали)',
    'hightech': 'хай-тек (стекло, металл, современные геометрические формы, минимум декора)',
    'modern': 'современный стиль (лаконичный фасад, панорамные окна, плоская или пологая крыша)',
}
# Сколько раз в сутки один пользователь может запускать генерацию —
# каждый запуск это 4 платных вызова OpenAI, ограничиваем, чтобы никто
# случайно (или специально) не «съел» весь бюджет за один вечер.
AI_DESIGN_DAILY_LIMIT = int(os.environ.get('AI_DESIGN_DAILY_LIMIT', '3'))
GENERATED_DIR = os.path.join(BASE_DIR, 'generated')

# ---------------------------------------------------------------------------
# CORS: раньше Access-Control-Allow-Origin был '*' — API отвечало вообще
# любому сайту в интернете, не только нашему. Теперь список разрешённых
# доменов явный. Можно расширить/сменить через переменную окружения на
# Railway CORS_ALLOWED_ORIGINS (через запятую, без пробелов), например:
#   CORS_ALLOWED_ORIGINS=https://stroy-a1.vercel.app,https://мой-домен.com
# localhost разрешён всегда — иначе локальная разработка сломается.
_default_origins = (
    'https://stroy-a1.vercel.app,'
    'https://web-production-619ab.up.railway.app'
)
ALLOWED_ORIGINS = set(
    o.strip() for o in os.environ.get('CORS_ALLOWED_ORIGINS', _default_origins).split(',') if o.strip()
)

# Каждый элемент: (ключ_ракурса, подпись для промпта). Подпись добавляется
# К промпту пользователя, а не заменяет его — так по одному описанию дома
# получается небольшой, но цельный комплект картинок.
# Ракурс "plan" (эскиз планировки) сюда специально НЕ включён: генеративная
# картинка не может гарантировать точные размеры комнат, а пользователю
# нужен именно точный план — он считается формулой в build_floor_plan() и
# рисуется на фронте как SVG, а не генерируется ИИ.
AI_DESIGN_ANGLES = [
    ('front', 'Фасад дома спереди, вид с улицы, реалистичное фото, дневной свет'),
    ('side', 'Тот же дом, вид сбоку (с торца), реалистичное фото, дневной свет'),
    ('perspective', 'Тот же дом, вид с угла в три четверти, реалистичное фото, дневной свет'),
]


def generate_gemini_image(prompt, reference_png_bytes=None):
    """
    Один вызов Gemini generateContent с модальностью IMAGE. Если передано
    reference_png_bytes — референс-фото добавляется в тот же запрос как
    inline_data, и модель генерирует "по мотивам" него; отдельного
    edit-эндпоинта, как у OpenAI, у Gemini нет — это один и тот же вызов.
    Возвращает bytes картинки (PNG). Бросает ApiError с понятным текстом,
    если ключ не настроен или API отказал (неверный ключ, лимит исчерпан,
    сработал фильтр безопасности и т.п.) — чтобы пользователь увидел
    причину, а не просто "ошибка".
    """
    if not GEMINI_API_KEY:
        raise ApiError(503, 'ИИ-Дизайн временно недоступен: не настроен GEMINI_API_KEY на сервере.')

    parts = [{'text': prompt}]
    if reference_png_bytes is not None:
        parts.append({
            'inline_data': {
                'mime_type': 'image/png',
                'data': base64.b64encode(reference_png_bytes).decode('ascii'),
            }
        })

    payload = json.dumps({
        'contents': [{'parts': parts}],
        'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']},
    }).encode('utf-8')

    url = GEMINI_GENERATE_URL.format(model=GEMINI_IMAGE_MODEL)
    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': GEMINI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            pass
        print(f'[ai-design] Gemini отказал (HTTP {e.code}): {body}')
        if e.code in (401, 403):
            raise ApiError(502, 'ИИ-Дизайн: неверный или отозванный GEMINI_API_KEY.')
        if e.code == 429:
            raise ApiError(429, 'ИИ-Дизайн: превышен дневной лимит запросов у Gemini, попробуйте позже.')
        raise ApiError(502, 'ИИ-Дизайн: сервис генерации изображений вернул ошибку, попробуйте другой запрос.')
    except (urllib.error.URLError, OSError) as e:
        print(f'[ai-design] Сеть недоступна ({type(e).__name__}): {e}')
        raise ApiError(502, 'ИИ-Дизайн: не удалось связаться с сервисом генерации изображений.')

    try:
        candidate = data['candidates'][0]
        # блокировка safety-фильтром — обычная (не HTTP-)ошибка, у неё
        # просто нет частей с картинкой в ответе
        if candidate.get('finishReason') == 'SAFETY':
            raise ApiError(422, 'ИИ-Дизайн: запрос отклонён фильтром безопасности, переформулируйте описание.')
        image_b64 = None
        for part in candidate['content']['parts']:
            inline = part.get('inlineData') or part.get('inline_data')
            if inline and inline.get('data'):
                image_b64 = inline['data']
                break
        if not image_b64:
            raise ApiError(502, 'ИИ-Дизайн: сервис не вернул картинку, попробуйте другой запрос.')
    except ApiError:
        raise
    except (KeyError, IndexError, TypeError):
        raise ApiError(502, 'ИИ-Дизайн: сервис генерации вернул неожиданный ответ.')
    return base64.b64decode(image_b64)


def decode_and_prepare_reference_photo(photo_b64):
    """
    Декодирует base64-фото, присланное фронтом (может быть с префиксом
    data:image/...;base64,), проверяет что это валидное изображение и
    сжимает его до разумного размера перед отправкой в OpenAI. Возвращает
    PNG bytes или None, если фото не передали.
    """
    if not photo_b64:
        return None
    if ',' in photo_b64 and photo_b64.strip().startswith('data:'):
        photo_b64 = photo_b64.split(',', 1)[1]
    try:
        raw = base64.b64decode(photo_b64, validate=True)
    except Exception:
        raise ApiError(400, 'Не удалось прочитать загруженное фото')
    if len(raw) > AI_DESIGN_MAX_PHOTO_BYTES:
        raise ApiError(400, 'Фото слишком большое (максимум 6 МБ)')
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert('RGB')
    except Exception:
        raise ApiError(400, 'Файл не похож на изображение — попробуйте другое фото')
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

STATUS_LABELS = {
    'processing': 'В обработке',
    'materials_delivered': 'Материалы доставлены',
    'ready_for_pickup': 'Готово к выдаче',
    'crew_dispatched': 'Бригада выезжает',
    'completed': 'Завершено',
    'cancelled': 'Отменён',
}


def order_step_keys(order):
    """
    Шаги статуса конкретного заказа. «Бригада выезжает» — это отдельная
    платная услуга монтажа/укладки, а не что-то, что есть у каждого заказа:
    показываем этот шаг, только если клиент сам выбрал её при оформлении
    (order['needs_crew']). Название второго шага тоже зависит от способа
    получения — доставка или самовывоз.
    """
    second = 'ready_for_pickup' if order['mode'] == 'pickup' else 'materials_delivered'
    keys = ['processing', second]
    if order['needs_crew']:
        keys.append('crew_dispatched')
    keys.append('completed')
    return keys


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
class PGCursorWrapper:
    """Оборачивает psycopg2-курсор: делегирует fetchone/fetchall, и для
    INSERT-запросов эмулирует sqlite3-style cur.lastrowid через
    автоматически добавленный RETURNING id."""

    def __init__(self, raw_cursor):
        self._raw = raw_cursor
        self.lastrowid = None

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self._raw)

    @property
    def rowcount(self):
        # Сколько строк реально задел последний UPDATE/DELETE — нужно,
        # например, чтобы понять, что чей-то повторный ответ на уже
        # отвеченный запрос стоимости доставки ни на что не повлиял
        # (см. set-delivery-quote).
        return self._raw.rowcount


class PGConnWrapper:
    """Тонкая обёртка над psycopg2-соединением, чтобы код ниже (написанный
    под sqlite3: conn.execute(...).fetchone()/.fetchall(), cur.lastrowid)
    менялся как можно меньше."""

    def __init__(self, raw, pool=None):
        self._raw = raw
        self._pool = pool  # если задан — close() возвращает соединение в пул,
        # а не рвёт TCP/TLS-сессию (это и есть ускорение: не переоткрываем
        # соединение к Neon на каждый запрос)

    def execute(self, sql, params=()):
        raw_cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        stripped = sql.strip()
        no_id_tables = ('tokens', 'admin_sessions', 'finance_settings')
        is_insert = (
            stripped.upper().startswith('INSERT')
            and 'RETURNING' not in stripped.upper()
            and not any(re.match(r'INSERT\s+INTO\s+' + t, stripped, re.IGNORECASE) for t in no_id_tables)
        )
        if is_insert:
            sql_to_run = stripped.rstrip().rstrip(';') + ' RETURNING id'
        else:
            sql_to_run = sql
        raw_cur.execute(sql_to_run, params)
        wrapper = PGCursorWrapper(raw_cur)
        if is_insert:
            row = raw_cur.fetchone()
            wrapper.lastrowid = row['id'] if row else None
        return wrapper

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._pool is not None:
            try:
                # На всякий случай откатываем незавершённую транзакцию — если
                # обработчик упал с исключением после INSERT/UPDATE без явного
                # commit()/rollback(), следующий, кто возьмёт это соединение
                # из пула, не должен унаследовать чужую "подвисшую" транзакцию.
                # Если commit() уже был вызван — rollback() на уже завершённой
                # транзакции безопасен и ничего не делает.
                self._raw.rollback()
                self._pool.putconn(self._raw)
                return
            except Exception:
                # Соединение сломано (например, Neon сам разорвал простаивавшую
                # сессию) — не возвращаем его в пул, пусть пул создаст новое.
                try:
                    self._pool.putconn(self._raw, close=True)
                except Exception:
                    pass
                return
        try:
            self._raw.close()
        except Exception:
            pass


def _strip_channel_binding(dsn):
    """Убирает ?channel_binding=... / &channel_binding=... из DSN — нужно
    как запасной вариант, если бандл libpq в окружении его не знает."""
    stripped = re.sub(r'([?&])channel_binding=[^&]*&?', r'\1', dsn)
    return stripped.rstrip('?&')


def _connect_kwargs_for(dsn):
    # Neon в pooled-режиме уже кладёт sslmode прямо в строку подключения —
    # не дублируем его отдельным аргументом, если он там уже есть.
    return {} if 'sslmode=' in dsn else {'sslmode': 'require'}


def _make_raw_connection(dsn=None):
    """Открывает одно сырое соединение к Postgres — с тем же fallback-ом по
    channel_binding, что и раньше. Используется и пулом при создании, и как
    аварийный запасной вариант, если пул исчерпан или соединение протухло."""
    dsn = dsn or DATABASE_URL
    connect_kwargs = _connect_kwargs_for(dsn)
    try:
        return psycopg2.connect(dsn, **connect_kwargs)
    except psycopg2.OperationalError as e:
        if 'channel_binding' in dsn and 'channel_binding' in str(e):
            return psycopg2.connect(_strip_channel_binding(dsn), **connect_kwargs)
        raise


# Пул соединений к Postgres. Раньше каждый API-запрос открывал НОВОЕ
# соединение к Neon с нуля — а Neon физически находится в другом регионе,
# и полный цикл TCP+TLS+аутентификация занимает секунды. Пул держит уже
# открытые соединения и переиспользует их между запросами — это и есть
# главное ускорение сайта.
_CONN_POOL = None
_CONN_POOL_LOCK = threading.Lock()
POOL_MAX_CONN = int(os.environ.get('POOL_MAX_CONN', '20'))


def _get_pool():
    global _CONN_POOL
    if _CONN_POOL is not None:
        return _CONN_POOL
    with _CONN_POOL_LOCK:
        if _CONN_POOL is None:
            dsn = DATABASE_URL
            connect_kwargs = _connect_kwargs_for(dsn)
            try:
                _CONN_POOL = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX_CONN, dsn, **connect_kwargs)
            except psycopg2.OperationalError as e:
                if 'channel_binding' in dsn and 'channel_binding' in str(e):
                    dsn = _strip_channel_binding(dsn)
                    _CONN_POOL = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX_CONN, dsn, **connect_kwargs)
                else:
                    raise
    return _CONN_POOL


def db():
    if not DATABASE_URL:
        raise RuntimeError(
            'DATABASE_URL не задан. Впишите строку подключения Neon в переменные '
            'окружения (см. README/раздел деплоя).'
        )
    pool_ = _get_pool()
    try:
        raw = pool_.getconn()
    except psycopg2.pool.PoolError:
        # Пул временно исчерпан — резкий всплеск параллельных запросов
        # (например, много пользователей одновременно). Не роняем запрос:
        # открываем отдельное соединение сверх пула. Оно просто закроется
        # после использования, а не вернётся в пул (см. PGConnWrapper.close) —
        # ровно то же поведение, что было ДО появления пула. На время всплеска
        # теряется часть выигрыша в скорости, зато сайт не падает под нагрузкой.
        return PGConnWrapper(_make_raw_connection())
    # Соединение из пула могло протухнуть, если долго простаивало (Neon и/или
    # его pooler сами обрывают неактивные сессии) — проверяем дешёвым запросом
    # и, если протухло, выбрасываем его и берём свежее, вместо падения на
    # реальном запросе пользователя.
    try:
        probe = raw.cursor()
        probe.execute('SELECT 1')
        probe.fetchone()
        probe.close()
    except Exception:
        try:
            pool_.putconn(raw, close=True)
        except Exception:
            pass
        raw = _make_raw_connection()
    return PGConnWrapper(raw, pool_)


SCHEMA_STATEMENTS = [
    '''CREATE TABLE IF NOT EXISTS categories(
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE
    )''',
    '''CREATE TABLE IF NOT EXISTS products(
        id SERIAL PRIMARY KEY,
        category_id INTEGER NOT NULL REFERENCES categories(id),
        name TEXT NOT NULL,
        brand TEXT,
        price DOUBLE PRECISION NOT NULL,
        base_price DOUBLE PRECISION NOT NULL DEFAULT 0,
        unit TEXT NOT NULL,
        weight_kg DOUBLE PRECISION DEFAULT 0,
        color TEXT,
        rating DOUBLE PRECISION DEFAULT 5,
        description TEXT,
        specs_json TEXT DEFAULT '{}',
        image TEXT,
        in_stock INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS reviews(
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id),
        author_name TEXT NOT NULL,
        rating INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS faq(
        id SERIAL PRIMARY KEY,
        question TEXT NOT NULL,
        answer TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE,
        phone TEXT UNIQUE,
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        address TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS otp_codes(
        id SERIAL PRIMARY KEY,
        channel TEXT NOT NULL,
        destination TEXT NOT NULL,
        code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS cart_items(
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL DEFAULT 1
    )''',
    '''CREATE TABLE IF NOT EXISTS orders(
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        order_number TEXT UNIQUE,
        mode TEXT NOT NULL DEFAULT 'delivery',
        status TEXT NOT NULL DEFAULT 'processing',
        payment_status TEXT NOT NULL DEFAULT 'awaiting_payment',
        total DOUBLE PRECISION NOT NULL,
        delivery_fee DOUBLE PRECISION NOT NULL,
        delivery_real_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        prepay_percent INTEGER NOT NULL DEFAULT 30,
        prepay_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        is_prepaid INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS order_items(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        product_id INTEGER REFERENCES products(id),
        quantity INTEGER NOT NULL,
        price DOUBLE PRECISION NOT NULL,
        weight_kg DOUBLE PRECISION DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS notifications(
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        text TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS bot_outbox(
        id SERIAL PRIMARY KEY,
        kind TEXT NOT NULL,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        delivered INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS admin_sessions(
        token TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )''',
    # --- мини-бухгалтерия (/admin/finance) --------------------------------
    '''CREATE TABLE IF NOT EXISTS factories(
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS finance_transactions(
        id SERIAL PRIMARY KEY,
        type TEXT NOT NULL,
        amount_uzs DOUBLE PRECISION NOT NULL,
        order_id INTEGER REFERENCES orders(id),
        category TEXT,
        note TEXT,
        event_id TEXT UNIQUE,
        created_at TEXT NOT NULL,
        created_by TEXT
    )''',
    # Снапшот buy/sell по каждой позиции заказа на момент продажи — нужен,
    # чтобы маржа и отчёт «по заводам» не пересчитывались задним числом,
    # если прайс товара потом изменится.
    '''CREATE TABLE IF NOT EXISTS finance_order_snapshots(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        product_id INTEGER,
        product_name TEXT,
        factory_id INTEGER,
        factory_name TEXT,
        qty DOUBLE PRECISION NOT NULL,
        buy_price DOUBLE PRECISION NOT NULL,
        sell_price DOUBLE PRECISION NOT NULL,
        line_buy_total DOUBLE PRECISION NOT NULL,
        line_sell_total DOUBLE PRECISION NOT NULL,
        line_margin DOUBLE PRECISION NOT NULL,
        created_at TEXT NOT NULL
    )''',
    # Простое key-value хранилище настроек финансов (курс USD и время его
    # обновления) — без отдельной таблицы на одну строку.
    '''CREATE TABLE IF NOT EXISTS finance_settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )''',
    # Стоимость доставки теперь считается по частям — если в заказе товары
    # НЕСКОЛЬКИХ заводов, у каждого завода директор указывает стоимость
    # доставки СВОЕЙ части лично боту, и только когда ответили ВСЕ —
    # итоговая доставка (сумма их ответов) применяется к заказу. Одна
    # строка = один ожидаемый/полученный ответ одного завода по одному
    # заказу. factory_id может быть NULL — «без завода» (для товаров без
    # привязки к заводу), такие тогда спрашиваются в общем канале, а не в
    # личке директора.
    '''CREATE TABLE IF NOT EXISTS order_delivery_quotes(
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        factory_id INTEGER REFERENCES factories(id),
        factory_name TEXT NOT NULL,
        amount DOUBLE PRECISION,
        created_at TEXT NOT NULL
    )''',
    # Одна строка = один запуск ИИ-дизайна (по одному промпту пользователя
    # генерируется сразу несколько картинок — разные ракурсы фасада плюс
    # эскиз планировки). images_json — список объектов
    # {"angle": "...", "path": "/generated/..png"}. status: pending /
    # done / failed. error — текст ошибки, если что-то не получилось
    # (чтобы показать пользователю, а не просто "ничего не произошло").
    '''CREATE TABLE IF NOT EXISTS ai_designs(
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        prompt TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        images_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        created_at TEXT NOT NULL
    )''',
]


def init_db():
    conn = db()
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
    # "Свежая" база — определяем не по файлу (как в sqlite-версии), а по
    # отсутствию категорий: значит демо-каталог ещё не засеян.
    fresh = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()['c'] == 0
    # Миграция для уже существующей базы, созданной ДО добавления новой системы
    # цен/доставки/оплаты. Postgres поддерживает IF NOT EXISTS в ADD COLUMN,
    # так что повторный запуск безопасен без try/except.
    migrations = [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS base_price DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_number TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'delivery'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'awaiting_payment'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_real_cost DOUBLE PRECISION NOT NULL DEFAULT 0",
        # --- новая логика ручного расчёта доставки через бота ---
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_quoted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_address TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS desired_time TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS blank_submitted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_screenshot_b64 TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_screenshot_mime TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS needs_crew INTEGER NOT NULL DEFAULT 0",
        # --- узбекские переводы названия/описания/характеристик товара и категории ---
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS name_uz TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS description_uz TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS specs_json_uz TEXT",
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_uz TEXT",
        # --- мини-бухгалтерия ---
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS factory_id INTEGER REFERENCES factories(id)",
        # buy_price сознательно БЕЗ DEFAULT/NOT NULL — NULL означает «закупочная цена ещё "
        # не задана владельцем», и однократно бэкфиллится в _backfill_finance_defaults().
        # Так админ может позже осознанно поставить 0, и это не перетрётся повторным запуском.
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS buy_price DOUBLE PRECISION",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TEXT",
        # Личка директора завода в Telegram — куда бот шлёт запрос «впишите
        # стоимость доставки вашей части», когда в заказе есть его товары.
        "ALTER TABLE factories ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT",
        # Счётчик неверных попыток ввода OTP-кода — без этого код можно было
        # подбирать (brute-force) неограниченным числом запросов к /otp/verify/,
        # пока не истекут 5 минут жизни кода.
        "ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
        # --- дедупликация корзины ---
        # На проде уже могли накопиться дубли cart_items на один и тот же
        # товар (из-за гонки при двойном клике "в корзину" в старой версии
        # кода) — сначала схлопываем их в одну строку с суммой quantity...
        "UPDATE cart_items c SET quantity = agg.total_qty FROM ("
        "  SELECT user_id, product_id, MIN(id) AS keep_id, SUM(quantity) AS total_qty "
        "  FROM cart_items GROUP BY user_id, product_id HAVING COUNT(*) > 1"
        ") agg WHERE c.id = agg.keep_id",
        "DELETE FROM cart_items c USING cart_items c2 "
        "WHERE c.user_id = c2.user_id AND c.product_id = c2.product_id AND c.id > c2.id",
        # ...а затем навсегда закрываем саму возможность дубля уникальным
        # индексом — он же служит целью для ON CONFLICT в апсерте ниже.
        "CREATE UNIQUE INDEX IF NOT EXISTS cart_items_user_product_uidx ON cart_items(user_id, product_id)",
        # --- точный план этажа для ИИ-Дизайна (считается формулой, не ИИ) ---
        # Храним исходные числа отдельно от текста prompt, чтобы можно было
        # пересчитать точный план и в истории генераций, а не только в
        # момент создания.
        "ALTER TABLE ai_designs ADD COLUMN IF NOT EXISTS size_m2 DOUBLE PRECISION",
        "ALTER TABLE ai_designs ADD COLUMN IF NOT EXISTS bedrooms INTEGER",
    ]
    for stmt in migrations:
        conn.execute(stmt)
    # У товаров, добавленных до миграции, base_price мог остаться 0 — тогда
    # используем текущую price как разумное приближение закупочной цены.
    conn.execute("UPDATE products SET base_price = price WHERE base_price = 0 OR base_price IS NULL")
    # Новые цены на облицовочный кирпич (по запросу): 4500 → 5200, 4000 → 4700.
    # Обновляем и уже существующие товары в базе, а не только новые посевы.
    conn.execute(
        "UPDATE products SET base_price=5200, price=5200 "
        "WHERE category_id=(SELECT id FROM categories WHERE slug='kirpich') AND base_price=4500"
    )
    conn.execute(
        "UPDATE products SET base_price=4700, price=4700 "
        "WHERE category_id=(SELECT id FROM categories WHERE slug='kirpich') AND base_price=4000"
    )
    # Заказы, оформленные ДО перехода на ручной расчёт доставки, уже имеют
    # готовую цену (автоматическую или самовывоз с фикс. нулём) — считаем их
    # «расчёт получен», чтобы старые заказы не зависли в статусе ожидания.
    conn.execute("UPDATE orders SET delivery_quoted=1 WHERE mode='pickup' AND delivery_quoted=0")
    conn.execute("UPDATE orders SET delivery_quoted=1 WHERE mode='delivery' AND delivery_fee>0 AND delivery_quoted=0")
    # Раньше у заказа было всего 2 статуса (processing/delivered) — теперь их
    # 4 (пункт «дашборд с подтверждением каждого шага»). Старые «доставленные»
    # заказы переносим в финальный статус нового флоу.
    conn.execute("UPDATE orders SET status='completed' WHERE status='delivered'")
    # Заказы, которые уже дошли до шага «Бригада выезжает» на старой логике,
    # помечаем needs_crew=1 задним числом — иначе этот статус станет для них
    # недопустимым (шаг теперь только для заказов, где клиент выбрал бригаду).
    conn.execute("UPDATE orders SET needs_crew=1 WHERE status='crew_dispatched' AND needs_crew=0")
    # Узбекские переводы названия/описания/характеристик — заполняем для ВСЕХ
    # товаров и категорий, у которых их ещё нет (и для только что мигрированной
    # старой базы, и как подстраховка на случай, если seed_demo() их не поставил).
    _backfill_uz_translations(conn)
    conn.commit()
    if fresh:
        seed_demo(conn)
        _backfill_uz_translations(conn)
    # Каждому товару без завода назначаем завод по бренду (или «Без завода»),
    # и товарам без закупочной цены — временно закупочную = продажной (маржа
    # 0 до тех пор, пока владелец не впишет реальный закуп в /admin/finance).
    _backfill_finance_defaults(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Узбекские переводы каталога (названия/описания/характеристики)
# ---------------------------------------------------------------------------
CATEGORY_NAME_UZ = {'Кирпич': "G'isht", 'Цемент': 'Sement', 'Газоблок': 'Gazoblok'}
COLOR_NAME_UZ = {
    'Белый': 'Oq', 'Тёмно-серый': "To'q kulrang", 'Красный': 'Qizil', 'Шоколад': 'Shokolad',
    'Жёлтый': 'Sariq', 'Мокрый': 'Nam asfalt', 'Персик': 'Shaftoli',
}
SPEC_KEY_UZ = {
    'Размер': "O'lcham", 'Марка': 'Markasi', 'Производство': 'Ishlab chiqarilgan joyi',
    'Метод': 'Usul', 'Высота': 'Balandligi', 'Длина×Ширина': 'Uzunligi×Kengligi', 'Вес мешка': "Qop og'irligi",
}
SPEC_VALUE_UZ = {'Ташкент': 'Toshkent', 'Гиперпресс': 'Giperpress'}
CEMENT_DESC_UZ = {
    'CONICH': "Portlandsement SEM II/A-M 32.5N, markasi M400. Qop 50 kg ±1%. Yuqori mustahkamlik, ishonchlilik va "
              "uzoq xizmat muddati, GOST 31108-2020 talablariga mos. \"CONICH CEMENT\" MChJning original mahsuloti.",
    'YASIN': "Portlandsement SEM II/B-I 32.5B. Qop 50 kg ±1%. Yuqori mustahkamlik, barqaror sifat, GOST 31108-2020 "
             "talablariga mos, ishonchli qadoq. Ishlab chiqaruvchi: Yasin qurilish mollari ishlab chiqarish fabrikasi.",
}
DELIVERY_TAIL_UZ = (" Yetkazib berish va olib ketishda narx bir xil; yetkazib berishda savatda alohida "
                     "yetkazib berish narxi qo'shiladi.")


def _uz_units(text):
    """Заменяет русские сокращения единиц измерения на узбекские в строке
    (мм→mm, см→sm, кг→kg) — используется и в описании, и в характеристиках."""
    if not text:
        return text
    text = re.sub(r'(?<=\d)\s*мм\b', ' mm', text)
    text = re.sub(r'(?<=\d)\s*см\b', ' sm', text)
    text = re.sub(r'(?<=\d)\s*кг\b', ' kg', text)
    return text


def _translate_specs_uz(specs):
    return {SPEC_KEY_UZ.get(k, k): _uz_units(SPEC_VALUE_UZ.get(v, v)) for k, v in (specs or {}).items()}


def _build_uz_fields(cat_slug, name, specs):
    """Возвращает (name_uz, description_uz, specs_uz_dict) для известного
    товара из демо-каталога, либо (None, None, None), если не удалось
    распознать шаблон (например, товар добавлен вручную) — тогда узбекское
    название просто не показываем, используется русское как запасной вариант."""
    if cat_slug == 'kirpich':
        m = re.search(r'«(.+?)»', name)
        if not m:
            return None, None, None
        color_ru = m.group(1)
        color_uz = COLOR_NAME_UZ.get(color_ru, color_ru)
        size = _uz_units((specs or {}).get('Размер', ''))
        name_uz = f'Fasad g\'ishti «{color_uz}»'
        desc_uz = (f'Fasad g\'ishti (giperpress), rangi «{color_uz}», o\'lchami {size}. Toshkent, markasi M-150.'
                   + DELIVERY_TAIL_UZ)
        return name_uz, desc_uz, _translate_specs_uz(specs)
    if cat_slug == 'cement':
        brand_key = 'CONICH' if 'CONICH' in name else ('YASIN' if 'YASIN' in name else None)
        if not brand_key:
            return None, None, None
        name_uz = name.replace('Портландцемент', 'Portlandsement')
        desc_uz = CEMENT_DESC_UZ[brand_key] + DELIVERY_TAIL_UZ
        return name_uz, desc_uz, _translate_specs_uz(specs)
    if cat_slug == 'gazoblok':
        m = re.search(r'Газоблок (\S+) 60×30×(\d+)', name)
        if not m:
            return None, None, None
        brand, h_cm = m.group(1), m.group(2)
        name_uz = f'Gazoblok {brand} 60×30×{h_cm}'
        desc_uz = f'{brand} markali gazobeton blok, o\'lchami 60×30×{h_cm} sm.' + DELIVERY_TAIL_UZ
        return name_uz, desc_uz, _translate_specs_uz(specs)
    return None, None, None


def _backfill_uz_translations(conn):
    # Категории
    cats = conn.execute('SELECT id, name, name_uz FROM categories').fetchall()
    for c in cats:
        if not c['name_uz']:
            name_uz = CATEGORY_NAME_UZ.get(c['name'])
            if name_uz:
                conn.execute('UPDATE categories SET name_uz=%s WHERE id=%s', (name_uz, c['id']))
    # Товары
    rows = conn.execute(
        'SELECT p.id, p.name, p.specs_json, p.name_uz, c.slug as cat_slug '
        'FROM products p JOIN categories c ON c.id=p.category_id '
        "WHERE p.name_uz IS NULL OR p.name_uz = ''"
    ).fetchall()
    for r in rows:
        specs = json.loads(r['specs_json'] or '{}')
        name_uz, desc_uz, specs_uz = _build_uz_fields(r['cat_slug'], r['name'], specs)
        if name_uz:
            conn.execute(
                'UPDATE products SET name_uz=%s, description_uz=%s, specs_json_uz=%s WHERE id=%s',
                (name_uz, desc_uz, json.dumps(specs_uz, ensure_ascii=False), r['id'])
            )
    conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fmt_sum(amount):
    return f'{amount:,.0f}'.replace(',', ' ') + ' сум'


def seed_demo(conn):
    cats = [('Кирпич', 'kirpich'), ('Цемент', 'cement'), ('Газоблок', 'gazoblok')]
    cat_ids = {}
    for name, slug in cats:
        cur = conn.execute('INSERT INTO categories(name, slug) VALUES (%s,%s)', (name, slug))
        cat_ids[slug] = cur.lastrowid

    # ------------------------------------------------------------------
    # КИРПИЧ — реальный прайс-лист поставщика STROYSERVIS, ТОЛЬКО ташкентский
    # регион (14 позиций, гиперпресс, марка М-150; позиции Туркменистана и
    # России из каталога сознательно не берём — работаем только по Ташкенту).
    #
    # base_price здесь = закупочная (оптовая) цена, ровно как указано в
    # прайс-листе / названо покупателем. Розничная цена клиенту считается от
    # неё через коэффициент режима (см. product_to_json и MARKUP_DELIVERY /
    # MARKUP_PICKUP в начале файла): при доставке — без наценки, при
    # самовывозе — дороже на MARKUP_PICKUP (сейчас 1%).
    # ------------------------------------------------------------------
    brick_specs_common = {'Марка': 'М-150', 'Производство': 'Ташкент', 'Метод': 'Гиперпресс'}
    bricks = [
        # (цвет, base_price, размер, вес кг, номер фото по каталогу, hex-цвет для свотча)
        ('Белый', 5200, '250×120×88 мм', 3.3, 1, '#e8e4da'),
        ('Тёмно-серый', 5200, '250×120×88 мм', 3.3, 2, '#4a4a52'),
        ('Красный', 5200, '250×120×88 мм', 3.3, 3, '#a8402f'),
        ('Шоколад', 5200, '250×120×88 мм', 3.3, 4, '#5a4438'),
        ('Жёлтый', 5200, '250×120×88 мм', 3.3, 5, '#c9a227'),
        ('Мокрый', 5200, '250×120×88 мм', 3.3, 6, '#2f3336'),
        ('Персик', 5200, '250×120×88 мм', 3.3, 7, '#c97a56'),
        ('Персик', 4700, '250×120×65 мм', 2.5, 8, '#c97a56'),
        ('Шоколад', 4700, '250×120×65 мм', 2.5, 9, '#5a4438'),
        ('Мокрый', 4700, '250×120×65 мм', 2.5, 10, '#2f3336'),
        ('Персик', 4700, '250×120×65 мм', 2.5, 11, '#c97a56'),
        ('Жёлтый', 4700, '250×120×65 мм', 2.5, 12, '#c9a227'),
        ('Тёмно-серый', 4700, '250×120×65 мм', 2.5, 13, '#4a4a52'),
        ('Белый', 4700, '250×120×65 мм', 2.5, 14, '#e8e4da'),
    ]

    products = []
    for name, base_price, size, weight_kg, photo_no, color in bricks:
        specs = {'Размер': size, **brick_specs_common}
        image = f'/media/products/brick-{photo_no:02d}.jpg'
        products.append((
            'kirpich', f'Облицовочный кирпич «{name}»', 'STROYSERVIS', base_price, 'шт', weight_kg,
            color, 4.8,
            f'Облицовочный кирпич гиперпрессования, цвет «{name}», размер {size}. Ташкент, марка М-150. '
            f'Цена одинакова при доставке и самовывозе; при доставке отдельно добавляется стоимость '
            f'доставки в корзине.',
            specs, image
        ))

    # ------------------------------------------------------------------
    # ЦЕМЕНТ — 2 позиции по цене за мешок 50 кг.
    # ------------------------------------------------------------------
    cements = [
        # (название, brand, base_price, описание, номер фото)
        (
            'Портландцемент CONICH М400', 'CONICH CEMENT', 40000,
            'Портландцемент ЦЕМ II/А-М 32.5N, марка М400. Мешок 50 кг ±1%. Высокая прочность, надёжность '
            'и долгий срок службы, соответствует ГОСТ 31108-2020. Оригинальная продукция TOO «CONICH CEMENT».',
            1,
        ),
        (
            'Портландцемент YASIN CEMENT', 'YASIN CEMENT', 38000,
            'Портландцемент SEM II/B-И 32.5Б. Мешок 50 кг ±1%. Высокая прочность, стабильное качество, '
            'соответствует ГОСТ 31108-2020, надёжная упаковка. Производство: фабрика Yasin qurilish mollari ishlab chiqarish.',
            2,
        ),
    ]
    for name, brand, base_price, desc, photo_no in cements:
        specs = {'Вес мешка': '50 кг ±1%', 'Производство': 'Ташкент'}
        image = f'/media/products/cement-{photo_no:02d}.jpg'
        products.append((
            'cement', name, brand, base_price, 'мешок', 50, None, 4.8,
            desc + ' Цена одинакова при доставке и самовывозе; при доставке отдельно добавляется '
                   'стоимость доставки в корзине.',
            specs, image
        ))

    # ------------------------------------------------------------------
    # ГАЗОБЛОК — реальные цены по размеру, марки М600 и М700 (прислано
    # заказчиком). base_price = цена из таблицы, показывается клиенту как
    # есть, без наценки (см. комментарий у MARKUP_DELIVERY выше).
    # ------------------------------------------------------------------
    gazoblok_prices = {
        'М600': {10: 10400, 12: 12500, 15: 15600, 20: 20800, 24: 25000, 30: 31200},
        'М700': {10: 11000, 12: 13200, 15: 16500, 20: 22000, 24: 26400, 30: 33000},
    }
    for brand, density in (('М600', 600), ('М700', 700)):
        for h_cm, base_price in gazoblok_prices[brand].items():
            weight_kg = round(0.6 * 0.3 * (h_cm / 100) * density, 1)
            specs = {'Высота': f'{h_cm} см', 'Длина×Ширина': '60×30 см', 'Марка': brand, 'Производство': 'Ташкент'}
            products.append((
                'gazoblok', f'Газоблок {brand} 60×30×{h_cm}', 'STROYSERVIS', base_price, 'шт', weight_kg,
                None, 4.8,
                f'Газобетонный блок марки {brand}, размер 60×30×{h_cm} см. Цена одинакова при доставке и '
                f'самовывозе; при доставке отдельно добавляется стоимость доставки в корзине.',
                specs, '/media/products/gazoblok-01.jpg'
            ))

    product_ids = []
    product_names = []
    for cat_slug, name, brand, base_price, unit, weight_kg, color, rating, desc, specs, image in products:
        display_price = round(base_price * MARKUP_DELIVERY)  # с MARKUP_DELIVERY=1.0 совпадает с base_price
        cur = conn.execute(
            'INSERT INTO products(category_id, name, brand, price, base_price, unit, weight_kg, color, '
            'rating, description, specs_json, image, in_stock, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)',
            (cat_ids[cat_slug], name, brand, display_price, base_price, unit, weight_kg, color, rating, desc,
             json.dumps(specs, ensure_ascii=False), image, now_iso())
        )
        product_ids.append(cur.lastrowid)
        product_names.append(name)

    def pid(name_substr):
        for i, n in enumerate(product_names):
            if name_substr in n:
                return product_ids[i]
        return None

    # Отзывов при первом запуске нет намеренно — рейтинг у всех товаров начинается с 5.0
    # и меняется только когда реальный покупатель оставит отзыв через сайт после доставки.
    review_count = 0

    faqs = [
        ('Как оформить заказ?',
         'Выберите товары в каталоге, добавьте в корзину и нажмите «Оформить заказ». '
         'Заказ появится в разделе «Мои заказы».'),
        ('Доставка и оплата',
         'Мы доставляем по Ташкенту и Ташкентской области, обычно за 1–2 дня. Оплата — картой '
         f'онлайн или наличными курьеру. Стоимость доставки — {DELIVERY_FEE:,} сум'.replace(',', ' ')),
        ('Возврат товара',
         'Вы можете вернуть неиспользованный товар в течение 14 дней при сохранении упаковки и чека.'),
        ('Как связаться с поддержкой?',
         'Через чат в разделе «Поддержка» или по телефону +998 78 150-00-00.'),
    ]
    for q, a in faqs:
        conn.execute('INSERT INTO faq(question, answer) VALUES (%s,%s)', (q, a))

    conn.commit()
    print(f'[seed] Загружено демо-данных: {len(product_ids)} товаров, '
          f'{review_count} отзывов, {len(faqs)} вопросов FAQ')


# ---------------------------------------------------------------------------
# Вспомогательные функции для JSON-ответов и авторизации
# ---------------------------------------------------------------------------
def row_to_dict(row):
    return dict(row) if row else None


def coef_for_mode(mode):
    """Коэффициент наценки на товар: без наценки при доставке, +MARKUP_PICKUP при самовывозе."""
    return MARKUP_PICKUP if mode == 'pickup' else MARKUP_DELIVERY


def calc_real_delivery_cost(total_weight_kg):
    """«Реальная» стоимость доставки — см. комментарий у DELIVERY_BASE_COST выше."""
    extra_kg = max(0.0, (total_weight_kg or 0) - DELIVERY_FREE_KG)
    return DELIVERY_BASE_COST + extra_kg * DELIVERY_COST_PER_KG


def calc_delivery_fee(total_weight_kg):
    """Возвращает (цена доставки клиенту, реальная стоимость) — цена = реальная × 1.25."""
    real_cost = calc_real_delivery_cost(total_weight_kg)
    fee = round(real_cost * (1 + DELIVERY_MARKUP_PERCENT / 100))
    return fee, round(real_cost)


def gen_order_number(conn):
    """Короткий понятный номер заказа: ORD-YYMMDD-XXXX (пункт 4 ТЗ)."""
    date_part = datetime.now(timezone.utc).strftime('%y%m%d')
    for _ in range(50):
        candidate = f'ORD-{date_part}-{secrets.randbelow(10000):04d}'
        exists = conn.execute('SELECT 1 FROM orders WHERE order_number=%s', (candidate,)).fetchone()
        if not exists:
            return candidate
    raise ApiError(500, 'Не удалось сгенерировать номер заказа, попробуйте ещё раз')


PAYMENT_STATUS_LABELS = {
    'awaiting_payment': 'Оплата при получении',
    'paid': 'Оплачено',
    'payment_problem': 'Проблема с оплатой',
}


def order_items_total(conn, order_id):
    """Сумма товаров заказа (без доставки) — пересчитывается из order_items,
    чтобы после того как админ укажет стоимость доставки, итог всегда был
    total = сумма товаров + доставка, без риска рассинхронизации."""
    row = conn.execute(
        'SELECT COALESCE(SUM(price*quantity),0) s FROM order_items WHERE order_id=%s', (order_id,)
    ).fetchone()
    return row['s'] or 0


# ---------------------------------------------------------------------------
# Общие действия над заказом — используются одновременно ботом (через
# /internal/...) и веб-панелью администратора (через /api/admin/...), чтобы
# логика не дублировалась в двух местах и не расходилась.
# ---------------------------------------------------------------------------
def apply_delivery_fee(conn, order, fee):
    items_total = order_items_total(conn, order['id'])
    new_total = items_total + fee
    conn.execute(
        'UPDATE orders SET delivery_fee=%s, delivery_real_cost=%s, delivery_quoted=1, total=%s WHERE id=%s',
        (fee, fee, new_total, order['id'])
    )
    conn.execute(
        'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
        (order['user_id'], 'Стоимость доставки указана',
         f'Для заказа №{order["order_number"]} администратор указал стоимость доставки: '
         f'{fmt_sum(fee)}. Итого к оплате: {fmt_sum(new_total)}. Откройте «Оформить заказ», '
         f'чтобы заполнить данные и оплатить.', now_iso())
    )
    conn.commit()


def apply_payment_decision(conn, order, approved):
    if approved:
        conn.execute(
            "UPDATE orders SET payment_status='paid', is_prepaid=1, paid_at=%s WHERE id=%s",
            (now_iso(), order['id'])
        )
        title, text = 'Оплата подтверждена', (
            f'Оплата заказа №{order["order_number"]} на сумму {fmt_sum(order["total"])} '
            f'подтверждена администратором.'
        )
    else:
        conn.execute("UPDATE orders SET payment_status='payment_problem' WHERE id=%s", (order['id'],))
        title, text = 'Проблема с оплатой', (
            f'Администратор не подтвердил оплату заказа №{order["order_number"]}. '
            f'Свяжитесь с поддержкой или отправьте новый скриншот оплаты.'
        )
    conn.execute(
        'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
        (order['user_id'], title, text, now_iso())
    )
    conn.commit()
    # Мини-бухгалтерия: подтверждение оплаты = доход (см. finance_record_income_on_payment_confirm).
    if approved:
        finance_record_income_on_payment_confirm(conn, order)


def set_order_status(conn, order, new_status):
    if new_status not in order_step_keys(order):
        raise ApiError(400, 'Некорректный статус для этого заказа')
    conn.execute('UPDATE orders SET status=%s WHERE id=%s', (new_status, order['id']))
    conn.execute(
        'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
        (order['user_id'], 'Статус заказа изменён',
         f'Заказ №{order["order_number"]}: {STATUS_LABELS[new_status]}', now_iso())
    )
    conn.commit()
    if new_status == 'completed':
        finance_record_income_on_completion(conn, order)


def can_cancel_order(order):
    """Заказ можно отменить, пока он не завершён и ещё не отменён ранее."""
    return order['status'] not in ('completed', 'cancelled')


def can_edit_order(order):
    """Данные получения (имя/адрес/время) можно править, пока заказ не
    завершён и не отменён — независимо от того, отправлены они уже
    администратору (blank_submitted) или ещё нет."""
    return order['status'] not in ('completed', 'cancelled')


def cancel_order(conn, order):
    if not can_cancel_order(order):
        raise ApiError(400, 'Этот заказ уже нельзя отменить')
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=%s", (order['id'],))
    conn.execute(
        'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
        (order['user_id'], 'Заказ отменён',
         f'Заказ №{order["order_number"]} отменён.', now_iso())
    )
    # Отмену тоже отправляем в очередь бота, чтобы админ увидел её в Telegram,
    # если бот запущен (см. bot_outbox для остальных событий заказа).
    conn.execute(
        "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES ('order_cancelled', %s, 0, %s)",
        (order['id'], now_iso())
    )
    conn.commit()
    finance_reverse_order_income(conn, order)


# ---------------------------------------------------------------------------
# Мини-бухгалтерия (/admin/finance) — доходы/расходы, маржа по заводам,
# курс USD. Полностью отделена от клиентской части: ничего из этого раздела
# не отдаётся не-админским эндпоинтам.
# ---------------------------------------------------------------------------
FINANCE_EXPENSE_CATEGORIES = {'factory', 'delivery', 'fuel', 'breakage', 'ads', 'other'}
DEFAULT_USD_RATE = 12700.0  # используется только если курс вообще ни разу не удалось получить


def get_or_create_factory(conn, name):
    name = (name or '').strip()
    if not name:
        return None
    row = conn.execute('SELECT id FROM factories WHERE name=%s', (name,)).fetchone()
    if row:
        return row['id']
    cur = conn.execute('INSERT INTO factories(name, created_at) VALUES (%s,%s)', (name, now_iso()))
    conn.commit()
    return cur.lastrowid


def create_delivery_quotes_for_order(conn, order_id):
    """Заказ создан в режиме «Доставка» — смотрим, товары СКОЛЬКИХ разных
    заводов в нём, и заводим по одной ожидающей строке на каждый завод.
    Каждый директор потом впишет стоимость доставки своей части лично
    боту (см. order_delivery_quotes) — итоговая доставка = сумма их
    ответов, применяется автоматически, когда ответили ВСЕ (см.
    set-delivery-quote в /internal/)."""
    rows = conn.execute(
        'SELECT DISTINCT p.factory_id, COALESCE(f.name, \'Без завода\') AS factory_name '
        'FROM order_items oi JOIN products p ON p.id = oi.product_id '
        'LEFT JOIN factories f ON f.id = p.factory_id WHERE oi.order_id=%s',
        (order_id,)
    ).fetchall()
    for r in rows:
        conn.execute(
            'INSERT INTO order_delivery_quotes(order_id, factory_id, factory_name, amount, created_at) '
            'VALUES (%s,%s,%s,NULL,%s)',
            (order_id, r['factory_id'], r['factory_name'], now_iso())
        )
    conn.commit()


def delivery_quotes_to_json(conn, order_id):
    rows = conn.execute(
        'SELECT q.id, q.factory_id, q.factory_name, q.amount, f.telegram_chat_id '
        'FROM order_delivery_quotes q LEFT JOIN factories f ON f.id = q.factory_id '
        'WHERE q.order_id=%s ORDER BY q.id ASC',
        (order_id,)
    ).fetchall()
    return [
        {
            'factory_id': r['factory_id'],
            'factory_name': r['factory_name'],
            'telegram_chat_id': r['telegram_chat_id'],
            'amount': r['amount'],
            'submitted': r['amount'] is not None,
        }
        for r in rows
    ]


def _backfill_finance_defaults(conn):
    rows = conn.execute('SELECT id, brand FROM products WHERE factory_id IS NULL').fetchall()
    for r in rows:
        fid = get_or_create_factory(conn, r['brand'] or 'Без завода')
        conn.execute('UPDATE products SET factory_id=%s WHERE id=%s', (fid, r['id']))
    # Закупочная цена ещё не задана владельцем — временно приравниваем к
    # продажной (base_price), чтобы отчёты не падали, маржа при этом = 0,
    # пока в /admin/finance не впишут реальный закуп.
    conn.execute('UPDATE products SET buy_price = base_price WHERE buy_price IS NULL')
    conn.commit()


# --- курс USD -> UZS, обновляется фоновым потоком раз в минуту -------------
def _fetch_usd_rate_from_network():
    """Возвращает float-курс USD в суммах или None, если ни один источник
    не ответил (тогда снаружи используется последний сохранённый курс)."""
    try:
        req = urllib.request.Request(
            'https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/',
            headers={'User-Agent': 'StroyAI-Finance/1.0'}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if isinstance(data, list) and data:
            return float(data[0]['Rate'])
    except Exception as e:  # noqa: BLE001
        print(f'[finance] Курс ЦБ РУз недоступен, пробуем резервный источник: {e}')
    try:
        with urllib.request.urlopen('https://open.er-api.com/v6/latest/USD', timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        rate = (data.get('rates') or {}).get('UZS')
        if rate:
            return float(rate)
    except Exception as e:  # noqa: BLE001
        print(f'[finance] Резервный источник курса тоже недоступен: {e}')
    return None


def get_usd_rate(conn):
    rate_row = conn.execute("SELECT value FROM finance_settings WHERE key='usd_rate'").fetchone()
    updated_row = conn.execute("SELECT value FROM finance_settings WHERE key='usd_rate_updated_at'").fetchone()
    rate = float(rate_row['value']) if rate_row and rate_row['value'] else DEFAULT_USD_RATE
    updated_at = updated_row['value'] if updated_row else None
    return rate, updated_at


def set_usd_rate(conn, rate):
    now = now_iso()
    conn.execute(
        "INSERT INTO finance_settings(key, value) VALUES ('usd_rate', %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value RETURNING key", (str(rate),)
    )
    conn.execute(
        "INSERT INTO finance_settings(key, value) VALUES ('usd_rate_updated_at', %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value RETURNING key", (now,)
    )
    conn.commit()
    return rate, now


def _usd_rate_updater_loop():
    """Фоновый поток: раз в минуту обновляет курс USD. Если сеть недоступна —
    просто оставляет в базе последний успешно сохранённый курс (п.10 ТЗ)."""
    while True:
        try:
            rate = _fetch_usd_rate_from_network()
            if rate:
                conn = db()
                try:
                    set_usd_rate(conn, rate)
                finally:
                    conn.close()
        except Exception as e:  # noqa: BLE001 — поток не должен падать
            print(f'[finance] Ошибка обновления курса USD: {e}')
        time.sleep(60)


def _start_usd_rate_updater():
    threading.Thread(target=_usd_rate_updater_loop, daemon=True).start()


def money_obj(conn, amount_uzs):
    """Единый формат денежного значения для API: сумма в UZS и USD + курс."""
    amount_uzs = round(amount_uzs or 0)
    rate, updated_at = get_usd_rate(conn)
    amount_usd = round(amount_uzs / rate, 2) if rate else None
    return {
        'amount_uzs': amount_uzs,
        'amount_usd': amount_usd,
        'currency_rate_usd': rate,
        'rate_updated_at': updated_at,
    }


# --- доход/снапшот при оплате заказа ---------------------------------------
def _finance_snapshot_order_items(conn, order):
    """Фиксирует buy/sell/маржу по каждой позиции заказа — один раз на заказ,
    независимо от того, сколько income-событий у заказа впоследствии будет
    (предоплата + остаток при завершении)."""
    exists = conn.execute('SELECT 1 FROM finance_order_snapshots WHERE order_id=%s LIMIT 1', (order['id'],)).fetchone()
    if exists:
        return
    items = conn.execute(
        'SELECT oi.product_id, oi.quantity, oi.price, p.name AS p_name, p.factory_id, p.buy_price, '
        'f.name AS factory_name FROM order_items oi '
        'LEFT JOIN products p ON p.id = oi.product_id '
        'LEFT JOIN factories f ON f.id = p.factory_id '
        'WHERE oi.order_id=%s', (order['id'],)
    ).fetchall()
    if not items:
        return
    # Раньше здесь был INSERT ВНУТРИ ЦИКЛА — отдельный запрос к базе на КАЖДУЮ
    # позицию заказа. Именно это и делало подтверждение оплаты из админки
    # таким долгим (8-10 сек): один клик "Отметить оплаченным" внутри себя
    # запускал по запросу на каждый товар в заказе. Теперь — один INSERT
    # с несколькими VALUES сразу на весь заказ, независимо от числа позиций.
    now = now_iso()
    value_groups = []
    params = []
    for it in items:
        qty = it['quantity'] or 0
        sell_price = it['price'] or 0
        # Если товар с тех пор удалили или у него ещё нет buy_price — считаем
        # закуп равным продаже (маржа 0), лишь бы отчёт не падал.
        buy_price = it['buy_price'] if it['buy_price'] is not None else sell_price
        line_sell = sell_price * qty
        line_buy = buy_price * qty
        value_groups.append('(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)')
        params.extend([
            order['id'], it['product_id'], it['p_name'] or 'Товар', it['factory_id'],
            it['factory_name'] or 'Без завода', qty, buy_price, sell_price, line_buy, line_sell,
            line_sell - line_buy, now,
        ])
    conn.execute(
        'INSERT INTO finance_order_snapshots(order_id, product_id, product_name, factory_id, factory_name, '
        'qty, buy_price, sell_price, line_buy_total, line_sell_total, line_margin, created_at) VALUES '
        + ','.join(value_groups),
        params
    )


def _finance_insert_income(conn, order, amount, event_id, note):
    """Идемпотентно (по event_id) создаёт доход и, при первом income заказа,
    снапшот его позиций."""
    if not amount or amount <= 0:
        return
    already = conn.execute('SELECT 1 FROM finance_transactions WHERE event_id=%s', (event_id,)).fetchone()
    if already:
        return
    conn.execute(
        "INSERT INTO finance_transactions(type, amount_uzs, order_id, category, note, event_id, created_at, created_by) "
        "VALUES ('income', %s, %s, NULL, %s, %s, %s, 'system')",
        (amount, order['id'], note, event_id, now_iso())
    )
    _finance_snapshot_order_items(conn, order)
    conn.commit()


def finance_record_income_on_payment_confirm(conn, order):
    """Вызывается при подтверждении оплаты (apply_payment_decision approved=True).
    Если у заказа есть предоплата (prepay_amount>0) — доход = именно она,
    остаток фиксируется отдельным событием при завершении заказа (см. ниже).
    Если предоплаты нет (prepay_amount=0) — считаем, что подтверждена оплата
    полной суммы сразу."""
    amount_now = order['prepay_amount'] if order['prepay_amount'] else order['total']
    _finance_insert_income(
        conn, order, amount_now, f"order_paid_{order['id']}",
        f"Оплата заказа №{order['order_number']}"
    )


def finance_record_income_on_completion(conn, order):
    """Вызывается при переводе заказа в статус 'completed'. Если ранее была
    учтена только предоплата — сейчас доучитывается остаток (обычно наличными
    при получении). Если оплата вообще не подтверждалась через отдельный шаг
    (payment_status не 'paid') — считаем, что вся сумма получена сейчас
    (модель «оплата при получении»)."""
    prepay_amount = order['prepay_amount'] or 0
    total = order['total'] or 0
    if order['payment_status'] == 'paid' and prepay_amount > 0:
        remainder = total - prepay_amount
        if remainder > 0:
            _finance_insert_income(
                conn, order, remainder, f"order_completed_remainder_{order['id']}",
                f"Остаток по заказу №{order['order_number']} при завершении"
            )
    elif order['payment_status'] != 'paid':
        _finance_insert_income(
            conn, order, total, f"order_completed_full_{order['id']}",
            f"Оплата заказа №{order['order_number']} при завершении (наличными/при получении)"
        )


def finance_reverse_order_income(conn, order):
    """Сторно при отмене заказа: не удаляет историю, а добавляет
    компенсирующую отрицательную транзакцию на всю ранее учтённую по этому
    заказу сумму дохода. Идемпотентно — повторная отмена ничего не создаст."""
    event_id = f"order_refund_{order['id']}"
    already = conn.execute('SELECT 1 FROM finance_transactions WHERE event_id=%s', (event_id,)).fetchone()
    if already:
        return
    total_income = conn.execute(
        "SELECT COALESCE(SUM(amount_uzs),0) s FROM finance_transactions WHERE order_id=%s AND type='income'",
        (order['id'],)
    ).fetchone()['s']
    if not total_income:
        return
    conn.execute(
        "INSERT INTO finance_transactions(type, amount_uzs, order_id, category, note, event_id, created_at, created_by) "
        "VALUES ('income', %s, %s, NULL, %s, %s, %s, 'system')",
        (-total_income, order['id'], f"Сторно (отмена) заказа №{order['order_number']}", event_id, now_iso())
    )
    conn.commit()


# --- отчёты ------------------------------------------------------------
def _finance_period_bounds(query):
    today = datetime.now(timezone.utc).date()
    date_from = (query.get('from') or [None])[0] or (today - timedelta(days=29)).isoformat()
    date_to = (query.get('to') or [None])[0] or today.isoformat()
    ts_from = f'{date_from}T00:00:00'
    ts_to = f'{date_to}T23:59:59.999999'
    return date_from, date_to, ts_from, ts_to


def finance_summary(conn, ts_from, ts_to, date_from, date_to):
    income = conn.execute(
        "SELECT COALESCE(SUM(amount_uzs),0) s FROM finance_transactions "
        "WHERE type='income' AND created_at BETWEEN %s AND %s", (ts_from, ts_to)
    ).fetchone()['s']
    expense = conn.execute(
        "SELECT COALESCE(SUM(amount_uzs),0) s FROM finance_transactions "
        "WHERE type='expense' AND created_at BETWEEN %s AND %s", (ts_from, ts_to)
    ).fetchone()['s']
    net = income - expense
    orders_paid_row = conn.execute(
        "SELECT COUNT(DISTINCT order_id) c, COALESCE(AVG(amount_uzs),0) a FROM finance_transactions "
        "WHERE type='income' AND order_id IS NOT NULL AND amount_uzs>0 AND created_at BETWEEN %s AND %s",
        (ts_from, ts_to)
    ).fetchone()
    exp_by_cat_rows = conn.execute(
        "SELECT COALESCE(category,'other') category, COALESCE(SUM(amount_uzs),0) s FROM finance_transactions "
        "WHERE type='expense' AND created_at BETWEEN %s AND %s GROUP BY COALESCE(category,'other')",
        (ts_from, ts_to)
    ).fetchall()
    income_by_day_rows = conn.execute(
        "SELECT LEFT(created_at,10) AS day, COALESCE(SUM(amount_uzs),0) s FROM finance_transactions "
        "WHERE type='income' AND created_at BETWEEN %s AND %s GROUP BY LEFT(created_at,10) ORDER BY day",
        (ts_from, ts_to)
    ).fetchall()
    # Отменённые заказы исключаем из маржи по товару (сама история снапшота
    # не удаляется — просто не участвует в отчёте, т.к. дохода по ней больше нет).
    margin_row = conn.execute(
        "SELECT COALESCE(SUM(s.line_buy_total),0) buy_sum, COALESCE(SUM(s.line_margin),0) margin_sum "
        "FROM finance_order_snapshots s JOIN orders o ON o.id = s.order_id "
        "WHERE s.created_at BETWEEN %s AND %s AND o.status <> 'cancelled'", (ts_from, ts_to)
    ).fetchone()
    delivery_row = conn.execute(
        "SELECT COALESCE(SUM(delivery_fee),0) client_sum, COALESCE(SUM(delivery_real_cost),0) real_sum "
        "FROM orders WHERE payment_status='paid' AND COALESCE(paid_at, created_at) BETWEEN %s AND %s",
        (ts_from, ts_to)
    ).fetchone()
    return {
        'period': {'from': date_from, 'to': date_to},
        'total_income': money_obj(conn, income),
        'total_expense': money_obj(conn, expense),
        'net': money_obj(conn, net),
        'expected_balance': money_obj(conn, net),
        'orders_paid_count': orders_paid_row['c'],
        'average_check': money_obj(conn, orders_paid_row['a']),
        'expenses_by_category': [
            {'category': r['category'], 'amount': money_obj(conn, r['s'])} for r in exp_by_cat_rows
        ],
        'income_by_day': [{'day': r['day'], 'amount': money_obj(conn, r['s'])} for r in income_by_day_rows],
        # Разбивка из п.6 ТЗ («важно показывать отдельно»):
        'gross_turnover_clients': money_obj(conn, income),
        'goods_buy_sum': money_obj(conn, margin_row['buy_sum']),
        'goods_margin': money_obj(conn, margin_row['margin_sum']),
        'delivery_client_sum': money_obj(conn, delivery_row['client_sum']),
        'delivery_real_cost_sum': money_obj(conn, delivery_row['real_sum']),
    }


def finance_by_factory(conn, ts_from, ts_to):
    rows = conn.execute(
        "SELECT s.factory_id, COALESCE(s.factory_name,'Без завода') factory_name, "
        "COUNT(DISTINCT s.order_id) orders_count, COALESCE(SUM(s.qty),0) units_sold, "
        "COALESCE(SUM(s.line_buy_total),0) buy_sum, COALESCE(SUM(s.line_sell_total),0) sell_sum, "
        "COALESCE(SUM(s.line_margin),0) margin_sum FROM finance_order_snapshots s "
        "JOIN orders o ON o.id = s.order_id "
        "WHERE s.created_at BETWEEN %s AND %s AND o.status <> 'cancelled' "
        "GROUP BY s.factory_id, s.factory_name ORDER BY margin_sum DESC",
        (ts_from, ts_to)
    ).fetchall()
    total_margin = sum(r['margin_sum'] for r in rows)
    result = []
    for r in rows:
        share = round(r['margin_sum'] / total_margin * 100, 1) if total_margin else 0
        result.append({
            'factory_id': r['factory_id'],
            'factory_name': r['factory_name'],
            'orders_count': r['orders_count'],
            'units_sold': r['units_sold'],
            'buy_sum': money_obj(conn, r['buy_sum']),
            'sell_sum': money_obj(conn, r['sell_sum']),
            'margin_sum': money_obj(conn, r['margin_sum']),
            'margin_share_percent': share,
        })
    def _top(key_fn):
        best = max(result, key=key_fn, default=None)
        return best['factory_name'] if best else None
    return {
        'factories': result,
        'top_by_margin': _top(lambda x: x['margin_sum']['amount_uzs']),
        'top_by_turnover': _top(lambda x: x['sell_sum']['amount_uzs']),
        'top_by_orders': _top(lambda x: x['orders_count']),
    }


def _batch_categories(conn, category_ids):
    """Одним запросом получает категории для целого списка товаров — вместо
    отдельного SELECT на каждый товар в цикле. Критично при высокой задержке
    до базы (например, подключение из Узбекистана к Neon в США): один лишний
    запрос на КАЖДЫЙ товар каталога/корзины/заказа умножает время ожидания
    на количество позиций."""
    ids = list({cid for cid in category_ids if cid is not None})
    if not ids:
        return {}
    rows = conn.execute('SELECT id, name, name_uz, slug FROM categories WHERE id = ANY(%s)', (ids,)).fetchall()
    return {r['id']: r for r in rows}


def _batch_review_stats(conn, product_ids):
    """Аналогично — количество и средний рейтинг отзывов для целого списка
    товаров одним запросом вместо одного на товар."""
    ids = list({pid for pid in product_ids if pid is not None})
    if not ids:
        return {}
    rows = conn.execute(
        'SELECT product_id, COUNT(*) c, AVG(rating) avg_r FROM reviews '
        'WHERE product_id = ANY(%s) GROUP BY product_id', (ids,)
    ).fetchall()
    return {r['product_id']: r for r in rows}


def product_to_json(conn, row, mode='delivery', category=None, review_stats=None):
    # category/review_stats можно передать заранее (см. _batch_categories,
    # _batch_review_stats) — тогда лишних запросов к базе не будет вообще.
    # Если не передали (одиночный лукап одного товара) — считаем как раньше.
    if category is None:
        category = conn.execute(
            'SELECT id, name, name_uz, slug FROM categories WHERE id=%s', (row['category_id'],)
        ).fetchone()
    if review_stats is None:
        review_stats = conn.execute(
            'SELECT COUNT(*) c, AVG(rating) avg_r FROM reviews WHERE product_id=%s', (row['id'],)
        ).fetchone()
    reviews_count = review_stats['c']
    # Пока у товара нет ни одного отзыва — показываем 5.0. Как только появляются
    # реальные отзывы от покупателей — рейтинг считается как их среднее.
    rating = round(review_stats['avg_r'], 1) if reviews_count > 0 else 5.0
    base_price = row['base_price'] if row['base_price'] else row['price']
    return {
        'id': row['id'],
        'name': row['name'],
        'name_uz': row['name_uz'],
        'brand': row['brand'],
        'price': round(base_price * coef_for_mode(mode)),  # цена в текущем режиме (доставка по умолчанию)
        'price_delivery': round(base_price * MARKUP_DELIVERY),
        'price_pickup': round(base_price * MARKUP_PICKUP),
        'unit': row['unit'],
        'weight_kg': row['weight_kg'],
        'color': row['color'],
        'rating': rating,
        'reviews_count': reviews_count,
        'description': row['description'],
        'description_uz': row['description_uz'],
        'specs': json.loads(row['specs_json'] or '{}'),
        'specs_uz': json.loads(row['specs_json_uz']) if row['specs_json_uz'] else None,
        'image': row['image'],
        'in_stock': bool(row['in_stock']),
        'category': {'id': category['id'], 'name': category['name'], 'name_uz': category['name_uz'], 'slug': category['slug']},
        'created_at': row['created_at'],
    }


def user_to_json(row):
    return {
        'id': row['id'],
        'email': row['email'],
        'phone': row['phone'],
        'first_name': row['first_name'],
        'last_name': row['last_name'],
        'address': row['address'],
    }


def gen_code(n=6):
    return ''.join(secrets.choice(string.digits) for _ in range(n))


def send_otp_email(to_address, code):
    """
    Отправляет код подтверждения через HTTP API Brevo (не SMTP — см. пояснение
    выше про блокировку SMTP на Railway). Возвращает True, если письмо реально
    ушло; False — если API-ключ не настроен или отправка не удалась (в обоих
    случаях код всё равно печатается в консоль, чтобы тестировать можно было
    в любом случае).
    """
    if not BREVO_API_KEY or not EMAIL_HOST_USER:
        print('[email] BREVO_API_KEY/EMAIL_HOST_USER не заданы — письмо не отправлено, код только в консоли.')
        return False
    payload = json.dumps({
        'sender': {'name': EMAIL_FROM_NAME, 'email': EMAIL_HOST_USER},
        'to': [{'email': to_address}],
        'subject': f'Код подтверждения: {code}',
        'textContent': (
            f'Ваш код подтверждения: {code}\n\nОн действует 5 минут.\n\n'
            f'Если вы не запрашивали вход — просто проигнорируйте это письмо.'
        ),
    }).encode('utf-8')
    req = urllib.request.Request(
        BREVO_API_URL,
        data=payload,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'api-key': BREVO_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print(f'[email] Письмо с кодом успешно отправлено на {to_address} (через Brevo).')
        return True
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            pass
        # Самая частая причина 401/403: отправитель EMAIL_HOST_USER не
        # подтверждён в Brevo (Settings → Senders) или API-ключ неверный/отозван.
        print(f'[email] Brevo отказал в отправке на {to_address} '
              f'(HTTP {e.code}): {body}. Проверьте: 1) подтверждён ли отправитель '
              f'{EMAIL_HOST_USER} в Brevo (Settings → Senders, Domains, IPs → Senders); '
              f'2) действителен ли BREVO_API_KEY.')
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f'[email] Не удалось отправить письмо на {to_address} '
              f'({type(e).__name__}): {e}')
        return False
    except Exception as e:  # noqa: BLE001 — не роняем сервер из-за проблем с почтой
        print(f'[email] Неожиданная ошибка при отправке на {to_address} '
              f'({type(e).__name__}): {e}')
        return False


def gen_token():
    return secrets.token_urlsafe(32)


def cart_to_json(conn, user_id, mode='delivery'):
    """
    Корзина в выбранном режиме (пункт 2 ТЗ). Цены всегда считаются на лету из
    base_price товара — поэтому переключение режима в корзине мгновенно
    пересчитывает и позиции, и итог, без похода в базу за 'сохранённой' ценой.
    При mode='delivery' отдельно считается и возвращается стоимость доставки
    (по суммарному весу корзины); при 'pickup' доставка = 0 и блок скрывается
    на фронтенде.
    """
    mode = 'pickup' if mode == 'pickup' else 'delivery'
    rows = conn.execute(
        'SELECT ci.id as item_id, ci.quantity, p.* FROM cart_items ci '
        'JOIN products p ON p.id = ci.product_id WHERE ci.user_id=%s', (user_id,)
    ).fetchall()
    # Раньше каждая позиция корзины делала ЕЩЁ 2 отдельных запроса
    # (категория + статистика отзывов) внутри product_to_json — при высокой
    # задержке до базы (например, подключение из другой страны) это и
    # превращало добавление товара в корзину в 5-10 секунд ожидания.
    # Забираем то же самое двумя запросами на ВСЮ корзину сразу.
    categories = _batch_categories(conn, (r['category_id'] for r in rows))
    review_stats = _batch_review_stats(conn, (r['id'] for r in rows))
    items = []
    total = 0
    count = 0
    weight_total = 0.0
    for r in rows:
        p = product_to_json(
            conn, r, mode=mode,
            category=categories.get(r['category_id']),
            review_stats=review_stats.get(r['id']) or {'c': 0, 'avg_r': None},
        )
        line_total = p['price'] * r['quantity']
        total += line_total
        count += r['quantity']
        weight_total += (r['weight_kg'] or 0) * r['quantity']
        items.append({'id': r['item_id'], 'product': p, 'quantity': r['quantity'], 'line_total': line_total})

    if mode == 'delivery' and items:
        delivery_fee, real_cost = calc_delivery_fee(weight_total)
    else:
        delivery_fee, real_cost = 0, 0

    return {
        'items': items,
        'mode': mode,
        'total': total,
        'count': count,
        'weight_kg': round(weight_total, 1),
        'delivery_fee': delivery_fee,
        'delivery_real_cost': real_cost,
        'grand_total': total + delivery_fee,
    }


def order_to_json(conn, row):
    items = conn.execute('SELECT * FROM order_items WHERE order_id=%s', (row['id'],)).fetchall()
    # Раньше на КАЖДУЮ позицию заказа уходило по 3 отдельных запроса (сам
    # товар + его категория + статистика отзывов) — заказ с несколькими
    # разными товарами превращался в десятки последовательных обращений
    # к базе. Теперь — то же самое общим числом запросов, не зависящим от
    # количества позиций: один на все товары сразу, ещё два батчем на их
    # категории/отзывы.
    product_ids = list({it['product_id'] for it in items})
    products_map = {}
    if product_ids:
        prows = conn.execute('SELECT * FROM products WHERE id = ANY(%s)', (product_ids,)).fetchall()
        products_map = {p['id']: p for p in prows}
    categories = _batch_categories(conn, (p['category_id'] for p in products_map.values()))
    review_stats = _batch_review_stats(conn, product_ids)
    item_list = []
    for it in items:
        prow = products_map.get(it['product_id'])
        item_list.append({
            'product': product_to_json(
                conn, prow,
                category=categories.get(prow['category_id']) if prow else None,
                review_stats=(review_stats.get(it['product_id']) or {'c': 0, 'avg_r': None}) if prow else None,
            ) if prow else None,
            'quantity': it['quantity'],
            'price': it['price'],
            'weight_kg': it['weight_kg'],
            'line_total': it['price'] * it['quantity'],
            'line_weight_kg': it['weight_kg'] * it['quantity'],
        })
    user = conn.execute('SELECT * FROM users WHERE id=%s', (row['user_id'],)).fetchone()
    customer_name = ' '.join(filter(None, [user['first_name'], user['last_name']])).strip() or 'Клиент'
    order_number = row['order_number']
    mode = row['mode'] or 'delivery'
    payment_status = row['payment_status'] or 'awaiting_payment'
    pay_url = f'https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_number}' \
        if (TELEGRAM_BOT_USERNAME and order_number) else None

    delivery_quoted = bool(row['delivery_quoted'])
    blank_submitted = bool(row['blank_submitted'])
    # Пока способ получения — «доставка» и админ ещё не указал её стоимость
    # в боте, бланк оплаты клиенту не показываем (пункт 3.А ТЗ).
    awaiting_delivery_quote = (mode == 'delivery' and not delivery_quoted)
    can_pay = delivery_quoted and not blank_submitted

    weight_total = sum((it['weight_kg'] or 0) * it['quantity'] for it in items)
    estimated_delivery_fee = None
    if awaiting_delivery_quote:
        estimated_delivery_fee, _ = calc_delivery_fee(weight_total)
    delivery_quotes = delivery_quotes_to_json(conn, row['id']) if mode == 'delivery' else []

    steps = [{'key': k, 'label': STATUS_LABELS[k]} for k in order_step_keys(row)]

    return {
        'id': row['id'],
        'order_number': order_number,
        'mode': mode,
        'mode_display': 'Доставка' if mode == 'delivery' else 'Самовывоз',
        'status': row['status'],
        'status_display': STATUS_LABELS.get(row['status'], row['status']),
        'needs_crew': bool(row['needs_crew']),
        'steps': steps,
        'payment_status': payment_status,
        'payment_status_display': PAYMENT_STATUS_LABELS.get(payment_status, payment_status),
        'pay_url': pay_url,
        'total': row['total'],
        'delivery_fee': row['delivery_fee'],
        'delivery_real_cost': row['delivery_real_cost'],
        'delivery_quoted': delivery_quoted,
        'awaiting_delivery_quote': awaiting_delivery_quote,
        'estimated_delivery_fee': estimated_delivery_fee,
        'delivery_quotes': delivery_quotes,
        'blank_submitted': blank_submitted,
        'can_pay': can_pay,
        'can_cancel': can_cancel_order(row),
        'can_edit': can_edit_order(row),
        'customer_name': row['customer_name'],
        'delivery_address': row['delivery_address'],
        'desired_time': row['desired_time'],
        'payment_card_number': PAYMENT_CARD_NUMBER,
        'payment_card_holder': PAYMENT_CARD_HOLDER,
        'pickup_address': PICKUP_ADDRESS if mode == 'pickup' else None,
        'pickup_hours': PICKUP_HOURS if mode == 'pickup' else None,
        'pickup_ready_minutes': PICKUP_READY_MINUTES if mode == 'pickup' else None,
        'prepay_percent': row['prepay_percent'],
        'prepay_amount': row['prepay_amount'],
        'is_prepaid': bool(row['is_prepaid']),
        'items': item_list,
        'created_at': row['created_at'],
        'customer': {
            'name': customer_name,
            'phone': user['phone'],
            'email': user['email'],
            'address': user['address'],
        },
    }


def ai_design_to_json(row):
    try:
        images = json.loads(row['images_json'] or '[]')
    except json.JSONDecodeError:
        images = []
    return {
        'id': row['id'],
        'prompt': row['prompt'],
        'status': row['status'],
        'images': images,
        'error': row['error'],
        'created_at': row['created_at'],
        # Нужны фронту, чтобы посчитать точный SVG-план этажа (см.
        # buildFloorPlan() в index.html) — как для только что созданной
        # генерации, так и заново для каждой записи в истории.
        'size_m2': row['size_m2'],
        'bedrooms': row['bedrooms'],
    }


# ---------------------------------------------------------------------------
# HTTP-обработчик
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    # Без этого (по умолчанию у http.server — HTTP/1.0) браузер открывает
    # НОВОЕ TCP-соединение на КАЖДЫЙ запрос: на каждую картинку товара,
    # на каждый вызов API, на каждый шрифт. На странице каталога это легко
    # 10-20 отдельных "с нуля" подключений вместо переиспользования одного.
    # HTTP/1.1 включает keep-alive — соединение переиспользуется между
    # запросами. Безопасно, потому что Content-Length уже проставляется
    # на всех путях ответа (JSON/HTML/статика) — это единственное жёсткое
    # требование для keep-alive.
    protocol_version = 'HTTP/1.1'
    server_version = 'RenoMVP/1.0'

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

    # --- низкоуровневые помощники -----------------------------------------
    def _accepts_gzip(self):
        return 'gzip' in (self.headers.get('Accept-Encoding', '') or '')

    def _write_body(self, body, extra_headers=(), allow_gzip=True):
        """Общая точка отправки тела ответа: сжимает gzip'ом, если браузер
        это поддерживает (почти всегда) и содержимое текстовое — меньше байт
        по сети, быстрее отрисовка, особенно на мобильном интернете. Для уже
        сжатых форматов (JPEG, WOFF и т.п.) gzip только тратит CPU впустую —
        такие вызовы передают allow_gzip=False."""
        if allow_gzip and self._accepts_gzip() and len(body) > 256:
            body = gzip.compress(body, compresslevel=6)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _cors_origin(self):
        """Возвращает Origin запроса, если он в белом списке (или локальный
        для разработки), иначе None — тогда заголовок CORS не отправляется
        и браузер сам заблокирует чтение ответа с чужого сайта."""
        origin = self.headers.get('Origin', '')
        if not origin:
            return None
        if origin in ALLOWED_ORIGINS:
            return origin
        if origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1'):
            return origin
        return None

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        origin = self._cors_origin()
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Admin-Token')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
        self._write_body(body)

    def _send_html_file(self, path):
        if not os.path.exists(path):
            self._send_json(404, {'detail': f'Файл не найден: {os.path.basename(path)}'})
            return
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self._write_body(body)

    def _read_json_body(self):
        # Тело уже целиком прочитано в _route() (см. комментарий там) —
        # здесь только парсим то, что уже лежит в буфере, повторно к сокету
        # не обращаемся.
        raw = getattr(self, '_raw_body', b'')
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            raise ApiError(400, 'Некорректный JSON в теле запроса')

    def _current_user(self, conn, required=True):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            if required:
                raise ApiError(401, 'Требуется авторизация')
            return None
        token = auth[len('Bearer '):].strip()
        row = conn.execute('SELECT * FROM tokens WHERE token=%s', (token,)).fetchone()
        if not row:
            if required:
                raise ApiError(401, 'Сессия недействительна, войдите заново')
            return None
        user = conn.execute('SELECT * FROM users WHERE id=%s', (row['user_id'],)).fetchone()
        if not user:
            raise ApiError(401, 'Пользователь не найден')
        return user

    def _current_admin(self, conn):
        token = self.headers.get('X-Admin-Token', '')
        if not token:
            raise ApiError(401, 'Требуется вход в панель администратора')
        row = conn.execute('SELECT * FROM admin_sessions WHERE token=%s', (token,)).fetchone()
        if not row:
            raise ApiError(401, 'Сессия администратора недействительна — войдите заново')
        return row

    # --- маршрутизация -------------------------------------------------
    def do_OPTIONS(self):
        self._send_json(200, {})

    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def do_PATCH(self):
        self._route('PATCH')

    def do_DELETE(self):
        self._route('DELETE')

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # КРИТИЧНО для HTTP/1.1 keep-alive: читаем тело запроса ЦЕЛИКОМ здесь,
        # сразу, ещё до всякой маршрутизации — независимо от того, нужно ли
        # оно вообще конкретному обработчику. Раньше тело читалось лениво,
        # только внутри тех обработчиков, что явно вызывали
        # _read_json_body() — а часть обработчиков сначала проверяет что-то
        # другое (например, существует ли заказ) и в случае ошибки выходит
        # ДО чтения тела. При HTTP/1.0 это было не страшно: соединение всё
        # равно рвалось после каждого ответа. С HTTP/1.1 keep-alive
        # непрочитанные байты тела остаются в сокете и ломают/подвешивают
        # СЛЕДУЮЩИЙ запрос на том же переиспользуемом соединении — именно
        # это могло быть причиной "каталог грузится вечно" после нескольких
        # предыдущих запросов на странице.
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            content_length = 0
        self._raw_body = self.rfile.read(content_length) if content_length > 0 else b''

        # Сайт: отдаём index.html на главной и на всём, что не начинается с /api
        if not path.startswith('/api/'):
            if path in ('/admin', '/admin/'):
                self._send_html_file(ADMIN_HTML_PATH)
                return
            if path in ('/', '') or not os.path.splitext(path)[1]:
                self._send_html_file(INDEX_HTML_PATH)
            else:
                # запрошен статический файл (картинка и т.п.) — отдаём как есть, если есть
                local_path = os.path.normpath(os.path.join(BASE_DIR, path.lstrip('/')))
                if not local_path.startswith(BASE_DIR):
                    self._send_json(404, {'detail': 'Не найдено'})
                    return
                if os.path.exists(local_path) and os.path.isfile(local_path):
                    mime, _ = mimetypes.guess_type(local_path)
                    with open(local_path, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', mime or 'application/octet-stream')
                    # Фото товаров/шрифты/hero-картинка практически не меняются
                    # между деплоями — держим в кэше браузера неделю вместо часа.
                    # gzip только для текстовых форматов (svg/css/js) — JPEG/WOFF
                    # уже сжаты, повторное сжатие тратит CPU без пользы.
                    compressible = (mime or '').startswith(('text/', 'image/svg')) or mime in (
                        'application/javascript', 'application/json'
                    )
                    self._write_body(
                        body,
                        extra_headers=[('Cache-Control', 'public, max-age=604800')],
                        allow_gzip=compressible,
                    )
                else:
                    self._send_json(404, {'detail': 'Не найдено'})
            return

        conn = None
        try:
            conn = db()
            payload = self._dispatch_api(method, path, query, conn)
            self._send_json(200, payload)
        except ApiError as e:
            self._send_json(e.status, {'detail': e.detail})
        except Exception as e:  # noqa: BLE001 — последняя линия обороны, чтобы сервер не падал
            print('ОШИБКА:', repr(e))
            self._send_json(500, {'detail': 'Внутренняя ошибка сервера'})
        finally:
            if conn is not None:
                conn.close()

    # --- сами эндпоинты --------------------------------------------------
    def _dispatch_api(self, method, path, query, conn):
        p = path[len('/api'):]  # убираем префикс /api для удобства сравнения

        if p == '/health/' and method == 'GET':
            return {'ok': True, 'service': 'stroyai-backend (simple)'}

        if p == '/categories/' and method == 'GET':
            rows = conn.execute('SELECT * FROM categories ORDER BY id').fetchall()
            return [{'id': r['id'], 'name': r['name'], 'name_uz': r['name_uz'], 'slug': r['slug']} for r in rows]

        if p == '/store-stats/' and method == 'GET':
            # Публичная сводка для главной страницы клиентского приложения:
            # сколько товаров продано (сумма количеств в завершённых заказах),
            # сколько позиций в каталоге и сколько клиентов зарегистрировано.
            # Ничего приватного не отдаём — только агрегированные числа.
            products_count = conn.execute(
                'SELECT COUNT(*) c FROM products WHERE in_stock=1'
            ).fetchone()['c']
            sold_row = conn.execute(
                "SELECT COALESCE(SUM(oi.quantity),0) s FROM order_items oi "
                "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed'"
            ).fetchone()
            orders_row = conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE status = 'completed'"
            ).fetchone()
            clients_row = conn.execute('SELECT COUNT(*) c FROM users').fetchone()
            return {
                'products_count': products_count,
                'sold_count': int(sold_row['s'] or 0),
                'completed_orders_count': orders_row['c'],
                'clients_count': clients_row['c'],
            }

        if p == '/products/' and method == 'GET':
            sql = 'SELECT p.* FROM products p JOIN categories c ON c.id=p.category_id WHERE 1=1'
            params = []
            cat = (query.get('category') or [None])[0]
            if cat and cat != 'all':
                sql += ' AND c.slug=%s'
                params.append(cat)
            search = (query.get('search') or [None])[0]
            if search:
                sql += ' AND p.name ILIKE %s'
                params.append(f'%{search}%')
            sql += ' ORDER BY p.id'
            rows = conn.execute(sql, params).fetchall()
            mode = (query.get('mode') or ['delivery'])[0]
            # Было: 2 отдельных запроса НА КАЖДЫЙ товар (категория + отзывы)
            # внутри product_to_json — для каталога из ~28 товаров это ~57
            # последовательных обращений к базе. При задержке до Neon
            # в доли секунды на каждый запрос это и превращало открытие
            # каталога в "грузится вечно". Теперь — 2 запроса на весь каталог.
            categories = _batch_categories(conn, (r['category_id'] for r in rows))
            review_stats = _batch_review_stats(conn, (r['id'] for r in rows))
            return [
                product_to_json(
                    conn, r, mode=mode,
                    category=categories.get(r['category_id']),
                    review_stats=review_stats.get(r['id']) or {'c': 0, 'avg_r': None},
                ) for r in rows
            ]

        m = re.match(r'^/products/(\d+)/$', p)
        if m and method == 'GET':
            row = conn.execute('SELECT * FROM products WHERE id=%s', (m.group(1),)).fetchone()
            if not row:
                raise ApiError(404, 'Товар не найден')
            mode = (query.get('mode') or ['delivery'])[0]
            return product_to_json(conn, row, mode=mode)

        m = re.match(r'^/products/(\d+)/reviews/$', p)
        if m and method == 'GET':
            rows = conn.execute(
                'SELECT * FROM reviews WHERE product_id=%s ORDER BY id DESC', (m.group(1),)
            ).fetchall()
            return [{'id': r['id'], 'author_name': r['author_name'], 'rating': r['rating'],
                      'text': r['text'], 'created_at': r['created_at']} for r in rows]

        if p == '/faq/' and method == 'GET':
            rows = conn.execute('SELECT * FROM faq ORDER BY id').fetchall()
            return [{'question': r['question'], 'answer': r['answer']} for r in rows]

        if p == '/support/chat/' and method == 'POST':
            body = self._read_json_body()
            text = (body.get('message') or '').lower()
            faqs = conn.execute('SELECT * FROM faq').fetchall()
            reply = 'Спасибо за обращение! Наш специалист свяжется с вами в ближайшее время.'
            for f in faqs:
                if any(w in text for w in ['заказ', 'доставк', 'возврат', 'оплат']) and \
                   any(w in f['question'].lower() for w in ['заказ', 'доставк', 'возврат', 'оплат']):
                    reply = f['answer']
                    break
            return {'reply': reply}

        # --- авторизация по OTP ---------------------------------------
        if p == '/register/' and method == 'POST':
            body = self._read_json_body()
            first_name = (body.get('first_name') or '').strip()
            last_name = (body.get('last_name') or '').strip()
            email = (body.get('email') or '').strip() or None
            phone = (body.get('phone') or '').strip() or None
            if not first_name:
                raise ApiError(400, 'Укажите имя')
            if not email and not phone:
                raise ApiError(400, 'Укажите email или телефон')
            if email and conn.execute('SELECT id FROM users WHERE email=%s', (email,)).fetchone():
                raise ApiError(400, 'Аккаунт с таким email уже существует, войдите вместо регистрации')
            if phone and conn.execute('SELECT id FROM users WHERE phone=%s', (phone,)).fetchone():
                raise ApiError(400, 'Аккаунт с таким телефоном уже существует, войдите вместо регистрации')
            cur = conn.execute(
                'INSERT INTO users(email, phone, first_name, last_name, created_at) VALUES (%s,%s,%s,%s,%s)',
                (email, phone, first_name, last_name, now_iso())
            )
            conn.execute(
                'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
                (cur.lastrowid, 'Добро пожаловать!', 'Спасибо, что зарегистрировались в StroyAI.', now_iso())
            )
            conn.commit()
            user = conn.execute('SELECT * FROM users WHERE id=%s', (cur.lastrowid,)).fetchone()
            return user_to_json(user)

        if p == '/otp/request/' and method == 'POST':
            body = self._read_json_body()
            channel = body.get('channel')
            destination = (body.get('destination') or '').strip()
            if channel not in ('email', 'sms') or not destination:
                raise ApiError(400, 'Некорректные данные')

            # Простой троттлинг: не чаще одного нового кода в 60 секунд на
            # один и тот же email/телефон — без этого можно было засыпать
            # почту сотнями писем (или забить дневной лимит Brevo) одним
            # скриптом. Не защищает от смены IP/почты, но закрывает самый
            # дешёвый и очевидный вид спама/DoS.
            recent = conn.execute(
                "SELECT created_at FROM otp_codes WHERE channel=%s AND destination=%s "
                "ORDER BY id DESC LIMIT 1", (channel, destination)
            ).fetchone()
            if recent:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(recent['created_at'])).total_seconds()
                if elapsed < 60:
                    raise ApiError(429, f'Код уже отправлен, подождите {int(60 - elapsed)} сек. перед повторной отправкой')

            col = 'email' if channel == 'email' else 'phone'
            existing_user = conn.execute(f'SELECT id FROM users WHERE {col}=%s', (destination,)).fetchone()
            if not existing_user:
                raise ApiError(
                    404,
                    'Аккаунт с таким ' + ('email' if channel == 'email' else 'номером телефона') +
                    ' не найден. Сначала зарегистрируйтесь.'
                )

            code = gen_code()
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            conn.execute(
                'INSERT INTO otp_codes(channel, destination, code, created_at, expires_at, used) '
                'VALUES (%s,%s,%s,%s,%s,0)', (channel, destination, code, now_iso(), expires)
            )
            conn.commit()

            sent_for_real = False
            if channel == 'email':
                sent_for_real = send_otp_email(destination, code)
            if not sent_for_real:
                # SMS ещё не подключён, либо email не настроен (нет App Password) —
                # печатаем код в консоль, чтобы можно было тестировать без реальной отправки.
                print(f'\n>>> ОДНОРАЗОВЫЙ КОД для {destination} ({channel}): {code}\n')

            detail = 'Код отправлен на почту' if sent_for_real else 'Код отправлен (см. консоль сервера)'
            return {'detail': detail, 'expires_in': 300}

        if p == '/otp/verify/' and method == 'POST':
            body = self._read_json_body()
            channel = body.get('channel')
            destination = (body.get('destination') or '').strip()
            code = (body.get('code') or '').strip()
            row = conn.execute(
                'SELECT * FROM otp_codes WHERE channel=%s AND destination=%s AND used=0 '
                'ORDER BY id DESC LIMIT 1', (channel, destination)
            ).fetchone()
            if not row:
                raise ApiError(400, 'Код не найден, запросите новый')
            if datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
                raise ApiError(400, 'Код истёк, запросите новый')
            # Лимит попыток на один код — без этого 6-значный код можно
            # подобрать простым перебором за отведённые ему 5 минут жизни,
            # никак не ограничиваясь сервером. 5 попыток на код достаточно
            # для реальной опечатки, но не оставляет практического шанса
            # перебору (1 из 1 000 000 максимум за 5 попыток).
            if row['attempts'] >= 5:
                raise ApiError(429, 'Слишком много неверных попыток для этого кода, запросите новый')
            if row['code'] != code:
                conn.execute('UPDATE otp_codes SET attempts = attempts + 1 WHERE id=%s', (row['id'],))
                conn.commit()
                raise ApiError(400, 'Неверный код подтверждения')
            conn.execute('UPDATE otp_codes SET used=1 WHERE id=%s', (row['id'],))
            conn.commit()

            # Аккаунт уже должен существовать — его создаёт /register/, а /otp/request/
            # отказывает незарегистрированным email/телефонам. Если пользователя всё же
            # нет (например, удалили вручную из базы) — вход не даём, а не создаём заново.
            col = 'email' if channel == 'email' else 'phone'
            user = conn.execute(f'SELECT * FROM users WHERE {col}=%s', (destination,)).fetchone()
            if not user:
                raise ApiError(404, 'Аккаунт не найден. Сначала зарегистрируйтесь.')

            token = gen_token()
            conn.execute('INSERT INTO tokens(token, user_id, created_at) VALUES (%s,%s,%s)',
                         (token, user['id'], now_iso()))
            conn.commit()
            return {'access': token, 'refresh': token, 'user': user_to_json(user)}

        # --- профиль -----------------------------------------------------
        if p == '/me/' and method == 'GET':
            user = self._current_user(conn)
            return user_to_json(user)

        if p == '/me/' and method == 'PATCH':
            user = self._current_user(conn)
            body = self._read_json_body()
            fields = {k: v for k, v in body.items()
                      if k in ('first_name', 'last_name', 'address', 'email', 'phone') and v}
            if fields:
                sets = ', '.join(f'{k}=%s' for k in fields)
                try:
                    conn.execute(f'UPDATE users SET {sets} WHERE id=%s', (*fields.values(), user['id']))
                    conn.commit()
                except psycopg2.IntegrityError:
                    raise ApiError(400, 'Этот email или телефон уже привязан к другому аккаунту')
            user = conn.execute('SELECT * FROM users WHERE id=%s', (user['id'],)).fetchone()
            return user_to_json(user)

        # --- корзина -------------------------------------------------
        # Режим (доставка/самовывоз) передаётся как ?mode=delivery|pickup в
        # запросе к корзине — цены и итог пересчитываются на лету, ничего не
        # хранится "залипшим" в конкретном режиме (пункт 2 ТЗ).
        cart_mode = (query.get('mode') or ['delivery'])[0]

        if p == '/cart/' and method == 'GET':
            user = self._current_user(conn)
            return cart_to_json(conn, user['id'], mode=cart_mode)

        if p == '/cart/items/' and method == 'POST':
            user = self._current_user(conn)
            body = self._read_json_body()
            product_id = body.get('product_id')
            qty = int(body.get('quantity', 1))
            if not product_id:
                raise ApiError(400, 'product_id обязателен')
            # Атомарный upsert на уровне БД. Раньше здесь было "проверить SELECT-ом,
            # есть ли уже такая позиция, потом отдельным запросом обновить или
            # вставить" — два запроса не одной операцией. Двойной тап по "в
            # корзину" (обычное дело на мобильном) успевал прислать 2 запроса,
            # которые ОБА проходили проверку "такой позиции ещё нет" до того,
            # как первый успевал её вставить — и в итоге появлялись 2 строки на
            # один и тот же товар вместо одной с увеличенным количеством
            # (см. cart_items_user_product_uidx в миграциях). ON CONFLICT
            # делает проверку и запись неделимой операцией — так дубль
            # физически не может возникнуть, независимо от скорости кликов.
            conn.execute(
                'INSERT INTO cart_items(user_id, product_id, quantity) VALUES (%s,%s,%s) '
                'ON CONFLICT (user_id, product_id) DO UPDATE SET quantity = cart_items.quantity + EXCLUDED.quantity',
                (user['id'], product_id, qty)
            )
            conn.commit()
            return cart_to_json(conn, user['id'], mode=cart_mode)

        m = re.match(r'^/cart/items/(\d+)/$', p)
        if m and method == 'PATCH':
            user = self._current_user(conn)
            body = self._read_json_body()
            qty = int(body.get('quantity', 1))
            item = conn.execute('SELECT * FROM cart_items WHERE id=%s AND user_id=%s',
                                 (m.group(1), user['id'])).fetchone()
            if not item:
                raise ApiError(404, 'Товар не найден в корзине')
            if qty <= 0:
                conn.execute('DELETE FROM cart_items WHERE id=%s', (item['id'],))
            else:
                conn.execute('UPDATE cart_items SET quantity=%s WHERE id=%s', (qty, item['id']))
            conn.commit()
            return cart_to_json(conn, user['id'], mode=cart_mode)

        if m and method == 'DELETE':
            user = self._current_user(conn)
            conn.execute('DELETE FROM cart_items WHERE id=%s AND user_id=%s', (m.group(1), user['id']))
            conn.commit()
            return cart_to_json(conn, user['id'], mode=cart_mode)

        # --- заказы -----------------------------------------------------
        if p == '/orders/checkout/' and method == 'POST':
            # Оформление заказа: клиент подтверждает режим (доставка/самовывоз),
            # сервер пересчитывает всё заново из base_price товаров — цены,
            # переданные с фронтенда, НЕ используются, чтобы их нельзя было
            # подделать (пункт 1, 4 ТЗ: "цены должны считаться, а не быть
            # захардкоженными").
            user = self._current_user(conn)
            body = self._read_json_body()
            order_mode = body.get('mode')
            if order_mode not in ('delivery', 'pickup'):
                raise ApiError(400, "Укажите режим получения заказа: 'delivery' или 'pickup'")
            needs_crew = 1 if body.get('needs_crew') else 0

            rows = conn.execute(
                'SELECT ci.id as item_id, ci.quantity, p.* FROM cart_items ci '
                'JOIN products p ON p.id = ci.product_id WHERE ci.user_id=%s', (user['id'],)
            ).fetchall()
            if not rows:
                raise ApiError(400, 'Корзина пуста')

            coef = coef_for_mode(order_mode)
            items_total = 0
            weight_total = 0.0
            order_items = []
            for r in rows:
                base_price = r['base_price'] if r['base_price'] else r['price']
                unit_price = round(base_price * coef)
                line_total = unit_price * r['quantity']
                items_total += line_total
                weight_total += (r['weight_kg'] or 0) * r['quantity']
                order_items.append((r['id'], r['quantity'], unit_price, r['weight_kg'] or 0))

            # Стоимость доставки БОЛЬШЕ НЕ считается автоматически — теперь
            # администратор указывает её вручную через бота, после того как
            # получит чек заказа (новая логика оформления). При самовывозе
            # доставка сразу = 0 и считается «рассчитанной».
            delivery_fee, real_cost = 0, 0
            delivery_quoted = 1 if order_mode == 'pickup' else 0
            total = items_total  # доставка прибавится к total, когда админ её укажет
            order_number = gen_order_number(conn)

            cur = conn.execute(
                'INSERT INTO orders(user_id, order_number, mode, status, payment_status, total, delivery_fee, '
                'delivery_real_cost, prepay_percent, prepay_amount, is_prepaid, delivery_quoted, needs_crew, '
                "created_at) VALUES (%s,%s,%s,'processing','awaiting_payment',%s,%s,%s,0,0,0,%s,%s,%s)",
                (user['id'], order_number, order_mode, total, delivery_fee, real_cost, delivery_quoted,
                 needs_crew, now_iso())
            )
            order_id = cur.lastrowid
            for product_id, qty, unit_price, weight_kg in order_items:
                conn.execute(
                    'INSERT INTO order_items(order_id, product_id, quantity, price, weight_kg) VALUES (%s,%s,%s,%s,%s)',
                    (order_id, product_id, qty, unit_price, weight_kg)
                )
            if order_mode == 'delivery':
                # По одному «ожидающему» запросу цены доставки на каждый
                # завод, чьи товары попали в заказ — см. create_delivery_quotes_for_order.
                create_delivery_quotes_for_order(conn, order_id)
            conn.execute('DELETE FROM cart_items WHERE user_id=%s', (user['id'],))
            # Чек сразу уходит администратору в Telegram-бота (пункт 2 ТЗ) —
            # кладём в очередь bot_outbox, telegram_bot.py заберёт его при
            # следующем опросе backend.py.
            conn.execute(
                "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES ('receipt', %s, 0, %s)",
                (order_id, now_iso())
            )
            if order_mode == 'delivery':
                notif_text = (f'Заказ №{order_number} создан на сумму {fmt_sum(total)}. Ожидаем, пока '
                               f'администратор укажет стоимость доставки — статус появится в «Оформить заказ».')
            else:
                notif_text = (f'Заказ №{order_number} создан на сумму {fmt_sum(total)}. Перейдите к оформлению, '
                               f'чтобы заполнить данные и оплатить.')
            conn.execute(
                'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
                (user['id'], 'Заказ оформлен', notif_text, now_iso())
            )
            conn.commit()
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order_id,)).fetchone()
            return order_to_json(conn, order)

        if p == '/orders/' and method == 'GET':
            user = self._current_user(conn)
            rows = conn.execute('SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC', (user['id'],)).fetchall()
            return [order_to_json(conn, r) for r in rows]

        m = re.match(r'^/orders/(\d+)/$', p)
        if m and method == 'GET':
            # Используется страницей «Оформить заказ» для опроса статуса —
            # появилась ли стоимость доставки, указанная админом (пункт 3.А.3-4 ТЗ).
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            return order_to_json(conn, order)

        m = re.match(r'^/orders/(\d+)/submit-payment/$', p)
        if m and method == 'POST':
            # Клиент подтверждает данные получения на сайте: имя, адрес (при
            # доставке), желаемое время. Предоплата и скриншот больше НЕ
            # нужны — оплата происходит на месте, при получении заказа.
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            if not order['delivery_quoted']:
                raise ApiError(400, 'Стоимость доставки ещё не указана администратором — подождите расчёта')
            if order['blank_submitted']:
                raise ApiError(400, 'Данные по этому заказу уже отправлены администратору')

            body = self._read_json_body()
            customer_name = (body.get('customer_name') or '').strip()
            delivery_address = (body.get('delivery_address') or '').strip()
            desired_time = (body.get('desired_time') or '').strip()

            if not customer_name:
                raise ApiError(400, 'Укажите имя заказчика')
            if order['mode'] == 'delivery' and not delivery_address:
                raise ApiError(400, 'Укажите адрес доставки')
            if not desired_time:
                raise ApiError(400, 'Укажите желаемое время получения')

            conn.execute(
                "UPDATE orders SET customer_name=%s, delivery_address=%s, desired_time=%s, "
                "blank_submitted=1 WHERE id=%s",
                (customer_name, delivery_address, desired_time, order['id'])
            )
            # Данные из бланка + итоговая сумма уходят администратору в бота
            # (пункт 5 ТЗ) — снова через очередь bot_outbox.
            conn.execute(
                "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES ('blank_submitted', %s, 0, %s)",
                (order['id'], now_iso())
            )
            conn.commit()
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
            return order_to_json(conn, order)

        m = re.match(r'^/orders/(\d+)/$', p)
        if m and method == 'PATCH':
            # Клиент правит уже отправленные данные получения (имя, адрес,
            # желаемое время) — например, если ошибся при оформлении.
            # Работает и до, и после submit-payment; недоступно для
            # завершённых/отменённых заказов.
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            if not can_edit_order(order):
                raise ApiError(400, 'Этот заказ уже нельзя редактировать')

            body = self._read_json_body()
            customer_name = body.get('customer_name')
            delivery_address = body.get('delivery_address')
            desired_time = body.get('desired_time')

            sets, params = [], []
            if customer_name is not None:
                customer_name = customer_name.strip()
                if not customer_name:
                    raise ApiError(400, 'Укажите имя заказчика')
                sets.append('customer_name=%s'); params.append(customer_name)
            if delivery_address is not None:
                delivery_address = delivery_address.strip()
                if order['mode'] == 'delivery' and not delivery_address:
                    raise ApiError(400, 'Укажите адрес доставки')
                sets.append('delivery_address=%s'); params.append(delivery_address)
            if desired_time is not None:
                desired_time = desired_time.strip()
                if not desired_time:
                    raise ApiError(400, 'Укажите желаемое время получения')
                sets.append('desired_time=%s'); params.append(desired_time)

            if not sets:
                raise ApiError(400, 'Нечего сохранять')

            params.append(order['id'])
            conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=%s", params)
            conn.execute(
                "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES ('order_updated', %s, 0, %s)",
                (order['id'], now_iso())
            )
            conn.commit()
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
            return order_to_json(conn, order)

        m = re.match(r'^/orders/(\d+)/cancel/$', p)
        if m and method == 'POST':
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            cancel_order(conn, order)
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
            return order_to_json(conn, order)

        m = re.match(r'^/orders/(\d+)/pay-prepayment/$', p)
        if m and method == 'POST':
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            if order['is_prepaid']:
                return order_to_json(conn, order)
            # ВАЖНО: это НЕ реальное списание денег с карты — сайт не принимает и не хранит
            # настоящие карточные данные без подключения лицензированного платёжного шлюза
            # (Payme/Click). Это лишь фиксация факта предоплаты в системе (например, после
            # оплаты наличными, переводом или через терминал курьера).
            conn.execute('UPDATE orders SET is_prepaid=1 WHERE id=%s', (order['id'],))
            conn.execute(
                'INSERT INTO notifications(user_id, title, text, is_read, created_at) VALUES (%s,%s,%s,0,%s)',
                (user['id'], 'Предоплата получена',
                 f'Предоплата по заказу №{order["id"]} на сумму {fmt_sum(order["prepay_amount"])} подтверждена.',
                 now_iso())
            )
            conn.commit()
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
            return order_to_json(conn, order)

        m = re.match(r'^/orders/(\d+)/advance/$', p)
        if m and method == 'POST':
            user = self._current_user(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=%s AND user_id=%s',
                                  (m.group(1), user['id'])).fetchone()
            if not order:
                raise ApiError(404, 'Заказ не найден')
            if not order['blank_submitted']:
                raise ApiError(400, 'Сначала нужно оформить данные получения заказа')
            steps = order_step_keys(order)
            idx = steps.index(order['status'])
            if idx < len(steps) - 1:
                set_order_status(conn, order, steps[idx + 1])
            order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
            return order_to_json(conn, order)

        m = re.match(r'^/products/(\d+)/reviews/$', p)
        if m and method == 'POST':
            user = self._current_user(conn)
            body = self._read_json_body()
            rating = int(body.get('rating') or 0)
            text = (body.get('text') or '').strip()
            if rating < 1 or rating > 5:
                raise ApiError(400, 'Оценка должна быть от 1 до 5')
            if not text:
                raise ApiError(400, 'Напишите текст отзыва')
            product = conn.execute('SELECT * FROM products WHERE id=%s', (m.group(1),)).fetchone()
            if not product:
                raise ApiError(404, 'Товар не найден')
            author_name = ' '.join(filter(None, [user['first_name'], user['last_name']])).strip() or 'Покупатель'
            conn.execute(
                'INSERT INTO reviews(product_id, author_name, rating, text, created_at) VALUES (%s,%s,%s,%s,%s)',
                (product['id'], author_name, rating, text, now_iso())
            )
            conn.commit()
            return {'detail': 'Спасибо за отзыв!'}

        # --- служебные эндпоинты для telegram_bot.py (пункт 3 ТЗ) ---------
        # Защищены общим секретом (заголовок X-Internal-Secret), который бот
        # читает из того же internal_secret.txt, что и backend.py — так бот
        # может по номеру заказа прочитать состав/сумму/режим и подтвердить
        # или отклонить оплату, не имея доступа к остальному API от имени
        # покупателя.
        if p.startswith('/internal/'):
            secret = self.headers.get('X-Internal-Secret', '')
            if not secret or secret != INTERNAL_API_SECRET:
                raise ApiError(401, 'Неверный внутренний секрет')

            m = re.match(r'^/internal/orders/by-number/([A-Za-z0-9\-]+)/$', p)
            if m and method == 'GET':
                order = conn.execute('SELECT * FROM orders WHERE order_number=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                return order_to_json(conn, order)

            if p == '/internal/notifications/pending/' and method == 'GET':
                # Очередь исходящих уведомлений для telegram_bot.py: новые чеки
                # (сразу после оформления) и заполненные клиентом бланки оплаты
                # (пункты 2 и 5 ТЗ). Бот опрашивает этот эндпоинт в своём цикле.
                rows = conn.execute(
                    'SELECT * FROM bot_outbox WHERE delivered=0 ORDER BY id ASC LIMIT 20'
                ).fetchall()
                result = []
                for r in rows:
                    order = conn.execute('SELECT * FROM orders WHERE id=%s', (r['order_id'],)).fetchone()
                    if not order:
                        continue
                    item = {'id': r['id'], 'kind': r['kind'], 'order': order_to_json(conn, order)}
                    if r['kind'] == 'blank_submitted':
                        item['screenshot_b64'] = order['payment_screenshot_b64']
                        item['screenshot_mime'] = order['payment_screenshot_mime']
                    result.append(item)
                return result

            m = re.match(r'^/internal/notifications/(\d+)/ack/$', p)
            if m and method == 'POST':
                conn.execute('UPDATE bot_outbox SET delivered=1 WHERE id=%s', (m.group(1),))
                conn.commit()
                return {'detail': 'ok'}

            m = re.match(r'^/internal/orders/by-number/([A-Za-z0-9\-]+)/set-delivery/$', p)
            if m and method == 'POST':
                # Администратор в боте прислал число стоимости доставки —
                # фиксируем, пересчитываем итог и открываем клиенту бланк
                # оплаты (пункт 3.А.2-4 ТЗ).
                order = conn.execute('SELECT * FROM orders WHERE order_number=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                body = self._read_json_body()
                try:
                    fee = round(float(body.get('fee')))
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректная сумма доставки')
                if fee < 0:
                    raise ApiError(400, 'Сумма не может быть отрицательной')
                apply_delivery_fee(conn, order, fee)
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/internal/orders/by-number/([A-Za-z0-9\-]+)/set-delivery-quote/$', p)
            if m and method == 'POST':
                # Стоимость доставки от ОДНОГО завода (когда в заказе товары
                # нескольких заводов и каждый директор отвечает лично боту).
                # Когда ответили ВСЕ заводы по этому заказу — их суммы
                # складываются и применяются как итоговая доставка заказа
                # (см. create_delivery_quotes_for_order / apply_delivery_fee).
                order = conn.execute('SELECT * FROM orders WHERE order_number=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                if order['delivery_quoted']:
                    raise ApiError(400, 'Стоимость доставки по этому заказу уже определена')
                body = self._read_json_body()
                factory_id = body.get('factory_id')
                try:
                    amount = round(float(body.get('amount')))
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректная сумма доставки')
                if amount < 0:
                    raise ApiError(400, 'Сумма не может быть отрицательной')
                cur = conn.execute(
                    'UPDATE order_delivery_quotes SET amount=%s WHERE order_id=%s AND '
                    'COALESCE(factory_id,0)=COALESCE(%s,0) AND amount IS NULL',
                    (amount, order['id'], factory_id)
                )
                if cur.rowcount == 0:
                    raise ApiError(400, 'Этот завод уже указал стоимость доставки по этому заказу либо он не участвует в заказе')
                conn.commit()
                quotes = delivery_quotes_to_json(conn, order['id'])
                all_done = all(q['submitted'] for q in quotes)
                if all_done:
                    total_fee = round(sum(q['amount'] for q in quotes))
                    apply_delivery_fee(conn, order, total_fee)
                    order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return {'order': order_to_json(conn, order), 'all_done': all_done, 'quotes': quotes}

            m = re.match(r'^/internal/orders/by-number/([A-Za-z0-9\-]+)/confirm/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE order_number=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=True)
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/internal/orders/by-number/([A-Za-z0-9\-]+)/reject/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE order_number=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=False)
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            raise ApiError(404, f'Неизвестный внутренний маршрут: {method} {path}')

        # --- уведомления --------------------------------------------------
        if p == '/notifications/' and method == 'GET':
            user = self._current_user(conn)
            rows = conn.execute('SELECT * FROM notifications WHERE user_id=%s ORDER BY id DESC',
                                 (user['id'],)).fetchall()
            return [{'id': r['id'], 'title': r['title'], 'text': r['text'],
                      'is_read': bool(r['is_read']), 'created_at': r['created_at']} for r in rows]

        if p == '/notifications/mark-all-read/' and method == 'POST':
            user = self._current_user(conn)
            conn.execute('UPDATE notifications SET is_read=1 WHERE user_id=%s', (user['id'],))
            conn.commit()
            return {'detail': 'ok'}

        # --- ИИ-Дизайн ------------------------------------------------------
        if p == '/ai-design/' and method == 'GET':
            user = self._current_user(conn)
            rows = conn.execute(
                'SELECT * FROM ai_designs WHERE user_id=%s ORDER BY id DESC LIMIT 30',
                (user['id'],)
            ).fetchall()
            return [ai_design_to_json(r) for r in rows]

        m = re.match(r'^/ai-design/(\d+)/$', p)
        if m and method == 'GET':
            user = self._current_user(conn)
            row = conn.execute(
                'SELECT * FROM ai_designs WHERE id=%s AND user_id=%s',
                (m.group(1), user['id'])
            ).fetchone()
            if not row:
                raise ApiError(404, 'Не найдено')
            return ai_design_to_json(row)

        if p == '/ai-design/generate/' and method == 'POST':
            user = self._current_user(conn)
            body = self._read_json_body()
            prompt = (body.get('prompt') or '').strip()
            if not prompt:
                raise ApiError(400, 'Опишите дом текстом — что вы хотите увидеть')
            if len(prompt) > 600:
                raise ApiError(400, 'Слишком длинное описание (максимум 600 символов)')

            # Необязательные поля: площадь дома и стиль из дропдауна —
            # добавляем их в текст промпта, чтобы модель учла их наравне
            # с описанием, и чтобы это же было видно в истории генераций.
            size_m2_raw = body.get('size_m2')
            size_m2 = None
            if size_m2_raw not in (None, ''):
                try:
                    size_m2 = float(size_m2_raw)
                except (TypeError, ValueError):
                    raise ApiError(400, 'Площадь дома должна быть числом')
                if size_m2 <= 0 or size_m2 > 10000:
                    raise ApiError(400, 'Укажите реалистичную площадь дома (до 10000 м²)')

            style_key = (body.get('style') or '').strip()
            if style_key and style_key not in AI_DESIGN_STYLE_LABELS:
                raise ApiError(400, 'Неизвестный стиль')

            bedrooms_raw = body.get('bedrooms')
            bedrooms = None
            if bedrooms_raw not in (None, ''):
                try:
                    bedrooms = int(bedrooms_raw)
                except (TypeError, ValueError):
                    raise ApiError(400, 'Количество спален должно быть числом')
                if bedrooms < 1 or bedrooms > 10:
                    raise ApiError(400, 'Укажите реалистичное количество спален (1-10)')

            full_prompt = prompt
            if size_m2:
                size_txt = f'{size_m2:g}'
                full_prompt += f'. Площадь дома около {size_txt} м².'
            if style_key:
                full_prompt += f'. Стиль: {AI_DESIGN_STYLE_LABELS[style_key]}.'
            if bedrooms:
                full_prompt += f'. Количество спален: {bedrooms}.'

            reference_png = decode_and_prepare_reference_photo(body.get('photo_base64'))

            today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            used_today = conn.execute(
                "SELECT COUNT(*) AS c FROM ai_designs WHERE user_id=%s AND created_at >= %s",
                (user['id'], today_start)
            ).fetchone()['c']
            if used_today >= AI_DESIGN_DAILY_LIMIT:
                raise ApiError(429, f'Дневной лимит ИИ-Дизайна исчерпан ({AI_DESIGN_DAILY_LIMIT} в сутки). Попробуйте завтра.')

            created_at = now_iso()
            cur = conn.execute(
                "INSERT INTO ai_designs(user_id, prompt, status, created_at, size_m2, bedrooms) "
                "VALUES(%s,%s,'pending',%s,%s,%s)",
                (user['id'], full_prompt, created_at, size_m2, bedrooms)
            )
            design_id = cur.lastrowid
            conn.commit()

            images = []
            first_error = None
            # Генерируем 3 картинки параллельно (разные ракурсы фасада) — иначе
            # по одной последовательно это легко 30-45 секунд ожидания.
            # Если пользователь приложил референс-фото — используем его как
            # основу через Images Edit API вместо генерации с нуля.
            def _one(angle_key, angle_prompt):
                angle_full_prompt = f'{full_prompt}. {angle_prompt}.'
                png_bytes = generate_gemini_image(angle_full_prompt, reference_png)
                filename = f'{design_id}_{angle_key}.png'
                with open(os.path.join(GENERATED_DIR, filename), 'wb') as f:
                    f.write(png_bytes)
                return {'angle': angle_key, 'path': f'/generated/{filename}'}

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(_one, k, p_): k for k, p_ in AI_DESIGN_ANGLES}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        images.append(fut.result())
                    except ApiError as e:
                        first_error = first_error or e.detail
                    except Exception as e:  # noqa: BLE001
                        print(f'[ai-design] Неожиданная ошибка генерации: {type(e).__name__}: {e}')
                        first_error = first_error or 'Не удалось сгенерировать одну из картинок'

            # порядок картинок должен быть стабильным (фронт/зад/перспектива/план),
            # as_completed отдаёт их в случайном порядке готовности
            order = {k: i for i, (k, _) in enumerate(AI_DESIGN_ANGLES)}
            images.sort(key=lambda im: order.get(im['angle'], 99))

            if images:
                status = 'done' if not first_error else 'partial'
            else:
                status = 'failed'
            conn.execute(
                'UPDATE ai_designs SET status=%s, images_json=%s, error=%s WHERE id=%s',
                (status, json.dumps(images, ensure_ascii=False), first_error, design_id)
            )
            conn.commit()
            row = conn.execute('SELECT * FROM ai_designs WHERE id=%s', (design_id,)).fetchone()
            return ai_design_to_json(row)

        # --- панель администратора (веб-дашборд на /admin/) ----------------
        if p.startswith('/admin/'):
            if p == '/admin/login/' and method == 'POST':
                _check_admin_login_throttle()
                body = self._read_json_body()
                password = body.get('password') or ''
                if not secrets.compare_digest(password, ADMIN_PANEL_PASSWORD):
                    _record_admin_login_failure()
                    raise ApiError(401, 'Неверный пароль')
                token = secrets.token_hex(24)
                conn.execute('INSERT INTO admin_sessions(token, created_at) VALUES (%s,%s)', (token, now_iso()))
                conn.commit()
                return {'token': token}

            admin = self._current_admin(conn)

            if p == '/admin/logout/' and method == 'POST':
                conn.execute('DELETE FROM admin_sessions WHERE token=%s', (admin['token'],))
                conn.commit()
                return {'detail': 'ok'}

            if p == '/admin/wipe-data/' and method == 'POST':
                # Полная очистка: обнуляет заказы, клиентов, финансовую историю,
                # уведомления и т.д. — КАТАЛОГ (categories/products/factories)
                # не трогаем. Требует явного подтверждения телом запроса,
                # чтобы случайный вызов не снёс базу.
                body = self._read_json_body()
                if (body.get('confirm') or '') != 'WIPE':
                    raise ApiError(400, 'Требуется подтверждение: передайте {"confirm":"WIPE"}')
                # Порядок важен из-за внешних ключей (сначала дочерние таблицы).
                for tbl in (
                    'finance_order_snapshots', 'finance_transactions',
                    'bot_outbox', 'notifications',
                    'order_delivery_quotes', 'order_items', 'orders',
                    'cart_items', 'tokens', 'otp_codes', 'reviews',
                    'users',
                ):
                    conn.execute(f'DELETE FROM {tbl}')
                # Все сессии администратора, кроме текущей, тоже сбрасываем —
                # но себя не разлогиниваем.
                conn.execute('DELETE FROM admin_sessions WHERE token != %s', (admin['token'],))
                conn.commit()
                return {'detail': 'ok'}

            if p == '/admin/orders/' and method == 'GET':
                # ВСЕ заказы всех клиентов, самые новые сверху (включая отменённые —
                # они не удаляются, а просто получают статус status='cancelled',
                # поэтому всегда остаются видны и в этом списке).
                rows = conn.execute('SELECT * FROM orders ORDER BY id DESC').fetchall()
                return [order_to_json(conn, r) for r in rows]

            if p == '/admin/users/' and method == 'GET':
                # Список всех зарегистрированных клиентов + число их заказов и
                # сумма, которую они заказали — для вкладки «Клиенты» в панели.
                rows = conn.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
                result = []
                for u in rows:
                    stats = conn.execute(
                        'SELECT COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE user_id=%s', (u['id'],)
                    ).fetchone()
                    result.append({
                        **user_to_json(u),
                        'created_at': u['created_at'],
                        'orders_count': stats['c'],
                        'orders_total': stats['s'],
                    })
                return result

            m = re.match(r'^/admin/orders/(\d+)/cancel/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                cancel_order(conn, order)
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/$', p)
            if m and method == 'GET':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                data = order_to_json(conn, order)
                # В отличие от обычного order_to_json (для клиента), админу
                # дополнительно отдаём сам скриншот оплаты, чтобы его можно
                # было посмотреть прямо в дашборде, не открывая бота.
                data['payment_screenshot_b64'] = order['payment_screenshot_b64']
                data['payment_screenshot_mime'] = order['payment_screenshot_mime']
                return data

            m = re.match(r'^/admin/orders/(\d+)/set-delivery/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                body = self._read_json_body()
                try:
                    fee = round(float(body.get('fee')))
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректная сумма доставки')
                if fee < 0:
                    raise ApiError(400, 'Сумма не может быть отрицательной')
                apply_delivery_fee(conn, order, fee)
                # Если стоимость доставки указали из веб-панели (а не ответом
                # боту в Telegram) — бот об этом никак не узнает сам, канал
                # молчит. Кладём в очередь, чтобы telegram_bot.py всё равно
                # отправил админу подтверждение (см. 'admin_delivery_set' в
                # telegram_bot.py). Когда цену вводят через сам бот, это не
                # дублируется — тот путь не проходит через этот эндпоинт.
                conn.execute(
                    "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES "
                    "('admin_delivery_set', %s, 0, %s)", (order['id'], now_iso())
                )
                conn.commit()
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/confirm-payment/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=True)
                conn.execute(
                    "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES "
                    "('admin_payment_confirmed', %s, 0, %s)", (order['id'], now_iso())
                )
                conn.commit()
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/reject-payment/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=False)
                conn.execute(
                    "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES "
                    "('admin_payment_rejected', %s, 0, %s)", (order['id'], now_iso())
                )
                conn.commit()
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/set-status/$', p)
            if m and method == 'POST':
                # Каждый шаг («В обработке» → «Материалы доставлены» →
                # «Бригада выезжает» → «Завершено») можно подтвердить отдельно,
                # в любом порядке — не обязательно строго по одному вперёд.
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                body = self._read_json_body()
                new_status = body.get('status')
                set_order_status(conn, order, new_status)
                # Смена статуса из веб-панели тоже уходит в канал — раньше
                # уходило только уведомление внутри сайта клиенту, в Telegram
                # ничего не приходило вообще.
                conn.execute(
                    "INSERT INTO bot_outbox(kind, order_id, delivered, created_at) VALUES "
                    "('admin_status_changed', %s, 0, %s)", (order['id'], now_iso())
                )
                conn.commit()
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            # --- мини-бухгалтерия: /admin/finance/... -----------------------
            if p == '/admin/finance/rate/' and method == 'GET':
                rate, updated_at = get_usd_rate(conn)
                return {'usd_rate': rate, 'usd_rate_updated_at': updated_at}

            if p == '/admin/finance/summary/' and method == 'GET':
                date_from, date_to, ts_from, ts_to = _finance_period_bounds(query)
                return finance_summary(conn, ts_from, ts_to, date_from, date_to)

            if p == '/admin/finance/transactions/' and method == 'GET':
                _, _, ts_from, ts_to = _finance_period_bounds(query)
                rows = conn.execute(
                    'SELECT * FROM finance_transactions WHERE created_at BETWEEN %s AND %s '
                    'ORDER BY created_at DESC, id DESC', (ts_from, ts_to)
                ).fetchall()
                result = []
                for r in rows:
                    order_number = None
                    if r['order_id']:
                        o = conn.execute('SELECT order_number FROM orders WHERE id=%s', (r['order_id'],)).fetchone()
                        order_number = o['order_number'] if o else None
                    result.append({
                        'id': r['id'], 'type': r['type'], 'amount': money_obj(conn, r['amount_uzs']),
                        'order_id': r['order_id'], 'order_number': order_number, 'category': r['category'],
                        'note': r['note'], 'created_at': r['created_at'], 'created_by': r['created_by'],
                    })
                return result

            if p == '/admin/finance/expense/' and method == 'POST':
                body = self._read_json_body()
                try:
                    amount = float(body.get('amount_uzs'))
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректная сумма')
                if amount <= 0:
                    raise ApiError(400, 'Сумма должна быть больше нуля')
                category = body.get('category') or 'other'
                if category not in FINANCE_EXPENSE_CATEGORIES:
                    raise ApiError(400, 'Неизвестная категория расхода')
                note = (body.get('note') or '').strip()
                created_at = body.get('created_at') or now_iso()
                event_id = f'manual_expense_{secrets.token_hex(8)}'
                conn.execute(
                    "INSERT INTO finance_transactions(type, amount_uzs, order_id, category, note, event_id, "
                    "created_at, created_by) VALUES ('expense', %s, NULL, %s, %s, %s, %s, 'admin')",
                    (amount, category, note, event_id, created_at)
                )
                conn.commit()
                return {'detail': 'ok'}

            if p == '/admin/finance/by-factory/' and method == 'GET':
                _, _, ts_from, ts_to = _finance_period_bounds(query)
                return finance_by_factory(conn, ts_from, ts_to)

            # Детализация по одному заводу — клик по заводу в /admin/finance/by-factory
            # (п.7 ТЗ: «клик по заводу → список позиций/заказов за период»).
            if p == '/admin/finance/by-factory-items/' and method == 'GET':
                _, _, ts_from, ts_to = _finance_period_bounds(query)
                factory_id_raw = (query.get('factory_id') or [None])[0]
                sql = (
                    "SELECT s.*, o.order_number, o.status FROM finance_order_snapshots s "
                    "JOIN orders o ON o.id = s.order_id "
                    "WHERE s.created_at BETWEEN %s AND %s AND o.status <> 'cancelled'"
                )
                params = [ts_from, ts_to]
                if factory_id_raw and factory_id_raw != 'none':
                    sql += ' AND s.factory_id = %s'
                    params.append(int(factory_id_raw))
                else:
                    sql += ' AND s.factory_id IS NULL'
                sql += ' ORDER BY s.created_at DESC'
                rows = conn.execute(sql, params).fetchall()
                return [{
                    'order_id': r['order_id'], 'order_number': r['order_number'],
                    'product_name': r['product_name'], 'qty': r['qty'],
                    'buy_price': r['buy_price'], 'sell_price': r['sell_price'],
                    'line_buy_total': money_obj(conn, r['line_buy_total']),
                    'line_sell_total': money_obj(conn, r['line_sell_total']),
                    'line_margin': money_obj(conn, r['line_margin']),
                    'created_at': r['created_at'],
                } for r in rows]

            # Заводы + их директор в Telegram (личка, куда бот шлёт запрос
            # «впишите стоимость доставки») — отдельная страница настроек,
            # не привязанная к конкретному товару.
            if p == '/admin/finance/factories/' and method == 'GET':
                rows = conn.execute('SELECT id, name, telegram_chat_id FROM factories ORDER BY name').fetchall()
                return [{'id': r['id'], 'name': r['name'], 'telegram_chat_id': r['telegram_chat_id']} for r in rows]

            m = re.match(r'^/admin/finance/factories/(\d+)/$', p)
            if m and method == 'POST':
                factory = conn.execute('SELECT * FROM factories WHERE id=%s', (m.group(1),)).fetchone()
                if not factory:
                    raise ApiError(404, 'Завод не найден')
                body = self._read_json_body()
                chat_id = (body.get('telegram_chat_id') or '').strip()
                if chat_id and not re.match(r'^-?\d+$', chat_id):
                    raise ApiError(400, 'Chat ID — это число (например -1004376636234 или 1307249120), не username')
                conn.execute('UPDATE factories SET telegram_chat_id=%s WHERE id=%s', (chat_id or None, factory['id']))
                conn.commit()
                return {'detail': 'ok'}

            m = re.match(r'^/admin/finance/products/(\d+)/$', p)
            if m and method == 'POST':
                product = conn.execute('SELECT * FROM products WHERE id=%s', (m.group(1),)).fetchone()
                if not product:
                    raise ApiError(404, 'Товар не найден')
                body = self._read_json_body()
                sets, params = [], []
                if body.get('buy_price') is not None:
                    try:
                        buy_price = float(body.get('buy_price'))
                    except (TypeError, ValueError):
                        raise ApiError(400, 'Некорректная закупочная цена')
                    if buy_price < 0:
                        raise ApiError(400, 'Закупочная цена не может быть отрицательной')
                    sets.append('buy_price=%s'); params.append(buy_price)
                if body.get('sell_price') is not None:
                    # Цена продажи (base_price) — та, что клиент видит в каталоге.
                    # 'price' держим синхронно как производную от неё (см.
                    # product_to_json / coef_for_mode выше), чтобы старые места
                    # кода, которые ещё читают 'price' напрямую, не отставали.
                    try:
                        sell_price = float(body.get('sell_price'))
                    except (TypeError, ValueError):
                        raise ApiError(400, 'Некорректная цена продажи')
                    if sell_price < 0:
                        raise ApiError(400, 'Цена продажи не может быть отрицательной')
                    sets.append('base_price=%s'); params.append(sell_price)
                    sets.append('price=%s'); params.append(round(sell_price * MARKUP_DELIVERY))
                factory_name = (body.get('factory_name') or '').strip()
                factory_id = get_or_create_factory(conn, factory_name) if factory_name else None
                if factory_id:
                    sets.append('factory_id=%s'); params.append(factory_id)
                if not sets:
                    raise ApiError(400, 'Нечего сохранять')
                params.append(product['id'])
                conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=%s", params)
                conn.commit()
                return {'detail': 'ok'}

            # Массовое проставление одной закупочной цены/завода сразу нескольким
            # товарам (когда у них одинаковая цена — например, кирпич разных
            # цветов от одного завода). Один UPDATE на весь выбранный список,
            # а не по одному запросу на каждый товар.
            if p == '/admin/finance/products/bulk-set/' and method == 'POST':
                body = self._read_json_body()
                product_ids = body.get('product_ids') or []
                if not isinstance(product_ids, list) or not product_ids:
                    raise ApiError(400, 'Не выбрано ни одного товара')
                try:
                    product_ids = [int(pid) for pid in product_ids]
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректный список товаров')
                try:
                    buy_price = float(body.get('buy_price'))
                except (TypeError, ValueError):
                    raise ApiError(400, 'Некорректная закупочная цена')
                if buy_price < 0:
                    raise ApiError(400, 'Цена не может быть отрицательной')
                factory_name = (body.get('factory_name') or '').strip()
                factory_id = get_or_create_factory(conn, factory_name) if factory_name else None
                conn.execute(
                    'UPDATE products SET buy_price=%s, factory_id=COALESCE(%s, factory_id) WHERE id = ANY(%s)',
                    (buy_price, factory_id, product_ids)
                )
                conn.commit()
                return {'detail': 'ok', 'updated': len(product_ids)}

            raise ApiError(404, f'Неизвестный маршрут панели администратора: {method} {path}')

        raise ApiError(404, f'Неизвестный маршрут: {method} {path}')


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
_bot_process = None  # дочерний процесс telegram_bot.py, если удалось его поднять


def _start_telegram_bot_subprocess():
    """
    Запускает telegram_bot.py отдельным процессом рядом с сайтом, чтобы не
    нужно было открывать второе окно терминала — pip install не требуется,
    используется тот же интерпретатор Python, что и для backend.py.
    Если telegram_bot.py рядом нет или в нём не вписан BOT_TOKEN — просто
    печатаем предупреждение, сайт при этом продолжает работать как обычно.
    """
    global _bot_process
    if os.environ.get('AUTO_START_BOT', '1') != '1':
        print('[bot] AUTO_START_BOT=0 — бот запускается отдельным процессом/сервисом (см. деплой).')
        return
    bot_path = os.path.join(BASE_DIR, 'telegram_bot.py')
    if not os.path.exists(bot_path):
        print('[bot] telegram_bot.py не найден рядом с backend.py — бот не запущен.')
        return
    try:
        _bot_process = subprocess.Popen([sys.executable, bot_path], cwd=BASE_DIR)
        print(f'[bot] telegram_bot.py запущен автоматически (pid {_bot_process.pid}).')
    except OSError as e:
        print(f'[bot] Не удалось автоматически запустить telegram_bot.py: {e!r}')
        print('[bot] Запустите его вручную вторым терминалом: python telegram_bot.py')


def _stop_telegram_bot_subprocess():
    if _bot_process and _bot_process.poll() is None:
        _bot_process.terminate()
        try:
            _bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _bot_process.kill()


def main():
    init_db()
    os.makedirs(GENERATED_DIR, exist_ok=True)
    # По умолчанию у стандартного http.server очередь на подключение всего 5
    # ожидающих соединений — при резком всплеске (много людей одновременно
    # открыли сайт) новые подключения могли обрываться ("Connection reset")
    # ещё до того, как до них доходила очередь. Увеличиваем запас.
    class RobustThreadingHTTPServer(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = RobustThreadingHTTPServer((HOST, PORT), Handler)
    print('=' * 60)
    print('StroyAI — сервер запущен')
    print(f'Сайт:   http://127.0.0.1:{PORT}/')
    print(f'API:    http://127.0.0.1:{PORT}/api/health/')
    print(f'База данных: PostgreSQL ({"подключено" if DATABASE_URL else "DATABASE_URL не задан!"})')
    if ADMIN_PANEL_PASSWORD == 'admin123':
        print('!' * 60)
        print('ВНИМАНИЕ: пароль админки НЕ ИЗМЕНЁН (всё ещё admin123)!')
        print('Любой, кто это увидит или угадает — получит полный доступ')
        print('к заказам, клиентам И СМОЖЕТ ПОЛНОСТЬЮ СТЕРЕТЬ БАЗУ ДАННЫХ.')
        print('Задайте ADMIN_PANEL_PASSWORD в Variables на Railway СЕЙЧАС ЖЕ.')
        print('!' * 60)
    if BREVO_API_KEY and EMAIL_HOST_USER:
        _masked = EMAIL_HOST_USER[:2] + '***' + EMAIL_HOST_USER[EMAIL_HOST_USER.find('@'):]
        print(f'Email OTP: настроен, отправитель {_masked} через Brevo API (HTTP, не SMTP). '
              f'Если письма не доходят — смотрите строки "[email] ..." ниже в логах.')
    else:
        print('Email OTP: НЕ настроен (нет BREVO_API_KEY/EMAIL_HOST_USER) — коды печатаются в консоль.')
    print('Коды подтверждения (OTP) в любом случае дублируются прямо сюда, в консоль, если письмо не ушло.')
    print('-' * 60)
    atexit.register(_stop_telegram_bot_subprocess)
    _start_telegram_bot_subprocess()
    _start_usd_rate_updater()
    print('Остановить сервер (и бота вместе с ним): Ctrl+C')
    print('=' * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nОстановлено.')
    finally:
        _stop_telegram_bot_subprocess()


if __name__ == '__main__':
    main()
