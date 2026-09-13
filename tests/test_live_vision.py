import asyncio
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
import memory.config_manager as config_manager
import actions.screen_processor as screen_processor
from PyQt6.QtWidgets import QApplication
from ui import MainWindow, VisionCaptureBorderOverlay, VisionHighlightOverlay


class _FakeUI:
    def __init__(self):
        self.states = []
        self.logs = []
        self.camera_starts = 0
        self.camera_stops = 0
        self.highlights = []
        self.muted = False

    def set_vision_state(self, *state):
        self.states.append(state)

    def write_log(self, line):
        self.logs.append(line)

    def start_camera_stream(self):
        self.camera_starts += 1

    def stop_camera_stream(self):
        self.camera_stops += 1

    def show_vision_highlight(self, *args):
        self.highlights.append(args)

    def set_state(self, *_args):
        pass


class _FakeSession:
    def __init__(self):
        self.frames = []
        self.turns = []

    async def send_realtime_input(self, **kwargs):
        self.frames.append((time.monotonic(), kwargs))

    async def send_client_content(self, **kwargs):
        self.turns.append(kwargs)


def _controller(source=""):
    obj = main.JarvisLive.__new__(main.JarvisLive)
    obj.ui = _FakeUI()
    obj.session = _FakeSession()
    obj._loop = None
    obj._live_vision_source = source
    obj._live_vision_epoch = 1
    obj._live_vision_latest = None
    obj._live_vision_announced = False
    obj._live_vision_should_greet = False
    obj._live_vision_frames = 0
    obj._live_vision_paused = False
    obj._live_vision_fingerprint = None
    obj._live_vision_last_sent_at = 0.0
    obj._live_vision_skipped = 0
    obj._live_vision_label = ""
    return obj


class LiveVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_highlight_maps_to_active_source(self):
        controller = _controller("window:77")
        fc = type("FC", (), {
            "name": "vision_highlight", "id": "tool-1",
            "args": {"x": 1200, "y": -5, "label": "Open settings"},
        })()
        response = await controller._dispatch_tool(fc)
        self.assertEqual(
            controller.ui.highlights,
            [("window:77", 1000, 0, "Open settings")],
        )
        self.assertIn("Pointer shown", response.response["result"])

    async def test_screen_stream_is_rate_limited_and_stop_is_immediate(self):
        controller = _controller("screen")
        controller._live_vision_should_greet = True
        original_capture = main._capture_screen
        original_fingerprint = main.frame_fingerprint
        original_change = main.frame_change_score
        main._capture_screen = lambda *_args: (b"jpeg-frame", "image/jpeg")
        main.frame_fingerprint = lambda data: data
        main.frame_change_score = lambda _old, _new: 1.0
        task = asyncio.create_task(controller._run_live_vision())
        try:
            deadline = time.monotonic() + 3.0
            while len(controller.session.frames) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

            self.assertEqual(len(controller.session.frames), 2)
            spacing = controller.session.frames[1][0] - controller.session.frames[0][0]
            self.assertGreaterEqual(spacing, 0.95)
            self.assertEqual(
                controller.session.frames[0][1]["video"].data,
                b"jpeg-frame",
            )
            self.assertEqual(len(controller.session.turns), 1)
            self.assertEqual(controller.ui.states[-1][:2], ("screen", "active"))

            controller._deactivate_live_vision()
            sent_at_stop = len(controller.session.frames)
            await asyncio.sleep(0.15)
            self.assertEqual(len(controller.session.frames), sent_at_stop)
            self.assertEqual(controller.ui.states[-1][1], "off")
        finally:
            main._capture_screen = original_capture
            main.frame_fingerprint = original_fingerprint
            main.frame_change_score = original_change
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_camera_uses_only_the_latest_frame(self):
        controller = _controller("camera")
        controller._on_live_camera_frame(b"first")
        controller._on_live_camera_frame(b"newest")
        original_fingerprint = main.frame_fingerprint
        main.frame_fingerprint = lambda data: data
        task = asyncio.create_task(controller._run_live_vision())
        try:
            deadline = time.monotonic() + 1.0
            while not controller.session.frames and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(
                controller.session.frames[0][1]["video"].data,
                b"newest",
            )
        finally:
            main.frame_fingerprint = original_fingerprint
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_source_switch_invalidates_a_capture_in_flight(self):
        controller = _controller("screen")

        async def delayed_capture():
            await asyncio.sleep(0.08)
            return b"old-screen", "image/jpeg"

        original_to_thread = main.asyncio.to_thread
        main.asyncio.to_thread = lambda *_args, **_kwargs: delayed_capture()
        task = asyncio.create_task(controller._run_live_vision())
        try:
            await asyncio.sleep(0.02)
            controller._activate_live_vision("camera", greet=False)
            await asyncio.sleep(0.12)
            self.assertEqual(controller.session.frames, [])
            self.assertEqual(controller._live_vision_source, "camera")
            self.assertEqual(controller.ui.camera_starts, 1)
        finally:
            main.asyncio.to_thread = original_to_thread
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_smart_capture_skips_static_frames(self):
        controller = _controller("monitor:1")
        original_capture = main._capture_screen
        original_fingerprint = main.frame_fingerprint
        main._capture_screen = lambda *_args: (b"same", "image/jpeg")
        main.frame_fingerprint = lambda data: data
        task = asyncio.create_task(controller._run_live_vision())
        try:
            await asyncio.sleep(2.2)
            self.assertEqual(len(controller.session.frames), 1)
            self.assertGreaterEqual(controller._live_vision_skipped, 1)
        finally:
            main._capture_screen = original_capture
            main.frame_fingerprint = original_fingerprint
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_pause_stops_camera_and_resume_restarts_it(self):
        controller = _controller("camera")
        controller._set_live_vision_paused(True)
        self.assertTrue(controller._live_vision_paused)
        self.assertEqual(controller.ui.camera_stops, 1)
        self.assertEqual(controller.ui.states[-1][1], "paused")
        controller._set_live_vision_paused(False)
        self.assertFalse(controller._live_vision_paused)
        self.assertEqual(controller.ui.camera_starts, 1)
        self.assertEqual(controller.ui.states[-1][1], "starting")


