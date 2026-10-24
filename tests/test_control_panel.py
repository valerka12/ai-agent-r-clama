"""Границы команд, локальный доступ и состояния панели."""
import http.client
import io
import json
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from control_panel import Controller, build_command, make_server


class PanelCommandTests(unittest.TestCase):
    def options(self, **extra):
        return dict(source='twitch-web', countries=['ru', 'by'], limit=2, **extra)

    def test_default_is_unattended_dry_run(self):
        args = build_command(self.options())
        self.assertIn('--dry-run', args)
        self.assertIn('--unattended', args)
        self.assertIn('--discover', args)
        self.assertNotIn('--write', args)

    def test_windows_background_launcher_uses_console_interpreter_for_worker(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'executable', 'C:/Python/pythonw.exe'):
            args = build_command(self.options())
        self.assertEqual(args[0].replace('\\', '/'), 'C:/Python/python.exe')

    def test_write_requires_boolean_and_explicit_selection(self):
        self.assertIn('--write', build_command(self.options(write=True)))
        with self.assertRaises(ValueError):
            build_command(self.options(write='true'))

    def test_bad_source_country_limit_and_injection_rejected(self):
        for changes in ({'source': 'shell'}, {'source': []}, {'source': {}}, {'countries': ['us']}, {'limit': True}, {'limit': 100}, {'seed': 'https://www.twitch.tv/example;rm'}):
            value = self.options()
            value.update(changes)
            with self.assertRaises(ValueError):
                build_command(value)

    def test_tg_category_and_channels_do_not_mix(self):
        value = dict(source='tgstat-web', countries=['ru'], limit=1, category='tech', seed='@kod_ru')
        with self.assertRaises(ValueError):
            build_command(value)
        value['seed'] = ''
        self.assertIn('--category', build_command(value))

    def test_profiles_required_for_instagram(self):
        value = self.options()
        value['source'] = 'instagram-web'
        with self.assertRaises(ValueError):
            build_command(value)


class PanelHTTPTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cache = patch('config.CACHE_DIR', Path(temporary.name)/'cache')
        cache.start()
        self.addCleanup(cache.stop)
        self.controller = Controller(report_dir=False)
        self.server = make_server(self.controller, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method='GET', body=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers)
        result = connection.getresponse()
        status, raw = result.status, result.read().decode()
        connection.close()
        return status, raw

    def test_page_and_token_protected_status(self):
        status, html = self.request('/')
        self.assertEqual(status, 200)
        import re
        token = re.search(r'token="([A-Za-z0-9_-]+)"', html)[1]
        self.assertEqual(self.request('/api/status')[0], 403)
        status, raw = self.request('/api/status', **{'X-Control-Token': token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'idle')

    def test_foreign_host_and_origin_rejected(self):
        self.assertEqual(self.request('/', Host='evil.example')[0], 403)
        self.assertEqual(self.request('/', Origin='https://evil.example')[0], 403)

    def test_post_without_token_never_starts_process(self):
        with patch('control_panel.subprocess.Popen') as spawn:
            self.assertEqual(self.request('/api/start', 'POST', body='{}', **{'Content-Type': 'application/json'})[0], 403)
            spawn.assert_not_called()


    def test_tables_require_local_token_and_do_not_create_storage(self):
        import tempfile,re
        from pathlib import Path
        from safe_writer import SafeWriter
        _,html = self.request('/')
        token = re.search(r'token="([A-Za-z0-9_-]+)"',html)[1]
        self.assertEqual(self.request('/api/tables/telegram')[0],403)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'storage'
            storage = SafeWriter(root)
            with patch('safe_writer.SafeWriter',return_value=storage):
                status,raw = self.request('/api/tables/telegram',**{'X-Control-Token':token})
                self.assertEqual(status,200)
                result = json.loads(raw)
                self.assertEqual(len(result['columns']),14)
                self.assertEqual(result['rows'],[])
                self.assertEqual(self.request('/api/tables/unknown',**{'X-Control-Token':token})[0],400)
            self.assertFalse(root.exists())


class PanelControllerTests(unittest.TestCase):
    def test_process_runs_without_shell_and_report_is_collected(self):
        process = MagicMock()
        process.stdout = io.BytesIO(b'{"status":"completed","summary":{"total":0}}')
        process.stderr = io.BytesIO(b'')
        process.returncode = 0
        controller = Controller(report_dir=False)
        with patch('control_panel.subprocess.Popen', return_value=process) as spawn:
            controller.start(dict(source='twitch-web', countries=['ru'], limit=1))
            for _ in range(100):
                if controller.snapshot()['status'] != 'running':
                    break
                time.sleep(.01)
        self.assertEqual(controller.snapshot()['status'], 'completed')
        self.assertEqual(controller.snapshot()['report']['summary']['total'], 0)
        self.assertNotIn('shell', spawn.call_args.kwargs)
        self.assertIsInstance(spawn.call_args.args[0], list)
        self.assertEqual(spawn.call_args.kwargs['env']['PYTHONIOENCODING'], 'utf-8')

    def test_session_setup_does_not_collect_or_write(self):
        process = MagicMock()
        process.stdout = io.BytesIO(b'{"status":"completed","session_status":"saved_unverified"}')
        process.stderr = io.BytesIO(b'')
        process.returncode = 0
        controller = Controller(report_dir=False)
        with patch('control_panel.subprocess.Popen', return_value=process) as spawn:
            controller.start({'source': 'tgstat-web'}, session_setup=True)
            for _ in range(100):
                if controller.snapshot()['status'] != 'running':
                    break
                time.sleep(.01)
        args = spawn.call_args.args[0]
        self.assertEqual(args[3:], ['--setup-session', 'tgstat'])
        self.assertNotIn('--write', args)
        self.assertNotIn('--source', args)

    def test_parallel_start_rejected(self):
        controller = Controller(report_dir=False)
        controller.state['status'] = 'running'
        with self.assertRaises(ValueError):
            controller.start(dict(source='twitch-web', countries=['ru'], limit=1))


class FailureExplanationTests(unittest.TestCase):
    def test_access_denial_is_visible_and_does_not_promise_login_will_fix_it(self):
        from control_panel import explain_failure
        problem = explain_failure('Источник недоступен: HTTP 403')
        self.assertEqual(problem['title'], 'Сайт запретил доступ')
        self.assertIn('пока недоступен', problem['message'])

    def test_missing_session_has_actionable_instruction(self):
        from control_panel import explain_failure
        self.assertIn('Войти в аккаунт', explain_failure('auth_required')['message'])
        self.assertIsNone(explain_failure(''))

    def test_partial_source_error_appears_in_status(self):
        controller = Controller(report_dir=False)
        controller.state.update(report={'source_error': {'reason': 'HTTP 403'}})
        self.assertEqual(controller.snapshot()['problem']['title'], 'Сайт запретил доступ')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from control_panel import Scheduler
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'cache' / 'autonomy.sqlite3'
        self.now = 1000.0
        self.controller = MagicMock()
        self.state = {'status': 'idle', 'report': None, 'exit_code': None, 'error': ''}
        self.controller.snapshot.side_effect = lambda: dict(self.state)
        self.scheduler = Scheduler(self.controller, self.path, clock=lambda: self.now)
        self.options = dict(source='tgstat-web', countries=['ru', 'by'], limit=1, category='tech')

    def tearDown(self):
        self.scheduler.close()
        self.temp.cleanup()

    def complete(self, **changes):
        self.state.update(status='completed', exit_code=0,
                          report={'status': 'completed', 'summary': {'failed': 0}})
        self.state.update(changes)

    def test_read_empty_schedule_creates_no_files(self):
        self.assertEqual(self.scheduler.jobs(), [])
        self.scheduler.tick()
        self.assertFalse(self.path.exists())

    def test_invalid_task_never_creates_database(self):
        for interval in (True, 1, 604801, '300'):
            with self.assertRaises(ValueError):
                self.scheduler.add(self.options, interval)
        with self.assertRaises(ValueError):
            self.scheduler.add(dict(self.options, secret='not stored'), 300)
        self.assertFalse(self.path.exists())

    def test_saved_task_survives_restart_and_defaults_to_dry_run(self):
        from control_panel import Scheduler
        identifier = self.scheduler.add(self.options, 300)
        other = Scheduler(self.controller, self.path)
        saved = other.jobs()[0]
        self.assertEqual(saved['id'], identifier)
        self.assertNotIn('write', saved['options'])
        self.assertIn('--dry-run', build_command(saved['options']))
        self.assertEqual(saved['options']['countries'], ['ru', 'by'])

    def test_completed_task_waits_full_interval_without_catchup(self):
        self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        self.controller.start.assert_called_once_with(self.options)
        self.complete()
        self.now = 1100
        self.scheduler.tick()
        saved = self.scheduler.jobs()[0]
        self.assertEqual(saved['status'], 'waiting')
        self.assertEqual(saved['next_run'], 1400)
        self.scheduler.tick()
        self.assertEqual(self.controller.start.call_count, 1)
        self.now = 1400
        self.scheduler.tick()
        self.assertEqual(self.controller.start.call_count, 2)

    def test_two_service_instances_cannot_claim_same_job(self):
        from control_panel import Scheduler
        self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        other_controller = MagicMock()
        other = Scheduler(other_controller, self.path, clock=lambda: self.now)
        other.tick()
        other_controller.start.assert_not_called()
        self.assertEqual(self.scheduler.jobs()[0]['status'], 'running')
        other.close()

    def test_crashed_inflight_write_is_paused_and_not_replayed(self):
        from control_panel import Scheduler
        self.scheduler.add(dict(self.options, write=True), 300)
        self.scheduler.tick()
        # Первый процесс исчез; lease истёк. Его результат записи неизвестен.
        self.now += 121
        other_controller = MagicMock()
        other_controller.snapshot.return_value = dict(self.state)
        other = Scheduler(other_controller, self.path, clock=lambda: self.now)
        other.tick()
        other_controller.start.assert_not_called()
        saved = other.jobs()[0]
        self.assertEqual(saved['status'], 'attention')
        self.assertFalse(saved['enabled'])
        self.assertIn('бэкапы', saved['error'])
        other.close()

    def test_auth_failure_pauses_same_source_but_other_platform_continues(self):
        failed = self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        next_id = self.scheduler.add(dict(self.options, category='games'), 300)
        other_id = self.scheduler.add(dict(source='twitch-web', countries=['ru'], limit=1), 300)
        self.state.update(status='attention', exit_code=1, error='auth_required', report=None)
        self.scheduler.tick()
        saved = {row['id']: row for row in self.scheduler.jobs()}
        self.assertFalse(saved[failed]['enabled'])
        self.assertEqual(saved[failed]['error'], 'auth_required')
        self.assertEqual(saved[next_id]['status'], 'attention')
        self.assertFalse(saved[next_id]['enabled'])
        self.assertEqual(saved[other_id]['status'], 'running')
        self.assertEqual(self.controller.start.call_count, 2)

    def test_partial_http_denial_pauses_other_saved_tasks_of_same_source(self):
        self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        pending = self.scheduler.add(dict(self.options, category='games'), 300)
        self.complete(status='attention', exit_code=1,
                      report={'status': 'stopped', 'summary': {'failed': 0},
                              'source_error': {'reason': 'HTTP 403'}})
        self.scheduler.tick()
        saved = {row['id']: row for row in self.scheduler.jobs()}
        self.assertFalse(saved[pending]['enabled'])
        self.assertEqual(self.controller.start.call_count, 1)

    def test_access_classifier_does_not_treat_arbitrary_numbers_as_status(self):
        from control_panel import source_access_blocked
        for text in ('HTTP 403', 'HTTP 429', 'auth_required', 'На странице проверка доступа.'):
            self.assertTrue(source_access_blocked(text), text)
        for text in ('Строка 403 повреждена', 'HTTP 4030', 'HTTP 500', '', None):
            self.assertFalse(source_access_blocked(text), text)

    def test_storage_failure_never_retries_even_with_success_exit_code(self):
        self.scheduler.add(dict(self.options, write=True), 300)
        self.scheduler.tick()
        self.complete(report={'status': 'stopped', 'summary': {'failed': 1}})
        self.scheduler.tick()
        self.now += 10000
        self.scheduler.tick()
        self.assertFalse(self.scheduler.jobs()[0]['enabled'])
        self.assertEqual(self.controller.start.call_count, 1)

    def test_quality_rejection_is_successful_run_not_operational_failure(self):
        self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        self.complete(status='attention', exit_code=2)
        self.scheduler.tick()
        self.assertTrue(self.scheduler.jobs()[0]['enabled'])
        self.assertEqual(self.scheduler.jobs()[0]['status'], 'waiting')

    def test_manual_running_task_defers_schedule(self):
        self.scheduler.add(self.options, 300)
        self.state['status'] = 'running'
        self.scheduler.tick()
        self.controller.start.assert_not_called()
        self.assertEqual(self.scheduler.jobs()[0]['status'], 'queued')

    def test_pause_and_resume_require_explicit_operator_action(self):
        identifier = self.scheduler.add(self.options, 300)
        self.scheduler.set_enabled(identifier, False)
        self.scheduler.tick()
        self.controller.start.assert_not_called()
        self.scheduler.set_enabled(identifier, True)
        self.scheduler.tick()
        self.controller.start.assert_called_once()
        with self.assertRaises(ValueError):
            self.scheduler.set_enabled(identifier, False)

    def test_process_spawn_failure_disables_task(self):
        self.scheduler.add(self.options, 300)
        self.controller.start.side_effect = OSError('private detail')
        self.scheduler.tick()
        saved = self.scheduler.jobs()[0]
        self.assertFalse(saved['enabled'])
        self.assertNotIn('private detail', saved['error'])

    def test_shutdown_keeps_inflight_marker_for_safe_recovery(self):
        self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        self.scheduler.close()
        self.controller.stop.assert_called_once()
        self.assertEqual(self.scheduler.jobs()[0]['status'], 'running')
        self.scheduler.active = None

    def test_close_waits_for_concurrent_spawn_then_stops_process(self):
        self.scheduler.add(self.options, 300)
        entered, release = threading.Event(), threading.Event()
        def spawn(options):
            entered.set()
            release.wait(3)
        self.controller.start.side_effect = spawn
        worker = threading.Thread(target=self.scheduler.tick)
        worker.start()
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=self.scheduler.close)
        closer.start()
        self.assertTrue(closer.is_alive())
        release.set()
        worker.join(3)
        closer.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.controller.stop.assert_called_once()
        self.assertEqual(self.scheduler.jobs()[0]['status'], 'running')
        self.scheduler.active = None

    def test_cli_service_dispatch_has_no_browser_or_table_write(self):
        from agent import main
        with patch('control_panel.service_main', return_value=0) as run:
            self.assertEqual(main(['--service']), 0)
            run.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()


