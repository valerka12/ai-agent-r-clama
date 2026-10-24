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

SOURCES = ('telegram-public', 'tgstat-web', 'wb-web', 'ozon-web', 'instagram-web', 'youtube-web', 'twitch-web', 'kick-web')


def _validate_facts(options):
    """Файл оператора остаётся набором явных доказательств, а не выводом LLM."""
    proofs = options.get('proofs')
    enrich = options.get('enrich_sites', False)
    if type(enrich) is not bool:
        raise ValueError('Некорректный режим обогащения сайта.')
    allowed = {'wb-web': ('wb_parser', 'WBWebSource'), 'ozon-web': ('ozon_parser', 'OzonWebSource'),
               'instagram-web': ('instagram_parser', 'InstagramWebSource'), 'youtube-web': ('youtube_parser', 'YouTubeWebSource')}
    if options.get('source') not in allowed:
        if proofs or enrich:
            raise ValueError('Проверенные факты и сайт применяются только к рекламодателям.')
        return
    if proofs is None or proofs == {}:
        return
    if not isinstance(proofs, dict) or len(proofs) > 10 or options.get('facts_verified') is not True:
        raise ValueError('Нужен файл фактов не более чем для 10 карточек и подтверждение его проверки.')
    try:
        if len(json.dumps(proofs, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 12000:
            raise ValueError('Файл фактов превышает 12 КБ.')
    except (TypeError, ValueError) as error:
        raise ValueError('Файл фактов должен быть JSON размером до 12 КБ.') from error
    from datetime import datetime, timezone
    from importlib import import_module
    from validation import validate_url
    from wb_parser import timestamp
    module, name = allowed[options['source']]
    collector = getattr(import_module(module), name)()
    fields = {'brand', 'country', 'website', 'instagram', 'telegram', 'youtube', 'contact',
              'last_activity', 'advertising', 'niche', 'agency', 'account_age_days', 'followers', 'micro_niche'}
    for url, facts in proofs.items():
        if collector.normalize_url(url) != url:
            raise ValueError('Ключ фактов должен быть нормализованной ссылкой карточки без параметров и завершающего слеша.')
        if not isinstance(facts, dict) or not facts or set(facts) - fields:
            raise ValueError('Неизвестные поля фактов карточки.')
        for field, fact in facts.items():
            if (not isinstance(fact, dict) or set(fact) != {'value', 'url', 'checked_at', 'operator_verified'}
                    or fact['operator_verified'] is not True):
                raise ValueError('Каждый факт требует value, url, checked_at и operator_verified=true.')
            validate_url(fact['url'])
            checked = timestamp(fact['checked_at'])
            if checked is None or not 0 <= (datetime.now(timezone.utc)-checked).total_seconds() <= config.ACTIVE_WITHIN_DAYS*86400:
                raise ValueError('Факты должны иметь свежую дату проверки с часовым поясом.')
            if field in ('agency', 'advertising', 'micro_niche'):
                if type(fact['value']) is not bool:
                    raise ValueError('Логический факт должен быть true или false.')
            elif field in ('followers', 'account_age_days'):
                if type(fact['value']) is not int or fact['value'] < 0:
                    raise ValueError('Число подписчиков и возраст — целые неотрицательные числа.')
            elif not isinstance(fact['value'], str) or not fact['value'].strip():
                raise ValueError('Текстовое значение факта не должно быть пустым.')


def _build_command(options):
    if not isinstance(options, dict):
        raise ValueError('Некорректные параметры.')
    source = options.get('source')
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError('Выберите источник.')
    _validate_facts(options)
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
    if not seed.strip() and options.get('proofs'):
        seed = ' '.join(options['proofs'])
        if len(seed) > 8000:
            raise ValueError('Ссылки из файла превышают лимит 8000 символов.')
    interpreter = Path(sys.executable)
    if sys.platform == 'win32' and interpreter.name.lower() == 'pythonw.exe':
        interpreter = interpreter.with_name('python.exe')
    args = [str(interpreter), '-m', 'agent', '--source', source, '--countries', *dict.fromkeys(countries),
            '--limit', str(limit), '--unattended', '--write' if write else '--dry-run']
    if source == 'telegram-public':
        from tg_parser import TelegramPublicSource
        if write:
            raise ValueError('Публичный поиск пока доступен только для просмотра кандидатов.')
        query = options.get('query', '')
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('Поисковая фраза: не более 200 символов.')
        if seed.strip():
            values = ['@' + TelegramPublicSource.username(value) for value in seed.split()]
            args += ['--channels', *values]
        elif query.strip():
            if len(query.strip()) < 3:
                raise ValueError('Введите хотя бы 3 символа для поиска.')
            args += ['--query', query.strip()]
    elif source == 'tgstat-web':
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
    if options.get('enrich_sites'):
        args.append('--enrich-sites')
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
    if 'browser_unavailable:' in text:
        reason = text.split('browser_unavailable:', 1)[1].strip()
        return {'title': 'Браузер не запустился', 'message': reason}
    if '403' in text:
        return {'title': 'Сайт запретил доступ', 'message': 'Источник вернул HTTP 403. Данные не получены. Проверьте доступ кнопкой «Войти в аккаунт». Если сайт продолжает отказывать, этот источник пока недоступен программе.'}
    if 'auth_required' in text:
        return {'title': 'Нужно войти в аккаунт', 'message': 'Нажмите «Войти в аккаунт», завершите вход в открытом браузере и закройте его окно. После этого повторите Пуск.'}
    if '429' in text:
        return {'title': 'Слишком много запросов', 'message': 'Источник ограничил частоту запросов. Остановите поиск и повторите позже.'}
    if text:
        return {'title': 'Поиск остановлен', 'message': 'Источник не удалось прочитать. Подробности доступны в диагностике ниже.'}
    return None


def source_access_blocked(error):
    """Только явные отказы доступа, а не произвольные числа в сообщении."""
    import re
    return bool(re.search(r'HTTP\s*(?:403|429)\b|\bauth_required\b|На странице проверка доступа|Обнаружена проверка доступа',
                          str(error or ''), re.IGNORECASE))


class Controller:
    def __init__(self, report_dir=None):
        self.lock = threading.Lock()
        self.process = None
        self.generation = 0
        self.review_preview = None
        self.state = {'status': 'idle', 'report': None, 'error': '', 'exit_code': None}
        self.report_dir = None if report_dir is False else Path(report_dir) if report_dir is not None else config.CACHE_DIR / 'reports'
        self._restore_report()

    def _report_path(self):
        if self.report_dir is None:
            return None
        path = self.report_dir / 'latest.json'
        if self.report_dir.is_symlink() or self.report_dir.parent.is_symlink() or path.is_symlink():
            raise OSError('Report symlinks are not supported')
        return path

    def _restore_report(self):
        try:
            path = self._report_path()
            if path is None or not path.exists():
                return
            if path.stat().st_size > 2_000_000:
                raise ValueError('Report too large')
            saved = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(saved, dict) or saved.get('version') != 1
                    or saved.get('status') not in ('completed', 'attention', 'cancelled')
                    or saved.get('source') not in SOURCES
                    or type(saved.get('write')) is not bool
                    or not isinstance(saved.get('error'), str)
                    or saved.get('report') is not None and not isinstance(saved['report'], dict)
                    or saved.get('exit_code') is not None and type(saved['exit_code']) is not int):
                raise ValueError('Invalid report')
            self.state = {key: saved[key] for key in ('status', 'source', 'write', 'report', 'error', 'exit_code')}
            self.state.update(operation='search', restored=True)
        except (OSError, ValueError, KeyError):
            self.state['report_warning'] = 'Последний отчёт не удалось восстановить. Исходный файл оставлен без изменений.'

    def _persist_report(self):
        if (self.report_dir is None or self.state.get('operation') == 'login'
                or not isinstance(self.state.get('report'), dict)):
            return
        temporary = None
        try:
            path = self._report_path()
            saved = {key: self.state.get(key) for key in ('status', 'source', 'write', 'report', 'error', 'exit_code')}
            saved['version'] = 1
            raw = json.dumps(saved, ensure_ascii=False).encode('utf-8')
            if len(raw) > 2_000_000:
                raise ValueError('Report too large')
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.report-', dir=path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            self.state.pop('report_warning', None)
        except (OSError, ValueError, TypeError):
            self.state['report_warning'] = 'Отчёт виден в окне, но сохранить его на диск не удалось. Скачайте JSON перед закрытием. Результат записи таблиц от этого не меняется.'
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def table_snapshot(self, table, *, writer=None):
        from safe_writer import make_writer
        columns = {'advertisers': config.ADVERTISER_COLUMNS, 'telegram': config.TELEGRAM_COLUMNS,
                   'streamers': config.STREAMER_COLUMNS}
        if table not in columns:
            raise ValueError('Неизвестная таблица.')
        storage = writer if writer is not None else make_writer(google=True)
        rows = storage.read(table)
        result = {'table': table, 'columns': list(columns[table]), 'rows': rows}
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 2_000_000:
            raise ValueError('Таблица превышает лимит просмотра 2 МБ. Используйте исходный CSV.')
        return result

    def snapshot(self):
        with self.lock:
            report = self.state.get('report') or {}
            storage_error = next((item.get('reason') for item in report.get('results', [])
                                  if item.get('action') == 'error'), None)
            problem = ({'title': 'Запись не завершена',
                        'message': str(storage_error) + ' Проверьте журнал изменений и резервную копию перед повторным запуском.'}
                       if storage_error else explain_failure(self.state.get('error') or
                           report.get('source_error', {}).get('reason')))
            return dict(self.state, problem=problem)

    def review_telegram(self, body, *, commit=False, writer=None):
        """Явная проверка оператором; автоматический источник остаётся read-only."""
        from datetime import datetime, timezone
        from safe_writer import make_writer
        from validation import ValidationError, calculate_err, validate_url, validate_counter
        if not isinstance(body, dict):
            raise ValueError('Нужны данные проверки.')
        storage = writer if writer is not None else make_writer(google=True)
        with self.lock:
            if self.state['status'] in ('running', 'stopping'):
                raise ValueError('Дождитесь окончания поиска.')
            report = self.state.get('report') or {}
            if self.state.get('status') != 'completed' or report.get('source') != 'telegram-public' or report.get('source_error'):
                raise ValueError('Проверка доступна только для завершённого публичного поиска Telegram.')
            if commit:
                pending = self.review_preview
                token = body.get('token')
                if (not pending or not isinstance(token, str)
                        or not secrets.compare_digest(token, pending['token'])
                        or pending['generation'] != self.generation
                        or datetime.now(timezone.utc).timestamp() > pending['expires_at']):
                    raise ValueError('План устарел. Выполните предварительную проверку заново.')
                self.review_preview = None  # Не повторяем запись с неясным результатом.
                candidate, proof = pending['candidate'], pending['proof']
                current = storage.upsert('telegram', candidate, dry_run=True, confirmed_fields=('Контакт',))
                if current != pending['plan']:
                    raise ValueError('Таблица изменилась. Проверьте новый план перед сохранением.')
                storage.log_report({'kind': 'operator_review', 'candidate': candidate, 'proof': proof})
                result = storage.upsert('telegram', candidate, dry_run=False, confirmed_fields=('Контакт',))
                report.setdefault('manual_reviews', []).append({'candidate': candidate, 'proof': proof, 'result': result})
                self._persist_report()
                return {'saved': True, 'result': result, 'report_warning': self.state.get('report_warning')}
            self.review_preview = None
            index = body.get('index')
            decisions = report.get('decisions', [])
            if type(index) is not int or not 0 <= index < len(decisions):
                raise ValueError('Выберите кандидата из текущего отчёта.')
            decision = decisions[index]
            if decision.get('decision') != 'review' or decision.get('placement', {}).get('status') != 'offered':
                raise ValueError('Нет подтверждаемого предложения рекламы; требуется дальнейший сбор фактов.')
            if body.get('verified') is not True:
                raise ValueError('Подтвердите личную проверку фактов по указанным источникам.')
            country = body.get('country')
            if country not in ('ru', 'by') or country not in decision.get('evidence', {}).get('requested_countries', []):
                raise ValueError('Страна должна соответствовать фильтру поиска: РФ или Беларусь.')
            contact = body.get('contact')
            if not isinstance(contact, str) or not contact or contact not in decision.get('contact_suggestions', []):
                raise ValueError('Выберите рекламный контакт из предложения источника.')
            candidate = {k: v for k, v in decision.get('candidate', {}).items()
                         if k in ('Название', 'Ссылка', 'Подписчики', 'Последнее обновление', 'Тематика')}
            if (body.get('candidate_url') != candidate.get('Ссылка')
                    or body.get('observed_at') != candidate.get('Последнее обновление')):
                raise ValueError('Карточка изменилась после поиска. Откройте проверку заново.')
            if not candidate.get('Название') or not candidate.get('Ссылка'):
                raise ValueError('Нет подтверждённого названия или ссылки.')
            try:
                observed = datetime.fromisoformat(candidate.get('Последнее обновление', ''))
            except (ValueError, TypeError) as error:
                raise ValueError('Нет корректной даты снимка. Повторите поиск.') from error
            if observed.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-observed).total_seconds() <= 86400:
                raise ValueError('Снимок старше суток или имеет неверную дату. Повторите поиск.')
            proof = {'country': country, 'operator_confirmed': True,
                     'checked_at': datetime.now(timezone.utc).isoformat()}
            for key in ('country_url', 'reach_url', 'placement_url'):
                value = body.get(key)
                validate_url(value)
                proof[key] = value
            reach = body.get('reach')
            validate_counter(reach)
            candidate['Средний охват'] = reach
            candidate['ERR (%)'] = calculate_err(reach, candidate.get('Подписчики'))
            if (candidate['Подписчики'] < config.TG_MIN_SUBSCRIBERS or reach < config.TG_MIN_REACH
                    or not config.TG_MIN_ERR <= candidate['ERR (%)'] <= config.TG_MAX_ERR):
                raise ValueError('Канал не проходит минимальные пороги подписчиков, охвата или ERR.')
            candidate['Контакт'] = contact
            plan = storage.upsert('telegram', candidate, dry_run=True, confirmed_fields=('Контакт',))
            if plan['action'] not in ('add', 'update', 'skip'):
                raise ValidationError('Запись требует дополнительной проверки.')
            token = secrets.token_urlsafe(32)
            self.review_preview = {'token': token, 'generation': self.generation,
                                   'candidate': candidate, 'proof': proof, 'plan': plan,
                                   'expires_at': datetime.now(timezone.utc).timestamp() + 600}
            return {'token': token, 'plan': plan, 'proof': proof}

    def google_status(self):
        from safe_writer import GoogleSheetsStore
        return GoogleSheetsStore().status()

    def google_setup(self, body, *, credentials=False):
        from safe_writer import GoogleSheetsStore
        if not isinstance(body,dict):
            raise ValueError('Нужны параметры подключения Google Sheets.')
        with self.lock:
            if self.state['status'] in ('running','stopping'):
                raise ValueError('Дождитесь завершения текущего задания.')
            store=GoogleSheetsStore()
            if credentials:
                if body.get('save_locally') is not True:
                    raise ValueError('Подтвердите локальное сохранение выбранного ключа.')
                return store.save_credentials(body.get('credentials'))
            expected=body.get('table') or None
            if expected is not None and expected not in ('advertisers','telegram','streamers'):
                raise ValueError('Неизвестный тип таблицы.')
            return store.attach(body.get('url'),expected,replace=body.get('replace') is True)

    def start(self, options, *, session_setup=False):
        if session_setup:
            source = options.get('source')
            if source not in SOURCES or source == 'telegram-public':
                raise ValueError('Для этого источника вход не требуется либо источник не выбран.')
            interpreter = Path(sys.executable)
            if sys.platform == 'win32' and interpreter.name.lower() == 'pythonw.exe':
                interpreter = interpreter.with_name('python.exe')
            args = [str(interpreter), '-m', 'agent', '--setup-session', source.removesuffix('-web')]
        else:
            args = build_command(options)
        from safe_writer import GoogleSheetsStore, StorageError
        import hashlib
        google_binding=None
        output_table=None
        if not session_setup:
            output_table=('telegram' if options['source'] in ('telegram-public','tgstat-web') else
                          'streamers' if options['source'] in ('twitch-web','kick-web') else 'advertisers')
            try:
                store=GoogleSheetsStore()
                google_binding=store.bindings().get(output_table)
                if google_binding and not store.status()['configured']:
                    raise ValueError('Для подключённого листа требуется локальный JSON-ключ Google. Запись в CSV вместо него не выполняется.')
            except StorageError as error:
                raise ValueError(str(error)) from error
        environment=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUTF8='1',
                         ADRESEARCH_GOOGLE_OUTPUT='1' if google_binding else '0',
                         ADRESEARCH_GOOGLE_TABLE=output_table or '',
                         ADRESEARCH_GOOGLE_TARGET=hashlib.sha256(json.dumps(google_binding,sort_keys=True).encode()).hexdigest() if google_binding else '')
        with self.lock:
            if self.state['status'] in ('running', 'stopping'):
                raise ValueError('Сбор уже запущен. Дождитесь завершения или остановите его.')
            self.generation += 1
            generation = self.generation
            # Передаются только проверенные аргументы, shell никогда не используется.
            spool = None
            try:
                if getattr(sys, 'frozen', False) or (not session_setup and options.get('proofs')):
                    if config.CACHE_DIR.is_symlink():
                        raise ValueError('Папка кэша не должна быть символьной ссылкой.')
                    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    spool = tempfile.TemporaryDirectory(prefix='desktop-job-', dir=config.CACHE_DIR)
                if not session_setup and options.get('proofs'):
                    proof_file = Path(spool.name)/'proofs.json'
                    proof_file.write_text(json.dumps(options['proofs'], ensure_ascii=False), encoding='utf-8')
                    args += ['--brand-proofs', str(proof_file)]
                if getattr(sys, 'frozen', False):
                    args = [sys.executable, '--agent-worker', str(Path(spool.name)/'stdout.txt'),
                            str(Path(spool.name)/'stderr.txt'), *args[3:]]
                self.process = subprocess.Popen(args, cwd=config.PROJECT_DIR, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                env=environment,
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                                start_new_session=(os.name == 'posix'))
            except (OSError, ValueError):
                if spool is not None:
                    spool.cleanup()
                raise
            process = self.process
            self.state = {'status': 'running', 'source': options['source'], 'write': options.get('write', False),
                          'report': None, 'error': '', 'exit_code': None, 'operation': 'login' if session_setup else 'search',
                          'destination':google_binding['sheet_title'] if google_binding else 'Локальный CSV'}
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
            self._persist_report()

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
        allowed = {'source', 'countries', 'limit', 'write', 'seed', 'category', 'query', 'proofs', 'facts_verified', 'enrich_sites'}
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
                    failure = state.get('error') or ((report or {}).get('source_error') or {}).get('reason', '')
                    if not success and source_access_blocked(failure):
                        active_row = db.execute('SELECT options FROM jobs WHERE id=?', (self.active,)).fetchone()
                        source = json.loads(active_row['options'])['source']
                        pending = db.execute("SELECT id,options FROM jobs WHERE enabled=1 AND status!='running'").fetchall()
                        for pending_row in pending:
                            if json.loads(pending_row['options']).get('source') == source:
                                db.execute("UPDATE jobs SET enabled=0,status='attention',error=? WHERE id=?",
                                           ('Источник приостановлен после отказа в доступе. Восстановите доступ и возобновите задание вручную.', pending_row['id']))
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
<label for="source">Платформа</label><select id="source"><option value="telegram-public">Telegram · публичный поиск кандидатов</option><option value="tgstat-web">Telegram · TGStat</option><option value="wb-web">Wildberries</option><option value="ozon-web">Ozon</option><option value="instagram-web">Instagram</option><option value="youtube-web">YouTube</option><option value="twitch-web">Twitch</option><option value="kick-web">Kick</option></select>
<div id="public-query" hidden><label for="query">Что искать?</label><input id="query" placeholder="Например: технологии, Беларусь" style="width:100%;padding:10px;background:#101820;color:white;border:1px solid #455464"><p class="hint">Без входа и оплаты. Страна и средний охват требуют проверки, автозапись отключена. Пустая фраза — поиск по словам «Россия» и «Беларусь».</p></div><div id="tg"><label for="topic">Тематика каналов</label><select id="topic"><option value="tech">Технологии</option><option value="">Другая категория TGStat</option></select><input id="category" hidden placeholder="Код категории TGStat" style="width:100%;padding:10px;background:#101820;color:white;border:1px solid #455464"><p class="hint">Вместо тематики можно задать конкретные каналы в дополнительных настройках.</p></div>
<label for="limit">Сколько проверить</label><input id="limit" type="number" min="1" max="10" value="4">
<div class="row"><button type="button" id="start">▶ Пуск</button><button type="button" class="secondary" id="stop" disabled>■ Стоп</button></div><p id="task-hint" class="hint">Найдём каналы выбранной тематики и проверим показатели и возможность рекламы.</p>
<button type="button" class="secondary" id="login">Войти в аккаунт</button><p class="hint" id="login-hint">Для первого входа откроется отдельный браузер. Войдите на сайте и закройте его окно. Программа сохранит сессию.</p>
<details><summary>Дополнительные настройки</summary><label for="seed">Конкретные каналы или ссылки (необязательно)</label><textarea id="seed" placeholder="@kod_ru или ссылки через пробел"></textarea><p class="hint" id="seed-hint">Если указаны каналы, поиск по тематике не используется.</p>
<div id="advertiser-facts" hidden><label>Проверенные факты о рекламодателях (необязательно)</label><input id="proof-file" type="file" accept=".json,application/json"><p id="proof-hint" class="hint">Файл JSON до 12 КБ: ключи — ссылки карточек; факты содержат value, url, checked_at, operator_verified. Без него неполные кандидаты останутся на проверке.</p><label><input id="facts-verified" type="checkbox"> Я проверил факты и источники в файле</label><label><input id="enrich-sites" type="checkbox" checked> Проверять доступность подтверждённого сайта и искать публичные контакты</label><p class="hint">Контакты — предложения. Их наличие не разрешает запись в ручные поля.</p></div><div class="countries"><label><input id="ru" type="checkbox" checked> РФ</label><label><input id="by" type="checkbox" checked> Беларусь</label></div>
<label class="write"><input id="write" type="checkbox"> Сохранять результат в таблицы</label><p class="hint">По умолчанию — просмотр. Ручные поля и заполненные ячейки защищены.</p>
<label for="interval">Повторять каждые (минут)</label><input id="interval" type="number" min="5" max="10080" value="60"><button type="button" class="secondary" id="schedule" style="margin-top:12px">Включить расписание</button><p class="hint">Работает, пока приложение или серверный сервис включён.</p></details></form></section>
<section><h2>Ход выполнения</h2><span id="status" class="badge">Готов к запуску</span><p id="notice" role="alert"></p><p id="storage-notice" class="hint" role="status"></p><div id="problem" hidden style="border:1px solid #bf7653;background:#392820;padding:18px;border-radius:10px;margin-top:20px"><strong id="problem-title"></strong><p id="problem-text"></p></div>
<div class="cards"><div class="metric"><strong id="accepted">—</strong><span>ПОДХОДЯТ</span></div><div class="metric"><strong id="review">—</strong><span>НУЖНА ПРОВЕРКА</span></div><div class="metric"><strong id="rejected">—</strong><span>ОТКЛОНЕНЫ</span></div></div><div id="results"><p class="hint">Выберите задачу и нажмите Пуск.</p></div>
<section id="review-form" hidden style="margin-top:20px"><h2>Проверка канала</h2><p id="review-title"></p><p class="hint">Укажите проверенные факты. Это личное подтверждение оператора, а не автоматическая проверка источников.</p><label>Страна</label><select id="review-country"><option value="">Выберите</option><option value="ru">РФ</option><option value="by">Беларусь</option></select><label>Источник подтверждения страны</label><input id="review-country-url" type="url" placeholder="https://..."><label>Средний охват публикации</label><input id="review-reach" type="number" min="0" step="1"><label>Источник среднего охвата (не медиана и не рекламный охват)</label><input id="review-reach-url" type="url" placeholder="https://..."><label>Рекламный контакт</label><select id="review-contact"></select><label>Источник предложения рекламы и контакта</label><input id="review-placement-url" type="url" placeholder="https://..."><label><input id="review-verified" type="checkbox"> Я проверил страну, средний охват, предложение рекламы и контакт</label><div class="row"><button id="review-preview" type="button">Показать изменения</button><button id="review-save" type="button" disabled>Сохранить в таблицу</button><button id="review-close" type="button" class="secondary">Закрыть</button></div><pre id="review-plan">Сначала проверьте факты и запросите план.</pre></section><details><summary>Подключить Google Sheets</summary><p class="hint">Доступ — через служебный аккаунт. Таблицу не нужно открывать для всех. Агент добавляет только новые строки; существующие значения, формулы и форматирование не редактируются. Не меняйте колонки во время записи.</p><label>JSON-ключ Google (только локально)</label><input type="file" id="google-key" accept=".json,application/json"><label><input type="checkbox" id="google-save-key"> Разрешаю сохранить выбранный ключ на этом компьютере для подключённых листов</label><button type="button" id="google-key-save" class="secondary">Сохранить ключ локально</button><p class="hint" id="google-status"></p><label>Ссылка на конкретный лист Google Sheets</label><input type="url" id="google-url" placeholder="https://docs.google.com/spreadsheets/d/…/edit#gid=0" style="width:100%;padding:10px"><label>Тип данных</label><select id="google-type"><option value="">Определить по заголовкам</option><option value="advertisers">Рекламодатели — 22 колонки</option><option value="telegram">Telegram — 14 колонок</option><option value="streamers">Стримеры — 13 колонок</option></select><label><input type="checkbox" id="google-replace"> Заменить уже подключённый лист этого типа (данные в нём не удаляются)</label><button type="button" id="google-attach" class="secondary">Проверить и подключить</button><pre id="google-map">Лист и расположение полей появятся после проверки ссылки.</pre></details><details><summary>Мои таблицы</summary><label>Таблица</label><select id="table-select"><option value="advertisers">Рекламодатели</option><option value="telegram">Telegram-каналы</option><option value="streamers">Стримеры Twitch/Kick</option></select><div class="row"><button type="button" class="secondary" id="table-load">Открыть таблицу</button><button type="button" class="secondary" id="table-download" disabled>Скачать CSV</button></div><p class="hint" id="table-notice">Просмотр и скачивание не меняют данные. Здесь нет удаления и перезаписи ячеек.</p><div id="table-view" style="overflow:auto;max-height:400px"></div></details><details><summary>Расписание</summary><div id="jobs"></div><p id="scheduler-error" class="hint"></p></details><details><summary>Подробный отчёт</summary><button type="button" id="download-report" class="secondary" disabled>Скачать отчёт JSON</button><pre id="report">Нет задания</pre></details><details><summary>Диагностика</summary><pre id="error">Нет сообщений</pre></details></section></main>
<script>
const $=id=>document.getElementById(id),token=__TOKEN__;
let reviewIndex=null,reviewToken=null,reviewSnapshot=null;
function clearReviewPlan(){reviewToken=null;$('review-save').disabled=true;}
function openReview(index,decision){reviewIndex=index;reviewSnapshot={candidate_url:decision.candidate['Ссылка'],observed_at:decision.candidate['Последнее обновление']};clearReviewPlan();$('review-form').hidden=false;$('review-title').textContent=decision.candidate['Название'];for(const id of ['review-country','review-country-url','review-reach','review-reach-url','review-placement-url'])$(id).value='';$('review-verified').checked=false;$('review-plan').textContent='Сначала проверьте факты и запросите план.';const select=$('review-contact');select.replaceChildren();for(const contact of decision.contact_suggestions||[]){const option=document.createElement('option');option.value=contact;option.textContent=contact;select.append(option);}}
for(const id of ['review-country','review-country-url','review-reach','review-reach-url','review-placement-url','review-contact','review-verified'])$(id).oninput=clearReviewPlan;
$('review-close').onclick=()=>{$('review-form').hidden=true;clearReviewPlan();};
$('review-preview').onclick=async()=>{clearReviewPlan();try{const reach=$('review-reach').value.trim();if(!/^[0-9]+$/.test(reach))throw Error('Введите целое неотрицательное число среднего охвата.');const r=await api('/api/review/preview',{...reviewSnapshot,index:reviewIndex,country:$('review-country').value,country_url:$('review-country-url').value,reach:Number(reach),reach_url:$('review-reach-url').value,placement_url:$('review-placement-url').value,contact:$('review-contact').value,verified:$('review-verified').checked});$('review-plan').textContent=reviewPlanText(r.plan);reviewToken=r.token;$('review-save').disabled=false;}catch(e){$('review-plan').textContent=e.message;}};
$('review-save').onclick=async()=>{const token=reviewToken;clearReviewPlan();try{const r=await api('/api/review/commit',{token});$('review-plan').textContent='Обработано.\\n'+reviewPlanText(r.result);$('notice').textContent='Проверенный канал обработан. Ручные и заполненные поля защищены.';}catch(e){$('review-plan').textContent=e.message;}};
async function refreshGoogle(){try{const state=await api('/api/google/status');const names={advertisers:'Рекламодатели',telegram:'Telegram',streamers:'Стримеры'};$('google-status').textContent=(state.configured?'Ключ сохранён. Предоставьте доступ «Редактор» этому адресу: '+state.email:'Выберите JSON-ключ служебного аккаунта Google.')+' Подключения: '+Object.entries(state.links||{}).map(([type,link])=>(names[type]||type)+' → '+link.sheet_title).join('; ');}catch(e){$('google-status').textContent=e.message;}}
$('google-key-save').onclick=async()=>{try{const file=$('google-key').files[0];if(!file||file.size>12000)throw Error('Выберите JSON-ключ Google размером до 12 КБ.');if(!$('google-save-key').checked)throw Error('Подтвердите локальное сохранение ключа.');await api('/api/google/key',{credentials:JSON.parse(await file.text()),save_locally:true});$('google-key').value='';$('google-save-key').checked=false;await refreshGoogle();}catch(e){$('google-status').textContent=e.message;}};
$('google-attach').onclick=async()=>{const button=$('google-attach');button.disabled=true;try{const result=await api('/api/google/attach',{url:$('google-url').value,table:$('google-type').value,replace:$('google-replace').checked});const modes={unavailable:'колонка отсутствует — не записывается',manual:'ручное поле — не заполняется',confirm:'требует подтверждения', 'auto-new-row':'автоматически, только в новой строке'};$('google-map').textContent='Подключён лист: '+result.binding.sheet_title+' ('+result.table+').'+String.fromCharCode(10)+result.columns.map(c=>c.column+' → '+c.field+' — '+modes[c.mode]).join(String.fromCharCode(10));await refreshGoogle();}catch(e){$('google-map').textContent=e.message;}finally{button.disabled=false;}};
let loadedTable=null;
function tableCSV(value){return '\ufeff'+[value.columns,...value.rows.map(row=>value.columns.map(column=>row[column]??''))].map(row=>row.map(cell=>'"'+String(cell).replaceAll('"','""')+'"').join(',')).join('\\r\\n')+'\\r\\n';}
$('table-load').onclick=async()=>{loadedTable=null;$('table-download').disabled=true;try{const value=await api('/api/tables/'+$('table-select').value);loadedTable=value;const table=document.createElement('table');table.style.borderCollapse='collapse';for(const cells of [value.columns,...value.rows.slice(0,100).map(row=>value.columns.map(c=>row[c]))]){const tr=document.createElement('tr');for(const cell of cells){const td=document.createElement('td');td.textContent=cell??'';td.style.cssText='border:1px solid #455464;padding:8px;white-space:pre-wrap;min-width:110px';tr.append(td);}table.append(tr);}$('table-view').replaceChildren(table);$('table-notice').textContent='Показано '+Math.min(value.rows.length,100)+' из '+value.rows.length+' строк. CSV содержит все строки. Данные не изменены.';$('table-download').disabled=false;}catch(e){$('table-view').replaceChildren();$('table-notice').textContent=e.message;}};
$('table-select').onchange=()=>{loadedTable=null;$('table-download').disabled=true;$('table-view').replaceChildren();};
$('table-download').onclick=()=>{if(!loadedTable)return;const url=URL.createObjectURL(new Blob([tableCSV(loadedTable)],{type:'text/csv;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=loadedTable.table+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
function reviewPlanText(plan){const actions={add:'Будет добавлена новая строка',update:'Будут заполнены пустые поля',skip:'Изменений нет'};return [actions[plan.action]||'Требуется проверка',...(plan.target?['Лист: '+plan.target.sheet_title+', строка на снимке: '+plan.target.row,...plan.target.cells.map(c=>c.cell+' → '+c.field)]:[]),...(plan.changes||[]).map(item=>item.field+': '+(item.before===''?'[пусто]':item.before)+' → '+item.after),...(plan.warnings||[]).map(value=>'Внимание: '+value)].join('\\n');}
function suggestedContacts(value){return [...new Set([...(value.contact_suggestions||[]),...(value.enrichment?.contact_suggestions||[])])];}
let uploadedFacts=null;
function clearFacts(){uploadedFacts=null;$('proof-file').value='';$('facts-verified').checked=false;$('proof-hint').textContent='Файл JSON до 12 КБ с проверенными фактами. Ключи — прямые ссылки карточек.';}
$('proof-file').onchange=async()=>{uploadedFacts=null;$('facts-verified').checked=false;updateSource();try{const file=$('proof-file').files[0];if(!file)return;if(file.size>12000)throw Error('Файл больше 12 КБ.');const value=JSON.parse(await file.text());if(!value||Array.isArray(value)||typeof value!=='object')throw Error('Нужен JSON-объект с ключами-ссылками.');uploadedFacts=value;updateSource();$('proof-hint').textContent='Загружено карточек: '+Object.keys(value).length+'. Проверьте файл и отметьте подтверждение. Если ссылки пусты, используются карточки из файла.';}catch(e){$('proof-hint').textContent=e.message;}};
let latestReport=null;
function candidateLink(value){try{const url=new URL(value);if(!['https:','http:'].includes(url.protocol)||url.username||url.password)return null;return url.href;}catch{return null;}}
$('download-report').onclick=()=>{if(!latestReport)return;const blob=new Blob([JSON.stringify(latestReport,null,2)],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='agent-report.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
const groups={telegram:['telegram-public','tgstat-web'],advertisers:['wb-web','ozon-web','instagram-web','youtube-web'],streamers:['twitch-web','kick-web']};
function updateSource(){const source=$('source').value;const publicSource=source==='telegram-public';$('advertiser-facts').hidden=$('task').value!=='advertisers';$('public-query').hidden=!publicSource;$('write').disabled=publicSource;if(publicSource)$('write').checked=false;$('login').disabled=publicSource;$('login-hint').textContent=publicSource?'Этот источник работает без аккаунта. Найденные каналы требуют проверки страны и охвата.':'Войдите на сайте в отдельном браузере и закройте его окно. Сессия сохранится.';$('tg').hidden=source!=='tgstat-web';const direct=['instagram-web','youtube-web'].includes(source);$('seed').required=direct&&!(uploadedFacts&&Object.keys(uploadedFacts).length);if(direct)$('seed').closest('details').open=true;$('seed-hint').textContent=direct?'Для этой платформы нужны прямые ссылки на профили. Самостоятельный поиск ещё не реализован.':['tgstat-web','telegram-public'].includes(source)?'Конкретные каналы имеют приоритет над поисковой фразой и тематикой.':'Можно оставить пустым — программа выберет карточки из каталога.';$('task-hint').textContent=$('task').value==='telegram'?(publicSource?'Найдём кандидатов. Страна, средний охват и возможность рекламы пока не подтверждены.':'Проверим каналы, охваты и возможность размещения рекламы.'):$('task').value==='advertisers'?'Соберём кандидатов. Без подтверждённых фактов бренд останется на проверке.':'Проверим профили стримеров. Неподтверждённые показатели останутся пустыми.';}
$('task').onchange=()=>{const allowed=groups[$('task').value];for(const option of $('source').options){option.hidden=!allowed.includes(option.value);option.disabled=option.hidden;}$('source').value=allowed[0];$('seed').value='';clearFacts();updateSource();};
$('topic').onchange=()=>{$('category').hidden=!!$('topic').value;};
$('source').onchange=()=>{clearFacts();updateSource();};
$('task').onchange();
async function api(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-Control-Token':token},body:data?JSON.stringify(data):undefined});const value=await r.json();if(!r.ok)throw Error(value.error||'Ошибка запроса');return value;}
function options(){return {source:$('source').value,seed:$('seed').value,category:$('seed').value.trim()?'':($('topic').value||$('category').value),query:$('query').value,countries:['ru','by'].filter(x=>$(x).checked),limit:Number($('limit').value),write:$('write').checked,proofs:$('task').value==='advertisers'?uploadedFacts:null,facts_verified:$('facts-verified').checked,enrich_sites:$('task').value==='advertisers'&&$('enrich-sites').checked};}
$('form').onsubmit=e=>e.preventDefault();
$('start').onclick=async e=>{e.preventDefault();if(!$('form').reportValidity())return;$('notice').textContent='';try{await api('/api/start',options());await refresh();}catch(e){$('notice').textContent=e.message;}};
$('schedule').onclick=async()=>{try{await api('/api/jobs/add',{options:options(),interval_seconds:Number($('interval').value)*60});$('notice').textContent='Задание сохранено. Первый запуск начнётся автоматически.';await refreshJobs();}catch(e){$('notice').textContent=e.message;}};
async function refreshJobs(){try{const value=await api('/api/jobs');$('scheduler-error').textContent=value.error||'';const list=$('jobs');list.replaceChildren();for(const j of value.jobs){const row=document.createElement('div');row.className='item';const text=document.createElement('div');text.textContent=j.options.source+' · '+(j.options.write?'запись':'просмотр')+' · каждые '+j.interval_seconds/60+' мин · '+({queued:'В очереди',running:'Выполняется',waiting:'Ожидает',paused:'Пауза',attention:'Нужна проверка'}[j.status]||j.status);const detail=document.createElement('small');detail.textContent=j.error||(j.enabled?'Следующий запуск: '+new Date(j.next_run*1000).toLocaleString():'Автозапуск отключён');const toggle=document.createElement('button');toggle.className='secondary';toggle.style.marginTop='10px';toggle.textContent=j.enabled?'Пауза':'Возобновить';toggle.disabled=j.status==='running';toggle.onclick=async()=>{try{await api('/api/jobs/toggle',{id:j.id,enabled:!j.enabled});await refreshJobs();}catch(e){$('notice').textContent=e.message;}};row.append(text,detail,toggle);if(j.report){const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='Последний отчёт';pre.textContent=JSON.stringify(j.report,null,2);details.append(summary,pre);row.append(details);}list.append(row);}if(!value.jobs.length)list.textContent='Заданий пока нет.';}catch(e){$('scheduler-error').textContent=e.message;}}

$('login').onclick=async()=>{try{await api('/api/session',{source:$('source').value});$('notice').textContent='Войдите в открытом браузере и закройте его окно. Пароли и коды вводите только на сайте.';await refresh();}catch(e){$('notice').textContent=e.message;}};
$('stop').onclick=async()=>{try{await api('/api/stop',{});await refresh();}catch(e){$('notice').textContent=e.message;}};
async function refresh(){try{const s=await api('/api/status');const names={idle:'Готов к запуску',running:'Идёт сбор',stopping:'Остановка',completed:'Завершён',attention:'Нужна проверка',cancelled:'Остановлен'};$('status').textContent=s.status==='running'&&s.operation==='login'?'Ожидаем входа в браузере':names[s.status]||s.status;const busy=['running','stopping'].includes(s.status);$('start').disabled=busy;$('login').disabled=busy||$('source').value==='telegram-public';$('task').disabled=busy;$('source').disabled=busy;$('stop').disabled=!busy;$('problem').hidden=!s.problem;if(s.problem){$('notice').textContent='';$('problem-title').textContent=s.problem.title;$('problem-text').textContent=s.problem.message;}$('storage-notice').textContent=s.report_warning||(s.restored?'Восстановлен последний отчёт. Просроченные факты нужно проверить заново.':'');const r=s.report||{};latestReport=s.report||null;$('download-report').disabled=!latestReport;if(r.session_status==='saved_unverified')$('notice').textContent=r.message;const counts=r.source_summary||{};for(const [id,key] of [['accepted','accept'],['review','review'],['rejected','reject']])$(id).textContent=counts[key]??'—';$('report').textContent=s.report?JSON.stringify(s.report,null,2):'Нет отчёта';$('error').textContent=s.error||'Нет сообщений';if(s.destination)$('status').textContent+=' · '+s.destination;if(s.status==='completed'&&s.report&&!s.report.session_setup){$('notice').textContent='Поиск завершён. '+(s.write?'Разрешённые данные сохранены.':'Режим просмотра: таблицы не изменены.');}const err=(s.error||'')+JSON.stringify(r.source_error||{});if(err.includes('auth_required'))$('notice').textContent='Нужен вход в рабочий аккаунт. Задание не ждёт ввода; восстановите сессию отдельно.';const list=$('results');list.replaceChildren();if(s.report){for(const d of (r.decisions||[])){const row=document.createElement('div');row.className='item';const c=d.candidate||{};const title=document.createElement('strong');title.textContent=c['Название компании']||c['Название']||c['Стример']||d.product_observation?.title||'Кандидат без подтверждённого имени';const info=document.createElement('small');info.textContent=({accept:'Принят',review:'Требует проверки',reject:'Отклонён'}[d.decision]||d.decision)+' · '+((d.reasons||[]).join(' / ')||'Проверки пройдены');row.append(title,info);const facts=document.createElement('small');facts.textContent=[c['Ссылка'],c['Подписчики']!==undefined?c['Подписчики'].toLocaleString()+' подписчиков':'',typeof d.score==='number'?'Score: '+d.score:'',suggestedContacts(d).length?'Контакты для проверки: '+suggestedContacts(d).join(', '):''].filter(Boolean).join(' · ');row.append(facts);const href=candidateLink(c['Ссылка']||c['Сайт']||d.evidence?.url);if(href){const link=document.createElement('a');link.href=href;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Открыть источник';link.style.color='#79e0ba';row.append(link);}if(r.source==='telegram-public'&&d.decision==='review'&&d.placement?.status==='offered'&&(d.contact_suggestions||[]).length){const button=document.createElement('button');button.type='button';button.className='secondary';const reviewed=(r.manual_reviews||[]).some(item=>item.candidate?.['Ссылка']===c['Ссылка']);button.textContent=reviewed?'Проверка выполнена':'Проверить и сохранить';button.disabled=reviewed;button.onclick=()=>openReview(r.decisions.indexOf(d),d);row.append(button);}list.append(row);}if(!(r.decisions||[]).length)list.textContent='Нет принятых или рассмотренных кандидатов.';}}catch(e){$('status').textContent='Панель недоступна';}}
refresh();refreshJobs();refreshGoogle();setInterval(refresh,2000);setInterval(refreshJobs,5000);
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
            elif self.path.startswith('/api/tables/'):
                from safe_writer import StorageError
                from validation import ValidationError
                try:
                    self.send(200, controller.table_snapshot(self.path.removeprefix('/api/tables/')))
                except (ValueError, StorageError, ValidationError, OSError):
                    self.send(400, {'error': 'Таблицу не удалось прочитать. Проверьте выбранную таблицу, её заголовки и доступ к папке данных.'})
            elif self.path == '/api/google/status':
                from safe_writer import StorageError
                try:
                    self.send(200,controller.google_status())
                except (StorageError,OSError):
                    self.send(400,{'error':'Настройки Google недоступны. Проверьте локальный ключ и подключения.'})
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
                elif self.path in ('/api/google/key','/api/google/attach'):
                    from safe_writer import StorageError
                    from validation import ValidationError
                    if scheduler.active is not None:
                        raise ValueError('Дождитесь завершения задания расписания.')
                    try:
                        result=controller.google_setup(body,credentials=self.path.endswith('/key'))
                    except (StorageError,ValidationError) as error:
                        raise ValueError(str(error)) from error
                    self.send(200,result)
                    return
                elif self.path in ('/api/review/preview', '/api/review/commit'):
                    if scheduler.active is not None:
                        raise ValueError('Дождитесь завершения задания расписания.')
                    from safe_writer import StorageError
                    from validation import ValidationError
                    try:
                        result = controller.review_telegram(body, commit=self.path.endswith('/commit'))
                    except (StorageError, ValidationError) as error:
                        raise ValueError(str(error)) from error
                    self.send(200, result)
                    return
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
