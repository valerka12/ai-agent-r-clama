"""Проверка схем таблиц без изменения файлов и данных."""

from collections import Counter
from collections.abc import Sequence

import config


class ValidationError(ValueError):
    """Структура таблицы или настройки полей некорректны."""


# Используем существующие схемы, не дублируя названия колонок.
_TABLE_SCHEMAS = {
    "advertisers": (
        config.ADVERTISER_COLUMNS,
        config.ADVERTISER_AUTO_FIELDS,
        config.ADVERTISER_MANUAL_FIELDS,
        config.ADVERTISER_CONFIRMATION_FIELDS,
        22,
    ),
    "telegram": (
        config.TELEGRAM_COLUMNS,
        config.TELEGRAM_AUTO_FIELDS,
        config.TELEGRAM_MANUAL_FIELDS,
        config.TELEGRAM_CONFIRMATION_FIELDS,
        14,
    ),
    "streamers": (
        config.STREAMER_COLUMNS,
        config.STREAMER_AUTO_FIELDS,
        config.STREAMER_MANUAL_FIELDS,
        config.STREAMER_CONFIRMATION_FIELDS,
        13,
    ),
}


def _get_schema(table_name: str) -> tuple:
    """Возвращает настройки известной таблицы."""
    if not isinstance(table_name, str) or table_name not in _TABLE_SCHEMAS:
        raise ValidationError(
            f"Неизвестная таблица: {table_name!r}. "
            "Допустимы: advertisers, telegram, streamers."
        )
    return _TABLE_SCHEMAS[table_name]


def _check_columns(columns: Sequence[str], label: str) -> tuple[str, ...]:
    """Проверяет тип, названия и отсутствие повторов."""
    if isinstance(columns, (str, bytes)) or not isinstance(columns, Sequence):
        raise ValidationError(f"{label}: ожидается список или кортеж колонок.")

    result = tuple(columns)

    if any(not isinstance(name, str) or not name.strip() for name in result):
        raise ValidationError(
            f"{label}: названия колонок должны быть непустыми строками."
        )

    duplicates = [
        name for name, count in Counter(result).items() if count > 1
    ]
    if duplicates:
        raise ValidationError(
            f"{label}: повторяющиеся колонки: {duplicates!r}."
        )

    return result


def validate_schema(table_name: str) -> None:
    """Проверяет согласованность настроек одной таблицы."""
    columns, auto, manual, confirmation, expected_count = _get_schema(table_name)
    columns = _check_columns(columns, table_name)

    if len(columns) != expected_count:
        raise ValidationError(
            f"{table_name}: ожидается {expected_count} колонок, "
            f"получено {len(columns)}."
        )

    field_groups = {
        "автоматические": auto,
        "ручные": manual,
        "с подтверждением": confirmation,
    }

    for label, fields in field_groups.items():
        if not isinstance(fields, (set, frozenset)):
            raise ValidationError(
                f"{table_name}: группа «{label}» должна быть set или frozenset."
            )
        if any(not isinstance(field, str) for field in fields):
            raise ValidationError(
                f"{table_name}: группа «{label}» содержит нестроковые поля."
            )

    known_fields = set(columns)
    assigned_fields = auto | manual | confirmation

    unknown = assigned_fields - known_fields
    if unknown:
        raise ValidationError(
            f"{table_name}: правила заданы для неизвестных колонок: "
            f"{sorted(unknown)!r}."
        )

    missing = known_fields - assigned_fields
    if missing:
        raise ValidationError(
            f"{table_name}: отсутствуют правила для колонок: "
            f"{sorted(missing)!r}."
        )

    overlap = manual & (auto | confirmation)
    if overlap:
        raise ValidationError(
            f"{table_name}: ручные поля пересекаются с автоматическими "
            f"или подтверждаемыми: {sorted(overlap)!r}."
        )

    # Пересечение auto и confirmation допустимо:
    # например, контакт собирается автоматически,
    # но записывается только после подтверждения человека.


def validate_all_schemas() -> None:
    """Проверяет настройки всех трёх таблиц."""
    for table_name in _TABLE_SCHEMAS:
        validate_schema(table_name)


