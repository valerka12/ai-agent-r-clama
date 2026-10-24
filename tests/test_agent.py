"""Пакетная обработка и CLI без изменения рабочих таблиц."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import main, run_candidates
from safe_writer import SafeWriter, StorageError
from validation import ValidationError


class AgentTests(unittest.TestCase):
    def setUp(self):
        isolation = patch.dict('os.environ', {'ADRESEARCH_GOOGLE_OUTPUT':'0'})
        isolation.start()
        self.addCleanup(isolation.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "storage"
        self.writer = SafeWriter(self.root)
        self.first = {"Название": "Канал", "Ссылка": "https://t.me/example_channel"}
        self.second = {"Ссылка": self.first["Ссылка"], "Тематика": "Игры"}

    def run_batch(self, rows, dry_run=True):
        return run_candidates("telegram", rows, writer=self.writer, dry_run=dry_run)

    def test_preview_sees_earlier_candidates_without_files(self):
        report = self.run_batch([self.first, self.second, self.second])
        self.assertEqual([item["action"] for item in report["results"]], ["add", "update", "skip"])
        self.assertFalse(self.root.exists())

    def test_preview_does_not_change_existing_files(self):
        self.writer.upsert("telegram", self.first, dry_run=False)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = self.run_batch([self.second, self.second])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual([r["action"] for r in report["results"]], ["update", "skip"])

    def test_invalid_candidate_does_not_block_next(self):
        bad = {"Название": "Ошибка", "Ссылка": "https://t.me/invalid_channel", "Подписчики": -1}
        report = self.run_batch([bad, None, self.first, {"Название": "Без ссылки"}])
        self.assertEqual(report["summary"]["rejected"], 2)
        self.assertEqual(report["summary"]["added"], 1)
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["summary"]["remaining"], 0)

    def test_real_batch_matches_preview(self):
        rows = [self.first, self.second, self.second]
        preview = self.run_batch(rows)
        real = self.run_batch(rows, dry_run=False)
        self.assertEqual(preview["summary"], real["summary"])
        self.assertEqual(self.writer.read("telegram")[0]["Тематика"], "Игры")

    def test_storage_failure_stops_batch(self):
        with patch.object(self.writer, "upsert", side_effect=StorageError("journal incomplete")) as upsert:
            report = self.run_batch([self.first, self.second], dry_run=False)
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["remaining"], 1)

    def test_io_failure_stops_batch(self):
        with patch.object(self.writer, "upsert", side_effect=OSError("disk full")):
            report = self.run_batch([self.first, self.second], dry_run=False)
        self.assertEqual(report["status"], "stopped")

    def test_top_level_input_checked_before_write(self):
        for rows in ({}, "string", None):
            with self.subTest(rows=rows):
                with self.assertRaises(ValidationError):
                    self.run_batch(rows, dry_run=False)
        self.assertFalse(self.root.exists())

    def test_manual_fields_and_contacts_remain_protected(self):
        row = dict(self.first, Контакт="@manager", Статус="new", Комментарий="score")
        report = self.run_batch([row], dry_run=False)
        saved = self.writer.read("telegram")[0]
        for field in ("Контакт", "Статус", "Комментарий"):
            self.assertEqual(saved[field], "")
        self.assertEqual(len(report["results"][0]["warnings"]), 3)

    def test_empty_batch(self):
        report = self.run_batch([])
        self.assertEqual(report["summary"]["processed"], 0)
        self.assertFalse(self.root.exists())

    def test_cli_default_is_dry_run(self):
        source = Path(self.temp.name) / "input.json"
        source.write_text(json.dumps([self.first]), encoding="utf-8")
        output = io.StringIO()
        with patch("agent.SafeWriter", return_value=self.writer), contextlib.redirect_stdout(output):
            code = main(["--input", str(source)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        self.assertFalse(self.root.exists())

    def test_cli_write_requires_explicit_flag(self):
        source = Path(self.temp.name) / "input.json"
        source.write_text(json.dumps([self.first]), encoding="utf-8")
        with patch("agent.SafeWriter", return_value=self.writer), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--input", str(source), "--write"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.writer.read("telegram")), 1)

    def test_cli_rejected_exit_code_and_demo_guard(self):
        with patch("agent.SafeWriter", return_value=self.writer), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--demo"]), 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--demo", "--write"])
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
