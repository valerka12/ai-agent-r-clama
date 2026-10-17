"""TGStat API и импорт выгрузки. Без userbot и доступа к аккаунту Telegram.

Цены и рекламные контакты не угадываются; ERR вычисляется из метрик.
Общий транспорт первого источника переиспользуется без нового модуля.
"""

from datetime import datetime, timezone
import math
import os
import re

import config
from dedup import telegram_key
from validation import ValidationError, calculate_err, validate_counter
from wb_parser import Source, SourceError, SourceHTTP, country_code, timestamp


def _response(value):
    if not isinstance(value, dict):
        raise SourceError("TGStat: ожидается объект ответа.")
    if "status" in value:
        if value["status"] != "ok":
            # Тело ошибки может содержать секреты: не выводим его.
            messages = {
                "no_active_subscription": "TGStat: API-подписка не активирована или истекла (no_active_subscription). Проверьте доступ в личном кабинете; повтор запроса не поможет.",
                "token_invalid": "TGStat: токен не принят (token_invalid). Проверьте значение в локальном .env.",
                "empty_token": "TGStat: токен не передан (empty_token).",
                "quota_foreign_channel": "TGStat: тариф не разрешает чужие каналы (quota_foreign_channel). Бесплатный доступ ограничен подтверждёнными собственными каналами.",
                "quota_requests_reached": "TGStat: исчерпана месячная квота запросов (quota_requests_reached).",
                "quota_channel_reached": "TGStat: исчерпана квота уникальных каналов (quota_channel_reached).",
                "quota_keywords_reached": "TGStat: исчерпана квота ключевых слов (quota_keywords_reached).",
                "flood_control_10": "TGStat: слишком частые запросы (flood_control_10). Сбор остановлен.",
                "flood_control_60": "TGStat: слишком частые запросы (flood_control_60). Сбор остановлен.",
                "outdated_statistics": "TGStat: статистика устарела (outdated_statistics). Сервис рекомендует повторить запрос через 15 минут.",
            }
            code = value.get("error")
            if isinstance(code, str) and code in messages:
                raise SourceError(messages[code])
            raise SourceError("TGStat отказал в запросе. Проверьте токен, тариф и квоту.")
        value = value.get("response")
    if not isinstance(value, dict):
        raise SourceError("TGStat: отсутствует объект response.")
    return value


def placement_evidence(description):
    """Явное предложение рекламы рядом с публичным контактом.

    Это признак предложения, а не подтверждение сделки или личности контакта.
    Простое упоминание рекламы либо сотрудничества не считается продажей места.
    """
    if not isinstance(description, str):
        description = ""
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    negative = re.compile(
        r"реклам\w*\s+(?:у\s+нас\s+)?не\s+(?:размещ|прода|принима)|"
        r"не\s+(?:размещаем|прода[её]м|принимаем)\s+реклам|"
        r"без\s+рекламы|рекламы\s+нет", re.I)
    positive = re.compile(
        r"^[^\w@]{0,12}(?:"
        r"реклама(?=\s*(?:[:/|—–\-@]|$|https?://))|"
        r"(?:купить|заказать|разместить)\s+рекламу\b|"
        r"по\s+(?:вопросам\s+)?(?:размещения\s+)?рекламы\b|"
        r"(?:стоимость|прайс|цены|условия)\s+(?:на\s+)?(?:размещения\s+)?рекламы\b"
        r")", re.I)
    unrelated = re.compile(
        r"^(?:редакция|карьера|вакансии|поддержка|разбан|сотрудничество|"
        r"категория|гео\s+и\s+язык|сводная|читать\s+канал)\b", re.I)

    def contacts(text):
        values = set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text))
        values.update(re.findall(r"(?<![\w.+-])@[A-Za-z][A-Za-z0-9_]{4,31}\b", text))
        values.update("@" + username for username in re.findall(
            r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_/])", text))
        return sorted(values)

    if any(negative.search(line) for line in lines):
        return {"status": "not_offered", "contacts": [], "snippets": []}
    suggestions = set()
    snippets = []
    for index, line in enumerate(lines):
        if not positive.search(line):
            continue
        context = line
        if index + 1 < len(lines) and not unrelated.search(lines[index + 1]):
            context += "\n" + lines[index + 1]
        found = contacts(context[:300])
        if found:
            suggestions.update(found)
            snippets.append(context[:300])
    return {"status": "offered" if suggestions else "unconfirmed",
            "contacts": sorted(suggestions), "snippets": snippets[:3]}


