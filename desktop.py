"""Настольное окно macOS/Windows и вход дочернего процесса упакованного агента."""
import contextlib
import os
from pathlib import Path
import sys
import threading


def prepare_runtime():
    import config
    config.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable).resolve()
    bundle_parent = executable.parents[2].parent if sys.platform == 'darwin' and '.app' in str(executable) else executable.parent
    bundled = bundle_parent / 'browser_engine'
    if getattr(sys, 'frozen', False) and bundled.is_dir():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled)


class LimitedOutput:
    """Ограничивает файлы вывода worker, включая режим без консоли Windows."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 2_000_000

    def write(self, text):
        raw = text.encode('utf-8', errors='replace')
        if len(raw) > self.remaining:
            raise OSError('Output limit exceeded')
        self.remaining -= len(raw)
        self.stream.write(text)
        self.stream.flush()
        return len(text)

    def flush(self):
        self.stream.flush()


def run_worker(argv):
    import config
    if len(argv) < 3:
        return 2
    out, err = (Path(value).resolve() for value in argv[:2])
    root = config.CACHE_DIR.resolve()
    if (out.parent != err.parent or out.name != 'stdout.txt' or err.name != 'stderr.txt'
            or not out.is_relative_to(root) or not err.is_relative_to(root)):
        return 2
    from agent import main as agent_main
    with out.open('w', encoding='utf-8') as stdout, err.open('w', encoding='utf-8') as stderr:
        with contextlib.redirect_stdout(LimitedOutput(stdout)), contextlib.redirect_stderr(LimitedOutput(stderr)):
            try:
                return agent_main(argv[2:])
            except SystemExit as error:
                return error.code if type(error.code) is int else 1
            except Exception:
                print('Ошибка процесса агента. Проверьте данные и доступ к источнику.', file=sys.stderr)
                return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    prepare_runtime()
    if argv and argv[0] == '--agent-worker':
        return run_worker(argv[1:])
    # multiprocessing/CLR у некоторых GUI-библиотек требуют freeze_support.
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        import webview
    except ImportError:
        if sys.stderr is not None:
            print('Установите requirements-desktop.txt для настольного окна.', file=sys.stderr)
        return 1
    from control_panel import Controller, make_server
    controller = Controller()
    server = make_server(controller, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.create_window('Агент · Реклама', f'http://127.0.0.1:{server.server_port}/',
                              width=1180, height=820, min_size=(760, 620), background_color='#101419')
        # Системный WebKit на Mac / WebView2 на Windows, без открытия внешнего браузера.
        webview.start(debug=False, private_mode=True)
    finally:
        process = controller.process
        controller.stop()
        if process is not None:
            try:
                process.wait(timeout=6)
            except Exception:
                controller._terminate(process, force=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
