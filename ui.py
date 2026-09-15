from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QTabWidget, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from memory.config_manager import get_base_dir

BASE_DIR   = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"
APP_NAME   = "LUMINA"
APP_ICON_PNG = CONFIG_DIR / "lumina.png"
APP_ICON_ICO = CONFIG_DIR / "lumina.ico"

# Auto-start identifiers. The _LEGACY_ names are what the app registered back when
# it was called JARVIS. They are still read and removed on every toggle, so an
# upgrade never leaves a second, orphaned entry quietly launching the app.
_AUTOSTART_REG            = "LUMINA_AI"
_AUTOSTART_REG_LEGACY     = "JARVIS_AI"
_AUTOSTART_LABEL          = "com.lumina.assistant"
_AUTOSTART_PLIST          = f"{_AUTOSTART_LABEL}.plist"
_AUTOSTART_PLIST_LEGACY   = "com.jarvis.assistant.plist"
_AUTOSTART_DESKTOP        = "lumina.desktop"
_AUTOSTART_DESKTOP_LEGACY = "jarvis.desktop"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_DEFAULT_W, _DEFAULT_H = 1080, 740
_MIN_W,     _MIN_H     = 860, 600
_LEFT_W  = 156
_RIGHT_W = 360

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#0d0204"
    PANEL     = "#150407"
    PANEL2    = "#1c0609"
    BORDER    = "#4a151c"
    BORDER_B  = "#8c2633"
    BORDER_A  = "#651d27"
    PRI       = "#ff3b4d"
    PRI_DIM   = "#b92335"
    PRI_GHO   = "#3b0a10"
    ACC       = "#ff814a"
    ACC2      = "#ffc857"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3347"
    MUTED_C   = "#ff4965"
    TEXT      = "#ffdce1"
    TEXT_DIM  = "#a96771"
    TEXT_MED  = "#d5969f"
    WHITE     = "#fff5f6"
    DARK      = "#090102"
    BAR_BG    = "#23070b"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]
LEGACY_UI_COLOR  = "#00d4ff"

_NIGHT_OVERRIDES = {
    "BG":       "#000000",
    "PANEL":    "#050505",
    "PANEL2":   "#090909",
    "DARK":     "#020202",
    "BAR_BG":   "#0d0d0d",
    "TEXT":     "#eadfe1",
    "TEXT_DIM": "#846c70",
    "TEXT_MED": "#b99da2",
    "WHITE":    "#f8f3f4",
}


def _effective_ui_color(config: dict) -> str:
    """Migrate the old cyan factory colour to Lumina red without touching secrets."""
    saved = str(config.get("ui_color") or "").strip().lower()
    return DEFAULT_UI_COLOR if not saved or saved == LEGACY_UI_COLOR else saved


def apply_night_palette(enabled: bool) -> None:
    """Turn the neutral surfaces black while preserving the selected accent."""
    if enabled:
        for key, value in _NIGHT_OVERRIDES.items():
            setattr(C, key, value)


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole turquoise-family palette from the chosen accent
    colour (a hue shift — brightness and saturation ratios are preserved, so
    the design survives intact). Painted elements (HUD, waveform, metrics)
    pick it up next frame; stylesheet panels on their next rebuild.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme desaturates

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours held on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    A LIVE, complete theme swap. Replaces the old palette colours with the
    new ones in EVERY widget's stylesheet and repaints them, so the change
    lands INSTANTLY across the whole interface — panels, buttons and borders
    included, not only the painted elements — with no restart.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


