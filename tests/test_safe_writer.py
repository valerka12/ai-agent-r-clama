"""Проверки на временных данных; запуск: python -m unittest discover -s tests -v."""

import csv
import io
import json
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


class GoogleSheetsStorageTests(unittest.TestCase):
    def setUp(self):
        from safe_writer import GoogleSheetsStore,_atomic_write
        self.temp_google = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_google.cleanup)
        self.google_root = Path(self.temp_google.name)
        self.link = {'spreadsheet_id':'A'*30,'sheet_id':7,'sheet_title':'Площадки'}
        self.store = GoogleSheetsStore(self.google_root)
        _atomic_write(self.store._path('bindings.json'),json.dumps({'version':1,'links':{'telegram':self.link}}).encode())
        self.client = self.FakeClient(self.link,list(config.TELEGRAM_COLUMNS))

    class FakeClient:
        def __init__(self,link,columns):
            self.link=link;self.raw=[columns];self.effective=[columns.copy()]
            self.formats={};self.calls=[];self.error=None;self.snapshot_count=0;self.mutate_at=None
        def snapshot(self,binding,expected=None,display=False):
            from copy import deepcopy
            from safe_writer import _google_snapshot
            self.snapshot_count+=1
            if self.mutate_at==self.snapshot_count:
                self.raw[0]=self.raw[0][::-1]
                self.effective[0]=self.effective[0][::-1]
            result=_google_snapshot(self.link,self.link['sheet_title'],deepcopy(self.raw),deepcopy(self.effective),formats=self.formats.copy())
            if expected is not None and expected!=result['table']:
                raise ValidationError('Wrong schema')
            return result
        def append(self,binding,cells):
            from copy import deepcopy
            self.calls.append(deepcopy(cells))
            if self.error:
                raise self.error
            row=[c.get('userEnteredValue',{}).get('stringValue',c.get('userEnteredValue',{}).get('numberValue','')) for c in cells]
            self.raw.append(row);self.effective.append(row.copy())
            return {}

    def candidate(self,link='https://t.me/new_channel',name='Новый канал'):
        return {'Название':name,'Ссылка':link,'Подписчики':1000,'Средний охват':250,'ERR (%)':25.0}

    def writer(self):
        from safe_writer import GoogleSheetWriter
        return GoogleSheetWriter(self.google_root,client=self.client)

    def test_link_parsing_uses_fixed_google_host_and_explicit_gid(self):
        from safe_writer import GoogleSheetsStore
        result=GoogleSheetsStore.parse_url('https://docs.google.com/spreadsheets/d/'+'A'*30+'/edit?usp=sharing#gid=7')
        self.assertEqual(result,{'spreadsheet_id':'A'*30,'sheet_id':7})
        for url in ('https://evil.test/spreadsheets/d/'+'A'*30+'/edit',
                    'http://docs.google.com/spreadsheets/d/'+'A'*30+'/edit',
                    'https://docs.google.com/spreadsheets/d/'+'A'*30+'/edit#gid=no',
                    'https://docs.google.com/spreadsheets/d/'+'A'*30+'/edit?gid=1#gid=2'):
            with self.subTest(url=url),self.assertRaises(ValidationError):
                GoogleSheetsStore.parse_url(url)

    def test_three_schemas_are_detected_without_changing_column_order(self):
        from safe_writer import _google_snapshot
        for name,columns in [('advertisers',config.ADVERTISER_COLUMNS),('telegram',config.TELEGRAM_COLUMNS),('streamers',config.STREAMER_COLUMNS)]:
            header=list(reversed(columns))
            result=_google_snapshot(self.link,'Лист',[header],[header])
            self.assertEqual(result['table'],name)
            self.assertEqual(result['positions'][columns[0]],len(columns)-1)
            self.assertEqual(result['raw'][0],header)

    def test_ambiguous_missing_and_unknown_headers_are_rejected(self):
        from safe_writer import _google_snapshot
        columns=list(config.TELEGRAM_COLUMNS)
        for header in (columns[:-1],columns+[columns[0]],columns+['Мой столбец'],['Номер','Название']):
            with self.subTest(header=header),self.assertRaises(ValidationError):
                _google_snapshot(self.link,'Лист',[header],[header])

    def test_preview_maps_each_value_to_actual_reordered_column_without_writes(self):
        header=list(reversed(config.TELEGRAM_COLUMNS))
        self.client.raw=[header];self.client.effective=[header.copy()]
        before={p.relative_to(self.google_root):p.read_bytes() for p in self.google_root.rglob('*') if p.is_file()}
        result=self.writer().upsert('telegram',self.candidate(),dry_run=True)
        self.assertEqual(result['action'],'add')
        self.assertEqual(result['target']['row'],2)
        cells={c['field']:c['cell'] for c in result['target']['cells']}
        self.assertEqual(cells['Номер'],'N2')
        self.assertEqual(cells['Название'],'M2')
        self.assertEqual(self.client.calls,[])
        self.assertEqual(before,{p.relative_to(self.google_root):p.read_bytes() for p in self.google_root.rglob('*') if p.is_file()})

    def test_append_keeps_manual_values_and_formulas_and_creates_backup_journal(self):
        from copy import deepcopy
        cols=list(config.TELEGRAM_COLUMNS)
        old=['' for _ in cols]
        old[0]=1;old[1]='Существующий';old[2]='https://t.me/old_channel';old[10]='Мой статус';old[12]='=CONCAT("Ручной"," комментарий")'
        self.client.raw.append(old);self.client.effective.append(old.copy())
        baseline=deepcopy(self.client.raw)
        result=self.writer().upsert('telegram',dict(self.candidate(),Статус='Нельзя записывать',Комментарий='Нельзя',Контакт='@ads'),dry_run=False)
        self.assertEqual(result['action'],'add')
        self.assertEqual(self.client.raw[:-1],baseline)
        self.assertEqual(len(self.client.calls),1)
        cells=self.client.calls[0]
        for field in ('Статус','Комментарий','Рекламодатель','Контакт'):
            self.assertEqual(cells[cols.index(field)],{})
        self.assertEqual(len(list((self.google_root/'backups').rglob('*.csv'))),3)
        self.assertIn('committed',(self.google_root/'logs/changes_log.csv').read_text(encoding='utf-8-sig'))
        self.assertFalse((self.google_root/'data/telegram.csv').exists())

    def test_existing_row_is_never_changed_even_when_empty_auto_fields_exist(self):
        cols=list(config.TELEGRAM_COLUMNS);old=['' for _ in cols]
        old[0]=1;old[1]='Сохранить название';old[2]='https://t.me/new_channel';old[12]='Сохранить комментарий'
        self.client.raw.append(old);self.client.effective.append(old.copy())
        result=self.writer().upsert('telegram',self.candidate(),dry_run=False)
        self.assertEqual(result['action'],'skip')
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.client.raw[1],old)

    def test_percent_format_is_respected_for_new_err_value(self):
        self.client.formats[config.TELEGRAM_COLUMNS.index('ERR (%)')]='PERCENT'
        self.writer().upsert('telegram',self.candidate(),dry_run=False)
        actual=self.client.calls[0][config.TELEGRAM_COLUMNS.index('ERR (%)')]['userEnteredValue']['numberValue']
        self.assertEqual(actual,.25)

    def test_read_change_before_append_aborts_without_remote_write(self):
        self.client.mutate_at=4  # initial read, plan, backup, final re-read
        with self.assertRaises(StorageError):
            self.writer().upsert('telegram',self.candidate(),dry_run=False)
        self.assertEqual(self.client.calls,[])

    def test_uncertain_write_is_not_retried_and_blocks_next_write(self):
        from safe_writer import GoogleRequestError
        self.client.error=GoogleRequestError()
        writer=self.writer()
        with self.assertRaises(StorageError):writer.upsert('telegram',self.candidate(),dry_run=False)
        self.assertEqual(len(self.client.calls),1)
        with self.assertRaises(StorageError):writer.upsert('telegram',self.candidate('https://t.me/other_channel','Другой'),dry_run=False)
        self.assertEqual(len(self.client.calls),1)
        log=(self.google_root/'logs/changes_log.csv').read_text(encoding='utf-8-sig')
        self.assertIn('prepared',log);self.assertNotIn('committed',log)

    def test_known_forbidden_request_is_recorded_as_aborted(self):
        from safe_writer import GoogleRequestError
        self.client.error=GoogleRequestError(403)
        with self.assertRaises(StorageError):self.writer().upsert('telegram',self.candidate(),dry_run=False)
        self.assertIn('aborted',(self.google_root/'logs/changes_log.csv').read_text(encoding='utf-8-sig'))
        self.assertEqual(len(self.client.calls),1)

    def test_array_formulas_prevent_automatic_addition(self):
        cols=list(config.TELEGRAM_COLUMNS);raw=['' for _ in cols];effective=raw.copy()
        raw[0]='=SEQUENCE(10)';effective[0]=1
        raw[1]=effective[1]='Старый канал';raw[2]=effective[2]='https://t.me/old_channel'
        self.client.raw.append(raw);self.client.effective.append(effective)
        with self.assertRaises(ValidationError):self.writer().upsert('telegram',self.candidate(),dry_run=True)
        self.assertEqual(self.client.calls,[])

    def test_binding_change_is_not_silently_redirected(self):
        from safe_writer import _atomic_write
        writer=self.writer()
        changed=self.link|{'sheet_id':9}
        _atomic_write(self.store._path('bindings.json'),json.dumps({'version':1,'links':{'telegram':changed}}).encode())
        with self.assertRaises(StorageError):writer.upsert('telegram',self.candidate(),dry_run=False)
        self.assertEqual(self.client.calls,[])

    def test_preview_batch_uses_google_snapshot_and_virtual_previous_candidates(self):
        from agent import run_candidates
        report=run_candidates('telegram',[self.candidate(),self.candidate('https://t.me/second_channel','Вторая площадка'),self.candidate()],writer=self.writer(),dry_run=True)
        self.assertEqual(report['summary']['added'],2)
        self.assertEqual(report['summary']['skipped'],1)
        self.assertEqual([r['target']['row'] for r in report['results'] if r['action']=='add'],[2,3])
        self.assertEqual(self.client.calls,[])

    def test_attaching_replacement_requires_explicit_intent(self):
        from safe_writer import GoogleSheetsStore
        store=GoogleSheetsStore(self.google_root)
        self.client.link=self.link|{'sheet_id':9}
        with self.assertRaises(ValidationError):
            store.attach('https://docs.google.com/spreadsheets/d/'+'A'*30+'/edit#gid=9',client=self.client)
        self.assertEqual(store.bindings()['telegram']['sheet_id'],7)
        result=store.attach('https://docs.google.com/spreadsheets/d/'+'A'*30+'/edit#gid=9',replace=True,client=self.client)
        self.assertEqual(result['binding']['sheet_id'],9)
        self.assertEqual(self.client.calls,[])

    def test_external_advertiser_template_maps_without_changing_25_columns(self):
        from safe_writer import _ADVERTISER_EXTERNAL_HEADERS, _google_snapshot, _atomic_write
        from copy import deepcopy
        header=list(_ADVERTISER_EXTERNAL_HEADERS)
        old=['' for _ in header]
        for field,value in {'№':1,'ID клиента':'C000001','Название компании':'Старая компания',
                            'Сайт':'https://old.example.org','Тelegram':'@keep',
                            'Email':'keep@example.org','Менеджер (кто пишет)':'Не менять',
                            'Комментарий':'=1+1'}.items():
            old[header.index(field)]=value
        self.client.raw=[header,old];self.client.effective=deepcopy(self.client.raw)
        _atomic_write(self.store._path('bindings.json'),json.dumps({'version':1,'links':{'advertisers':self.link}}).encode())
        snapshot=_google_snapshot(self.link,'Рекламодатели',self.client.raw,self.client.effective)
        self.assertEqual(snapshot['table'],'advertisers')
        self.assertEqual(snapshot['positions']['Страна'],10)
        self.assertEqual(snapshot['positions']['Номер'],0)
        self.assertNotIn('Telegram/почта',snapshot['positions'])
        self.assertEqual(len(snapshot['rows'][0]),22)
        before=deepcopy(self.client.raw)
        candidate={'Название компании':'Новая компания','Сайт':'https://new.example.org',
                   'Страна':'Беларусь','Комментарий':'Нельзя','Telegram/почта':'Нельзя'}
        writer=self.writer()
        preview=writer.upsert('advertisers',candidate,dry_run=True)
        targets={item['field']:item['cell'] for item in preview['target']['cells']}
        self.assertEqual(targets['Страна'],'K3')
        result=writer.upsert('advertisers',candidate,dry_run=False)
        self.assertEqual(result['action'],'add')
        self.assertEqual(self.client.raw[:2],before)
        self.assertEqual(len(self.client.calls[0]),25)
        self.assertEqual(self.client.calls[0][10]['userEnteredValue']['stringValue'],'Беларусь')
        for field in ('Тelegram','Email','Менеджер (кто пишет)','Комментарий','Должность'):
            self.assertEqual(self.client.calls[0][header.index(field)],{})

    def test_external_template_is_exact_and_does_not_guess_arbitrary_headers(self):
        from safe_writer import _ADVERTISER_EXTERNAL_HEADERS, _google_snapshot
        header=list(reversed(_ADVERTISER_EXTERNAL_HEADERS))
        result=_google_snapshot(self.link,'Лист',[header],[header])
        self.assertEqual(result['positions']['Номер'],24)
        for changed in (header[:-1],header+['Другое'],[h.replace('Cтрана','Страна') for h in header]):
            with self.assertRaises(ValidationError):
                _google_snapshot(self.link,'Лист',[changed],[changed])

    def test_external_manual_only_unnumbered_row_blocks_write(self):
        from safe_writer import _ADVERTISER_EXTERNAL_HEADERS, _google_snapshot
        header=list(_ADVERTISER_EXTERNAL_HEADERS);row=['' for _ in header]
        row[header.index('Менеджер (кто пишет)')]='Есть ручная запись'
        with self.assertRaises(ValidationError):
            _google_snapshot(self.link,'Лист',[header,row],[header,row])

    def test_unknown_credentials_are_rejected_without_disclosing_key(self):
        from safe_writer import GoogleSheetsStore
        store=GoogleSheetsStore(self.google_root)
        for value in ({'private_key':'SECRET'}, {'type':'service_account','client_email':'x@x.iam.gserviceaccount.com','token_uri':'https://evil.test/token','private_key':'SECRET'}):
            with self.assertRaises(ValidationError) as context:store.save_credentials(value)
            self.assertNotIn('SECRET',str(context.exception))
        self.assertFalse(store._path('service_account.json').exists())


