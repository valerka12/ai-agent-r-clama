"""Локальное CSV-хранилище. Без сети, LLM и изменения ручных полей.

Один вызов upsert — одна транзакция. По умолчанию только dry-run.
Журнал prepared создаётся до замены, committed — после. При незавершённой
транзакции новые записи блокируются до ручной проверки бэкапа и журнала.

Это описание CSV-класса. Ниже добавлен отдельный сетевой адаптер Google Sheets
с добавлением только новых строк; существующие облачные ячейки не обновляются.
"""

import csv
import io
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config
from dedup import find_matches
from validation import (
    ValidationError, calculate_err, is_empty, validate_counter,
    validate_err, validate_headers, validate_platform, validate_schema,
    validate_url,
)


class StorageError(RuntimeError):
    """Запись остановлена для защиты данных."""


_SCHEMAS = {
    "advertisers": (config.ADVERTISER_COLUMNS, config.ADVERTISER_AUTO_FIELDS,
                    config.ADVERTISER_MANUAL_FIELDS, config.ADVERTISER_CONFIRMATION_FIELDS,
                    "advertisers.csv"),
    "telegram": (config.TELEGRAM_COLUMNS, config.TELEGRAM_AUTO_FIELDS,
                 config.TELEGRAM_MANUAL_FIELDS, config.TELEGRAM_CONFIRMATION_FIELDS,
                 "telegram.csv"),
    "streamers": (config.STREAMER_COLUMNS, config.STREAMER_AUTO_FIELDS,
                  config.STREAMER_MANUAL_FIELDS, config.STREAMER_CONFIRMATION_FIELDS,
                  "streamers.csv"),
}
_LOG_COLUMNS = ("transaction", "time_utc", "state", "table", "number",
                "field", "before", "after", "reason")
_COUNTERS = {"Номер", "Подписчики", "Средний охват", "Средний онлайн", "Пиковый онлайн"}
_PRICES = {"Стоимость 1/24", "Стоимость нативной"}


def _encode_csv(columns, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode(config.CSV_ENCODING)


def _read_csv(raw, columns):
    try:
        reader = csv.reader(io.StringIO(raw.decode(config.CSV_ENCODING), newline=""), strict=True)
        header = next(reader)
        if tuple(header) != tuple(columns):
            raise StorageError("Заголовки CSV не соответствуют схеме.")
        rows = []
        for values in reader:
            if len(values) != len(columns):
                raise StorageError("Неверное число ячеек в CSV. Нужна ручная проверка.")
            rows.append(dict(zip(columns, values)))
        return rows
    except (UnicodeError, csv.Error, StopIteration) as error:
        raise StorageError("CSV повреждён или имеет неверную кодировку.") from error


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_value(field, value):
    if field in _COUNTERS:
        validate_counter(value, positive=field == "Номер")
    elif field == "ERR (%)":
        validate_err(value)
    elif field == "Платформа (Twitch/Kick)":
        validate_platform(value)
    elif field in {"Сайт", "Ссылка"}:
        validate_url(value)
    elif field in _PRICES:
        import math
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value < 0 or not math.isfinite(value)):
            raise ValidationError(f"{field}: требуется конечное неотрицательное число.")
    elif not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}: требуется непустая строка.")
    if isinstance(value, str):
        if "\x00" in value or value.lstrip().startswith(("=", "+", "-")):
            raise ValidationError(f"{field}: управляющее значение или формула запрещены.")
        if field == "Последнее обновление":
            try:
                timestamp = datetime.fromisoformat(value)
            except ValueError as error:
                raise ValidationError("Дата обновления должна быть в ISO 8601.") from error
            if timestamp.tzinfo is None:
                raise ValidationError("Дата обновления должна содержать часовой пояс.")


