"""Single-instance ownership for LUMINA.

The HUD owns the microphone and the Gemini Live session, so a second copy would
fight the first one for both. Owning that rule here — rather than in whatever
started the app — lets every launcher stay dumb: the desktop shortcut, the
`Abrir LUMINA.bat`, and the microphone button in the Lumina OpenClaw web UI can
all start LUMINA unconditionally. Whichever process loses the race hands focus
to the window that is already up and exits.

The running instance is found by looking for it, not by reading a lock file it
was supposed to have written. A lock is only ever as good as the last writer:
a copy started before this module existed, or one killed hard, leaves the file
lying about what is running — and the answer decides whether the user gets a
second HUD fighting for the microphone.

Claim before the heavy imports (`sounddevice`, `numpy`, `google.genai`), or a
duplicate launch costs seconds of startup work just to discover it is a
duplicate.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import psutil

_APP_DIR = Path(__file__).resolve().parent.parent
_ENTRY = "main.py"


def _is_lumina(proc: psutil.Process) -> bool:
    """Whether a live process is another LUMINA started from this install."""
    try:
        cmdline = [part.lower() for part in proc.cmdline()]
    except (psutil.Error, OSError):
        return False
    if not any("python" in part for part in cmdline):
        return False
    if not any(part.endswith(_ENTRY) for part in cmdline):
        return False
    try:
        return Path(proc.cwd()).resolve() == _APP_DIR
    except (psutil.Error, OSError):
        # Some processes deny cwd inspection; the command line already matched.
        return True


def _own_tree() -> set[int]:
    """This process's ancestors and descendants.

    One LUMINA is more than one process: the app spawns a helper that reruns the
    same command line, so a relative running `main.py` is this instance rather
    than a competing one. Without this the helper would mistake its own parent
    for a duplicate and quit on the spot.
    """
    tree = {os.getpid()}
    try:
        me = psutil.Process()
        for relative in (*me.parents(), *me.children(recursive=True)):
            tree.add(relative.pid)
    except (psutil.Error, OSError):
        pass
    return tree


def running_instance() -> int | None:
    """The pid of a LUMINA already running, or None when this process is first."""
    own = _own_tree()
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        if pid in own:
            continue
        if _is_lumina(proc):
            return pid
    return None


def _focus(pid: int) -> bool:
    """Raises the running HUD's window so a second launch still shows the app."""
    if platform.system() != "Windows":
        return False
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return False

    handles: list[int] = []

    def _collect(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, owner = win32process.GetWindowThreadProcessId(hwnd)
        if owner == pid:
            handles.append(hwnd)

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception:
        return False

    for hwnd in handles:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            # Windows refuses SetForegroundWindow to a process that never held
            # focus — which is exactly the case when the Gateway launched us.
            # A topmost flash brings the window up without stealing input.
            try:
                for flag in (win32con.HWND_TOPMOST, win32con.HWND_NOTOPMOST):
                    win32gui.SetWindowPos(
                        hwnd,
                        flag,
                        0,
                        0,
                        0,
                        0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                    )
                return True
            except Exception:
                continue
    return False


def claim_or_focus() -> bool:
    """True when this process owns the instance, False when one already runs.

    A caller that gets False has nothing left to do: the running HUD has been
    raised, so the user still sees LUMINA come up.
    """
    running = running_instance()
    if running is None:
        return True
    _focus(running)
    return False
