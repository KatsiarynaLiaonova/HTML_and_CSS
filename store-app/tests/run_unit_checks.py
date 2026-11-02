"""Preserve unittest results and make failed CI output visible in annotations."""
import subprocess
import sys

result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
print(result.stdout, end='')
if result.returncode:
    message = result.stdout[-6000:].replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::error title=Unit test failure::' + message)
sys.exit(result.returncode)
