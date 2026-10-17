"""Проверки на временных данных; запуск: python -m unittest discover -s tests -v."""

import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from dedup import find_matches
from safe_writer import SafeWriter, StorageError
from validation import ValidationError


class SafeWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "isolated"
        self.writer = SafeWriter(self.root)
        self.candidate = {"Название": "Канал", "Ссылка": "https://t.me/example_channel",
                          "Подписчики": 1000, "Средний охват": 250, "ERR (%)": 25.0}

    def save(self, candidate=None, **kwargs):
        return self.writer.upsert("telegram", candidate if candidate is not None else self.candidate,
                                  dry_run=False, **kwargs)

    def seed(self, table, row):
        columns = {"telegram": config.TELEGRAM_COLUMNS,
                   "advertisers": config.ADVERTISER_COLUMNS,
                   "streamers": config.STREAMER_COLUMNS}[table]
        path = self.root / "data" / f"{table}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding=config.CSV_ENCODING, newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in columns})
        return path

    def test_dry_run_creates_nothing(self):
        result = self.writer.upsert("telegram", self.candidate)
        self.assertEqual(result["action"], "add")
        self.assertFalse(self.root.exists())

    def test_add_backup_journal(self):
        self.assertEqual(self.save()["action"], "add")
        self.assertEqual(self.writer.read("telegram")[0]["Подписчики"], "1000")
        self.assertEqual(len(list((self.root / "backups").glob("*/*.csv"))), 3)
        with (self.root / "logs" / "changes_log.csv").open(encoding=config.CSV_ENCODING) as stream:
            states = {row["state"] for row in csv.DictReader(stream)}
        self.assertEqual(states, {"prepared", "committed"})

    def test_duplicate_no_overwrite(self):
        self.save()
        path = self.root / "data" / "telegram.csv"
        before = path.read_bytes()
        candidate = dict(self.candidate, Подписчики=2000)
        self.assertEqual(self.save(candidate)["action"], "skip")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.writer.read("telegram")), 1)

    def test_preserve_manual_and_formula_and_zero(self):
        self.seed("telegram", {"Номер": "1", "Ссылка": self.candidate["Ссылка"],
                               "Название": "Канал", "Подписчики": "0",
                               "Комментарий": "=SUM(A1:A2)", "Статус": "ручной"})
        result = self.save({"Ссылка": self.candidate["Ссылка"], "Подписчики": 100,
                            "Статус": "новый", "Комментарий": "другой", "Тематика": "Игры"})
        row = self.writer.read("telegram")[0]
        self.assertEqual(result["action"], "update")
        self.assertEqual(row["Подписчики"], "0")
        self.assertEqual(row["Комментарий"], "=SUM(A1:A2)")
        self.assertEqual(row["Статус"], "ручной")
        self.assertEqual(row["Тематика"], "Игры")

    def test_manual_not_inserted(self):
        result = self.save(dict(self.candidate, Статус="new", Комментарий="score"))
        self.assertEqual(self.writer.read("telegram")[0]["Статус"], "")
        self.assertTrue(result["warnings"])

    def test_contact_needs_confirmation(self):
        self.save(dict(self.candidate, Контакт="@manager"))
        self.assertEqual(self.writer.read("telegram")[0]["Контакт"], "")
        self.save({"Ссылка": self.candidate["Ссылка"], "Контакт": "@manager"},
                  confirmed_fields={"Контакт"})
        self.assertEqual(self.writer.read("telegram")[0]["Контакт"], "@manager")

    def test_bad_headers_unchanged(self):
        path = self.root / "data" / "telegram.csv"
        path.parent.mkdir(parents=True)
        path.write_text("Неверная,схема\n", encoding=config.CSV_ENCODING)
        before = path.read_bytes()
        with self.assertRaises(StorageError):
            self.save()
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_and_invalid_values(self):
        for field, value in (("Несуществующее", "x"), ("Подписчики", True),
                             ("ERR (%)", 101), ("Название", "=CMD()")):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    self.writer.upsert("telegram", dict(self.candidate, **{field: value}))
        self.assertFalse(self.root.exists())

    def test_inconsistent_err(self):
        with self.assertRaises(ValidationError):
            self.writer.upsert("telegram", dict(self.candidate, **{"ERR (%)": 50}))

    def test_unknown_key_and_similar_name(self):
        self.assertEqual(self.writer.upsert("advertisers", {"Название компании": "Бренд"})["action"], "skip")
        self.save()
        other = dict(self.candidate, Ссылка="https://t.me/another_channel")
        self.assertEqual(self.save(other)["action"], "skip")
        self.assertEqual(len(self.writer.read("telegram")), 1)

    def test_none_keys_not_duplicates(self):
        matches = find_matches("advertisers", {}, [{}])
        self.assertIsNone(matches["key"])
        self.assertEqual(matches["exact"], [])
        self.assertEqual(matches["review"], [0])

    def test_lock(self):
        self.root.mkdir()
        lock = self.root / ".safe_writer.lock"
        lock.write_text("other process")
        with self.assertRaises(StorageError):
            self.save()
        self.assertTrue(lock.exists())

    def test_write_failure_preserves_original_and_blocks_retry(self):
        self.save()
        path = self.root / "data" / "telegram.csv"
        original = path.read_bytes()
        import safe_writer
        actual = safe_writer._atomic_write

        def fail_table(target, raw):
            if target == path:
                raise OSError("simulated failure")
            return actual(target, raw)

        candidate = {"Ссылка": self.candidate["Ссылка"], "Тематика": "Игры"}
        with patch("safe_writer._atomic_write", side_effect=fail_table):
            with self.assertRaises(OSError):
                self.save(candidate)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / ".safe_writer.lock").exists())
        with self.assertRaises(StorageError):
            self.save(candidate)

    def test_backup_failure_prevents_write(self):
        with patch("safe_writer._atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save()
        self.assertFalse((self.root / "data" / "telegram.csv").exists())

    def test_advertiser_ids_and_platforms(self):
        company = {"Название компании": "Бренд", "Сайт": "https://example.com"}
        self.writer.upsert("advertisers", company, dry_run=False)
        row = self.writer.read("advertisers")[0]
        self.assertEqual(row["Номер"], "1")
        self.assertEqual(len(row["ID клиента"]), 32)
        for platform in ("Twitch", "Kick"):
            self.writer.upsert("streamers", {"Стример": "player_one",
                                            "Платформа (Twitch/Kick)": platform}, dry_run=False)
        self.assertEqual(len(self.writer.read("streamers")), 2)

    def test_multiple_exact_matches_blocked(self):
        path = self.seed("telegram", {"Номер": "1", **self.candidate})
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=config.TELEGRAM_COLUMNS)
            writer.writerow({"Номер": "2", **self.candidate})
        original = path.read_bytes()
        self.assertEqual(self.save()["action"], "skip")
        self.assertEqual(path.read_bytes(), original)

    def test_manual_edit_between_read_and_replace(self):
        self.save()
        path = self.root / "data" / "telegram.csv"
        journal = self.writer._journal

        def concurrent_edit(transaction, state, table, result):
            journal(transaction, state, table, result)
            if state == "prepared":
                path.write_bytes(path.read_bytes().replace("Канал".encode(), "Ручная правка".encode()))

        with patch.object(self.writer, "_journal", side_effect=concurrent_edit):
            with self.assertRaises(StorageError):
                self.save({"Ссылка": self.candidate["Ссылка"], "Тематика": "Игры"})
        self.assertEqual(self.writer.read("telegram")[0]["Название"], "Ручная правка")
        self.writer._check_journal()

    def test_committed_log_failure_blocks_next_write(self):
        journal = self.writer._journal

        def fail_commit(transaction, state, table, result):
            if state == "committed":
                raise OSError("log unavailable")
            journal(transaction, state, table, result)

        with patch.object(self.writer, "_journal", side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(len(self.writer.read("telegram")), 1)
        with self.assertRaises(StorageError):
            self.save()

    def test_unknown_table_and_false_confirmation(self):
        with self.assertRaises(ValidationError):
            self.writer.upsert("unknown", {})
        with self.assertRaises(ValidationError):
            self.writer.upsert("telegram", self.candidate, confirmed_fields={"Статус"})

    def test_original_bytes_in_backup(self):
        path = self.seed("telegram", {"Номер": "1", "Ссылка": self.candidate["Ссылка"]})
        original = path.read_bytes()
        self.save({"Ссылка": self.candidate["Ссылка"], "Тематика": "Игры"})
        snapshots = list((self.root / "backups").glob("*/telegram.csv"))
        self.assertEqual(snapshots[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