def validate_headers(table_name: str, headers: Sequence[str]) -> None:
    """Сравнивает реальные заголовки с config.py без исправлений."""
    validate_schema(table_name)

    expected = _get_schema(table_name)[0]
    actual = _check_columns(headers, f"{table_name}: заголовки")

    if len(actual) != len(expected):
        raise ValidationError(
            f"{table_name}: ожидается {len(expected)} колонок, "
            f"получено {len(actual)}."
        )

    for position, (expected_name, actual_name) in enumerate(
        zip(expected, actual), start=1
    ):
        if actual_name != expected_name:
            raise ValidationError(
                f"{table_name}: колонка {position}: "
                f"ожидается {expected_name!r}, получено {actual_name!r}. "
                "Запись запрещена до проверки структуры оператором."
            )

def is_empty(value: object) -> bool:
    """Пустым считается только None или строка нулевой длины.

    Ноль, False и строки из пробелов не считаются пустыми.
    """
    return value is None or (isinstance(value, str) and value == "")


def validate_url(value: object, *, required: bool = True) -> None:
    """Проверяет формат HTTP/HTTPS-ссылки без сетевого запроса."""
    from urllib.parse import urlsplit

    if is_empty(value):
        if required:
            raise ValidationError("Ссылка обязательна.")
        return

    if not isinstance(value, str):
        raise ValidationError("Ссылка должна быть строкой.")

    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(
            "Ссылка не должна содержать пробелы или управляющие символы."
        )

    if "\\" in value:
        raise ValidationError("Ссылка не должна содержать обратный слеш.")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Обращение к port также проверяет корректность номера порта.
        port = parsed.port
    except ValueError as error:
        raise ValidationError("Некорректный формат ссылки.") from error

    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValidationError(
            "Ссылка должна начинаться с http:// или https:// "
            "и содержать имя хоста."
        )

    if parsed.username is not None or parsed.password is not None:
        raise ValidationError(
            "Ссылки с логином или паролем не принимаются."
        )

    if parsed.netloc.endswith(":") or port == 0:
        raise ValidationError("Некорректный порт в ссылке.")


def validate_counter(
    value: object,
    *,
    required: bool = True,
    positive: bool = False,
) -> None:
    """Проверяет целый счётчик без преобразования значения.

    positive=True используется для номера строки: требуется число от 1.
    По умолчанию допускается ноль.
    """
    if is_empty(value):
        if required:
            raise ValidationError("Целое число обязательно.")
        return

    # bool является подклассом int, поэтому исключаем его явно.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            "Ожидается целое число, без дробной части и преобразования строк."
        )

    minimum = 1 if positive else 0
    if value < minimum:
        raise ValidationError(f"Число должно быть не меньше {minimum}.")


def validate_platform(value: object, *, required: bool = True) -> None:
    """Принимает только точные значения Twitch или Kick."""
    if is_empty(value):
        if required:
            raise ValidationError("Платформа обязательна.")
        return

    if (
        not isinstance(value, str)
        or value not in config.ALLOWED_STREAMING_PLATFORMS
    ):
        raise ValidationError("Платформа должна быть строго Twitch или Kick.")
def validate_err(value: object, *, required: bool = True) -> None:
    """Проверяет ERR: конечное число от 0 до 100 включительно."""
    if is_empty(value):
        if required:
            raise ValidationError("ERR обязателен.")
        return

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("ERR должен быть числом, а не строкой или bool.")

    # Такое сравнение также отклоняет NaN и бесконечность.
    if not 0 <= value <= 100:
        raise ValidationError("ERR должен быть конечным числом от 0 до 100.")


def calculate_err(reach: int, subscribers: int) -> float:
    """Рассчитывает ERR и округляет до двух знаков.

    reach — средний охват;
    subscribers — число подписчиков, строго больше нуля.
    """
    validate_counter(reach)
    validate_counter(subscribers, positive=True)

    # Проверяем до округления, чтобы превышение не превратилось в 100.
    if reach > subscribers:
        raise ValidationError(
            "Расчётный ERR превышает 100%. Требуется проверка оператором."
        )

    result = (reach / subscribers) * 100
    validate_err(result)

    return round(result, 2)