class ExplicitStartTests(unittest.TestCase):
    def test_enter_in_form_does_not_start_search_but_start_button_does(self):
        import shutil
        import subprocess
        from control_panel import HTML
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node нужен только для проверки событий интерфейса')
        script = HTML.split('<script>')[1].split('</script>')[0].replace('__TOKEN__', '"test"')
        harness = r"""
const assert=require('node:assert/strict');
const nodes={};function node(){return {value:'',checked:false,options:[],replaceChildren(){},append(){},closest(){return {}},reportValidity(){return true}};}
global.document={getElementById(id){return nodes[id]??=(node());},createElement:node};
for(const [id,value] of Object.entries({task:'telegram',source:'tgstat-web',topic:'tech',limit:'1',interval:'60'}))document.getElementById(id).value=value;
document.getElementById('ru').checked=true;
let starts=0;global.fetch=async(path,opts)=>{if(path==='/api/start')starts++;return {ok:true,json:async()=>path==='/api/jobs'?{jobs:[]}:{status:'idle'}}};
global.setInterval=()=>{};
"""
        verification = r"""
(async()=>{await document.getElementById('form').onsubmit({preventDefault(){}});assert.equal(starts,0);await document.getElementById('start').onclick({preventDefault(){}});assert.equal(starts,1);
const exported=tableCSV({columns:['Колонка'],rows:[{'Колонка':'a,"b"'+String.fromCharCode(10)+'строка'}]});assert.equal(exported.charCodeAt(0),65279);assert.ok(exported.includes('""b""'));assert.ok(exported.endsWith(String.fromCharCode(13,10)));
assert.deepEqual(suggestedContacts({contact_suggestions:[],enrichment:{contact_suggestions:['sales@example.org']}}),['sales@example.org']);
assert.ok(reviewPlanText({action:'update',changes:[{field:'Контакт',before:'',after:'@manager'}]}).includes('Контакт: [пусто] → @manager'));
assert.equal(candidateLink('javascript:alert(1)'),null);
assert.equal(candidateLink('https://user:password@example.com'),null);
assert.equal(candidateLink('not a link'),null);
assert.equal(candidateLink('https://t.me/kod_ru'),'https://t.me/kod_ru');
assert.equal(document.getElementById('download-report').disabled,true);
let download=null,revoked=null;
global.Blob=class{constructor(parts,options){this.parts=parts;this.options=options;}};
URL.createObjectURL=blob=>{assert.deepEqual(JSON.parse(blob.parts[0]),{source:'telegram-public',decisions:[]});return 'blob:test';};
URL.revokeObjectURL=url=>{revoked=url;};global.setTimeout=fn=>fn();
const originalCreate=document.createElement;document.createElement=tag=>tag==='a'?{click(){download={href:this.href,name:this.download};}}:originalCreate(tag);
latestReport={source:'telegram-public',decisions:[]};document.getElementById('download-report').onclick();
assert.deepEqual(download,{href:'blob:test',name:'agent-report.json'});assert.equal(revoked,'blob:test');
document.getElementById('task').value='advertisers';document.getElementById('source').value='wb-web';
const fileFacts={'https://www.wildberries.ru/catalog/123/detail.aspx':{brand:{value:'Example',operator_verified:true}}};
document.getElementById('proof-file').files=[{size:100,text:async()=>JSON.stringify(fileFacts)}];
await document.getElementById('proof-file').onchange();assert.deepEqual(uploadedFacts,fileFacts);assert.equal(document.getElementById('facts-verified').checked,false);
document.getElementById('facts-verified').checked=true;assert.deepEqual(options().proofs,fileFacts);clearFacts();assert.equal(uploadedFacts,null);
const reviewRequests=[];global.fetch=async(path,opts)=>{reviewRequests.push([path,JSON.parse(opts.body)]);return {ok:true,json:async()=>path.endsWith('/preview')?{token:'review-token',plan:{action:'add'}}:{saved:true,result:{action:'add'}}};};
openReview(0,{candidate:{'Название':'Канал'},contact_suggestions:['@ad_manager']});
for(const [id,value] of Object.entries({'review-country':'ru','review-country-url':'https://example.org/geo','review-reach':'500','review-reach-url':'https://example.org/stat','review-placement-url':'https://t.me/example_channel','review-contact':'@ad_manager'}))document.getElementById(id).value=value;
document.getElementById('review-verified').checked=true;
await document.getElementById('review-preview').onclick();assert.equal(document.getElementById('review-save').disabled,false);
assert.equal(reviewRequests[0][0],'/api/review/preview');assert.equal(reviewRequests[0][1].reach,500);
document.getElementById('review-reach').oninput();assert.equal(document.getElementById('review-save').disabled,true);
await document.getElementById('review-preview').onclick();await document.getElementById('review-save').onclick();
assert.deepEqual(reviewRequests[2],['/api/review/commit',{token:'review-token'}]);assert.equal(document.getElementById('review-save').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run([node, '-e', harness+script+verification], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class PublicSearchPanelTests(unittest.TestCase):
    def test_public_query_needs_no_session_and_is_dry_run(self):
        args = build_command(dict(source='telegram-public',countries=['ru','by'],limit=2,query='Беларусь'))
        self.assertIn('--query', args)
        self.assertIn('Беларусь', args)
        self.assertIn('--unattended', args)
        self.assertIn('--dry-run', args)
        self.assertNotIn('--keep-session', args)

    def test_public_write_and_login_are_rejected(self):
        with self.assertRaises(ValueError):
            build_command(dict(source='telegram-public',countries=['ru'],limit=1,write=True))
        with self.assertRaises(ValueError):
            Controller(report_dir=False).start({'source':'telegram-public'},session_setup=True)

    def test_direct_channels_take_precedence_over_query(self):
        args = build_command(dict(source='telegram-public',countries=['by'],limit=1,query='Беларусь',seed='@devby'))
        self.assertIn('--channels', args)
        self.assertNotIn('--query', args)


class BrowserProblemPanelTests(unittest.TestCase):
    def test_browser_start_failure_does_not_request_site_login(self):
        from control_panel import explain_failure
        reason = 'Chromium не установлен. Переустановите полную сборку.'
        problem = explain_failure('Источник недоступен: browser_unavailable: ' + reason)
        self.assertEqual(problem['title'], 'Браузер не запустился')
        self.assertEqual(problem['message'], reason)
        self.assertNotIn('Войти в аккаунт', problem['message'])


class TelegramOperatorReviewTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from datetime import datetime, timezone
        from safe_writer import SafeWriter
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'storage'
        self.writer = SafeWriter(self.root)
        self.controller = Controller(report_dir=False)
        self.candidate = {'Название':'Проверяемый канал','Ссылка':'https://t.me/example_channel',
                          'Подписчики':2000,'Последнее обновление':datetime.now(timezone.utc).isoformat()}
        self.controller.state = {'status':'completed','report':{'source':'telegram-public','decisions':[
            {'decision':'review','candidate':self.candidate,'evidence':{'requested_countries':['ru']},
             'placement':{'status':'offered'},'contact_suggestions':['@ad_manager']} ]}}
        self.body = {'index':0,'candidate_url':self.candidate['Ссылка'],'observed_at':self.candidate['Последнее обновление'],'country':'ru','reach':500,'contact':'@ad_manager','verified':True,
                     'country_url':'https://example.org/country', 'reach_url':'https://example.org/stat',
                     'placement_url':'https://t.me/example_channel'}

    def preview(self):
        return self.controller.review_telegram(self.body, writer=self.writer)

    def commit(self, token):
        return self.controller.review_telegram({'token':token}, commit=True, writer=self.writer)

    def test_preview_has_exact_changes_and_creates_no_files(self):
        result = self.preview()
        self.assertFalse(self.root.exists())
        self.assertEqual(result['plan']['action'], 'add')
        changes = {x['field']: x['after'] for x in result['plan']['changes']}
        self.assertEqual(changes['Контакт'], '@ad_manager')
        self.assertEqual(changes['ERR (%)'], '25.0')
        self.assertNotIn('Страна', changes)

    def test_commit_uses_safe_writer_backup_journal_and_one_use_token(self):
        result = self.commit(self.preview()['token'])
        self.assertTrue(result['saved'])
        self.assertEqual(self.writer.read('telegram')[0]['Контакт'], '@ad_manager')
        self.assertTrue(list((self.root/'backups').rglob('telegram.csv')))
        self.assertIn('committed', (self.root/'logs/changes_log.csv').read_text(encoding='utf-8-sig'))
        self.assertEqual(len(self.controller.state['report']['manual_reviews']), 1)
        with self.assertRaises(ValueError):
            self.commit('bad-token')

    def test_missing_confirmation_country_or_proof_does_not_write(self):
        from validation import ValidationError
        for changes in ({'verified':False},{'country':'by'},{'contact':'@editor'},
                        {'country_url':'not a url'},{'reach':True},{'reach':0},{'reach':2001}):
            with self.subTest(changes=changes):
                body = dict(self.body, **changes)
                with self.assertRaises((ValueError, ValidationError)):
                    self.controller.review_telegram(body, writer=self.writer)
                self.assertFalse(self.root.exists())

    def test_new_search_invalidates_preview(self):
        token = self.preview()['token']
        self.controller.generation += 1
        with self.assertRaises(ValueError):
            self.commit(token)
        self.assertFalse(self.root.exists())

    def test_table_change_requires_new_preview_and_preserves_manual_values(self):
        token = self.preview()['token']
        self.writer.upsert('telegram',dict(self.candidate, Название='Ручное название'),dry_run=False)
        original = (self.root/'data/telegram.csv').read_bytes()
        with self.assertRaises(ValueError):
            self.commit(token)
        self.assertEqual((self.root/'data/telegram.csv').read_bytes(), original)
        result = self.commit(self.preview()['token'])
        self.assertTrue(result['saved'])
        self.assertEqual(self.writer.read('telegram')[0]['Название'], 'Ручное название')

    def test_stale_snapshot_or_rejected_candidate_cannot_be_promoted(self):
        self.candidate['Последнее обновление'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError):
            self.preview()
        self.controller.state['report']['decisions'][0]['decision'] = 'reject'
        with self.assertRaises(ValueError):
            self.preview()
        self.assertFalse(self.root.exists())

    def test_storage_failure_consumes_token_and_does_not_claim_success(self):
        from safe_writer import StorageError
        token = self.preview()['token']
        with patch.object(self.writer, 'log_report', side_effect=StorageError('audit failed')):
            with self.assertRaises(StorageError):
                self.commit(token)
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            self.commit(token)

    def test_conflicting_saved_metrics_are_not_mixed_with_new_observation(self):
        self.writer.upsert('telegram',dict(self.candidate, Подписчики=3000),dry_run=False)
        original = (self.root/'data/telegram.csv').read_bytes()
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual((self.root/'data/telegram.csv').read_bytes(), original)

    def test_expired_preview_cannot_write(self):
        token = self.preview()['token']
        self.controller.review_preview['expires_at'] = 0
        with self.assertRaises(ValueError):
            self.commit(token)
        self.assertFalse(self.root.exists())

    def test_operator_comment_survives_review_commit(self):
        import csv
        self.writer.upsert('telegram',self.candidate,dry_run=False)
        path = self.root/'data/telegram.csv'
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader = csv.DictReader(stream)
            columns, rows = reader.fieldnames, list(reader)
        rows[0]['Комментарий'] = 'Ручной комментарий: не менять'
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=columns)
            writer.writeheader();writer.writerows(rows)
        self.commit(self.preview()['token'])
        self.assertEqual(self.writer.read('telegram')[0]['Комментарий'], 'Ручной комментарий: не менять')

    def test_old_form_cannot_approve_another_channel_at_same_index(self):
        self.controller.state['report']['decisions'][0]['candidate']['Ссылка'] = 'https://t.me/another_channel'
        with self.assertRaises(ValueError):
            self.preview()
        self.assertFalse(self.root.exists())

    def test_http_preview_and_explicit_commit_with_local_token(self):
        from pathlib import Path
        import re
        with patch('config.CACHE_DIR', Path(self.temp.name)/'panel-cache'), patch('safe_writer.SafeWriter', return_value=self.writer):
            server = make_server(self.controller, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, body=None, token=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                headers = {'Content-Type':'application/json'}
                if token is not None:
                    headers['X-Control-Token'] = token
                connection.request('POST' if body is not None else 'GET',path,
                                   body=json.dumps(body) if body is not None else None,headers=headers)
                response = connection.getresponse()
                status,raw = response.status,response.read().decode()
                connection.close()
                return status,raw
            try:
                status,html = request('/')
                self.assertEqual(status,200)
                token = re.search(r'token="([A-Za-z0-9_-]+)"',html)[1]
                self.assertEqual(request('/api/review/preview',self.body)[0],403)
                status,raw = request('/api/review/preview',self.body,token)
                self.assertEqual(status,200,raw)
                preview = json.loads(raw)
                self.assertFalse(self.root.exists())
                status,raw = request('/api/review/commit',{'token':preview['token']},token)
                self.assertEqual(status,200,raw)
                self.assertTrue(json.loads(raw)['saved'])
                self.assertEqual(self.writer.read('telegram')[0]['Контакт'],'@ad_manager')
            finally:
                server.shutdown();server.server_close();thread.join(3)


class ReportPersistenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'reports'

    def state(self):
        return dict(status='completed',source='telegram-public',write=False,error='',exit_code=0,
                    operation='search',report={'source':'telegram-public','decisions':[], 'manual_reviews':[]})

    def test_restore_keeps_report_but_never_restores_confirmation_token(self):
        controller = Controller(report_dir=self.path)
        self.assertFalse(self.path.exists())
        controller.state = self.state()
        controller.review_preview = {'token':'must-not-persist'}
        controller._persist_report()
        restored = Controller(report_dir=self.path)
        self.assertEqual(restored.snapshot()['report'],controller.state['report'])
        self.assertTrue(restored.snapshot()['restored'])
        self.assertIsNone(restored.review_preview)
        self.assertNotIn('must-not-persist',(self.path/'latest.json').read_text(encoding='utf-8'))

    def test_failure_without_report_keeps_previous_candidates(self):
        controller = Controller(report_dir=self.path)
        controller.state = self.state()
        controller._persist_report()
        original = (self.path/'latest.json').read_bytes()
        controller.state.update(status='cancelled',report=None,error='Остановлено',exit_code=-15)
        controller._persist_report()
        self.assertEqual((self.path/'latest.json').read_bytes(),original)
        restored = Controller(report_dir=self.path).snapshot()
        self.assertEqual(restored['status'],'completed')
        self.assertEqual(restored['report'],self.state()['report'])

    def test_login_does_not_replace_last_search(self):
        controller = Controller(report_dir=self.path)
        controller.state = self.state()
        controller._persist_report()
        original = (self.path/'latest.json').read_bytes()
        controller.state.update(operation='login',report={'session_status':'saved_unverified'})
        controller._persist_report()
        self.assertEqual((self.path/'latest.json').read_bytes(),original)

    def test_corruption_and_oversized_report_are_visible_and_preserved(self):
        self.path.mkdir()
        for raw in ('broken JSON','{}','x'*2_000_001):
            with self.subTest(size=len(raw)):
                (self.path/'latest.json').write_text(raw,encoding='utf-8')
                restored = Controller(report_dir=self.path)
                self.assertEqual(restored.snapshot()['status'],'idle')
                self.assertIn('report_warning',restored.snapshot())
                self.assertEqual((self.path/'latest.json').read_text(encoding='utf-8'),raw)

    def test_failed_atomic_save_preserves_previous_report_and_cleans_temporary(self):
        controller = Controller(report_dir=self.path)
        controller.state = self.state()
        controller._persist_report()
        original = (self.path/'latest.json').read_bytes()
        controller.state['report']['decisions'] = [{'decision':'review'}]
        with patch('control_panel.os.replace',side_effect=OSError('private detail')):
            controller._persist_report()
        self.assertEqual((self.path/'latest.json').read_bytes(),original)
        self.assertEqual([p.name for p in self.path.iterdir()],['latest.json'])
        self.assertNotIn('private detail',controller.state['report_warning'])
        self.assertEqual(controller.state['report']['decisions'],[{'decision':'review'}])

    def test_symlink_report_is_never_followed(self):
        outside = self.path.parent/'outside.json'
        outside.write_text('keep',encoding='utf-8')
        self.path.mkdir()
        try:
            (self.path/'latest.json').symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable on this Windows runner')
        controller = Controller(report_dir=self.path)
        controller.state = self.state()
        controller._persist_report()
        self.assertEqual(outside.read_text(encoding='utf-8'),'keep')
        self.assertIn('report_warning',controller.state)


class PanelTableReadTests(unittest.TestCase):
    def test_three_schemas_are_read_only_and_empty_read_creates_nothing(self):
        import tempfile
        from pathlib import Path
        from safe_writer import SafeWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'data-root'
            controller = Controller(report_dir=False)
            writer = SafeWriter(root)
            for name,count in [('advertisers',22),('telegram',14),('streamers',13)]:
                result = controller.table_snapshot(name,writer=writer)
                self.assertEqual(len(result['columns']),count)
                self.assertEqual(result['rows'],[])
            self.assertFalse(root.exists())
            with self.assertRaises(ValueError):
                controller.table_snapshot('../.env',writer=writer)

    def test_existing_file_bytes_remain_unchanged(self):
        import tempfile
        from pathlib import Path
        from safe_writer import SafeWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            writer = SafeWriter(root)
            writer.upsert('telegram',{'Название':'Канал','Ссылка':'https://t.me/example_channel'},dry_run=False)
            original = (root/'data/telegram.csv').read_bytes()
            rows = Controller(report_dir=False).table_snapshot('telegram',writer=writer)['rows']
            self.assertEqual(rows[0]['Название'],'Канал')
            self.assertEqual((root/'data/telegram.csv').read_bytes(),original)


class AdvertiserFactsPanelTests(unittest.TestCase):
    def options(self):
        from datetime import datetime, timezone
        url = 'https://www.wildberries.ru/catalog/123/detail.aspx'
        fact = {'value':'Example Brand','url':url,'checked_at':datetime.now(timezone.utc).isoformat(),'operator_verified':True}
        return dict(source='wb-web',countries=['ru'],limit=1,seed=url,write=False,
                    proofs={url:{'brand':fact}},facts_verified=True,enrich_sites=True)

    def test_verified_file_and_site_flag_are_validated_without_files(self):
        args = build_command(self.options())
        self.assertIn('--enrich-sites',args)
        self.assertNotIn('--brand-proofs',args)  # File created only for an actual process.

    def test_uploaded_facts_supply_card_urls_when_seed_is_empty(self):
        options = self.options()
        options['seed'] = ''
        args = build_command(options)
        self.assertIn('--urls',args)
        self.assertIn(next(iter(options['proofs'])),args)
        self.assertNotIn('--discover',args)

    def test_unconfirmed_stale_wrong_platform_and_malformed_facts_are_rejected(self):
        from copy import deepcopy
        options = self.options()
        variants = [dict(options,facts_verified=False),dict(options,source='twitch-web'),dict(options,proofs=[])]
        for change in ({'operator_verified':False},{'checked_at':'2020-01-01T00:00:00+00:00'},{'url':'not a URL'},{'value':None}):
            value = deepcopy(options)
            next(iter(value['proofs'].values()))['brand'].update(change)
            variants.append(value)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):
                build_command(value)

    def test_facts_file_reaches_worker_and_is_cleaned_after_exit(self):
        import tempfile
        from pathlib import Path
        options = self.options()
        process = MagicMock()
        process.stdout = io.BytesIO(b'{"source":"wb-web","status":"completed","decisions":[]}')
        process.stderr = io.BytesIO(b'')
        process.returncode = 0
        captured = []
        def spawn(args,**kwargs):
            file = Path(args[args.index('--brand-proofs')+1])
            self.assertEqual(json.loads(file.read_text(encoding='utf-8')),options['proofs'])
            captured.append(file)
            self.assertEqual(kwargs['stdin'],__import__('subprocess').DEVNULL)
            return process
        with tempfile.TemporaryDirectory() as folder,patch('config.CACHE_DIR',Path(folder)/'cache'),patch('control_panel.subprocess.Popen',side_effect=spawn):
            controller = Controller(report_dir=False)
            controller.start(options)
            for _ in range(300):
                if controller.snapshot()['status'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(controller.snapshot()['status'],'completed')
            self.assertTrue(captured)
            self.assertFalse(captured[0].exists())

    def test_schedule_keeps_explicit_facts_without_schema_change(self):
        import tempfile
        from pathlib import Path
        from control_panel import Scheduler
        with tempfile.TemporaryDirectory() as folder:
            controller = MagicMock()
            controller.snapshot.return_value={'status':'idle'}
            scheduler = Scheduler(controller,Path(folder)/'autonomy.sqlite3')
            try:
                options = self.options()
                scheduler.add(options,300)
                self.assertEqual(scheduler.jobs()[0]['options']['proofs'],options['proofs'])
            finally:
                scheduler.close()