def _vision_icon(kind: str, color: str, size: int = 22) -> QIcon:
    """Draw sharp, font-independent icons for the live Vision controls."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if kind in {"screen", "monitor"}:
        p.drawRoundedRect(QRectF(2.5, 3.5, 17.0, 12.0), 2.0, 2.0)
        p.drawLine(QPointF(11.0, 15.5), QPointF(11.0, 18.5))
        p.drawLine(QPointF(7.5, 18.5), QPointF(14.5, 18.5))
        p.drawLine(QPointF(11.0, 12.0), QPointF(11.0, 7.0))
        p.drawLine(QPointF(8.5, 9.3), QPointF(11.0, 6.8))
        p.drawLine(QPointF(13.5, 9.3), QPointF(11.0, 6.8))
    elif kind == "camera":
        p.drawRoundedRect(QRectF(3.0, 6.0, 16.0, 11.5), 2.0, 2.0)
        p.drawRoundedRect(QRectF(6.0, 3.8, 5.5, 3.2), 1.0, 1.0)
        p.drawEllipse(QPointF(11.0, 11.7), 3.2, 3.2)
    elif kind == "stop":
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(6.0, 6.0, 10.0, 10.0), 1.5, 1.5)
    elif kind == "window":
        p.drawRoundedRect(QRectF(3.0, 4.0, 16.0, 14.0), 2.0, 2.0)
        p.drawLine(QPointF(3.0, 8.0), QPointF(19.0, 8.0))
        p.drawEllipse(QPointF(6.0, 6.0), 0.7, 0.7)
        p.drawEllipse(QPointF(8.5, 6.0), 0.7, 0.7)
    elif kind == "pause":
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(5.0, 4.0, 4.0, 14.0), 1.0, 1.0)
        p.drawRoundedRect(QRectF(13.0, 4.0, 4.0, 14.0), 1.0, 1.0)
    elif kind == "resume":
        p.setBrush(QColor(color))
        path = QPainterPath()
        path.moveTo(7.0, 4.0); path.lineTo(18.0, 11.0)
        path.lineTo(7.0, 18.0); path.closeSubpath()
        p.drawPath(path)
    elif kind == "mic":
        p.drawRoundedRect(QRectF(7.2, 2.5, 7.6, 11.0), 3.8, 3.8)
        p.drawArc(QRectF(4.5, 7.0, 13.0, 10.0), 180 * 16, 180 * 16)
        p.drawLine(QPointF(11.0, 17.0), QPointF(11.0, 20.0))
        p.drawLine(QPointF(7.5, 20.0), QPointF(14.5, 20.0))
    else:  # glasses / Vision entry point
        p.drawEllipse(QRectF(2.5, 7.0, 7.0, 7.0))
        p.drawEllipse(QRectF(12.5, 7.0, 7.0, 7.0))
        p.drawLine(QPointF(9.5, 10.0), QPointF(12.5, 10.0))
        p.drawLine(QPointF(3.0, 8.0), QPointF(1.5, 5.5))
        p.drawLine(QPointF(19.0, 8.0), QPointF(20.5, 5.5))

    p.end()
    return QIcon(px)


def _vision_source_kind(source_id: str) -> str:
    if source_id == "camera":
        return "camera"
    if str(source_id).startswith("window:"):
        return "window"
    return "monitor"


def _exclude_window_from_capture(widget: QWidget) -> None:
    """Keep Lumina's controls out of the visual stream on supported Windows."""
    if _OS != "Windows":
        return
    try:
        import ctypes
        # WDA_EXCLUDEFROMCAPTURE. Unsupported builds simply return false.
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)
    except Exception:
        pass


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free, works on all platforms if installed
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes
        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess)
        if _OS == "Windows":
            try:
                import wmi  # type: ignore
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = APP_NAME, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp

        # Slow "breathing" base target (random shimmer), refreshed on a timer.
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._base_scale = 1.03
                self._base_halo  = 122.0
            elif self.muted:
                self._base_scale = random.uniform(0.998, 1.002)
                self._base_halo  = random.uniform(15, 28)
            else:
                self._base_scale = random.uniform(1.001, 1.008)
                self._base_halo  = random.uniform(48, 68)
            self._last_t = now

        # Every frame, the live audio level lifts the target on top of the base
        # — this is what makes the core visibly pulse to the actual voice.
        if self.muted:
            self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
        elif self.speaking:
            self._tgt_scale = self._base_scale + amp * 0.13
            self._tgt_halo  = self._base_halo  + amp * 95.0
        else:
            self._tgt_scale = self._base_scale + amp * 0.06
            self._tgt_halo  = self._base_halo  + amp * 75.0

        sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        # Rings/scanners spin faster while speaking, reacting to loudness.
        boost  = 1.0 + amp * 1.6
        speeds = ([1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9])
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd * boost) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3) * boost) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75) * boost) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # corner brackets
        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw // 2, cx + fw // 2
        ht, hb = cy - fw // 2, cy + fw // 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # face
        if self._face_px:
            fsz    = int(fw * 0.62 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            orb   = QColor(C.MUTED_C if self.muted else C.PRI)
            oc    = (orb.red(), orb.green(), orb.blue())
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, self._assistant_name)

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # status text
        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform — reacts to the real audio level (mic while listening,
        # JARVIS's own voice while speaking). Falls back to a gentle idle
        # ripple when there's no sound. _amp_disp is the smoothed 0–1 level.
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        amp = self._amp_disp
        mid = (N - 1) / 2.0
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            else:
                env     = (1.0 - abs(i - mid) / mid) ** 0.7      # center-weighted hump
                shimmer = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                idle    = 3.0 + 2.0 * math.sin(self._tick * 0.09 + i * 0.6)
                hgt     = int(max(2, min(24, idle + amp * 22.0 * env * shimmer)))
                if amp > 0.05:
                    cl = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
                else:
                    cl = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

        p.end()   # end deterministically so the backing store never flushes an active painter

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()

# Backlog, in characters, that still gets the character-by-character animation.
# Roughly one spoken sentence: short enough that a reply never has to queue
# behind a long one, generous enough that ordinary lines still type out.
_TYPE_SMOOTH_CHARS = 160


class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = APP_NAME.lower()   # updated when assistant name changes
        self._user_name_lc = "you"            # updated from the saved user name
        # The provisional line showing what the user is saying right now.
        # _live_len is how many characters of it are currently drawn, so the
        # next update knows exactly how much to take back off the end.
        self._live_len     = 0
        self._live_pending = ""
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def set_live(self, text: str):
        """Show what the user is saying before their turn has closed.

        Written straight into the document instead of through the typewriter
        queue. This line is rewritten several times a second as the transcript
        grows, and animating it character by character would put it further
        behind the speaker with every update — the opposite of the point."""
        if self._typing:
            # The typewriter owns the end of the document while it runs, and
            # this used to be stashed until it let go. That is what made live
            # transcription look broken: one reply is several hundred
            # characters at six milliseconds each, so the user spoke, saw
            # nothing, spoke again, and then watched every sentence they had
            # said arrive at once and out of order with the conversation.
            # Their own words appearing as they say them is the feature; the
            # animation is decoration, so the animation gives way.
            self._flush_typing()
        self._render_live(text)

    def _tag_colour(self) -> QColor:
        return {
            "you":  qcol(C.WHITE),
            "ai":   qcol(C.PRI),
            "err":  qcol(C.RED),
            "file": qcol(C.GREEN),
            "sys":  qcol(C.ACC2),
        }.get(self._tag, qcol(C.TEXT))

    def _flush_typing(self) -> None:
        """Land every line that is still being typed out, immediately.

        The guard is not defensive tidiness: _next() refills _text from the
        queue, so this drains lines that arrive while it runs, and a log that
        somehow never empties must not take the interface with it.
        """
        guard = 0
        while self._typing and guard < 500:
            guard += 1
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            if self._pos < len(self._text):
                fmt = cur.charFormat()
                fmt.setForeground(QBrush(self._tag_colour()))
                cur.insertText(self._text[self._pos:], fmt)
                self._pos = len(self._text)
                self.setTextCursor(cur)
            else:
                self._tmr.stop()
                cur.insertText("\n")
                self.setTextCursor(cur)
                self._next()          # straight on, without the 20 ms hop
        self.ensureCursorVisible()

    def _render_live(self, text: str):
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        for _ in range(self._live_len):
            cur.deletePreviousChar()
        self._live_len = 0
        if text:
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(qcol(C.TEXT_DIM)))
            cur.insertText(text, fmt)
            self._live_len = len(text)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def _enqueue(self, text: str):
        # A real log line supersedes the provisional one it was previewing.
        self._live_pending = ""
        self._render_live("")
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            if self._live_pending:
                self._render_live(self._live_pending)
                self._live_pending = ""
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        _user_pfx = f"{self._user_name_lc}:"
        if   tl.startswith("you:") or tl.startswith(_user_pfx): self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            # How far behind the log is: what is left of this line plus
            # everything still queued behind it.
            backlog = (len(self._text) - self._pos) + sum(len(t) for t in self._queue)

            # One character per tick is 6 ms per character, which is a pleasant
            # 0.3 s for a short line and over half a minute once a news briefing
            # is queued. Everything after it waits — including the user's own
            # words, so speaking appears to do nothing at all and the assistant
            # looks deaf. Past a line's worth of backlog, the animation gives
            # way to catching up.
            span = 1 if backlog <= _TYPE_SMOOTH_CHARS else max(2, backlog // 40)
            chunk = self._text[self._pos : self._pos + span]

            cur = self.textCursor()
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(self._tag_colour()))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(chunk, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += len(chunk)
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select a file for {APP_NAME}", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol(C.PRI_GHO if z._drag_over else (C.PANEL2 if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cy = H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: {C.PANEL};
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl(f"Configure {APP_NAME} before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows": (C.PRI, C.DARK), "mac": (C.ACC2, C.DARK),
               "linux": (C.GREEN, C.DARK)}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C.DARK}; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (the small white
    circle) around the wheel to choose from EVERY hue.
    The filled circle at the centre live-previews the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y grows down — flip to maths axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── Drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # Center preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # Dragged handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str, str, str)
    _OW, _OH = 400, 588

    def __init__(self, assistant_name=APP_NAME, user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", provider="gemini",
                 openai_voice="shimmer", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: {C.DARK}; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import (
            AVAILABLE_VOICES, DEFAULT_VOICE, OPENAI_VOICES,
            DEFAULT_OPENAI_VOICE, VOICE_PROVIDERS,
        )
        lay.addSpacing(4)
        lay.addWidget(_lbl("VOICE ENGINE  ·  VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_provider = provider if provider in VOICE_PROVIDERS else "gemini"
        self._gemini_voice = voice if voice in AVAILABLE_VOICES else DEFAULT_VOICE
        self._openai_voice = (
            openai_voice if openai_voice in OPENAI_VOICES else DEFAULT_OPENAI_VOICE
        )
        combo_css = f"""
            QComboBox {{ background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}
            QComboBox:hover {{ border-color: {C.BORDER_B}; }}
            QComboBox QAbstractItemView {{ background: {C.DARK}; color: {C.TEXT};
                selection-background-color: {C.PRI_GHO}; }}
        """
        self._provider_box = QComboBox()
        self._provider_box.addItem("Gemini Live", "gemini")
        self._provider_box.addItem("OpenAI Realtime", "openai")
        self._provider_box.setCurrentIndex(0 if self._sel_provider == "gemini" else 1)
        self._provider_box.setFixedHeight(30)
        self._provider_box.setStyleSheet(combo_css)
        self._voice_box = QComboBox()
        self._voice_box.setFixedHeight(30)
        self._voice_box.setStyleSheet(combo_css)
        voice_row = QHBoxLayout(); voice_row.setSpacing(6)
        voice_row.addWidget(self._provider_box, 2)
        voice_row.addWidget(self._voice_box, 3)
        lay.addLayout(voice_row)
        self._provider_box.currentIndexChanged.connect(self._on_provider_pick)
        self._voice_box.currentIndexChanged.connect(self._remember_voice_pick)
        self._refresh_voice_options()

        # ── UI colour — hue wheel ────────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow binds it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText(f"{DEFAULT_UI_COLOR}   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── Voice selection ──────────────────────────────────────────────────────
    def _remember_voice_pick(self):
        value = self._voice_box.currentData()
        if not value:
            return
        if self._sel_provider == "openai":
            self._openai_voice = value
        else:
            self._gemini_voice = value

    def _on_provider_pick(self):
        self._remember_voice_pick()
        self._sel_provider = self._provider_box.currentData() or "gemini"
        self._refresh_voice_options()

    def _refresh_voice_options(self):
        from memory.config_manager import AVAILABLE_VOICES, OPENAI_VOICES
        self._voice_box.blockSignals(True)
        self._voice_box.clear()
        if self._sel_provider == "openai":
            labels = {"shimmer": "Sol · soft and youthful (Shimmer)"}
            for voice in OPENAI_VOICES:
                self._voice_box.addItem(labels.get(voice, voice.title()), voice)
            selected = self._openai_voice
        else:
            for voice in AVAILABLE_VOICES:
                self._voice_box.addItem(voice, voice)
            selected = self._gemini_voice
        index = self._voice_box.findData(selected)
        self._voice_box.setCurrentIndex(max(0, index))
        self._voice_box.blockSignals(False)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the colour; hex box and wheel stay in sync, theme previews live."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # While dragging: update the hex box, do not apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # If a preview was applied, go back to the colour we opened with
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or APP_NAME
        user = self._user_input.text().strip()
        self._remember_voice_pick()
        self.saved.emit(
            name, user, self._sel_color or DEFAULT_UI_COLOR,
            self._sel_provider, self._gemini_voice, self._openai_voice,
        )
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class VisionConsentOverlay(_HudOverlay):
    """One-time, human-controlled opt-in before continuous Vision is sent."""

    answered = pyqtSignal(bool)
    _OW = 460

    def __init__(self, source: str, source_label: str = "", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            VisionConsentOverlay {{
                background: rgba(7, 5, 18, 252);
                border: 1px solid {C.PRI};
                border-radius: 8px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 17, 20, 17)
        lay.setSpacing(9)

        hdr = QHBoxLayout(); hdr.setSpacing(9)
        icon = QLabel()
        icon.setPixmap(_vision_icon("glasses", C.PRI, 26).pixmap(26, 26))
        icon.setStyleSheet("background: transparent;")
        hdr.addWidget(icon)
        title = QLabel("TURN ON LIVE VISION")
        title.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        source_name = (
            "your camera" if source == "camera"
            else (source_label or "the selected screen or application")
        )
        body = QLabel(
            f"Lumina will send snapshots from {source_name} to Gemini while "
            "Vision is active so you can ask questions by voice in real time."
        )
        body.setWordWrap(True)
        body.setFont(QFont("Courier New", 9))
        body.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(body)

        privacy = QLabel(
            "Vision is off by default. A permanent on-screen indicator and STOP "
            "control remain visible while sharing. Lumina does not save the frames."
        )
        privacy.setWordWrap(True)
        privacy.setFont(QFont("Courier New", 8))
        privacy.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(privacy)

        row = QHBoxLayout(); row.setSpacing(8)
        cancel = QPushButton("CANCEL")
        cancel.setFixedHeight(34)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(cancel)

        start = QPushButton("START VISION")
        start.setIcon(_vision_icon(source, C.WHITE, 20))
        start.setIconSize(QSize(20, 20))
        start.setFixedHeight(34)
        start.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        start.setCursor(Qt.CursorShape.PointingHandCursor)
        start.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_DIM}; color: {C.WHITE};
                border: 1px solid {C.PRI}; border-radius: 4px; }}
            QPushButton:hover {{ background: {C.PRI}; }}
        """)
        start.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(start)
        lay.addLayout(row)

        cancel.setDefault(True)
        cancel.setFocus()


class VisionSourceOverlay(_HudOverlay):
    """Visual share picker with Window and Entire Screen thumbnail tabs."""

    selected = pyqtSignal(str, str)
    thumbnailReady = pyqtSignal(str, bytes)
    _OW = 640
    _OH = 535

    def __init__(self, sources: list[dict[str, str]], parent=None):
        super().__init__(parent)
        self._sources = sources
        self._choices: dict[str, QToolButton] = {}
        self._selected_id = ""
        self._selected_label = ""
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(self._OW, self._OH)
        self.setStyleSheet(f"""
            VisionSourceOverlay {{
                background: rgba(7, 5, 18, 252);
                border: 1px solid {C.PRI}; border-radius: 8px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 15, 18, 15)
        root.setSpacing(9)

        header = QHBoxLayout(); header.setSpacing(9)
        icon = QLabel()
        icon.setPixmap(_vision_icon("screen", C.PRI, 25).pixmap(25, 25))
        header.addWidget(icon)
        title = QLabel("CHOOSE WHAT TO SHARE WITH LUMINA")
        title.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        header.addWidget(title, stretch=1)
        close = QPushButton("CLOSE")
        close.setFixedSize(66, 27)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ color: {C.TEXT_MED}; background: transparent;
                border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}
        """)
        close.clicked.connect(self.hide)
        header.addWidget(close)
        root.addLayout(header)

        monitors = [s for s in sources if s.get("kind") == "monitor"]
        windows = [s for s in sources if s.get("kind") == "window"]
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ background: {C.PANEL2}; border: 1px solid {C.BORDER};
                border-radius: 6px; top: -1px; }}
            QTabBar::tab {{ background: transparent; color: {C.TEXT_MED};
                padding: 8px 24px; border: none; border-bottom: 2px solid transparent;
                font-family: 'Courier New'; font-size: 9pt; font-weight: bold; }}
            QTabBar::tab:selected {{ color: {C.WHITE}; border-bottom-color: {C.PRI}; }}
            QTabBar::tab:hover {{ color: {C.PRI}; }}
        """)
        tabs.addTab(self._make_source_page(windows), "WINDOW")
        tabs.addTab(self._make_source_page(monitors), "ENTIRE SCREEN")
        root.addWidget(tabs, stretch=1)

        footer = QHBoxLayout(); footer.setSpacing(8)
        privacy = QLabel(
            "Share sends frames to Gemini until Stop; Lumina does not save them. "
            "A yellow border marks the source."
        )
        privacy.setWordWrap(True)
        privacy.setFont(QFont("Courier New", 7))
        privacy.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        footer.addWidget(privacy, stretch=1)
        cancel = QPushButton("CANCEL")
        cancel.setFixedSize(78, 32)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ color: {C.TEXT_MED}; background: transparent;
                border: 1px solid {C.BORDER}; border-radius: 5px; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}
        """)
        cancel.clicked.connect(self.hide)
        footer.addWidget(cancel)
        self._share = QPushButton("SHARE")
        self._share.setFixedSize(82, 32)
        self._share.setEnabled(False)
        self._share.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._share.setCursor(Qt.CursorShape.PointingHandCursor)
        self._share.setStyleSheet(f"""
            QPushButton {{ color: {C.WHITE}; background: {C.PRI_DIM};
                border: 1px solid {C.PRI}; border-radius: 5px; }}
            QPushButton:hover {{ background: {C.PRI}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; background: {C.PANEL};
                border-color: {C.BORDER}; }}
        """)
        self._share.clicked.connect(self._share_selected)
        footer.addWidget(self._share)
        root.addLayout(footer)

        self.thumbnailReady.connect(self._apply_thumbnail)
        threading.Thread(
            target=self._load_thumbnails, daemon=True, name="vision-thumbnails"
        ).start()

    def _make_source_page(self, sources: list[dict[str, str]]) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: {C.PANEL}; width: 8px; }}
            QScrollBar::handle:vertical {{ background: {C.PRI_DIM}; min-height: 28px; }}
        """)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        grid = QGridLayout(content)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(12); grid.setVerticalSpacing(10)
        for index, source in enumerate(sources):
            source_id = str(source.get("id") or "")
            label = str(source.get("label") or "Untitled")
            detail = str(source.get("detail") or "")
            button = QToolButton()
            button.setObjectName("VisionSourceChoice")
            button.setText(f"{label}\n{detail}" if detail else label)
            button.setIcon(_vision_icon(source.get("kind", "window"), C.TEXT, 44))
            button.setIconSize(QSize(244, 137))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setFixedSize(268, 190)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFont(QFont("Courier New", 8))
            button.setToolTip(label)
            button.setAccessibleName(f"Share {label}")
            button.setStyleSheet(f"""
                QToolButton#VisionSourceChoice {{
                    padding: 7px;
                    color: {C.TEXT}; background: {C.PANEL2};
                    border: 1px solid {C.BORDER}; border-radius: 5px;
                }}
                QToolButton#VisionSourceChoice:hover {{
                    color: {C.WHITE}; background: {C.PRI_GHO};
                    border-color: {C.PRI};
                }}
                QToolButton#VisionSourceChoice:checked {{
                    color: {C.WHITE}; background: {C.PRI_GHO};
                    border: 2px solid {C.ACC2};
                }}
            """)
            button.clicked.connect(
                lambda _checked=False, sid=source_id, lbl=label: self._choose(sid, lbl)
            )
            self._choices[source_id] = button
            grid.addWidget(button, index // 2, index % 2)
        if not sources:
            empty = QLabel("No shareable sources found in this category.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 30px;")
            grid.addWidget(empty, 0, 0, 1, 2)
        grid.setRowStretch((len(sources) + 1) // 2, 1)
        scroll.setWidget(content)
        return scroll

    def _choose(self, source_id: str, label: str) -> None:
        self._selected_id = source_id
        self._selected_label = label
        for sid, button in self._choices.items():
            button.setChecked(sid == source_id)
        self._share.setEnabled(bool(source_id))

    def _share_selected(self) -> None:
        if self._selected_id:
            self.selected.emit(self._selected_id, self._selected_label)

    def _load_thumbnails(self) -> None:
        try:
            from actions.screen_processor import capture_source_thumbnail
            for source in self._sources:
                if not self.parentWidget() or not self.parentWidget().isVisible():
                    break
                try:
                    data = capture_source_thumbnail(str(source.get("id") or ""))
                    if data:
                        self.thumbnailReady.emit(str(source.get("id") or ""), data)
                except Exception:
                    continue
        except Exception:
            pass

    def _apply_thumbnail(self, source_id: str, data: bytes) -> None:
        button = self._choices.get(source_id)
        if not button:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if not pixmap.isNull():
            button.setIcon(QIcon(pixmap))


class VisionFloatingBar(QWidget):
    """Always-visible sharing controls, independent of Lumina's main window."""

    pauseRequested = pyqtSignal(bool)
    stopRequested = pyqtSignal()
    micRequested = pyqtSignal()
    sourceRequested = pyqtSignal()

    def __init__(self):
        flags = (
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self.setObjectName("VisionFloatingBar")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedHeight(52)
        self.setMinimumWidth(570)
        self._paused = False
        self._drag_offset: QPoint | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 7, 10, 7)
        root.setSpacing(7)

        shell = QFrame()
        shell.setObjectName("VisionBarShell")
        shell.setStyleSheet(f"""
            QFrame#VisionBarShell {{ background: rgba(12, 3, 6, 246);
                border: 1px solid {C.PRI}; border-radius: 12px; }}
            QLabel {{ background: transparent; border: none; }}
        """)
        row = QHBoxLayout(shell)
        row.setContentsMargins(11, 5, 7, 5)
        row.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        row.addWidget(self._dot)
        self._source_icon = QLabel()
        self._source_icon.setFixedSize(19, 19)
        row.addWidget(self._source_icon)
        self._source = QLabel("VISION")
        self._source.setMinimumWidth(150)
        self._source.setMaximumWidth(250)
        self._source.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._source.setStyleSheet(f"color: {C.WHITE};")
        row.addWidget(self._source, stretch=1)

        self._mic = QPushButton("LISTENING")
        self._mic.setIcon(_vision_icon("mic", C.GREEN, 18))
        self._mic.clicked.connect(self.micRequested.emit)
        row.addWidget(self._mic)
        self._pause = QPushButton("PAUSE")
        self._pause.clicked.connect(self._toggle_pause)
        row.addWidget(self._pause)
        self._stop = QPushButton("STOP")
        self._stop.setIcon(_vision_icon("stop", C.MUTED_C, 17))
        self._stop.clicked.connect(self.stopRequested.emit)
        row.addWidget(self._stop)
        self._style_buttons()
        root.addWidget(shell)

    def _style_buttons(self) -> None:
        common = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 6px;
                padding: 4px 8px; font-family: 'Courier New';
                font-size: 8pt; font-weight: bold; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI};
                background: {C.PRI_GHO}; }}
        """
        self._mic.setStyleSheet(common)
        self._pause.setStyleSheet(common)
        self._stop.setStyleSheet(common + f"""
            QPushButton {{ color: {C.MUTED_C}; border-color: {C.MUTED_C}; }}
        """)

    def _toggle_pause(self) -> None:
        self.pauseRequested.emit(not self._paused)

    def update_state(self, source_id: str, source_label: str,
                     state: str, detail: str) -> None:
        self._paused = state == "paused"
        kind = _vision_source_kind(source_id)
        self._source_icon.setPixmap(_vision_icon(kind, C.WHITE, 18).pixmap(18, 18))
        self._source.setText(source_label.upper()[:32] or kind.upper())
        self._source.setToolTip(source_label)
        self._source.setAccessibleName(f"Shared source: {source_label}")
        self._pause.setText("RESUME" if self._paused else "PAUSE")
        icon_kind = "resume" if self._paused else "pause"
        self._pause.setIcon(_vision_icon(icon_kind, C.TEXT_MED, 17))
        color = C.ACC2 if state in {"starting", "paused"} else C.GREEN
        self._dot.setStyleSheet(f"color: {color};")
        self.setToolTip(detail)
        if not self.isVisible():
            self._place_near_source(source_id)
            self.show(); self.raise_()
            _exclude_window_from_capture(self)

    def update_mic(self, muted: bool, busy: str, mic_open: bool) -> None:
        if muted:
            text, color = "MIC MUTED", C.MUTED_C
        elif busy and not mic_open:
            text, color = "MIC OFF", C.ACC2
        else:
            text, color = "LISTENING", C.GREEN
        self._mic.setText(text)
        self._mic.setIcon(_vision_icon("mic", color, 18))
        self._mic.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {color};
                border: 1px solid {color}; border-radius: 6px;
                padding: 4px 8px; font-family: 'Courier New';
                font-size: 8pt; font-weight: bold; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; }}
        """)

    def _place_near_source(self, source_id: str) -> None:
        try:
            if source_id != "camera":
                from actions.screen_processor import capture_source_geometry
                area = capture_source_geometry(source_id)
                left, top = area["left"], area["top"]
                width = area["width"]
            else:
                geo = QApplication.primaryScreen().availableGeometry()
                left, top, width = geo.left(), geo.top(), geo.width()
            self.adjustSize()
            x = int(left + max(8, (width - self.width()) / 2))
            self.move(x, int(top + 12))
        except Exception:
            geo = QApplication.primaryScreen().availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.top() + 12)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class VisionCaptureBorderOverlay(QWidget):
    """Yellow, click-through frame that follows the exact shared source."""

    def __init__(self):
        flags = (
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        try:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        except AttributeError:
            pass
        self._source_id = ""
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._follow_source)

    def track(self, source_id: str, state: str) -> None:
        if not source_id or source_id == "camera" or state in {"off", "error"}:
            self.stop()
            return
        self._source_id = source_id
        self._paused = state == "paused"
        self._follow_source()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._source_id = ""
        self.hide()

    def _follow_source(self) -> None:
        if not self._source_id:
            return
        try:
            from actions.screen_processor import capture_source_geometry
            area = capture_source_geometry(self._source_id)
            margin = 0
            geometry = (
                int(area["left"]) - margin, int(area["top"]) - margin,
                int(area["width"]) + margin * 2, int(area["height"]) + margin * 2,
            )
            if self.geometry().getRect() != geometry:
                self.setGeometry(*geometry)
            if not self.isVisible():
                self.show(); self.raise_()
                _exclude_window_from_capture(self)
            self.update()
        except Exception:
            self.hide()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        color = QColor(C.ACC2)
        color.setAlpha(150 if self._paused else 255)
        pen = QPen(color, 4.0)
        if self._paused:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(2, 2, max(1, self.width() - 5), max(1, self.height() - 5))
        p.end()


