# Agent-only console/stdio package. Never rewrites the GUI build identity.
import json
import os
from pathlib import Path

workspace = Path(SPECPATH).resolve().parent
package_name = 'Local-AI-Companion-Agent-0.1.17-agent.1'
agent_info_value = os.environ.get('LOCAL_AI_COMPANION_AGENT_BUILD_INFO')
if not agent_info_value:
    raise ValueError('Use agent/build-agent.ps1 to provide isolated AGENT_BUILD_INFO.json')
agent_info = Path(agent_info_value).resolve()
data_names = ('radar_sources.json', 'curated_sources.json', 'first_party_youtube_index.json')
required_files = [workspace / 'scout_agent.py', workspace / 'BUILD_INFO.json', agent_info]
required_files.extend(workspace / 'data' / name for name in data_names)
for path in required_files:
    if not path.is_file():
        raise FileNotFoundError(str(path))
if not (workspace / 'locales').is_dir():
    raise FileNotFoundError(str(workspace / 'locales'))
metadata = json.loads(agent_info.read_text(encoding='utf-8-sig'))
if (agent_info.name != 'AGENT_BUILD_INFO.json'
        or metadata.get('agent_version') != '0.1.17-agent.1'
        or metadata.get('package_name') != package_name
        or not metadata.get('build_id')
        or not metadata.get('source_fingerprint')):
    raise ValueError('Agent build metadata identity is invalid')

# ProductUIController loads these modules lazily. Include the complete local
# search/research/plan/job closure without exposing any installation tool.
core_modules = [
    'app', 'build_identity', 'handoff_debug', 'product_cli', 'product_config',
    'solution_scout', 'job_context', 'install_research', 'installation_plan',
    'audit', 'conda_bootstrap', 'executor', 'storage_safety', 'plan', 'preflight',
    'resume', 'editable_project_install', 'external_asset_execution',
    'external_asset_followup', 'first_party_identity', 'runtime_paths',
    'query_planner', 'local_only_test', 'curated_catalog',
]
for module in core_modules:
    if not (workspace / (module + '.py')).is_file():
        raise FileNotFoundError('Missing Product Core module: ' + module)

a = Analysis(
    [str(workspace / 'scout_agent.py')],
    pathex=[str(workspace)],
    binaries=[],
    datas=[(str(workspace / 'data' / name), 'data') for name in data_names] + [
        (str(workspace / 'locales'), 'locales'),
        (str(workspace / 'BUILD_INFO.json'), '.'),
        (str(agent_info), '.'),
    ],
    hiddenimports=core_modules + [
        'packaging.requirements', 'packaging.markers', 'packaging.specifiers',
        'packaging.utils', 'packaging.version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=package_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=package_name,
)
