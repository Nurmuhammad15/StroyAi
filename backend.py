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
import mimetypes
import os
import re
import secrets
import smtplib
import string
import subprocess
import sys
import atexit
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# видно ВСЕ заказы всех клиентов и можно подтверждать каждый шаг вручную).
# Обязательно смените перед тем, как показывать сайт кому-то ещё, кроме себя.
ADMIN_PANEL_PASSWORD = 'admin123'

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
#   1. Включите двухфакторную аутентификацию на аккаунте Google (обязательно,
#      без неё пароль приложения не создать):
#      https://myaccount.google.com/security
#   2. Зайдите на https://myaccount.google.com/apppasswords
#   3. Создайте новый пароль приложения (название — любое, например "reno").
#   4. Google покажет пароль из 16 символов — скопируйте его без пробелов.
#   5. Впишите ниже EMAIL_HOST_USER (ваша почта) и EMAIL_HOST_PASSWORD
#      (тот самый 16-значный пароль, НЕ обычный пароль от Gmail).
#
# Если оставить пустым — коды по-прежнему будут просто печататься в консоль
# (как раньше), сайт при этом продолжит работать.
# ---------------------------------------------------------------------------
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_HOST_USER = 'gjjnn05@gmail.com'       # например: 'mystartup@gmail.com'
EMAIL_HOST_PASSWORD = 'pgsoiczbwmigttka'   # 16-значный пароль приложения из Google, без пробелов
EMAIL_FROM_NAME = 'StroyAI'

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


class PGConnWrapper:
    """Тонкая обёртка над psycopg2-соединением, чтобы код ниже (написанный
    под sqlite3: conn.execute(...).fetchone()/.fetchall(), cur.lastrowid)
    менялся как можно меньше."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        raw_cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        stripped = sql.strip()
        no_id_tables = ('tokens', 'admin_sessions')
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
        self._raw.close()


def db():
    if not DATABASE_URL:
        raise RuntimeError(
            'DATABASE_URL не задан. Впишите строку подключения Neon в переменные '
            'окружения (см. README/раздел деплоя).'
        )
    raw = psycopg2.connect(DATABASE_URL, sslmode='require')
    return PGConnWrapper(raw)


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
        conn.execute("UPDATE orders SET payment_status='paid', is_prepaid=1 WHERE id=%s", (order['id'],))
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


def product_to_json(conn, row, mode='delivery'):
    cat = conn.execute('SELECT id, name, name_uz, slug FROM categories WHERE id=%s', (row['category_id'],)).fetchone()
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
        'category': {'id': cat['id'], 'name': cat['name'], 'name_uz': cat['name_uz'], 'slug': cat['slug']},
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
    Отправляет код подтверждения на настоящую почту через Gmail (App Password).
    Возвращает True, если письмо реально ушло; False — если email не настроен
    или отправка не удалась (в обоих случаях код всё равно печатается в консоль,
    чтобы тестировать можно было в любом случае).
    """
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        return False
    try:
        msg = MIMEText(
            f'Ваш код подтверждения: {code}\n\nОн действует 5 минут.\n\n'
            f'Если вы не запрашивали вход — просто проигнорируйте это письмо.',
            'plain', 'utf-8'
        )
        msg['Subject'] = f'Код подтверждения: {code}'
        msg['From'] = f'{EMAIL_FROM_NAME} <{EMAIL_HOST_USER}>'
        msg['To'] = to_address

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10) as server:
            server.starttls()
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, [to_address], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001 — не роняем сервер из-за проблем с почтой
        print(f'[email] Не удалось отправить письмо на {to_address}: {e}')
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
    items = []
    total = 0
    count = 0
    weight_total = 0.0
    for r in rows:
        p = product_to_json(conn, r, mode=mode)
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
    item_list = []
    for it in items:
        prow = conn.execute('SELECT * FROM products WHERE id=%s', (it['product_id'],)).fetchone()
        item_list.append({
            'product': product_to_json(conn, prow) if prow else None,
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


# ---------------------------------------------------------------------------
# HTTP-обработчик
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    server_version = 'RenoMVP/1.0'

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

    # --- низкоуровневые помощники -----------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, path):
        if not os.path.exists(path):
            self._send_json(404, {'detail': f'Файл не найден: {os.path.basename(path)}'})
            return
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
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
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'public, max-age=3600')
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_json(404, {'detail': 'Не найдено'})
            return

        conn = db()
        try:
            payload = self._dispatch_api(method, path, query, conn)
            self._send_json(200, payload)
        except ApiError as e:
            self._send_json(e.status, {'detail': e.detail})
        except Exception as e:  # noqa: BLE001 — последняя линия обороны, чтобы сервер не падал
            print('ОШИБКА:', repr(e))
            self._send_json(500, {'detail': 'Внутренняя ошибка сервера'})
        finally:
            conn.close()

    # --- сами эндпоинты --------------------------------------------------
    def _dispatch_api(self, method, path, query, conn):
        p = path[len('/api'):]  # убираем префикс /api для удобства сравнения

        if p == '/health/' and method == 'GET':
            return {'ok': True, 'service': 'stroyai-backend (simple)'}

        if p == '/categories/' and method == 'GET':
            rows = conn.execute('SELECT * FROM categories ORDER BY id').fetchall()
            return [{'id': r['id'], 'name': r['name'], 'name_uz': r['name_uz'], 'slug': r['slug']} for r in rows]

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
            return [product_to_json(conn, r, mode=mode) for r in rows]

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

            # Код отправляем только на уже зарегистрированный email/телефон —
            # это предотвращает вход/создание аккаунта кем попало.
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
            if row['code'] != code:
                raise ApiError(400, 'Неверный код подтверждения')
            if datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
                raise ApiError(400, 'Код истёк, запросите новый')
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
            existing = conn.execute(
                'SELECT * FROM cart_items WHERE user_id=%s AND product_id=%s', (user['id'], product_id)
            ).fetchone()
            if existing:
                conn.execute('UPDATE cart_items SET quantity=quantity+%s WHERE id=%s', (qty, existing['id']))
            else:
                conn.execute('INSERT INTO cart_items(user_id, product_id, quantity) VALUES (%s,%s,%s)',
                             (user['id'], product_id, qty))
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

        # --- панель администратора (веб-дашборд на /admin/) ----------------
        if p.startswith('/admin/'):
            if p == '/admin/login/' and method == 'POST':
                body = self._read_json_body()
                password = body.get('password') or ''
                if password != ADMIN_PANEL_PASSWORD:
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
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/confirm-payment/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=True)
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

            m = re.match(r'^/admin/orders/(\d+)/reject-payment/$', p)
            if m and method == 'POST':
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (m.group(1),)).fetchone()
                if not order:
                    raise ApiError(404, 'Заказ не найден')
                apply_payment_decision(conn, order, approved=False)
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
                order = conn.execute('SELECT * FROM orders WHERE id=%s', (order['id'],)).fetchone()
                return order_to_json(conn, order)

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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print('=' * 60)
    print('StroyAI — сервер запущен')
    print(f'Сайт:   http://127.0.0.1:{PORT}/')
    print(f'API:    http://127.0.0.1:{PORT}/api/health/')
    print(f'База данных: PostgreSQL ({"подключено" if DATABASE_URL else "DATABASE_URL не задан!"})')
    print('Коды подтверждения (OTP) будут печататься прямо сюда, в консоль.')
    print('-' * 60)
    atexit.register(_stop_telegram_bot_subprocess)
    _start_telegram_bot_subprocess()
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
