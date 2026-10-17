"""Локальное CSV-хранилище. Без сети, LLM и изменения ручных полей.

Один вызов upsert — одна транзакция. По умолчанию только dry-run.
Журнал prepared создаётся до замены, committed — после. При незавершённой
транзакции новые записи блокируются до ручной проверки бэкапа и журнала.
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
