"""Isolated main register and two verified desktop gateways for browser tests."""
from pathlib import Path
import json
import sys
import tempfile
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from access import Accounts
from connections import create_gateway, start_lan_server

with tempfile.TemporaryDirectory() as folder:
    server.DATA = Path(folder)
    accounts = Accounts(folder)
    accounts.create('owner', 'Owner browser password!', 'owner', initial=True)
    accounts.create('cashier1', 'Cashier one password!', 'cashier', 1)
    accounts.create('cashier2', 'Cashier two password!', 'cashier', 2)
    services = []
    def start(service):
        services.append(service)
        threading.Thread(target=service.serve_forever, daemon=True).start()
        return service
    try:
        main = start(server.create_server(0, accounts=accounts))
        tls = start(start_lan_server(server.Handler, accounts, Path(folder) / 'network', '127.0.0.1', 0))
        address = f'https://127.0.0.1:{tls.server_port}'
        certificate = Path(folder) / 'network' / 'connection-certificate.pem'
        first = start(create_gateway(address, certificate))
        second = start(create_gateway(address, certificate))
        print(json.dumps({'owner': f'http://127.0.0.1:{main.server_port}', 'first': f'http://127.0.0.1:{first.server_port}', 'second': f'http://127.0.0.1:{second.server_port}'}), flush=True)
        sys.stdin.read()
    finally:
        for service in reversed(services):
            service.shutdown(); service.server_close()