class LiveVisionThreadEntrypointTests(unittest.TestCase):
    def test_offline_start_reports_a_clear_error(self):
        controller = _controller()
        controller.session = None
        controller.start_live_vision("screen")
        self.assertEqual(
            controller.ui.states[-1],
            ("", "error", "VOICE SESSION OFFLINE"),
        )

    def test_camera_callback_is_ignored_when_camera_is_not_shared(self):
        controller = _controller("screen")
        controller._on_live_camera_frame(b"private-frame")
        self.assertIsNone(controller._live_vision_latest)


class LiveVisionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_source_icons_status_and_stop_control_are_visible(self):
        original = config_manager.get_vision_consent
        config_manager.get_vision_consent = lambda: True
        window = MainWindow("face.png")
        starts = []
        stops = []
        pauses = []
        window.on_vision_requested = starts.append
        window.on_vision_stop = lambda: stops.append(True)
        window.on_vision_pause = pauses.append
        try:
            window.show()
            self.app.processEvents()
            self.assertFalse(window._vision_screen_btn.icon().isNull())
            self.assertFalse(window._vision_camera_btn.icon().isNull())

            window._vision_screen_btn.click()
            self.app.processEvents()
            picker = window._vision_source_overlay
            self.assertIsNotNone(picker)
            self.assertTrue(picker.isVisible())
            self.assertIn("monitor:1", picker._choices)
            picker._choose("monitor:1", "Display 1")
            picker._share.click()
            self.app.processEvents()
            self.assertEqual(starts, ["monitor:1"])
            self.assertTrue(window._vision_screen_btn.isChecked())
            self.assertTrue(window._vision_stop_btn.isVisible())

            window._apply_vision_state(
                "monitor:1", "active", "SHARING DISPLAY · SMART · VOICE ACTIVE"
            )
            self.assertTrue(window._vision_status_lbl.text().startswith("SHARING DISPLAY"))
            self.assertTrue(window._vision_bar.isVisible())
            self.assertEqual(window._vision_border._source_id, "monitor:1")
            window._vision_bar._pause.click()
            self.assertEqual(pauses, [True])
            window._vision_stop_btn.click()
            self.assertEqual(stops, [True])
        finally:
            window.close()
            self.app.processEvents()
            config_manager.get_vision_consent = original

    def test_first_use_requires_explicit_consent(self):
        old_get = config_manager.get_vision_consent
        old_save = config_manager.save_vision_consent
        saved = []
        config_manager.get_vision_consent = lambda: False
        config_manager.save_vision_consent = saved.append
        window = MainWindow("face.png")
        starts = []
        window.on_vision_requested = starts.append
        try:
            window.show()
            self.app.processEvents()
            window._vision_camera_btn.click()
            self.app.processEvents()
            overlay = window._vision_consent_overlay
            self.assertIsNotNone(overlay)
            self.assertTrue(overlay.isVisible())
            self.assertEqual(starts, [])

            overlay.answered.emit(True)
            self.app.processEvents()
            self.assertEqual(saved, [True])
            self.assertEqual(starts, ["camera"])
        finally:
            window.close()
            self.app.processEvents()
            config_manager.get_vision_consent = old_get
            config_manager.save_vision_consent = old_save

    def test_source_normalization_and_change_score(self):
        self.assertEqual(screen_processor._normalise_capture_source("screen"), "monitor:1")
        self.assertEqual(
            screen_processor._normalise_capture_source("window:123"), "window:123"
        )
        self.assertEqual(screen_processor.frame_change_score(b"\x00", b"\x00"), 0.0)
        self.assertEqual(screen_processor.frame_change_score(b"\x00", b"\xff"), 1.0)

    def test_yellow_border_and_highlight_follow_normalized_source_geometry(self):
        old_geometry = screen_processor.capture_source_geometry
        screen_processor.capture_source_geometry = lambda _source: {
            "left": 40, "top": 60, "width": 800, "height": 600,
        }
        border = VisionCaptureBorderOverlay()
        highlight = VisionHighlightOverlay()
        try:
            border.track("window:123", "active")
            self.app.processEvents()
            self.assertEqual(border.geometry().getRect(), (40, 60, 800, 600))
            self.assertTrue(border.isVisible())

            highlight.show_highlight(
                {"left": 40, "top": 60, "width": 800, "height": 600},
                250, 750, "Target",
            )
            self.app.processEvents()
            self.assertAlmostEqual(highlight._target.x(), 200.0)
            self.assertAlmostEqual(highlight._target.y(), 450.0)
            self.assertTrue(highlight.isVisible())
        finally:
            border.close(); highlight.close()
            screen_processor.capture_source_geometry = old_geometry


if __name__ == "__main__":
    unittest.main()
