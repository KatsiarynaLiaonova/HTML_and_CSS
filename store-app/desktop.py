"""ALI BABA desktop window. The local service and database never leave this computer."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

import server

APP_NAME = 'ALI BABA'


def notify(message, error=True):
    """Keep startup errors visible in a windowed executable with no console."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(APP_NAME, message, parent=root)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def self_test():
    # This checks the packaged resources and dependency, without a display or user data.
    import webview
    assert callable(webview.create_window)
    with tempfile.TemporaryDirectory() as folder:
        original = server.DATA
        try:
            server.DATA = Path(folder)
            server.initialize()
            assert (server.ROOT / 'static' / 'extra.js').is_file()
            with server.connect() as db:
                assert server.snapshot(db)['version'] == server.VERSION
        finally:
            server.DATA = original


def run(smoke_test=False):
    import webview

    service = None
    stop = threading.Event()
    failure = []
    with socket.socket() as probe:
        occupied = probe.connect_ex(('127.0.0.1', 8765)) == 0
    if occupied:
        raise RuntimeError(
            'ALI BABA is already running, or port 8765 is in use. '
            'Switch to its window or close the previous version before updating.\n\n'
            'ALI BABA уже запущена или порт 8765 занят. Переключитесь в ее окно '
            'или завершите предыдущую версию перед обновлением.'
        )
    try:
        service = server.create_server()
        threading.Thread(target=service.serve_forever, daemon=True).start()
        threading.Thread(target=server.daily_backup, args=(stop,), daemon=True).start()
        webview.settings['ALLOW_DOWNLOADS'] = True
        window = webview.create_window(
            APP_NAME + ' · ' + server.VERSION,
            'http://127.0.0.1:8765', width=1280, height=840,
            min_size=(900, 600), background_color='#f3f5f0',
            confirm_close=not smoke_test, hidden=smoke_test,
            localization={
                'global.quitConfirmation': 'Close ALI BABA? / Завершить работу ALI BABA?',
                'global.quit': 'Close / Завершить', 'global.cancel': 'Cancel / Отмена',
            },
        )

        def choose_folder():
            selected = window.create_file_dialog(webview.FileDialog.FOLDER)
            return selected[0] if selected else ''

        server.CHOOSE_FOLDER = choose_folder

        def check_window():
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    ready = window.evaluate_js(
                        "document.title.includes('ALI BABA') && "
                        "document.querySelector('#scan') !== null"
                    )
                    if ready:
                        with urlopen('http://127.0.0.1:8765/api/state', timeout=5) as response:
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
        if service:
            service.shutdown()
            service.server_close()
            # Finish any operation that was already being handled before taking the final copy.
            with server.LOCK:
                with server.connect() as db:
                    server.backup(db)


def main():
    try:
        if '--self-test' in sys.argv:
            self_test()
        elif '--desktop-smoke-test' in sys.argv:
            with tempfile.TemporaryDirectory() as folder:
                server.DATA = Path(folder)
                run(smoke_test=True)
        else:
            run()
        return 0
    except Exception as exc:
        if '--self-test' in sys.argv or '--desktop-smoke-test' in sys.argv:
            raise
        notify(
            str(exc) + '\n\nFor a source installation, run Install-Mac.command or '
            'Install-Windows.cmd again. Windows also needs Microsoft Edge WebView2 Runtime.\n'
            'Для исходников повторите установку. На Windows необходим Microsoft Edge WebView2 Runtime.'
        )
        return 1


if __name__ == '__main__':
    sys.exit(main())
