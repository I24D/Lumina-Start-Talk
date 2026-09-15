"""Unit tests for how Lumina talks to Gemini 3.1 Flash Live, and that the OpenAI
session keeps the calls it had.

Run from the project root: python -m unittest tests.test_gemini_live
"""

import asyncio
import contextlib
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from core.openai_realtime import OpenAIRealtimeSession


class _GeminiSession:
    def __init__(self):
        self.calls = []

    async def send_realtime_input(self, **kwargs):
        self.calls.append(("realtime", kwargs))

    async def send_client_content(self, **kwargs):
        self.calls.append(("client_content", kwargs))


def _openai_session():
    session = OpenAIRealtimeSession.__new__(OpenAIRealtimeSession)
    session.send_realtime_input = mock.AsyncMock()
    session.send_client_content = mock.AsyncMock()
    return session


def _controller(session):
    obj = main.JarvisLive.__new__(main.JarvisLive)
    obj.session = session
    obj.out_queue = asyncio.Queue()
    obj._sent_blocks = 0
    obj._input_sample_rate = main.SEND_SAMPLE_RATE
    return obj


async def _send_one_block(controller, msg):
    await controller.out_queue.put(msg)
    task = asyncio.create_task(controller._send_realtime())
    deadline = time.monotonic() + 2.0
    while controller._sent_blocks == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class GeminiLiveTests(unittest.IsolatedAsyncioTestCase):
    def test_the_assistant_voice_runs_on_3_1_flash_live(self):
        self.assertEqual(main.LIVE_MODEL, "models/gemini-3.1-flash-live-preview")

    async def test_microphone_audio_reaches_gemini_as_audio(self):
        session = _GeminiSession()
        block = {"data": b"\x01\x00" * 8, "mime_type": "audio/pcm;rate=16000"}
        await _send_one_block(_controller(session), block)
        self.assertEqual(len(session.calls), 1)
        kind, kwargs = session.calls[0]
        self.assertEqual((kind, set(kwargs)), ("realtime", {"audio"}))
        self.assertEqual(kwargs["audio"].data, block["data"])
        self.assertEqual(kwargs["audio"].mime_type, "audio/pcm;rate=16000")

    async def test_openai_keeps_its_media_entry(self):
        session = _openai_session()
        block = {"data": b"\x01\x00" * 8, "mime_type": "audio/pcm;rate=24000"}
        await _send_one_block(_controller(session), block)
        session.send_realtime_input.assert_awaited_once_with(media=block)

    async def test_a_text_turn_reaches_gemini_as_realtime_text(self):
        session = _GeminiSession()
        await _controller(session)._send_text_turn("Hola")
        self.assertEqual(session.calls, [("realtime", {"text": "Hola"})])

    async def test_a_text_turn_to_openai_is_unchanged(self):
        session = _openai_session()
        await _controller(session)._send_text_turn("Hola")
        session.send_client_content.assert_awaited_once_with(
            turns={"parts": [{"text": "Hola"}]}, turn_complete=True,
        )
        session.send_realtime_input.assert_not_called()


if __name__ == "__main__":
    unittest.main()
