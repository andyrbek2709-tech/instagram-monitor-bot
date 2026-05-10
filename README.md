# 📱 Instagram Monitor Bot

Полностью реализованный Telegram-бот для мониторинга, анализа и управления Instagram-аккаунтами с использованием Claude AI и OpenAI.

## ✨ Возможности

✅ **Автоматический мониторинг** постов Instagram  
✅ **AI-анализ контента** (Claude + OpenAI)  
✅ **Определение вирусного потенциала** и релевантности  
✅ **Ежедневные дайджесты** с аналитикой  
✅ **Статистика и отчёты** по постам  
✅ **Пауза/возобновление** мониторинга  
✅ **Retry-механизм** для всех API вызовов  
✅ **Валидация аккаунтов** и проверка статуса  
✅ **Очистка данных** и архивирование  

## 🏗️ Архитектура

```
main.py (Orchestrator)
├── parser.py (Instagram парсинг)
├── filter.py (Классификация контента)
├── analyzer.py (AI анализ через Claude)
├── bot.py (Telegram интерфейс)
├── db_init.py (Инициализация БД)
├── validator.py (Проверка аккаунтов)
└── maintenance.py (Обслуживание данных)
```

## 📋 Требования

- Python 3.11+
- Docker & Docker Compose (опционально)
- Telegram Bot API Token
- OpenAI API Key
- Anthropic Claude API Key
- Instagram аккаунт

## 🚀 Быстрый старт (Railway)

### 1. Подключите GitHub репозиторий