class SafeWriter:
    """Корень можно заменить только для изолированных тестов.

    confirmed_fields передаёт доверенный CLI после подтверждения оператором.
    Этот параметр нельзя отдавать под управление будущего LLM-инструмента.
    """

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else config.PROJECT_DIR

    def _schema(self, table):
        validate_schema(table)
        return _SCHEMAS[table]

    def _load(self, table):
        columns, *_, filename = self._schema(table)
        path = self.root / "data" / filename
        if path.is_symlink():
            raise StorageError("Символические ссылки на таблицы не поддерживаются.")
        raw = path.read_bytes() if path.exists() else None
        rows = _read_csv(raw, columns) if raw is not None else []
        validate_headers(table, columns)
        numbers = [row["Номер"] for row in rows]
        if any(not number.isascii() or not number.isdigit() or int(number) < 1 for number in numbers):
            raise StorageError("Некорректные номера существующих строк.")
        if len({int(number) for number in numbers}) != len(numbers):
            raise StorageError("Повторяющиеся номера существующих строк.")
        if table == "advertisers":
            identifiers = [row["ID клиента"] for row in rows if row["ID клиента"]]
            if len(set(identifiers)) != len(identifiers):
                raise StorageError("Повторяющиеся ID клиентов. Нужна ручная проверка.")
        return path, raw, rows

    def read(self, table):
        """Возвращает строки без исправления существующих значений."""
        return self._load(table)[2]

    def log_report(self, report):
        """Прозрачность отбора источников. Вызывать только при --write."""
        self._log_message({"kind": "source_run", **report})

    def upsert(self, table, candidate, *, dry_run=True, confirmed_fields=()):
        """Добавляет строку либо заполняет пустые разрешённые ячейки.

        Результат содержит action, changes и warnings. Никаких удалений.
        dry-run не создаёт даже каталоги, журналы или блокировку.
        """
        if type(dry_run) is not bool:
            raise ValidationError("dry_run должен быть bool.")
        if dry_run:
            return self._plan(table, candidate, confirmed_fields)[0]
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".safe_writer.lock"
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise StorageError("Есть блокировка писателя. Нужна проверка активного процесса.") from error
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(str(os.getpid()))
            self._check_journal()
            # Снимки всех существующих таблиц до планирования реальной операции.
            transaction = uuid.uuid4().hex
            backup = self.root / "backups" / transaction
            for name in _SCHEMAS:
                source, raw, _ = self._load(name)
                columns = _SCHEMAS[name][0]
                _atomic_write(backup / source.name,
                              raw if raw is not None else _encode_csv(columns, []))
            result, path, original, rows = self._plan(table, candidate, confirmed_fields)
            self._log_message(result)
            if result["action"] not in {"add", "update"}:
                return result
            columns = _SCHEMAS[table][0]
            self._journal(transaction, "prepared", table, result)
            # Обнаруживаем внешнее изменение после чтения. При конфликте не пишем.
            current = path.read_bytes() if path.exists() else None
            if current != original:
                self._journal(transaction, "aborted", table, result)
                raise StorageError("Таблица изменилась во время операции. Запись отменена.")
            _atomic_write(path, _encode_csv(columns, rows))
            self._journal(transaction, "committed", table, result)
            return result
        finally:
            lock.unlink()

    def _plan(self, table, candidate, confirmed_fields):
        columns, auto, manual, confirmation, _ = self._schema(table)
        if not isinstance(candidate, dict):
            raise ValidationError("Кандидат должен быть словарём.")
        if set(candidate) - set(columns):
            raise ValidationError("Кандидат содержит неизвестные колонки.")
        if isinstance(confirmed_fields, (str, bytes)):
            raise ValidationError("Подтверждения должны быть коллекцией имён полей.")
        confirmed = frozenset(confirmed_fields)
        if confirmed - confirmation:
            raise ValidationError("Подтверждать можно только специально выделенные поля.")
        path, original, rows = self._load(table)
        result = {"action": "skip", "number": None, "changes": [], "warnings": []}
        matches = find_matches(table, candidate, rows)
        if matches["key"] is None:
            result["warnings"].append("Недостаточно данных для ключа. Нужна проверка.")
            return result, path, original, rows
        if len(matches["exact"]) > 1 or matches["review"]:
            result["warnings"].append("Неоднозначные или похожие записи. Нужна проверка.")
            return result, path, original, rows
        adding = not matches["exact"]
        row = {field: "" for field in columns} if adding else rows[matches["exact"][0]].copy()
        for field, value in candidate.items():
            if is_empty(value):
                continue
            if field in {"Номер", "ID клиента"}:
                result["warnings"].append(f"{field}: назначается хранилищем, пропущено.")
                continue
            if field in manual:
                result["warnings"].append(f"{field}: ручное поле, пропущено.")
                continue
            if field in confirmation and field not in confirmed:
                result["warnings"].append(f"{field}: нет подтверждения, пропущено.")
                continue
            if not is_empty(row[field]):
                result["warnings"].append(f"{field}: заполненная ячейка сохранена.")
                continue
            if field not in auto | confirmation:
                raise StorageError("Нет разрешения на запись поля.")
            _validate_value(field, value)
            row[field] = str(value)
            result["changes"].append({"field": field, "before": "", "after": str(value)})
        if adding:
            number = str(max((int(item["Номер"]) for item in rows), default=0) + 1)
            row["Номер"] = number
            result["changes"].insert(0, {"field": "Номер", "before": "", "after": number})
            if table == "advertisers":
                row["ID клиента"] = uuid.uuid4().hex
                result["changes"].append({"field": "ID клиента", "before": "", "after": row["ID клиента"]})
        # Проверяем согласованность ERR только при изменении связанных полей.
        changed = {item["field"] for item in result["changes"]}
        if table == "telegram" and changed & {"Подписчики", "Средний охват", "ERR (%)"}:
            if row["Подписчики"] and row["Средний охват"]:
                try:
                    subscribers = int(row["Подписчики"])
                    reach = int(row["Средний охват"])
                    expected = calculate_err(reach, subscribers)
                    if row["ERR (%)"]:
                        validate_err(float(row["ERR (%)"]))
                        if abs(float(row["ERR (%)"]) - expected) > 0.005:
                            raise ValidationError("ERR не соответствует охвату и подписчикам.")
                except ValueError as error:
                    raise ValidationError("Метрики Telegram требуют проверки.") from error
            elif "ERR (%)" in changed:
                raise ValidationError("Для записи ERR нужны охват и число подписчиков.")
        result["number"] = row["Номер"]
        if result["changes"]:
            result["action"] = "add" if adding else "update"
            if adding:
                rows.append(row)
            else:
                rows[matches["exact"][0]] = row
        return result, path, original, rows

    def _check_journal(self):
        path = self.root / "logs" / "changes_log.csv"
        if not path.exists():
            return
        entries = _read_csv(path.read_bytes(), _LOG_COLUMNS)
        states = {}
        for entry in entries:
            states[entry["transaction"]] = entry["state"]
        if any(state not in {"committed", "aborted"} for state in states.values()):
            raise StorageError("Незавершённая транзакция. Проверьте журнал и бэкапы вручную.")

    def _journal(self, transaction, state, table, result):
        path = self.root / "logs" / "changes_log.csv"
        entries = _read_csv(path.read_bytes(), _LOG_COLUMNS) if path.exists() else []
        now = datetime.now(timezone.utc).isoformat()
        for change in result["changes"]:
            entries.append(dict(transaction=transaction, time_utc=now, state=state,
                                table=table, number=result["number"], reason=result["action"], **change))
        _atomic_write(path, _encode_csv(_LOG_COLUMNS, entries))

    def _log_message(self, result):
        path = self.root / "logs" / "agent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger(f"safe_writer.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.info(json.dumps(result, ensure_ascii=False))
        finally:
            logger.removeHandler(handler)
            handler.close()


