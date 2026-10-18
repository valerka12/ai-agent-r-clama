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

    def test_parallel_start_rejected(self):
        controller = Controller()
        controller.state['status'] = 'running'
        with self.assertRaises(ValueError):
            controller.start(dict(source='twitch-web', countries=['ru'], limit=1))


if __name__ == '__main__':
    unittest.main()