def _check_placement(result, description):
    evidence = placement_evidence(description)
    result["placement"] = evidence
    if result["decision"] == "accept" and evidence["status"] != "offered":
        result["decision"] = "review"
        result["reasons"].append(
            "Нет явного предложения рекламного размещения с контактом; нужна проверка оператором."
            if evidence["status"] == "unconfirmed" else
            "В описании указано отсутствие или отказ от рекламы; нужна проверка оператором."
        )


def channel_decision(info, stat, *, observed_at, min_subscribers=config.TG_MIN_SUBSCRIBERS,
                     min_reach=config.TG_MIN_REACH, min_err=config.TG_MIN_ERR,
                     max_err=config.TG_MAX_ERR):
    now = datetime.now(timezone.utc)
    result = {"source": "tgstat", "decision": "review", "score": None,
              "candidate": {}, "reasons": [], "observed_at": observed_at,
              "evidence": {"info": info, "stat": stat}}
    try:
        info, stat = _response(info), _response(stat)
        if info.get("peer_type") != "channel" or stat.get("peer_type") != "channel":
            raise ValidationError("Принимаются только каналы, не чаты.")
        link = info.get("link")
        if isinstance(link, str) and link.startswith(("t.me/", "telegram.me/")):
            link = "https://" + link
        key = telegram_key(link or info.get("username"))
        if key is None:
            raise ValidationError("Нет однозначной публичной ссылки канала.")
        info_key = telegram_key(info.get("username"))
        stat_key = telegram_key(stat.get("username"))
        if info_key != key or stat_key != key:
            raise ValidationError("Идентичность канала в info/stat не совпадает.")
        if info.get("id") is None or info.get("id") != stat.get("id"):
            raise ValidationError("ID канала в info/stat не совпадает.")
        if country_code(info.get("country")) not in config.MVP_COUNTRIES:
            raise ValidationError("Страна канала не подтверждена как РФ или Беларусь.")
        restrictions = info.get("tgstat_restrictions")
        if restrictions is None:
            raise ValidationError("Нет сведений об ограничениях TGStat.")
        if restrictions:
            result["decision"] = "reject"
            raise ValidationError("У канала есть ограничения TGStat.")
        observed = timestamp(observed_at)
        if (observed is None or observed > now
                or (now-observed).total_seconds() > config.ACTIVE_WITHIN_DAYS * 86400):
            raise ValidationError("Нет свежей даты измерения с часовым поясом.")
        subscribers = stat.get("participants_count")
        reach = stat.get("avg_post_reach")
        validate_counter(subscribers, positive=True)
        validate_counter(reach)
        err = calculate_err(reach, subscribers)
        title = info.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("Нет названия канала.")
        result["candidate"] = {
            "Название": title, "Ссылка": "https://t.me/" + key[1],
            "Подписчики": subscribers, "Средний охват": reach,
            "ERR (%)": err, "Последнее обновление": observed.isoformat(),
        }
        category = info.get("category")
        if isinstance(category, str) and category.strip():
            result["candidate"]["Тематика"] = category
        if subscribers < min_subscribers or reach < min_reach or not min_err <= err <= max_err:
            result["decision"] = "reject"
            result["reasons"].append("Метрики ниже заданных порогов либо ERR вне диапазона.")
        else:
            result["decision"] = "accept"
        about = info.get("about")
        if isinstance(about, str):
            # Только предложения оператору. Поле Контакт не заполняем.
            result["contact_suggestions"] = sorted(set(re.findall(
                r"(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,31}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", about
            )))[:10]
        _check_placement(result, about)
    except (ValidationError, SourceError) as error:
        result["reasons"].append(str(error))
    return result


