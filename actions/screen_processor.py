from __future__ import annotations

import io
import json
import os
import platform
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

from memory.config_manager import get_base_dir

_BASE        = get_base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()

_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82
_WINDOW_FALLBACK_WARNED: set[int] = set()


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"

def _normalise_capture_source(source_id: str | None = None) -> str:
    """Return the stable identifier used by Live Vision screen sources."""
    value = str(source_id or "").strip()
    if not value or value == "screen":
        return "monitor:1"
    if value.startswith("monitor:") or value.startswith("window:"):
        return value
    return "monitor:1"


def _window_rect(hwnd: int) -> dict[str, int]:
    """Return a visible Windows top-level window's physical capture bounds."""
    if platform.system() != "Windows":
        raise RuntimeError("Application/window capture is only available on Windows.")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindow(hwnd):
        raise RuntimeError("The selected window is no longer open.")
    if user32.IsIconic(hwnd):
        raise RuntimeError("Restore the selected window before sharing it.")

    rect = wintypes.RECT()
    # The DWM frame excludes invisible resize borders and matches what the user
    # sees. Fall back to GetWindowRect on old Windows builds or classic apps.
    ok = False
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        ok = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
        ) == 0
    except Exception:
        ok = False
    if not ok and not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise RuntimeError("Windows could not read the selected window bounds.")

    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < 2 or height < 2:
        raise RuntimeError("The selected window has no visible capture area.")
    return {
        "left": int(rect.left), "top": int(rect.top),
        "width": width, "height": height,
    }


def capture_source_geometry(source_id: str | None = None) -> dict[str, int]:
    """Resolve a monitor/window identifier to current desktop pixel bounds."""
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    source_id = _normalise_capture_source(source_id)
    if source_id.startswith("window:"):
        try:
            return _window_rect(int(source_id.split(":", 1)[1]))
        except ValueError as exc:
            raise RuntimeError("Invalid window source.") from exc

    try:
        requested = max(1, int(source_id.split(":", 1)[1]))
    except (ValueError, IndexError):
        requested = 1
    with mss.MSS() as sct:
        monitors = sct.monitors
        if len(monitors) <= 1:
            requested = 0
        elif requested >= len(monitors):
            raise RuntimeError("The selected display is no longer available.")
        mon = monitors[requested]
        return {k: int(mon[k]) for k in ("left", "top", "width", "height")}


def list_capture_sources() -> list[dict[str, str]]:
    """Enumerate shareable displays and visible application windows.

    Identifiers are deliberately opaque to the UI. A window is re-resolved on
    every capture, so moving or resizing it while sharing works naturally.
    """
    sources: list[dict[str, str]] = []
    if _MSS:
        try:
            with mss.MSS() as sct:
                real = sct.monitors[1:] or sct.monitors[:1]
                for index, mon in enumerate(real, start=1):
                    primary = int(mon["left"]) == 0 and int(mon["top"]) == 0
                    suffix = " (primary)" if primary else ""
                    sources.append({
                        "id": f"monitor:{index}",
                        "kind": "monitor",
                        "label": f"Display {index}{suffix}",
                        "detail": f"{int(mon['width'])} x {int(mon['height'])}",
                    })
        except Exception:
            pass

    if platform.system() != "Windows":
        return sources

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        windows: list[dict[str, str]] = []

        def visit(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, len(title))
                title_text = " ".join(title.value.split())
                if not title_text or title_text.upper() == "LUMINA":
                    return True
                class_name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_name, len(class_name))
                if class_name.value in {"Progman", "WorkerW", "Shell_TrayWnd"}:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) == os.getpid():
                    return True
                rect = _window_rect(int(hwnd))
                if rect["width"] < 160 or rect["height"] < 100:
                    return True
                try:
                    import psutil
                    app_name = Path(psutil.Process(int(pid.value)).exe()).stem
                except Exception:
                    app_name = "Application"
                windows.append({
                    "id": f"window:{int(hwnd)}",
                    "kind": "window",
                    "label": title_text[:90],
                    "detail": app_name[:40],
                })
            except Exception:
                pass
            return True

        user32.EnumWindows(callback_type(visit), 0)
        windows.sort(key=lambda item: (item["detail"].lower(), item["label"].lower()))
        sources.extend(windows)
    except Exception:
        pass
    return sources


def describe_capture_source(source_id: str | None = None) -> str:
    """Human-readable current label for a stable source identifier."""
    source_id = _normalise_capture_source(source_id)
    for source in list_capture_sources():
        if source["id"] == source_id:
            if source["kind"] == "window" and source.get("detail"):
                return f"{source['detail']} - {source['label']}"
            return source["label"]
    if source_id.startswith("monitor:"):
        return f"Display {source_id.split(':', 1)[1]}"
    return "Selected window"


def focus_capture_source(source_id: str | None = None) -> bool:
    """Bring a selected application window forward after the picker closes."""
    source_id = _normalise_capture_source(source_id)
    if platform.system() != "Windows" or not source_id.startswith("window:"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(source_id.split(":", 1)[1])
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            return False
        user32.ShowWindowAsync(wintypes.HWND(hwnd), 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))
    except Exception:
        return False


