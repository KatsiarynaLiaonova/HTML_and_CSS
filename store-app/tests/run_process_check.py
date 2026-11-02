"""Run native-window checks and preserve their failure details in CI annotations."""
import os
import subprocess
import sys

result = subprocess.run([sys.executable, *sys.argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
print(result.stdout, end='')
if result.returncode:
    message = result.stdout[-8000:].replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::error title=Native application check failed::' + message)
sys.exit(result.returncode)
