"""Локальный оркестратор: список кандидатов -> защищённое хранилище.

Без OpenAI и сети. Dry-run моделирует последовательность в памяти:
следующие кандидаты видят изменения предыдущих, рабочие файлы не меняются.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from safe_writer import SafeWriter, StorageError
from validation import ValidationError, validate_all_schemas, validate_schema


class _PreviewWriter(SafeWriter):
    """Виртуальные таблицы поверх снимков существующего хранилища."""

    def __init__(self, source):
        super().__init__(source.root)
        self.tables = {}

    def _load(self, table):
        if table not in self.tables:
            self.tables[table] = super()._load(table)
        return deepcopy(self.tables[table])

    def upsert(self, table, candidate, *, dry_run=True, confirmed_fields=()):
        result, path, original, rows = self._plan(table, candidate, confirmed_fields)
        if result["action"] in {"add", "update"}:
            self.tables[table] = (path, original, rows)
        return result


def run_candidates(table, candidates, *, dry_run=True, writer=None):
    """Обрабатывает одну таблицу. Ошибка кандидата не останавливает список.

    Сбой хранилища останавливает запуск. Предыдущие операции уже могли быть
    сохранены: пакет не является одной общей транзакцией.
    Контакты автоматически не подтверждаются.
    """
    validate_all_schemas()
    validate_schema(table)
    if type(dry_run) is not bool:
        raise ValidationError("dry_run должен быть bool.")
    if not isinstance(candidates, list):
        raise ValidationError("Ожидается JSON-массив кандидатов.")
    storage = writer if writer is not None else SafeWriter()
    target = _PreviewWriter(storage) if dry_run else storage
    summary = dict(total=len(candidates), processed=0, added=0, updated=0,
                   skipped=0, rejected=0, failed=0, remaining=len(candidates))
    report = {"dry_run": dry_run, "status": "completed", "summary": summary, "results": []}
    counts = {"add": "added", "update": "updated", "skip": "skipped"}
    for index, candidate in enumerate(candidates, start=1):
        try:
            result = target.upsert(table, candidate, dry_run=dry_run)
        except ValidationError as error:
            result = {"action": "reject", "reason": str(error)}
            summary["rejected"] += 1
        except (StorageError, OSError) as error:
            result = {"action": "error", "reason": str(error),
                      "warning": "Операция могла частично завершиться. Проверьте журнал и бэкапы."}
            summary["failed"] += 1
            report["status"] = "stopped"
        else:
            summary[counts[result["action"]]] += 1
        report["results"].append({"candidate_index": index, **result})
        summary["processed"] += 1
        summary["remaining"] -= 1
        if report["status"] == "stopped":
            break
    return report


def demo_candidates():
    """Пример без реального поиска или подтверждённых площадок."""
    return [
        {"Название": "Демонстрационный канал", "Ссылка": "https://t.me/demo_channel",
         "Подписчики": 1000, "Средний охват": 250, "ERR (%)": 25.0},
        {"Ссылка": "https://t.me/demo_channel", "Тематика": "Игры"},
        {"Название": "Ошибка метрики", "Ссылка": "https://t.me/invalid_metrics",
         "Подписчики": -1},
        {"Название": "Нет адреса"},
    ]


def run_source(source_name, *, writer=None, dry_run=True, payload=None,
               proofs=None, query=None, limit=50, niche=None,
               countries=("ru", "by"), category=None, allow_unofficial=False,
               tg_filters=None, enrich_sites=False, channels=None, urls=None, interactive_login=False, discover=False):
    """Сначала решения источника, затем только принятые кандидаты -> ядро."""
    from wb_parser import WBSource, SourceError, country_code, enrich_website
    from tg_parser import TGStatSource

    if not countries or set(countries) - {"ru", "by"}:
        raise ValidationError("Разрешены только страны ru и by.")
    source_failure = None
    if source_name == "wb":
        decisions = WBSource().collect(payload=payload, proofs=proofs, query=query,
                                       limit=limit, niche=niche, allow_unofficial=allow_unofficial)
        table = "advertisers"
        for decision in decisions:
            country = country_code(decision["candidate"].get("Страна"))
            if country is not None and country not in countries:
                decision["decision"] = "reject"
                decision["reasons"].append("Страна не входит в фильтры запуска.")
            decision["mode"] = "import" if payload is not None else "live"
            if enrich_sites:
                evidence = decision.get("evidence", {})
                site_proof = evidence.get("website", {}) if isinstance(evidence, dict) else {}
                site = site_proof.get("value") if isinstance(site_proof, dict) else None
                brand = decision["candidate"].get("Название компании")
                if site and brand:
                    try:
                        decision["enrichment"] = enrich_website(brand, site)
                    except (SourceError, ValidationError) as error:
                        decision["enrichment"] = {"warning": str(error)}
                        if decision["decision"] == "accept":
                            decision["decision"] = "review"
                            decision["reasons"].append("Живая проверка сайта не выполнена.")
    elif source_name == "tgstat":
        decisions = TGStatSource().collect(payload=payload, query=query, limit=limit,
                                            countries=countries, category=category, **(tg_filters or {}))
        table = "telegram"
    elif source_name == "tgstat-web":
        from tg_parser import TGStatWebSource, WebCollectionError
        try:
            decisions = TGStatWebSource().collect(payload=payload, channels=channels,
                                                   category=category, limit=limit,
                                                   countries=countries, **(tg_filters or {}))
        except WebCollectionError as error:
            decisions = error.decisions
            source_failure = {"reason": str(error), "failed_url": error.failed_url,
                              "collected": len(decisions), "write_blocked": True}
        table = "telegram"
    elif source_name in {"wb-web", "ozon-web", "instagram-web", "youtube-web", "twitch-web", "kick-web"}:
        from importlib import import_module
        from wb_parser import BrowserBatchError
        module, class_name = {
            "wb-web": ("wb_parser", "WBWebSource"),
            "ozon-web": ("ozon_parser", "OzonWebSource"),
            "instagram-web": ("instagram_parser", "InstagramWebSource"),
            "youtube-web": ("youtube_parser", "YouTubeWebSource"),
            "twitch-web": ("twitch_parser", "TwitchWebSource"),
            "kick-web": ("kick_parser", "KickWebSource"),
        }[source_name]
        collector = getattr(import_module(module), class_name)()
        table = collector.table
        try:
            decisions = collector.collect(urls=urls, payload=payload, proofs=proofs,
                                          countries=countries, limit=limit, niche=niche,
                                          interactive_login=interactive_login, discover=discover)
        except BrowserBatchError as error:
            decisions = error.decisions
            source_failure = {"reason": str(error), "collected": len(decisions), "write_blocked": True}
    else:
        raise ValidationError("Неизвестный источник.")
    accepted = [(index, decision["candidate"]) for index, decision in enumerate(decisions)
                if decision["decision"] == "accept"]
    storage = writer if writer is not None else SafeWriter()
    report = run_candidates(table, [candidate for _, candidate in accepted],
                            dry_run=True if source_failure else dry_run, writer=storage)
    report["source"] = source_name
    report["source_summary"] = {state: sum(d["decision"] == state for d in decisions)
                                for state in ("accept", "review", "reject")}
    report["decisions"] = decisions
    for result in report["results"]:
        result["source_index"] = accepted[result["candidate_index"] - 1][0] + 1
    if source_failure:
        report["status"] = "stopped"
        report["source_status"] = "stopped"
        report["source_error"] = source_failure
        report["requested_dry_run"] = dry_run
    if not dry_run and not source_failure:
        storage.log_report(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальный оркестратор кандидатов")
    parser.add_argument("--table", choices=("advertisers", "telegram", "streamers"), default="telegram")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="Пример без записи")
    source.add_argument("--input", type=Path, help="JSON-массив кандидатов одной таблицы")
    source.add_argument("--source", choices=("wb", "tgstat", "tgstat-web", "wb-web", "ozon-web", "instagram-web", "youtube-web", "twitch-web", "kick-web"), help="Источник MVP с фильтром качества")
    parser.add_argument("--wb-file", type=Path, help="Сохранённый JSON-каталог WB")
    parser.add_argument("--tg-file", type=Path, help="Разрешённая выгрузка TGStat: info/stat")
    parser.add_argument("--web-file", type=Path, help="Сохранённые видимые снимки TGStat: captures")
    parser.add_argument("--discover", action="store_true", help="Найти карточки в публичном каталоге WB/Ozon/Twitch/Kick")
    parser.add_argument("--interactive-login", action="store_true", help="Разрешить паузу только при необходимом входе (новые браузерные источники)")
    parser.add_argument("--urls", nargs="+", help="Прямые ссылки для браузерных источников кроме TGStat")
    parser.add_argument("--channels", nargs="+", help="Браузер TGStat: @username или прямая карточка")
    parser.add_argument("--brand-proofs", type=Path, help="Проверенные факты по брендам")
    parser.add_argument("--query", help="Ключевое слово поиска; ниша бренда из него не выводится")
    parser.add_argument("--niche", help="Необязательный фильтр подтверждённой ниши WB")
    parser.add_argument("--countries", nargs="+", choices=("ru", "by"), default=["ru", "by"])
    parser.add_argument("--category", help="Категория TGStat")
    parser.add_argument("--limit", type=int, help="По умолчанию 50; браузер: 5, максимум 10")
    parser.add_argument("--allow-unofficial-wb", action="store_true", help="Разрешить попытку запроса WB без обхода защиты")
    parser.add_argument("--enrich-sites", action="store_true", help="WB: проверить заданные сайты и предложить контакты")
    parser.add_argument("--min-subscribers", type=int, help="TG: минимум подписчиков")
    parser.add_argument("--min-reach", type=int, help="TG: минимум среднего охвата")
    parser.add_argument("--min-err", type=float, help="TG: минимальный ERR")
    parser.add_argument("--max-err", type=float, help="TG: максимальный ERR, не выше 100")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Только план (по умолчанию)")
    mode.add_argument("--write", action="store_true", help="Реальная запись через safe_writer")
    args = parser.parse_args(argv)
    limit = args.limit if args.limit is not None else (5 if args.source and args.source.endswith("-web") else 50)
    if args.demo and (args.write or args.table != "telegram"):
        parser.error("Демонстрация разрешена только для Telegram без --write.")
    tg_filters = {name: getattr(args, name) for name in
                  ("min_subscribers", "min_reach", "min_err", "max_err")
                  if getattr(args, name) is not None}
    if not args.source and (args.wb_file or args.tg_file or args.brand_proofs or args.query
                            or args.niche or args.category or args.allow_unofficial_wb
                            or args.enrich_sites or tg_filters or args.web_file or args.channels or args.urls):
        parser.error("Параметры источника требуют --source.")
    browser_source = args.source and args.source.endswith("-web")
    generic_browser = browser_source and args.source != "tgstat-web"
    if args.web_file and not browser_source:
        parser.error("--web-file требует браузерный источник.")
    if args.channels and args.source != "tgstat-web":
        parser.error("--channels требует --source tgstat-web.")
    if args.discover and (args.source not in {"wb-web", "ozon-web", "twitch-web", "kick-web"} or args.urls or args.web_file):
        parser.error("--discover требует wb-web/ozon-web/twitch-web/kick-web и не смешивается с --urls/--web-file.")
    if args.interactive_login and not generic_browser:
        parser.error("--interactive-login поддерживается новыми браузерными источниками.")
    if args.urls and not generic_browser:
        parser.error("--urls требует wb/ozon/instagram/youtube/twitch/kick-web.")
    if generic_browser and (args.wb_file or args.tg_file or args.query or args.category
                            or args.allow_unofficial_wb or args.enrich_sites or tg_filters):
        parser.error("Браузерный источник принимает --urls либо --web-file, --brand-proofs и --countries.")
    if args.source == "wb" and (args.tg_file or args.category or tg_filters):
        parser.error("--tg-file и --category предназначены только для tgstat.")
    if args.source == "tgstat" and (args.wb_file or args.brand_proofs or args.niche
                                   or args.allow_unofficial_wb or args.enrich_sites):
        parser.error("Параметры WB не применяются к tgstat.")
    if args.source == "tgstat-web" and (args.wb_file or args.tg_file or args.brand_proofs
                                       or args.niche or args.allow_unofficial_wb or args.enrich_sites or args.query):
        parser.error("Для tgstat-web используйте --channels или --category; параметры API/WB не применяются.")
    if args.source == "tgstat-web" and args.channels and args.category:
        parser.error("Выберите либо --channels, либо --category.")
    if args.web_file and (args.channels or args.category or args.urls):
        parser.error("--web-file нельзя смешивать с живым сбором.")
    try:
        if args.source:
            from dotenv import load_dotenv
            import config
            from wb_parser import SourceError, read_json
            load_dotenv(config.PROJECT_DIR / ".env", override=False)
            payload_file = args.wb_file or args.tg_file or args.web_file
            payload = read_json(payload_file) if payload_file else None
            proofs = read_json(args.brand_proofs) if args.brand_proofs else None
            if args.write and isinstance(payload, dict) and payload.get("synthetic") is True:
                raise ValidationError("Тестовые данные synthetic запрещено писать в рабочие таблицы.")
            try:
                report = run_source(args.source, dry_run=not args.write, payload=payload,
                                    proofs=proofs, query=args.query, limit=limit,
                                    niche=args.niche, countries=args.countries, category=args.category,
                                    allow_unofficial=args.allow_unofficial_wb,
                                    tg_filters=tg_filters, enrich_sites=args.enrich_sites,
                                    channels=args.channels, urls=args.urls,
                                    interactive_login=args.interactive_login, discover=args.discover)
            except SourceError as error:
                parser.exit(1, f"Источник недоступен: {error}\n")
        else:
            candidates = (demo_candidates() if args.demo else
                          json.loads(args.input.read_text(encoding="utf-8")))
            report = run_candidates(args.table, candidates, dry_run=not args.write)
    except (ValidationError, StorageError, OSError, ValueError) as error:
        parser.exit(1, f"Запуск остановлен: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Отклонённые данные также отражаем в коде возврата для автоматизации.
    if report["status"] == "stopped":
        return 1
    return 2 if (report["summary"]["rejected"]
                 or report.get("source_summary", {}).get("reject", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
