"""Unit tests for the tools' safety fixes: app names never reach a shell, no
generated code runs, credential folders stay out of reach, reminder XML stays
valid, and a brightness change that fails is reported instead of claimed.

Run from the project root: python -m unittest tests.test_actions
"""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest import mock

from actions import (
    computer_control, computer_settings, desktop, dev_agent, file_controller,
    file_processor, flight_finder, open_app, reminder,
)

_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


class OpenAppTests(unittest.TestCase):
    def test_an_app_name_never_reaches_a_shell(self):
        calc = r"C:\Windows\System32\calc.exe"
        with mock.patch.object(open_app.shutil, "which",
                               side_effect=lambda name: calc if name == "calc" else None), \
             mock.patch.object(open_app.subprocess, "Popen") as popen, \
             mock.patch.object(open_app.time, "sleep"):
            self.assertTrue(open_app._launch_windows("calc.exe & del notes.txt"))
        self.assertEqual(popen.call_args.args[0], [calc])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_a_protocol_opens_like_a_double_click_not_through_cmd(self):
        with mock.patch.object(open_app.shutil, "which", return_value=None), \
             mock.patch.object(open_app.os, "startfile", create=True) as startfile, \
             mock.patch.object(open_app.subprocess, "Popen") as popen, \
             mock.patch.object(open_app.time, "sleep"):
            self.assertTrue(open_app._launch_windows("ms-settings: & calc"))
        startfile.assert_called_once_with("ms-settings: & calc")
        popen.assert_not_called()


class OpenVSCodeTests(unittest.TestCase):
    def test_a_missing_launcher_is_skipped_instead_of_reported_as_opened(self):
        installed = r"C:\Program Files\Microsoft VS Code\bin\code.cmd"
        with mock.patch.object(dev_agent.shutil, "which",
                               side_effect=lambda cmd: installed if cmd == installed else None), \
             mock.patch.object(dev_agent.subprocess, "Popen") as popen, \
             mock.patch.object(dev_agent.time, "sleep"):
            self.assertTrue(dev_agent._open_vscode(Path(r"C:\projects\demo")))
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [installed, r"C:\projects\demo"])
        self.assertNotIn("shell", popen.call_args.kwargs)


class DesktopControlTests(unittest.TestCase):
    def test_a_free_form_task_is_refused_rather_than_turned_into_code(self):
        with mock.patch("builtins.exec", side_effect=AssertionError("exec ran")):
            answer = desktop.desktop_control({"action": "task", "task": "delete the old files"})
        self.assertIn("Unknown desktop action 'task'", answer)


class ProtectedHomeTests(unittest.TestCase):
    def test_credential_folders_are_refused_even_through_dot_dot(self):
        home = Path.home()
        self.assertTrue(file_controller._is_safe_path(home))
        self.assertTrue(file_controller._is_safe_path(home / "Desktop" / "notes.txt"))
        self.assertFalse(file_controller._is_safe_path(home / ".ssh" / "id_ed25519"))
        self.assertFalse(file_controller._is_safe_path(home / "Desktop" / ".." / ".aws" / "credentials"))
        self.assertFalse(file_controller._is_safe_path(home / "AppData" / "Local" / "Google"))
        self.assertFalse(file_controller._is_safe_path(Path(home.anchor) / "Windows"))


class ReminderXmlTests(unittest.TestCase):
    def test_paths_with_xml_characters_still_register(self):
        captured = {}

        def fake_schtasks(cmd, **kwargs):
            captured["xml"] = Path(cmd[cmd.index("/XML") + 1]).read_bytes()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(reminder, "_scripts_dir", return_value=Path(tmp)), \
             mock.patch.object(reminder.subprocess, "run", side_effect=fake_schtasks), \
             mock.patch.object(reminder.sys, "executable", r"C:\Tom & Jerry\python.exe"):
            task = reminder._schedule_windows(
                datetime(2030, 1, 2, 3, 4), "JARVISReminder_test",
                Path(r"C:\Users\A&B <x>\reminder.py"), "hi",
            )
        self.assertEqual(task, "JARVISReminder_test")
        root = ET.fromstring(captured["xml"])
        self.assertEqual(root.find(f".//{_TASK_NS}Command").text, r"C:\Tom & Jerry\python.exe")
        self.assertEqual(root.find(f".//{_TASK_NS}Arguments").text,
                         r'"C:\Users\A&B <x>\reminder.py"')


class BrightnessTests(unittest.TestCase):
    def test_a_display_windows_cannot_dim_is_reported_not_claimed(self):
        with mock.patch.object(computer_settings, "_OS", "Windows"), \
             mock.patch.object(computer_settings.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            with self.assertRaises(RuntimeError):
                computer_settings.brightness_up()

    def test_a_panel_windows_drives_is_still_dimmed(self):
        with mock.patch.object(computer_settings, "_OS", "Windows"), \
             mock.patch.object(computer_settings.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            computer_settings.brightness_down()
        self.assertIn(".WmiSetBrightness(1, [math]::Max(0, ", run.call_args.args[0][2])


class FlightsUrlTests(unittest.TestCase):
    def test_the_search_is_not_replaced_by_a_fixed_itinerary(self):
        url = flight_finder._build_google_flights_url("Bogota", "Madrid", "2026-12-01")
        self.assertNotIn("tfs=", url)
        self.assertIn("q=Flights+from+Bogota+to+Madrid+on+2026-12-01", url)


class FileSizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_audio_over_the_inline_limit_is_refused_before_it_is_read(self):
        path = self.folder / "interview.mp3"
        path.write_bytes(b"\0" * 16)
        with mock.patch.object(file_processor, "_INLINE_LIMIT_BYTES", 8), \
             mock.patch.object(file_processor, "_gemini_client") as client:
            answer = file_processor._process_audio(path, "transcribe", {})
        client.assert_not_called()
        self.assertIn("20 MB", answer)

    def test_text_over_the_limit_is_refused(self):
        path = self.folder / "notes.txt"
        path.write_text("hello world", encoding="utf-8")
        with mock.patch.object(file_processor, "_TEXT_LIMIT_BYTES", 4):
            answer = file_processor.file_processor({"file_path": str(path), "action": "summarize"})
        self.assertIn("too large to read as text", answer)

    def test_an_unknown_file_is_previewed_without_reading_all_of_it(self):
        path = self.folder / "export.lumdata"
        path.write_text("a" * 20000, encoding="utf-8")
        model = mock.Mock()
        model.generate_content.return_value = mock.Mock(text="An export.")
        with mock.patch.object(file_processor, "_gemini_client", return_value=model), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("read the whole file")):
            answer = file_processor.file_processor({"file_path": str(path)})
        self.assertEqual(answer, "An export.")
        prompt = model.generate_content.call_args.args[0]
        self.assertIn("a" * 10000, prompt)
        self.assertNotIn("a" * 10001, prompt)


class GeneratedPasswordTests(unittest.TestCase):
    def test_a_form_password_does_not_come_from_the_predictable_generator(self):
        guard = mock.Mock(side_effect=AssertionError("random module used"))
        with mock.patch.object(computer_control.random, "choice", guard), \
             mock.patch.object(computer_control.random, "choices", guard), \
             mock.patch.object(computer_control.random, "sample", guard):
            password = computer_control._random_data("password")
        self.assertEqual(len(password), 12)
        self.assertTrue(any(c.isupper() for c in password))
        self.assertTrue(any(c.isdigit() for c in password))
        self.assertTrue(any(c in "!@#$%" for c in password))


if __name__ == "__main__":
    unittest.main()
