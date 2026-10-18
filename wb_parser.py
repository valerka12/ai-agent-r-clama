"""WB-кандидаты и проверяемый отбор. Не пишет в таблицы.

Поиск использует неофициальный endpoint, не обходит блокировки.
Обогащение — доказательства из операторской выгрузки; отсутствующие факты
не придумываются. Источник доказательства и дата обязательны.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import random
import socket
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

import config
from dedup import advertiser_key, telegram_key
from validation import ValidationError, validate_url


class SourceError(RuntimeError):
    """Безопасная ошибка источника без URL с токенами и тела ответа."""


class TemporarySourceError(SourceError):
    """Повторяемая серверная ошибка."""


class Source(ABC):
    """Минимальный интерфейс источника для оркестратора."""

    @abstractmethod
    def collect(self, **options):
        """Возвращает решения с кандидатами и доказательствами."""


class SourceHTTP:
    """Общий HTTP-транспорт источников: 3 попытки, лимит и паузы.

    Определён рядом с первым источником, чтобы не добавлять несогласованный модуль.
    Не логирует query-параметры: они могут содержать API-токен.
    """

    def __init__(self, *, client=None, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=config.HTTP_TIMEOUT_SECONDS,
                                            follow_redirects=False,
                                            headers={"User-Agent": "AdvertiserResearchMVP/0.1"})
        self.owns_client = client is None
        self.sleep = sleep
        self.requests = 0

    def close(self):
        if self.owns_client:
            self.client.close()

    def get(self, url, *, params=None, json_response=True):
        retries = Retrying(stop=stop_after_attempt(3),
                           wait=wait_exponential(min=2, max=30),
                           retry=retry_if_exception_type((httpx.TransportError, TemporarySourceError)),
                           sleep=self.sleep, reraise=True)
        try:
            for attempt in retries:
                with attempt:
                    if self.requests >= config.SOURCE_MAX_REQUESTS:
                        raise SourceError("Достигнут лимит запросов за запуск.")
                    if self.requests:
                        self.sleep(random.uniform(*config.SOURCE_DELAY_SECONDS))
                    self.requests += 1
                    with self.client.stream("GET", url, params=params) as response:
                        status = response.status_code
                        if status in {401, 403, 429, 498}:
                            raise SourceError(f"Источник отказал: HTTP {status}. Проверьте доступ/лимит; обход отключён.")
                        if status >= 500:
                            raise TemporarySourceError(f"Временная ошибка источника: HTTP {status}.")
                        if status != 200:
                            raise SourceError(f"Неожиданный ответ источника: HTTP {status}.")
                        chunks = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > config.HTTP_MAX_RESPONSE_BYTES:
                                raise SourceError("Ответ источника превышает допустимый размер.")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        if not json_response:
                            return raw.decode("utf-8", errors="replace")
                        try:
                            return json.loads(raw)
                        except (ValueError, UnicodeError) as error:
                            raise SourceError("Источник вернул не JSON; возможно, изменилась схема или включена защита.") from error
        except httpx.TransportError as error:
            raise SourceError("Сетевой запрос не выполнен после трёх попыток; TLS-проверка включена.") from error


def read_json(path):
    path = Path(path)
    if path.stat().st_size > config.HTTP_MAX_RESPONSE_BYTES:
        raise SourceError("Файл выгрузки слишком большой.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise SourceError("Некорректная JSON-выгрузка.") from error


def country_code(value):
    if not isinstance(value, str):
        return None
    return {"ru": "ru", "рф": "ru", "россия": "ru", "russia": "ru",
            "by": "by", "беларусь": "by", "белоруссия": "by", "belarus": "by"}.get(value.strip().casefold())


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def proof_value(proofs, field, now):
    """Только явно проверенные оператором факты со ссылкой и датой проверки."""
    item = proofs.get(field)
    if not isinstance(item, dict) or item.get("operator_verified") is not True:
        return None
    checked = timestamp(item.get("checked_at"))
    if (checked is None or checked > now
            or (now - checked).total_seconds() > config.ACTIVE_WITHIN_DAYS * 86400):
        return None
    try:
        validate_url(item.get("url"))
    except ValidationError:
        return None
    return item.get("value")


def _valid_url(value):
    try:
        validate_url(value)
        return value
    except ValidationError:
        return None


def assess_brand(product, proofs, *, niche=None, now=None, marketplace_metrics=True):
    """Результат: accept/review/reject + score + reasons + candidate.

    proofs — явная карта проверенных фактов, а не вывод LLM.
    Не заполняем ручные поля даже контактами из доказательств.
    """
    now = now or datetime.now(timezone.utc)
    result = {"decision": "review", "score": 0, "reasons": [], "candidate": {},
              "source": "wb", "evidence": proofs, "observed_at": now.isoformat()}
    if not isinstance(product, dict) or not isinstance(proofs, dict):
        result["reasons"] = ["Некорректный товар или доказательства."]
        return result
    brand = product.get("brand")
    if not isinstance(brand, str) or not brand.strip():
        result["decision"] = "reject"
        result["reasons"] = ["У товара нет названия бренда."]
        return result
    result["candidate"] = {"Название компании": brand.strip()}
    if marketplace_metrics:
        rating, reviews = product.get("reviewRating"), product.get("feedbacks")
        if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not math.isfinite(rating):
            result["reasons"].append("Нет подтверждённого рейтинга товара.")
        elif rating < config.WB_MIN_RATING:
            result["decision"] = "reject"
            result["reasons"].append("Рейтинг товара ниже порога.")
        if isinstance(reviews, bool) or not isinstance(reviews, int):
            result["reasons"].append("Нет достоверного числа отзывов.")
        elif reviews < config.WB_MIN_REVIEWS:
            result["decision"] = "reject"
            result["reasons"].append("Недостаточно отзывов.")
    verified_brand = proof_value(proofs, "brand", now)
    if not isinstance(verified_brand, str) or verified_brand.strip().casefold() != brand.strip().casefold():
        result["reasons"].append("Не подтверждена связь доказательств с брендом.")
    country = country_code(proof_value(proofs, "country", now))
    if country is None:
        result["reasons"].append("Страна не подтверждена: нужны РФ или Беларусь.")
    else:
        result["candidate"]["Страна"] = {"ru": "Россия", "by": "Беларусь"}[country]
    site = _valid_url(proof_value(proofs, "website", now))
    social = []
    signals = []
    if site:
        result["candidate"]["Сайт"] = site
        signals.append((15, "сайт"))
    for field, host, points in (("instagram", "instagram.com", 10),
                                ("telegram", "t.me", 10), ("youtube", "youtube.com", 5)):
        value = _valid_url(proof_value(proofs, field, now))
        if value:
            actual_host = urlsplit(value).hostname.casefold()
            if actual_host == host or actual_host == "www." + host:
                social.append(value)
                signals.append((points, field))
    if social:
        result["candidate"]["Соцсети"] = " | ".join(social)
    contact = proof_value(proofs, "contact", now)
    if isinstance(contact, str) and contact.strip():
        signals.append((20, "контакт подтверждён; ручное поле не заполняется"))
    else:
        result["reasons"].append("Нет проверенного контакта.")
    activity = timestamp(proof_value(proofs, "last_activity", now))
    if activity and 0 <= (now - activity).total_seconds() <= config.ACTIVE_WITHIN_DAYS * 86400:
        signals.append((15, "активность менее 30 дней"))
    else:
        result["reasons"].append("Нет подтверждения недавней активности.")
    if proof_value(proofs, "advertising", now) is True:
        signals.append((10, "есть рекламные размещения"))
    verified_niche = proof_value(proofs, "niche", now)
    if isinstance(verified_niche, str) and verified_niche.strip():
        result["candidate"]["Ниша"] = verified_niche
        if niche is not None and niche.casefold() in verified_niche.casefold():
            signals.append((15, "совпадение ниши"))
        elif niche is not None:
            result["reasons"].append("Ниша не совпадает с запросом.")
    elif niche is not None:
        result["reasons"].append("Ниша не подтверждена.")
    if proof_value(proofs, "agency", now) is True:
        result["decision"] = "reject"
        result["reasons"].append("Рекламное агентство исключено.")
    age = proof_value(proofs, "account_age_days", now)
    if type(age) is int and age < config.MIN_ACCOUNT_AGE_DAYS:
        result["decision"] = "reject"
        result["reasons"].append("Аккаунт моложе 90 дней.")
    elif type(age) is not int:
        result["reasons"].append("Возраст аккаунта не подтверждён.")
    followers = proof_value(proofs, "followers", now)
    micro = proof_value(proofs, "micro_niche", now) is True
    if type(followers) is int and followers < config.MIN_SOCIAL_FOLLOWERS and not micro:
        result["decision"] = "reject"
        result["reasons"].append("Менее 1000 подписчиков без исключения микрониши.")
    elif type(followers) is not int:
        result["reasons"].append("Число подписчиков не подтверждено.")
    if not site:
        result["reasons"].append("Нет проверенного сайта.")
    if not social:
        result["reasons"].append("Нет проверенных социальных страниц.")
    product_name = product.get("name")
    if isinstance(product_name, str) and product_name.strip():
        result["candidate"]["Рекламируемый продукт"] = product_name
    result["score"] = sum(points for points, _ in signals)
    result["signals"] = [label for _, label in signals]
    if result["decision"] != "reject" and not result["reasons"] and result["score"] >= config.ADVERTISER_MIN_SCORE:
        result["decision"] = "accept"
    elif result["score"] < config.ADVERTISER_MIN_SCORE:
        result["reasons"].append("Скор ниже порога качества.")
    return result


class WBSource(Source):
    def __init__(self, http=None):
        self.http = http

    def collect(self, *, payload=None, proofs=None, query=None, limit=config.MVP_LIMIT,
                niche=None, allow_unofficial=False):
        if type(limit) is not int or not 1 <= limit <= config.MVP_LIMIT:
            raise SourceError("Лимит WB должен быть от 1 до 50.")
        if proofs is None:
            proofs = {}
        if not isinstance(proofs, dict):
            raise SourceError("Карта доказательств брендов должна быть объектом.")
        if payload is None:
            if not allow_unofficial:
                raise SourceError("Сетевой WB отключён: нужен --allow-unofficial-wb либо --wb-file.")
            if not isinstance(query, str) or not query.strip():
                raise SourceError("Для поиска WB нужен --query.")
            http = self.http or SourceHTTP()
            try:
                # robots недоступен/запрещает — не продолжаем запрос каталога.
                robots = http.get("https://search.wb.ru/robots.txt", json_response=False)
                rules = RobotFileParser()
                rules.parse(robots.splitlines())
                if not rules.can_fetch("AdvertiserResearchMVP", config.WB_SEARCH_URL):
                    raise SourceError("robots.txt запрещает запрос каталога.")
                payload = http.get(config.WB_SEARCH_URL, params={
                    "appType": 1, "curr": "rub", "dest": -1257786,
                    "query": query, "resultset": "catalog", "sort": "popular", "page": 1,
                })
            finally:
                if self.http is None:
                    http.close()
        if not isinstance(payload, dict):
            raise SourceError("WB: ожидается JSON-объект.")
        data = payload.get("data", payload)
        products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(products, list):
            raise SourceError("WB: отсутствует массив products; схема источника изменилась.")
        decisions = []
        seen = set()
        # Один слабый товар не должен скрывать более сильный товар того же бренда.
        def product_rank(item):
            if not isinstance(item, dict):
                return (False, -1, -1)
            rating, reviews = item.get("reviewRating"), item.get("feedbacks")
            if (type(rating) not in {int, float} or not math.isfinite(rating)
                    or type(reviews) is not int):
                return (False, -1, -1)
            return (rating >= config.WB_MIN_RATING and reviews >= config.WB_MIN_REVIEWS,
                    reviews, rating)

        for product in sorted(products, key=product_rank, reverse=True):
            brand = product.get("brand") if isinstance(product, dict) else None
            key = brand.strip().casefold() if isinstance(brand, str) else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            decisions.append(assess_brand(product, proofs.get(key, {}), niche=niche))
            if len(decisions) >= limit:
                break
        return decisions


def _public_url(url):
    """Отказываемся от localhost/частных адресов при обогащении сайтов."""
    validate_url(url)
    parsed = urlsplit(url)
    if parsed.port not in {None, 80, 443}:
        raise SourceError("Нестандартные порты сайтов не поддерживаются.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise SourceError("Не удалось проверить адрес сайта.") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise SourceError("Для обогащения разрешены только публичные адреса сайтов.")
    return parsed


def enrich_website(brand, url, *, http=None):
    """Читает одну публичную страницу с учётом robots.txt.

    Социальные ссылки и контакты — предложения, не подтверждённые факты.
    Не подставляет их в ручные поля или подтверждения оператора.
    Перенаправления автоматически не выполняются.
    """
    from html.parser import HTMLParser
    import re
    from urllib.parse import urljoin

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
            self.text = []
            self.ignored = 0

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style"}:
                self.ignored += 1
            if tag == "a":
                href = dict(attrs).get("href")
                if isinstance(href, str):
                    self.links.append(href)

        def handle_endtag(self, tag):
            if tag in {"script", "style"}:
                self.ignored = max(0, self.ignored - 1)

        def handle_data(self, data):
            if not self.ignored:
                self.text.append(data)

    parsed = _public_url(url)
    client = http or SourceHTTP()
    try:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rules = RobotFileParser()
        rules.parse(client.get(origin + "/robots.txt", json_response=False).splitlines())
        if not rules.can_fetch("AdvertiserResearchMVP", url):
            raise SourceError("robots.txt сайта запрещает обогащение.")
        # Повторная проверка перед запросом страницы; DNS pinning не реализован.
        _public_url(url)
        html = client.get(url, json_response=False)
        page = Page()
        page.feed(html)
        text = " ".join(page.text)
        if not isinstance(brand, str) or not brand.strip() or brand.casefold() not in text.casefold():
            raise SourceError("Страница не содержит название бренда: принадлежность сайта не подтверждена.")
        social = set()
        contacts = set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text))
        for href in page.links:
            if href.lower().startswith("mailto:"):
                address = href[7:].split("?", 1)[0]
                if re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", address):
                    contacts.add(address)
                continue
            target = urljoin(url, href)
            if not _valid_url(target):
                continue
            host = urlsplit(target).hostname.casefold()
            if host in {"t.me", "telegram.me", "instagram.com", "www.instagram.com",
                        "youtube.com", "www.youtube.com", "youtu.be"}:
                social.add(target)
        return {"url": url, "checked_at": datetime.now(timezone.utc).isoformat(),
                "brand_mentioned": True, "social_suggestions": sorted(social)[:20],
                "contact_suggestions": sorted(contacts)[:20],
                "operator_verified": False}
    finally:
        if http is None:
            client.close()


class BrowserBatchError(SourceError):
    """Ошибка браузерного пакета: уже прочитанные решения доступны для просмотра."""

    def __init__(self, message, decisions):
        super().__init__(message)
        self.decisions = decisions


def browser_profile_dir(platform):
    """Только выделенные профили проекта; личные профили браузеров не используются."""
    allowed = {'wb', 'ozon', 'instagram', 'youtube', 'twitch', 'kick', 'tgstat'}
    if platform not in allowed:
        raise SourceError('Неизвестная платформа профиля браузера.')
    root = config.CACHE_DIR / 'browser_profiles'
    path = root / platform
    for directory in (config.CACHE_DIR, root, path):
        if directory.is_symlink():
            raise SourceError('Символьные ссылки для браузерных профилей запрещены.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    path.chmod(0o700)
    return path


def setup_browser_session(platform):
    """Первый вход в выделенный профиль; закрытие окна сохраняет сессию.

    Наличие профиля не доказывает авторизацию. Следующий сбор проверяет доступ.
    Пароли, OTP и cookies не читаются и не попадают в отчёт.
    """
    import os
    from playwright.sync_api import sync_playwright, Error as BrowserError
    homes = {
        'tgstat': 'https://tgstat.ru/', 'wb': 'https://www.wildberries.ru/',
        'ozon': 'https://www.ozon.ru/', 'instagram': 'https://www.instagram.com/',
        'youtube': 'https://www.youtube.com/', 'twitch': 'https://www.twitch.tv/',
        'kick': 'https://kick.com/',
    }
    if platform not in homes:
        raise SourceError('Неизвестная платформа входа.')
    profile = browser_profile_dir(platform)
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(config.CACHE_DIR / 'ms-playwright'))
    closed = False
    def mark_closed(*args):
        nonlocal closed
        closed = True
    try:
        with sync_playwright() as engine:
            context = engine.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, locale='ru-RU', accept_downloads=False)
            try:
                context.on('close', mark_closed)
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto(homes[platform], wait_until='domcontentloaded', timeout=30000)
                if response is None or response.status >= 400:
                    raise SourceError('Страница входа недоступна. Восстановите доступ обычным способом.')
                deadline = time.monotonic() + 840
                while not closed and time.monotonic() < deadline:
                    try:
                        page.wait_for_timeout(500)
                    except BrowserError:
                        if not closed and not page.is_closed():
                            raise
                        break
                if not closed and not page.is_closed():
                    raise SourceError('Время настройки истекло. Проверьте вход при следующем запуске.')
            finally:
                if not closed:
                    context.close()
    except BrowserError as error:
        raise SourceError('Не удалось открыть отдельный профиль. Закройте другие процессы этой платформы и проверьте установку Chromium.') from error
    return {'status': 'completed', 'session_setup': platform, 'session_status': 'saved_unverified',
            'message': 'Профиль сохранён. Авторизация будет проверена при сборе.',
            'dry_run': True, 'summary': {'total': 0, 'failed': 0}, 'results': []}


class VisibleBrowserSource(Source):
    """Общий браузер для согласованных модулей. Только видимый DOM, без API.

    Прямые ссылки вместо автоматического поиска. Каждый запуск использует
    отдельную временную сессию; вход и проверку доступа выполняет оператор.
    """

    platform = ""
    table = "advertisers"
    hosts = ()
    path_pattern = ""
    content_selector = "main"
    ready_selector = "main h1"

    def normalize_url(self, value):
        import re
        validate_url(value)
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in self.hosts
                or parsed.port is not None or parsed.query or parsed.fragment
                or not re.fullmatch(self.path_pattern, parsed.path)):
            raise SourceError(f"{self.platform}: нужна прямая HTTPS-ссылка на товар/профиль без параметров.")
        reserved = {"accounts", "explore", "direct", "reels", "stories", "p", "reel",
                    "login", "signup", "directory", "search", "browse", "settings", "downloads",
                    "events", "community-guidelines", "dmca-policy", "privacy-policy", "terms-of-service",
                    "cookie-policy", "partner-terms-and-conditions", "advertising-policy", "kicks-usage-policy"}
        if parsed.path.strip("/").split("/")[0].casefold() in reserved:
            raise SourceError("Служебная страница не является профилем.")
        return value.rstrip("/")

    def collect(self, *, urls=None, payload=None, proofs=None, countries=("ru", "by"),
                limit=5, niche=None, interactive_login=False, discover=False, keep_session=False, unattended=False):
        if type(unattended) is not bool or (unattended and interactive_login):
            raise SourceError("Автономный режим несовместим с ручным входом.")
        self.unattended = unattended
        keep_session = keep_session or unattended
        if type(keep_session) is not bool:
            raise SourceError("keep_session должен быть bool.")
        self.keep_session = keep_session
        self.discover = discover
        if discover and (urls or payload is not None or not getattr(self, "catalog_url", None)):
            raise SourceError("Автопоиск доступен WB/Ozon/Twitch/Kick и не смешивается с прямыми ссылками/снимками.")
        self.interactive_login = interactive_login
        if type(interactive_login) is not bool:
            raise SourceError("interactive_login должен быть bool.")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise SourceError("Браузерный лимит: от 1 до 10 страниц.")
        if not countries or set(countries) - {"ru", "by"}:
            raise SourceError("Разрешены только ru и by.")
        if proofs is None:
            proofs = {}
        if not isinstance(proofs, dict):
            raise SourceError("Доказательства должны быть JSON-объектом с ключами-ссылками.")
        decisions = []
        def consume(capture):
            if not isinstance(capture, dict):
                raise SourceError("Некорректный снимок страницы.")
            url = self.normalize_url(capture.get("url"))
            decisions.append(self.decide(capture, proofs.get(url, {}), countries, niche))
        if payload is not None:
            captures = payload.get("captures") if isinstance(payload, dict) else None
            if not isinstance(captures, list) or not captures:
                raise SourceError("Нужен непустой массив captures.")
            # Проверяем все адреса до обработки; импорт не открывает браузер.
            for capture in captures:
                if not isinstance(capture, dict):
                    raise SourceError("Некорректный снимок страницы.")
                self.normalize_url(capture.get("url"))
            for capture in captures[:limit]:
                consume(capture)
            return decisions
        if not discover and (not isinstance(urls, list) or not urls):
            raise SourceError("Для браузерного сбора укажите --urls с прямыми ссылками.")
        normalized = list(dict.fromkeys(self.normalize_url(url) for url in (urls or [])))[:limit]
        self.discovery_limit = limit
        try:
            self._browse(normalized, consume)
        except SourceError as error:
            raise BrowserBatchError(str(error), decisions) from error
        return decisions

    def _browse(self, urls, consume):
        import os
        import sys
        from playwright.sync_api import sync_playwright, Error as BrowserError
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.CACHE_DIR / "ms-playwright"))
        try:
            with sync_playwright() as engine:
                browser = None
                context = None
                try:
                    if getattr(self, "keep_session", False):
                        context = engine.chromium.launch_persistent_context(
                            user_data_dir=str(browser_profile_dir(self.platform.lower())),
                            headless=getattr(self, "unattended", False), locale="ru-RU", accept_downloads=False)
                    else:
                        browser = engine.chromium.launch(headless=False)
                        context = browser.new_context(locale="ru-RU", accept_downloads=False)
                    page = context.new_page()
                    if getattr(self, "discover", False):
                        urls = self._discover_urls(page)
                    for index, url in enumerate(urls):
                        if index:
                            time.sleep(random.uniform(*config.SOURCE_DELAY_SECONDS))
                        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        if response is None or response.status >= 400:
                            raise SourceError(f"{self.platform}: страница недоступна (HTTP {response.status if response else 'нет ответа'}).")
                        print(f"{self.platform}: чтение {index + 1}/{len(urls)}", file=sys.stderr)
                        # Публичные страницы обрабатываются без Enter на каждой карточке.
                        # Кнопка «Войти» в шапке сама по себе не означает закрытый профиль.
                        if self._login_required(page):
                            if getattr(self, "unattended", False) or not getattr(self, "interactive_login", False) or not sys.stdin.isatty():
                                raise SourceError("auth_required: сохранённый вход недоступен. Авторизуйте рабочий аккаунт отдельно; автономный сбор не ожидает ввода.")
                            input("Войдите в открытом окне и нажмите Enter один раз. ")
                            if self._login_required(page):
                                raise SourceError("Вход не завершён; пакет не записан.")
                        page.locator(self.ready_selector).first.wait_for(state="visible", timeout=20000)
                        if getattr(self, "ready_text_pattern", None):
                            page.wait_for_function("opts => { const root=document.querySelector(opts.selector); return !!root && new RegExp(opts.pattern, 'i').test(root.innerText); }",
                                                   arg={"selector": self.content_selector, "pattern": self.ready_text_pattern}, timeout=20000)
                        final_url = self.normalize_url(page.url)
                        if final_url != url:
                            raise SourceError("Адрес изменился: проверьте прямую ссылку и повторите запуск.")
                        capture = self._capture(page)
                        import re
                        if re.search(r"captcha|verify you are human|подтвердите.{0,25}не робот|доступ ограничен|access denied", capture["text"], re.I):
                            raise SourceError("Обнаружена проверка доступа. Сбор остановлен.")
                        capture["observed_at"] = datetime.now(timezone.utc).isoformat()
                        consume(capture)
                finally:
                    if browser is not None:
                        browser.close()
                    elif context is not None:
                        context.close()
        except (BrowserError, EOFError, KeyboardInterrupt) as error:
            raise SourceError("Браузерный сбор прерван. Запись пакета запрещена.") from error

    def _discover_urls(self, page):
        from urllib.parse import urljoin
        response = page.goto(self.catalog_url, wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status >= 400:
            raise SourceError("Каталог недоступен; автопоиск остановлен.")
        if urlsplit(page.url).hostname not in self.hosts:
            raise SourceError("Каталог перенаправил на другой сайт.")
        cards = page.locator(self.catalog_selector)
        cards.first.wait_for(state="visible", timeout=20000)
        hrefs = cards.evaluate_all("els => els.filter(e=>e.getClientRects().length).map(e=>e.getAttribute('href'))")
        urls = []
        for href in hrefs:
            try:
                url = self.normalize_url(urljoin(self.catalog_url, href or ""))
            except (SourceError, ValidationError):
                continue
            if url not in urls:
                urls.append(url)
            if len(urls) >= self.discovery_limit:
                break
        if not urls:
            raise SourceError("В каталоге не найдены подходящие ссылки. Возможно, изменилась разметка.")
        return urls

    def _login_required(self, page):
        import re
        path = urlsplit(page.url).path
        if re.search(r"/(?:accounts/login|login|signin)(?:/|$)", path, re.I):
            return True
        return page.locator('input[type="password"]:visible').count() > 0

    def _capture(self, page):
        """Читаем только выбранный блок профиля, исключая чат и навигацию."""
        capture = page.locator(self.content_selector).first.evaluate(r"""root => {
            const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            // Чат внутри выбранного блока означает неверную область сбора.
            const hasChat = !!root.querySelector('[role="log"],[data-a-target="chat-scroller"],#chatroom');
            return {url:location.href,title:Array.from(root.querySelectorAll('h1,h2')).filter(visible).map(e=>e.innerText).join(' '),
                text:root.innerText.slice(0,100000), has_chat:hasChat,
                links:Array.from(root.querySelectorAll('a[href]')).filter(visible).map(e=>e.href).slice(0,100)};
        }""")
        if capture.get("has_chat"):
            raise SourceError("В область профиля попал чат; сбор остановлен до уточнения селектора.")
        return capture

    def decide(self, capture, proofs, countries, niche):
        """Факты оператора связаны с конкретной страницей; текст не даёт прав записи."""
        from tg_parser import placement_evidence
        import re
        now = datetime.now(timezone.utc)
        observed = timestamp(capture.get("observed_at"))
        url = self.normalize_url(capture.get("url"))
        text = capture.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise SourceError("Пустой или слишком большой текст страницы.")
        if not isinstance(proofs, dict):
            raise SourceError("Некорректные доказательства страницы.")
        reasons = []
        fresh = observed is not None and 0 <= (now - observed).total_seconds() <= 86400
        if not fresh:
            reasons.append("Снимок старше суток, из будущего или без даты.")
        country = country_code(proof_value(proofs, "country", now))
        # Только явная подпись страны, не язык трансляции и не домен.
        visible_countries = {country_code(value) for value in re.findall(
            r"(?im)^(?:страна|country)[ \t]*[:\n][ \t]*([^\n]+)$", text)}
        if visible_countries and (None in visible_countries or len(visible_countries) > 1
                                  or (country is not None and country not in visible_countries)):
            reasons.append("Указания страны неоднозначны или противоречат доказательствам.")
        if country is None and len(visible_countries) == 1 and None not in visible_countries and fresh:
            country = visible_countries.pop()
        if country is None:
            reasons.append("Страна не подтверждена оператором: ru/by.")
        elif country not in countries:
            reasons.append("Страна не входит в фильтры запуска.")
        placement = placement_evidence(text)
        if self.table == "streamers":
            nick = urlsplit(url).path.strip("/")
            if not re.search(r"(?<![\w])" + re.escape(nick) + r"(?![\w])", text, re.I):
                reasons.append("Имя профиля не найдено в видимом тексте; возможна страница входа.")
            candidate = {"Стример": nick, "Платформа (Twitch/Kick)": self.platform}
            if fresh:
                candidate["Последнее обновление"] = observed.isoformat()
            # Только полные целые числа рядом с явной подписью. Текущий онлайн
            # и округлённые 12K не считаются исторической статистикой.
            for field, label in (("Подписчики", r"(?:подписчики|followers|фолловеры|фолловеров)"),
                                  ("Средний онлайн", r"(?:средний онлайн|average viewers)"),
                                  ("Пиковый онлайн", r"(?:пиковый онлайн|peak viewers)")):
                matches = re.findall(r"(?im)^" + label + r"[ \t]*[:\n][ \t]*([0-9][0-9 \u00a0\u202f]*)[ \t]*$", text)
                if field == "Подписчики":
                    matches += re.findall(r"(?im)^([0-9][0-9 \u00a0\u202f]*)[ \t]*(?:\n[ \t]*)?" + label + r"[ \t]*$", text)
                values = {int(re.sub(r"\s", "", match)) for match in matches}
                if len(values) == 1:
                    candidate[field] = values.pop()
            if placement["status"] != "offered":
                reasons.append("Не подтверждены рекламное предложение и контакт.")
            decision = {"candidate": candidate, "decision": "review" if reasons else "accept", "score": None,
                        "reasons": reasons}
        else:
            brand = proof_value(proofs, "brand", now)
            brand_fact = proofs.get("brand", {})
            # Название YouTube-канала или продавца само по себе не доказывает бренд.
            bound = (isinstance(brand_fact, dict) and brand_fact.get("url", "").rstrip("/") == url
                     and isinstance(brand, str) and brand.strip()
                     and brand.casefold() in text.casefold())
            if not bound:
                reasons.append("Бренд не подтверждён на этой странице; нужен факт brand со ссылкой на неё.")
                decision = {"candidate": {}, "decision": "review", "score": None, "reasons": []}
            else:
                product = {"brand": brand}
                # Рейтинг/отзывы — только подтверждённые факты товара, не выдуманные значения.
                visible_metrics = capture.get("product_metrics", {}) if fresh else {}
                if not isinstance(visible_metrics, dict):
                    visible_metrics = {}
                for target, key in (("reviewRating", "rating"), ("feedbacks", "reviews")):
                    verified = proof_value(proofs, key, now)
                    visible = visible_metrics.get(target)
                    product[target] = verified if verified is not None else visible
                    if verified is not None and visible is not None and verified != visible:
                        reasons.append("Метрики товара противоречат проверенным фактам.")
                title = capture.get("title")
                if self.platform in {"WB", "Ozon"} and isinstance(title, str) and title.strip():
                    product["name"] = title.strip()
                decision = assess_brand(product, proofs, niche=niche,
                                        marketplace_metrics=self.platform in {"WB", "Ozon"})
            decision["reasons"].extend(reasons)
            if reasons and decision["decision"] == "accept":
                decision["decision"] = "review"
        decision.update(source=self.platform.lower() + "-web", mode="browser", observed_at=capture.get("observed_at"),
                        evidence={"url": url, "country": country, "proofs": proofs}, placement=placement,
                        contact_suggestions=placement["contacts"])
        if self.platform in {"WB", "Ozon"}:
            decision["product_observation"] = {"title": capture.get("title"),
                                                "metrics": capture.get("product_metrics", {})}
        return decision


class WBWebSource(VisibleBrowserSource):
    platform = "WB"
    hosts = ("www.wildberries.ru", "www.wildberries.by")
    path_pattern = r"/catalog/[0-9]+/detail\.aspx/?"

    content_selector = '.product-page'
    ready_selector = '.product-page [class*="productTitle--"]'

    def _capture(self, page):
        # Берём только сведения до отзывов и рекомендованных товаров.
        root = page.locator(self.content_selector).first
        capture = root.evaluate(r"""root => {
            const title = root.querySelector('[class*="productTitle--"]');
            const content = root.querySelector('[class*="productPageContent--"]');
            return {url:location.href, title:title?.innerText || '',
                text:(content?.innerText || '').split('Характеристики и описание')[0].slice(0,10000)};
        }""")
        import re
        matches = re.findall(r'(?m)^([0-5][.,][0-9])\s*[·•]\s*([0-9][0-9\s]*)\s+оцен(?:ка|ки|ок)\s*$', capture['text'])
        values = {(float(rating.replace(',', '.')), int(re.sub(r'\s', '', count))) for rating, count in matches}
        capture['product_metrics'] = {}
        if len(values) == 1:
            rating, count = values.pop()
            if rating <= 5:
                capture['product_metrics'] = {'reviewRating': rating, 'feedbacks': count}
        return capture

    catalog_url = 'https://www.wildberries.ru/'
    catalog_selector = 'main article.product-card a.j-card-link[href*="/detail.aspx"]'

    def normalize_url(self, value):
        from urllib.parse import urlsplit, urlunsplit, parse_qsl
        validate_url(value)
        parsed = urlsplit(value)
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if not pairs or any(key != 'targetUrl' for key, _ in pairs):
                raise SourceError('WB: неизвестные параметры ссылки; нужна прямая карточка.')
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', parsed.fragment))
        return super().normalize_url(value)
