"""Настройки проекта и фиксированные схемы таблиц.

Секреты здесь не храним.
Импорт этого файла не создаёт папки и не изменяет данные.
"""

from pathlib import Path


# Пути считаются от папки проекта, независимо от места запуска.
PROJECT_DIR = Path(__file__).resolve().parent

DATA_DIR = PROJECT_DIR / "data"
BACKUPS_DIR = PROJECT_DIR / "backups"
LOGS_DIR = PROJECT_DIR / "logs"
CACHE_DIR = PROJECT_DIR / "cache"

ADVERTISERS_FILE = DATA_DIR / "advertisers.csv"
TELEGRAM_FILE = DATA_DIR / "telegram.csv"
STREAMERS_FILE = DATA_DIR / "streamers.csv"

OBSERVATIONS_FILE = DATA_DIR / "observations.csv"
FEEDBACK_FILE = DATA_DIR / "feedback.csv"
METRICS_FILE = DATA_DIR / "metrics.csv"

CHANGES_LOG_FILE = LOGS_DIR / "changes_log.csv"
AGENT_LOG_FILE = LOGS_DIR / "agent.log"

CSV_ENCODING = "utf-8-sig"
CSV_DELIMITER = ","


# Безопасные настройки по умолчанию.
# Их соблюдение реализуем и проверим в safe_writer.py.
DEFAULT_DRY_RUN = True
BACKUP_BEFORE_WRITE = True
FILL_ONLY_EMPTY_AUTO_FIELDS = True
ALLOW_ROW_DELETE = False
ALLOW_CELL_CLEAR = False


# Рекламодатели: строго 22 колонки.
ADVERTISER_COLUMNS = (
    "Номер",
    "ID клиента",
    "Название компании",
    "Ниша",
    "Контактное лицо",
    "Telegram/почта",
    "Сайт",
    "Соцсети",
    "Страна",
    "Бюджет на рекламу",
    "Формат (TG/Twitch/Kick)",
    "Целевая аудитория",
    "Рекламируемый продукт",
    "Предыдущий опыт",
    "Готовность к тесту",
    "Дата первого контакта",
    "Статус клиента",
    "Общая сумма оплаты",
    "Сумма на закуп",
    "Мой процент",
    "Комментарий",
    "Рейтинг клиента (A/B/C)",
)

ADVERTISER_AUTO_FIELDS = frozenset({
    "Номер",
    "ID клиента",
    "Название компании",
    "Ниша",
    "Сайт",
    "Соцсети",
    "Страна",
    "Формат (TG/Twitch/Kick)",
    "Целевая аудитория",
    "Рекламируемый продукт",
})

ADVERTISER_MANUAL_FIELDS = frozenset({
    "Telegram/почта",
    "Бюджет на рекламу",
    "Предыдущий опыт",
    "Готовность к тесту",
    "Дата первого контакта",
    "Статус клиента",
    "Общая сумма оплаты",
    "Сумма на закуп",
    "Мой процент",
    "Комментарий",
    "Рейтинг клиента (A/B/C)",
})

# LLM может предложить значение, но запись требует подтверждения.
ADVERTISER_CONFIRMATION_FIELDS = frozenset({
    "Контактное лицо",
})


# Telegram: строго 14 колонок.
TELEGRAM_COLUMNS = (
    "Номер",
    "Название",
    "Ссылка",
    "Тематика",
    "Подписчики",
    "Средний охват",
    "ERR (%)",
    "Стоимость 1/24",
    "Стоимость нативной",
    "Контакт",
    "Статус",
    "Последнее обновление",
    "Комментарий",
    "Рекламодатель",
)

TELEGRAM_AUTO_FIELDS = frozenset({
    "Номер",
    "Название",
    "Ссылка",
    "Тематика",
    "Подписчики",
    "Средний охват",
    "ERR (%)",
    "Стоимость 1/24",
    "Стоимость нативной",
    "Контакт",
    "Последнее обновление",
})

TELEGRAM_MANUAL_FIELDS = frozenset({
    "Статус",
    "Комментарий",
    "Рекламодатель",
})

# Контакт автоматический по схеме, но требует подтверждения человека.
TELEGRAM_CONFIRMATION_FIELDS = frozenset({
    "Контакт",
})


# Twitch/Kick: строго 13 колонок.
STREAMER_COLUMNS = (
    "Номер",
    "Стример",
    "Платформа (Twitch/Kick)",
    "Категория",
    "Подписчики",
    "Средний онлайн",
    "Пиковый онлайн",
    "Контакт",
    "Вид рекламы",
    "Стоимость рекламы",
    "Соц сети",
    "Последнее обновление",
    "Комментарий",
)

STREAMER_AUTO_FIELDS = frozenset({
    "Номер",
    "Стример",
    "Платформа (Twitch/Kick)",
    "Категория",
    "Подписчики",
    "Средний онлайн",
    "Пиковый онлайн",
    "Контакт",
    "Соц сети",
    "Последнее обновление",
})

STREAMER_MANUAL_FIELDS = frozenset({
    "Вид рекламы",
    "Стоимость рекламы",
    "Комментарий",
})

STREAMER_CONFIRMATION_FIELDS = frozenset({
    "Контакт",
})


ALLOWED_STREAMING_PLATFORMS = frozenset({
    "Twitch",
    "Kick",
})

CLIENT_STATUSES = (
    "new",
    "contacted",
    "negotiating",
    "testing",
    "active",
    "paused",
    "rejected",
)

# Пока автоматическое изменение ручного статуса запрещено.
# Возможность new -> contacted согласуем отдельно.
ALLOW_AUTOMATIC_STATUS_CHANGE = False

# MVP: только РФ и Беларусь. Это страна компании/канала, не зона доставки.
MVP_COUNTRIES = frozenset({"ru", "by"})
MVP_LIMIT = 50
HTTP_TIMEOUT_SECONDS = 20.0
HTTP_MAX_RESPONSE_BYTES = 2_000_000
SOURCE_DELAY_SECONDS = (2.0, 5.0)
SOURCE_MAX_REQUESTS = 120
WB_MIN_RATING = 4.5
WB_MIN_REVIEWS = 100
ADVERTISER_MIN_SCORE = 65
MIN_SOCIAL_FOLLOWERS = 1000
MIN_ACCOUNT_AGE_DAYS = 90
ACTIVE_WITHIN_DAYS = 30
TG_MIN_SUBSCRIBERS = 1000
TG_MIN_REACH = 100
TG_MIN_ERR = 1.0
TG_MAX_ERR = 100.0
TGSTAT_BASE_URL = "https://api.tgstat.ru"
# Неофициальный endpoint: доступность не гарантирована; 403 не обходим.
WB_SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v18/search"
