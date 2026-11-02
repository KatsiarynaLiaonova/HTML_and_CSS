"""ALI BABA desktop window. The local service and database never leave this computer."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

import server
from access import Accounts
from connections import create_gateway, read_config, start_lan_server
from urllib.parse import urlsplit

APP_NAME = 'ALI BABA'


class DesktopActions:
    def configure_connection(self):
        command = [sys.executable, '--configure-network'] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve()), '--configure-network']
        subprocess.Popen(command)
        return True


def notify(message, error=True, offer_setup=False):
    """Keep startup errors visible in a windowed executable with no console."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(APP_NAME, message, parent=root)
        configure = offer_setup and messagebox.askyesno(APP_NAME, 'Открыть настройки подключения? / Open connection settings?', parent=root)
        root.destroy()
        if configure:
            from network_setup import configure
            configure(server.DATA)
    except Exception:
        print(message, file=sys.stderr)


def self_test():
    # This checks the packaged resources and dependency, without a display or user data.
    import webview
    assert callable(webview.create_window)
    import certifi
    assert Path(certifi.where()).is_file()
    with tempfile.TemporaryDirectory() as folder:
        original = server.DATA
        try:
            server.DATA = Path(folder)
            server.initialize()
            assert (server.ROOT / 'static' / 'extra.js').is_file()
            assert (server.ROOT / 'NETWORK-SETUP.md').is_file()
            with server.connect() as db:
                assert server.snapshot(db)['version'] == server.VERSION
        finally:
            server.DATA = original


