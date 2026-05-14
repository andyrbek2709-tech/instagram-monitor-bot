# GEMINI.md — Контекст проекта

> Читай этот файл первым. Здесь всё что нужно знать перед тем как трогать код.

---

## Что это

Telegram-бот для мониторинга Instagram-аккаунтов и анализа постов через Gemini AI + GPT-4o Vision + Whisper.
Хостинг: **Railway.app**. БД: **PostgreSQL** (Railway managed). Парсинг: **HikerAPI**.

---

## ВАЖНО: instagrapi удалён полностью

Проект изначально строился на instagrapi (прямой логин в Instagram).
**Это не работало** — Railway/AWS IP заблокированы Instagram.

**Текущее решение**: HikerAPI (`https://api.hikerapi.com/v1`) — REST API без прямого логина.
`instagrapi` удалён из `requirements.txt` и из всего кода.
`INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD` — больше не нужны.

---

## Переменные окружения (Railway → Variables)

| Переменная | Обязательна | Назначение |
|------------|-------------|-----------|
| `DATABASE_URL` | ✅ | PostgreSQL (Railway автоподставляет) |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен от @BotFather |
| `HIKER_API_KEY` | ✅ | HikerAPI — без него парсинг не работает |
| `GEMINI_API_KEY` | ✅ | API key для Gemini, Vision и Whisper |

---

## Архитектура файлов

```
main.py          запуск: validate_environment() → initialize_components() → run_polling()
bot.py           весь Telegram UI (кнопки, ConversationHandlers, callback handlers)
parser.py        HikerAPIClient + Parser.monitor_account() + Parser.get_post_by_url()
filter.py        Filter.process_posts() → Gemini классификация контента
analyzer.py      Analyzer.process_posts() → Gemini sentiment + темы + релевантность
db_init.py       DatabaseInitializer.create_tables() — создаёт таблицы при старте
validator.py     AccountValidator — только DB-операции (instagrapi удалён)
maintenance.py   DataMaintenance.run_maintenance() — очистка старых данных
```

---

## Главное меню (порядок кнопок)

1. **🔗 Разобрать пост по ссылке** — главная фича (описана ниже)
2. **🚀 Начать парсинг аккаунта** — парсит добавленные аккаунты
3. **➕ Добавить аккаунт**
4. **📋 Мои аккаунты**
5. **📊 Получить дайджест**
6. **📈 Статистика**
7. **⚙️ Настройки**
8. **❓ Справка**

---

## Флоу 1: Разобрать пост по ссылке (главная фича)

```
Кнопка "🔗 Разобрать пост по ссылке"
→ analyze_url_start() → бот просит прислать ссылку (state: WAIT_URL)
→ пользователь шлёт https://www.instagram.com/p/... или /reel/...
→ analyze_url_receive():

  1. parser.get_post_by_url(url)
       → HikerAPI GET /v1/media/by/code?code={shortcode}
       → извлекает: caption, thumbnail_url, video_url, likes, comments, account

  2. Показывает карточку поста (текст + лайки + ссылка)

  3. Если caption пустой (видео/Reel без текста):
       a. GPT-4o Vision:
            - Скачивает thumbnail с Instagram CDN (requests + User-Agent заголовок)
            - Кодирует в base64 (Instagram CDN блокирует прямые URL для OpenAI)
            - Отправляет в gpt-4o как data URI
            - Показывает "👁 GPT-4o видит: [описание кадра]"
       b. Whisper транскрипция:
            - HEAD запрос для проверки размера (лимит 24 МБ)
            - Скачивает video_url
            - openai.audio.transcriptions.create(model="whisper-1")
            - Показывает "🎤 Речь в видео: [транскрипт]"
       c. caption = "[Что видно в кадре]: {visual}\n\n[Что говорит человек]: {transcript}"

  4. Gemini анализ (gemini-1.5):
       Промпт выбирается по типу контента:
       - Видео со звуком → приоритет РЕЧИ, визуал = только контекст
         Формат: ФОРМАТ И СТИЛЬ / СУТЬ / КЛЮЧЕВЫЕ ТЕЗИСЫ / ИДЕИ ДЛЯ КОНТЕНТА
       - Видео без звука → анализ по визуалу
       - Текстовый пост → СУТЬ / КЛЮЧЕВЫЕ ИДЕИ / ИДЕИ ДЛЯ КОНТЕНТА

  5. Показывает анализ с кнопками:
       [📝 Создать промпт]   ← только по нажатию
       [🔗 Разобрать другой пост]
       [🔙 Главное меню]

  6. При нажатии "📝 Создать промпт":
       create_prompt_action():
       - Берёт last_analysis и last_raw_content из context.user_data
       - Gemini генерирует готовый промпт, готовый к вставке в чат
       - Содержит: контекст идеи + задачу + запрос стека/плана
       - Пользователь копирует и вставляет в Gemini.ai или ChatGPT
```

