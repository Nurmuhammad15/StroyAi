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
   - `EMAIL_HOST_USER` = ваша Gmail-почта, с которой будут отправляться
     OTP-коды (например `mystartup@gmail.com`)
   - `EMAIL_HOST_PASSWORD` = пароль приложения Gmail (16 символов без
     пробелов) — как получить см. ниже, «Настройка email для OTP»
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
3. Перед деплоем откройте `vercel.json` в репозитории и замените
   `ВАШ-BACKEND.up.railway.app` на реальный домен из шага 3.1.5:

   ```json
   { "source": "/api/:path*", "destination": "https://stroyai-backend-production.up.railway.app/api/:path*" }
   ```

   Закоммитьте и запушьте изменение (`git add vercel.json && git commit -m "backend url" && git push`) —
   Vercel передеплоит автоматически.
4. Готово — сайт откроется на выданном Vercel-домене (или подключите свой
   домен в Settings → Domains). `/admin` откроет панель администратора,
   `/api/...` прозрачно проксируется на Railway-backend.

## 5. Проверка

- `https://ваш-сайт.vercel.app/` — открывается каталог, товары загружаются.
- `https://ваш-сайт.vercel.app/admin` — вход по паролю `admin123` —
  **обязательно смените** его в `backend.py` (`ADMIN_PANEL_PASSWORD`) и
  запушьте изменение до того, как показывать сайт кому-то ещё.
- Оформите тестовый заказ → в Telegram-канале администратора должен прийти
  чек.
- Почта для OTP теперь настраивается через переменные окружения
  `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` (см. раздел «Настройка email
  для OTP» ниже) — старый пароль приложения, который раньше был вписан
  прямо в `backend.py`, стоит считать скомпрометированным и отозвать.
  Реквизиты карты для оплаты тоже проверьте в `backend.py` — это те данные,
  которые нужны в проде.

## Частые проблемы

- **500 на `/api/health/`** — неверный или отсутствующий `DATABASE_URL`.
- **Бот не шлёт сообщения** — проверьте, что `INTERNAL_SECRET` в обоих
  Railway-сервисах совпадает дословно, и что `BACKEND_URL` у бота указывает
  на правильный домен backend-сервиса (без `/api` на конце).
- **CORS-ошибки в браузере** — значит `vercel.json` ещё не задеплоен с
  правильным адресом backend, или вы открываете сайт напрямую по адресу
  Railway вместо Vercel-домена.
- **OTP-код не приходит на почту** — код всегда дублируется в логи Railway
  (сервис backend → вкладка **Deployments** → **Logs**), даже если письмо не
  ушло, ищите строку `>>> ОДНОРАЗОВЫЙ КОД для ...`. Рядом будет строка,
  начинающаяся с `[email]`, объясняющая, что пошло не так. Чаще всего:
  1. **`[email] ОШИБКА АВТОРИЗАЦИИ`** — пароль приложения неверный или
     отозван, либо на аккаунте Google выключена двухфакторная
     аутентификация (без неё пароли приложений не работают вообще).
     Зайдите на https://myaccount.google.com/apppasswords, отзовите старый
     пароль (он уже мог «засветиться», если файл кому-то пересылали) и
     создайте новый, впишите его в `EMAIL_HOST_PASSWORD` на Railway.
  2. **`EMAIL_HOST_USER/EMAIL_HOST_PASSWORD не заданы`** — переменные не
     добавлены в Variables сервиса backend (см. шаг 3.1).
  3. Письмо ушло, но не найдено — проверьте папку «Спам».

### ИИ-Дизайн: всё на OpenAI

Раздел «ИИ-Дизайн» на сайте работает в 2 шага, оба через один и тот же
`OPENAI_API_KEY`:
1. **Chat Completions** (модель `OPENAI_TEXT_MODEL`, по умолчанию `gpt-5.4-mini` —
   дешёвая модель, для доработки короткого текста топовая не нужна)
   разворачивает короткое пожелание клиента в подробное архитектурное
   описание.
2. **Images API** (модель `OPENAI_IMAGE_MODEL`, по умолчанию `gpt-image-2` —
   текущий флагман OpenAI по фото; `gpt-image-1` прекращает работу 23
   октября 2026, поэтому в новый проект его не ставим)
   по этому описанию рисует и схематичный чертёж фасада, и 3 фотореалистичных
   ракурса дома.

На Railway → сервис backend → Variables добавьте только `OPENAI_API_KEY`.
Если ключа нет — шаг улучшения промта тихо пропускается (используется
исходный текст клиента), а генерация картинок вернёт понятную ошибку вместо
падения сервера.

### Настройка email для OTP

1. На аккаунте Google, с которого будут отправляться коды, включите
   двухфакторную аутентификацию: https://myaccount.google.com/security
2. Откройте https://myaccount.google.com/apppasswords, создайте пароль
   приложения (имя — любое, например «StroyAI»). Google покажет 16-значный
   пароль — скопируйте его без пробелов.
3. В Railway → сервис backend → **Variables** добавьте `EMAIL_HOST_USER`
   (сама почта) и `EMAIL_HOST_PASSWORD` (пароль приложения) — см. шаг 3.1.
4. Передеплойте сервис (Railway делает это автоматически при сохранении
   переменных) и проверьте логи при следующем запросе OTP.