def run(smoke_test=False, shared_smoke=False):
    import webview

    service = None
    lan_service = None
    service_started = False
    lan_started = False
    stop = threading.Event()
    failure = []
    config = {'mode': 'local'} if smoke_test else read_config(server.DATA)
    if shared_smoke:
        accounts = Accounts(server.DATA)
        accounts.create('smoke-owner', 'Isolated smoke password!', 'owner', initial=True)
        config = {'mode': 'main', 'address': '127.0.0.1'}
    mode = config.get('mode', 'local')
    if mode not in ('local', 'main', 'client'):
        raise ValueError('Unknown connection mode. Open connection settings again')
    probe_port = 8766 if mode == 'client' else 8765
    with socket.socket() as probe:
        occupied = probe.connect_ex(('127.0.0.1', probe_port)) == 0
    if occupied:
        raise RuntimeError(
            f'ALI BABA is already running, or port {probe_port} is in use. '
            'Switch to its window or close the previous version before updating.\n\n'
            f'ALI BABA уже запущена или порт {probe_port} занят. Переключитесь в ее окно '
            'или завершите предыдущую версию перед обновлением.'
        )
    try:
        if mode == 'client':
            service = create_gateway(config['url'], config.get('certificate') or None, port=8766)
        elif mode == 'main':
            accounts = Accounts(server.DATA)
            if not accounts.has_owner():
                raise ValueError('Open connection settings and initialize the owner account')
            hosts = {'127.0.0.1:8765', 'localhost:8765'}
            if config.get('remote_url'):
                hosts.add(urlsplit(config['remote_url']).netloc)
            service = server.create_server(accounts=accounts, allowed_hosts=hosts)
            service.allowed_origins = {'http://127.0.0.1:8765', 'http://localhost:8765'}
            if config.get('remote_url'):
                service.allowed_origins.add(config['remote_url'].rstrip('/'))
            lan_service = start_lan_server(server.Handler, accounts, server.DATA / 'network', config['address'])
            threading.Thread(target=lan_service.serve_forever, daemon=True).start()
            lan_started = True
        else:
            service = server.create_server()
        base_url = f'http://127.0.0.1:{service.server_port if mode == "client" else 8765}'
        threading.Thread(target=service.serve_forever, daemon=True).start()
        service_started = True
        if mode != 'client':
            threading.Thread(target=server.daily_backup, args=(stop,), daemon=True).start()
        webview.settings['ALLOW_DOWNLOADS'] = True
        window = webview.create_window(
            APP_NAME + ' · ' + server.VERSION,
            base_url, width=1280, height=840,
            min_size=(900, 600), background_color='#f3f5f0',
            confirm_close=not smoke_test, hidden=smoke_test,
            localization={
                'global.quitConfirmation': ('Stop the main register? Other computers will disconnect. / Остановить главную кассу? Другие компьютеры отключатся.' if mode == 'main' else 'Close ALI BABA? / Завершить работу ALI BABA?'),
                'global.quit': 'Close / Завершить', 'global.cancel': 'Cancel / Отмена',
            },
        )
        window.expose(DesktopActions().configure_connection)

        def choose_folder():
            selected = window.create_file_dialog(webview.FileDialog.FOLDER)
            return selected[0] if selected else ''

        server.CHOOSE_FOLDER = choose_folder if mode != 'client' else None

        def check_window():
            try:
                deadline = time.monotonic() + 30
                signed_in = False
                while time.monotonic() < deadline:
                    if shared_smoke and not signed_in and window.evaluate_js("document.querySelector('#loginForm') !== null"):
                        window.evaluate_js("document.querySelector('#loginForm [name=login]').value='smoke-owner';document.querySelector('#loginForm [name=password]').value='Isolated smoke password!';document.querySelector('#loginForm button').click();")
                        signed_in = True
                    ready = window.evaluate_js(
                        "document.title.includes('ALI BABA') && " +
                        ("document.querySelector('#accountBar') !== null" if shared_smoke else "document.querySelector('#scan') !== null") +
                        " && typeof window.pywebview?.api?.configure_connection === 'function'"
                    )
                    if ready:
                        if shared_smoke:
                            assert window.evaluate_js('state.version') == server.VERSION
                        else:
                            with urlopen(base_url + '/api/state', timeout=5) as response:
                                assert json.load(response)['version'] == server.VERSION
                        return
                    time.sleep(.1)
                raise RuntimeError('The native window did not load the checkout screen')
            except Exception as exc:
                failure.append(exc)
            finally:
                window.destroy()

        # Use the current OS engine; do not fall back to Internet Explorer on Windows.
        gui = 'edgechromium' if sys.platform == 'win32' else 'cocoa' if sys.platform == 'darwin' else None
        webview.start(
            check_window if smoke_test else None, gui=gui,
            private_mode=False, storage_path=str(server.DATA / 'webview'),
        )
        if failure:
            raise failure[0]
    finally:
        stop.set()
        server.CHOOSE_FOLDER = None
        if lan_service:
            if lan_started:
                lan_service.shutdown()
            lan_service.server_close()
        if service:
            if service_started:
                service.shutdown()
            service.server_close()
            # Finish any operation that was already being handled before taking the final copy.
            if mode != 'client':
                with server.LOCK:
                    with server.connect() as db:
                        server.backup(db)


def main():
    try:
        if '--setup-smoke-test' in sys.argv:
            from network_setup import configure
            with tempfile.TemporaryDirectory() as folder:
                configure(Path(folder), smoke_test=True)
        elif '--configure-network' in sys.argv:
            from network_setup import configure
            configure(server.DATA)
        elif '--self-test' in sys.argv:
            self_test()
        elif '--desktop-smoke-test' in sys.argv or '--shared-desktop-smoke-test' in sys.argv:
            self_test()
            with tempfile.TemporaryDirectory() as folder:
                server.DATA = Path(folder)
                run(smoke_test=True, shared_smoke='--shared-desktop-smoke-test' in sys.argv)
        else:
            run()
        return 0
    except Exception as exc:
        if any(flag in sys.argv for flag in ('--self-test', '--desktop-smoke-test', '--shared-desktop-smoke-test', '--setup-smoke-test')):
            raise
        notify(
            str(exc) + '\n\nFor a source installation, run Install-Mac.command or '
            'Install-Windows.cmd again. Windows also needs Microsoft Edge WebView2 Runtime.\n'
            'Для исходников повторите установку. На Windows необходим Microsoft Edge WebView2 Runtime.',
            offer_setup=(server.DATA / 'connection.json').is_file() and 'already running' not in str(exc),
        )
        return 1


if __name__ == '__main__':
    sys.exit(main())