class TGStatSource(Source):
    def __init__(self, token=None, http=None):
        self.token = token if token is not None else os.getenv("TGSTAT_API_TOKEN", "")
        self.http = http

    def _call(self, http, method, **params):
        return _response(http.get(config.TGSTAT_BASE_URL + "/" + method,
                                  params={"token": self.token, **params}))

    def collect(self, *, payload=None, query=None, countries=("ru", "by"),
                limit=config.MVP_LIMIT, category=None,
                min_subscribers=config.TG_MIN_SUBSCRIBERS, min_reach=config.TG_MIN_REACH,
                min_err=config.TG_MIN_ERR, max_err=config.TG_MAX_ERR):
        if type(limit) is not int or not 1 <= limit <= config.MVP_LIMIT:
            raise SourceError("Лимит каналов должен быть от 1 до 50.")
        if not countries or set(countries) - config.MVP_COUNTRIES:
            raise SourceError("Разрешены только страны ru и by.")
        for value in (min_subscribers, min_reach):
            validate_counter(value)
        if not 0 <= min_err <= max_err <= 100:
            raise SourceError("Диапазон ERR должен быть от 0 до 100.")
        live = payload is None
        if live:
            if not self.token.strip():
                raise SourceError("Нет TGSTAT_API_TOKEN. Добавьте токен локально в .env либо используйте --tg-file.")
            if category is None and (not isinstance(query, str) or len(query.strip()) < 3):
                raise SourceError("Для TGStat нужен --query длиной от 3 символов или --category.")
            http = self.http or SourceHTTP()
            records = []
            try:
                seen = set()
                for country_index, country in enumerate(countries):
                    quota = math.ceil((limit - len(records)) / (len(countries) - country_index))
                    if quota <= 0:
                        break
                    params = {"country": country, "limit": quota, "peer_type": "channel",
                              "search_by_description": 1}
                    if query:
                        params["q"] = query
                    if category:
                        params["category"] = category
                    found = self._call(http, "channels/search", **params)
                    items = found.get("items")
                    if not isinstance(items, list):
                        raise SourceError("TGStat: поиск не вернул массив items.")
                    for item in items[:quota]:
                        if not isinstance(item, dict):
                            raise SourceError("TGStat: некорректный объект в items.")
                        channel_id = item.get("id")
                        if channel_id is None:
                            raise SourceError("TGStat: отсутствует ID канала.")
                        if channel_id in seen:
                            continue
                        seen.add(channel_id)
                        info = self._call(http, "channels/get", channelId=channel_id)
                        stat = self._call(http, "channels/stat", channelId=channel_id)
                        records.append({"info": info, "stat": stat,
                                        "observed_at": datetime.now(timezone.utc).isoformat()})
                        if len(records) >= limit:
                            break
                    if len(records) >= limit:
                        break
            finally:
                if self.http is None:
                    http.close()
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("channels"), list):
                raise SourceError("TG-выгрузка должна содержать массив channels с info, stat, observed_at.")
            records = payload["channels"][:limit]
        decisions = []
        for item in records:
            if not isinstance(item, dict):
                decisions.append({"source": "tgstat", "decision": "review", "candidate": {},
                                  "reasons": ["Некорректная запись канала."], "score": None})
                continue
            decision = channel_decision(item.get("info"), item.get("stat"),
                                        observed_at=item.get("observed_at"),
                                        min_subscribers=min_subscribers, min_reach=min_reach,
                                        min_err=min_err, max_err=max_err)
            if decision["candidate"]:
                info = _response(item["info"])
                if country_code(info.get("country")) not in countries:
                    decision["decision"] = "reject"
                    decision["reasons"].append("Страна не входит в выбранные фильтры запуска.")
            decision["mode"] = "live" if live else "import"
            decisions.append(decision)
        return decisions


