# CLAUDE.md — Контекст проекта

> Читай этот файл **первым**. Здесь всё что нужно знать перед тем как трогать код.

---

## Что это

Telegram-бот для мониторинга Instagram-аккаунтов и анализа постов через **Gemini AI**.
Хостинг: **Railway.app**. БД: **PostgreSQL** (Railway managed). Парсинг: **HikerAPI**.

---

## Текущий стек AI (актуально на май 2026)

| Задача | Что используется | Пакет |
|--------|-----------------|-------|
| Анализ текста постов | Gemini 2.0 Flash | google-genai |
| Vision (описание кадра видео) | Gemini 2.0 Flash + PIL.Image | google-genai + pillow |
| Транскрипция речи из видео | Gemini File API (video/mp4) | google-genai |
| Классификация (реклама/приветствие) | Gemini 2.0 Flash | google-genai |
| Sentiment анализ | Gemini 2.0 Flash | google-genai |

**Важно:** пакет называется google-genai, НЕ google-generativeai.
Импорт: import google.genai as genai
API: genai.Client(api_key=...).models.generate_content(model='gemini-2.0-flash', contents=..., config={...})

**Старый способ (УДАЛЁН, не использовать):**

  # НЕПРАВИЛЬНО — старый deprecated SDK:
  import google.generativeai as genai
  genai.configure(api_key=...)
  model = genai.GenerativeModel('gemini-2.0-flash')

  # НЕПРАВИЛЬНО — openai SDK:
  import openai
  openai.OpenAI(api_key=...).chat.completions.create(model="gemini-1.5", ...)

---

## ВАЖНО: instagrapi удалён полностью

Проект изначально строился на instagrapi (прямой логин в Instagram).
**Это не работало** — Railway/AWS IP заблокированы Instagram.

**Текущее решение**: HikerAPI (https://api.hikerapi.com/v1) — REST API без прямого логина.
instagrapi удалён из requirements.txt и из всего кода.
INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD — больше не нужны.

---

## Переменные окружения (Railway → Variables)

DATABASE_URL      — PostgreSQL (Railway автоподставляет)
TELEGRAM_BOT_TOKEN — Токен от @BotFather
HIKER_API_KEY     — HikerAPI — без него парсинг не работает
GEMINI_API_KEY    — Один ключ для всего: текст, Vision, File API

---

## Архитектура файлов

main.py       — запуск: validate_environment() → initialize_components() → run_polling()
bot.py        — весь Telegram UI (кнопки, ConversationHandlers, callback handlers)
parser.py     — HikerAPIClient + Parser.monitor_account() + Parser.get_post_by_url()
filter.py     — Filter.process_posts() → Gemini классификация контента
analyzer.py   — Analyzer.process_posts() → Gemini sentiment + темы + релевантность
db_init.py    — DatabaseInitializer.create_tables() — создаёт таблицы при старте
validator.py  — AccountValidator — только DB-операции (instagrapi удалён)
maintenance.py — DataMaintenance.run_maintenance() — очистка старых данных

---

## Флоу: Разобрать пост по ссылке (главная фича)

1. parser.get_post_by_url(url)
     → HikerAPI GET /v1/media/by/code?code={shortcode}
     → извлекает: caption, thumbnail_url, video_url, likes, comments, account

2. Если caption пустой (видео без текста):

   a. Gemini Vision:
        img_resp = requests.get(thumbnail_url, headers={'User-Agent': 'iPhone...', 'Referer': 'instagram.com'})
        img = PIL.Image.open(io.BytesIO(img_resp.content))
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model='gemini-2.0-flash', contents=[img, "опиши..."])
        visual_desc = resp.text.strip()

   b. Gemini File API транскрипция:
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            f.write(video_bytes); tmp_path = f.name
        uploaded = client.files.upload(file=tmp_path, config={'mime_type': 'video/mp4'})
        resp = client.models.generate_content(model='gemini-2.0-flash', contents=[uploaded, "Транскрибируй..."])
        client.files.delete(name=uploaded.name)
        os.unlink(tmp_path)

   c. caption = "[Что видно в кадре]: {visual}\n\n[Что говорит человек]: {transcript}"

3. Gemini анализ (промпт по типу контента):
   - Видео со звуком: приоритет РЕЧИ, визуал = контекст
   - Видео без звука: анализ по визуалу
   - Текст: СУТЬ / КЛЮЧЕВЫЕ ИДЕИ / ИДЕИ ДЛЯ КОНТЕНТА

4. Кнопка "Создать промпт" → Gemini генерирует готовый промпт начиная с
   "Я хочу реализовать следующую идею..."

---

## ConversationHandler состояния

ADD_ACCOUNT = 0
CONFIRM_ACCOUNT = 1
SELECT_NUM_POSTS = 2
SELECT_INTERVAL = 3
WAIT_SESSIONID = 100  # устаревшее, можно удалить
WAIT_URL = 101        # ожидание ссылки Instagram для анализа

---

## История проблем и решений

ПРОБЛЕМА                                  | РЕШЕНИЕ
import openai → Railway crash             | Заменено на import google.genai as genai
google.generativeai deprecated            | Заменено на google-genai
filter.py — 42 null байта                | Перестроен из git-оригинала
requirements.txt усечён до psycopg2-bi   | Перезаписан через shell
bot.py усечён (1550 строк вместо 1566)   | Перестроен из git-оригинала
IndentationError в analyze_url_receive   | Правильный отступ в except-теле
Внешний try без except                   | Добавлен except на уровне 8 пробелов
git index.lock на Windows ФС             | Клонировать в /tmp/bot_fresh, пушить оттуда
Edit tool на Windows CRLF файлах         | Использовать Python-скрипты для изменений

---

## Что работает сейчас

- Парсинг аккаунтов через HikerAPI
- Разбор поста по прямой ссылке (пост/Reel)
- Gemini Vision: описание кадра для видео без текста
- Gemini File API: транскрипция речи из видео (лимит 100 МБ)
- Gemini анализ с умным промптом
- Кнопка "Создать промпт" → готовый промпт для Gemini.ai
- Ежедневный дайджест в 09:00
- /debug — логи парсинга из parse_logs

---

## Деплой

Хостинг: Railway.app
Запуск: Procfile → web: python main.py
Каждый push в GitHub → Railway автоматически деплоит (~1-2 мин)
GitHub: andyrbek2709-tech/instagram-monitor-bot

Как пушить (если git lock на основной ФС):
  cd /tmp/bot_fresh
  git add -A
  git commit -m "..."
  git push https://TOKEN@github.com/andyrbek2709-tech/instagram-monitor-bot.git master
