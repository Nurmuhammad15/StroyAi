# Деплой StroyAI: Vercel + Railway + Neon

Архитектура: сайт (index.html, admin.html, картинки) — на **Vercel**;
`backend.py` и `telegram_bot.py` — два отдельных сервиса на **Railway**;
база данных — **Neon** (PostgreSQL). backend.py уже переведён с sqlite на
PostgreSQL (см. `backend.py` — использует `psycopg2` и переменную
окружения `DATABASE_URL`).

Понадобится: аккаунт на GitHub, Neon (neon.tech), Railway (railway.app),
Vercel (vercel.com) — везде можно войти через GitHub, бесплатно.

---

## 1. Neon — база данных

1. Зайдите на neon.tech → New Project (любой регион, ближе к Railway —
   обычно AWS us-east или Europe).
2. В панели проекта откройте **Connection Details** → скопируйте
   **Connection string** (вид
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`).
   Это и есть `DATABASE_URL` — сохраните его, понадобится на шаге 3.
3. Таблицы создавать не нужно — `backend.py` сам создаст их и засеет
   демо-каталог при первом запуске.

## 2. GitHub — залить код

Railway и Vercel оба деплоят из git-репозитория.

```bash
cd reno_deploy
git init
git add .
git commit -m "StroyAI: деплой на Vercel + Railway + Neon"
```

Создайте пустой репозиторий на github.com (без README), затем:

```bash
git remote add origin https://github.com/ВАШ-АККАУНТ/stroyai.git
git branch -M main
git push -u origin main
```

## 3. Railway — backend и бот (два сервиса)

### 3.1 Сервис backend

1. railway.app → New Project → **Deploy from GitHub repo** → выберите
   репозиторий.
2. Railway сам определит Python-проект по `requirements.txt` и поставит
   зависимости (`psycopg2-binary`, `Pillow`).
3. Откройте сервис → **Variables** → добавьте:
   - `DATABASE_URL` = строка подключения из Neon (шаг 1.2)
   - `INTERNAL_SECRET` = любая длинная случайная строка, например
     сгенерируйте так: `python3 -c "import secrets; print(secrets.token_hex(24))"`
     — запомните её, она понадобится и для сервиса бота (должна совпадать!)
   - `AUTO_START_BOT` = `0` (бот теперь отдельный сервис, backend не должен
     сам его запускать)
   - `BREVO_API_KEY` = ключ Brevo (вида `xkeysib-...`) — Railway блокирует
     SMTP, поэтому письма уходят по HTTP API, а не через Gmail/smtplib.
     Как получить — см. ниже, «Настройка email для OTP»
   - `EMAIL_HOST_USER` = почта-отправитель, ПОДТВЕРЖДЁННАЯ в Brevo
   - `ADMIN_PANEL_PASSWORD` = длинный случайный пароль админ-панели.
     Без него панель `/admin` открыта паролем `admin123` — из неё можно
     стереть всю базу
   - `LLM_API_KEY` / `LLM_PROTOCOL` / `LLM_BASE_URL` / `LLM_MODEL` —
     ИИ-консультант и ИИ-Дизайн, см. раздел «Настройка LLM» ниже
4. **Settings** → **Start Command** → `python backend.py`
5. **Settings** → **Networking** → **Generate Domain** — Railway выдаст
   публичный адрес вида `stroyai-backend-production.up.railway.app`.
   Скопируйте его — понадобится для Vercel (шаг 4) и для бота (шаг 3.2).
6. Откройте `https://ВАШ-ДОМЕН/api/health/` в браузере — должно вернуть
   `{"ok": true, ...}`. Если 500-я ошибка — проверьте `DATABASE_URL` в Variables.

### 3.2 Сервис бота

1. В том же Railway-проекте → **+ New** → **GitHub Repo** → тот же
   репозиторий (это создаст второй, независимый сервис).
2. **Settings** → **Start Command** → `python telegram_bot.py`
3. **Variables**:
   - `BOT_TOKEN` = токен вашего бота от @BotFather
   - `ADMIN_CHAT_ID` = id чата/канала администратора
   - `BACKEND_URL` = `https://ВАШ-ДОМЕН.up.railway.app` (домен backend-сервиса
     из шага 3.1.5, БЕЗ `/` на конце)
   - `INTERNAL_SECRET` = **то же самое** значение, что вы задали в
     backend-сервисе на шаге 3.1.3
4. Публичный домен этому сервису не нужен — он просто опрашивает Telegram
   и backend, ничего не принимает напрямую из интернета.
5. Проверьте логи сервиса (**Deployments** → **View Logs**) — должно быть
   видно, что бот запустился без ошибок.

> ⚠️ В исходном `telegram_bot.py`, который вы прислали, токен и chat_id
> были вписаны прямо в код. Я вынес их в переменные окружения — но раз
> токен уже был в открытом виде, стоит на всякий случай перевыпустить его
> через @BotFather (`/revoke`) и вписать новый в Variables.

## 4. Vercel — сайт

1. vercel.com → **Add New → Project** → импортируйте тот же репозиторий.
2. Framework Preset — **Other** (это не Next.js/React, обычный статический
   сайт). Build Command и Output Directory оставьте пустыми — файлы отдаются
   как есть.
3. Пропишите адрес backend во фронтенде. Сейчас он зашит константой
   `API_BASE` в ДВУХ файлах — `index.html` (строка ~1379) и `admin.html`
   (строка ~293):

   ```js
   const API_BASE = 'https://ВАШ-BACKEND.up.railway.app/api';
   ```

   Замените на реальный домен из шага 3.1.5 в обоих файлах, закоммитьте и
   запушьте — Vercel передеплоит автоматически. `vercel.json` менять НЕ
   нужно: браузер ходит на Railway напрямую, а не через прокси Vercel
   (поэтому важно, чтобы домен сайта был в `CORS_ALLOWED_ORIGINS`).
4. Готово — сайт откроется на выданном Vercel-домене (или подключите свой
   домен в Settings → Domains). `/admin` откроет панель администратора,
   `/api/...` прозрачно проксируется на Railway-backend.

## 5. Проверка

- `https://ваш-сайт.vercel.app/` — открывается каталог, товары загружаются.
- `https://ваш-сайт.vercel.app/admin` — вход по `ADMIN_PANEL_PASSWORD`.
  Если переменная не задана, пароль равен `admin123`, а из панели доступен
  `/admin/wipe-data/`, стирающий заказы, клиентов и финансы. Задайте пароль
  ДО того, как показывать сайт кому-то ещё.
- Оформите тестовый заказ → в Telegram-канале администратора должен прийти
  чек.
- `python check_llm.py` — проверка ИИ-разделов (см. «Настройка LLM»).
- Реквизиты карты в `backend.py` (`PAYMENT_CARD_NUMBER`,
  `PAYMENT_CARD_HOLDER`) и `TELEGRAM_BOT_USERNAME` сейчас заглушки —
  впишите реальные, иначе бланк оплаты покажет клиенту нули.

## Частые проблемы

- **500 на `/api/health/`** — неверный или отсутствующий `DATABASE_URL`.
- **Бот не шлёт сообщения** — проверьте, что `INTERNAL_SECRET` в обоих
  Railway-сервисах совпадает дословно, и что `BACKEND_URL` у бота указывает
  на правильный домен backend-сервиса (без `/api` на конце).
- **CORS-ошибки в браузере** — домен сайта не в белом списке backend.
  Добавьте в Variables сервиса backend:
  `CORS_ALLOWED_ORIGINS=https://ваш-сайт.vercel.app` (через запятую, без
  пробелов, можно несколько). `localhost` разрешён всегда.
- **OTP-код не приходит на почту** — код всегда дублируется в логи Railway
  (сервис backend → вкладка **Deployments** → **Logs**), даже если письмо не
  ушло, ищите строку `>>> ОДНОРАЗОВЫЙ КОД для ...`. Рядом будет строка,
  начинающаяся с `[email]`, объясняющая, что пошло не так. Чаще всего:
  1. **`Brevo отказал в отправке (HTTP 401/403)`** — ключ неверен, либо
     отправитель `EMAIL_HOST_USER` не подтверждён в Brevo
     (Settings → Senders, Domains, IPs → Senders).
  2. **`BREVO_API_KEY/EMAIL_HOST_USER не заданы`** — переменные не
     добавлены в Variables сервиса backend (см. шаг 3.1).
  3. Письмо ушло, но не найдено — проверьте папку «Спам».
- **ИИ-консультант отвечает как раньше, по FAQ** — не задан `LLM_API_KEY`,
  либо запрос к провайдеру падает. Запустите `python check_llm.py` и
  смотрите строки `[support-chat]` / `[ai-design]` в логах Railway.

### Настройка LLM (ИИ-консультант поддержки + ИИ-Дизайн)

Оба ИИ-раздела ходят в модель через ОДНУ функцию `llm_complete()` в
`backend.py`. Провайдер задаётся переменными окружения, в коде адрес не
зашит. Пакет `openai` ставить НЕ нужно — backend делает тот же HTTP-запрос
через `urllib`, как и всё остальное в этом файле.

| Переменная | Что это |
|---|---|
| `LLM_PROTOCOL` | `anthropic` (по умолчанию) или `openai` |
| `LLM_BASE_URL` | базовый адрес **БЕЗ `/v1`** на конце |
| `LLM_API_KEY` | ключ (старое имя `ANTHROPIC_API_KEY` тоже работает) |
| `LLM_MODEL` | имя модели ровно как в списке моделей провайдера |
| `LLM_TIMEOUT` | таймаут запроса, секунд (по умолчанию 60) |

**Вариант А — официальный Anthropic** (ничего задавать не нужно, кроме ключа):

```
LLM_API_KEY=sk-ant-...
```

**Вариант Б — сторонний OpenAI-совместимый шлюз:**

```
LLM_PROTOCOL=openai
LLM_BASE_URL=https://адрес-шлюза.com
LLM_API_KEY=ключ-шлюза
LLM_MODEL=имя-модели-из-списка-шлюза
```

Разница только в форме запроса: при `anthropic` это
`POST {LLM_BASE_URL}/v1/messages` с заголовками `x-api-key` +
`anthropic-version`, при `openai` — `POST {LLM_BASE_URL}/v1/chat/completions`
с `Authorization: Bearer`, а системный промпт уезжает первым сообщением с
ролью `system`.

**Проверка перед деплоем.** Рядом с `backend.py` лежит `check_llm.py` — он
делает один короткий запрос и печатает диагноз:

```bash
python check_llm.py
```

Типичные ответы и что они значат:

- `HTTP 404` — почти всегда `/v1` написан дважды (в `LLM_BASE_URL` и в коде)
  или выбран не тот `LLM_PROTOCOL`. Скрипт печатает полный URL, который
  реально ушёл, — сверьте его с документацией провайдера.
- `провайдер вернул не JSON` — `LLM_BASE_URL` указывает на обычную
  веб-страницу, а не на API.
- `HTTP 401/403` — ключ неверный, отозван или не подходит к этому Base URL.
- `HTTP 400` со словом `model` — имя модели не из списка провайдера.

Если ключ не задан вообще: ИИ-консультант молча откатывается на старый
поиск по FAQ, ИИ-Дизайн возвращает понятную ошибку. Сайт не падает.

> ⚠️ Если сторонний шлюз отвечает только тогда, когда клиент подставляет
> в `User-Agent` строку браузера (`Mozilla/5.0 ...`) — это не техническое
> требование, а обход фильтрации. В `backend.py` намеренно стоит честный
> `User-Agent: StroyAI/1.0`.

**Про сам ИИ-Дизайн:** модель возвращает чертёж — структурированный список
комнат с площадями, который фронт рисует SVG-схемой. Это не
фотореалистичная картинка дома снаружи.

### Настройка email для OTP

Railway блокирует исходящий SMTP (порты 25/465/587/2525) на тарифах
Free/Trial/Hobby, поэтому Gmail + `smtplib` там не заработает в принципе —
ошибка `[Errno 101] Network is unreachable` это блокировка сети, а не
проблема пароля. Письма уходят через HTTP API Brevo.

1. Зарегистрируйтесь на https://www.brevo.com (300 писем/день бесплатно,
   карта не нужна).
2. Settings → Senders, Domains, IPs → Senders → Add a sender → впишите
   почту-отправителя → Brevo пришлёт на неё код → введите его. Домен и DNS
   настраивать не нужно.
3. Settings → SMTP & API → API Keys → Generate a new API key → скопируйте
   ключ вида `xkeysib-...`.
4. В Railway → сервис backend → **Variables**:
   ```
   BREVO_API_KEY=xkeysib-ваш-ключ
   EMAIL_HOST_USER=подтверждённая-в-brevo-почта@example.com
   ```
   `EMAIL_HOST_PASSWORD`, `EMAIL_HOST`, `EMAIL_PORT` больше не нужны и не
   читаются кодом.
5. Передеплойте (Railway делает это сам при сохранении переменных).

Если `BREVO_API_KEY` не задан — коды печатаются в логи и вход на сайт
продолжает работать.