# Google Sheets: отдельный адаптер. Реализация CSV выше остаётся прежней.
class GoogleRequestError(StorageError):
    def __init__(self, status=None):
        self.status = status
        super().__init__('Google Sheets: проверьте доступ, включение API и квоту.' if status in (401,403,429)
                         else 'Google Sheets не подтвердил операцию. Проверьте таблицу и журнал; автоматического повтора нет.')


class GoogleSheetsStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else config.PROJECT_DIR
        self.directory = (self.root/'cache' if root is not None else config.CACHE_DIR)/'google_sheets'

    def _path(self, name):
        path = self.directory/name
        if self.directory.is_symlink() or self.directory.parent.is_symlink() or path.is_symlink():
            raise StorageError('Символические ссылки в настройках Google запрещены.')
        return path

    def _json(self, name, limit=64000):
        path = self._path(name)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > limit:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise StorageError('Настройки Google повреждены или недоступны. Автоматическая запись остановлена.') from None

    @staticmethod
    def parse_url(value):
        import re
        from urllib.parse import urlsplit, parse_qs
        try:
            validate_url(value)
            p = urlsplit(value)
            match = re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{20,200})(?:/(?:edit|view|preview))?/?',p.path)
            if p.scheme != 'https' or p.hostname != 'docs.google.com' or p.port is not None or not match:
                raise ValueError()
            ids = parse_qs(p.query).get('gid',[]) + parse_qs(p.fragment).get('gid',[])
            if ids and (len(set(ids)) != 1 or not ids[0].isascii() or not ids[0].isdigit()):
                raise ValueError()
            return {'spreadsheet_id':match[1], 'sheet_id':int(ids[0]) if ids else None}
        except (ValueError, TypeError, ValidationError):
            raise ValidationError('Нужна ссылка https://docs.google.com/spreadsheets/d/…/edit#gid=…') from None

    def bindings(self):
        import re
        data = self._json('bindings.json')
        if data is None:
            return {}
        links = data.get('links')
        if data.get('version') != 1 or not isinstance(links, dict) or set(links)-set(_SCHEMAS):
            raise StorageError('Некорректные подключения Google Sheets.')
        for table, link in links.items():
            if (not isinstance(link,dict) or not isinstance(link.get('spreadsheet_id'),str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}',link['spreadsheet_id'])
                    or type(link.get('sheet_id')) is not int or link['sheet_id'] < 0
                    or not isinstance(link.get('sheet_title'),str)):
                raise StorageError('Некорректная привязка листа Google Sheets.')
        return links

    def credentials(self):
        data = self._json('service_account.json',12000)
        if (not data or data.get('type') != 'service_account'
                or not isinstance(data.get('client_email'),str) or not data['client_email'].endswith('.gserviceaccount.com')
                or data.get('token_uri') != 'https://oauth2.googleapis.com/token'
                or data.get('universe_domain','googleapis.com') != 'googleapis.com'):
            raise StorageError('Выберите JSON-ключ служебного аккаунта Google в настройках приложения.')
        return data

    def save_credentials(self, data):
        if (not isinstance(data,dict) or data.get('type') != 'service_account'
                or not isinstance(data.get('client_email'),str) or not data['client_email'].endswith('.gserviceaccount.com')
                or data.get('token_uri') != 'https://oauth2.googleapis.com/token'
                or data.get('universe_domain','googleapis.com') != 'googleapis.com'):
            raise ValidationError('Нужен JSON-ключ служебного аккаунта Google, не пароль или API-ключ.')
        try:
            from google.oauth2.service_account import Credentials
            Credentials.from_service_account_info(data,scopes=['https://www.googleapis.com/auth/spreadsheets'])
            raw = json.dumps(data).encode('utf-8')
            if len(raw)>12000:
                raise ValueError()
        except Exception:
            raise ValidationError('Ключ Google не удалось проверить. Выберите исходный JSON-файл; содержимое не выводится.') from None
        path=self._path('service_account.json')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.name=='posix':
            path.parent.chmod(0o700)
        _atomic_write(path,raw)
        return self.status()

    def status(self):
        key = self._json('service_account.json',12000)
        return {'configured':bool(key), 'email':key.get('client_email','') if key else '', 'links':self.bindings()}

    def attach(self, url, expected=None, replace=False, client=None):
        link = self.parse_url(url)
        api = client or GoogleSheetsAPI(self.credentials())
        snapshot = api.snapshot(link,expected)
        table = snapshot['table']
        links = self.bindings()
        binding = {key:snapshot[key] for key in ('spreadsheet_id','sheet_id','sheet_title')}
        if table in links and links[table] != binding and replace is not True:
            raise ValidationError('Лист этого типа уже подключён. Для замены отметьте соответствующий флажок.')
        links[table] = binding
        _atomic_write(self._path('bindings.json'),json.dumps({'version':1,'links':links},ensure_ascii=False).encode('utf-8'))
        return {'table':table,'binding':binding,'columns':snapshot['mapping'],'mode':'append-only'}