# Браузерный режим отдельный от API: не требует API-токена.
def web_channel_url(value):
    """Только прямые публичные карточки TGStat или @username."""
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise SourceError("Адрес канала должен быть строкой.")
    if value.startswith("@"):
        key = telegram_key(value)
        if key is None:
            raise SourceError("Некорректный публичный username.")
        return "https://tgstat.ru/channel/@" + key[1] + "/stat"
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"tgstat.ru", "by.tgstat.com"}
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment):
        raise SourceError("Разрешены только HTTPS-карточки tgstat.ru и by.tgstat.com.")
    match = re.fullmatch(r"/channel/(@[A-Za-z][A-Za-z0-9_]{4,31})(?:/stat)?/?", parsed.path)
    if match is None or telegram_key(match[1]) is None:
        raise SourceError("Нужна прямая ссылка на публичную карточку канала.")
    return f"https://{parsed.hostname}/channel/@{match[1][1:].lower()}/stat"


def _web_number(value):
    if not isinstance(value, str):
        raise ValidationError("Не найдено точное число в карточке статистики.")
    value = re.sub(r"[\s\u00a0\u202f]", "", value)
    if not re.fullmatch(r"[0-9]+", value):
        raise ValidationError("Сокращённые или неоднозначные метрики не принимаются.")
    return int(value)


def web_channel_decision(capture, *, countries=("ru", "by"),
                         min_subscribers=config.TG_MIN_SUBSCRIBERS,
                         min_reach=config.TG_MIN_REACH, min_err=config.TG_MIN_ERR,
                         max_err=config.TG_MAX_ERR):
    """Разбирает видимые данные, не подставляя веб-снимок под API-ответ."""
    result = {"source": "tgstat-web", "mode": "browser", "decision": "review",
              "candidate": {}, "score": None, "reasons": []}
    try:
        if not isinstance(capture, dict):
            raise ValidationError("Некорректный снимок карточки.")
        source_url = web_channel_url(capture.get("url"))
        from urllib.parse import urlsplit
        username = urlsplit(source_url).path.split("/")[2][1:]
        body = capture.get("text")
        title = capture.get("title")
        cards = capture.get("metrics")
        if not isinstance(body, str) or not isinstance(title, str) or not title.strip() or not isinstance(cards, list):
            raise ValidationError("Снимок не содержит названия, текста или карточек метрик.")
        if not re.search(r"(?<![A-Za-z0-9_])@" + re.escape(username) + r"(?![A-Za-z0-9_])", body, re.I):
            raise ValidationError("Username страницы не совпадает с адресом.")
        # Desktop: заголовок без двоеточия и значение на следующей строке.
        # Узкий экран: «Гео и язык канала: Россия, Русский» одной строкой.
        geo_values = re.findall(
            r"(?m)^[ \t]*Гео и язык канала[ \t]*:?[ \t]*(?:\r?\n[ \t]*)*([^,\r\n]+)", body)
        geo_codes = {country_code(value) for value in geo_values}
        country = next(iter(geo_codes)) if len(geo_codes) == 1 else None
        if country not in countries:
            raise ValidationError("Страна канала не подтверждена как выбранная РФ/Беларусь.")
        observed = timestamp(capture.get("observed_at"))
        now = datetime.now(timezone.utc)
        if observed is None or not 0 <= (now-observed).total_seconds() <= config.ACTIVE_WITHIN_DAYS * 86400:
            raise ValidationError("Нет свежего времени наблюдения с часовым поясом.")
        def metric(label):
            values = set()
            for card in cards:
                if not isinstance(card, dict) or not isinstance(card.get("label"), str):
                    continue
                text = " ".join(card["label"].split()).upper()
                if re.search(label, text):
                    values.add(_web_number(card.get("value")))
            if len(values) != 1:
                raise ValidationError("Метрика отсутствует или неоднозначна; запись требует проверки.")
            return values.pop()
        subscribers = metric(r"(?:^| )ПОДПИСЧИКИ(?: |$)")
        reach = metric(r"СРЕДНИЙ ОХВАТ 1 ПУБЛИКАЦИИ")
        err = calculate_err(reach, subscribers)
        # Сохраняем только публичные факты, не полный текст с именем аккаунта.
        result["observed_at"] = observed.isoformat()
        result["evidence"] = {"url": source_url, "country": country,
                              "subscribers": subscribers, "avg_post_reach": reach}
        candidate = {"Название": title.strip(), "Ссылка": "https://t.me/" + username,
                     "Подписчики": subscribers, "Средний охват": reach,
                     "ERR (%)": err, "Последнее обновление": observed.isoformat()}
        category = re.search(r"(?m)^[ \t]*Категория[ \t]*:?[ \t]*(?:\r?\n[ \t]*)*([^\r\n]+)", body)
        if category and category[1].strip():
            candidate["Тематика"] = category[1].strip()
        result["candidate"] = candidate
        if re.search(r"\bSCAM\b|\bFAKE\b|помечен как мошеннический|накрутка подписчиков", body, re.I):
            result["decision"] = "review"
            result["reasons"].append("На странице обнаружено предупреждение; нужна проверка.")
        elif subscribers < min_subscribers or reach < min_reach or not min_err <= err <= max_err:
            result["decision"] = "reject"
            result["reasons"].append("Метрики вне заданных фильтров.")
        else:
            result["decision"] = "accept"
        # Только подписи сотрудничества из верхней части карточки.
        header = body.split("Сводная", 1)[0]
        suggestions = re.findall(
            r"(?:Реклама|Сотрудничество)[^\n@]{0,40}\s*(@[A-Za-z][A-Za-z0-9_]{4,31})", header, re.I)
        result["contact_suggestions"] = sorted(set(suggestions))
        _check_placement(result, header)
        result["contact_suggestions"] = sorted(
            set(result["contact_suggestions"]) | set(result["placement"]["contacts"]))
    except (ValidationError, SourceError, ValueError) as error:
        result["reasons"].append(str(error))
    return result


