# Build on the target OS with: python -m PyInstaller packaging/NileStore.spec --noconfirm
from pathlib import Path
import sys
from PyInstaller.compat import is_darwin
root=Path(SPECPATH).parent
# Some standalone Linux Python distributions keep Tcl libraries outside ldconfig.
binaries=[(str(p),'.') for pattern in ('libtcl*.so','libtk*.so') for p in (Path(sys.base_prefix)/'lib').glob(pattern)] if sys.platform=='linux' else []
analysis=Analysis([str(root/'desktop.py')],pathex=[str(root)],datas=[(str(root/'static'),'static')],binaries=binaries,hiddenimports=[],hookspath=[],hooksconfig={},runtime_hooks=[],excludes=[],noarchive=False)
archive=PYZ(analysis.pure)
exe=EXE(archive,analysis.scripts,[],exclude_binaries=True,name='NileStore',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False)
collection=COLLECT(exe,analysis.binaries,analysis.datas,strip=False,upx=False,name='NileStore')
if is_darwin:
    app=BUNDLE(collection,name='Nile Store.app',bundle_identifier='local.nilestore.desktop',info_plist={'CFBundleShortVersionString':'2.0.0','CFBundleVersion':'2.0.0','NSHighResolutionCapable':True})
