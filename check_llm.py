#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика подключения к LLM-провайдеру.

Запуск (локально или в консоли Railway):

    python check_llm.py

Читает те же переменные окружения и .env, что и backend.py, делает ОДИН
короткий запрос и печатает, что именно пошло не так. Ничего не меняет
в базе и не запускает сервер.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv():
    path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()

PROTOCOL = os.environ.get('LLM_PROTOCOL', 'anthropic').strip().lower()
BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.anthropic.com').strip().rstrip('/')
if BASE_URL.endswith('/v1'):
    BASE_URL = BASE_URL[:-3].rstrip('/')
API_KEY = os.environ.get('LLM_API_KEY', '').strip() or os.environ.get('ANTHROPIC_API_KEY', '').strip()
MODEL = os.environ.get('LLM_MODEL', '').strip() or os.environ.get(
    'ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'
).strip()

print('-' * 64)
print(f'Протокол : {PROTOCOL}')
print(f'Base URL : {BASE_URL}')
print(f'Модель   : {MODEL}')
print(f'Ключ     : {"…" + API_KEY[-4:] + f" (длина {len(API_KEY)})" if API_KEY else "НЕ ЗАДАН"}')
print('-' * 64)

if PROTOCOL not in ('anthropic', 'openai'):
    print(f'ОШИБКА: LLM_PROTOCOL="{PROTOCOL}" — допустимо только anthropic или openai.')
    sys.exit(1)

if not API_KEY:
    print('ОШИБКА: не задан LLM_API_KEY (или ANTHROPIC_API_KEY).')
    sys.exit(1)

PROMPT = 'Ответь одним словом: работает'

if PROTOCOL == 'openai':
    url = BASE_URL + '/v1/chat/completions'
    body = {
        'model': MODEL,
        'max_tokens': 50,
        'messages': [{'role': 'user', 'content': PROMPT}],
    }
    headers = {'Authorization': 'Bearer ' + API_KEY}
else:
    url = BASE_URL + '/v1/messages'
    body = {
        'model': MODEL,
        'max_tokens': 50,
        'messages': [{'role': 'user', 'content': PROMPT}],
    }
    headers = {'x-api-key': API_KEY, 'anthropic-version': '2023-06-01'}

headers.update({
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'User-Agent': 'StroyAI/1.0',
})

print(f'POST {url}')
req = urllib.request.Request(
    url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST', headers=headers
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode('utf-8', 'replace')
        status = resp.status
except urllib.error.HTTPError as e:
    raw = ''
    try:
        raw = e.read().decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        pass
    print(f'\nНЕ РАБОТАЕТ — HTTP {e.code}')
    print(f'Ответ провайдера: {raw[:800]}')
    print()
    hints = {
        401: 'Ключ неверный, отозван или не подходит к этому Base URL.',
        403: 'Ключ отклонён: нет доступа к этой модели, либо провайдер заблокировал запрос.',
        402: 'Нулевой баланс у провайдера.',
        404: f'Адрес не найден. Проверьте LLM_BASE_URL (должен быть БЕЗ /v1) и LLM_PROTOCOL '
             f'(сейчас {PROTOCOL} → путь {url[len(BASE_URL):]}).',
        429: 'Лимит запросов. Подождите и повторите.',
        500: 'Ошибка на стороне провайдера.',
        502: 'Провайдер недоступен (шлюз не отвечает).',
        503: 'Провайдер временно недоступен.',
    }
    if e.code in hints:
        print('Вероятная причина:', hints[e.code])
    if e.code in (400, 404) and 'model' in raw.lower():
        print('Похоже на неверное имя модели — возьмите его ровно из списка моделей провайдера.')
    sys.exit(1)
except (urllib.error.URLError, OSError) as e:
    print(f'\nНЕ РАБОТАЕТ — сеть: {type(e).__name__}: {e}')
    print('Проверьте LLM_BASE_URL (домен существует? https?) и доступность сети с этого сервера.')
    sys.exit(1)

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print(f'\nНЕ РАБОТАЕТ — провайдер вернул не JSON (HTTP {status}).')
    print(f'Первые 300 символов ответа: {raw[:300]}')
    print('Обычно это значит, что LLM_BASE_URL указывает на обычную веб-страницу, а не на API.')
    sys.exit(1)

if isinstance(data, dict) and data.get('error'):
    err = data['error']
    print(f'\nНЕ РАБОТАЕТ — провайдер вернул ошибку в теле ответа (HTTP {status}):')
    print(err.get('message') if isinstance(err, dict) else err)
    sys.exit(1)

try:
    if PROTOCOL == 'openai':
        text = (data['choices'][0]['message'].get('content') or '').strip()
    else:
        text = '\n'.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text').strip()
except (KeyError, IndexError, TypeError, AttributeError):
    print(f'\nНЕ РАБОТАЕТ — неожиданная структура ответа:\n{raw[:600]}')
    print(f'Возможно, протокол выбран неверно: сейчас LLM_PROTOCOL={PROTOCOL}.')
    sys.exit(1)

if not text:
    print(f'\nНЕ РАБОТАЕТ — пустой ответ модели:\n{raw[:600]}')
    sys.exit(1)

print(f'\nРАБОТАЕТ. Ответ модели: {text}')
usage = data.get('usage') or {}
if usage:
    print(f'Токены: {json.dumps(usage, ensure_ascii=False)}')