1. Перейдите на [Railway.app](https://railway.app)
2. Нажмите "New Project" → "Deploy from GitHub repo"
3. Выберите `andyrbek2709-tech/instagram-monitor-bot`

### 2. Настройте переменные окружения

Добавьте все переменные в Railway (Environment):

```env
# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# APIs
OPENAI_API_KEY=sk-your-openai-key
CLAUDE_API_KEY=sk-ant-your-claude-key

# Instagram
INSTAGRAM_USERNAME=your_instagram_username
INSTAGRAM_PASSWORD=your_instagram_password

# Конфигурация
DATABASE_PATH=/data/instagram_monitor.db
LOG_LEVEL=INFO
TIMEZONE=Europe/Moscow

# Anti-detection
MIN_REQUEST_DELAY=8
MAX_REQUEST_DELAY=20
MIN_ACCOUNT_DELAY=30
MAX_ACCOUNT_DELAY=90
```

### 3. Настройте Volume для персистентности

В Railway добавьте Volume:
- Mount path: `/app/data`
- Это сохранит базу данных и логи между перезагрузками

### 4. Запустите бота

Railway автоматически запустит проект с помощью:
```bash
python main.py
```

## 📱 Команды Telegram

- `/start` — главное меню
- `/help` — справка
- **➕ Добавить аккаунт** — добавить Instagram аккаунт в мониторинг
- **📋 Мои аккаунты** — список отслеживаемых аккаунтов
- **📊 Получить дайджест** — ежедневный дайджест за 24 часа
- **📈 Статистика** — общая статистика по постам
- **⏸️ Пауза / ▶️ Возобновить** — управление мониторингом
- **⚙️ Настройки** — конфигурация бота

## 🔧 Компоненты

### Parser (`parser.py`)
- Аутентификация в Instagram с сохранением сессий
- Антидетекция (User-Agent ротация, случайные задержки)
- Retry-механизм для надёжности
- Загрузка медиа с дедубликацией

### Filter (`filter.py`)
- Классификация контента (реклама, приватные посты и т.д.)
- Анализ engagement-rate
- Детекция паттернов контента
- Двухуровневая классификация (ключевые слова + GPT-4o-mini)

### Analyzer (`analyzer.py`)
- Sentiment-анализ через Claude Haiku
- Извлечение ключевых тем и упоминаний брендов
- Расчёт релевантности и вирусного потенциала
- Генерация рекомендаций

### Bot (`bot.py`)
- Асинхронный Telegram интерфейс
- AsyncIOScheduler для ежедневных дайджестов
- Форматирование аналитических дайджестов
- Управление настройками пользователя

### Validator (`validator.py`)
- Проверка существования аккаунтов
- Валидация активности аккаунтов
- Обнаружение "залежавшихся" аккаунтов
- Массовая верификация аккаунтов

### Maintenance (`maintenance.py`)
- Очистка постов старше 90 дней
- Архивирование старых статистик
- Генерация ежедневных отчётов
- Управление логами

## 📊 Структура БД

```
monitored_accounts
├── id
├── username
├── session_key
├── last_fetch
├── is_active
└── created_at

posts
├── id
├── account_id (FK)
├── post_id
├── url
├── caption
├── media_type
├── content_hash (для дедубликации)
└── fetched_at

filter_results
├── id
├── post_id (FK)
├── is_ad
├── is_greeting
├── is_personal
├── engagement_rate
├── text_length
└── analyzed_at

analyses
├── id
├── post_id (FK)
├── sentiment
├── key_topics (JSON)
├── brand_mentions (JSON)
├── audience_segment
├── content_quality
├── relevance_score
├── viral_potential
├── recommendations (JSON)
└── analyzed_at

telegram_users
├── id
├── user_id (UNIQUE)
├── username
├── monitored_accounts
├── settings (JSON для pause/resume)
└── created_at

daily_stats
├── id
├── date (UNIQUE)
├── total_posts
├── avg_relevance
├── high_relevance_count
├── viral_count
├── sentiment_positive/negative/neutral
└── created_at
```

## 🔄 Pipeline цикла

```
1. Валидация окружения
2. Инициализация компонентов
3. Запуск бота и планировщика

Каждый час:
├── 0. Обслуживание данных
│   ├── Очистка старых постов (>90 дней)
│   ├── Архивирование старых статистик
│   └── Генерация ежедневных отчётов
├── 1. Парсинг (Instagram)
├── 2. Фильтрация (OpenAI)
├── 3. Анализ (Claude AI)
└── 4. Сохранение в БД

Каждый день в 09:00 (±15 минут):
└── Отправка ежедневных дайджестов всем пользователям
```

## 🛡️ Надёжность

### Retry-механизм
- **Parser**: 3 попытки (задержка 2-3 сек)
- **Filter**: 3 попытки (задержка 2 сек)
- **Analyzer**: 3 попытки (задержка 2 сек)
- **Login**: 3 попытки (задержка 5 сек)

### Anti-detection
- RotatedUser-Agent для каждого запроса
- Случайные задержки между постами (8-20 сек)
- Случайные задержки между аккаунтами (30-90 сек)
- Сохранение сессий для переиспользования

### Обработка ошибок
- Graceful fallbacks для всех API
- Логирование всех ошибок и попыток
- Автоматическая деактивация неработающих аккаунтов
- Валидация данных перед сохранением

## 📈 Мониторинг

Логи доступны в:
- Railway Dashboard → Deployments → Logs
- Локально: `logs/bot.log`

Ключевые метрики отслеживаются в:
- `daily_stats` таблица (ежедневно)
- Telegram статистика команда

## 🚨 Важно для Railway

### Порты
- Бот использует только Telegram API (outbound)
- Портов не требуется

### Ресурсы
- RAM: минимум 256 MB
- CPU: 0.5 vCPU достаточно
- Хранилище: 500 MB для БД и логов

### Периодические задачи
- Обслуживание БД: каждый час
- Отправка дайджестов: каждый день в 09:00
- Проверка аккаунтов: раз в неделю

## 🔐 Безопасность

- Все ключи хранятся в переменных окружения
- `.env` файлы в `.gitignore`
- Пароли Instagram не сохраняются на диск
- Все API ключи логируются только при ошибках

## 📝 Развертывание локально

```bash
# Установка зависимостей
pip install -r requirements.txt

# Инициализация БД
python db_init.py

# Запуск бота
python main.py
```

## 🐳 Docker

```bash
# Сборка
docker-compose build

# Запуск
docker-compose up -d

# Логи
docker-compose logs -f
```

## 📞 Поддержка

Если возникают проблемы:

1. Проверьте логи: `docker-compose logs`
2. Убедитесь, что все переменные окружения установлены
3. Проверьте подключение к Instagram
4. Проверьте API ключи OpenAI и Claude

## 📄 Лицензия

MIT License - видите `LICENSE` файл для деталей

---

**Создано с помощью Claude AI**  
Последнее обновление: май 2026