# Читаем только видимый DOM. Ни cookies, ни localStorage, ни сетевые ответы.
_WEB_CAPTURE_JS = r"""() => {
    const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    const metricPattern = /ПОДПИСЧИКИ|СРЕДНИЙ\s+ОХВАТ\s+1\s+ПУБЛИКАЦИИ/;
    const metrics = [];
    for (const h of document.querySelectorAll('h2')) {
        if (!visible(h)) continue;
        let node = h.parentElement;
        for (let depth=0; node && depth<5; depth++, node=node.parentElement) {
            const label = node.innerText || '';
            if (label.length > 700) break;
            if (node.querySelectorAll('h2').length === 1 && metricPattern.test(label.replace(/\s+/g,' '))) {
                metrics.push({value:h.innerText, label});
                break;
            }
        }
    }
    const title = document.querySelector('h1');
    const body = document.body.innerText;
    const start = title ? Math.max(0, body.indexOf(title.innerText)) : 0;
    return {url:location.href, title:title && visible(title) ? title.innerText : '',
            text:body.slice(start,start+50000), metrics};
}"""


class WebCollectionError(SourceError):
    """Сбор прервался: уже прочитанные карточки доступны для dry-run."""

    def __init__(self, message, *, captures, failed_url):
        super().__init__(message)
        self.captures = captures
        self.failed_url = failed_url
        self.decisions = []