def _google_column(index):
    result = ''
    index += 1
    while index:
        index,remainder = divmod(index-1,26)
        result = chr(65+remainder)+result
    return result


# Явно согласованный внешний шаблон. Внутренние 22 колонки не меняются.
_ADVERTISER_EXTERNAL_HEADERS = (
    '№', 'ID клиента', 'Название компании', 'Ниша', 'Контактное лицо',
    'Должность', 'Тelegram', 'Email', 'Сайт', 'Соцсети', 'Cтрана',
    'Бюджет на рекламу', 'Формат (TG/Twitch)', 'Целевая аудитория',
    'Рекламируемый продукт', 'Предыдущий опыт рекламы',
    'Результаты прошлых размещений', 'Готовность к тесту',
    'Дата первого контакта', 'Статус клиента', 'Дата последнего контакт',
    'Менеджер (кто пишет)', 'Общая сумма оплат', 'Комментарий',
    'Рейтинг клиента (A/B/C)',
)
_ADVERTISER_EXTERNAL_ALIASES = {
    'Номер': '№', 'Страна': 'Cтрана',
    'Формат (TG/Twitch/Kick)': 'Формат (TG/Twitch)',
    'Предыдущий опыт': 'Предыдущий опыт рекламы',
    'Общая сумма оплаты': 'Общая сумма оплат',
}


