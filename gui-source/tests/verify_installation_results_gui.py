"""Render the actual Tk result page using isolated offline execution records.

Run with a valid DISPLAY (Linux) or on Windows. Optional screenshot directory:
python tests/verify_installation_results_gui.py --screenshots <directory>
"""
import argparse
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import localization
import normal_ui
from job_context import JobLifecycleState
from test_installation_results import InstallationResultTests


def widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from widgets(child)


def verify(screenshots=None):
    root = tk.Tk()
    root.title('Local AI Companion 0.1.16 — isolated result-page verification')
    root.geometry('1100x850+20+20')
    app = normal_ui.NormalProductApp.__new__(normal_ui.NormalProductApp)
    app.root, app.tk, app.ttk = root, tk, ttk
    app._configure_styles()
    app.container = ttk.Frame(root, style='Card.TFrame', padding=24)
    app.container.pack(fill='both', expand=True)
    app.clear = lambda: [child.destroy() for child in app.container.winfo_children()]
    app.nav = lambda back: None
    app.home = app.show_approved_installation_plan = lambda: None
    receipts = []
    try:
        for outcome in ('success', 'failure', 'guard', 'cancel'):
            case = InstallationResultTests()
            case.setUp()
            try:
                state, _, _ = case.execute(outcome)
                # Exactly the record format the page reads after restarting a job.
                result = {'execution_state': state}
                app._active_job_context = SimpleNamespace(
                    state=JobLifecycleState.VERIFIED if outcome == 'success'
                    else JobLifecycleState.STOPPED)
                for language in localization.SUPPORTED_LANGUAGES:
                    app.localizer = localization.Localizer(language,
                        preferences_path=case.root / 'prefs.json')
                    app.show_installation_lifecycle(result)
                    root.update()
                    text = next(w for w in widgets(root) if isinstance(w, tk.Text))
                    content = text.get('1.0', 'end-1c')
                    assert content == normal_ui.installation_lifecycle_text(result, app.tr)
                    assert text.cget('state') == 'disabled'
                    assert app.tr('plan.lifecycle.unknown') not in content
                    assert app.current_view == ('installation_plan_lifecycle', state['status'])
                    assert text.winfo_width() > 500 and text.winfo_height() > 200
                    receipts.append({'outcome': outcome, 'language': language,
                                     'status': state['status'], 'text_readback': 'PASS'})
                    if screenshots and language == 'zh-CN':
                        from PIL import ImageGrab
                        folder = Path(screenshots)
                        folder.mkdir(parents=True, exist_ok=True)
                        ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                            root.winfo_rootx()+root.winfo_width(),
                            root.winfo_rooty()+root.winfo_height())).save(folder/f'{outcome}.png')
                        (folder/f'{outcome}-state.json').write_text(
                            json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            finally:
                case.doCleanups()
        print(json.dumps({'actual_tk_result_pages': len(receipts), 'checks': receipts},
                         ensure_ascii=False, indent=2))
    finally:
        root.destroy()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    verify(parser.parse_args().screenshots)
