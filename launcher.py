#!/usr/bin/env python3
"""EVE Voice launcher (run.bat / desktop shortcut start THIS, not the app directly).

1. If the last update never confirmed a good start -> roll back to the previous version.
2. First run: create commands.json from commands.default.json.
3. If 'Update automatically on start' is on: check for a newer SIGNED release (5 s
   timeout; offline = carry on), download + verify it, then apply it BEFORE the app's
   code is loaded, with a small 'Updating...' window. Your files are never touched.
4. Merge any NEW default commands/settings into commands.json (never changes yours).
5. Start the app. If it exits without confirming a good start -> roll back + restart.
Nothing here may stop the app from starting: every error is logged to update.log.
"""
import os
import queue
import subprocess
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
RESTART_CODE = 75      # the app exits with this to ask for 'restart and apply the update'


def _with_progress(title, work):
    """Run work(progress) in a thread while a tiny tk window shows progress. Falls back to
    running it directly if tkinter isn't available."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return work(lambda m: None)
    try:
        root = tk.Tk()
    except Exception:
        return work(lambda m: None)
    root.title("EVE Voice")
    root.configure(bg="#14171c")
    root.resizable(False, False)
    tk.Label(root, text=title, bg="#14171c", fg="#e8b04a", font=("Segoe UI", 12, "bold")).pack(padx=24, pady=(18, 6))
    msg = tk.Label(root, text="Starting...", bg="#14171c", fg="#cfd6df", width=60, anchor="w")
    msg.pack(padx=24)
    bar = ttk.Progressbar(root, mode="indeterminate", length=380)
    bar.pack(padx=24, pady=(8, 18)); bar.start(12)
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    q, out = queue.Queue(), {}

    def run():
        try:
            out["r"] = work(lambda m: q.put(m))
        except Exception as e:     # pragma: no cover - work() is never supposed to raise
            out["e"] = e
        q.put(None)

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m is None:
                    root.destroy()
                    return
                msg.configure(text=m[:90])
        except queue.Empty:
            pass
        root.after(80, poll)
    threading.Thread(target=run, daemon=True).start()
    root.after(80, poll)
    root.mainloop()
    return out.get("r")


def prepare(app_dir=APP_DIR, check_online=True, progress_ui=_with_progress, **cfg_kw):
    """Steps 1-4. Returns (version now installed, pending flag we created or None). Never raises."""
    pending = None
    try:
        import autoupdater as au
        import eve_update
        cfg = eve_update.make_config(app_dir, **cfg_kw)
        if au.handle_pending(cfg):
            cfg = eve_update.make_config(app_dir, **cfg_kw)
        eve_update.ensure_user_config(app_dir)
        if check_online and eve_update.auto_update_enabled(app_dir) and not au.staged(cfg):
            au.check_and_stage_safe(cfg)
        st = au.staged(cfg)
        if st:
            new = progress_ui(f"Updating EVE Voice to {st['version']}...", lambda p: au.apply_staged(cfg, p))
            if new:
                pending = au._read_json(os.path.join(app_dir, au.PENDING))
        try:
            eve_update.merge_defaults(app_dir, lambda m: au._log(cfg, m))
        except Exception as e:
            au._log(cfg, f"merging defaults failed: {e}")
        return eve_update.read_version(app_dir), pending
    except Exception as e:
        try:
            with open(os.path.join(app_dir, "update.log"), "a", encoding="utf-8") as f:
                f.write(f"launcher: update step failed: {e!r}\n")
        except OSError:
            pass
        return None, pending


def _start_gui(app_dir, args):
    env = dict(os.environ, EVE_VOICE_LAUNCHER="1")
    return subprocess.call([sys.executable, os.path.join(app_dir, "eve_voice_gui.py")] + list(args),
                           cwd=app_dir, env=env)


def run(app_dir=APP_DIR, args=(), start_gui=_start_gui, check_online=True, progress_ui=_with_progress,
        **cfg_kw) -> int:
    rc = 0
    for _attempt in range(3):
        version, pending = prepare(app_dir, check_online, progress_ui, **cfg_kw)
        rc = start_gui(app_dir, args)
        try:
            import autoupdater as au
            import eve_update
            now = au._read_json(os.path.join(app_dir, au.PENDING))
            if pending and now == pending:          # the new version never said it started fine
                au.rollback(eve_update.make_config(app_dir, **cfg_kw), f"the app exited ({rc}) before starting")
                check_online = False
                continue                            # start the restored version
        except Exception:
            pass
        if rc == RESTART_CODE:                      # 'Restart to update' in the app
            continue
        return rc
    return rc


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    sys.exit(run(args=sys.argv[1:]))
