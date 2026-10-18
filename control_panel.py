"""Локальная панель: один процесс агента, без изменения его правил записи."""
import json
import os
from pathlib import Path
import secrets
import signal
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


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.generation = 0
        self.state = {'status': 'idle', 'report': None, 'error': '', 'exit_code': None}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, options):
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
                          'report': None, 'error': '', 'exit_code': None}
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


HTML = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Агент · управление</title>
<style>
:root{color-scheme:dark;font:15px system-ui;background:#101419;color:#e9edf2}*{box-sizing:border-box}body{margin:0}header{padding:24px 5%;border-bottom:1px solid #303841;display:flex;justify-content:space-between;align-items:center}header small{color:#9aabbc}main{max-width:1180px;margin:32px auto;padding:0 24px;display:grid;grid-template-columns:340px 1fr;gap:24px}section{background:#19212a;border:1px solid #303c48;border-radius:16px;padding:24px}h1{font-size:22px;margin:0}h2{font-size:18px;margin:0 0 20px}label{display:block;margin:16px 0 6px;color:#bfccd9}input,select,textarea,button{font:inherit;border-radius:8px}input[type=number],select,textarea{width:100%;background:#101820;color:inherit;border:1px solid #455464;padding:10px}textarea{resize:vertical;min-height:90px}button{padding:12px 18px;border:0;cursor:pointer;background:#79e0ba;color:#102820;font-weight:650}button.secondary{background:#344252;color:#fff}button:disabled{opacity:.4;cursor:default}.row{display:flex;gap:12px;align-items:center;margin-top:20px}.hint{font-size:13px;color:#9aabbc;line-height:1.6}.badge{background:#2c3b49;padding:7px 12px;border-radius:30px;font-size:13px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:22px 0}.metric{padding:18px;background:#101820;border-radius:10px}.metric strong{display:block;font-size:28px}.metric span{font-size:12px;color:#a2b5c7}pre{white-space:pre-wrap;word-break:break-word;font:13px ui-monospace;background:#101820;padding:16px;border-radius:10px;max-height:300px;overflow:auto}.item{border-top:1px solid #344252;padding:15px 0}.item small{display:block;color:#a2b5c7;margin-top:6px}details{margin-top:20px}#notice{color:#ffc585;white-space:pre-wrap}.countries label{display:inline-block;margin-right:18px}.write{padding:12px;border:1px solid #66513c;border-radius:8px;font-size:13px} @media(max-width:800px){main{grid-template-columns:1fr}.cards{grid-template-columns:repeat(3,1fr)}}
</style><header><h1>Агент / Реклама</h1><small>Локальная панель · РФ и Беларусь</small></header><main><section><h2>Новое задание</h2>
<form id="form"><label for="source">Источник</label><select id="source"><option value="tgstat-web">Telegram · TGStat</option><option value="wb-web">Wildberries</option><option value="ozon-web">Ozon</option><option value="instagram-web">Instagram</option><option value="youtube-web">YouTube</option><option value="twitch-web">Twitch</option><option value="kick-web">Kick</option></select>
<label for="seed">Каналы или ссылки</label><textarea id="seed" placeholder="@kod_ru или ссылки на профили через пробел"></textarea><p class="hint">Для WB, Ozon, Twitch и Kick можно оставить пустым — агент выберет карточки сам.</p>
<div id="tg"><label for="category">Или категория TGStat</label><input id="category" placeholder="tech" style="width:100%;padding:10px;background:#101820;border:1px solid #455464;color:white"></div>
<div class="countries"><label><input id="ru" type="checkbox" checked> РФ</label><label><input id="by" type="checkbox" checked> Беларусь</label></div><label for="limit">Лимит карточек</label><input id="limit" type="number" min="1" max="10" value="4">
<label class="write"><input id="write" type="checkbox"> Разрешить запись в таблицы</label><p class="hint">По умолчанию — только просмотр. Контакты и ручные поля остаются защищёнными.</p><div class="row"><button id="start">Запустить</button><button type="button" class="secondary" id="stop" disabled>Остановить</button></div></form></section>
<section><div style="display:flex;justify-content:space-between"><h2>Результат сбора</h2><span id="status" class="badge">Готов к запуску</span></div><p class="hint">Работа без окон и ожидания ввода. Используются сохранённые профили. Первый вход или восстановление сессии выполняется отдельно.</p><p id="notice" role="alert"></p><div class="cards"><div class="metric"><strong id="accepted">—</strong><span>ПРИНЯТО</span></div><div class="metric"><strong id="review">—</strong><span>ПРОВЕРКА</span></div><div class="metric"><strong id="rejected">—</strong><span>ОТКЛОНЕНО</span></div></div><div id="results"><p class="hint">Здесь появятся найденные кандидаты и причины решений.</p></div><details><summary>Подробный отчёт</summary><pre id="report">Нет задания</pre></details><details><summary>Диагностика запуска</summary><pre id="error">Нет сообщений</pre></details></section></main>
<script>
const $=id=>document.getElementById(id),token=__TOKEN__;
$('source').onchange=()=>{$('tg').hidden=$('source').value!=='tgstat-web';$('notice').textContent='';};
async function api(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Control-Token':token},body:data?JSON.stringify(data):undefined});const value=await r.json();if(!r.ok)throw Error(value.error||'Ошибка запроса');return value;}
$('form').onsubmit=async e=>{e.preventDefault();$('notice').textContent='';try{await api('/api/start',{source:$('source').value,seed:$('seed').value,category:$('category').value,countries:['ru','by'].filter(x=>$(x).checked),limit:Number($('limit').value),write:$('write').checked});await refresh();}catch(e){$('notice').textContent=e.message;}};
$('stop').onclick=async()=>{try{await api('/api/stop',{});await refresh();}catch(e){$('notice').textContent=e.message;}};
async function refresh(){try{const s=await api('/api/status');const names={idle:'Готов к запуску',running:'Идёт сбор',stopping:'Остановка',completed:'Завершён',attention:'Нужна проверка',cancelled:'Остановлен'};$('status').textContent=names[s.status]||s.status;const busy=['running','stopping'].includes(s.status);$('start').disabled=busy;$('stop').disabled=!busy;const r=s.report||{};const counts=r.source_summary||{};for(const [id,key] of [['accepted','accept'],['review','review'],['rejected','reject']])$(id).textContent=counts[key]??'—';$('report').textContent=s.report?JSON.stringify(s.report,null,2):'Нет отчёта';$('error').textContent=s.error||'Нет сообщений';const err=(s.error||'')+JSON.stringify(r.source_error||{});if(err.includes('auth_required'))$('notice').textContent='Нужен вход в рабочий аккаунт. Задание не ждёт ввода; восстановите сессию отдельно.';const list=$('results');list.replaceChildren();if(s.report){for(const d of (r.decisions||[])){const row=document.createElement('div');row.className='item';const c=d.candidate||{};const title=document.createElement('strong');title.textContent=c['Название компании']||c['Название']||c['Стример']||d.product_observation?.title||'Кандидат без подтверждённого имени';const info=document.createElement('small');info.textContent=({accept:'Принят',review:'Требует проверки',reject:'Отклонён'}[d.decision]||d.decision)+' · '+((d.reasons||[]).join(' / ')||'Проверки пройдены');row.append(title,info);list.append(row);}if(!(r.decisions||[]).length)list.textContent='Нет принятых или рассмотренных кандидатов.';}}catch(e){$('status').textContent='Панель недоступна';}}
refresh();setInterval(refresh,2000);
</script></html>'''


def make_server(controller=None, port=8765):
    controller = controller or Controller()
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
                    controller.start(body)
                elif self.path == '/api/stop':
                    controller.stop()
                else:
                    self.send(404, {'error': 'Не найдено.'})
                    return
                self.send(200, controller.snapshot())
            except (ValueError, OSError) as error:
                self.send(400, {'error': str(error)})
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


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
