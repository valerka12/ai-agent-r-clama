"""Контракты источников, отказ сети и полный путь на временных таблицах."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib

import httpx

from agent import main, run_source
from safe_writer import SafeWriter
from tg_parser import TGStatSource, channel_decision
from wb_parser import (SourceError, SourceHTTP, WBSource, assess_brand,
                       country_code, enrich_website)


def proof(value, now):
    return dict(value=value, url="https://example.com/proof", checked_at=now.isoformat(), operator_verified=True)


def brand_data():
    now = datetime.now(timezone.utc)
    values = dict(brand="Example Brand", country="Россия", website="https://example.com",
                  telegram="https://t.me/example_brand", contact="public@example.com",
                  last_activity=(now-timedelta(days=3)).isoformat(), advertising=True,
                  niche="Технологии", account_age_days=365, followers=5000, agency=False)
    return ({"brand": "Example Brand", "name": "Товар", "reviewRating": 4.9, "feedbacks": 300},
            {key: proof(value, now) for key, value in values.items()})


def channel_data(country="Россия"):
    return dict(info=dict(id=1, title="Пример", username="@example_channel",
                          link="t.me/example_channel", peer_type="channel", country=country,
                          category="Технологии", about="Реклама @public_manager", tgstat_restrictions=[]),
                stat=dict(id=1, username="@example_channel", peer_type="channel",
                          participants_count=2000, avg_post_reach=600),
                observed_at=datetime.now(timezone.utc).isoformat())


class QualityTests(unittest.TestCase):
    def test_verified_brand_accepted_without_manual_fields(self):
        product, proofs = brand_data()
        result = assess_brand(product, proofs, niche="Технологии")
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["score"], 85)
        self.assertNotIn("Комментарий", result["candidate"])
        self.assertNotIn("Telegram/почта", result["candidate"])
        self.assertEqual(result["candidate"]["Страна"], "Россия")

    def test_any_niche_does_not_invent_match(self):
        product, proofs = brand_data()
        result = assess_brand(product, proofs)
        self.assertEqual(result["score"], 70)
        self.assertEqual(result["decision"], "accept")

    def test_belarus_accepted(self):
        product, proofs = brand_data()
        proofs["country"]["value"] = "Беларусь"
        self.assertEqual(assess_brand(product, proofs)["candidate"]["Страна"], "Беларусь")

    def test_unsupported_or_unknown_country_not_accepted(self):
        product, proofs = brand_data()
        for value in (None, "Казахстан", "Доставка по России"):
            proofs["country"]["value"] = value
            self.assertNotEqual(assess_brand(product, proofs)["decision"], "accept")

    def test_missing_stale_or_unverified_proof_needs_review(self):
        product, proofs = brand_data()
        for key in ("country", "contact", "last_activity", "brand"):
            for mode in ("missing", "stale", "unverified"):
                with self.subTest(key=key, mode=mode):
                    changed = deepcopy(proofs)
                    if mode == "missing":
                        del changed[key]
                    elif mode == "stale":
                        changed[key]["checked_at"] = "2020-01-01T00:00:00+00:00"
                    else:
                        changed[key]["operator_verified"] = False
                    self.assertNotEqual(assess_brand(product, changed)["decision"], "accept")

    def test_quality_rejections(self):
        product, proofs = brand_data()
        for field, value in (("agency", True), ("account_age_days", 10), ("followers", 10)):
            changed = deepcopy(proofs)
            changed[field]["value"] = value
            self.assertEqual(assess_brand(product, changed)["decision"], "reject")
        self.assertEqual(assess_brand(dict(product, reviewRating=2), proofs)["decision"], "reject")
        self.assertEqual(assess_brand(dict(product, feedbacks=1), proofs)["decision"], "reject")

    def test_brand_source_and_schema(self):
        product, proofs = brand_data()
        source = WBSource()
        decisions = source.collect(payload={"data": {"products": [product, product]}},
                                   proofs={"example brand": proofs})
        self.assertEqual(len(decisions), 1)
        with self.assertRaises(SourceError):
            source.collect(payload={"changed": []})
        with self.assertRaises(SourceError):
            source.collect(query="товар")

    def test_channel_accepted_and_contacts_only_suggested(self):
        result = channel_decision(**channel_data())
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate"]["ERR (%)"], 30.0)
        self.assertNotIn("Контакт", result["candidate"])
        self.assertIn("@public_manager", result["contact_suggestions"])
        self.assertNotIn("Стоимость 1/24", result["candidate"])

    def test_channel_belarus(self):
        self.assertEqual(channel_decision(**channel_data("Беларусь"))["decision"], "accept")

    def test_channel_country_restrictions_and_identity(self):
        for mutation in ("country", "restrictions", "identity", "id", "chat", "stale", "float", "over100"):
            data = channel_data()
            if mutation == "country": data["info"]["country"] = "Казахстан"
            if mutation == "restrictions": data["info"]["tgstat_restrictions"] = ["scam"]
            if mutation == "identity": data["stat"]["username"] = "@other_channel"
            if mutation == "id": data["stat"]["id"] = 999
            if mutation == "chat": data["info"]["peer_type"] = "chat"
            if mutation == "stale": data["observed_at"] = "2020-01-01T00:00:00+00:00"
            if mutation == "float": data["stat"]["avg_post_reach"] = 10.5
            if mutation == "over100": data["stat"]["avg_post_reach"] = 3000
            with self.subTest(mutation=mutation):
                self.assertNotEqual(channel_decision(**data)["decision"], "accept")

    def test_channel_filters(self):
        data = channel_data()
        self.assertEqual(channel_decision(**data, min_reach=1000)["decision"], "reject")
        self.assertEqual(channel_decision(**data, min_err=50)["decision"], "reject")

    def test_missing_token_fails_before_network(self):
        source = TGStatSource(token="")
        with patch("tg_parser.SourceHTTP") as transport:
            with self.assertRaises(SourceError):
                source.collect(query="игры")
            transport.assert_not_called()

    def test_country_aliases(self):
        self.assertEqual(country_code("РФ"), "ru")
        self.assertEqual(country_code("Беларусь"), "by")
        self.assertIsNone(country_code("Доставка в РФ"))


class HTTPTests(unittest.TestCase):
    def transport(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return SourceHTTP(client=client, sleep=lambda _: None)

    def test_retry_only_temporary_errors(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})
        self.assertEqual(self.transport(handler).get("https://example.com"), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_no_retry_on_block_or_quota(self):
        for status in (401, 403, 429, 498):
            calls = []
            def handler(request):
                calls.append(1)
                return httpx.Response(status)
            with self.assertRaises(SourceError):
                self.transport(handler).get("https://example.com")
            self.assertEqual(len(calls), 1)

    def test_token_never_in_user_error(self):
        def handler(request):
            raise httpx.ConnectError("secret-token-is-here", request=request)
        with self.assertRaises(SourceError) as context:
            self.transport(handler).get("https://api.tgstat.ru", params={"token": "secret-token-is-here"})
        self.assertNotIn("secret-token", str(context.exception))

    def test_invalid_json_and_size(self):
        with self.assertRaises(SourceError):
            self.transport(lambda _: httpx.Response(200, text="<html>captcha</html>")).get("https://example.com")
        with patch("config.HTTP_MAX_RESPONSE_BYTES", 5), self.assertRaises(SourceError):
            self.transport(lambda _: httpx.Response(200, text="1234567")).get("https://example.com")

    def test_tgstat_live_contract(self):
        data = channel_data()
        methods = []
        def handler(request):
            methods.append(request.url.path)
            if request.url.path.endswith("search"):
                self.assertEqual(request.url.params["country"], "ru")
                value = {"items": [{"id": 1}]}
            elif request.url.path.endswith("get"):
                value = data["info"]
            else:
                value = data["stat"]
            return httpx.Response(200, json={"status": "ok", "response": value})
        decisions = TGStatSource(token="test", http=self.transport(handler)).collect(query="игры", countries=("ru",))
        self.assertEqual(decisions[0]["decision"], "accept")
        self.assertEqual(len(methods), 3)

    def test_tgstat_api_error(self):
        source = TGStatSource(token="test", http=self.transport(
            lambda _: httpx.Response(200, json={"status": "error", "error": "token secret"})))
        with self.assertRaises(SourceError) as context:
            source.collect(query="игры")
        self.assertNotIn("token secret", str(context.exception))

    def test_wb_robots_denial(self):
        transport = self.transport(lambda _: httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
        with self.assertRaises(SourceError):
            WBSource(http=transport).collect(query="товар", allow_unofficial=True)
        self.assertEqual(transport.requests, 1)

    def test_enrichment_only_suggests_contacts(self):
        def handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(200, text='<html>Example Brand <a href="mailto:public@example.com">contact</a><a href="https://t.me/example_brand">TG</a></html>')
        with patch("wb_parser.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            result = enrich_website("Example Brand", "https://example.com", http=self.transport(handler))
        self.assertFalse(result["operator_verified"])
        self.assertIn("public@example.com", result["contact_suggestions"])

    def test_enrichment_rejects_private_network(self):
        with patch("wb_parser.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(SourceError):
                enrich_website("Example", "https://localhost")


class SourceIntegrationTests(unittest.TestCase):
    def test_tg_full_pipeline_and_second_run_preserves_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = SafeWriter(Path(folder) / "storage")
            payload = {"channels": [channel_data()]}
            preview = run_source("tgstat", payload=payload, writer=writer)
            self.assertFalse(writer.root.exists())
            real = run_source("tgstat", payload=payload, writer=writer, dry_run=False)
            self.assertEqual(preview["summary"], real["summary"])
            payload["channels"][0]["stat"]["participants_count"] = 4000
            run_source("tgstat", payload=payload, writer=writer, dry_run=False)
            row = writer.read("telegram")[0]
            self.assertEqual(row["Подписчики"], "2000")
            self.assertEqual(row["Контакт"], "")
            self.assertIn('source_run', (writer.root/"logs"/"agent.log").read_text(encoding="utf-8"))

    def test_wb_full_pipeline_protects_manual_fields(self):
        product, proofs = brand_data()
        with tempfile.TemporaryDirectory() as folder:
            writer = SafeWriter(Path(folder)/"storage")
            report = run_source("wb", payload={"products": [product]},
                                proofs={"example brand": proofs}, writer=writer, dry_run=False)
            self.assertEqual(report["source_summary"]["accept"], 1)
            row = writer.read("advertisers")[0]
            self.assertEqual(row["Страна"], "Россия")
            self.assertEqual(row["Комментарий"], "")
            self.assertEqual(row["Telegram/почта"], "")

    def test_country_subset(self):
        product, proofs = brand_data()
        report = run_source("wb", payload={"products": [product]},
                            proofs={"example brand": proofs}, countries=("by",))
        self.assertEqual(report["summary"]["added"], 0)

    def test_synthetic_fixture_cli_cannot_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--source", "wb", "--wb-file", "tests/fixtures/wb_catalog.json", "--write"])


class AdditionalSourceSafetyTests(unittest.TestCase):
    def test_subscription_error_is_actionable_without_raw_payload(self):
        from tg_parser import _response
        with self.assertRaises(SourceError) as context:
            _response({"status": "error", "error": "no_active_subscription",
                       "token": "secret-must-not-be-printed"})
        self.assertIn("no_active_subscription", str(context.exception))
        self.assertNotIn("secret-must-not-be-printed", str(context.exception))

    def test_unknown_error_payload_remains_redacted(self):
        from tg_parser import _response
        for error in ("secret-must-not-be-printed", {"token": "secret"}):
            with self.assertRaises(SourceError) as context:
                _response({"status": "error", "error": error})
            self.assertNotIn("secret", str(context.exception))

    def test_best_product_not_hidden_by_low_quality_duplicate(self):
        product, proofs = brand_data()
        low = dict(product, reviewRating=2, feedbacks=1)
        result = WBSource().collect(payload={"products": [low, product]},
                                    proofs={"example brand": proofs})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision"], "accept")

    def test_thirty_days_and_one_hour_is_stale(self):
        product, proofs = brand_data()
        proofs["country"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=30, hours=1)).isoformat()
        self.assertNotEqual(assess_brand(product, proofs)["decision"], "accept")
        data = channel_data()
        data["observed_at"] = proofs["country"]["checked_at"]
        self.assertNotEqual(channel_decision(**data)["decision"], "accept")

    def test_both_countries_are_queried(self):
        countries = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: self.response(request, countries)))
        self.addCleanup(client.close)
        transport = SourceHTTP(client=client, sleep=lambda _: None)
        self.assertEqual(TGStatSource(token="test", http=transport).collect(query="игры"), [])
        self.assertEqual(countries, ["ru", "by"])

    @staticmethod
    def response(request, countries):
        countries.append(request.url.params["country"])
        return httpx.Response(200, json={"status": "ok", "response": {"items": []}})

    def test_invalid_limits_and_filters(self):
        for limit in (0, 51, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(SourceError):
                WBSource().collect(payload={"products": []}, limit=limit)
            with self.subTest(limit=limit), self.assertRaises(SourceError):
                TGStatSource(token="").collect(payload={"channels": []}, limit=limit)
        with self.assertRaises(SourceError):
            TGStatSource(token="").collect(payload={"channels": []}, min_err=float("nan"))

    def test_enrichment_failure_downgrades_acceptance(self):
        product, proofs = brand_data()
        with patch("wb_parser.enrich_website", side_effect=SourceError("blocked")):
            report = run_source("wb", payload={"products": [product]},
                                proofs={"example brand": proofs}, enrich_sites=True)
        self.assertEqual(report["source_summary"]["accept"], 0)
        self.assertEqual(report["source_summary"]["review"], 1)

    def test_import_filters_do_not_change_country(self):
        report = run_source("tgstat", payload={"channels": [channel_data("Россия")]}, countries=("by",))
        self.assertEqual(report["summary"]["added"], 0)

    def test_levenshtein_signal_only(self):
        from dedup import name_similarity, find_matches
        self.assertAlmostEqual(name_similarity("kitten", "sitting"), 1 - 3 / 7)
        rows = [{"Название компании": "Example Brands", "Сайт": "https://other.example"}]
        matches = find_matches("advertisers", {"Название компании": "Example Brand", "Сайт": "https://example.com"}, rows)
        self.assertEqual(matches["exact"], [])
        self.assertEqual(matches["review"], [0])


class BrowserSourceTests(unittest.TestCase):
    def capture(self):
        return {
            "url": "https://tgstat.ru/channel/@example_channel/stat",
            "title": "Пример",
            "text": "@example_channel\nГео и язык канала: Россия, Русский\nКатегория:\nТехнологии\nРеклама //\n@public_manager\nСводная\nПОДПИСЧИКИ",
            "metrics": [
                {"value": "2 000", "label": "2 000\nПОДПИСЧИКИ"},
                {"value": "600", "label": "600\n30% ERR\nСРЕДНИЙ ОХВАТ\n1 ПУБЛИКАЦИИ"},
                {"value": "500", "label": "500 СРЕДНИЙ РЕКЛАМНЫЙ ОХВАТ 1 ПУБЛИКАЦИИ"},
            ],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def test_visible_capture_accepted_without_contacts(self):
        from tg_parser import web_channel_decision
        decision = web_channel_decision(self.capture())
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["candidate"]["Средний охват"], 600)
        self.assertEqual(decision["candidate"]["ERR (%)"], 30.0)
        self.assertEqual(decision["candidate"]["Тематика"], "Технологии")
        self.assertNotIn("Контакт", decision["candidate"])
        self.assertIn("@public_manager", decision["contact_suggestions"])
        self.assertNotIn("text", decision["evidence"])

    def test_no_guessing_when_metric_missing_abbreviated_or_conflicting(self):
        from tg_parser import web_channel_decision
        for mutation in ("missing", "abbreviated", "conflicting", "zero", "identity", "country", "date", "warning"):
            capture = self.capture()
            if mutation == "missing": capture["metrics"] = []
            if mutation == "abbreviated": capture["metrics"][0]["value"] = "2k"
            if mutation == "conflicting": capture["metrics"].append({"label": "ПОДПИСЧИКИ", "value": "9999"})
            if mutation == "zero": capture["metrics"][0]["value"] = "0"
            if mutation == "identity": capture["text"] = capture["text"].replace("@example_channel", "@other_channel")
            if mutation == "country": capture["text"] = capture["text"].replace("Россия", "Казахстан")
            if mutation == "date": capture["observed_at"] = "2020-01-01T00:00:00+00:00"
            if mutation == "warning": capture["text"] += "\nSCAM"
            with self.subTest(mutation=mutation):
                self.assertNotEqual(web_channel_decision(capture)["decision"], "accept")

    def test_country_and_category_in_both_responsive_layouts(self):
        from tg_parser import web_channel_decision
        for geo in ("Гео и язык канала: Россия, Русский",
                    "Гео и язык канала\nРоссия, Русский",
                    "Гео и язык канала:\nРоссия, Русский",
                    "Гео и язык канала\n\nРоссия, Русский"):
            for category in ("Категория: Технологии", "Категория\nТехнологии", "Категория:\nТехнологии"):
                capture = self.capture()
                capture["text"] = f"@example_channel\n{geo}\n{category}\nРеклама // @public_manager\nСводная"
                with self.subTest(geo=geo, category=category):
                    decision = web_channel_decision(capture)
                    self.assertEqual(decision["decision"], "accept")
                    self.assertEqual(decision["evidence"]["country"], "ru")
                    self.assertEqual(decision["candidate"]["Тематика"], "Технологии")

    def test_conflicting_or_missing_country_is_never_inferred(self):
        from tg_parser import web_channel_decision
        for text in ("@example_channel\nКатегория\nТехнологии",
                     "@example_channel\nГео и язык канала\nРоссия, Русский\nГео и язык канала\nБеларусь, Русский",
                     "@example_channel\nГео и язык канала\nКазахстан, Русский"):
            capture = self.capture()
            capture["text"] = text
            self.assertEqual(web_channel_decision(capture)["decision"], "review")

    def test_only_selected_countries(self):
        from tg_parser import web_channel_decision
        self.assertEqual(web_channel_decision(self.capture(), countries=("by",))["decision"], "review")

    def test_web_url_allowlist(self):
        from tg_parser import web_channel_url
        self.assertEqual(web_channel_url("@example_channel"), self.capture()["url"])
        self.assertEqual(web_channel_url("https://by.tgstat.com/channel/@EXAMPLE_CHANNEL"),
                         "https://by.tgstat.com/channel/@example_channel/stat")
        for url in ("https://evil.example/channel/@example_channel", "http://tgstat.ru/channel/@example_channel",
                    "https://tgstat.ru.evil.example/channel/@example_channel",
                    "https://tgstat.ru/channel/@example_channel?token=secret",
                    "https://tgstat.ru/channel/@example_channel/123", "https://user:password@tgstat.ru/channel/@example_channel"):
            with self.subTest(url=url), self.assertRaises(SourceError):
                web_channel_url(url)

    def test_import_uses_no_browser(self):
        from tg_parser import TGStatWebSource
        with patch.object(TGStatWebSource, "_browse") as browse:
            decisions = TGStatWebSource().collect(payload={"captures": [self.capture()]})
        browse.assert_not_called()
        self.assertEqual(decisions[0]["mode"], "web-import")

    def test_limit_and_required_arguments(self):
        from tg_parser import TGStatWebSource
        for limit in (0, 11, True):
            with self.assertRaises(SourceError):
                TGStatWebSource().collect(payload={"captures": []}, limit=limit)
        with self.assertRaises(SourceError):
            TGStatWebSource().collect()
        with self.assertRaises(SourceError):
            TGStatWebSource().collect(category="../private")

    def test_browser_decisions_use_safe_writer_and_default_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = SafeWriter(Path(folder)/"storage")
            report = run_source("tgstat-web", payload={"captures": [self.capture()]}, writer=writer, limit=5)
            self.assertEqual(report["summary"]["added"], 1)
            self.assertFalse(writer.root.exists())
            report = run_source("tgstat-web", payload={"captures": [self.capture()]}, writer=writer, limit=5, dry_run=False)
            self.assertEqual(writer.read("telegram")[0]["Контакт"], "")

    def test_live_adapter_invoked_with_explicit_channels(self):
        from tg_parser import TGStatWebSource
        with patch.object(TGStatWebSource, "_browse", return_value=[self.capture()]) as browse:
            report = run_source("tgstat-web", channels=["@example_channel"], limit=5)
        self.assertEqual(browse.call_args.args[0], [self.capture()["url"]])
        self.assertEqual(report["summary"]["added"], 1)

    def test_cli_browser_dispatch_and_filter_validation(self):
        from tg_parser import TGStatWebSource
        output = io.StringIO()
        with patch.object(TGStatWebSource, "_browse", return_value=[self.capture()]), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--source", "tgstat-web", "--channels", "@example_channel", "--dry-run"]), 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--source", "tgstat-web", "--query", "игры"])

    def test_browser_lifecycle_uses_manual_login_without_session_export(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import MagicMock, Mock
        manager = MagicMock()
        runtime = manager.__enter__.return_value
        browser = runtime.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        page.url = self.capture()["url"]
        page.title.return_value = "Статистика"
        page.locator.return_value.inner_text.return_value = "Статистика"
        page.locator.return_value.count.return_value = 1
        page.evaluate.return_value = self.capture()
        prompt = Mock()
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            decisions = TGStatWebSource(prompt=prompt, progress=lambda _: None, sleep=lambda _: None).collect(
                channels=["@example_channel"], limit=1)
        self.assertEqual(decisions[0]["decision"], "accept")
        runtime.chromium.launch.assert_called_once_with(headless=False)
        browser.new_context.assert_called_once_with(locale="ru-RU", accept_downloads=False)
        prompt.assert_called_once_with("")
        context.storage_state.assert_not_called()
        context.cookies.assert_not_called()
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_browser_closes_on_failed_login(self):
        from tg_parser import TGStatWebSource
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from unittest.mock import MagicMock
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.goto.return_value.status = 200
        page.url = self.capture()["url"]
        page.title.return_value = "Статистика"
        page.locator.return_value.inner_text.return_value = "Вход"
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.wait_for.side_effect = BrowserTimeout("login pending")
        with patch("playwright.sync_api.sync_playwright", return_value=manager), self.assertRaises(SourceError):
            TGStatWebSource(prompt=lambda _: None, progress=lambda _: None, sleep=lambda _: None).collect(
                channels=["@example_channel"], limit=1)
        browser.close.assert_called_once()

    def test_login_waits_for_account_menu(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import Mock
        page = Mock()
        prompt = Mock()
        TGStatWebSource(prompt=prompt, progress=lambda _: None)._ensure_login(page, self.capture()["url"])
        page.locator.return_value.wait_for.assert_called_once_with(state="visible", timeout=15000)
        prompt.assert_not_called()

    def test_login_can_finish_on_second_check(self):
        from tg_parser import TGStatWebSource
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from unittest.mock import Mock
        page = Mock()
        page.locator.return_value.wait_for.side_effect = [BrowserTimeout("pending"), None]
        prompt = Mock()
        source = TGStatWebSource(prompt=prompt, progress=lambda _: None)
        with patch.object(source, "_visit") as visit:
            source._ensure_login(page, self.capture()["url"])
        prompt.assert_called_once_with("")
        visit.assert_called_once_with(page, self.capture()["url"])
        self.assertEqual(page.locator.return_value.wait_for.call_count, 2)

    def test_partial_browser_collection_returns_plan_and_blocks_write(self):
        from tg_parser import TGStatWebSource, WebCollectionError
        first = self.capture()
        second = deepcopy(first)
        second["url"] = "https://tgstat.ru/channel/@another_channel/stat"
        second["title"] = "Вторая площадка"
        second["text"] = second["text"].replace("@example_channel", "@another_channel")
        with tempfile.TemporaryDirectory() as folder:
            writer = SafeWriter(Path(folder)/"storage")
            failure = WebCollectionError("HTTP 403", captures=[first, second],
                                         failed_url="https://by.tgstat.com/channel/@devby/stat")
            with patch.object(TGStatWebSource, "_browse", side_effect=failure):
                report = run_source("tgstat-web", channels=["@example_channel"],
                                    limit=3, writer=writer, dry_run=False)
            self.assertEqual(report["status"], "stopped")
            self.assertEqual(report["source_error"]["collected"], 2)
            self.assertTrue(report["source_error"]["write_blocked"])
            self.assertTrue(report["dry_run"])
            self.assertFalse(report["requested_dry_run"])
            self.assertEqual(report["summary"]["added"], 2)
            self.assertFalse(writer.root.exists())

    def test_cli_partial_failure_exits_nonzero_with_json_report(self):
        from tg_parser import TGStatWebSource, WebCollectionError
        failure = WebCollectionError("HTTP 403", captures=[self.capture()],
                                     failed_url="https://by.tgstat.com/channel/@devby/stat")
        output = io.StringIO()
        with patch.object(TGStatWebSource, "_browse", side_effect=failure), contextlib.redirect_stdout(output):
            code = main(["--source", "tgstat-web", "--channels", "@example_channel", "--dry-run"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["source_summary"]["accept"], 1)
        self.assertEqual(report["source_error"]["failed_url"], failure.failed_url)

    def test_browser_keeps_read_captures_when_next_page_returns_403(self):
        from tg_parser import TGStatWebSource, WebCollectionError
        from unittest.mock import MagicMock, Mock
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.goto.side_effect = [Mock(status=200), Mock(status=200), Mock(status=200), Mock(status=403)]
        page.url = self.capture()["url"]
        page.title.return_value = "Статистика"
        page.locator.return_value.inner_text.return_value = "Статистика"
        page.evaluate.return_value = self.capture()
        source = TGStatWebSource(prompt=lambda _: None, progress=lambda _: None, sleep=lambda _: None)
        with patch("playwright.sync_api.sync_playwright", return_value=manager), self.assertRaises(WebCollectionError) as caught:
            source.collect(channels=["@example_channel", "https://by.tgstat.com/channel/@devby"], limit=2)
        self.assertEqual(len(caught.exception.decisions), 1)
        self.assertEqual(caught.exception.decisions[0]["decision"], "accept")
        self.assertIn("by.tgstat.com", caught.exception.failed_url)
        self.assertEqual(page.goto.call_count, 4)
        browser.close.assert_called_once()

    def test_corporate_description_with_good_metrics_needs_review(self):
        from tg_parser import web_channel_decision
        capture = self.capture()
        capture["title"] = "Яндекс"
        capture["text"] = "@example_channel\nОфициальный канал компании\nГео и язык канала\nРоссия, Русский\nКатегория\nТехнологии\nСводная"
        decision = web_channel_decision(capture)
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["placement"]["status"], "unconfirmed")
        self.assertEqual(decision["candidate"]["Подписчики"], 2000)
        self.assertNotIn("Контакт", decision["candidate"])

    def test_offers_need_nearby_advertising_contact(self):
        from tg_parser import placement_evidence
        for text in ("Реклама // @public_manager", "Реклама:\n@public_manager",
                     "Купить рекламу: https://t.me/public_manager",
                     "По вопросам рекламы: ads@example.com",
                     "Стоимость рекламы: 5000 руб.\n@public_manager"):
            with self.subTest(text=text):
                self.assertEqual(placement_evidence(text)["status"], "offered")

    def test_generic_mentions_editor_and_cooperation_are_not_offers(self):
        from tg_parser import placement_evidence
        for text in ("Пишем про рекламу и технологии. @public_manager",
                     "Реклама и маркетинг: автор @public_manager",
                     "Сотрудничество: @todaycast", "Редакция: @public_manager",
                     "Реклама\nРедакция: @public_manager", "Реклама без контакта",
                     "Новости: бренд купил рекламу у @public_manager"):
            with self.subTest(text=text):
                self.assertEqual(placement_evidence(text)["status"], "unconfirmed")

    def test_explicit_refusal_is_not_accepted(self):
        from tg_parser import placement_evidence
        for text in ("Рекламу не размещаем\nРеклама: @public_manager",
                     "Не продаём рекламу. @public_manager"):
            self.assertEqual(placement_evidence(text)["status"], "not_offered")

    def test_offer_does_not_override_metric_rejection(self):
        from tg_parser import web_channel_decision
        decision = web_channel_decision(self.capture(), min_reach=1000)
        self.assertEqual(decision["placement"]["status"], "offered")
        self.assertEqual(decision["decision"], "reject")

    def test_api_also_requires_advertising_offer(self):
        data = channel_data()
        data["info"]["about"] = "Официальный канал компании. Поддержка: @public_manager"
        self.assertEqual(channel_decision(**data)["decision"], "review")

    def test_no_name_based_blacklist_when_offer_exists(self):
        from tg_parser import web_channel_decision
        capture = self.capture()
        capture["title"] = "Яндекс"
        self.assertEqual(web_channel_decision(capture)["decision"], "accept")

    def test_unconfirmed_placement_is_not_passed_to_storage(self):
        from tg_parser import TGStatWebSource
        capture = self.capture()
        capture["text"] = capture["text"].replace("Реклама //\n@public_manager", "Сотрудничество: @todaycast")
        with tempfile.TemporaryDirectory() as folder:
            writer = SafeWriter(Path(folder)/"storage")
            with patch.object(writer, "upsert") as upsert:
                report = run_source("tgstat-web", payload={"captures": [capture]}, writer=writer, limit=5, dry_run=False)
            upsert.assert_not_called()
            self.assertEqual(report["source_summary"]["review"], 1)
            self.assertFalse((writer.root/"data"/"telegram.csv").exists())

    def test_catalog_uses_only_main_list_and_respects_limit(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import Mock
        page = Mock()
        root = page.locator.return_value
        root.count.return_value = 1
        root.locator.return_value.evaluate_all.return_value = [
            "https://tgstat.ru/channel/@first_channel", "https://tgstat.ru/channel/@second_channel",
            "https://tgstat.ru/channel/@third_channel"]
        urls = TGStatWebSource._catalog_urls(page, limit=2)
        page.locator.assert_called_once_with("#category-list-form")
        root.locator.assert_called_once_with("a[href]")
        self.assertEqual(urls, ["https://tgstat.ru/channel/@first_channel/stat",
                               "https://tgstat.ru/channel/@second_channel/stat"])

    def test_catalog_deduplicates_alias_links_and_excludes_posts_and_chats(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import Mock
        page = Mock()
        root = page.locator.return_value
        root.count.return_value = 1
        root.locator.return_value.evaluate_all.return_value = [
            "https://tgstat.ru/channel/@first_channel", "https://by.tgstat.com/channel/@FIRST_CHANNEL/stat",
            "https://tgstat.ru/channel/@first_channel/123", "https://tgstat.ru/chat/@chat_channel",
            "https://tgstat.ru/channel/private_invite", "https://evil.example/channel/@another_channel",
            "https://tgstat.ru/channel/@second_channel"]
        urls = TGStatWebSource._catalog_urls(page, limit=5)
        self.assertEqual(len(urls), 2)
        self.assertIn("first_channel", urls[0])
        self.assertIn("second_channel", urls[1])

    def test_catalog_excludes_already_selected_username_across_domains(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import Mock
        page = Mock()
        root = page.locator.return_value
        root.count.return_value = 1
        root.locator.return_value.evaluate_all.return_value = [
            "https://by.tgstat.com/channel/@first_channel", "https://by.tgstat.com/channel/@second_channel"]
        urls = TGStatWebSource._catalog_urls(page, limit=5,
                    excluded=["https://tgstat.ru/channel/@first_channel/stat"])
        self.assertEqual(urls, ["https://by.tgstat.com/channel/@second_channel/stat"])

    def test_missing_catalog_never_falls_back_to_whole_page(self):
        from tg_parser import TGStatWebSource
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from unittest.mock import Mock
        page = Mock()
        page.locator.return_value.wait_for.side_effect = BrowserTimeout("missing root")
        with self.assertRaises(SourceError):
            TGStatWebSource._catalog_urls(page, limit=4)
        page.locator.assert_called_once_with("#category-list-form")
        page.locator.return_value.locator.assert_not_called()

    def test_access_block_is_not_bypassed(self):
        from tg_parser import TGStatWebSource
        from unittest.mock import Mock
        page = Mock()
        page.goto.return_value.status = 403
        with self.assertRaises(SourceError):
            TGStatWebSource._visit(page, self.capture()["url"])
        self.assertEqual(page.goto.call_count, 1)


class AllBrowserSourceTests(unittest.TestCase):
    def setUp(self):
        from twitch_parser import TwitchWebSource
        self.source = TwitchWebSource()
        self.now = datetime.now(timezone.utc)
        self.url = "https://www.twitch.tv/example"
        self.capture = {"url": self.url, "title": "example", "text": "example\nРеклама: @example_ads\nПодписчики: 12345\nCurrent viewers: 850", "observed_at": self.now.isoformat()}
        self.proofs = {self.url: {"country": proof("ru", self.now)}}

    def test_all_six_adapters_accept_only_own_urls(self):
        from wb_parser import WBWebSource
        from ozon_parser import OzonWebSource
        from instagram_parser import InstagramWebSource
        from youtube_parser import YouTubeWebSource
        from twitch_parser import TwitchWebSource
        from kick_parser import KickWebSource
        for cls, url in ((WBWebSource, "https://www.wildberries.ru/catalog/123/detail.aspx"),
                         (OzonWebSource, "https://www.ozon.ru/product/item-123/"),
                         (InstagramWebSource, "https://www.instagram.com/example/"),
                         (YouTubeWebSource, "https://www.youtube.com/@example/about"),
                         (TwitchWebSource, self.url), (KickWebSource, "https://kick.com/example")):
            with self.subTest(platform=cls.platform):
                self.assertTrue(cls().normalize_url(url))
                for bad in ("https://localhost/example", url + "?token=secret", url.replace("https:", "http:")):
                    with self.assertRaises((SourceError, ValueError)):
                        cls().normalize_url(bad)

    def test_service_pages_rejected(self):
        for path in ("directory", "login", "search", "settings"):
            with self.assertRaises(SourceError):
                self.source.normalize_url("https://www.twitch.tv/" + path)

    def test_current_online_is_not_average_and_contact_not_written(self):
        result = self.source.collect(payload={"captures": [self.capture]}, proofs=self.proofs)[0]
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate"]["Подписчики"], 12345)
        self.assertNotIn("Средний онлайн", result["candidate"])
        self.assertNotIn("Контакт", result["candidate"])
        self.assertIn("@example_ads", result["contact_suggestions"])

    def test_missing_country_is_review(self):
        result = self.source.collect(payload={"captures": [self.capture]})[0]
        self.assertEqual(result["decision"], "review")

    def test_selected_country_is_enforced(self):
        result = self.source.collect(payload={"captures": [self.capture]}, proofs=self.proofs, countries=["by"])[0]
        self.assertEqual(result["decision"], "review")

    def test_stale_or_login_capture_is_review(self):
        for changes in ({"observed_at": (self.now - timedelta(days=2)).isoformat()}, {"text": "Вход в аккаунт"}):
            capture = dict(self.capture, **changes)
            result = self.source.collect(payload={"captures": [capture]}, proofs=self.proofs)[0]
            self.assertEqual(result["decision"], "review")

    def test_rounded_and_conflicting_counts_omitted(self):
        capture = dict(self.capture, text="example\nРеклама: @example_ads\nFollowers: 12K\nСредний онлайн: 100\nСредний онлайн: 200")
        result = self.source.collect(payload={"captures": [capture]}, proofs=self.proofs)[0]
        self.assertNotIn("Подписчики", result["candidate"])
        self.assertNotIn("Средний онлайн", result["candidate"])

    def test_brand_profile_without_proofs_not_written(self):
        from instagram_parser import InstagramWebSource
        capture = dict(self.capture, url="https://www.instagram.com/example", title="Example shop")
        result = InstagramWebSource().collect(payload={"captures": [capture]})[0]
        self.assertEqual(result["decision"], "review")
        self.assertEqual(result["candidate"], {})

    def test_non_marketplace_brand_does_not_need_product_rating(self):
        from instagram_parser import InstagramWebSource
        from unittest.mock import patch
        source = InstagramWebSource()
        url = "https://www.instagram.com/example"
        capture = dict(self.capture, url=url)
        facts = {"brand": dict(proof("example", self.now), url=url), "country": proof("ru", self.now)}
        with patch("wb_parser.assess_brand", return_value={"decision": "review", "candidate": {}, "reasons": [], "score": 0}) as assess:
            source.collect(payload={"captures": [capture]}, proofs={url: facts})
            self.assertFalse(assess.call_args.kwargs["marketplace_metrics"])

    def test_partial_failure_blocks_entire_write(self):
        from wb_parser import VisibleBrowserSource
        def fail(source, urls, consume):
            consume(self.capture)
            raise SourceError("HTTP 403")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(VisibleBrowserSource, "_browse", fail):
                report = run_source("twitch-web", urls=[self.url], proofs=self.proofs,
                                    limit=2, dry_run=False, writer=SafeWriter(Path(directory)))
            self.assertEqual(report["status"], "stopped")
            self.assertTrue(report["dry_run"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_real_streamer_write_uses_safe_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SafeWriter(Path(directory))
            report = run_source("twitch-web", payload={"captures": [self.capture]}, proofs=self.proofs,
                                limit=1, dry_run=False, writer=writer)
            self.assertEqual(report["summary"]["added"], 1)
            second = run_source("twitch-web", payload={"captures": [self.capture]}, proofs=self.proofs,
                                limit=1, dry_run=False, writer=writer)
            self.assertEqual(second["summary"]["skipped"], 1)
            self.assertEqual(writer.read("streamers")[0]["Контакт"], "")


class BrowserAutomationTests(unittest.TestCase):
    def test_live_layout_followers_and_country_are_parsed(self):
        from twitch_parser import TwitchWebSource
        from kick_parser import KickWebSource
        now = datetime.now(timezone.utc).isoformat()
        for source, url, text, followers in (
            (TwitchWebSource(), "https://www.twitch.tv/nix", "Информация: Nix\n1\u00a0285\u00a0499\nфолловеров\nРусский", 1285499),
            (KickWebSource(), "https://kick.com/fijek", "Fijek\n676 Фолловеры\nRussian", 676)):
            result = source.collect(payload={"captures": [{"url": url, "text": text, "observed_at": now}]})[0]
            self.assertEqual(result["candidate"]["Подписчики"], followers)
            self.assertEqual(result["decision"], "review")  # Язык не подтверждает страну.
            self.assertNotIn("Средний онлайн", result["candidate"])

    def test_discovery_deduplicates_and_skips_navigation(self):
        from unittest.mock import MagicMock
        from twitch_parser import TwitchWebSource
        source = TwitchWebSource()
        source.discovery_limit = 2
        page = MagicMock()
        page.goto.return_value.status = 200
        page.url = source.catalog_url
        page.locator.return_value.evaluate_all.return_value = ["/directory", "/nix", "/nix", "https://evil.example/nix", "/t2x2", "/third"]
        self.assertEqual(source._discover_urls(page), ["https://www.twitch.tv/nix", "https://www.twitch.tv/t2x2"])
        page.locator.assert_called_once_with(source.catalog_selector)

    def test_discovery_empty_catalog_fails(self):
        from unittest.mock import MagicMock
        from kick_parser import KickWebSource
        source = KickWebSource()
        source.discovery_limit = 2
        page = MagicMock()
        page.goto.return_value.status = 200
        page.url = source.catalog_url
        page.locator.return_value.evaluate_all.return_value = ["/events", "/privacy-policy"]
        with self.assertRaises(SourceError):
            source._discover_urls(page)

    def test_login_button_is_not_a_login_gate(self):
        from unittest.mock import MagicMock
        from twitch_parser import TwitchWebSource
        page = MagicMock()
        page.url = "https://www.twitch.tv/nix"
        page.locator.return_value.count.return_value = 0
        self.assertFalse(TwitchWebSource()._login_required(page))
        page.url = "https://www.twitch.tv/login"
        self.assertTrue(TwitchWebSource()._login_required(page))

    def test_capture_rejects_chat_in_profile_scope(self):
        from unittest.mock import MagicMock
        from twitch_parser import TwitchWebSource
        page = MagicMock()
        page.locator.return_value.first.evaluate.return_value = {"has_chat": True}
        with self.assertRaises(SourceError):
            TwitchWebSource()._capture(page)
        script = page.locator.return_value.first.evaluate.call_args.args[0]
        self.assertIn('chat-scroller', script)

    def test_conflicting_visible_country_needs_review(self):
        from twitch_parser import TwitchWebSource
        now = datetime.now(timezone.utc)
        url = "https://www.twitch.tv/example"
        result = TwitchWebSource().collect(payload={"captures": [{"url": url, "text": "example\nРеклама: @example_ads\nCountry: Belarus", "observed_at": now.isoformat()}]},
                                          proofs={url: {"country": proof("ru", now)}})[0]
        self.assertEqual(result["decision"], "review")


class BrowserNonInteractiveTests(unittest.TestCase):
    def test_public_page_never_prompts_for_enter(self):
        from unittest.mock import MagicMock
        from twitch_parser import TwitchWebSource
        source = TwitchWebSource()
        source.interactive_login = False
        url = "https://www.twitch.tv/nix"
        engine = MagicMock()
        page = engine.chromium.launch.return_value.new_context.return_value.new_page.return_value
        page.goto.return_value.status = 200
        page.url = url
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.first.evaluate.return_value = {"url": url, "text": "Nix\n1285499\nфолловеров", "has_chat": False}
        consume = MagicMock()
        with patch("playwright.sync_api.sync_playwright") as start, patch("builtins.input", side_effect=AssertionError("unexpected prompt")), contextlib.redirect_stderr(io.StringIO()):
            start.return_value.__enter__.return_value = engine
            source._browse([url], consume)
        consume.assert_called_once()
        engine.chromium.launch.return_value.close.assert_called_once()


class MarketplaceBrowserLayoutTests(unittest.TestCase):
    def test_tracking_does_not_change_product_identity(self):
        from wb_parser import WBWebSource
        from ozon_parser import OzonWebSource
        for source, url, tracking in ((WBWebSource(), "https://www.wildberries.ru/catalog/16001947/detail.aspx", "?targetUrl=MI"),
                                      (OzonWebSource(), "https://www.ozon.ru/product/item-123", "?sh=example")):
            self.assertEqual(source.normalize_url(url + tracking), url)
            for query in ("?sku=456", "?token=secret", "?sh=x&token=secret"):
                with self.assertRaises(SourceError):
                    source.normalize_url(url + query)

    def test_ozon_live_rating_format(self):
        from ozon_parser import OzonWebSource
        self.assertEqual(OzonWebSource._product_metrics(["4.9 • 128 525 отзывов"]), {"reviewRating": 4.9, "feedbacks": 128525})
        self.assertEqual(OzonWebSource._product_metrics(["4.9 • 12K отзывов"]), {})
        self.assertEqual(OzonWebSource._product_metrics(["4.9 • 100 отзывов", "4.8 • 200 отзывов"]), {})
        self.assertEqual(OzonWebSource._product_metrics(["5.9 • 100 отзывов"]), {})

    def test_wb_title_is_product_not_rating(self):
        from unittest.mock import MagicMock
        from wb_parser import WBWebSource
        page = MagicMock()
        page.locator.return_value.first.evaluate.return_value = {"url": "https://www.wildberries.ru/catalog/16001947/detail.aspx", "title": "Футболка серая легкая хлопок", "text": "UZcotton\nФутболка серая легкая хлопок\n4,9 · 340\u00a0192 оценки\n1135 вопросов"}
        capture = WBWebSource()._capture(page)
        self.assertEqual(capture["product_metrics"], {"reviewRating": 4.9, "feedbacks": 340192})
        self.assertEqual(capture["title"], "Футболка серая легкая хлопок")
        script = page.locator.return_value.first.evaluate.call_args.args[0]
        self.assertIn('productTitle--', script)
        self.assertIn('Характеристики и описание', script)

    def test_visible_metrics_do_not_invent_verified_brand(self):
        from ozon_parser import OzonWebSource
        now = datetime.now(timezone.utc).isoformat()
        capture = {"url": "https://www.ozon.ru/product/item-123", "title": "RAGE товар", "text": "RAGE товар\n4.9 • 128 525 отзывов", "product_metrics": {"reviewRating": 4.9, "feedbacks": 128525}, "observed_at": now}
        result = OzonWebSource().collect(payload={"captures": [capture]})[0]
        self.assertEqual(result["decision"], "review")
        self.assertEqual(result["product_observation"]["metrics"]["feedbacks"], 128525)
        self.assertEqual(result["candidate"], {})

    def test_youtube_reads_only_channel_header(self):
        from youtube_parser import YouTubeWebSource
        self.assertEqual(YouTubeWebSource.content_selector, 'yt-page-header-renderer')
        self.assertEqual(YouTubeWebSource.ready_selector, 'yt-page-header-renderer h1')


class PersistentBrowserSessionTests(unittest.TestCase):
    def test_profile_is_local_private_and_separate(self):
        from wb_parser import browser_profile_dir
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory) / 'cache'):
            first = browser_profile_dir('instagram')
            self.assertEqual(first, browser_profile_dir('instagram'))
            self.assertNotEqual(first, browser_profile_dir('youtube'))
            import os
            if os.name != "nt":
                self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            else:
                self.assertTrue(first.is_dir())
            with self.assertRaises(SourceError):
                browser_profile_dir('../personal')

    def test_profile_symlink_is_rejected(self):
        from wb_parser import browser_profile_dir
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache'
            cache.mkdir()
            (cache / 'browser_profiles').symlink_to(Path(directory), target_is_directory=True)
            with patch('config.CACHE_DIR', cache), self.assertRaises(SourceError):
                browser_profile_dir('twitch')

    def test_persistent_context_is_used_and_closed_without_prompt(self):
        from unittest.mock import MagicMock
        from twitch_parser import TwitchWebSource
        source = TwitchWebSource()
        source.keep_session = True
        source.interactive_login = False
        engine = MagicMock()
        context = engine.chromium.launch_persistent_context.return_value
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        page.url = 'https://www.twitch.tv/nix'
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.first.evaluate.return_value = {'url': page.url, 'text': 'Nix', 'has_chat': False}
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory) / 'cache'), patch('playwright.sync_api.sync_playwright') as start, patch('builtins.input', side_effect=AssertionError('unexpected prompt')), contextlib.redirect_stderr(io.StringIO()):
            start.return_value.__enter__.return_value = engine
            source._browse([page.url], MagicMock())
        engine.chromium.launch.assert_not_called()
        engine.chromium.launch_persistent_context.assert_called_once()
        context.close.assert_called_once()

    def test_import_does_not_create_a_profile(self):
        from twitch_parser import TwitchWebSource
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory) / 'cache'):
            TwitchWebSource().collect(payload={'captures': [{'url': 'https://www.twitch.tv/nix', 'text': 'Nix', 'observed_at': datetime.now(timezone.utc).isoformat()}]}, keep_session=True)
            self.assertFalse((Path(directory) / 'cache').exists())


class UnattendedModeTests(unittest.TestCase):
    def test_all_six_collectors_use_headless_private_profiles(self):
        from unittest.mock import MagicMock
        from wb_parser import WBWebSource
        from ozon_parser import OzonWebSource
        from instagram_parser import InstagramWebSource
        from youtube_parser import YouTubeWebSource
        from twitch_parser import TwitchWebSource
        from kick_parser import KickWebSource
        cases = [(WBWebSource, 'https://www.wildberries.ru/catalog/123/detail.aspx'),
                 (OzonWebSource, 'https://www.ozon.ru/product/item-123'),
                 (InstagramWebSource, 'https://www.instagram.com/example'),
                 (YouTubeWebSource, 'https://www.youtube.com/@example'),
                 (TwitchWebSource, 'https://www.twitch.tv/example'),
                 (KickWebSource, 'https://kick.com/example')]
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)/'cache'):
            for cls, url in cases:
                with self.subTest(platform=cls.platform):
                    source = cls()
                    engine = MagicMock()
                    context = engine.chromium.launch_persistent_context.return_value
                    page = context.new_page.return_value
                    page.url = url
                    page.goto.return_value.status = 200
                    page.locator.return_value.count.return_value = 0
                    capture = {'url': url, 'text': 'example'}
                    with patch('playwright.sync_api.sync_playwright') as start, patch.object(source, '_capture', return_value=capture), patch('builtins.input', side_effect=AssertionError('prompt forbidden')), contextlib.redirect_stderr(io.StringIO()):
                        start.return_value.__enter__.return_value = engine
                        source.collect(urls=[url], unattended=True)
                    self.assertTrue(engine.chromium.launch_persistent_context.call_args.kwargs['headless'])
                    self.assertTrue(engine.chromium.launch_persistent_context.call_args.kwargs['user_data_dir'].endswith(cls.platform.lower()))
                    context.close.assert_called_once()

    def test_tgstat_unattended_uses_persistent_headless_context(self):
        from unittest.mock import MagicMock
        from tg_parser import TGStatWebSource
        engine = MagicMock()
        context = engine.chromium.launch_persistent_context.return_value
        context.new_page.return_value.evaluate.return_value = {'url': 'https://tgstat.ru/channel/@example/stat', 'text': 'example'}
        source = TGStatWebSource(prompt=MagicMock(side_effect=AssertionError('prompt forbidden')), sleep=lambda _: None, progress=lambda _: None)
        source.unattended = True
        source.keep_session = True
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)/'cache'), patch('playwright.sync_api.sync_playwright') as start, patch.object(source, '_visit'), patch.object(source, '_ensure_login'):
            start.return_value.__enter__.return_value = engine
            self.assertEqual(len(source._browse(['https://tgstat.ru/channel/@example/stat'], None, ('ru',), 1)), 1)
        self.assertTrue(engine.chromium.launch_persistent_context.call_args.kwargs['headless'])
        source.prompt.assert_not_called()
        context.close.assert_called_once()

    def test_expired_tgstat_session_never_prompts(self):
        from unittest.mock import MagicMock
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from tg_parser import TGStatWebSource
        source = TGStatWebSource(prompt=MagicMock(side_effect=AssertionError('prompt forbidden')))
        source.unattended = True
        page = MagicMock()
        page.locator.return_value.wait_for.side_effect = BrowserTimeout('expired')
        with self.assertRaisesRegex(SourceError, 'auth_required'):
            source._ensure_login(page, 'https://tgstat.ru/channel/@example/stat')
        source.prompt.assert_not_called()

    def test_expired_login_cannot_write(self):
        from unittest.mock import MagicMock
        from wb_parser import VisibleBrowserSource
        engine = MagicMock()
        context = engine.chromium.launch_persistent_context.return_value
        page = context.new_page.return_value
        page.url = 'https://www.twitch.tv/login'
        page.goto.return_value.status = 200
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)/'cache'), patch('playwright.sync_api.sync_playwright') as start, patch('builtins.input', side_effect=AssertionError('prompt forbidden')), contextlib.redirect_stderr(io.StringIO()):
            start.return_value.__enter__.return_value = engine
            data = Path(directory)/'storage'
            report = run_source('twitch-web', urls=['https://www.twitch.tv/example'], limit=1, unattended=True, dry_run=False, writer=SafeWriter(data))
            self.assertEqual(report['status'], 'stopped')
            self.assertTrue(report['dry_run'])
            self.assertIn('auth_required', report['source_error']['reason'])
            self.assertFalse(data.exists())

    def test_unattended_rejects_interactive_login_and_import(self):
        for kwargs in ({'interactive_login': True}, {'payload': {'captures': []}}):
            with self.assertRaises(ValueError):
                run_source('twitch-web', unattended=True, **kwargs)


if __name__ == "__main__":
    unittest.main()


class SessionSetupTests(unittest.TestCase):
    def test_seven_platforms_use_only_dedicated_visible_profiles(self):
        from unittest.mock import MagicMock
        from wb_parser import setup_browser_session
        from playwright.sync_api import Error as BrowserError
        for platform in ('tgstat', 'wb', 'ozon', 'instagram', 'youtube', 'twitch', 'kick'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)):
                engine = MagicMock()
                context = engine.chromium.launch_persistent_context.return_value
                page = context.pages[0]
                page.goto.return_value.status = 200
                callbacks = {}
                context.on.side_effect = lambda name, callback: callbacks.update({name: callback})
                def closed(*args):
                    callbacks['close']()
                    raise BrowserError('Target closed')
                page.wait_for_timeout.side_effect = closed
                with patch('playwright.sync_api.sync_playwright') as api:
                    api.return_value.__enter__.return_value = engine
                    report = setup_browser_session(platform)
                params = engine.chromium.launch_persistent_context.call_args.kwargs
                self.assertFalse(params['headless'])
                self.assertEqual(Path(params['user_data_dir']), Path(directory)/'browser_profiles'/platform)
                self.assertEqual(report['session_status'], 'saved_unverified')
                self.assertEqual(report['summary']['total'], 0)
                context.cookies.assert_not_called()
                context.storage_state.assert_not_called()

    def test_unknown_platform_creates_nothing(self):
        from wb_parser import setup_browser_session
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)/'cache'):
            with self.assertRaises(SourceError):
                setup_browser_session('../personal')
            self.assertFalse((Path(directory)/'cache').exists())

    def test_denied_login_page_closes_context_and_never_claims_login(self):
        from unittest.mock import MagicMock
        from wb_parser import setup_browser_session
        engine = MagicMock()
        context = engine.chromium.launch_persistent_context.return_value
        context.pages[0].goto.return_value.status = 403
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)), patch('playwright.sync_api.sync_playwright') as api:
            api.return_value.__enter__.return_value = engine
            with self.assertRaises(SourceError):
                setup_browser_session('tgstat')
            context.close.assert_called_once()

    def test_setup_cli_forbids_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--setup-session', 'tgstat', '--write'])
