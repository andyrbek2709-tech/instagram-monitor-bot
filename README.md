# Instagram Monitor Bot

Telegram-бот для мониторинга и анализа постов Instagram через HikerAPI + Gemini AI.

## Что умеет

- **Разбор поста по ссылке** — кидаешь ссылку на любой пост/Reel, бот показывает текст и делает анализ через Gemini на русском языке
- **Парсинг аккаунта** — добавляешь аккаунт, бот присылает каждый пост отдельным сообщением (текст + ссылка)
- **Ежедневный дайджест** — автоматически в 09:00 статистика по всем постам за 24 часа
- **Мониторинг по расписанию** — проверка новых постов с выбранным интервалом

## Архитектура

```
main.py          — запуск и оркестрация
bot.py           — Telegram интерфейс (все команды и кнопки)
parser.py        — HikerAPIClient + Parser (получение постов)
filter.py        — классификация контента через Gemini (реклама/личное)
analyzer.py      — анализ через Gemini (sentiment, темы, релевантность)
db_init.py       — создание таблиц PostgreSQL
validator.py     — проверка стейла аккаунтов через БД
maintenance.py   — очистка старых данных
```

## Стек

| Компонент | Решение |
|-----------|---------|
| Instagram парсинг | HikerAPI (hikerapi.com) — без прямого логина |
| AI анализ | Google Gemini (gemini-2.0-flash) |
| Контент-фильтр | Google Gemini (опционально) |
| БД | PostgreSQL (Railway Postgres) |
| Хостинг | Railway.app |
| Telegram | python-telegram-bot 20.x |

## Переменные окружения (Railway)

**Обязательные:**
- `DATABASE_URL` — Railway Postgres выдаёт автоматически
- `TELEGRAM_BOT_TOKEN` — от @BotFather
- `HIKER_API_KEY` — от hikerapi.com (есть бесплатный триал $2)

**Опциональные:**
- `GEMINI_API_KEY` — для AI-анализа постов, Vision и транскрипции видео

## Деплой на Railway

1. Fork этого репозитория
2. Railway → New Project → Deploy from GitHub
3. Добавить PostgreSQL: + New → Database → Add PostgreSQL
4. Variables → добавить `TELEGRAM_BOT_TOKEN`, `HIKER_API_KEY`
5. Deploy — Railway автоматически подхватит `Procfile`

## База данных (таблицы)

| Таблица | Назначение |
|---------|-----------|
| `monitored_accounts` | аккаунты на мониторинге |
| `posts` | спарсенные посты |
| `filter_results` | результаты Gemini-классификации |
| `analyses` | AI-анализ (sentiment, темы, релевантность) |
| `daily_stats` | дневная статистика |
| `telegram_users` | пользователи бота |
| `instagram_sessions` | зарезервировано (не используется) |
| `parse_logs` | логи парсинга (смотреть через /debug) |

## Как пользоваться

### Разбор одного поста
1. `/start` → **🔗 Разобрать пост по ссылке**
2. Отправить ссылку `https://www.instagram.com/p/...` или `/reel/...`
3. Бот покажет текст и автоматически сделает разбор через Gemini

### Регулярный мониторинг
1. `/start` → **➕ Добавить аккаунт** → ввести username
2. Выбрать количество постов и интервал
3. `/start` → **🚀 Начать парсинг** — получить посты сейчас

### Диагностика
- `/debug` или кнопка "Подробные логи" — последние 25 записей из `parse_logs`
