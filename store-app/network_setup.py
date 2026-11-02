"""Connection setup runs in a separate process, on the GUI main thread."""
from pathlib import Path
import ipaddress
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from access import Accounts
from connections import local_address, prepare_certificate, read_config, save_config, validate_client_url


def configure(folder, smoke_test=False):
    folder = Path(folder)
    current = read_config(folder)
    if smoke_test: print('Setup check: creating Tk window', flush=True)
    root = tk.Tk()
    if smoke_test: print('Setup check: Tk window created', flush=True)
    root.title('ALI BABA · Подключение компьютера')
    root.geometry('670x680')
    root.columnconfigure(1, weight=1)
    tk.Label(root, text='Подключение ALI BABA', font=('Arial', 18, 'bold')).grid(row=0, column=0, columnspan=2, pady=18)
    options = {'Одна касса — локальная программа': 'local', 'Главная касса — общая база магазина': 'main', 'Подключиться к главной кассе': 'client'}
    mode = tk.StringVar(value=next((label for label, value in options.items() if value == current.get('mode')), next(iter(options))))
    ttk.Combobox(root, textvariable=mode, values=list(options), state='readonly', width=55).grid(row=1, column=0, columnspan=2, padx=18, pady=10)
    fields, labels = {}, {}
    def field(row, key, title, value='', password=False):
        label = tk.Label(root, text=title, anchor='w', justify='left')
        label.grid(row=row, column=0, sticky='w', padx=18, pady=8)
        labels[key] = label
        entry = tk.Entry(root, show='*' if password else '')
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky='ew', padx=18, pady=8)
        fields[key] = entry
    if smoke_test: print('Setup check: detecting local address', flush=True)
    try:
        suggested_address = local_address()
    except OSError:
        suggested_address = ''
    if smoke_test: print('Setup check: building controls', flush=True)
    field(2, 'address', 'Главная касса: IPv4 в сети магазина', current.get('address', suggested_address))
    field(3, 'remote_url', 'Главная касса: HTTPS-адрес Tailscale\n(необязательно для работы в магазине)', current.get('remote_url', ''))
    field(4, 'url', 'Подключение: HTTPS-адрес главной кассы', current.get('url', ''))
    field(5, 'certificate', 'Подключение: публичный сертификат\nдля локального адреса', current.get('certificate', ''))
    def pick():
        chosen = filedialog.askopenfilename(parent=root, title='connection-certificate.pem', filetypes=[('Certificate', '*.pem *.crt'), ('All files', '*')])
        if chosen:
            fields['certificate'].delete(0, 'end'); fields['certificate'].insert(0, chosen)
    certificate_button = tk.Button(root, text='Выбрать сертификат', command=pick)
    certificate_button.grid(row=6, column=1, sticky='w', padx=18)
    field(7, 'owner', 'Главная касса: логин владельца')
    field(8, 'password', 'Пароль владельца (от 10 символов)', password=True)
    field(9, 'confirm', 'Повторите пароль (первый запуск)', password=True)
    owner_exists = (folder / 'accounts.sqlite').exists() and Accounts(folder).has_owner()
    def update_fields(*args):
        selected = options[mode.get()]
        visible = {'address', 'remote_url'} if selected == 'main' else {'url', 'certificate'} if selected == 'client' else set()
        if selected == 'main' or owner_exists:
            visible.update(('owner', 'password'))
        if selected == 'main' and not owner_exists:
            visible.add('confirm')
        for key in fields:
            for widget in (fields[key], labels[key]):
                widget.grid() if key in visible else widget.grid_remove()
        certificate_button.grid() if selected == 'client' else certificate_button.grid_remove()
    mode.trace_add('write', update_fields)
    update_fields()
    tk.Label(root, text='Главная касса должна оставаться включенной.\nДля второй кассы используйте локальный адрес вида https://192.168.1.10:8443\nи публичный сертификат с главной кассы. Ключ connection-key.pem не передавайте.\nДля Mac вне магазина используйте HTTPS-адрес Tailscale.\nСохранение настроек требует перезапуска ALI BABA.', justify='left', wraplength=620).grid(row=10, column=0, columnspan=2, padx=18, pady=16)
    def save():
        try:
            selected = options[mode.get()]
            config = {'mode': selected}
            existing_accounts = Accounts(folder)
            if existing_accounts.has_owner():
                token, _, account = existing_accounts.login(fields['owner'].get(), fields['password'].get())
                existing_accounts.logout(token)
                if account['role'] != 'owner':
                    raise ValueError('Изменить подключение главной кассы может только владелец')
            if selected == 'main':
                address = fields['address'].get().strip()
                if ipaddress.ip_address(address).is_loopback:
                    raise ValueError('Укажите адрес главной кассы в сети магазина, а не 127.0.0.1')
                remote = fields['remote_url'].get().strip().rstrip('/')
                if remote:
                    remote = validate_client_url(remote).geturl().rstrip('/')
                certificate, _ = prepare_certificate(folder / 'network', address)
                accounts = Accounts(folder)
                if not accounts.has_owner():
                    if fields['password'].get() != fields['confirm'].get():
                        raise ValueError('Пароли не совпадают')
                    accounts.create(fields['owner'].get(), fields['password'].get(), 'owner', initial=True)
                config.update(address=address, remote_url=remote)
                extra = '\n\nСертификат для второй кассы:\n' + str(certificate)
            elif selected == 'client':
                url = fields['url'].get().strip().rstrip('/')
                url = validate_client_url(url).geturl().rstrip('/')
                certificate = fields['certificate'].get().strip()
                if validate_client_url(url).hostname.endswith('.ts.net'):
                    certificate = ''
                if certificate and not Path(certificate).is_file():
                    raise ValueError('Файл публичного сертификата не найден')
                from connections import create_gateway
                probe = create_gateway(url, certificate or None)
                probe.server_close()
                if certificate:
                    trusted = folder / 'trusted-main-certificate.pem'
                    trusted.write_bytes(Path(certificate).read_bytes())
                    certificate = str(trusted)
                config.update(url=url, certificate=certificate)
                extra = ''
            else:
                extra = ''
            save_config(folder, config)
            fields['password'].delete(0, 'end'); fields['confirm'].delete(0, 'end')
            messagebox.showinfo('ALI BABA', 'Настройки сохранены. Полностью закройте и заново откройте ALI BABA.' + extra, parent=root)
            root.destroy()
        except Exception as exc:
            messagebox.showerror('ALI BABA', str(exc), parent=root)
    tk.Button(root, text='Сохранить подключение', command=save).grid(row=11, column=0, columnspan=2, pady=12)
    def help_window():
        window = tk.Toplevel(root)
        window.title('ALI BABA · Инструкция подключения')
        window.geometry('850x650')
        text = tk.Text(window, wrap='word', padx=18, pady=18)
        scroll = tk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', (Path(__file__).parent / 'NETWORK-SETUP.md').read_text(encoding='utf-8'))
        text.configure(state='disabled')
    tk.Button(root, text='Пошаговая инструкция (доступна без интернета)', command=help_window).grid(row=12, column=0, columnspan=2, pady=6)
    failures = []
    if smoke_test:
        def check_modes():
            print('Setup check: checking connection modes', flush=True)
            try:
                for selected in ('main', 'client', 'local'):
                    mode.set(next(label for label, value in options.items() if value == selected))
                    root.update_idletasks()
                    assert bool(fields['address'].winfo_ismapped()) == (selected == 'main')
                    assert bool(fields['url'].winfo_ismapped()) == (selected == 'client')
                    assert bool(fields['password'].winfo_ismapped()) == (selected == 'main')
                assert (Path(__file__).parent / 'NETWORK-SETUP.md').is_file()
            except Exception as exc:
                failures.append(exc)
            finally:
                root.destroy()
        root.after(200, check_modes)
    if smoke_test: print('Setup check: entering GUI event loop', flush=True)
    root.mainloop()
    if failures:
        raise failures[0]
