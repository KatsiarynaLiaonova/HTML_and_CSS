# The historical spec filename is retained for existing build commands.
# Build on the target OS with: python -m PyInstaller packaging/NileStore.spec --noconfirm
from pathlib import Path
import sys
from PyInstaller.compat import is_darwin
root = Path(SPECPATH).parent
# pywebview's bundled hook includes its JS bridge and Windows WebView2 DLLs.
# Keep the official hook directory explicit for reproducible PyInstaller builds.
from webview.__pyinstaller import get_hook_dirs
binaries = [(str(p), '.') for pattern in ('libtcl*.so', 'libtk*.so') for p in (Path(sys.base_prefix) / 'lib').glob(pattern)] if sys.platform == 'linux' else []
analysis = Analysis([str(root / 'desktop.py')], pathex=[str(root)], datas=[(str(root / 'static'), 'static'), (str(root / 'NETWORK-SETUP.md'), '.')], binaries=binaries, hiddenimports=[], hookspath=get_hook_dirs(), hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, name='AliBaba', debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='AliBaba')
if is_darwin:
    app = BUNDLE(collection, name='ALI BABA.app', bundle_identifier='local.nilestore.desktop', info_plist={'CFBundleName': 'ALI BABA', 'CFBundleDisplayName': 'ALI BABA', 'CFBundleShortVersionString': '3.0.1', 'CFBundleVersion': '3.0.1', 'NSHighResolutionCapable': True, 'LSApplicationCategoryType': 'public.app-category.business'})
