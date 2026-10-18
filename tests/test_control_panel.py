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
        for changes in ({'source': 'shell'}, {'countries': ['us']}, {'limit': True}, {'limit': 100}, {'seed': 'https://www.twitch.tv/example;rm'}):
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
        self.controller = Controller()
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


class PanelControllerTests(unittest.TestCase):
    def test_process_runs_without_shell_and_report_is_collected(self):
        process = MagicMock()
        process.stdout = io.BytesIO(b'{"status":"completed","summary":{"total":0}}')
        process.stderr = io.BytesIO(b'')
        process.returncode = 0
        controller = Controller()
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
        controller = Controller()
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
        controller = Controller()
        controller.state['status'] = 'running'
        with self.assertRaises(ValueError):
            controller.start(dict(source='twitch-web', countries=['ru'], limit=1))


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

    def test_auth_failure_pauses_only_failed_task(self):
        failed = self.scheduler.add(self.options, 300)
        self.scheduler.tick()
        next_id = self.scheduler.add(dict(self.options, category='games'), 300)
        self.state.update(status='attention', exit_code=1, error='auth_required', report=None)
        self.scheduler.tick()
        saved = {row['id']: row for row in self.scheduler.jobs()}
        self.assertFalse(saved[failed]['enabled'])
        self.assertEqual(saved[failed]['error'], 'auth_required')
        self.assertEqual(saved[next_id]['status'], 'running')
        self.assertEqual(self.controller.start.call_count, 2)

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
