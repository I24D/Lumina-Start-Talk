from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
import numpy as np

import core.openai_realtime as realtime
import learning_english.service as learning_service_module
import main
from core.echo_canceller import EchoCanceller
from core.openai_realtime import (
    OpenAIRealtimeSession, _openai_tools, create_call, session_config,
)
from learning_english.service import LearningEnglishService, OPENAI_TEXT_MODEL


def _sent(session):
    """Everything the session has queued for the socket, in order."""
    events = []
    while not session._outbox.empty():
        events.append(json.loads(session._outbox.get_nowait()))
    return events


def _types(events):
    return [event["type"] for event in events]


def _audio_delta(item, seconds):
    pcm = bytes(int(24000 * seconds) * 2)
    return {"type": "response.output_audio.delta", "item_id": item,
            "delta": base64.b64encode(pcm).decode("ascii")}


def _collect(session, events=(), *, seconds=0.25, later=(), later_after=0.25):
    """What receive() yields for the given events, and for more sent later."""
    async def exercise():
        items = []

        async def reader():
            async for item in session.receive():
                items.append(item)

        for event in events:
            session._handle_event(event)
        task = asyncio.create_task(reader())
        await asyncio.sleep(seconds)
        before = list(items)
        for event in later:
            session._handle_event(event)
        await asyncio.sleep(later_after if later else 0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task
        return before, items

    return asyncio.run(exercise())


def _kinds(items):
    kinds = []
    for item in items:
        sc = item.server_content
        if item.data:
            kinds.append("audio")
        elif item.tool_call:
            kinds.append("tool")
        elif sc is None:
            kinds.append("other")
        elif sc.turn_complete:
            kinds.append("turn")
        elif sc.interrupted:
            kinds.append("interrupted")
        elif sc.input_transcription:
            kinds.append("heard")
        elif sc.interim_input_transcription:
            kinds.append("interim")
        elif sc.output_transcription:
            kinds.append("said")
    return kinds


class _Socket:
    def __init__(self, incoming=()):
        self.incoming = [json.dumps(event) for event in incoming]
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return self.incoming.pop(0)


class OpenAIRealtimeConfigTests(unittest.TestCase):
    def test_tools_are_converted_from_gemini_to_openai_schema(self):
        tools = _openai_tools([{
            "name": "weather_report",
            "description": "Weather",
            "parameters": {
                "type": "OBJECT",
                "properties": {"city": {"type": "STRING"}},
                "required": ["city"],
            },
        }])
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["parameters"]["type"], "object")
        self.assertEqual(tools[0]["parameters"]["properties"]["city"]["type"], "string")

    def test_session_transcribes_detects_turns_and_lets_the_user_interrupt(self):
        config = session_config(
            instructions="Be brief", voice="shimmer",
            tools=[{"name": "undo", "parameters": {"type": "OBJECT", "properties": {}}}],
        )
        audio_in = config["audio"]["input"]
        self.assertEqual(audio_in["transcription"]["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(audio_in["turn_detection"]["type"], "semantic_vad")
        self.assertTrue(audio_in["turn_detection"]["interrupt_response"])
        self.assertEqual(config["audio"]["output"]["voice"], "shimmer")
        self.assertEqual(config["tools"][0]["name"], "undo")
        self.assertNotIn("format", audio_in)

    def test_the_socket_names_its_pcm_rate_both_ways(self):
        config = session_config(instructions="x", voice="shimmer", pcm_rate=24000)
        for side in ("input", "output"):
            self.assertEqual(config["audio"][side]["format"], {"type": "audio/pcm", "rate": 24000})

    def test_turns_wait_through_pauses_and_the_name_is_heard(self):
        audio_in = session_config(
            instructions="x", voice="shimmer", assistant_name="Lumina",
        )["audio"]["input"]
        self.assertEqual(audio_in["turn_detection"]["eagerness"], "low")
        self.assertIn("Lumina", audio_in["transcription"]["prompt"])
        self.assertNotIn("prompt", session_config(instructions="x", voice="shimmer")["audio"]["input"]["transcription"])

    def test_connected_means_the_server_accepted_the_session(self):
        socket = _Socket([{"type": "session.created"}, {"type": "session.updated"}])
        session = OpenAIRealtimeSession(socket, model="test")
        asyncio.run(session._configure({"type": "realtime"}))
        self.assertEqual(socket.sent[0]["type"], "session.update")

        rejected = OpenAIRealtimeSession(_Socket([{"type": "error", "error": {
            "code": "invalid_value", "message": "Unknown voice",
        }}]), model="test")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(rejected._configure({"type": "realtime"}))
        self.assertIn("Unknown voice", str(caught.exception))

    def test_the_browser_offer_travels_as_multipart_sdp_and_session(self):
        seen = {}

        def handler(request):
            seen["type"] = request.headers["content-type"]
            seen["body"] = request.read()
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(201, text="answer-sdp")

        answer = create_call(
            "sk-test", "offer-sdp", {"type": "realtime"},
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(answer, "answer-sdp")
        self.assertTrue(seen["type"].startswith("multipart/form-data"))
        self.assertIn(b'name="sdp"', seen["body"])
        self.assertIn(b'name="session"', seen["body"])
        self.assertEqual(seen["auth"], "Bearer sk-test")

    def test_a_rejected_key_names_the_error(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            401, json={"error": {"code": "invalid_api_key", "message": "Incorrect API key"}},
        ))
        with self.assertRaises(RuntimeError) as caught:
            create_call("sk-bad", "offer", {}, transport=transport)
        self.assertIn("invalid_api_key", str(caught.exception))


class OpenAIRealtimeSessionTests(unittest.TestCase):
    def test_microphone_audio_is_appended_as_base64_pcm(self):
        session = OpenAIRealtimeSession(model="test")
        asyncio.run(session.send_realtime_input(media={"data": b"\x01\x00" * 4, "mime_type": "audio/pcm"}))
        events = _sent(session)
        self.assertEqual(_types(events), ["input_audio_buffer.append"])
        self.assertEqual(base64.b64decode(events[0]["audio"]), b"\x01\x00" * 4)

    def test_her_voice_arrives_as_audio(self):
        session = OpenAIRealtimeSession(model="test")
        _, items = _collect(session, [_audio_delta("a1", 0.1)])
        self.assertEqual(_kinds(items), ["audio"])
        self.assertEqual(len(items[0].data), 4800)

    def test_a_response_cancelled_before_speaking_is_not_a_turn(self):
        session = OpenAIRealtimeSession(model="test")
        _, items = _collect(session, [
            {"type": "response.created"},
            {"type": "response.done", "response": {"status": "cancelled", "output": []}},
        ])
        self.assertNotIn("turn", _kinds(items))

    def test_a_turn_waits_for_the_users_transcription(self):
        session = OpenAIRealtimeSession(model="test")
        before, after = _collect(
            session,
            [
                {"type": "input_audio_buffer.committed", "item_id": "u1"},
                {"type": "response.done", "response": {"status": "completed", "output": [{"type": "message"}]}},
            ],
            later=[{
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "u1", "transcript": "qué hora es",
            }],
        )
        self.assertNotIn("turn", _kinds(before))
        kinds = _kinds(after)
        self.assertLess(kinds.index("heard"), kinds.index("turn"))

    def test_transcription_deltas_accumulate_per_item(self):
        session = OpenAIRealtimeSession(model="test")
        _, items = _collect(session, [
            {"type": "conversation.item.input_audio_transcription.delta", "item_id": "u1", "delta": "hola "},
            {"type": "conversation.item.input_audio_transcription.delta", "item_id": "u1", "delta": "Lumina"},
        ])
        interim = [i.server_content.interim_input_transcription.text for i in items
                   if i.server_content and i.server_content.interim_input_transcription]
        self.assertEqual(interim, ["hola ", "hola Lumina"])

    def test_a_function_call_is_dispatched_once_before_the_response_ends(self):
        session = OpenAIRealtimeSession(model="test")
        call = {"type": "function_call", "call_id": "call-1", "name": "weather_report",
                "arguments": "{\"city\":\"Miami\"}"}
        _, items = _collect(session, [
            {"type": "response.output_item.done", "item": call},
            {"type": "response.done", "response": {"status": "completed", "output": [call]}},
        ])
        tools = [i.tool_call for i in items if i.tool_call]
        self.assertEqual(len(tools), 1)
        fc = tools[0].function_calls[0]
        self.assertEqual((fc.id, fc.name, fc.args["city"]), ("call-1", "weather_report", "Miami"))

    def test_a_response_request_waits_for_the_active_response(self):
        session = OpenAIRealtimeSession(model="test")
        session._handle_event({"type": "response.created"})
        asyncio.run(session.send_client_content(
            turns={"parts": [{"text": "[SYSTEM_ALERT] hola"}]}, turn_complete=True,
        ))
        self.assertEqual(_types(_sent(session)), ["conversation.item.create"])
        session._handle_event({"type": "response.done", "response": {"status": "completed", "output": []}})
        self.assertEqual(_types(_sent(session)), ["response.create"])

    def test_silent_tool_results_ask_for_no_answer(self):
        session = OpenAIRealtimeSession(model="test")
        silent = SimpleNamespace(id="c1", response={"result": "ok", "silent": True})
        asyncio.run(session.send_tool_response(function_responses=[silent]))
        self.assertEqual(_types(_sent(session)), ["conversation.item.create"])

        spoken = SimpleNamespace(id="c2", response={"result": "Sunny"})
        asyncio.run(session.send_tool_response(function_responses=[spoken]))
        events = _sent(session)
        self.assertEqual(_types(events), ["conversation.item.create", "response.create"])
        self.assertEqual(events[0]["item"]["call_id"], "c2")

    def test_live_vision_keeps_only_the_newest_frame(self):
        session = OpenAIRealtimeSession(model="test")
        frame = SimpleNamespace(data=b"jpeg", mime_type="image/jpeg")
        asyncio.run(session.send_realtime_input(video=frame))
        asyncio.run(session.send_realtime_input(video=frame))
        events = _sent(session)
        self.assertEqual(_types(events), [
            "conversation.item.create", "conversation.item.create", "conversation.item.delete",
        ])
        self.assertEqual(events[2]["item_id"], events[0]["item"]["id"])
        self.assertTrue(events[1]["item"]["content"][0]["image_url"].startswith("data:image/jpeg;base64,"))

    def test_barge_in_truncates_what_she_had_not_said_yet(self):
        session = OpenAIRealtimeSession(model="test")
        session._handle_event(_audio_delta("a1", 3.0))
        session._audio_started_at = time.monotonic() - 1.4    # 1.0 s heard after the 0.4 s delay
        _, items = _collect(session, [{"type": "input_audio_buffer.speech_started"}])
        self.assertIn("interrupted", _kinds(items))
        truncate = [e for e in _sent(session) if e["type"] == "conversation.item.truncate"]
        self.assertEqual(len(truncate), 1)
        self.assertEqual((truncate[0]["item_id"], truncate[0]["content_index"]), ("a1", 0))
        self.assertAlmostEqual(truncate[0]["audio_end_ms"], 1000, delta=150)

    def test_speaking_after_she_finished_truncates_nothing(self):
        session = OpenAIRealtimeSession(model="test")
        session._handle_event(_audio_delta("a1", 0.5))
        session._audio_started_at = time.monotonic() - 5.0
        session._handle_event({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(_sent(session), [])

    def test_the_interrupt_button_cancels_and_truncates(self):
        session = OpenAIRealtimeSession(model="test")
        session._handle_event(_audio_delta("a1", 3.0))
        session._audio_started_at = time.monotonic()
        asyncio.run(session.cancel_response())
        self.assertEqual(_types(_sent(session)), ["conversation.item.truncate", "response.cancel"])

    def test_only_fatal_errors_end_the_session(self):
        session = OpenAIRealtimeSession(model="test")
        with contextlib.redirect_stdout(io.StringIO()):
            session._handle_event({"type": "error", "error": {
                "code": "conversation_already_has_active_response", "message": "busy",
            }})
        self.assertEqual(session._failure, "")

        session._handle_event({"type": "error", "error": {
            "code": "invalid_api_key", "message": "Incorrect API key",
        }})

        async def read():
            async for _ in session.receive():
                pass

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(read())
        self.assertIn("invalid_api_key", str(caught.exception))
        with self.assertRaises(RuntimeError):
            session._send({"type": "response.create"})


class _Frame:
    def __init__(self, data, rate, channels, samples):
        self.raw = bytearray(data)
        self.samples = samples

    @property
    def data(self):
        return memoryview(self.raw)


class _Processor:
    def __init__(self):
        self.reverse, self.capture, self.delays = [], [], []

    def process_reverse_stream(self, frame):
        self.reverse.append(len(frame.raw))

    def process_stream(self, frame):
        self.capture.append(len(frame.raw))
        frame.raw[:] = bytes(len(frame.raw))

    def set_stream_delay_ms(self, delay):
        self.delays.append(delay)


class EchoCancellerTests(unittest.TestCase):
    def test_whole_10ms_frames_are_processed_and_the_rest_carried(self):
        processor = _Processor()
        canceller = EchoCanceller(24000, processor, _Frame)
        canceller.set_delay(0.25)

        first = canceller.capture((np.ones(1024, dtype=np.int16) * 500).tobytes())
        second = canceller.capture((np.ones(1024, dtype=np.int16) * 500).tobytes())
        self.assertEqual(len(first), 960 * 2)
        self.assertEqual(len(second), 960 * 2)
        self.assertEqual(len(canceller._capture_rest), (2048 - 1920) * 2)
        self.assertEqual(set(processor.capture), {480})
        self.assertEqual(set(processor.delays), {250})
        self.assertEqual(first, bytes(len(first)))

        canceller.render(bytes(1200 * 2))
        self.assertEqual(processor.reverse, [480] * 5)

    def test_each_answer_opens_the_microphone_only_once_the_canceller_proves_itself(self):
        canceller = EchoCanceller(24000, _Processor(), _Frame)
        block = (np.ones(1024, dtype=np.int16) * 3000).tobytes()
        for _ in range(20):                                   # 0.85 s of her voice
            canceller.capture(block, her_voice=True)
        self.assertFalse(canceller.open)
        for _ in range(10):                                   # past 1 s
            canceller.capture(block, her_voice=True)
        self.assertTrue(canceller.open)
        self.assertLessEqual(canceller.interval_db(), EchoCanceller.OPEN_BELOW_DB)
        self.assertIsNone(canceller.interval_db())

        canceller.capture(block)                              # she has stopped
        self.assertFalse(canceller.open)
        canceller.capture(block, her_voice=True)              # the next answer starts closed
        self.assertFalse(canceller.open)

    def test_a_canceller_that_removes_nothing_keeps_the_microphone_gated(self):
        class _PassThrough(_Processor):
            def process_stream(self, frame):
                self.capture.append(len(frame.raw))

        canceller = EchoCanceller(24000, _PassThrough(), _Frame)
        block = (np.ones(1024, dtype=np.int16) * 3000).tobytes()
        for _ in range(200):
            canceller.capture(block, her_voice=True)
        self.assertFalse(canceller.open)
        self.assertGreater(canceller.reduction_db(), -2.0)

    def test_blocks_outside_her_voice_do_not_count(self):
        canceller = EchoCanceller(24000, _Processor(), _Frame)
        for _ in range(200):
            canceller.capture((np.ones(1024, dtype=np.int16) * 3000).tobytes())
        self.assertFalse(canceller.open)
        self.assertIsNone(canceller.interval_db())

    def test_the_native_canceller_loads(self):
        canceller = EchoCanceller.create(24000)
        self.assertIsNotNone(canceller)
        canceller.render(bytes(2400 * 2))
        cleaned = canceller.capture(bytes(1024 * 2))
        self.assertEqual(len(cleaned) % 480, 0)


class ToolsBesideTheConversationTests(unittest.TestCase):
    def test_the_receive_loop_keeps_reading_while_a_tool_runs(self):
        order, sent = [], []

        async def exercise():
            release = asyncio.Event()
            live = main.JarvisLive.__new__(main.JarvisLive)
            live._rx_at, live._rx_kinds, live._tool_tasks = 0.0, {}, set()

            class Session:
                async def receive(self):
                    yield SimpleNamespace(
                        data=None, server_content=None, session_resumption_update=None,
                        go_away=None, tool_call=SimpleNamespace(function_calls=[
                            SimpleNamespace(id="c1", name="slow_search", args={}),
                        ]),
                    )
                    order.append("receive continued")
                    release.set()
                    raise RuntimeError("end of test stream")

                async def send_tool_response(self, *, function_responses):
                    sent.append(function_responses)

            async def slow_tool(fc):
                await release.wait()
                order.append("tool finished")
                return SimpleNamespace(id=fc.id, response={"result": "ok"})

            live.session = Session()
            live._execute_tool = slow_tool
            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(main.traceback, "print_exc"):
                with self.assertRaises(RuntimeError):
                    await live._receive_audio()
                await asyncio.gather(*live._tool_tasks)

        asyncio.run(exercise())
        self.assertEqual(order, ["receive continued", "tool finished"])
        self.assertEqual(len(sent), 1)

    def test_phone_audio_is_resampled_to_the_session_rate(self):
        pcm = (np.ones(1600, dtype=np.int16) * 700).tobytes()
        resampled = main._resample_pcm16(pcm, 16000, 24000)
        self.assertEqual(len(resampled), 2400 * 2)
        self.assertTrue(np.all(np.frombuffer(resampled, dtype=np.int16) == 700))


class LearningOpenAITests(unittest.TestCase):
    def test_webrtc_offer_is_exchanged_server_side_without_exposing_key(self):
        captured = []
        service = LearningEnglishService(
            openai_key_loader=lambda: "sk-secret",
            openai_call_factory=lambda payload, key: captured.append((payload, key)) or "answer-sdp",
        )
        metadata = service.openai_session_description(
            {}, assistant_name="Lumina", user_name="Dal", voice_name="shimmer",
        )
        answer = service.create_openai_call(
            {}, assistant_name="Lumina", user_name="Dal", voice_name="shimmer",
            sdp="offer-sdp",
        )
        self.assertEqual(metadata["provider"], "openai")
        self.assertNotIn("sk-secret", json.dumps(metadata))
        self.assertEqual(answer, "answer-sdp")
        session = captured[0][0]["session"]
        self.assertEqual(captured[0][0]["sdp"], "offer-sdp")
        self.assertEqual(session["audio"]["output"]["voice"], "shimmer")
        self.assertEqual(session["audio"]["input"]["transcription"]["model"], "gpt-4o-mini-transcribe")
        self.assertNotIn("tools", session)

    def test_openai_leads_structured_learning_when_selected(self):
        calls = []
        service = LearningEnglishService(
            api_key_loader=lambda: None,
            openai_key_loader=lambda: "sk-secret",
            openai_response_factory=lambda payload, key: calls.append((payload, key)) or {
                "output": [{"content": [{"type": "output_text", "text": "{\"ok\":true}"}]}],
            },
        )
        original = learning_service_module.get_learning_voice_provider
        learning_service_module.get_learning_voice_provider = lambda: "openai"
        try:
            result = service._generate_json(
                {"task": "test"}, {"type": "object"},
                chain=(("gemini", "unused"), ("openai", OPENAI_TEXT_MODEL)),
                system_instruction="Return JSON", temperature=0,
                max_output_tokens=50, timeout=5, validate=lambda value: value,
            )
        finally:
            learning_service_module.get_learning_voice_provider = original
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0][0]["model"], OPENAI_TEXT_MODEL)


if __name__ == "__main__":
    unittest.main()
