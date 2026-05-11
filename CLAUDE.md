# CLAUDE.md — Контекст проекта для агентов

> Читай этот файл первым. Здесь всё что нужно знать перед тем как трогать код.

## Что это

Telegram-бот для мониторинга Instagram-аккаунтов и анализа постов через Claude AI.
Хостинг: **Railway.app**. БД: **PostgreSQL** (Railway managed). Парсинг: **HikerAPI**.

## Критически важно: миграция instagrapi → HikerAPI

**Проект изначально строился на instagrapi (прямой логин в Instagram).**
**Это не работает** — Railway/AWS/GCP IP заблокированы Instagram.

**Текущее решение**: HikerAPI (`https://api.hikerapi.com/v1`) — сторонний сервис,
который сам логинится в Instagram и отдаёт данные через REST API.
Никакого прямого логина, никаких сессий, никакого instagrapi.

**instagrapi удалён из requirements.txt и из кода полностью.**

## Переменные окружения (Railway Variables)

| Переменная | Обязательна | Назначение |
|------------|-------------|-----------|
| `DATABASE_URL` | ✅ | PostgreSQL URL (Railway автоподставляет) |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен бота от @BotFather |
| `HIKER_API_KEY` | ✅ | HikerAPI ключ — без него парсинг не работает |
| `CLAUDE_API_KEY` | ⚠️ опционально | Claude AI для анализа постов |
| `OPENAI_API_KEY` | ⚠️ опционально | GPT-4o-mini для фильтрации контента |

`INSTAGRAM_USERNAME` и `INSTAGRAM_PASSWORD` — **устаревшие, не нужны**.

## Архитектура файлов

```
main.py          запуск: validate_environment() → initialize_components() → run_polling()
bot.py           весь Telegram UI: кнопки, ConversationHandlers, callback handlers
parser.py        HikerAPIClient + Parser.monitor_account() + Parser.get_post_by_url()
filter.py        Filter.process_posts() → GPT-4o-mini классификация (реклама/личное)
analyzer.py      Analyzer.process_posts() → Claude sentiment + темы + релевантность
db_init.py       DatabaseInitializer.create_tables() — создаёт все таблицы при старте
validator.py     AccountValidator — только DB-операции, instagrapi удалён
maintenance.py   DataMaintenance.run_maintenance() — очистка старых постов/логов
```

## Telegram UI — состояния ConversationHandler

```python
ADD_ACCOUNT = 0      # ввод username аккаунта для добавления
CONFIRM_ACCOUNT = 1  # подтверждение
SELECT_NUM_POSTS = 2 # выбор количества постов
SELECT_INTERVAL = 3  # выбор интервала проверки
WAIT_SESSIONID = 100 # устаревшее, ConversationHandler остался но ничего не делает
WAIT_URL = 101       # ожидание ссылки Instagram для анализа
```

## Главное меню (кнопки)

1. **🔗 Разобрать пост по ссылке** — главная фича: кидаешь URL → Claude анализирует
2. **🚀 Начать парсинг аккаунта** — парсит все добавленные аккаунты, шлёт посты списком
3. **➕ Добавить аккаунт** — добавить аккаунт на регулярный мониторинг
4. **📋 Мои аккаунты** — список
5. **📊 Получить дайджест** — статистика за 24 часа
6. **📈 Статистика**
7. **⚙️ Настройки**
8. **❓ Справка**

## Ключевые флоу

### Флоу "Разобрать пост по ссылке" (WAIT_URL)
```
Кнопка "🔗 Разобрать пост"
→ analyze_url_start() → показывает инструкцию, ожидает WAIT_URL
→ пользователь шлёт URL instagram.com/p/... или /reel/...
→ analyze_url_receive():
    1. parser.get_post_by_url(url) → HikerAPI /media/by/code
    2. Показывает caption + лайки/комменты
    3. Claude prompt → "О чём пост / Ключевые идеи / Как использовать"
    4. Отвечает на русском независимо от языка поста
```

### Флоу "Начать парсинг аккаунта"
```
Кнопка "🚀 Начать парсинг"
→ start_parsing():
    1. Берёт все активные аккаунты из monitored_accounts WHERE is_active=1
    2. Для каждого: parser.monitor_account(username, num_posts)
       → HikerAPI /user/by/username → get pk
       → HikerAPI /user/medias → список постов
       → Сохраняет в posts таблицу
    3. В фоне: filter.process_posts() + analyzer.process_posts() (для статистики)
    4. Шлёт каждый пост отдельным сообщением: текст + ссылка
```

## База данных

Все таблицы создаются автоматически при старте через `DatabaseInitializer.create_tables()`.

```sql
monitored_accounts  id, user_id, username, num_posts, check_interval_hours,
                    last_fetch, next_check, is_active, created_at

posts               id, account_id(FK), post_id, url, caption, media_type,
                    content_hash, fetched_at

filter_results      id, post_id(FK), is_ad, is_greeting, is_personal,
                    engagement_rate, text_length, has_media, analyzed_at

analyses            id, post_id(FK), sentiment, key_topics(JSON), brand_mentions(JSON),
                    audience_segment, content_quality, relevance_score,
                    viral_potential, recommendations(JSON), analyzed_at

daily_stats         date, total_posts, avg_relevance, high_relevance_count,
                    viral_count, sentiment_positive/negative/neutral

telegram_users      user_id, username, monitored_accounts, settings(JSON)

parse_logs          target_username, stage, level, message, created_at
                    — смотреть через /debug в Telegram
```

## Известные особенности / подводные камни

1. **HikerAPI response format** непредсказуем: может вернуть `list`, `dict` с `items`,
   или `dict` с `response.items`. Логика в `HikerAPIClient.get_user_medias()` обрабатывает все варианты.

2. **caption** может прийти как строка или как `{"text": "..."}` — обрабатывает `_parse_caption()`.

3. **Telegram message limit** 4096 символов. Длинные посты обрезаются до 800 символов в UI.
   Markdown может сломаться на спецсимволах — везде есть fallback `except Exception`.

4. **analyses и filter_results** могут содержать NULL поля если AI-ключи не заданы.
   Везде в `get_digest()` и `daily_digest_job()` используется `json.loads(x) if x else []`.

5. **Railway ephemeral filesystem** — ничего нельзя хранить на диске между деплоями.
   Сессии, кэш, логи — только в PostgreSQL или в памяти.

6. **OPENAI_API_KEY опционален** — если не задан, `filter.py` gracefully деградирует
   (возвращает `{"is_ad": False, ...}` после 3 retry).

## Что сейчас работает

- ✅ Парсинг аккаунтов через HikerAPI
- ✅ Разбор поста по прямой ссылке + Claude анализ
- ✅ Сохранение постов в PostgreSQL
- ✅ Ежедневный дайджест (09:00 автоматически)
- ✅ /debug — логи парсинга из parse_logs
- ✅ Добавление аккаунтов, управление через Telegram

## Что можно улучшить (не сделано)

- Видео-посты не имеют текста — показывается "нет текста". Можно добавить
  транскрипцию аудио через Whisper или анализ описания видео.
- Аккаунты добавляются для всех пользователей (нет изоляции по user_id).
- Нет уведомлений о новых постах — только по ручному запуску или в 09:00 дайджест.
- `WAIT_SESSIONID` ConversationHandler устарел — можно удалить.

## Деплой

Хостинг: **Railway.app**
Запуск определяется `Procfile`: `web: python main.py`
При каждом push в GitHub Railway автоматически деплоит.

GitHub репозиторий: `andyrbek2709-tech/instagram-monitor-bot`
