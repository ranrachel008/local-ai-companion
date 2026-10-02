# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

workspace = Path(SPECPATH).resolve().parent
required_files = [
    workspace / 'app.py',
    workspace / 'BUILD_INFO.json',
    workspace / 'data' / 'radar_sources.json',
    workspace / 'data' / 'curated_sources.json',
    workspace / 'data' / 'first_party_youtube_index.json',
]
required_directories = [workspace / 'data', workspace / 'locales']
missing = [str(path) for path in required_files if not path.is_file()]
missing.extend(str(path) for path in required_directories if not path.is_dir())
if missing:
    raise FileNotFoundError('Missing required build inputs: ' + ', '.join(missing))

a = Analysis(
    [str(workspace / 'app.py')],
    pathex=[str(workspace)],
    binaries=[],
    datas=[
        (str(workspace / 'data' / 'radar_sources.json'), 'data'),
        (str(workspace / 'data' / 'curated_sources.json'), 'data'),
        (str(workspace / 'data' / 'first_party_youtube_index.json'), 'data'),
        (str(workspace / 'locales'), 'locales'),
        (str(workspace / 'BUILD_INFO.json'), '.'),
    ],
    hiddenimports=[
        'external_asset_execution',
        'packaging.requirements', 'packaging.markers', 'packaging.specifiers',
        'packaging.utils', 'packaging.version',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='Local-AI-Companion-0.1.16',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=True, upx_exclude=[], name='Local-AI-Companion-0.1.16',
)
