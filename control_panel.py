"""Локальная панель: один процесс агента, без изменения его правил записи."""
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

SOURCES = ('tgstat-web', 'wb-web', 'ozon-web', 'instagram-web', 'youtube-web', 'twitch-web', 'kick-web')


def _build_command(options):
    if not isinstance(options, dict):
        raise ValueError('Некорректные параметры.')
    source = options.get('source')
    if source not in SOURCES:
        raise ValueError('Выберите источник.')
    countries = options.get('countries')
    if not isinstance(countries, list) or not countries or any(c not in ('ru', 'by') for c in countries):
        raise ValueError('Выберите РФ и/или Беларусь.')
    limit = options.get('limit')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Лимит должен быть от 1 до 10.')
    write = options.get('write', False)
    if type(write) is not bool:
        raise ValueError('Некорректный режим записи.')
    seed = options.get('seed', '')
    if not isinstance(seed, str) or len(seed) > 8000:
        raise ValueError('Слишком длинный список ссылок.')
    interpreter = Path(sys.executable)
    if sys.platform == 'win32' and interpreter.name.lower() == 'pythonw.exe':
        interpreter = interpreter.with_name('python.exe')
    args = [str(interpreter), '-m', 'agent', '--source', source, '--countries', *dict.fromkeys(countries),
            '--limit', str(limit), '--unattended', '--write' if write else '--dry-run']
    if source == 'tgstat-web':
        category = options.get('category', '')
        if not isinstance(category, str) or len(category) > 40:
            raise ValueError('Некорректная категория.')
        if seed.strip() and category.strip():
            raise ValueError('Укажите каналы либо категорию TGStat.')
        if seed.strip():
            values = seed.split()
            from tg_parser import web_channel_url
            for value in values:
                web_channel_url(value)
            args += ['--channels', *values]
        elif category.strip():
            import re
            if not re.fullmatch('[a-z][a-z0-9-]{0,39}', category.strip()):
                raise ValueError('Категория — код TGStat, например tech.')
            args += ['--category', category.strip()]
        else:
            raise ValueError('Укажите каналы либо категорию TGStat.')
    elif seed.strip():
        from importlib import import_module
        module, cls = {
            'wb-web': ('wb_parser', 'WBWebSource'), 'ozon-web': ('ozon_parser', 'OzonWebSource'),
            'instagram-web': ('instagram_parser', 'InstagramWebSource'), 'youtube-web': ('youtube_parser', 'YouTubeWebSource'),
            'twitch-web': ('twitch_parser', 'TwitchWebSource'), 'kick-web': ('kick_parser', 'KickWebSource'),
        }[source]
        collector = getattr(import_module(module), cls)()
        values = [collector.normalize_url(value) for value in seed.split()]
        args += ['--urls', *values]
    elif source in ('wb-web', 'ozon-web', 'twitch-web', 'kick-web'):
        args += ['--discover']
    else:
        raise ValueError('Для Instagram/YouTube нужны прямые ссылки на профили.')
    return args


def build_command(options):
    from wb_parser import SourceError
    from validation import ValidationError
    try:
        return _build_command(options)
    except (SourceError, ValidationError) as error:
        raise ValueError(str(error)) from error