def _capture_window_native(hwnd: int) -> tuple[bytes, str]:
    """Capture one HWND independently of windows layered above it.

    PrintWindow is the closest dependency-free Windows equivalent to the
    per-window stream behind browser share pickers. Chromium and most modern
    desktop apps support PW_RENDERFULLCONTENT; callers retain an mss fallback
    for applications that explicitly refuse it.
    """
    if platform.system() != "Windows" or not _PIL:
        raise RuntimeError("Native window capture is unavailable.")
    import ctypes
    import win32gui
    import win32ui

    area = _window_rect(hwnd)
    width, height = area["width"], area["height"]
    window_dc = 0
    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        if not window_dc:
            raise RuntimeError("The selected application refused a capture context.")
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            int(hwnd), int(memory_dc.GetSafeHdc()), 0x00000002
        )
        if not rendered:
            raise RuntimeError("The selected application refused native window capture.")
        info = bitmap.GetInfo()
        pixels = bitmap.GetBitmapBits(True)
        image = PIL.Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), pixels,
            "raw", "BGRX", 0, 1,
        )
        # Hardware-accelerated or protected windows can return a successful
        # call filled entirely with black. Treat that as failure, not vision.
        sample = np.asarray(image.resize((32, 18), PIL.Image.Resampling.BILINEAR))
        if float(sample.mean()) < 2.0 and float(sample.std()) < 1.0:
            raise RuntimeError("Native window capture returned an empty frame.")
        output = io.BytesIO()
        image.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.Resampling.LANCZOS)
        image.save(output, format="JPEG", quality=_JPEG_Q, optimize=False)
        return output.getvalue(), "image/jpeg"
    finally:
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if memory_dc is not None:
            try:
                memory_dc.DeleteDC()
            except Exception:
                pass
        if source_dc is not None:
            try:
                source_dc.DeleteDC()
            except Exception:
                pass
        if window_dc:
            try:
                win32gui.ReleaseDC(hwnd, window_dc)
            except Exception:
                pass


def _capture_screen(source_id: str | None = None) -> tuple[bytes, str]:

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    source_id = _normalise_capture_source(source_id)
    if source_id.startswith("window:"):
        hwnd = int(source_id.split(":", 1)[1])
        try:
            return _capture_window_native(hwnd)
        except Exception as exc:
            # Some protected/GPU surfaces reject PrintWindow. A visible-region
            # fallback is still more useful than ending the Vision session.
            if hwnd not in _WINDOW_FALLBACK_WARNED:
                _WINDOW_FALLBACK_WARNED.add(hwnd)
                print(f"[Vision] Native window capture fallback: {exc}")
    with mss.MSS() as sct:
        target = capture_source_geometry(source_id)
        # Clamp windows to the virtual desktop. A window can be partly outside
        # a monitor while the user drags it; mss rejects negative overflow.
        virtual = sct.monitors[0]
        left = max(int(target["left"]), int(virtual["left"]))
        top = max(int(target["top"]), int(virtual["top"]))
        right = min(
            int(target["left"] + target["width"]),
            int(virtual["left"] + virtual["width"]),
        )
        bottom = min(
            int(target["top"] + target["height"]),
            int(virtual["top"] + virtual["height"]),
        )
        if right - left < 2 or bottom - top < 2:
            raise RuntimeError("The selected source is outside the visible desktop.")
        target = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def frame_fingerprint(img_bytes: bytes) -> bytes:
    """Small luminance sample used to suppress visually unchanged frames."""
    if not _PIL:
        return img_bytes[:4096]
    img = PIL.Image.open(io.BytesIO(img_bytes)).convert("L")
    img = img.resize((64, 36), PIL.Image.Resampling.BILINEAR)
    return img.tobytes()


def frame_change_score(previous: bytes | None, current: bytes) -> float:
    """Return normalized mean luminance change (0.0 identical, 1.0 opposite)."""
    if previous is None or not previous or len(previous) != len(current):
        return 1.0
    a = np.frombuffer(previous, dtype=np.uint8).astype(np.int16)
    b = np.frombuffer(current, dtype=np.uint8).astype(np.int16)
    return float(np.mean(np.abs(a - b)) / 255.0)


def capture_source_thumbnail(source_id: str, size: tuple[int, int] = (244, 137)) -> bytes:
    """Return a compact JPEG preview for the visual source chooser."""
    image_bytes, _mime = _capture_screen(source_id)
    if not _PIL:
        return image_bytes
    image = PIL.Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image.thumbnail(size, PIL.Image.Resampling.LANCZOS)
    canvas = PIL.Image.new("RGB", size, (18, 18, 22))
    left = (size[0] - image.width) // 2
    top = (size[1] - image.height) // 2
    canvas.paste(image, (left, top))
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=70, optimize=False)
    return output.getvalue()


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW    
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION  
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
