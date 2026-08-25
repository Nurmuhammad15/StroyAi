#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StroyAI — Telegram-бот администратора (оформление заказа: доставка/
самовывоз, ручной расчёт доставки, приём бланка оплаты).

Работает БЕЗ pip install — только urllib из стандартной библиотеки Python,
как и backend.py. Запускается отдельным процессом рядом с сайтом:

    python backend.py        (в одном окне)
    python telegram_bot.py   (в другом окне, одновременно)

### Что делает бот (новая логика)

Клиент теперь ВСЁ делает на сайте (адрес, время, имя, скриншот оплаты) —
бот общается только с администратором:

1. Клиент доходит до страницы «Оформить заказ» → сайт сразу отправляет чек
   заказа на backend.py, а тот кладёт уведомление в очередь. Бот забирает
   его при следующем опросе и присылает администратору чек. Если выбрана
   «Доставка» — под чеком есть кнопка «📦 Указать стоимость доставки».
2. Администратор нажимает кнопку → бот просит прислать сумму доставки
   обычным сообщением (только число) → администратор присылает число →
   бот сохраняет стоимость через backend.py. После этого у клиента на
   сайте открывается бланк оплаты (пункт 3.А ТЗ). При «Самовывозе» доставка
   сразу 0 и бланк открывается клиенту без ожидания.
3. Клиент заполняет бланк на сайте (имя, адрес/время, скриншот) → это тоже
   уходит в очередь → бот присылает администратору скриншот оплаты с
   подписью (состав заказа, сумма, адрес, время) и кнопками «Подтвердить
   оплату» / «Отказать».
4. Администратор нажимает кнопку → бот обновляет статус заказа через
   backend.py. Статус на сайте у клиента меняется сам (сайт опрашивает
   backend.py), без ручных действий на сайте.

### Настройка (обязательно перед запуском)

1. Получите токен бота у @BotFather (команда /newbot) и впишите его ниже,
   в BOT_TOKEN.
2. Узнайте id вашего чата/группы администратора (добавьте туда бота с
   правом отправки сообщений, затем узнайте id, например через
   @getmyid_bot / @userinfobot) и впишите в ADMIN_CHAT_ID ниже.
3. В backend.py впишите PICKUP_ADDRESS / PICKUP_HOURS (для самовывоза) и
   PAYMENT_CARD_NUMBER / PAYMENT_CARD_HOLDER (реквизиты, их показывает
   бланк оплаты на сайте).
