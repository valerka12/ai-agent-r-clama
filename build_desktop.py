"""Сборка на целевой ОС: python build_desktop.py (macOS либо Windows)."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    if sys.platform not in ('darwin', 'win32'):
        raise SystemExit('Сборку выполняйте на macOS или Windows.')
    root = Path(__file__).resolve().parent
    browser = root / 'cache' / 'ms-playwright'
    if not any(browser.glob('chromium-*')):
        print('Сначала установите Chromium для текущей ОС. Инструкция в README.', file=sys.stderr)
        return 1
    target = root / 'outputs' / ('desktop-' + platform.system().lower() + '-' + platform.machine().lower())
    build = root / 'work' / 'desktop-build'
    build.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / 'cache'))
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
               '--name', 'AdResearchAgent', '--distpath', str(target), '--workpath', str(build / 'work'),
               '--specpath', str(build),
               '--hidden-import', 'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.winforms',
               '--collect-data', 'webview']
    for module in ('agent', 'control_panel', 'config', 'safe_writer', 'validation', 'dedup', 'wb_parser',
                   'ozon_parser', 'instagram_parser', 'youtube_parser', 'tg_parser', 'twitch_parser', 'kick_parser'):
        command += ['--hidden-import', module]
    command += [str(root / 'desktop.py')]
    subprocess.run(command, cwd=root, env=environment, check=True)
    # Chromium копируется рядом целиком: PyInstaller не переписывает подписи
    # и структуру vendor .app/framework, необходимые для macOS.
    browser_target = target / 'browser_engine' if sys.platform == 'darwin' else target / 'AdResearchAgent' / 'browser_engine'
    if browser_target.is_symlink():
        raise SystemExit('Каталог browser_engine не должен быть символьной ссылкой.')
    if browser_target.exists():
        shutil.rmtree(browser_target)
    shutil.copytree(browser, browser_target, symlinks=True)
    print('Сборка создана:', target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
