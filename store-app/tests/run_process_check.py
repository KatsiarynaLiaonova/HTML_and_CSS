"""Run native-window checks and preserve their failure details in CI annotations."""
import os
import subprocess
import sys

command = sys.argv[2:] if sys.argv[1:2] == ['--executable'] else [sys.executable, *sys.argv[1:]]
try:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'), timeout=40 if '--setup-smoke-test' in sys.argv else 150)
except subprocess.TimeoutExpired as exc:
    output=exc.stdout or b''
    if isinstance(output,bytes): output=output.decode('utf-8','replace')
    message=('Native check timed out. '+output)[-8000:].replace('%','%25').replace('\r','%0D').replace('\n','%0A')
    print('::error title=Native setup timeout::'+message)
    sys.exit(124)
print(result.stdout, end='')
if result.returncode:
    message = result.stdout[-8000:].replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::error title=Native application check failed::' + message)
sys.exit(result.returncode)