4. Запустите backend.py хотя бы один раз до этого бота — он создаст рядом
   internal_secret.txt, который бот использует, чтобы обращаться к
   служебному API backend.py.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Настройки — на Railway задаются переменными окружения (Variables вкладка
# сервиса бота). Локально можно оставить пустыми и вписать сюда вручную —
# тогда используются значения по умолчанию ниже.
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')  # токен от @BotFather
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID', '')  # id чата/канала администратора

# Публичный адрес сервиса backend на Railway, например:
# https://stroyai-backend-production.up.railway.app (БЕЗ / на конце).
# Локально, если бот и backend на одном компьютере, можно оставить дефолт.
BACKEND_URL = os.environ.get('BACKEND_URL', 'http://127.0.0.1:8000').rstrip('/')
NOTIFICATIONS_POLL_EVERY = 1  # опрашивать backend.py на новые чеки/бланки каждые N циклов getUpdates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# INTERNAL_SECRET можно передать напрямую переменной окружения (нужно на
# Railway — там backend и бот это разные контейнеры без общей файловой
# системы, поэтому internal_secret.txt между ними не расшарить). Если не
# задана — как и раньше, читаем файл рядом (для локального запуска).
INTERNAL_SECRET_ENV = os.environ.get('INTERNAL_SECRET', '')
INTERNAL_SECRET_PATH = os.path.join(BASE_DIR, 'internal_secret.txt')
STATE_PATH = os.path.join(BASE_DIR, 'bot_state.json')  # переживает перезапуск бота
FONT_REGULAR_PATH = os.path.join(BASE_DIR, 'assets', 'fonts', 'DejaVuSans.ttf')
FONT_BOLD_PATH = os.path.join(BASE_DIR, 'assets', 'fonts', 'DejaVuSans-Bold.ttf')


# ---------------------------------------------------------------------------
# Внутреннее состояние:
#   orders[order_number] = {receipt_msg_id, admin_message_id, is_photo}
#     — id сообщений в чате администратора, чтобы потом их редактировать
#       (проставлять вердикт «оплата подтверждена/отклонена»).
#   admin_pending[chat_id] = order_number
#     — администратор нажал «Указать доставку» и сейчас должен прислать
#       число; следующее его текстовое сообщение — это сумма доставки.
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data.setdefault('orders', {})
                data.setdefault('admin_pending', {})
                data.setdefault('update_offset', 0)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {'orders': {}, 'admin_pending': {}, 'update_offset': 0}


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_internal_secret():
    if INTERNAL_SECRET_ENV:
        return INTERNAL_SECRET_ENV
    if not os.path.exists(INTERNAL_SECRET_PATH):
        raise SystemExit(
            'Не задан INTERNAL_SECRET (переменная окружения) и не найден '
            'internal_secret.txt рядом с ботом. На Railway задайте переменную '
            'INTERNAL_SECRET в сервисе бота тем же значением, что и в сервисе '
            'backend. Локально можно один раз запустить backend.py — он сам '
            'создаст internal_secret.txt рядом.'
        )
    with open(INTERNAL_SECRET_PATH, 'r', encoding='utf-8') as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# Обёртки над Telegram Bot API и внутренним API backend.py (всё на urllib)
# ---------------------------------------------------------------------------
def tg_call(method, params=None):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    data = json.dumps(params or {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f'[telegram] Ошибка {method}: {e.read().decode("utf-8", "ignore")}')
        return {'ok': False}
    except urllib.error.URLError as e:
        print(f'[telegram] Сеть недоступна ({method}): {e}')
        return {'ok': False}


def backend_call(method, path, body=None):
    url = BACKEND_URL + '/api' + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-Internal-Secret', INTERNAL_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'ignore')
        print(f'[backend] Ошибка {method} {path}: {detail}')
        return None
    except urllib.error.URLError as e:
        print(f'[backend] backend.py недоступен ({BACKEND_URL}) — запущен ли он? {e}')
        return None


def send_message(chat_id, text, reply_markup=None):
    params = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        params['reply_markup'] = reply_markup
    return tg_call('sendMessage', params)


def edit_message_caption(chat_id, message_id, caption, reply_markup=None):
    params = {'chat_id': chat_id, 'message_id': message_id, 'caption': caption, 'parse_mode': 'HTML'}
    params['reply_markup'] = reply_markup or {'inline_keyboard': []}
    return tg_call('editMessageCaption', params)


def edit_message_text(chat_id, message_id, text, reply_markup=None):
    params = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    params['reply_markup'] = reply_markup or {'inline_keyboard': []}
    return tg_call('editMessageText', params)


def send_photo(chat_id, photo_bytes, filename, mime_type, caption=None, reply_markup=None):
    """
    Отправляет фото (скриншот оплаты, присланный клиентом через бланк на
    сайте) администратору. В отличие от остальных вызовов, sendPhoto с
    реальными байтами файла требует multipart/form-data, а не JSON — поэтому
    собираем тело запроса вручную (без pip install, только urllib).
    """
    boundary = uuid.uuid4().hex
    parts = []

    def add_field(name, value):
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
            .encode('utf-8')
        )

    add_field('chat_id', chat_id)
    if caption:
        add_field('caption', caption)
        add_field('parse_mode', 'HTML')
    if reply_markup:
        add_field('reply_markup', json.dumps(reply_markup))
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
         f'Content-Type: {mime_type}\r\n\r\n').encode('utf-8')
    )
    parts.append(photo_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(parts)

    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto'
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f'[telegram] Ошибка sendPhoto: {e.read().decode("utf-8", "ignore")}')
        return {'ok': False}
    except urllib.error.URLError as e:
        print(f'[telegram] Сеть недоступна (sendPhoto): {e}')
        return {'ok': False}


def answer_callback_query(callback_query_id, text=None):
    params = {'callback_query_id': callback_query_id}
    if text:
        params['text'] = text
    tg_call('answerCallbackQuery', params)


# ---------------------------------------------------------------------------
# Форматирование сообщений
# ---------------------------------------------------------------------------
def fmt_sum(amount):
    return f'{amount:,.0f}'.replace(',', ' ') + ' сум'


