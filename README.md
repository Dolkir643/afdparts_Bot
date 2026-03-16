# AFDparts Telegram Bot

Бот для поиска запчастей на [AFDparts.ru](https://afdparts.ru) по артикулу.

## Настройка

1. Создайте бота через [@BotFather](https://t.me/BotFather), получите токен.
2. Скопируйте конфиг:
   ```bash
   cp .env.example .env
   ```
3. В `.env` укажите `BOT_TOKEN`. Логин и пароль AFDparts уже заданы по умолчанию.

## Запуск

```bash
cd Desktop/addons/afdparts_bot
pip install -r requirements.txt
python tg_bot.py
```

После `/start` бот авторизуется на AFDparts.ru и будет искать запчасти по введённому артикулу.

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `BOT_TOKEN` | Токен Telegram-бота (обязательно) |
| `AFDPARTS_LOGIN` | Логин на afdparts.ru |
| `AFDPARTS_PASSWORD` | Пароль на afdparts.ru |
| `DEBUG_SAVE_HTML` | `1` — сохранять HTML ответов в файлы для отладки |
| `TELEGRAM_ORDER_CHAT_ID` | Куда слать уведомления о заявках (по умолчанию 232066339) |

## Деплой на Railway

1. Зарегистрируйтесь на [railway.app](https://railway.app), создайте новый проект.
2. **New** → **GitHub Repo** → выберите репозиторий с этим ботом (или **Deploy from GitHub** после пуша).
3. В настройках сервиса задайте **Variables** (переменные окружения):
   - `BOT_TOKEN` — токен от @BotFather (обязательно);
   - `AFDPARTS_LOGIN`, `AFDPARTS_PASSWORD` — логин и пароль AFDparts;
   - `TELEGRAM_ORDER_CHAT_ID` — ID чата для заявок (по умолчанию 232066339).
4. У бота нет веб-сервера, это **worker**. В **Settings** → **Deploy** задайте **Start Command**: `python tg_bot.py` (если Railway не подхватил `Procfile`).
5. Запустите деплой. Бот будет работать в облаке и автоматически перезапускаться при сбоях.

## Установка как аддон Home Assistant OS

Аддон не конфликтует с другими бота-аддонами (например, CarVector Bot): у каждого свой контейнер и свой `slug` (`afdparts_bot`).

1. Залейте этот проект в GitHub (например, в репозиторий с другими аддонами — тогда добавьте папку `afdparts_bot` с этими файлами: `config.yaml`, `Dockerfile`, `run.sh`, `parser.py`, `tg_bot.py`, `requirements.txt`).
2. Если репозиторий только для этого аддона: в корне репозитория нужен файл `repository.yaml`:
   ```yaml
   name: AFDparts Bot Add-on
   url: https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПОЗИТОРИЙ
   maintainer: Ваше имя
   ```
   и вся текущая папка должна быть в подпапке `afdparts_bot/` в репозитории.
3. В Home Assistant: **Настройки** → **Дополнения** → **Магазин дополнений** → ⋮ → **Репозитории** → добавьте URL вашего репозитория.
4. Обновите список, найдите **AFDparts Bot** → **Установить**.
5. Во вкладке **Конфигурация** укажите `bot_token` (от @BotFather), при необходимости `afdparts_login`, `afdparts_password`, `telegram_order_chat_id` (по умолчанию 232066339). Сохраните и запустите аддон.

## Для разработки

**Правило:** после любых доработок кода бота перезапускать его:
`pkill -f tg_bot.py; sleep 2; cd путь/к/afdparts_bot && source venv/bin/activate && python tg_bot.py`
