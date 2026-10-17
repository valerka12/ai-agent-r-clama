"""Ключи точного сравнения без изменения исходных данных.

None означает: ключ не построен, нужна проверка.
Это не означает, что кандидат уникален.
"""

import re
from urllib.parse import urlsplit

from validation import ValidationError, validate_platform, validate_url


def _normalize_text(value: object) -> str | None:
    """Убирает лишние пробелы и различия регистра только для сравнения."""
    if not isinstance(value, str):
        return None

    normalized = " ".join(value.split()).casefold()
    return normalized or None


def _website_key(value: object) -> tuple[str, str, str, str] | None:
    """Сохраняет путь, параметры и фрагмент ссылки.

    HTTP и HTTPS считаются одним сайтом.
    www и другие поддомены автоматически не объединяются.
    """
    try:
        validate_url(value)
    except ValidationError:
        return None

    parsed = urlsplit(value)
    hostname = parsed.hostname
    port = parsed.port

    # validate_url уже проверил наличие хоста.
    authority = hostname.casefold()
    if ":" in authority:
        authority = f"[{authority}]"

    # Стандартный порт убираем только для соответствующего протокола.
    default_port = (
        parsed.scheme == "http" and port == 80
    ) or (
        parsed.scheme == "https" and port == 443
    )
    if port is not None and not default_port:
        authority = f"{authority}:{port}"

    path = parsed.path or "/"
    return authority, path, parsed.query, parsed.fragment


def telegram_key(value: object) -> tuple[str, str] | None:
    """Принимает @username или прямую публичную ссылку на канал.

    Приглашения, ссылки на посты и служебные адреса не принимаются.
    Проверяет формат, но не существование канала.
    """
    if not isinstance(value, str):
        return None

    value = value.strip()

    if value.startswith("@"):
        username = value[1:]
    else:
        try:
            validate_url(value)
        except ValidationError:
            return None

        parsed = urlsplit(value)

        if parsed.hostname.casefold() not in {
            "t.me",
            "telegram.me",
            "www.t.me",
            "www.telegram.me",
        }:
            return None

        if (
            parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            return None

        path = parsed.path
        if not path.startswith("/"):
            return None

        username = path[1:]
        if username.endswith("/"):
            username = username[:-1]

    # Консервативно принимаем обычные публичные usernames.
    # Короткие и нестандартные адреса отправляем на ручную проверку.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        return None

    username = username.casefold()

    if username in {
        "share",
        "proxy",
        "socks",
        "login",
        "joinchat",
        "addstickers",
        "addemoji",
        "setlanguage",
        "confirmphone",
        "invoice",
        "addtheme",
        "contact",
        "boost",
    }:
        return None

    return "telegram", username


def advertiser_key(
    name: object,
    website: object = None,
    telegram: object = None,
) -> tuple[str, ...] | None:
    """Строит ключ компании по названию и сайту либо публичному TG.

    telegram передаётся явно из проверенного источника.
    Ручное поле «Telegram/почта» эта функция не читает.
    """
    normalized_name = _normalize_text(name)
    if normalized_name is None:
        return None

    # Пустой сайт допускает переход к TG.
    # Непустой, но некорректный сайт требует проверки.
    if website is not None and website != "":
        site = _website_key(website)
        if site is None:
            return None
        return ("advertiser", normalized_name, "website", *site)

    channel = telegram_key(telegram)
    if channel is None:
        return None

    return "advertiser", normalized_name, "telegram", channel[1]


def streamer_key(
    nickname: object,
    platform: object,
) -> tuple[str, str, str] | None:
    """Строит ключ из ника и точного названия платформы."""
    try:
        validate_platform(platform)
    except ValidationError:
        return None

    if not isinstance(nickname, str):
        return None

    nickname = nickname.strip()

    # Ссылки, пробелы и @ не превращаем в ник автоматически.
    if not re.fullmatch(r"[A-Za-z0-9_]+", nickname):
        return None

    return "streamer", platform, nickname.casefold()

def row_key(table_name: str, row: dict) -> tuple[str, ...] | None:
    """Ключ записи. Ручные контакты не используются."""
    if table_name == "advertisers":
        return advertiser_key(
            row.get("Название компании"), row.get("Сайт"), row.get("Соцсети")
        )
    if table_name == "telegram":
        return telegram_key(row.get("Ссылка"))
    if table_name == "streamers":
        return streamer_key(row.get("Стример"), row.get("Платформа (Twitch/Kick)"))
    raise ValidationError(f"Неизвестная таблица: {table_name!r}.")


def find_matches(table_name: str, candidate: dict, rows: list[dict]) -> dict:
    """Индексы совпадений и неоднозначных строк; никаких записей.

    Индексы отсчитываются от нуля, без заголовка CSV.
    Похожие названия — только сигнал оператору, не автоматический дубль.
    """
    key = row_key(table_name, candidate)
    result = {"key": key, "exact": [], "review": []}
    label = {
        "advertisers": "Название компании",
        "telegram": "Название",
        "streamers": "Стример",
    }[table_name]
    name = _normalize_text(candidate.get(label))
    for index, row in enumerate(rows):
        existing = row_key(table_name, row)
        if key is not None and existing == key:
            result["exact"].append(index)
            continue
        other = _normalize_text(row.get(label))
        if existing is None or (
            name and other
            and (max(len(name), len(other)) > 256 or name_similarity(name, other) >= 0.85)
            and not (
                table_name == "streamers"
                and candidate.get("Платформа (Twitch/Kick)")
                != row.get("Платформа (Twitch/Kick)")
            )
        ):
            result["review"].append(index)
    return result


def name_similarity(left: str, right: str) -> float:
    """Нормализованное сходство Левенштейна: 1 — одинаковые строки.

    Длинные необычные названия требуют проверки, а не дорогого расчёта.
    """
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if max(len(left), len(right)) > 256:
        raise ValidationError("Слишком длинные названия требуют ручной проверки.")
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_char in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[column_index] + 1,
                               previous[column_index - 1] + (left_char != right_char)))
        previous = current
    return 1 - previous[-1] / len(left)