def format_order_summary(order):
    lines = [f'<b>Заказ №{order["order_number"]}</b>']
    lines.append('Режим получения: ' + order['mode_display'])
    lines.append('')
    lines.append('<b>Состав заказа:</b>')
    for it in order['items']:
        name = it['product']['name'] if it['product'] else 'Товар'
        lines.append(f'• {name} × {it["quantity"]} — {fmt_sum(it["line_total"])}')
    if order['mode'] == 'delivery':
        if order.get('delivery_quoted'):
            lines.append(f'• Доставка — {fmt_sum(order["delivery_fee"])}')
        elif order.get('estimated_delivery_fee'):
            lines.append(f'• Доставка — ожидает расчёта (ориентировочно {fmt_sum(order["estimated_delivery_fee"])})')
        else:
            lines.append('• Доставка — ожидает расчёта')
    lines.append('')
    lines.append(f'<b>Итого: {fmt_sum(order["total"])}</b>')
    return '\n'.join(lines)


def format_receipt_message(order):
    """Чек заказа, который администратор получает сразу при переходе клиента
    на страницу «Оформить заказ» (пункт 2 ТЗ) — до того, как клиент указал
    данные получения. Предоплаты нет — оплата на месте."""
    summary = format_order_summary(order)
    contact = order['customer'].get('phone') or order['customer'].get('email') or '—'
    lines = ['🧾 <b>Новый заказ</b>', '', summary, '', f'Клиент: {contact}']
    if order['mode'] == 'delivery' and not order.get('delivery_quoted'):
        lines.append('')
        lines.append('👇 Укажите стоимость доставки, чтобы клиент мог оформить заказ.')
    return '\n'.join(lines)


def receipt_keyboard(order_number):
    return {'inline_keyboard': [[
        {'text': '📦 Указать стоимость доставки', 'callback_data': f'setdeliv:{order_number}'}
    ]]}


# ---------------------------------------------------------------------------
# Чек заказа КАРТИНКОЙ (как печатный бланк на сайте) — присылается в канал
# вместе с текстовым сообщением, чтобы там сразу было видно всё оформление
# в привычном виде, не открывая сайт. Требует Pillow (pip install Pillow);
# если библиотеки нет — бот просто не прикладывает картинку и работает
# как раньше, только текстом (см. PIL_AVAILABLE).
# ---------------------------------------------------------------------------
_FONT_CACHE = {}


def _font(size, bold=False):
    key = (size, bold)
    if key not in _FONT_CACHE:
        path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except OSError:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _text_w(draw, text, font):
    return draw.textlength(text, font=font)


