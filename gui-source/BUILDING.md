# Local AI Companion 0.1.17 Windows build

Use the existing CPython 3.10.11 Windows build environment and pinned dependencies
in `requirements-build.txt`. Do not rebuild the Windows program on Linux:
PyInstaller produces binaries for its host platform.

The build script writes the actual Windows build identity and hashes for every
root Python module into `BUILD_INFO.json`. Keep the source and onedir packages
from the same build together.

Run these independent single-line PowerShell commands from the extracted source:

```powershell
& 'C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe' -m unittest tests.test_installation_results tests.test_job_history_ui -v
& .\build\build-release.ps1 -Python 'C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe'
```

The script uses `build/Local-AI-Companion-0.1.17.spec` and distinct
`build/work-<build_id>` / `build/dist-<build_id>` directories. It does not install
build dependencies or change model services.

Deliver the complete `Local-AI-Companion-0.1.17` onedir folder, including `_internal`;
do not copy only its EXE. Verify source/dist BUILD_INFO consistency and ZIP hashes.

Before release, launch the new Windows EXE and reopen the existing successful
installation job from `%LOCALAPPDATA%\Ornith-Solution-Scout\jobs` through the home
page's Work history button, select a job, then Open saved job. It should show
installation success and every saved step without reinstalling. Verify failure,
guard stop, cancel, pending steps, and six language labels with isolated fixtures;
do not change old job files or reinstall the existing candidate to test the page.
Switch languages on that result page: the same job and result must remain open.
Windows EXE startup, real fonts and DPI, and the user's existing job remain pending
until actually observed. Record build ID, EXE hash, screenshots, and outcomes.

See `UI_README.md` for the cloud AI / human review workflow.