def explain_failure(error):
    """Понятная причина отказа; исходная диагностика остаётся отдельно."""
    text = str(error or '')
    if '403' in text:
        return {'title': 'Сайт запретил доступ', 'message': 'Источник вернул HTTP 403. Данные не получены. Проверьте доступ кнопкой «Войти в аккаунт». Если сайт продолжает отказывать, этот источник пока недоступен программе.'}
    if 'auth_required' in text:
        return {'title': 'Нужно войти в аккаунт', 'message': 'Нажмите «Войти в аккаунт», завершите вход в открытом браузере и закройте его окно. После этого повторите Пуск.'}
    if '429' in text:
        return {'title': 'Слишком много запросов', 'message': 'Источник ограничил частоту запросов. Остановите поиск и повторите позже.'}
    if text:
        return {'title': 'Поиск остановлен', 'message': 'Источник не удалось прочитать. Подробности доступны в диагностике ниже.'}
    return None


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.generation = 0
        self.state = {'status': 'idle', 'report': None, 'error': '', 'exit_code': None}

    def snapshot(self):
        with self.lock:
            return dict(self.state, problem=explain_failure(self.state.get('error') or
                        (self.state.get('report') or {}).get('source_error', {}).get('reason')))

    def start(self, options, *, session_setup=False):
        if session_setup:
            source = options.get('source')
            if source not in SOURCES:
                raise ValueError('Выберите источник для входа.')
            interpreter = Path(sys.executable)
            if sys.platform == 'win32' and interpreter.name.lower() == 'pythonw.exe':
                interpreter = interpreter.with_name('python.exe')
            args = [str(interpreter), '-m', 'agent', '--setup-session', source.removesuffix('-web')]
        else:
            args = build_command(options)
        with self.lock:
            if self.state['status'] in ('running', 'stopping'):
                raise ValueError('Сбор уже запущен. Дождитесь завершения или остановите его.')
            self.generation += 1
            generation = self.generation
            # Передаются только проверенные аргументы, shell никогда не используется.
            spool = None
            if getattr(sys, 'frozen', False):
                config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                spool = tempfile.TemporaryDirectory(prefix='desktop-job-', dir=config.CACHE_DIR)
                args = [sys.executable, '--agent-worker', str(Path(spool.name)/'stdout.txt'),
                        str(Path(spool.name)/'stderr.txt'), *args[3:]]
            try:
                self.process = subprocess.Popen(args, cwd=config.PROJECT_DIR, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1'),
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                                start_new_session=(os.name == 'posix'))
            except OSError:
                if spool is not None:
                    spool.cleanup()
                raise
            process = self.process
            self.state = {'status': 'running', 'source': options['source'], 'write': options.get('write', False),
                          'report': None, 'error': '', 'exit_code': None, 'operation': 'login' if session_setup else 'search'}
        threading.Thread(target=self._collect, args=(process, generation, spool), daemon=True).start()

    def _collect(self, process, generation, spool=None):
        # Один запуск ограничен десятью карточками; объём захваченного вывода ограничен.
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        overflow = threading.Event()
        def drain(name, stream):
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if len(output[name]) + len(chunk) > 2_000_000:
                    overflow.set()
                    self._terminate(process)
                    break
                output[name].extend(chunk)
            stream.close()
        readers = [threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
                   for name in output]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            overflow.set()
            self._terminate(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate(process, force=True)
                process.wait()
        for reader in readers:
            reader.join(timeout=5)
        if spool is not None:
            try:
                for name in output:
                    path = Path(spool.name) / (name + '.txt')
                    if path.exists():
                        with path.open('rb') as stream:
                            output[name] = bytearray(stream.read(2_000_001))
                        if len(output[name]) > 2_000_000:
                            overflow.set()
                            output[name] = output[name][:2_000_000]
            finally:
                spool.cleanup()
        raw = output['stdout'].decode('utf-8', errors='replace')
        try:
            report = json.loads(raw)
            if not isinstance(report, dict):
                report = None
        except ValueError:
            report = None
        error = output['stderr'].decode('utf-8', errors='replace')[-6000:]
        # На случай внешнего исключения не показываем значения секретов из окружения/.env.
        from dotenv import dotenv_values
        secret_values = list(os.environ.values()) + list(dotenv_values(config.PROJECT_DIR / '.env').values())
        for value in secret_values:
            if isinstance(value, str) and len(value) >= 12:
                error = error.replace(value, '[скрыто]')
        with self.lock:
            if generation != self.generation:
                return
            cancelled = self.state['status'] == 'stopping'
            self.state.update(status='cancelled' if cancelled else ('completed' if process.returncode == 0 else 'attention'),
                              report=report, error='Превышено время или объём вывода.' if overflow.is_set() else error,
                              exit_code=process.returncode)
            self.process = None

    @staticmethod
    def _terminate(process, force=False):
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    def stop(self):
        with self.lock:
            if self.process is None or self.state['status'] not in ('running', 'stopping'):
                return
            process = self.process
            self.state['status'] = 'stopping'
            self._terminate(process)
        def ensure_stopped():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate(process, force=True)
        threading.Thread(target=ensure_stopped, daemon=True).start()


class Scheduler:
    """Сохранённые задания. Сбой/неясная запись требуют проверки, а не повтора."""
    def __init__(self, controller, path=None, clock=None):
        import time
        self.controller = controller
        self.path = Path(path) if path is not None else config.CACHE_DIR / 'autonomy.sqlite3'
        self.clock = clock or time.time
        self.owner = secrets.token_hex(16)
        self.active = None
        self.lease = False
        self.event = threading.Event()
        self.thread = None
        self.error = ''
        self.guard = threading.Lock()

    def _connect(self):
        import sqlite3
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('База заданий не должна быть символической ссылкой.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        if os.name == 'posix':
            self.path.chmod(0o600)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, options TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL, status TEXT NOT NULL, next_run REAL NOT NULL,
                last_run REAL, report TEXT, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS service_lease (
                id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires REAL NOT NULL);
        """)
        return connection

    def add(self, options, interval_seconds):
        if type(interval_seconds) is not int or not 300 <= interval_seconds <= 604800:
            raise ValueError('Интервал: от 5 минут до 7 суток.')
        build_command(options)
        allowed = {'source', 'countries', 'limit', 'write', 'seed', 'category'}
        if set(options) - allowed:
            raise ValueError('Неизвестные параметры задания.')
        identifier = secrets.token_hex(8)
        from contextlib import closing
        with closing(self._connect()) as db, db:
            if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 100:
                raise ValueError('Максимум 100 сохранённых заданий.')
            db.execute('INSERT INTO jobs(id,options,interval_seconds,enabled,status,next_run) VALUES(?,?,?,1,?,?)',
                       (identifier, json.dumps(options, ensure_ascii=False), interval_seconds, 'queued', self.clock()))
        return identifier

    def jobs(self):
        if not self.path.exists():
            return []
        from contextlib import closing
        with closing(self._connect()) as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY next_run,id').fetchall()
        return [{**dict(row), 'options': json.loads(row['options']),
                 'report': json.loads(row['report']) if row['report'] else None,
                 'enabled': bool(row['enabled'])} for row in rows]

    def set_enabled(self, identifier, enabled):
        if type(enabled) is not bool or not isinstance(identifier, str):
            raise ValueError('Некорректное состояние задания.')
        from contextlib import closing
        with closing(self._connect()) as db, db:
            row = db.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Задание не найдено.')
            if row['status'] == 'running':
                raise ValueError('Сначала остановите текущее задание и дождитесь завершения.')
            db.execute('UPDATE jobs SET enabled=?,status=?,next_run=? WHERE id=?',
                       (int(enabled), 'queued' if enabled else 'paused', self.clock(), identifier))

    def _heartbeat(self, db, now):
        db.execute('BEGIN IMMEDIATE')
        lease = db.execute('SELECT owner,expires FROM service_lease WHERE id=1').fetchone()
        if lease and lease['owner'] != self.owner and lease['expires'] > now:
            db.rollback()
            return False
        if not self.lease:
            # После аварии неизвестно, успел ли writer завершить запись. Не повторяем.
            db.execute("UPDATE jobs SET status='attention',enabled=0,error=? WHERE status='running'",
                       ('Предыдущий процесс прерван. Проверьте журнал и бэкапы перед возобновлением.',))
        db.execute('INSERT OR REPLACE INTO service_lease(id,owner,expires) VALUES(1,?,?)', (self.owner, now + 120))
        db.commit()
        self.lease = True
        return True

    def tick(self):
        with self.guard:
            self._tick()

    def _tick(self):
        if self.event.is_set() or not self.path.exists():
            return
        from contextlib import closing
        now = self.clock()
        with closing(self._connect()) as db:
            if not self._heartbeat(db, now):
                if self.active is not None:
                    self.controller.stop()
                self.lease = False
                return
            state = self.controller.snapshot()
            if self.active is not None:
                if state['status'] in ('running', 'stopping'):
                    return
                report = state.get('report')
                success = (state['status'] in ('completed', 'attention') and state.get('exit_code') in (0, 2)
                           and isinstance(report, dict) and report.get('status') == 'completed'
                           and report.get('summary', {}).get('failed') == 0)
                saved_report = json.dumps(report, ensure_ascii=False) if report else None
                if saved_report is not None and len(saved_report.encode('utf-8')) > 200_000:
                    saved_report = json.dumps({key: report[key] for key in
                                               ('status', 'summary', 'source', 'source_summary', 'dry_run')
                                               if key in report} | {'details_truncated': True}, ensure_ascii=False)
                with db:
                    db.execute('UPDATE jobs SET status=?,enabled=?,next_run=?+interval_seconds,report=?,error=? WHERE id=?',
                               ('waiting' if success else 'attention', int(success), now,
                                saved_report,
                                state.get('error') or ('' if success else 'Нужна проверка источника или журнала; автоматический повтор отключён.'), self.active))
                self.active = None
            if state['status'] in ('running', 'stopping'):
                return
            # Единственный владелец lease запускает строго одно задание за раз.
            row = db.execute('SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run,id LIMIT 1', (now,)).fetchone()
            if row is None or self.event.is_set():
                return
            with db:
                db.execute("UPDATE jobs SET status='running',last_run=? WHERE id=?", (now, row['id']))
            self.active = row['id']
            try:
                self.controller.start(json.loads(row['options']))
            except Exception:
                with db:
                    db.execute("UPDATE jobs SET status='attention',enabled=0,error=? WHERE id=?",
                               ('Не удалось запустить процесс. Проверьте установку и параметры.', self.active))
                self.active = None

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.event.is_set():
                try:
                    self.tick()
                    self.error = ''
                except Exception:
                    self.error = 'Ошибка базы заданий. Автоматический запуск приостановлен.'
                self.event.wait(1)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def close(self):
        self.event.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
        with self.guard:
            self._close_runtime()

    def _close_runtime(self):
        if self.active is not None:
            process = self.controller.process
            self.controller.stop()
            if process is not None:
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.controller._terminate(process, force=True)
        if self.path.exists() and self.lease:
            from contextlib import closing
            with closing(self._connect()) as db, db:
                # running сохраняется для безопасного восстановления после рестарта.
                db.execute('DELETE FROM service_lease WHERE owner=?', (self.owner,))
        self.lease = False


def service_main():
    """Без интерфейса: сохранённые задания, пригодно для systemd/launchd."""
    controller = Controller()
    scheduler = Scheduler(controller)
    def stop_service(signum, frame):
        scheduler.event.set()
    previous = {}
    for name in ('SIGINT', 'SIGTERM'):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, stop_service)
    print('Сервис запущен. Использует сохранённые задания; Ctrl+C для остановки.', flush=True)
    try:
        scheduler.start()
        while not scheduler.event.wait(1):
            if scheduler.error:
                print(scheduler.error, file=sys.stderr, flush=True)
                return 1
    finally:
        scheduler.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


HTML = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Агент · Реклама</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#101419;color:#e9edf2}*{box-sizing:border-box}body{margin:0}header{padding:24px 5%;border-bottom:1px solid #303841;display:flex;justify-content:space-between;align-items:center}header small{color:#9aabbc}main{max-width:1180px;margin:32px auto;padding:0 24px;display:grid;grid-template-columns:340px 1fr;gap:24px}section{background:#19212a;border:1px solid #303c48;border-radius:16px;padding:24px}h1{font-size:22px;margin:0}h2{font-size:18px;margin:0 0 20px}label{display:block;margin:16px 0 6px;color:#bfccd9}input,select,textarea,button{font:inherit;border-radius:8px}input[type=number],select,textarea{width:100%;background:#101820;color:inherit;border:1px solid #455464;padding:10px}textarea{resize:vertical;min-height:90px}button{padding:12px 18px;border:0;cursor:pointer;background:#79e0ba;color:#102820;font-weight:650}button.secondary{background:#344252;color:#fff}button:disabled{opacity:.4;cursor:default}.row{display:flex;gap:12px;align-items:center;margin-top:20px}.hint{font-size:13px;color:#9aabbc;line-height:1.6}.badge{background:#2c3b49;padding:7px 12px;border-radius:30px;font-size:13px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:22px 0}.metric{padding:18px;background:#101820;border-radius:10px}.metric strong{display:block;font-size:28px}.metric span{font-size:12px;color:#a2b5c7}pre{white-space:pre-wrap;word-break:break-word;font:13px ui-monospace;background:#101820;padding:16px;border-radius:10px;max-height:300px;overflow:auto}.item{border-top:1px solid #344252;padding:15px 0}.item small{display:block;color:#a2b5c7;margin-top:6px}details{margin-top:20px}#notice{color:#ffc585;white-space:pre-wrap}.countries label{display:inline-block;margin-right:18px}.write{padding:12px;border:1px solid #66513c;border-radius:8px;font-size:13px} @media(max-width:800px){main{grid-template-columns:1fr}.cards{grid-template-columns:repeat(3,1fr)}}
</style><header><h1>Агент / Реклама</h1><small>РФ и Беларусь</small></header><main><section><h2>Что сделать?</h2><form id="form">
<label for="task">Задача</label><select id="task"><option value="telegram">Найти Telegram-каналы</option><option value="advertisers">Найти рекламодателей</option><option value="streamers">Найти стримеров</option></select>
<label for="source">Платформа</label><select id="source"><option value="tgstat-web">Telegram · TGStat</option><option value="wb-web">Wildberries</option><option value="ozon-web">Ozon</option><option value="instagram-web">Instagram</option><option value="youtube-web">YouTube</option><option value="twitch-web">Twitch</option><option value="kick-web">Kick</option></select>
<div id="tg"><label for="topic">Тематика каналов</label><select id="topic"><option value="tech">Технологии</option><option value="">Другая категория TGStat</option></select><input id="category" hidden placeholder="Код категории TGStat" style="width:100%;padding:10px;background:#101820;color:white;border:1px solid #455464"><p class="hint">Вместо тематики можно задать конкретные каналы в дополнительных настройках.</p></div>
<label for="limit">Сколько проверить</label><input id="limit" type="number" min="1" max="10" value="4">
<div class="row"><button id="start">▶ Пуск</button><button type="button" class="secondary" id="stop" disabled>■ Стоп</button></div><p id="task-hint" class="hint">Найдём каналы выбранной тематики и проверим показатели и возможность рекламы.</p>
<button type="button" class="secondary" id="login">Войти в аккаунт</button><p class="hint">Для первого входа откроется отдельный браузер. Войдите на сайте и закройте его окно. Программа сохранит сессию.</p>
<details><summary>Дополнительные настройки</summary><label for="seed">Конкретные каналы или ссылки (необязательно)</label><textarea id="seed" placeholder="@kod_ru или ссылки через пробел"></textarea><p class="hint" id="seed-hint">Если указаны каналы, поиск по тематике не используется.</p>
<div class="countries"><label><input id="ru" type="checkbox" checked> РФ</label><label><input id="by" type="checkbox" checked> Беларусь</label></div>
<label class="write"><input id="write" type="checkbox"> Сохранять результат в таблицы</label><p class="hint">По умолчанию — просмотр. Ручные поля и заполненные ячейки защищены.</p>
<label for="interval">Повторять каждые (минут)</label><input id="interval" type="number" min="5" max="10080" value="60"><button type="button" class="secondary" id="schedule" style="margin-top:12px">Включить расписание</button><p class="hint">Работает, пока приложение или серверный сервис включён.</p></details></form></section>
<section><h2>Ход выполнения</h2><span id="status" class="badge">Готов к запуску</span><p id="notice" role="alert"></p><div id="problem" hidden style="border:1px solid #bf7653;background:#392820;padding:18px;border-radius:10px;margin-top:20px"><strong id="problem-title"></strong><p id="problem-text"></p></div>
<div class="cards"><div class="metric"><strong id="accepted">—</strong><span>ПОДХОДЯТ</span></div><div class="metric"><strong id="review">—</strong><span>НУЖНА ПРОВЕРКА</span></div><div class="metric"><strong id="rejected">—</strong><span>ОТКЛОНЕНЫ</span></div></div><div id="results"><p class="hint">Выберите задачу и нажмите Пуск.</p></div>
<details><summary>Расписание</summary><div id="jobs"></div><p id="scheduler-error" class="hint"></p></details><details><summary>Подробный отчёт</summary><pre id="report">Нет задания</pre></details><details><summary>Диагностика</summary><pre id="error">Нет сообщений</pre></details></section></main>
<script>
const $=id=>document.getElementById(id),token=__TOKEN__;
const groups={telegram:['tgstat-web'],advertisers:['wb-web','ozon-web','instagram-web','youtube-web'],streamers:['twitch-web','kick-web']};
function updateSource(){const source=$('source').value;$('tg').hidden=source!=='tgstat-web';const direct=['instagram-web','youtube-web'].includes(source);$('seed').required=direct;if(direct)$('seed').closest('details').open=true;$('seed-hint').textContent=direct?'Для этой платформы нужны прямые ссылки на профили. Самостоятельный поиск ещё не реализован.':source==='tgstat-web'?'Если указаны каналы, поиск по тематике не используется.':'Можно оставить пустым — программа выберет карточки из каталога.';$('task-hint').textContent=$('task').value==='telegram'?'Проверим каналы, охваты и возможность размещения рекламы.':$('task').value==='advertisers'?'Соберём кандидатов. Без подтверждённых фактов бренд останется на проверке.':'Проверим профили стримеров. Неподтверждённые показатели останутся пустыми.';}
$('task').onchange=()=>{const allowed=groups[$('task').value];for(const option of $('source').options){option.hidden=!allowed.includes(option.value);option.disabled=option.hidden;}$('source').value=allowed[0];$('seed').value='';updateSource();};
$('topic').onchange=()=>{$('category').hidden=!!$('topic').value;};
$('source').onchange=updateSource;
$('task').onchange();
async function api(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Control-Token':token},body:data?JSON.stringify(data):undefined});const value=await r.json();if(!r.ok)throw Error(value.error||'Ошибка запроса');return value;}
function options(){return {source:$('source').value,seed:$('seed').value,category:$('seed').value.trim()?'':($('topic').value||$('category').value),countries:['ru','by'].filter(x=>$(x).checked),limit:Number($('limit').value),write:$('write').checked};}
$('form').onsubmit=async e=>{e.preventDefault();$('notice').textContent='';try{await api('/api/start',options());await refresh();}catch(e){$('notice').textContent=e.message;}};
$('schedule').onclick=async()=>{try{await api('/api/jobs/add',{options:options(),interval_seconds:Number($('interval').value)*60});$('notice').textContent='Задание сохранено. Первый запуск начнётся автоматически.';await refreshJobs();}catch(e){$('notice').textContent=e.message;}};
async function refreshJobs(){try{const value=await api('/api/jobs');$('scheduler-error').textContent=value.error||'';const list=$('jobs');list.replaceChildren();for(const j of value.jobs){const row=document.createElement('div');row.className='item';const text=document.createElement('div');text.textContent=j.options.source+' · '+(j.options.write?'запись':'просмотр')+' · каждые '+j.interval_seconds/60+' мин · '+({queued:'В очереди',running:'Выполняется',waiting:'Ожидает',paused:'Пауза',attention:'Нужна проверка'}[j.status]||j.status);const detail=document.createElement('small');detail.textContent=j.error||(j.enabled?'Следующий запуск: '+new Date(j.next_run*1000).toLocaleString():'Автозапуск отключён');const toggle=document.createElement('button');toggle.className='secondary';toggle.style.marginTop='10px';toggle.textContent=j.enabled?'Пауза':'Возобновить';toggle.disabled=j.status==='running';toggle.onclick=async()=>{try{await api('/api/jobs/toggle',{id:j.id,enabled:!j.enabled});await refreshJobs();}catch(e){$('notice').textContent=e.message;}};row.append(text,detail,toggle);if(j.report){const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='Последний отчёт';pre.textContent=JSON.stringify(j.report,null,2);details.append(summary,pre);row.append(details);}list.append(row);}if(!value.jobs.length)list.textContent='Заданий пока нет.';}catch(e){$('scheduler-error').textContent=e.message;}}

$('login').onclick=async()=>{try{await api('/api/session',{source:$('source').value});$('notice').textContent='Войдите в открытом браузере и закройте его окно. Пароли и коды вводите только на сайте.';await refresh();}catch(e){$('notice').textContent=e.message;}};
$('stop').onclick=async()=>{try{await api('/api/stop',{});await refresh();}catch(e){$('notice').textContent=e.message;}};
async function refresh(){try{const s=await api('/api/status');const names={idle:'Готов к запуску',running:'Идёт сбор',stopping:'Остановка',completed:'Завершён',attention:'Нужна проверка',cancelled:'Остановлен'};$('status').textContent=s.status==='running'&&s.operation==='login'?'Ожидаем входа в браузере':names[s.status]||s.status;const busy=['running','stopping'].includes(s.status);$('start').disabled=busy;$('login').disabled=busy;$('task').disabled=busy;$('source').disabled=busy;$('stop').disabled=!busy;$('problem').hidden=!s.problem;if(s.problem){$('notice').textContent='';$('problem-title').textContent=s.problem.title;$('problem-text').textContent=s.problem.message;}const r=s.report||{};if(r.session_status==='saved_unverified')$('notice').textContent=r.message;const counts=r.source_summary||{};for(const [id,key] of [['accepted','accept'],['review','review'],['rejected','reject']])$(id).textContent=counts[key]??'—';$('report').textContent=s.report?JSON.stringify(s.report,null,2):'Нет отчёта';$('error').textContent=s.error||'Нет сообщений';if(s.status==='completed'&&s.report&&!s.report.session_setup){$('notice').textContent='Поиск завершён. '+(s.write?'Разрешённые данные сохранены.':'Режим просмотра: таблицы не изменены.');}const err=(s.error||'')+JSON.stringify(r.source_error||{});if(err.includes('auth_required'))$('notice').textContent='Нужен вход в рабочий аккаунт. Задание не ждёт ввода; восстановите сессию отдельно.';const list=$('results');list.replaceChildren();if(s.report){for(const d of (r.decisions||[])){const row=document.createElement('div');row.className='item';const c=d.candidate||{};const title=document.createElement('strong');title.textContent=c['Название компании']||c['Название']||c['Стример']||d.product_observation?.title||'Кандидат без подтверждённого имени';const info=document.createElement('small');info.textContent=({accept:'Принят',review:'Требует проверки',reject:'Отклонён'}[d.decision]||d.decision)+' · '+((d.reasons||[]).join(' / ')||'Проверки пройдены');row.append(title,info);list.append(row);}if(!(r.decisions||[]).length)list.textContent='Нет принятых или рассмотренных кандидатов.';}}catch(e){$('status').textContent='Панель недоступна';}}
refresh();refreshJobs();setInterval(refresh,2000);setInterval(refreshJobs,5000);
</script></html>'''


def make_server(controller=None, port=8765):
    controller = controller or Controller()
    scheduler = Scheduler(controller)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, html=False):
            raw = value.encode('utf-8') if html else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self, token_required=True):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                self.send(403, {'error': 'Разрешён только локальный адрес панели.'})
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin != 'http://' + host:
                self.send(403, {'error': 'Внешние запросы запрещены.'})
                return False
            if token_required and not secrets.compare_digest(self.headers.get('X-Control-Token',''), token):
                self.send(403, {'error': 'Недействительный ключ панели.'})
                return False
            return True

        def do_GET(self):
            if not self.authorized(token_required=self.path != '/'):
                return
            if self.path == '/':
                self.send(200, HTML.replace('__TOKEN__', json.dumps(token)), html=True)
            elif self.path == '/api/status':
                self.send(200, controller.snapshot())
            elif self.path == '/api/jobs':
                try:
                    self.send(200, {'jobs': scheduler.jobs(), 'error': scheduler.error})
                except Exception:
                    self.send(503, {'error': 'База заданий недоступна; проверьте папку данных.'})
            else:
                self.send(404, {'error': 'Не найдено.'})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Нужен небольшой JSON-запрос.')
                body = json.loads(self.rfile.read(length))
                if self.path == '/api/start':
                    if scheduler.active is not None:
                        raise ValueError('Дождитесь завершения задания расписания.')
                    controller.start(body)
                elif self.path == '/api/session':
                    if scheduler.active is not None:
                        raise ValueError('Дождитесь завершения задания расписания.')
                    if not isinstance(body, dict):
                        raise ValueError('Выберите источник.')
                    controller.start({'source': body.get('source')}, session_setup=True)
                elif self.path == '/api/stop':
                    controller.stop()
                elif self.path == '/api/jobs/add':
                    if not isinstance(body, dict):
                        raise ValueError('Нужны параметры задания.')
                    scheduler.add(body.get('options'), body.get('interval_seconds'))
                elif self.path == '/api/jobs/toggle':
                    if not isinstance(body, dict):
                        raise ValueError('Нужны параметры задания.')
                    scheduler.set_enabled(body.get('id'), body.get('enabled'))
                else:
                    self.send(404, {'error': 'Не найдено.'})
                    return
                self.send(200, controller.snapshot())
            except (ValueError, OSError) as error:
                self.send(400, {'error': str(error)})
            except sqlite3.Error:
                self.send(503, {'error': 'База заданий недоступна. Проверьте папку данных.'})
    class LocalServer(ThreadingHTTPServer):
        def server_close(self):
            try:
                scheduler.close()
            finally:
                super().server_close()
    server = LocalServer(('127.0.0.1', port), Handler)
    scheduler.start()
    return server


def main():
    controller = Controller()
    try:
        server = make_server(controller)
    except OSError:
        print('Не удалось открыть локальную панель: порт 8765 занят или запуск ограничен средой.', file=sys.stderr)
        return 1
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Панель управления: {url}\nДля выхода нажмите Ctrl+C. Панель не доступна из интернета.', flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
