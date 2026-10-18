"""Пути сборок, вход worker и жизненный цикл системного окна."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import sys

import config
from desktop import LimitedOutput, main, run_worker


class DesktopTests(unittest.TestCase):
    def test_source_keeps_existing_project_folder(self):
        with patch.object(sys, 'frozen', False, create=True):
            self.assertEqual(config._project_dir(), Path(config.__file__).resolve().parent)

    def test_packaged_paths_are_user_writable_on_both_platforms(self):
        with patch.object(sys, 'frozen', True, create=True):
            with patch.object(sys, 'platform', 'win32'), patch.dict('os.environ', {'LOCALAPPDATA': '/local-user-data'}):
                self.assertEqual(config._project_dir(), Path('/local-user-data/AdResearchAgent'))
            with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/home-user')):
                self.assertEqual(config._project_dir(), Path('/home-user/Library/Application Support/AdResearchAgent'))

    def test_worker_redirects_without_gui_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)):
            stdout, stderr = Path(directory)/'stdout.txt', Path(directory)/'stderr.txt'
            def run(args):
                print('{"status":"completed"}')
                print('diagnostic', file=sys.stderr)
                return 2
            with patch('agent.main', side_effect=run) as agent:
                result = run_worker([str(stdout), str(stderr), '--demo', '--dry-run'])
            self.assertEqual(result, 2)
            self.assertIn('completed', stdout.read_text())
            self.assertIn('diagnostic', stderr.read_text())
            agent.assert_called_once_with(['--demo', '--dry-run'])

    def test_worker_cannot_write_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as directory, patch('config.CACHE_DIR', Path(directory)/'cache'):
            self.assertEqual(run_worker([str(Path(directory)/'stdout.txt'), str(Path(directory)/'stderr.txt'), '--demo']), 2)
            self.assertFalse((Path(directory)/'stdout.txt').exists())

    def test_output_is_bounded(self):
        output = LimitedOutput(io.StringIO())
        with self.assertRaises(OSError):
            output.write('x' * 2_000_001)

    def test_window_does_not_open_external_browser_and_closes_server(self):
        gui = MagicMock()
        server = MagicMock()
        server.server_port = 12345
        controller = MagicMock()
        controller.process = None
        with patch.dict(sys.modules, {'webview': gui}), patch('desktop.prepare_runtime'), patch('control_panel.Controller', return_value=controller), patch('control_panel.make_server', return_value=server) as make, patch('webbrowser.open', side_effect=AssertionError('external browser forbidden')):
            self.assertEqual(main([]), 0)
        self.assertEqual(make.call_args.kwargs['port'], 0)
        gui.create_window.assert_called_once()
        gui.start.assert_called_once_with(debug=False, private_mode=True)
        controller.stop.assert_called_once()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
