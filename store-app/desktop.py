"""Windowed launcher used by shortcuts and the standalone desktop bundles."""
import json
import sys
import tempfile
from pathlib import Path
import socket
import threading
import tkinter as tk
from tkinter import messagebox, filedialog
from urllib.request import urlopen
import webbrowser
import server


def main():
    root = tk.Tk()
    root.title('Nile Store ' + server.VERSION)
    root.geometry('460x260')
    root.resizable(False, False)
    root.configure(bg='#f3f5f0')
    def choose_folder():
        done = threading.Event()
        result = {'folder': ''}
        def pick():
            try:
                result['folder'] = filedialog.askdirectory(parent=root, title='Backup folder / Папка копий')
            finally:
                done.set()
        root.after(0, pick)
        if not done.wait(180):
            raise ValueError('Folder selection timed out')
        return result['folder']
    server.CHOOSE_FOLDER = choose_folder
    service = None
    stop = threading.Event()
    try:
        # Do not start a second database service when the user clicks twice.
        with socket.socket() as probe:
            occupied = probe.connect_ex(('127.0.0.1', 8765)) == 0
        if occupied:
            try:
                with urlopen('http://127.0.0.1:8765/api/state', timeout=3) as response:
                    current = json.load(response)
                if current.get('version') == server.VERSION:
                    webbrowser.open('http://127.0.0.1:8765')
                    root.destroy()
                    return
            except (OSError, ValueError):
                pass
            raise RuntimeError('Port 8765 is in use. Close the previous Nile Store before opening this version.')
        service = server.create_server()
        threading.Thread(target=service.serve_forever, daemon=True).start()
        threading.Thread(target=server.daily_backup, args=(stop,), daemon=True).start()
    except Exception as exc:
        messagebox.showerror('Nile Store', str(exc), parent=root)
        if service:
            service.server_close()
        root.destroy()
        return

    tk.Label(root, text='◈ NILE STORE', font=('Arial', 24, 'bold'), fg='#123d33', bg='#f3f5f0').pack(pady=(25, 12))
    tk.Label(root, text='Local store is running / Программа работает', bg='#f3f5f0').pack()
    tk.Button(root, text='Open store / Открыть магазин', command=lambda: webbrowser.open('http://127.0.0.1:8765')).pack(pady=16)

    def close():
        if not messagebox.askyesno('Nile Store', 'Stop the store? / Завершить работу?', parent=root):
            return
        stop.set()
        service.shutdown()
        service.server_close()
        try:
            with server.connect() as db:
                server.backup(db)
        except OSError as exc:
            messagebox.showwarning('Backup / Копия', str(exc), parent=root)
        root.destroy()

    tk.Button(root, text='Stop / Завершить', command=close).pack()
    root.protocol('WM_DELETE_WINDOW', close)
    webbrowser.open('http://127.0.0.1:8765')
    root.mainloop()

if __name__ == '__main__':
    if '--self-test' in sys.argv:
        with tempfile.TemporaryDirectory() as folder:
            server.DATA = Path(folder)
            server.initialize()
            assert (server.ROOT / 'static' / 'extra.js').is_file()
            with server.connect() as db:
                assert server.snapshot(db)['version'] == server.VERSION
    else:
        main()
