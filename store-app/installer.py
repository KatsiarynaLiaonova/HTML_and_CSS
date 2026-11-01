"""Install the source edition with an isolated desktop runtime and app shortcut."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
import venv

ROOT = Path(__file__).parent
FILES = ('server.py', 'desktop.py', 'static', 'README.md', 'requirements-desktop.txt')


def install_files(target):
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target / name)


def install_runtime(target):
    """Avoid modifying the user's system Python or another application's packages."""
    folder = target / '.venv'
    venv.EnvBuilder(with_pip=True).create(folder)
    python = folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    subprocess.run(
        [str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
         '-r', str(target / 'requirements-desktop.txt')],
        check=True, stdin=subprocess.DEVNULL,
    )
    return python


def install():
    if sys.platform == 'darwin':
        app = Path.home() / 'Applications' / 'ALI BABA.app'
        resources = app / 'Contents' / 'Resources'
        install_files(resources)
        python = install_runtime(resources)
        executable = app / 'Contents' / 'MacOS' / 'AliBaba'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(
            '#!/bin/sh\n'
            'resources_dir=$(CDPATH= cd "$(dirname "$0")/../Resources" && pwd)\n'
            'exec "$resources_dir/.venv/bin/python" "$resources_dir/desktop.py"\n'
        )
        executable.chmod(0o755)
        with (app / 'Contents' / 'Info.plist').open('wb') as output:
            plistlib.dump({
                'CFBundleExecutable': 'AliBaba', 'CFBundleName': 'ALI BABA',
                # Retain the identity used by earlier releases.
                'CFBundleIdentifier': 'local.nilestore.desktop',
                'CFBundleVersion': '2.1.1', 'CFBundleShortVersionString': '2.1.1',
                'CFBundlePackageType': 'APPL', 'NSHighResolutionCapable': True,
            }, output)
        subprocess.run(['open', str(app.parent)], check=False)
        return str(app)
    if sys.platform == 'win32':
        target = Path(os.environ['LOCALAPPDATA']) / 'ALI BABA'
        install_files(target)
        python = install_runtime(target)
        pythonw = python.with_name('pythonw.exe')
        if not pythonw.exists():
            raise RuntimeError('pythonw.exe not found. Install Python with the Windows desktop components.')
        # Values pass through environment variables, not PowerShell code interpolation.
        env = dict(os.environ, ALIBABA_PYTHON=str(pythonw), ALIBABA_SCRIPT=str(target / 'desktop.py'), ALIBABA_FOLDER=str(target))
        command = '''$shell=New-Object -ComObject WScript.Shell
$places=@([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))
foreach($place in $places){$link=$shell.CreateShortcut((Join-Path $place 'ALI BABA.lnk'));$link.TargetPath=$env:ALIBABA_PYTHON;$link.Arguments='"'+$env:ALIBABA_SCRIPT+'"';$link.WorkingDirectory=$env:ALIBABA_FOLDER;$link.Save()}'''
        subprocess.run(['powershell', '-NoProfile', '-Command', command], env=env, check=True)
        return str(target)
    raise RuntimeError('Installer supports Windows and macOS. On Linux use python3 server.py.')


def main():
    root = tk.Tk()
    root.title('ALI BABA · Setup')
    root.geometry('540x260')
    tk.Label(root, text='ALI BABA 2.1.1', font=('Arial', 22, 'bold')).pack(pady=20)
    tk.Label(root, text='Install / Update · Установить / Обновить\nClose the previous version first. Your database stays in your home folder.\nFirst installation downloads the desktop runtime. Internet is required.').pack()
    status = tk.Label(root, text='')
    status.pack()

    def run():
        button.configure(state='disabled')
        status.configure(text='Installing… / Установка… См. окно Терминала.')
        root.update_idletasks()
        try:
            target = install()
            messagebox.showinfo('ALI BABA', 'Installed / Установлено:\n' + target + '\n\nOpen ALI BABA using its icon.')
            root.destroy()
        except Exception as exc:
            messagebox.showerror('ALI BABA', str(exc))
            button.configure(state='normal')
            status.configure(text='Installation failed / Установка не завершена')

    button = tk.Button(root, text='Install / Update', command=run)
    button.pack(pady=12)
    root.mainloop()


if __name__ == '__main__':
    main()