class GoogleSheetsWireTests(unittest.TestCase):
    def test_append_uses_only_new_rows_and_value_field_mask(self):
        from safe_writer import GoogleSheetsAPI
        from unittest.mock import Mock
        client = GoogleSheetsAPI.__new__(GoogleSheetsAPI)
        client._call = Mock(return_value={})
        cells = [{'userEnteredValue': {'stringValue': '=literal text'}}, {}]
        client.append({'spreadsheet_id': 'A'*30, 'sheet_id': 7}, cells)
        client._call.assert_called_once_with('POST', 'A'*30, ':batchUpdate', body={
            'requests': [{'appendCells': {'sheetId': 7, 'rows': [{'values': cells}],
                                         'fields': 'userEnteredValue'}}]})

    def test_http_failure_does_not_retry_or_leak_response(self):
        from safe_writer import GoogleSheetsAPI, GoogleRequestError
        from unittest.mock import Mock, MagicMock
        client = GoogleSheetsAPI.__new__(GoogleSheetsAPI)
        client.last_request = 0
        client.session = Mock()
        response = MagicMock(status_code=403)
        response.__enter__.return_value = response
        client.session.request.return_value = response
        with self.assertRaises(GoogleRequestError) as context:
            client._call('POST', 'A'*30, ':batchUpdate', body={})
        client.session.request.assert_called_once()
        response.iter_content.assert_not_called()
        self.assertEqual(context.exception.status, 403)


class GoogleCredentialFileTests(unittest.TestCase):
    def test_valid_generated_key_stays_private_and_out_of_status(self):
        import os
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from safe_writer import GoogleSheetsStore
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        info = {'type': 'service_account', 'client_email': 'test@example.iam.gserviceaccount.com',
                'token_uri': 'https://oauth2.googleapis.com/token', 'private_key': pem}
        with tempfile.TemporaryDirectory() as root:
            store = GoogleSheetsStore(root)
            status = store.save_credentials(info)
            self.assertTrue(status['configured'])
            self.assertNotIn(pem, json.dumps(status))
            self.assertEqual(store.credentials()['private_key'], pem)
            if os.name == 'posix':
                self.assertEqual(store._path('service_account.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)