def _google_snapshot(binding, title, raw, effective, *, formatted=None, formats=None):
    import hashlib,re,math
    if (not isinstance(raw,list) or not isinstance(effective,list) or not raw or not effective
            or len(raw)>10000 or len(effective)>10000 or not isinstance(effective[0],list)):
        raise ValidationError('Лист пуст, слишком велик или имеет неизвестную структуру.')
    header = effective[0]
    if any(not isinstance(value,str) for value in header):
        raise ValidationError('Заголовки Google Sheets должны быть текстом.')
    names = [value for value in header if value != '']
    if len(names) != len(set(names)):
        raise ValidationError('В Google Sheets повторяются заголовки. Запись запрещена.')
    matches = [name for name,schema in _SCHEMAS.items() if set(schema[0]) == set(names)]
    external_advertisers = set(names) == set(_ADVERTISER_EXTERNAL_HEADERS)
    if len(matches) != 1 and not external_advertisers:
        raise ValidationError('Заголовки листа не соответствуют согласованной схеме или поддерживаемому шаблону рекламодателей. Названия автоматически не меняются.')
    table = 'advertisers' if external_advertisers else matches[0]
    columns,auto,manual,confirmation,_ = _SCHEMAS[table]
    positions = ({field:header.index(_ADVERTISER_EXTERNAL_ALIASES.get(field,field))
                  for field in columns if _ADVERTISER_EXTERNAL_ALIASES.get(field,field) in names}
                 if external_advertisers else {name:header.index(name) for name in names})
    def text(value):
        if isinstance(value,bool):
            return 'TRUE' if value else 'FALSE'
        if isinstance(value,float) and math.isfinite(value) and value.is_integer():
            return str(int(value))
        return '' if value is None else str(value)
    rows,display = [],[]
    for i,values in enumerate(effective[1:],start=1):
        if not isinstance(values,list) or any(v not in ('',None) and (j>=len(header) or header[j]=='') for j,v in enumerate(values)):
            raise ValidationError('На листе есть данные в колонках без заголовков. Нужна проверка структуры.')
        row = {field:text(values[positions[field]]) if field in positions and positions[field]<len(values) else '' for field in columns}
        if not any(value not in ('',None) for value in values):
            continue
        rows.append(row)
        view = formatted[i] if isinstance(formatted,list) and i<len(formatted) else values
        display.append({field:text(view[positions[field]]) if field in positions and positions[field]<len(view) else '' for field in columns})
    numbers = [row['Номер'] for row in rows]
    if any(not number.isascii() or not number.isdigit() or int(number)<1 for number in numbers) or len(set(map(int,numbers))) != len(numbers):
        raise ValidationError('Существующие строки должны иметь уникальные положительные номера. Ячейки автоматически не исправляются.')
    if table == 'advertisers':
        ids = [row['ID клиента'] for row in rows if row['ID клиента']]
        if len(ids) != len(set(ids)):
            raise ValidationError('Повторяются ID клиентов. Нужна ручная проверка.')
    spill = any(isinstance(v,str) and v.startswith('=') and
                (re.search(r'\b(?:ARRAYFORMULA|FILTER|QUERY|SORT|UNIQUE|SEQUENCE|TRANSPOSE|IMPORTRANGE|MAP|BYROW|BYCOL|SCAN|MAKEARRAY)\s*\(',v,re.I) or '{' in v)
                for row in raw if isinstance(row,list) for v in row)
    digest = hashlib.sha256(json.dumps([binding['spreadsheet_id'],binding['sheet_id'],raw,effective],ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    return {**binding,'sheet_title':title,'table':table,'rows':rows,'display_rows':display,'raw':raw,
            'positions':positions,'column_count':len(header),'next_row':max(len(raw),len(effective))+1,'fingerprint':digest,
            'spill_formula':bool(spill),'formats':formats or {},
            'mapping':[{'field':field,'column':_google_column(positions[field]) if field in positions else '—',
                        'mode':'unavailable' if field not in positions else 'manual' if field in manual else 'confirm' if field in confirmation else 'auto-new-row'} for field in columns]}


class GoogleSheetsAPI:
    def __init__(self, info):
        try:
            from google.oauth2.service_account import Credentials
            from google.auth.transport.requests import AuthorizedSession
            creds = Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets'])
            self.session = AuthorizedSession(creds,max_refresh_attempts=1)
        except Exception:
            raise StorageError('Не удалось подготовить авторизацию Google. Проверьте JSON-ключ.') from None
        self.last_request = 0.0

    def _call(self, method, spreadsheet, suffix='', *, body=None, params=None):
        import time
        delay = 1.1-(time.monotonic()-self.last_request)
        if delay>0:
            time.sleep(delay)
        self.last_request=time.monotonic()
        try:
            with self.session.request(method,'https://sheets.googleapis.com/v4/spreadsheets/'+spreadsheet+suffix,
                                      json=body,params=params,timeout=(5,20),allow_redirects=False,stream=True) as response:
                if response.status_code != 200:
                    raise GoogleRequestError(response.status_code)
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>2_000_000:
                        raise StorageError('Ответ Google превышает лимит 2 МБ. Запись остановлена.')
                    chunks.append(chunk)
                value=json.loads(b''.join(chunks))
                if not isinstance(value,dict):
                    raise ValueError()
                return value
        except (GoogleRequestError,StorageError):
            raise
        except Exception:
            raise GoogleRequestError() from None

    def snapshot(self, binding, expected=None, *, display=False):
        try:
            return self._snapshot(binding,expected,display=display)
        except (StorageError,ValidationError):
            raise
        except Exception:
            raise StorageError('Неизвестная структура ответа Google. Запись не выполнялась.') from None

    def _snapshot(self, binding, expected=None, *, display=False):
        spreadsheet=binding['spreadsheet_id']
        meta=self._call('GET',spreadsheet,params={'fields':'spreadsheetId,sheets(properties)'})
        properties=[sheet.get('properties',{}) for sheet in meta.get('sheets',[])]
        selected=next((p for p in properties if p.get('sheetId')==binding.get('sheet_id')),None) if binding.get('sheet_id') is not None else next(iter(properties),None)
        if not selected or selected.get('sheetType','GRID')!='GRID':
            raise ValidationError('Выберите обычный лист Google Sheets по ссылке с #gid=…')
        link={'spreadsheet_id':spreadsheet,'sheet_id':selected['sheetId']}
        def values(mode):
            data=self._call('POST',spreadsheet,'/values:batchGetByDataFilter',body={
                'dataFilters':[{'gridRange':{'sheetId':link['sheet_id']}}],
                'majorDimension':'ROWS','valueRenderOption':mode})
            ranges=data.get('valueRanges',[])
            if len(ranges)!=1:
                raise StorageError('Google вернул неоднозначный диапазон.')
            return ranges[0].get('valueRange',{}).get('values',[])
        raw,effective=values('FORMULA'),values('UNFORMATTED_VALUE')
        result=_google_snapshot(link,selected['title'],raw,effective,
                                formatted=values('FORMATTED_VALUE') if display else None)
        if expected is not None and result['table'] != expected:
            raise ValidationError('Заголовки относятся к другому типу таблицы. Проверьте выбранный лист и gid.')
        grid=selected.get('gridProperties',{})
        if result['next_row']<=grid.get('rowCount',0):
            start=result['next_row']-1
            data=self._call('POST',spreadsheet,':getByDataFilter',params={
                'fields':'sheets(data(rowData(values(effectiveFormat(numberFormat)))))'},body={
                'dataFilters':[{'gridRange':{'sheetId':link['sheet_id'],'startRowIndex':start,'endRowIndex':start+1,
                                            'startColumnIndex':0,'endColumnIndex':max(result['positions'].values())+1}}],
                'includeGridData':True})
            try:
                cells=data['sheets'][0]['data'][0]['rowData'][0]['values']
                result['formats']={i:c.get('effectiveFormat',{}).get('numberFormat',{}).get('type') for i,c in enumerate(cells)}
            except (KeyError,IndexError,TypeError):
                pass
        return result

    def append(self, binding, cells):
        return self._call('POST',binding['spreadsheet_id'],':batchUpdate',body={
            'requests':[{'appendCells':{'sheetId':binding['sheet_id'],'rows':[{'values':cells}],
                                       'fields':'userEnteredValue'}}]})


class GoogleSheetWriter(SafeWriter):
    """Только новые строки. Нет операций обновления, удаления и очистки Google."""
    def __init__(self, root=None, client=None):
        super().__init__(root)
        self.store=GoogleSheetsStore(root)
        self.links=self.store.bindings()
        self.client=client
        self.snapshots={}
        self.virtual_adds={}

    def _api(self):
        if self.client is None:
            self.client=GoogleSheetsAPI(self.store.credentials())
        return self.client

    def _load(self, table):
        if table not in self.links:
            return super()._load(table)
        snapshot=self._api().snapshot(self.links[table],table)
        self.snapshots[table]=snapshot
        return self.root/'data'/self._schema(table)[-1],_encode_csv(self._schema(table)[0],snapshot['rows']),snapshot['rows']

    def read(self, table):
        if table not in self.links:
            return super().read(table)
        return self._api().snapshot(self.links[table],table,display=True)['display_rows']

    def precheck(self, table, candidate, rows):
        if table not in self.links:
            return None
        if not isinstance(candidate,dict) or set(candidate)-set(self._schema(table)[0]):
            raise ValidationError('Некорректные поля кандидата.')
        matches=find_matches(table,candidate,rows)
        if matches['key'] is None or matches['exact'] or matches['review']:
            return {'action':'skip','number':rows[matches['exact'][0]]['Номер'] if len(matches['exact'])==1 else None,
                    'changes':[],'warnings':['Google Sheets: существующие и неоднозначные строки не изменяются. Разрешено только добавление новых.']}
        return None

    def filter_plan(self, table, result, *, virtual=False):
        if table not in self.links:
            return result
        if result['action']=='update':
            return {**result,'action':'skip','changes':[],
                    'warnings':result['warnings']+['Google Sheets: обновление существующих строк отключено.']}
        if result['action']=='add':
            snapshot=self.snapshots[table]
            if snapshot['spill_formula']:
                raise ValidationError('На листе обнаружены массивные/разливающиеся формулы. Автоматическое добавление остановлено до ручной проверки.')
            if any(change['field'] not in snapshot['positions'] for change in result['changes']):
                raise ValidationError('Для поля нет колонки в подключённом листе. Запись остановлена.')
            row=snapshot['next_row']+(self.virtual_adds.get(table,0) if virtual else 0)
            result['target']={'type':'google_sheets','spreadsheet_id':snapshot['spreadsheet_id'],
                              'sheet_id':snapshot['sheet_id'],'sheet_title':snapshot['sheet_title'],'row':row,
                              'cells':[{'field':c['field'],'cell':_google_column(snapshot['positions'][c['field']])+str(row)} for c in result['changes']]}
            if virtual:
                self.virtual_adds[table]=self.virtual_adds.get(table,0)+1
        return result

    def upsert(self, table, candidate, *, dry_run=True, confirmed_fields=()):
        if table not in self.links:
            return super().upsert(table,candidate,dry_run=dry_run,confirmed_fields=confirmed_fields)
        if type(dry_run) is not bool:
            raise ValidationError('dry_run должен быть bool.')
        if dry_run:
            _,_,existing=self._load(table)
            skipped=self.precheck(table,candidate,existing)
            return skipped if skipped else self.filter_plan(table,self._plan(table,candidate,confirmed_fields)[0])
        self.root.mkdir(parents=True,exist_ok=True)
        lock=self.root/'.safe_writer.lock'
        try:
            descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            raise StorageError('Есть блокировка писателя. Проверьте активный процесс.') from None
        os.close(descriptor)
        try:
            self._check_journal()
            if self.store.bindings().get(table)!=self.links[table]:
                raise StorageError('Подключение листа изменилось. Запустите проверку заново.')
            _,_,existing=self._load(table)
            skipped=self.precheck(table,candidate,existing)
            if skipped:
                return skipped
            result,_,_,_=self._plan(table,candidate,confirmed_fields)
            result=self.filter_plan(table,result)
            if result['action']!='add':
                return result
            planned=self.snapshots[table]
            backup=self.root/'backups'/('google-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'-'+uuid.uuid4().hex[:8])
            for name in _SCHEMAS:
                _,raw,_=self._load(name)
                if name in self.links:
                    output=io.StringIO(newline='')
                    csv.writer(output).writerows(self.snapshots[name]['raw'])
                    raw=output.getvalue().encode(config.CSV_ENCODING)
                _atomic_write(backup/self._schema(name)[-1],raw if raw is not None else _encode_csv(self._schema(name)[0],[]))
            _atomic_write(backup/'google-target.json',json.dumps(result['target'],ensure_ascii=False).encode('utf-8'))
            current=self._api().snapshot(self.links[table],table)
            if current['fingerprint']!=planned['fingerprint'] or current['formats']!=planned['formats']:
                raise StorageError('Лист изменился после планирования. Запись отменена, запросите новый план.')
            cells=[{} for _ in range(planned['column_count'])]
            numeric=_COUNTERS|_PRICES|{'ERR (%)'}
            for change in result['changes']:
                field,value=change['field'],change['after']
                index=planned['positions'][field]
                if field in numeric:
                    number=float(value)
                    if abs(number)>2**53:
                        raise ValidationError('Число слишком велико для точной записи Google Sheets.')
                    if field=='ERR (%)' and planned['formats'].get(index)=='PERCENT':
                        number/=100
                    cells[index]={'userEnteredValue':{'numberValue':number}}
                else:
                    cells[index]={'userEnteredValue':{'stringValue':str(value)}}
            transaction=uuid.uuid4().hex
            self._log_message({'kind':'google_append','target':result['target'],'plan':result})
            self._journal(transaction,'prepared',table,result)
            try:
                self._api().append(planned,cells)
            except GoogleRequestError as error:
                if error.status in (400,401,403,404,409,429):
                    self._journal(transaction,'aborted',table,result)
                raise
            self._journal(transaction,'committed',table,result)
            result['warnings'].append('Google добавил строку после последней строки с данными. Номер A1 в плане относится к проверенному снимку; параллельные изменения могут сдвинуть его.')
            return result
        finally:
            lock.unlink()


def make_writer(root=None, *, google=False, client=None):
    if google and GoogleSheetsStore(root).bindings():
        return GoogleSheetWriter(root,client=client)
    return SafeWriter(root)


def default_writer():
    if os.environ.get('ADRESEARCH_GOOGLE_OUTPUT')!='1':
        return SafeWriter()
    import hashlib
    expected=os.environ.get('ADRESEARCH_GOOGLE_TARGET')
    table=os.environ.get('ADRESEARCH_GOOGLE_TABLE')
    if expected:
        binding=GoogleSheetsStore().bindings().get(table)
        actual=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
        if actual!=expected:
            raise StorageError('Подключённый лист изменился после запуска задания. Автоматическая запись остановлена.')
    return make_writer(google=True)


def main():
    """CLI до подключения агента: JSON -> план либо явная запись."""
    import argparse

    parser = argparse.ArgumentParser(description="Безопасная запись кандидата в CSV")
    parser.add_argument("--table", choices=tuple(_SCHEMAS), default="telegram")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON-файл с одной записью")
    source.add_argument("--demo", action="store_true", help="Встроенный пример, только dry-run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Только план (по умолчанию)")
    mode.add_argument("--write", action="store_true", help="Разрешить реальную запись")
    parser.add_argument("--confirm-contacts", action="store_true",
                        help="Запросить подтверждение контактных полей через терминал")
    args = parser.parse_args()
    if args.demo and (args.write or args.table != "telegram"):
        parser.error("Демонстрация разрешена только для Telegram и без --write.")
    try:
        candidate = ({"Название": "Демонстрационный канал",
                      "Ссылка": "https://t.me/demo_channel", "Подписчики": 1000,
                      "Средний охват": 250, "ERR (%)": 25.0}
                     if args.demo else json.loads(args.input.read_text(encoding="utf-8")))
        confirmed = set()
        if args.confirm_contacts:
            if not isinstance(candidate, dict):
                raise ValidationError("В JSON ожидается объект с названиями колонок.")
            for field in sorted(_SCHEMAS[args.table][3]):
                if not is_empty(candidate.get(field)):
                    print(f"Подтвердить поле {field}: {candidate[field]!r}")
                    if input("Введите ДА для подтверждения: ").strip() == "ДА":
                        confirmed.add(field)
        result = SafeWriter().upsert(args.table, candidate, dry_run=not args.write,
                                     confirmed_fields=confirmed)
        print(json.dumps({"dry_run": not args.write, **result}, ensure_ascii=False, indent=2))
    except (ValidationError, StorageError, OSError, EOFError, ValueError) as error:
        parser.exit(1, f"Операция остановлена: {error}\n")


if __name__ == "__main__":
    main()