class TGStatWebSource(Source):
    """Обычный Chromium, ручной вход, временная сессия, без stealth.

    Не подключается к сессии встроенного браузера Codex или личному профилю.
    Для локальной проверки payload содержит заранее сохранённые captures.
    """

    def __init__(self, *, prompt=input, progress=None, sleep=None):
        import time
        self.prompt = prompt
        self.progress = progress or (lambda message: print(message, file=__import__('sys').stderr))
        self.sleep = sleep or time.sleep

    @staticmethod
    def _catalog_urls(page, *, limit, excluded=()):
        """Только основной список, без витрин и ссылок на публикации.

        Username — идентичность канала независимо от домена каталога.
        """
        from urllib.parse import urlsplit
        from playwright.sync_api import TimeoutError as BrowserTimeout

        root = page.locator("#category-list-form")
        try:
            root.wait_for(state="visible", timeout=15000)
        except BrowserTimeout as error:
            raise SourceError("Основной список категории TGStat не найден. Разметка могла измениться.") from error
        if root.count() != 1:
            raise SourceError("Основной список категории TGStat неоднозначен; сбор остановлен.")
        links = root.locator("a[href]").evaluate_all(
            "els => els.filter(e => e.getClientRects().length).map(e => e.href)")
        seen = {urlsplit(web_channel_url(url)).path.split("/")[2].casefold()
                for url in excluded}
        selected = []
        for link in links:
            try:
                url = web_channel_url(link)
            except (SourceError, ValueError):
                continue
            username = urlsplit(url).path.split("/")[2].casefold()
            if username in seen:
                continue
            seen.add(username)
            selected.append(url)
            if len(selected) >= limit:
                break
        return selected

    def collect(self, *, channels=None, category=None, countries=("ru", "by"),
                limit=5, payload=None, **filters):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise SourceError("Браузерный MVP ограничен 1–10 каналами за запуск.")
        if not countries or set(countries) - config.MVP_COUNTRIES:
            raise SourceError("Разрешены только ru/by.")
        for key, value in filters.items():
            if key in {"min_subscribers", "min_reach"}:
                validate_counter(value)
        if not 0 <= filters.get("min_err", config.TG_MIN_ERR) <= filters.get("max_err", config.TG_MAX_ERR) <= 100:
            raise SourceError("ERR должен быть в диапазоне 0–100.")
        if category is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", category):
            raise SourceError("Для браузера category — slug каталога, например tech.")
        urls = list(dict.fromkeys(web_channel_url(value) for value in (channels or [])))
        if payload is not None:
            if not isinstance(payload, dict) or not isinstance(payload.get("captures"), list):
                raise SourceError("Веб-выгрузка должна содержать массив captures.")
            captures = payload["captures"][:limit]
        else:
            if not urls and not category:
                raise SourceError("Укажите --channels @имя ... либо --category tech.")
            try:
                captures = self._browse(urls, category, tuple(dict.fromkeys(countries)), limit)
            except WebCollectionError as error:
                error.decisions = [web_channel_decision(capture, countries=countries, **filters)
                                   for capture in error.captures]
                for decision in error.decisions:
                    decision["mode"] = "browser"
                raise
        decisions = [web_channel_decision(capture, countries=countries, **filters) for capture in captures]
        for decision in decisions:
            decision["mode"] = "web-import" if payload is not None else "browser"
        return decisions

    def _browse(self, urls, category, countries, limit):
        import random
        try:
            from playwright.sync_api import sync_playwright, Error as BrowserError
        except ImportError as error:
            raise SourceError("Установите зависимости: python -m pip install -r requirements.txt") from error
        # Установленные движки хранятся в согласованном cache/ проекта.
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.CACHE_DIR / "ms-playwright"))
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                try:
                    context = browser.new_context(locale="ru-RU", accept_downloads=False)
                    page = context.new_page()
                    page.set_default_timeout(20000)
                    first = urls[0] if urls else ("https://by.tgstat.com/" if countries[0] == "by" else "https://tgstat.ru/") + category
                    self._visit(page, first)
                    self.progress("Открылось отдельное окно Chromium. Войдите в TGStat самостоятельно. Пароли и коды в терминал не вводите.")
                    self.progress("После входа вернитесь в терминал и нажмите Enter.")
                    self.prompt("")
                    # Человек мог перейти на другую страницу при входе.
                    self.sleep(random.uniform(*config.SOURCE_DELAY_SECONDS))
                    self._visit(page, first)
                    self._ensure_login(page, first)
                    if not urls:
                        for index, country in enumerate(countries):
                            origin = "https://tgstat.ru" if country == "ru" else "https://by.tgstat.com"
                            self.sleep(random.uniform(*config.SOURCE_DELAY_SECONDS))
                            self._visit(page, origin + "/" + category)
                            self._ensure_login(page, origin + "/" + category)
                            quota = max(1, math.ceil((limit-len(urls))/(len(countries)-index)))
                            urls.extend(self._catalog_urls(page, limit=quota, excluded=urls))
                            if len(urls) >= limit:
                                break
                    captures = []
                    for index, url in enumerate(urls[:limit], start=1):
                        self.progress(f"Чтение канала {index}/{min(len(urls), limit)}")
                        self.sleep(random.uniform(*config.SOURCE_DELAY_SECONDS))
                        try:
                            self._visit(page, url)
                            self._ensure_login(page, url)
                            page.wait_for_function("document.querySelectorAll('h2').length > 0", timeout=15000)
                            capture = page.evaluate(_WEB_CAPTURE_JS)
                            capture["observed_at"] = datetime.now(timezone.utc).isoformat()
                            captures.append(capture)
                        except (SourceError, BrowserError, EOFError, KeyboardInterrupt) as error:
                            message = str(error) if isinstance(error, SourceError) else "Чтение страницы прервано."
                            raise WebCollectionError(message, captures=captures, failed_url=url) from error
                    context.close()
                    return captures
                finally:
                    browser.close()
        except BrowserError as error:
            raise SourceError("Браузер не смог прочитать страницу. Если Chromium не установлен: PLAYWRIGHT_BROWSERS_PATH=cache/ms-playwright python -m playwright install chromium. Подробности запуска: README.md.") from error
        except (EOFError, KeyboardInterrupt) as error:
            raise SourceError("Вход или сбор остановлен оператором. Рабочие таблицы не изменены.") from error

    def _ensure_login(self, page, url):
        """Дожидается меню аккаунта; даёт завершить ручной вход на этом домене."""
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from urllib.parse import urlsplit

        try:
            page.locator("#topbar-userdrop").wait_for(state="visible", timeout=15000)
            return
        except BrowserTimeout:
            pass
        host = urlsplit(url).hostname
        self.progress(
            f"Вход на {host} пока не подтверждён. Окно Chromium остаётся открытым. "
            "Завершите вход именно в этом окне; вход в Safari или встроенном браузере не переносится. "
            "Если страница уже показывает статистику, дождитесь завершения авторизации."
        )
        self.progress("После входа вернитесь в терминал и нажмите Enter ещё раз.")
        self.prompt("")
        self._visit(page, url)
        try:
            page.locator("#topbar-userdrop").wait_for(state="visible", timeout=15000)
        except BrowserTimeout as error:
            raise SourceError(
                f"Вход на {host} не подтверждён после повторной проверки. "
                "В отдельном Chromium должно появиться меню вашего аккаунта. "
                "Вход в другом браузере не подходит. Рабочие таблицы не изменены."
            ) from error

    @staticmethod
    def _visit(page, url):
        from urllib.parse import urlsplit
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is not None and response.status >= 400:
            raise SourceError(f"Браузерный доступ ограничен: HTTP {response.status}, страница {url}. Сбор остановлен.")
        host = urlsplit(page.url).hostname
        if host not in {"tgstat.ru", "by.tgstat.com"}:
            raise SourceError("Сайт перенаправил на внешний адрес; сбор остановлен.")
        body = page.locator("body").inner_text(timeout=15000)
        if re.search(r"just a moment|verify you are human|подтвердите,? что вы человек|проверка браузера|captcha", page.title() + "\n" + body, re.I):
            raise SourceError("На странице проверка доступа. Автоматический обход отключён.")