---

## Флоу 2: Парсинг аккаунта

```
Кнопка "🚀 Начать парсинг"
→ start_parsing():
  1. Берёт активные аккаунты из monitored_accounts WHERE is_active=1
  2. parser.monitor_account(username, num_posts):
       → HikerAPI GET /v1/user/by/username → получает pk
       → HikerAPI GET /v1/user/medias → список постов
       → Сохраняет в таблицу posts
  3. В фоне: filter.process_posts() + analyzer.process_posts() (для статистики в БД)
  4. Каждый пост — отдельное сообщение: текст подписи + ссылка
     Если текста нет → "(текст отсутствует — только медиа)"
```

---

## ConversationHandler состояния

```python
ADD_ACCOUNT = 0       # ввод username для добавления аккаунта
CONFIRM_ACCOUNT = 1
SELECT_NUM_POSTS = 2
SELECT_INTERVAL = 3
WAIT_SESSIONID = 100  # устаревшее, обработчик есть но ничего не делает
WAIT_URL = 101        # ожидание ссылки Instagram для анализа
```

---

## Технические детали

**Instagram CDN и OpenAI Vision:**
CDN-ссылки Instagram (`scontent-*.cdninstagram.com`) нельзя передать напрямую в OpenAI —
возвращает 400 "Error while downloading". Решение: скачиваем сами через `requests` с
`User-Agent: Mozilla/5.0 (iPhone...)` и `Referer: https://www.instagram.com/`, затем base64.

**HikerAPI форматы ответа:**
Может вернуть `list`, `dict.items`, `dict.response.items`. Всё обрабатывается в
`HikerAPIClient.get_user_medias()`.

**Caption может быть dict:**
`{"text": "..."}` вместо строки. Обрабатывает `Parser._parse_caption()`.

**Telegram 4096 символов:**
Длинные тексты обрезаются до 800 символов. Везде есть fallback без parse_mode='Markdown'
на случай спецсимволов в тексте.

**NULL в analyses:**
Поля `key_topics` / `recommendations` могут быть NULL если GEMINI_API_KEY не задан.
В `get_digest()` и `daily_digest_job()` везде: `json.loads(x) if x else []`.

**Railway ephemeral filesystem:**
Ничего нельзя хранить на диске между деплоями. Всё — только PostgreSQL.

---

## База данных

Все таблицы создаются автоматически при старте через `db_init.py`.

```
monitored_accounts  — аккаунты на мониторинге
posts               — спарсенные посты (caption, url, media_type, content_hash)
filter_results      — GPT-классификация (is_ad, is_greeting, is_personal)
analyses            — Gemini анализ (sentiment, key_topics JSON, relevance_score)
daily_stats         — дневная агрегированная статистика
telegram_users      — пользователи бота
instagram_sessions  — зарезервировано, не используется
parse_logs          — логи парсинга, смотреть через /debug
```

---

## Что работает сейчас

- ✅ Парсинг аккаунтов через HikerAPI, посты в виде отдельных сообщений
- ✅ Разбор поста по прямой ссылке (пост/Reel)
- ✅ GPT-4o Vision: описание кадра для видео без текста
- ✅ Whisper: транскрипция речи из видео (лимит 24 МБ)
- ✅ Gemini анализ с умным промптом (речь = суть, визуал = контекст)
- ✅ Кнопка "Создать промпт" → готовый промпт для копирования в Gemini.ai
- ✅ Ежедневный дайджест в 09:00
- ✅ /debug — логи парсинга из parse_logs

## Что можно улучшить

- Видео > 24 МБ — Whisper пропускается, только Vision
- Аккаунты без изоляции по user_id (все видят всех)
- `WAIT_SESSIONID` ConversationHandler можно удалить (мёртвый код)
- Нет push-уведомлений о новых постах (только ручной запуск и дайджест 09:00)

---

## Деплой

Хостинг: **Railway.app**
Запуск: `Procfile` → `web: python main.py`
Каждый push в GitHub → Railway автоматически деплоит (~1-2 мин).
GitHub: `andyrbek2709-tech/instagram-monitor-bot`