class VisionHighlightOverlay(QWidget):
    """Click-through animated pointer placed over a shared screen/window."""

    def __init__(self):
        flags = (
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        try:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        except AttributeError:
            pass
        self._target = QPointF(0, 0)
        self._label = ""
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._animate)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_highlight(self, geometry: dict[str, int], x: int, y: int,
                       label: str = "") -> None:
        width = max(2, int(geometry.get("width", 2)))
        height = max(2, int(geometry.get("height", 2)))
        self.setGeometry(
            int(geometry.get("left", 0)), int(geometry.get("top", 0)), width, height
        )
        self._target = QPointF(
            max(0.0, min(width - 1.0, width * max(0, min(1000, x)) / 1000.0)),
            max(0.0, min(height - 1.0, height * max(0, min(1000, y)) / 1000.0)),
        )
        self._label = str(label or "Click here")[:48]
        self._phase = 0
        self.show(); self.raise_(); self.update()
        _exclude_window_from_capture(self)
        self._timer.start()
        self._hide_timer.start(6000)

    def _animate(self) -> None:
        self._phase += 1
        if not self.isVisible():
            self._timer.stop()
            return
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pulse = 4.0 + 7.0 * (0.5 + 0.5 * math.sin(self._phase * 0.22))
        center = self._target
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.PRI, 90), 3.0))
        p.drawEllipse(center, 24.0 + pulse, 24.0 + pulse)
        p.setPen(QPen(QColor(C.WHITE), 3.0))
        p.drawEllipse(center, 18.0, 18.0)
        p.setBrush(QColor(C.PRI))
        p.setPen(QPen(QColor(C.WHITE), 2.0))
        p.drawEllipse(center, 6.0, 6.0)

        label_w = min(260, max(105, 18 + len(self._label) * 8))
        left = min(max(8.0, center.x() + 28.0), max(8.0, self.width() - label_w - 8.0))
        top = min(max(8.0, center.y() - 20.0), max(8.0, self.height() - 42.0))
        box = QRectF(left, top, label_w, 34)
        p.setBrush(qcol(C.DARK, 235)); p.setPen(QPen(QColor(C.PRI), 1.5))
        p.drawRoundedRect(box, 7.0, 7.0)
        p.setPen(QColor(C.WHITE)); p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.drawText(box.adjusted(10, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, self._label)
        p.end()


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone LUMINA listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'LUMINA can't hear me' is usually 'LUMINA is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: {C.DARK}; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row(f"MICROPHONE — what {APP_NAME} hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row(f"SPEAKERS — what {APP_NAME} talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class ApiKeysOverlay(_HudOverlay):
    """Optional service keys, entered here instead of by hand in api_keys.json.

    Every field is generated from config_manager.OPTIONAL_KEYS, so an
    integration added to that table shows up here with no work in this file.
    The point is that someone who clones the repo can reach the same Lumina as
    the person who built it without being told to go and edit JSON."""

    saved = pyqtSignal(list, list)      # (labels changed, labels of odd format)
    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        from memory.config_manager import OPTIONAL_KEYS, get_optional_key

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ApiKeysOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        _field_css = f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """
        _eye_css = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
        """

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(5)

        hdr = QLabel("🔑  API KEYS")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        intro = QLabel(
            f"Add OpenAI here to enable its Realtime voice in {APP_NAME} and "
            "Learning English. Gemini remains available. Other services are "
            "optional. Keys stay in this PC; changes apply without an app restart."
        )
        intro.setWordWrap(True)
        intro.setFont(QFont("Courier New", 7))
        intro.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(intro)
        lay.addSpacing(6)

        # (spec, field, value as it was when the panel opened)
        self._rows: list[tuple[dict, QLineEdit, str]] = []

        for spec in OPTIONAL_KEYS:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {C.BORDER};")
            lay.addWidget(sep)
            lay.addSpacing(2)

            stored = get_optional_key(spec["config_key"])
            head = QLabel(f"{spec['label']}   "
                          + ("● CONFIGURED" if stored else "○ NOT SET"))
            head.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            head.setStyleSheet(
                f"color: {C.ACC2 if stored else C.TEXT_DIM}; background: transparent;")
            lay.addWidget(head)

            why = QLabel(spec["purpose"])
            why.setWordWrap(True)
            why.setFont(QFont("Courier New", 7))
            why.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(why)

            row = QHBoxLayout(); row.setSpacing(6)
            edit = QLineEdit(stored)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(f"{spec.get('prefix', '')}…")
            edit.setFont(QFont("Courier New", 9))
            edit.setFixedHeight(30)
            edit.setStyleSheet(_field_css)
            row.addWidget(edit)

            # Reveal toggle: checking a 50-character key you have just pasted
            # into a field of dots is otherwise guesswork.
            eye = QPushButton("👁")
            eye.setFixedSize(30, 30)
            eye.setCheckable(True)
            eye.setCursor(Qt.CursorShape.PointingHandCursor)
            eye.setStyleSheet(_eye_css)
            eye.toggled.connect(
                lambda on, e=edit: e.setEchoMode(
                    QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
                )
            )
            row.addWidget(eye)
            lay.addLayout(row)

            link = QLabel(f"Get one at  {spec['signup']}")
            link.setFont(QFont("Courier New", 7))
            link.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(link)
            lay.addSpacing(6)

            self._rows.append((spec, edit, stored))

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  SAVE")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import save_optional_key

        changed, odd = [], []
        for spec, edit, before in self._rows:
            now = edit.text().strip()
            if now == before:
                continue                     # untouched — do not rewrite the file
            save_optional_key(spec["config_key"], now)
            changed.append(spec["label"])

            # A wrong-looking prefix is reported, never rejected: key formats do
            # change, and refusing a valid new one is worse than a stale warning.
            prefix = spec.get("prefix", "")
            if now and prefix and not now.startswith(prefix):
                odd.append(f"{spec['label']} (expected {prefix}…)")

        self.hide()
        self.saved.emit(changed, odd)


class MemoryOverlay(_HudOverlay):
    """Everything LUMINA has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel(f"🧠  WHAT {APP_NAME} REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Lumina actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText(f"Phone connected — {APP_NAME} ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _live_sig       = pyqtSignal(str)   # partial transcript, replaced as it grows
    _state_sig      = pyqtSignal(str)
    _busy_sig       = pyqtSignal(str, bool)   # what she is doing, and whether the mic is live
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _cam_error_sig  = pyqtSignal(str)        # camera worker failed to open/read
    _vision_state_sig = pyqtSignal(str, str, str)  # source, state, detail
    _vision_highlight_sig = pyqtSignal(str, int, int, str)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or APP_NAME).strip()
        self._user_name: str = (_cfg.get("user_name") or "You").strip()
        _display = self._assistant_name.upper()

        # Apply the saved theme before any widget captures palette colours.
        _ui_color = _effective_ui_color(_cfg)
        self._night_mode = bool(_cfg.get("night_mode", False))
        apply_ui_accent(_ui_color)
        apply_night_palette(self._night_mode)

        self.setWindowTitle(APP_NAME)
        if APP_ICON_ICO.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_ICO)))
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_learning_english = None # callable: () -> None — opens web studio
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self.on_vision_requested = None     # callable: stable source id -> None
        self.on_vision_stop      = None     # callable: () -> None
        self.on_vision_pause     = None     # callable: paused bool -> None
        self.on_camera_frame     = None     # callable: (jpeg bytes) -> None
        self.on_camera_error     = None     # callable: (message) -> None
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self._muted            = False
        self._vision_source    = ""
        self._vision_source_label = ""
        self._vision_source_labels: dict[str, str] = {}
        self._vision_state     = "off"
        self._vision_detail    = "Vision off"
        self._vision_consent_overlay = None
        self._vision_source_overlay = None
        # Why the app itself has the microphone shut, if it has: the
        # user's mute is theirs and is never overwritten by this.
        self._busy             = ""
        self._busy_mic_open    = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None
        # Voice transcription temporarily owns the command box only while the
        # user has not typed there. This makes speech unmistakably visible in
        # the chat controls without ever overwriting a manual draft.
        self._voice_draft_owned = False
        self._voice_draft_text = ""
        self._voice_monitor_transcribed = False

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet(f"background: {C.BG};")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        self._cam_title = QLabel("●  LIVE VISION — CAMERA")
        self._cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(self._cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self._request_stop_vision)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)
        self._log._ai_name_lc = self._assistant_name.lower()
        self._log._user_name_lc = self._user_name.lower()

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        self._update_night_btn()
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrics refresh timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        # Is the interface thread actually running? A slot can be entered and
        # its text still not reach the screen, because painting only happens
        # when the event loop gets a turn. This ticks twice a second and says
        # so whenever it did not — which is the difference between "the words
        # never arrived" and "the window was frozen when they did".
        self._beat_at = time.time()

        def _beat():
            now = time.time()
            late = now - self._beat_at - 0.5
            self._beat_at = now
            if late > 1.0:
                print(f"[UI] main thread stalled {late:.1f}s", flush=True)

        self._beat_tmr = QTimer(self)
        self._beat_tmr.timeout.connect(_beat)
        self._beat_tmr.start(500)

        self._log_sig.connect(self._log.append_log)
        self._live_sig.connect(self._apply_live_transcript)
        self._state_sig.connect(self._apply_state)
        self._busy_sig.connect(self._apply_busy)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._cam_error_sig.connect(self._on_cam_error)
        self._vision_state_sig.connect(self._apply_vision_state)
        self._vision_highlight_sig.connect(self._apply_vision_highlight)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._cam_stop = threading.Event()
        self._cam_thread = None
        self._cam_lock = threading.Lock()
        self._cam_desired = False
        self._cam_restart_pending = False

        # Persistent top-level sharing UI: it remains usable if the main
        # window is obscured and is excluded from screen capture on Windows.
        self._vision_bar = VisionFloatingBar()
        self._vision_bar.pauseRequested.connect(self._request_pause_vision)
        self._vision_bar.stopRequested.connect(self._request_stop_vision)
        self._vision_bar.micRequested.connect(self._toggle_mute)
        self._vision_border = VisionCaptureBorderOverlay()
        self._vision_highlight = VisionHighlightOverlay()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        with self._cam_lock:
            self._cam_desired = True
            existing = self._cam_thread
            if existing is not None and existing.is_alive():
                if not self._cam_stop.is_set() or self._cam_restart_pending:
                    return
                # A fast camera → screen → camera switch can arrive before the
                # device worker has released the webcam. Restart only after it has.
                self._cam_restart_pending = True
                threading.Thread(
                    target=self._restart_camera_after,
                    args=(existing,),
                    daemon=True,
                    name="cam-restart",
                ).start()
                return
            stop_event = threading.Event()
            self._cam_stop = stop_event
            self._cam_thread = threading.Thread(
                target=self._cam_loop,
                args=(stop_event,),
                daemon=True,
                name="cam-stream",
            )
            thread = self._cam_thread
        self._cam_stream_sig.emit(True)
        thread.start()

    def _restart_camera_after(self, previous: threading.Thread) -> None:
        previous.join(timeout=3.0)
        with self._cam_lock:
            self._cam_restart_pending = False
            desired = self._cam_desired
            still_alive = previous.is_alive()
        if still_alive:
            self._cam_error_sig.emit("The previous camera stream did not stop in time.")
        elif desired:
            self.start_camera_stream()

    def _cam_loop(self, stop_event: threading.Event) -> None:
        cap = None
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text(encoding="utf-8"))
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                raise RuntimeError("No camera could be opened. Check Windows camera permissions.")
            # warm-up frames
            for _ in range(5):
                cap.read()
            last_vision_frame = 0.0
            consecutive_failures = 0
            while not stop_event.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    consecutive_failures = 0
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    data = buf.tobytes()
                    self._cam_frame_sig.emit(data)
                    # Gemini Live accepts at most one video frame per second.
                    # The preview stays fluid at ~30 fps; only this callback is gated.
                    now = time.monotonic()
                    if now - last_vision_frame >= 1.0:
                        last_vision_frame = now
                        if self.on_camera_frame:
                            self.on_camera_frame(data)
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 30:
                        raise RuntimeError("The camera stopped returning frames.")
            if not stop_event.is_set() and not cap.isOpened():
                raise RuntimeError("The camera was disconnected.")
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
            self._cam_error_sig.emit(str(e))
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            with self._cam_lock:
                if self._cam_thread is threading.current_thread():
                    self._cam_thread = None
                    is_current = True
                else:
                    is_current = False
            if is_current:
                self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        with self._cam_lock:
            self._cam_desired = False
            self._cam_stop.set()

    def _on_cam_error(self, message: str) -> None:
        self._log.append_log(f"ERR: Camera Vision — {message}")
        if self.on_camera_error:
            self.on_camera_error(message)

    # ------------------------------------------------------------------
    # Icon generation — Lumina mascot rendered into a multi-resolution ICO
    # ------------------------------------------------------------------
    @staticmethod
    def _build_lumina_icon(out_path: Path) -> bool:
        """Build the Windows icon from the bundled Lumina mascot PNG."""
        try:
            import PIL.Image
            source = PIL.Image.open(APP_ICON_PNG).convert("RGBA")
            source.save(
                out_path,
                format="ICO",
                sizes=[(256, 256), (128, 128), (64, 64),
                       (48, 48), (32, 32), (16, 16)],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] Lumina icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "LUMINA AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "LUMINA AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Lumina mascot icon (.ico — also exported as .png for Linux/macOS)
        ico_path = APP_ICON_ICO
        if not ico_path.exists():
            self._build_lumina_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "LUMINA.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "LUMINA.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "LUMINA"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n',
                    encoding="utf-8"
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>LUMINA</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.lumina.assistant</string>\n'
                    '  <key>CFBundleName</key><string>LUMINA</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n',
                    encoding="utf-8"
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text(encoding="utf-8")
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        ),
                        encoding="utf-8"
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "LUMINA.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=LUMINA\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line,
                    encoding="utf-8"
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._vision_consent_overlay and self._vision_consent_overlay.isVisible():
            self._centre_overlay(self._vision_consent_overlay)
        if self._vision_source_overlay and self._vision_source_overlay.isVisible():
            self._centre_overlay(self._vision_source_overlay)
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(64)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 0, 14, 0)

        left = QWidget()
        left.setFixedWidth(180)
        left.setStyleSheet("background: transparent;")
        left_lay = QHBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(10)
        mascot = QLabel()
        mascot.setFixedSize(40, 40)
        mascot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mascot.setStyleSheet("background: transparent;")
        if APP_ICON_PNG.exists():
            px = QPixmap(str(APP_ICON_PNG))
            mascot.setPixmap(px.scaled(
                38, 38, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        mascot.setToolTip(APP_NAME)
        left_lay.addWidget(mascot)

        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(32, 32)
        self._drawer_btn.setFont(QFont("Courier New", 13))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        left_lay.addWidget(self._drawer_btn)
        left_lay.addStretch()
        lay.addWidget(left)

        mid = QVBoxLayout(); mid.setSpacing(1)
        self._title_lbl = QLabel(APP_NAME)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        self._sub_lbl = QLabel("PERSONAL AI ASSISTANT")
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid, stretch=1)

        right = QWidget()
        right.setFixedWidth(180)
        right.setStyleSheet("background: transparent;")
        right_col = QVBoxLayout(); right_col.setSpacing(2)
        right_col.setContentsMargins(0, 0, 0, 0)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        right.setLayout(right_col)
        lay.addWidget(right)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Courier New", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addStretch()

        for txt, col in [
            ("AI CORE\nACTIVE",  C.GREEN),
            ("SEC\nCLEARED",     C.PRI),
            ("SYSTEM\nREADY",    C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {col}; background: {C.PANEL2};"
                f"border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 4px;"
            )
            lay.addWidget(lbl)

        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))

        # Keep the live words directly beside the conversation instead of
        # below file upload, where they were easy to miss on a compact window.
        self._live_caption = QLabel()
        self._live_caption.setObjectName("LiveTranscript")
        self._live_caption.setWordWrap(True)
        self._live_caption.setMinimumHeight(44)
        self._live_caption.setMaximumHeight(72)
        self._live_caption.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._live_caption.setStyleSheet(f"""
            QLabel#LiveTranscript {{
                color: {C.WHITE}; background: {C.PRI_GHO};
                border: 1px solid {C.PRI}; border-radius: 4px;
                padding: 5px 7px;
            }}
        """)
        self._live_caption.hide()
        lay.addWidget(self._live_caption)

        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep_vision = QFrame(); sep_vision.setFrameShape(QFrame.Shape.HLine)
        sep_vision.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep_vision)

        lay.addWidget(_sec("LIVE VISION"))
        lay.addWidget(self._build_vision_controls())

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #200010; border: 1px solid #ff6688;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        return w

    def _build_vision_controls(self) -> QWidget:
        """Visible, explicit screen/camera controls modelled on Copilot Vision."""
        card = QWidget()
        card.setObjectName("VisionCard")
        card.setStyleSheet(f"""
            QWidget#VisionCard {{
                background: {C.PANEL2}; border: 1px solid {C.BORDER};
                border-radius: 5px;
            }}
        """)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(7, 7, 7, 7)
        lay.setSpacing(6)

        source_row = QHBoxLayout(); source_row.setSpacing(6)
        self._vision_screen_btn = QPushButton("SELECT SOURCE")
        self._vision_camera_btn = QPushButton("CAMERA")
        for button, source, tip in (
            (self._vision_screen_btn, "screen",
             "Choose a display or application window for Lumina Vision"),
            (self._vision_camera_btn, "camera",
             "Share the camera with Lumina Vision"),
        ):
            button.setCheckable(True)
            button.setFixedHeight(34)
            button.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            source_row.addWidget(button)
        self._vision_screen_btn.clicked.connect(self._open_vision_source_selector)
        self._vision_camera_btn.clicked.connect(
            lambda _checked=False: self._request_vision("camera", "Camera")
        )
        lay.addLayout(source_row)

        smart = QLabel("✦ SMART CAPTURE · SENDS CHANGES")
        smart.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        smart.setToolTip("Static frames are skipped; periodic context refreshes remain enabled.")
        smart.setStyleSheet(
            f"color: {C.ACC2}; background: transparent; border: none;"
        )
        lay.addWidget(smart)

        status_row = QHBoxLayout(); status_row.setSpacing(6)
        self._vision_status_icon = QLabel()
        self._vision_status_icon.setFixedSize(20, 20)
        self._vision_status_icon.setStyleSheet("background: transparent;")
        status_row.addWidget(self._vision_status_icon)

        self._vision_status_lbl = QLabel("VISION OFF")
        self._vision_status_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._vision_status_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        status_row.addWidget(self._vision_status_lbl, stretch=1)

        self._vision_stop_btn = QPushButton("STOP")
        self._vision_stop_btn.setIcon(_vision_icon("stop", C.MUTED_C, 18))
        self._vision_stop_btn.setIconSize(QSize(18, 18))
        self._vision_stop_btn.setFixedHeight(25)
        self._vision_stop_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._vision_stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vision_stop_btn.setToolTip("Stop sharing visual input")
        self._vision_stop_btn.setAccessibleName("Stop Live Vision")
        self._vision_stop_btn.clicked.connect(self._request_stop_vision)
        status_row.addWidget(self._vision_stop_btn)
        lay.addLayout(status_row)

        self._style_vision_controls()
        return card

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        learning_btn = QPushButton("🎓  LEARNING ENGLISH")
        learning_btn.setFixedHeight(30)
        learning_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        learning_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        learning_btn.setToolTip("Open English Learning Studio in your browser")
        learning_btn.setAccessibleName("Open Learning English")
        learning_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(20, 120, 82, 0.18); color: #62F0B2;
                border: 1px solid rgba(67, 240, 170, 0.42); border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{
                background: rgba(20, 160, 102, 0.28);
                border-color: #43F0AA; color: #DFFFF2;
            }}
        """)
        learning_btn.clicked.connect(self._open_learning_english)
        lay.addWidget(learning_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        self._night_btn = QPushButton()
        self._night_btn.setFixedHeight(26)
        self._night_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._night_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._night_btn.clicked.connect(self._toggle_night_mode)
        lay.addWidget(self._night_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(26)
        self._brief_btn.setFont(QFont("Courier New", 7))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(26)
        audio_btn.setFont(QFont("Courier New", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(26)
        mem_btn.setFont(QFont("Courier New", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(26)
        plugin_btn.setFont(QFont("Courier New", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        keys_btn = QPushButton("🔑  API KEYS")
        keys_btn.setFixedHeight(26)
        keys_btn.setFont(QFont("Courier New", 7))
        keys_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        keys_btn.setStyleSheet(_BTN_STYLE_DIM)
        keys_btn.clicked.connect(self._open_api_keys)
        lay.addWidget(keys_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        self._quick_drawer.setGeometry(12, 54, _W, self._quick_drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _apply_live_transcript(self, text: str) -> None:
        """Render partial speech immediately, independent of log animation."""
        self._log.set_live(text)
        raw = (text or "").strip()
        speaker = self._user_name or "You"
        spoken = raw
        prefix, separator, remainder = raw.partition(":")
        if separator and prefix.casefold() in {"you", speaker.casefold()}:
            spoken = remainder.lstrip()
        if not spoken:
            self._live_caption.clear()
            self._live_caption.hide()
            if (
                self._voice_draft_owned
                and self._input.text() == self._voice_draft_text
            ):
                self._input.clear()
            self._voice_draft_owned = False
            self._voice_draft_text = ""
            self._voice_monitor_transcribed = False
            return

        # Keep the newest words visible when a long utterance exceeds the
        # compact panel. The activity log still retains the complete preview.
        visible = spoken if len(spoken) <= 240 else f"...{spoken[-237:]}"
        self._live_caption.setText(
            f"● {speaker.upper()} — LIVE TRANSCRIPT\n{visible}"
        )
        self._live_caption.show()

        # Mirror the live words in the command field. If the user starts typing,
        # their text wins immediately and later voice fragments leave it alone.
        current_draft = self._input.text()
        if self._voice_draft_owned and current_draft != self._voice_draft_text:
            self._voice_draft_owned = False
        if not self._voice_draft_owned and not current_draft.strip():
            self._voice_draft_owned = True
        if self._voice_draft_owned:
            self._voice_draft_text = spoken
            self._input.setText(spoken)
            self._input.setCursorPosition(len(spoken))

        if (
            spoken.casefold() != "listening..."
            and not self._voice_monitor_transcribed
        ):
            rendered = self._log.toPlainText().endswith(raw)
            print(
                f"[VOICE FLOW] 2/3 live transcript rendered in chat: "
                f"{'yes' if rendered else 'no'}",
                flush=True,
            )
            self._voice_monitor_transcribed = True

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(26)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        self._footer_theme_lbl = _fl("RED INTERFACE", C.PRI_DIM)
        lay.addWidget(self._footer_theme_lbl)
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    def _open_learning_english(self):
        if not self.on_learning_english:
            self._log.append_log("SYS: Learning English is still starting.")
            return
        self._drawer_btn.setChecked(False)
        self._toggle_drawer(False)
        try:
            self.on_learning_english()
        except Exception as exc:
            # An exception raised by a Qt slot can terminate the whole process.
            # Keep Lumina alive and make the launch failure visible instead.
            print(f"[Learning English] Could not start: {exc}")
            self._log.append_log(
                "ERR: Learning English could not start. Check the console for details."
            )

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    for name in (_AUTOSTART_REG, _AUTOSTART_REG_LEGACY):
                        try:
                            winreg.QueryValueEx(key, name)
                            return True
                        except FileNotFoundError:
                            continue
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                agents = Path.home() / "Library" / "LaunchAgents"
                return any((agents / n).exists()
                           for n in (_AUTOSTART_PLIST, _AUTOSTART_PLIST_LEGACY))
            else:
                autostart = Path.home() / ".config" / "autostart"
                return any((autostart / n).exists()
                           for n in (_AUTOSTART_DESKTOP, _AUTOSTART_DESKTOP_LEGACY))
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if not currently_on:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, _AUTOSTART_REG, 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                # Turning it off clears both names; turning it on clears the legacy
                # one, so the entry is never registered twice under two names.
                stale = ((_AUTOSTART_REG, _AUTOSTART_REG_LEGACY) if currently_on
                         else (_AUTOSTART_REG_LEGACY,))
                for name in stale:
                    try:
                        winreg.DeleteValue(reg, name)
                    except FileNotFoundError:
                        pass
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / _AUTOSTART_PLIST
                (plist_dir / _AUTOSTART_PLIST_LEGACY).unlink(missing_ok=True)
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        f'  <key>Label</key><string>{_AUTOSTART_LABEL}</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n',
                        encoding="utf-8"
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / _AUTOSTART_DESKTOP
                (desk_dir / _AUTOSTART_DESKTOP_LEGACY).unlink(missing_ok=True)
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n",
                        encoding="utf-8"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_night_mode(self):
        old = current_palette()
        self._night_mode = not self._night_mode
        accent = _effective_ui_color(_read_full_config())
        apply_ui_accent(accent)
        apply_night_palette(self._night_mode)
        retheme_all_widgets(old, current_palette())

        from memory.config_manager import save_night_mode
        save_night_mode(self._night_mode)
        self._update_night_btn()
        self._log.append_log(
            f"SYS: {'Night' if self._night_mode else 'Red'} interface enabled."
        )

    def _update_night_btn(self):
        if not hasattr(self, "_night_btn"):
            return
        if self._night_mode:
            self._night_btn.setText("☾  NIGHT MODE: ON")
            self._night_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C.PRI_GHO}; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ border-color: {C.PRI}; }}
            """)
            if hasattr(self, "_footer_theme_lbl"):
                self._footer_theme_lbl.setText("NIGHT MODE")
        else:
            self._night_btn.setText("☾  NIGHT MODE: OFF")
            self._night_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
            """)
            if hasattr(self, "_footer_theme_lbl"):
                self._footer_theme_lbl.setText("RED INTERFACE")

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", APP_NAME) or APP_NAME,
            cfg.get("user_name", ""),
            _effective_ui_color(cfg),
            cfg.get("voice_name", ""),
            cfg.get("voice_provider", "gemini"),
            cfg.get("openai_voice", "shimmer"),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the interface in the new colour (does NOT write config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            apply_night_palette(self._night_mode)
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(
        self, name: str, user_name: str, ui_color: str = "",
        provider: str = "gemini", gemini_voice: str = "", openai_voice: str = "",
    ):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or APP_NAME
        self._user_name = user_name.strip() or "You"
        display = self._assistant_name.upper()
        self.setWindowTitle(APP_NAME)
        self._title_lbl.setText(APP_NAME)
        self._sub_lbl.setText("PERSONAL AI ASSISTANT")
        self._log._ai_name_lc = self._assistant_name.lower()
        self._log._user_name_lc = self._user_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                apply_night_palette(self._night_mode)
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        from memory.config_manager import (
            get_voice, get_voice_provider, get_openai_voice, get_openai_key,
            save_voice_settings,
        )
        voice_changed = (
            provider != get_voice_provider()
            or (gemini_voice and gemini_voice != get_voice())
            or (openai_voice and openai_voice != get_openai_voice())
        )
        if voice_changed:
            save_voice_settings(
                provider=provider,
                gemini_voice=gemini_voice,
                openai_voice=openai_voice,
            )

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                shown = (
                    "Sol · OpenAI (Shimmer)"
                    if provider == "openai" and openai_voice == "shimmer"
                    else (openai_voice.title() if provider == "openai" else gemini_voice)
                )
                self._log.append_log(
                    f"SYS: Voice engine set — {provider.title()} · {shown}"
                )
                if provider == "openai" and not get_openai_key():
                    self._log.append_log(
                        "ERR: Add your OpenAI key in API Keys before using this voice."
                    )
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Optional API keys ────────────────────────────────────────────────────

    def _open_api_keys(self):
        ov = ApiKeysOverlay(parent=self.centralWidget())
        ov.saved.connect(self._on_api_keys_saved)
        self._centre_overlay(ov)
        self._api_keys_overlay = ov         # keep a reference so it isn't GC'd

    def _on_api_keys_saved(self, changed: list, odd: list):
        for label in odd:
            self._log.append_log(f"WARN: {label} — unusual format, saved anyway.")
        if changed:
            # No reconnect: these keys are read from disk on each use, unlike
            # the voice, which is fixed when the Live session opens.
            self._log.append_log(f"SYS: API keys updated — {', '.join(changed)}")

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _open_vision_source_selector(self) -> None:
        try:
            from actions.screen_processor import list_capture_sources
            sources = list_capture_sources()
        except Exception as exc:
            self._log.append_log(f"ERR: Screen source list failed — {exc}")
            sources = []
        self._vision_source_labels = {
            str(item.get("id") or ""): str(item.get("label") or "")
            for item in sources
        }
        old = self._vision_source_overlay
        if old is not None:
            old.hide(); old.deleteLater()
        overlay = VisionSourceOverlay(sources, parent=self.centralWidget())
        overlay.selected.connect(self._on_vision_source_selected)
        self._vision_source_overlay = overlay
        self._centre_overlay(overlay)

    def _on_vision_source_selected(self, source: str, label: str) -> None:
        overlay = self._vision_source_overlay
        if overlay is not None:
            overlay.hide(); overlay.deleteLater()
            self._vision_source_overlay = None
        self._vision_source_labels[source] = label
        # Pressing the enabled SHARE button after choosing a concrete source is
        # the explicit opt-in. Keep the separate consent panel for camera,
        # whose button does not pass through this picker.
        from memory.config_manager import get_vision_consent, save_vision_consent
        if not get_vision_consent():
            save_vision_consent(True)
        self._begin_vision_request(source)
        if source.startswith("window:"):
            def _focus_selected():
                try:
                    from actions.screen_processor import focus_capture_source
                    focus_capture_source(source)
                except Exception:
                    pass
            QTimer.singleShot(120, _focus_selected)

    def _request_vision(self, source: str, source_label: str = "") -> None:
        source = "camera" if source == "camera" else str(source or "monitor:1")
        if source_label:
            self._vision_source_labels[source] = source_label
        if self._vision_source == source and self._vision_state in {"starting", "active"}:
            return
        if self._vision_source == source and self._vision_state == "paused":
            self._request_pause_vision(False)
            return

        from memory.config_manager import get_vision_consent
        if not get_vision_consent():
            if self._vision_consent_overlay:
                self._vision_consent_overlay.hide()
            label = self._label_for_vision_source(source)
            ov = VisionConsentOverlay(source, label, parent=self.centralWidget())
            ov.answered.connect(
                lambda accepted, s=source, lbl=label: self._on_vision_consent(
                    s, lbl, accepted
                )
            )
            self._centre_overlay(ov)
            ov.show(); ov.raise_()
            self._vision_consent_overlay = ov
            return

        self._begin_vision_request(source)

    def _on_vision_consent(self, source: str, source_label: str,
                           accepted: bool) -> None:
        if self._vision_consent_overlay:
            self._vision_consent_overlay.hide()
            self._vision_consent_overlay.deleteLater()
            self._vision_consent_overlay = None
        if not accepted:
            self._log.append_log("SYS: Live Vision was not started.")
            return
        from memory.config_manager import save_vision_consent
        save_vision_consent(True)
        if source_label:
            self._vision_source_labels[source] = source_label
        self._begin_vision_request(source)

    def _begin_vision_request(self, source: str) -> None:
        if not self.on_vision_requested:
            self._apply_vision_state("", "error", "VOICE SESSION OFFLINE")
            self._log.append_log("ERR: Live Vision is unavailable until voice connects.")
            return
        self._apply_vision_state(source, "starting", "CONNECTING…")
        self.on_vision_requested(source)

    def _request_pause_vision(self, paused: bool) -> None:
        if self.on_vision_pause:
            self.on_vision_pause(bool(paused))
        elif self._vision_source:
            state = "paused" if paused else "starting"
            detail = "VISION PAUSED · VOICE ACTIVE" if paused else "RESUMING VISION…"
            self._apply_vision_state(self._vision_source, state, detail)

    def _request_stop_vision(self) -> None:
        if self.on_vision_stop:
            self.on_vision_stop()
        else:
            self.stop_camera_stream()
            self._apply_vision_state("", "off", "VISION OFF")

    def _apply_vision_state(self, source: str, state: str, detail: str) -> None:
        self._vision_source = str(source or "")
        self._vision_state = (
            state if state in {"off", "starting", "active", "paused", "error"}
            else "off"
        )
        self._vision_detail = detail or ""
        self._style_vision_controls()

    def _label_for_vision_source(self, source: str) -> str:
        if source == "camera":
            return "Camera"
        known = self._vision_source_labels.get(source)
        if known:
            return known
        try:
            from actions.screen_processor import describe_capture_source
            known = describe_capture_source(source)
        except Exception:
            known = "Selected window" if source.startswith("window:") else "Display"
        self._vision_source_labels[source] = known
        return known

    def _apply_vision_highlight(self, source: str, x: int, y: int,
                                label: str) -> None:
        if source != self._vision_source or source == "camera":
            return
        try:
            from actions.screen_processor import capture_source_geometry
            geometry = capture_source_geometry(source)
            self._vision_highlight.show_highlight(geometry, x, y, label)
        except Exception as exc:
            self._log.append_log(f"ERR: Vision highlight failed — {exc}")

    def _style_vision_controls(self) -> None:
        if not hasattr(self, "_vision_screen_btn"):
            return
        active = self._vision_state in {"starting", "active", "paused"}
        selected = self._vision_source if active else ""
        selected_kind = _vision_source_kind(selected) if selected else ""
        base = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM};
                background: {C.PRI_GHO}; }}
            QPushButton:checked {{ color: {C.WHITE}; border-color: {C.PRI};
                background: {C.PRI_DIM}; }}
        """
        for button, source_kind in (
            (self._vision_screen_btn, "monitor"),
            (self._vision_camera_btn, "camera"),
        ):
            is_selected = (
                selected_kind == source_kind
                or (source_kind == "monitor" and selected_kind == "window")
            )
            button.setChecked(is_selected)
            button.setStyleSheet(base)
            icon_color = C.WHITE if is_selected else C.TEXT_MED
            button.setIcon(_vision_icon(source_kind, icon_color, 20))
            button.setIconSize(QSize(20, 20))

        self._vision_stop_btn.setVisible(active)
        self._vision_stop_btn.setStyleSheet(f"""
            QPushButton {{ background: #180008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 4px; }}
            QPushButton:hover {{ background: #26000c; color: {C.WHITE}; }}
        """)

        if self._vision_state == "active":
            label = self._vision_detail or (
                "SHARING CAMERA" if selected_kind == "camera" else "SHARING SOURCE"
            )
            color = C.GREEN
            icon_kind = selected_kind or "glasses"
        elif self._vision_state == "paused":
            label = self._vision_detail or "VISION PAUSED · VOICE ACTIVE"
            color = C.ACC2
            icon_kind = "pause"
        elif self._vision_state == "starting":
            label = self._vision_detail or "CONNECTING…"
            color = C.ACC2
            icon_kind = selected_kind or "glasses"
        elif self._vision_state == "error":
            label = self._vision_detail or "VISION ERROR"
            color = C.MUTED_C
            icon_kind = "glasses"
        else:
            label = "VISION OFF"
            color = C.TEXT_DIM
            icon_kind = "glasses"

        self._vision_status_icon.setPixmap(
            _vision_icon(icon_kind, color, 18).pixmap(18, 18)
        )
        self._vision_status_lbl.setText(label.upper()[:34])
        self._vision_status_lbl.setToolTip(self._vision_detail)
        self._vision_status_lbl.setStyleSheet(
            f"color: {color}; background: transparent; border: none;"
        )
        if active and hasattr(self, "_vision_bar"):
            source_label = self._label_for_vision_source(selected)
            self._vision_bar.update_state(
                selected, source_label, self._vision_state, self._vision_detail
            )
            self._vision_bar.update_mic(
                self._muted, self._busy, self._busy_mic_open
            )
            self._vision_border.track(selected, self._vision_state)
        elif hasattr(self, "_vision_bar"):
            self._vision_bar.hide()
            self._vision_border.stop()
            self._vision_highlight.hide()

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _apply_busy(self, reason: str, mic_open: bool):
        """Slot — she started or finished doing something, and may be deaf for it.

        The user could never tell whether she was listening, so every silence
        read as a failure. The button now names what she is doing and whether
        the microphone is live through it, which is not the same question: she
        is deaf while she speaks through the speakers, but not while she waits
        on another assistant, and not while she speaks on headphones. Green
        means she is hearing you, and it means it.
        """
        if (reason, mic_open) == (self._busy, self._busy_mic_open):
            return
        self._busy = reason
        self._busy_mic_open = mic_open
        self._style_mute_btn()
        if self._muted:
            return                      # the user's own mute outranks this
        if reason == "SPEAKING":
            self._apply_state("SPEAKING")
        elif reason:
            self._apply_state("WORKING")
        else:
            self._apply_state("LISTENING")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        elif self._busy:
            doing = "SPEAKING" if self._busy == "SPEAKING" else "WORKING"
            if self._busy_mic_open:
                # Busy, but the words still reach her — worth saying, because
                # the whole point is knowing when it is worth talking.
                self._mute_btn.setText(f"🎙  {doing} — STILL LISTENING")
                self._mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #00140a; color: {C.GREEN_D};
                        border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    }}
                """)
            else:
                self._mute_btn.setText(f"🔇  {doing} — MIC OFF")
                self._mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #140f00; color: {C.ACC2};
                        border: 1px solid {C.ACC2}; border-radius: 3px;
                    }}
                """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)
        if hasattr(self, "_vision_bar"):
            self._vision_bar.update_mic(
                self._muted, self._busy, self._busy_mic_open
            )

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def closeEvent(self, event):
        self.stop_camera_stream()
        for widget_name in ("_vision_bar", "_vision_border", "_vision_highlight"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.close()
        super().closeEvent(event)

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({
                "gemini_api_key": key,
                "os_system": os_name,
                "assistant_name": APP_NAME,
                "ui_color": DEFAULT_UI_COLOR,
                "night_mode": False,
            }, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", APP_NAME) or APP_NAME
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        if APP_ICON_ICO.exists():
            self._app.setWindowIcon(QIcon(str(APP_ICON_ICO)))
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_learning_english(self):
        return self._win.on_learning_english

    @on_learning_english.setter
    def on_learning_english(self, cb):
        self._win.on_learning_english = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    @property
    def on_vision_requested(self):
        return self._win.on_vision_requested

    @on_vision_requested.setter
    def on_vision_requested(self, cb):
        self._win.on_vision_requested = cb

    @property
    def on_vision_stop(self):
        return self._win.on_vision_stop

    @on_vision_stop.setter
    def on_vision_stop(self, cb):
        self._win.on_vision_stop = cb

    @property
    def on_vision_pause(self):
        return self._win.on_vision_pause

    @on_vision_pause.setter
    def on_vision_pause(self, cb):
        self._win.on_vision_pause = cb

    @property
    def on_camera_frame(self):
        return self._win.on_camera_frame

    @on_camera_frame.setter
    def on_camera_frame(self, cb):
        self._win.on_camera_frame = cb

    @property
    def on_camera_error(self):
        return self._win.on_camera_error

    @on_camera_error.setter
    def on_camera_error(self, cb):
        self._win.on_camera_error = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    def set_busy(self, reason: str = "", mic_open: bool = True) -> None:
        """Thread-safe: say why the app itself has the microphone shut.

        "SPEAKING" while her own voice is going out, "WORKING" while she is
        waiting on another assistant, "" when she is listening. The button
        turns amber and says which, so green comes to mean she really is
        hearing you rather than probably hearing you.
        """
        try:
            self._win._busy_sig.emit(reason or "", bool(mic_open))
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def set_live_transcript(self, text: str):
        """Thread-safe: show the user's speech as it is being transcribed.

        Pass "" to take the provisional line down. Writing a real log line
        clears it too, so the finished turn replaces the preview by itself."""
        self._win._live_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    def set_vision_state(self, source: str = "", state: str = "off",
                         detail: str = "") -> None:
        """Thread-safe: update the persistent Live Vision controls."""
        self._win._vision_state_sig.emit(source or "", state or "off", detail or "")

    def show_vision_highlight(self, source: str, x: int, y: int,
                              label: str = "") -> None:
        """Thread-safe: point at normalized coordinates on the shared source."""
        self._win._vision_highlight_sig.emit(
            str(source or ""), int(x), int(y), str(label or "")
        )

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
