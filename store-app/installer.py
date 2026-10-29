"""Install source version and create a desktop shortcut. Requires Python 3.10+."""
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

ROOT=Path(__file__).parent
FILES=('server.py','desktop.py','static','README.md')


def install_files(target):
    target.mkdir(parents=True,exist_ok=True)
    for name in FILES:
        source=ROOT/name
        if source.is_dir():
            shutil.copytree(source,target/name,dirs_exist_ok=True)
        else:
            shutil.copy2(source,target/name)


def install():
    if sys.platform == 'darwin':
        app=Path.home()/'Applications'/'Nile Store.app'
        resources=app/'Contents'/'Resources'
        install_files(resources)
        executable=app/'Contents'/'MacOS'/'NileStore'
        executable.parent.mkdir(parents=True,exist_ok=True)
        executable.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(resources/'desktop.py'))+'\n')
        executable.chmod(0o755)
        with (app/'Contents'/'Info.plist').open('wb') as output:
            plistlib.dump({'CFBundleExecutable':'NileStore','CFBundleName':'Nile Store','CFBundleIdentifier':'local.nilestore.desktop','CFBundleVersion':'2.0.0','CFBundleShortVersionString':'2.0.0','CFBundlePackageType':'APPL'},output)
        subprocess.run(['open',str(app.parent)],check=False)
        return str(app)
    if sys.platform == 'win32':
        target=Path(os.environ['LOCALAPPDATA'])/'Nile Store'
        install_files(target)
        pythonw=Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.exists():
            raise RuntimeError('pythonw.exe not found. Install Python with the Windows desktop components.')
        # Values pass through environment variables, not PowerShell code interpolation.
        env=dict(os.environ,NILE_PYTHON=str(pythonw),NILE_SCRIPT=str(target/'desktop.py'),NILE_FOLDER=str(target))
        command='''$shell=New-Object -ComObject WScript.Shell
$places=@([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))
foreach($place in $places){$link=$shell.CreateShortcut((Join-Path $place 'Nile Store.lnk'));$link.TargetPath=$env:NILE_PYTHON;$link.Arguments='"'+$env:NILE_SCRIPT+'"';$link.WorkingDirectory=$env:NILE_FOLDER;$link.Save()}'''
        subprocess.run(['powershell','-NoProfile','-Command',command],env=env,check=True)
        return str(target)
    raise RuntimeError('Installer supports Windows and macOS. On Linux use python3 server.py.')


def main():
    root=tk.Tk();root.title('Nile Store · Setup');root.geometry('480x220')
    tk.Label(root,text='Nile Store 2.0',font=('Arial',22,'bold')).pack(pady=20)
    tk.Label(root,text='Install / Update · Установить / Обновить\nClose Nile Store first. Your database stays in your home folder.').pack()
    def run():
        try:
            target=install();messagebox.showinfo('Nile Store','Installed / Установлено:\n'+target+'\n\nOpen Nile Store using its icon.');root.destroy()
        except Exception as exc:messagebox.showerror('Nile Store',str(exc))
    tk.Button(root,text='Install / Update',command=run).pack(pady=20)
    root.mainloop()

if __name__=='__main__':main()