def _wrap_text(draw, text, font, max_width):
    """Простой перенос по словам под ширину колонки — чтобы длинные названия
    товаров не вылезали за таблицу на картинке чека."""
    words = text.split(' ')
    lines, cur = [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if _text_w(draw, trial, font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or ['']


def build_receipt_image(order):
    """Рисует картинку чека заказа (шапка STROYAI, состав, итог, оплата/
    получение, подпись) в том же виде, что и печатный бланк на сайте —
    и возвращает её байты (JPEG), готовые к отправке через send_photo()."""
    W = 900
    PAD = 40
    COL_X = [PAD, PAD + 40, PAD + 40 + 380, PAD + 40 + 380 + 160, W - PAD]
    black, gray, line_gray, header_bg = (17, 17, 17), (90, 90, 90), (210, 210, 210), (238, 238, 238)

    tmp = Image.new('RGB', (10, 10))
    d = ImageDraw.Draw(tmp)
    f_title = _font(26, bold=True)
    f_sub = _font(14)
    f_label = _font(14)
    f_value = _font(14, bold=True)
    f_th = _font(13, bold=True)
    f_td = _font(13)
    f_total_label = _font(17, bold=True)
    f_section = _font(14, bold=True)
    f_footer = _font(16, bold=True)
    f_footer_sub = _font(12)

    date = order.get('created_at', '')[:10]
    try:
        y, m, dd = date.split('-')
        date = f'{dd}.{m}.{y}'
    except ValueError:
        pass
    customer = order.get('customer') or {}
    address = (order.get('delivery_address') if order['mode'] == 'delivery' else order.get('pickup_address')) or '—'
    meta_rows = [
        ('Дата:', date),
        ('Способ получения:', order.get('mode_display', '')),
        ('Статус:', f"{order.get('status_display','')} · {order.get('payment_status_display','')}"),
        ('Клиент:', order.get('customer_name') or customer.get('name') or 'Клиент'),
        ('Телефон:', customer.get('phone') or '—'),
        ('Адрес:', address),
    ]

    items = order.get('items', [])
    item_lines = []  # (row_lines_count, [(no, name_lines, qty, price, total)])
    for i, it in enumerate(items):
        p = it.get('product')
        name = p['name'] if p else 'Товар удалён'
        name_lines = _wrap_text(d, name, f_td, COL_X[2] - COL_X[1] - 12)
        qty = f"{it['quantity']} {p['unit']}" if p else str(it['quantity'])
        item_lines.append((i + 1, name_lines, qty, fmt_sum(it['price']), fmt_sum(it['line_total'])))

    # --- считаем итоговую высоту картинки заранее ---
    y_cursor = PAD
    y_cursor += 34  # заголовок
    y_cursor += 22  # подзаголовок
    y_cursor += 14  # отступ
    y_cursor += len(meta_rows) * 24  # мета-таблица
    y_cursor += 16
    y_cursor += 34  # шапка таблицы товаров
    for _, name_lines, *_r in item_lines:
        y_cursor += max(1, len(name_lines)) * 19 + 10
    y_cursor += 10
    if order['mode'] == 'delivery':
        y_cursor += 26
    y_cursor += 40  # итого
    y_cursor += 30  # заголовок "оплата и получение"
    y_cursor += 24 * 2
    y_cursor += 20
    y_cursor += 26 + 20  # спасибо + поддержка
    y_cursor += PAD

    img = Image.new('RGB', (W, int(y_cursor)), (255, 255, 255))
    d = ImageDraw.Draw(img)
    y = PAD

    d.text((PAD, y), f"STROYAI — ЗАКАЗ № {order.get('order_number') or order['id']}", font=f_title, fill=black)
    y += 34
    d.text((PAD, y), 'Ремонт и материалы — Ташкент и область', font=f_sub, fill=gray)
    y += 28

    for label, value in meta_rows:
        d.text((PAD, y), label, font=f_label, fill=gray)
        d.text((PAD + 170, y), str(value), font=f_value, fill=black)
        y += 24
    y += 12
    d.line([(PAD, y), (W - PAD, y)], fill=line_gray, width=1)
    y += 4

    # --- таблица товаров ---
    th_y0 = y
    d.rectangle([PAD, y, W - PAD, y + 32], fill=header_bg)
    headers = ['№', 'Наименование', 'Кол-во', 'Цена', 'Сумма']
    for i, htext in enumerate(headers):
        x = COL_X[i] + 6
        if i == 4:
            tw = _text_w(d, htext, f_th)
            x = COL_X[i] - tw
        d.text((x, y + 8), htext, font=f_th, fill=black)
    y += 32
    d.line([(PAD, y), (W - PAD, y)], fill=black, width=2)
    y += 6

    for no, name_lines, qty, price, total in item_lines:
        row_h = max(1, len(name_lines)) * 19
        d.text((COL_X[0] + 6, y), str(no), font=f_td, fill=black)
        for li, nl in enumerate(name_lines):
            d.text((COL_X[1] + 6, y + li * 19), nl, font=f_td, fill=black)
        d.text((COL_X[2] + 6, y), qty, font=f_td, fill=black)
        d.text((COL_X[3] + 6, y), price, font=f_td, fill=black)
        tw = _text_w(d, total, f_td)
        d.text((COL_X[4] - tw, y), total, font=f_td, fill=black)
        y += row_h + 10
        d.line([(PAD, y - 4), (W - PAD, y - 4)], fill=line_gray, width=1)

    y += 8
    if order['mode'] == 'delivery':
        deliv = fmt_sum(order['delivery_fee']) if order.get('delivery_quoted') else 'ожидает расчёта'
        d.text((PAD, y), 'Доставка:', font=f_label, fill=gray)
        tw = _text_w(d, deliv, f_value)
        d.text((W - PAD - tw, y), deliv, font=f_value, fill=black)
        y += 26

    d.line([(PAD, y), (W - PAD, y)], fill=black, width=2)
    y += 8
    total_text = fmt_sum(order['total'])
    d.text((PAD, y), 'Итого:', font=f_total_label, fill=black)
    tw = _text_w(d, total_text, f_total_label)
    d.text((W - PAD - tw, y), total_text, font=f_total_label, fill=black)
    y += 40

    d.text((PAD, y), 'ОПЛАТА И ПОЛУЧЕНИЕ', font=f_section, fill=black)
    y += 28
    pay_rows = [
        ('Статус оплаты:', order.get('payment_status_display', '')),
        ('Способ получения:', order.get('mode_display', '')),
    ]
    for label, value in pay_rows:
        d.text((PAD, y), label, font=f_td, fill=black)
        tw = _text_w(d, value, f_td)
        d.text((W - PAD - tw, y), value, font=f_td, fill=black)
        y += 24
    y += 16

    footer1 = 'СПАСИБО ЗА ЗАКАЗ!'
    tw = _text_w(d, footer1, f_footer)
    d.text(((W - tw) / 2, y), footer1, font=f_footer, fill=black)
    y += 26
    footer2 = 'Поддержка: +998 78 150-00-00'
    tw = _text_w(d, footer2, f_footer_sub)
    d.text(((W - tw) / 2, y), footer2, font=f_footer_sub, fill=gray)

    import io
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


def admin_caption(order):
    """Сообщение о том, что клиент подтвердил данные получения заказа на
    сайте (пункт 5 ТЗ) — состав заказа + имя/адрес/время. Оплата — на месте,
    без предоплаты и скриншотов."""
    summary = format_order_summary(order)
    contact = order['customer'].get('phone') or order['customer'].get('email') or '—'
    lines = ['📋 <b>Заказ подтверждён клиентом</b>', '', summary, '']
    lines.append(f'Заказчик: {order.get("customer_name") or order["customer"]["name"]}')
    lines.append(f'Контакт: {contact}')
    if order['mode'] == 'delivery':
        lines.append(f'Адрес доставки: {order.get("delivery_address") or "—"}')
    else:
        lines.append('Способ получения: самовывоз')
    lines.append(f'Желаемое время: {order.get("desired_time") or "—"}')
    lines.append('')
    lines.append('Оплата — на месте, при получении.')
    return '\n'.join(lines)


ADMIN_KEYBOARD_TEMPLATE = {
    'inline_keyboard': [[
        {'text': '✅ Отметить оплаченным', 'callback_data': 'confirm:{n}'},
        {'text': '❌ Проблема с оплатой', 'callback_data': 'reject:{n}'},
    ]]
}


def admin_keyboard(order_number):
    kb = json.loads(json.dumps(ADMIN_KEYBOARD_TEMPLATE))
    for btn in kb['inline_keyboard'][0]:
        btn['callback_data'] = btn['callback_data'].format(n=order_number)
    return kb


# ---------------------------------------------------------------------------
# Обработка входящих обновлений от Telegram (сообщения/кнопки в чате)
# ---------------------------------------------------------------------------
def handle_start(chat_id, order_number):
    # Клиент больше ничего не оплачивает через бота (всё на сайте) — просто
    # вежливо подсказываем, если кто-то всё же перешёл по старой ссылке.
    order = backend_call('GET', f'/internal/orders/by-number/{order_number}/')
    if not order:
        send_message(chat_id, f'Заказ {order_number} не найден. Проверьте ссылку или обратитесь в поддержку.')
        return
    send_message(
        chat_id,
        f'Заказ №{order_number}: {order["payment_status_display"]}.\n\n'
        f'Оформление и оплата заказа происходят на сайте, в разделе «Оформить заказ» — '
        f'откройте его там, чтобы увидеть статус и, при необходимости, бланк оплаты.'
    )


def send_receipt_photo(order):
    """Отдельным сообщением шлёт в канал картинку готового чека (когда сумма
    уже окончательная) — используется после того, как админ вписал стоимость
    доставки, см. handle_admin_price_reply()."""
    if not PIL_AVAILABLE:
        return
    try:
        photo_bytes = build_receipt_image(order)
    except Exception as e:  # noqa: BLE001 — картинка необязательна
        print(f'[bot] Не удалось нарисовать картинку чека №{order["order_number"]}: {e!r}')
        return
    send_photo(ADMIN_CHAT_ID, photo_bytes, f'receipt_{order["order_number"]}.jpg', 'image/jpeg')


def handle_admin_price_reply(state, chat_id, text):
    order_number = state['admin_pending'].get(str(chat_id))
    digits = re.sub(r'[^0-9]', '', text)
    if not digits:
        send_message(chat_id, 'Не понял сумму. Пришлите стоимость доставки только цифрами, например: 35000')
        return
    fee = int(digits)
    order = backend_call('POST', f'/internal/orders/by-number/{order_number}/set-delivery/', {'fee': fee})
    if not order:
        send_message(chat_id, 'Не удалось сохранить стоимость доставки — backend.py недоступен. Попробуйте ещё раз.')
        return
    state['admin_pending'].pop(str(chat_id), None)
    save_state(state)
    send_message(
        chat_id,
        f'✅ Стоимость доставки для заказа №{order_number}: {fmt_sum(fee)}.\n'
        f'Итого к оплате: {fmt_sum(order["total"])}. Клиенту открылся бланк оплаты на сайте.'
    )
    # Убираем кнопку «Указать доставку» из чека, чтобы её больше нельзя было нажать повторно.
    receipt_msg_id = state.get('orders', {}).get(order_number, {}).get('receipt_msg_id')
    if receipt_msg_id:
        full_text = format_receipt_message(order)
        if state.get('orders', {}).get(order_number, {}).get('receipt_is_photo'):
            caption = full_text if len(full_text) <= 1024 else (full_text[:1000].rsplit('\n', 1)[0] + '\n…')
            edit_message_caption(chat_id, receipt_msg_id, caption, reply_markup=None)
        else:
            edit_message_text(chat_id, receipt_msg_id, full_text, reply_markup=None)
    # Сумма теперь окончательная — шлём картинку готового чека (см. флоу на скриншоте пользователя).
    send_receipt_photo(order)


def handle_callback_query(state, callback_query):
    data = callback_query.get('data', '')
    action, _, order_number = data.partition(':')
    message = callback_query['message']
    admin_chat_id = message['chat']['id']
    admin_msg_id = message['message_id']

    if action == 'setdeliv':
        state.setdefault('admin_pending', {})[str(admin_chat_id)] = order_number
        save_state(state)
        answer_callback_query(callback_query['id'])
        send_message(admin_chat_id, f'Введите стоимость доставки для заказа №{order_number} одним числом '
                                     f'(сум), например: 35000')
        return

    if action not in ('confirm', 'reject'):
        answer_callback_query(callback_query['id'])
        return

    endpoint = 'confirm' if action == 'confirm' else 'reject'
    order = backend_call('POST', f'/internal/orders/by-number/{order_number}/{endpoint}/')
    if not order:
        answer_callback_query(callback_query['id'], text='Ошибка: backend.py недоступен')
        return

    verdict = '✅ ОПЛАТА ПОДТВЕРЖДЕНА' if action == 'confirm' else '❌ ОПЛАТА ОТКЛОНЕНА'
    new_caption = admin_caption(order) + f'\n\n<b>{verdict}</b>'
    is_photo = state.get('orders', {}).get(order_number, {}).get('is_photo')
    if is_photo:
        edit_message_caption(admin_chat_id, admin_msg_id, new_caption, reply_markup=None)
    else:
        edit_message_text(admin_chat_id, admin_msg_id, new_caption, reply_markup=None)
    answer_callback_query(callback_query['id'], text='Готово')
    save_state(state)


def process_update(state, update):
    # В КАНАЛЕ (в отличие от группы) сообщения администратора приходят не как
    # обычное 'message', а как отдельный тип 'channel_post' — раньше бот его
    # не слушал вовсе, из-за чего ответ с суммой доставки просто терялся.
    message = update.get('message') or update.get('channel_post')
    if message:
        chat_id = message['chat']['id']
        text = message.get('text', '')
        is_admin = bool(ADMIN_CHAT_ID) and str(chat_id) == str(ADMIN_CHAT_ID)
        if text.startswith('/start'):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                handle_start(chat_id, parts[1].strip())
            else:
                send_message(chat_id, 'Здравствуйте! Оформление и оплата заказа происходят на сайте, '
                                       'в разделе «Оформить заказ».')
        elif is_admin and str(chat_id) in state.get('admin_pending', {}) and text and not text.startswith('/'):
            handle_admin_price_reply(state, chat_id, text)
        # прочие сообщения (в т.ч. случайные фото не по делу) — молча игнорируем
    elif 'callback_query' in update:
        handle_callback_query(state, update['callback_query'])


# ---------------------------------------------------------------------------
# Опрос backend.py на новые чеки заказов и заполненные бланки оплаты
# ---------------------------------------------------------------------------
def process_notification(state, note):
    """
    Возвращает True, если сообщение реально ушло в Telegram — только тогда
    вызывающий код подтвердит (ack) уведомление backend.py. Если ADMIN_CHAT_ID
    не настроен или Telegram не ответил успехом (сеть, бот не в чате и т.п.),
    возвращаем False — уведомление останется в очереди и будет отправлено
    повторно на следующем опросе, а не потеряется навсегда.
    """
    kind = note['kind']
    order = note['order']
    order_number = order['order_number']
    if not ADMIN_CHAT_ID:
        print('[bot] ADMIN_CHAT_ID не настроен — не могу доставить уведомление, попробую позже.')
        return False

    if kind == 'receipt':
        text = format_receipt_message(order)
        needs_delivery_btn = (order['mode'] == 'delivery' and not order.get('delivery_quoted'))
        kb = receipt_keyboard(order_number) if needs_delivery_btn else None

        # Картинку чека шлём только когда сумма УЖЕ окончательная: при
        # самовывозе она известна сразу, а при доставке — только после того,
        # как админ впишет стоимость доставки (это отдельным шагом отправит
        # send_receipt_photo() из handle_admin_price_reply). Пока доставка не
        # посчитана, здесь как раньше уходит обычный текст с кнопкой.
        photo_bytes = None
        if PIL_AVAILABLE and not needs_delivery_btn:
            try:
                photo_bytes = build_receipt_image(order)
            except Exception as e:  # noqa: BLE001 — картинка необязательна, не роняем уведомление
                print(f'[bot] Не удалось нарисовать картинку чека №{order_number}: {e!r}')

        if photo_bytes:
            # У caption для sendPhoto жёсткий лимит Телеграма — 1024 символа.
            # У большинства заказов текст короче, но на всякий случай (много
            # позиций) подрежем подпись — вся детализация всё равно видна на
            # самой картинке чека, так что подпись — это только шапка.
            caption = text if len(text) <= 1024 else (text[:1000].rsplit('\n', 1)[0] + '\n…')
            result = send_photo(ADMIN_CHAT_ID, photo_bytes, f'receipt_{order_number}.jpg', 'image/jpeg',
                                 caption=caption, reply_markup=kb)
            is_photo = True
        else:
            result = send_message(ADMIN_CHAT_ID, text, reply_markup=kb)
            is_photo = False

        if result and result.get('ok'):
            state.setdefault('orders', {}).setdefault(order_number, {})['receipt_msg_id'] = \
                result['result']['message_id']
            state['orders'][order_number]['receipt_is_photo'] = is_photo
            save_state(state)
            return True
        print(f'[bot] Не удалось отправить чек заказа №{order_number} — попробую снова на следующем опросе.')
        return False

    elif kind == 'blank_submitted':
        # Предоплаты и скриншотов больше нет — просто текстовое сообщение
        # с данными, которые клиент указал на сайте (имя, адрес, время).
        caption = admin_caption(order)
        result = send_message(ADMIN_CHAT_ID, caption, reply_markup=admin_keyboard(order_number))
        if result and result.get('ok'):
            state.setdefault('orders', {}).setdefault(order_number, {})['admin_message_id'] = \
                result['result']['message_id']
            state['orders'][order_number]['is_photo'] = False
            save_state(state)
            return True
        print(f'[bot] Не удалось отправить бланк оплаты заказа №{order_number} — попробую снова на следующем опросе.')
        return False

    elif kind == 'order_cancelled':
        text = f'🚫 <b>Заказ отменён клиентом</b>\n\nЗаказ №{order_number} отменён.\nИтого было: {fmt_sum(order["total"])}'
        result = send_message(ADMIN_CHAT_ID, text)
        return bool(result and result.get('ok'))

    elif kind == 'order_updated':
        contact = order['customer'].get('phone') or order['customer'].get('email') or '—'
        lines = [f'✏️ <b>Клиент изменил данные заказа №{order_number}</b>', '']
        lines.append(f'Заказчик: {order.get("customer_name") or order["customer"]["name"]}')
        lines.append(f'Контакт: {contact}')
        if order['mode'] == 'delivery':
            lines.append(f'Адрес доставки: {order.get("delivery_address") or "—"}')
        lines.append(f'Желаемое время: {order.get("desired_time") or "—"}')
        result = send_message(ADMIN_CHAT_ID, '\n'.join(lines))
        return bool(result and result.get('ok'))

    # Неизвестный тип уведомления — подтверждаем, чтобы не зациклиться на нём навсегда.
    return True


def poll_backend_notifications(state):
    result = backend_call('GET', '/internal/notifications/pending/')
    if not result:
        return
    for note in result:
        try:
            delivered = process_notification(state, note)
        except Exception as e:  # noqa: BLE001 — одно плохое уведомление не должно ронять бота
            print(f'[bot] Ошибка обработки уведомления №{note.get("id")}: {e!r}')
            delivered = False
        # ack шлём в backend ТОЛЬКО если сообщение реально ушло в Telegram —
        # иначе уведомление осталось бы неотправленным навсегда при любом
        # сбое (сеть, бот не добавлен в канал и т.п.).
        if delivered:
            backend_call('POST', f'/internal/notifications/{note["id"]}/ack/')


def main():
    global INTERNAL_SECRET
    if not BOT_TOKEN:
        raise SystemExit('Впишите BOT_TOKEN в начале telegram_bot.py (токен от @BotFather) и запустите снова.')
    INTERNAL_SECRET = load_internal_secret()

    state = load_state()
    print('=' * 60)
    print('StroyAI — Telegram-бот администратора запущен')
    print(f'Backend: {BACKEND_URL}')

    # Быстрая самопроверка при старте — чтобы сразу было видно причину,
    # если чеки/скриншоты не доходят до администратора.
    me = tg_call('getMe')
    if me and me.get('ok'):
        print(f'Токен бота рабочий: @{me["result"].get("username")}')
    else:
        print('ВНИМАНИЕ: не удалось проверить токен бота (getMe). Проверьте BOT_TOKEN и интернет.')

    if ADMIN_CHAT_ID:
        test = send_message(ADMIN_CHAT_ID, '🤖 Бот запущен и готов присылать чеки заказов.')
        if test and test.get('ok'):
            print(f'Тестовое сообщение отправлено в чат {ADMIN_CHAT_ID} — проверьте, пришло ли оно.')
        else:
            print(f'ВНИМАНИЕ: не удалось отправить сообщение в чат {ADMIN_CHAT_ID}.')
            print('Обычно причина: бот не добавлен в этот чат/канал, либо не имеет права писать туда.')
    else:
        print('ВНИМАНИЕ: ADMIN_CHAT_ID не настроен — чеки и скриншоты некому будет показать.')

    ping = backend_call('GET', '/internal/notifications/pending/')
    if ping is None:
        print(f'ВНИМАНИЕ: backend.py не отвечает по адресу {BACKEND_URL} — он точно запущен?')
    else:
        print(f'Связь с backend.py в порядке (в очереди сейчас: {len(ping)}).')

    print('Ожидание сообщений (Ctrl+C для остановки)…')
    print('=' * 60)

    offset = state.get('update_offset', 0)
    loop_count = 0
    while True:
        try:
            result = tg_call('getUpdates', {'offset': offset, 'timeout': 10})
        except KeyboardInterrupt:
            print('\nОстановлено.')
            return
        if not result or not result.get('ok'):
            time.sleep(3)
        else:
            for update in result.get('result', []):
                offset = update['update_id'] + 1
                try:
                    process_update(state, update)
                except Exception as e:  # noqa: BLE001 — не роняем бота на одном плохом апдейте
                    print(f'[bot] Ошибка обработки апдейта: {e!r}')
                state['update_offset'] = offset
                save_state(state)

        loop_count += 1
        if loop_count >= NOTIFICATIONS_POLL_EVERY:
            loop_count = 0
            try:
                poll_backend_notifications(state)
            except KeyboardInterrupt:
                print('\nОстановлено.')
                return
            except Exception as e:  # noqa: BLE001
                print(f'[bot] Ошибка опроса уведомлений: {e!r}')


INTERNAL_SECRET = ''

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено.')
