"""OpenAI Realtime for Lumina: the desktop voice over a WebSocket, and the
session and call helpers the browser tutor's WebRTC uses.

The desktop assistant talks to a small session surface (`send_realtime_input`,
`send_client_content`, `send_tool_response` and `receive`), and
`OpenAIRealtimeSession` implements it on OpenAI's Realtime WebSocket, the same
shape as the Gemini Live socket beside it. The Gemini path is untouched.

Why a WebSocket and not WebRTC, measured on 2026-09-14: aiortc runs RTP in
Python, and inside Lumina it shares the interpreter lock with the HUD, which
keeps about one core busy. In the real app her voice chopped 289 times in
20 s, the user's words arrived as nonsense and the call dropped. Beside a
thread holding the lock the same way, WebRTC delivered no audio at all in
16 s, while this socket delivered 20 s of voice and the player never ran dry:
the server sends audio ahead of real time and the queue absorbs the jitter.
The browser tutor keeps WebRTC, where Chrome runs it natively.

What the service did in the probes, which shaped the event handling:

- A pause mid-sentence commits the audio and starts a response that is
  cancelled, with no output, when the user carries on. That is not a turn.
- The user's transcription can land after her answer has started, so a turn
  waits briefly for it.
- A function call is complete at `response.output_item.done`, while she may
  still be saying "one moment".
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
from websockets.asyncio.client import connect


OPENAI_REALTIME_MODEL = "gpt-realtime-2.1"
# OpenAI's PCM rate in both directions. It is also the rate her voice plays
# at, so the echo canceller compares like with like.
OPENAI_SAMPLE_RATE = 24_000
OPENAI_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"
REALTIME_SOCKET_URL = "wss://api.openai.com/v1/realtime?model={model}"

_TURN_WAIT_SECONDS = 3.0      # longest a finished turn waits for a transcription
_RESPONSE_ACK_SECONDS = 5.0   # a response.create never acknowledged is forgotten
# How long a chunk takes to be heard once it arrives: the player primes 200 ms
# and the speaker's buffer holds about as much again (213 ms measured on MME).
_PLAYBACK_DELAY_SECONDS = 0.4
# Errors this session cannot survive. Every other error is printed and the
# conversation carries on.
_FATAL_ERROR_CODES = {"invalid_api_key", "session_expired", "insufficient_quota"}

_CLOSED = object()


def _ns(**fields):
    return SimpleNamespace(**fields)


def _response(*, data: bytes | None = None, server_content=None, tool_call=None):
    """Return the attributes consumed by JarvisLive._receive_audio."""
    return _ns(
        data=data,
        server_content=server_content,
        tool_call=tool_call,
        session_resumption_update=None,
        go_away=None,
    )


def _server_content(
    *, output: str = "", input_final: str = "", input_interim: str = "",
    turn_complete: bool = False, interrupted: bool = False,
):
    return _ns(
        output_transcription=_ns(text=output) if output else None,
        input_transcription=_ns(text=input_final) if input_final else None,
        interim_input_transcription=(
            _ns(text=input_interim) if input_interim else None
        ),
        turn_complete=turn_complete,
        interrupted=interrupted,
        model_turn=None,
    )


def _json_schema(value: Any) -> Any:
    """Gemini accepts upper-case JSON-schema type names; OpenAI uses lower-case."""
    if isinstance(value, dict):
        converted = {key: _json_schema(item) for key, item in value.items()}
        if isinstance(converted.get("type"), str):
            converted["type"] = converted["type"].lower()
        return converted
    if isinstance(value, list):
        return [_json_schema(item) for item in value]
    return value


def _openai_tools(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for declaration in declarations:
        tool = {
            "type": "function",
            "name": declaration["name"],
            "parameters": _json_schema(declaration.get("parameters") or {
                "type": "object", "properties": {},
            }),
        }
        if declaration.get("description"):
            tool["description"] = declaration["description"]
        tools.append(tool)
    return tools


def _data_uri(data: bytes | str, mime: str) -> str:
    if isinstance(data, bytes):
        data = base64.b64encode(data).decode("ascii")
    return f"data:{mime or 'image/jpeg'};base64,{data}"


def session_config(
    *,
    instructions: str,
    voice: str,
    tools: list[dict[str, Any]] | None = None,
    model: str = OPENAI_REALTIME_MODEL,
    pcm_rate: int | None = None,
    assistant_name: str = "",
) -> dict[str, Any]:
    """The session every Lumina conversation opens with, desktop and browser.

    `pcm_rate` is for the socket, which carries raw PCM and must name its rate;
    a WebRTC call negotiates Opus instead.
    """
    transcription: dict[str, Any] = {"model": OPENAI_TRANSCRIPTION_MODEL}
    if assistant_name:
        # Heard as "Linda" and "Lovina" without it.
        transcription["prompt"] = f"A conversation with a voice assistant named {assistant_name}."
    audio_in: dict[str, Any] = {
        "noise_reduction": {"type": "far_field"},
        "transcription": transcription,
        # The server ends the user's turn and stops her when the user talks
        # over her; both measured working in the probes. Low eagerness waits
        # longer through a pause: on "auto" she began answering "Hola Lumina."
        # while the question after it was still being asked.
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "low",
            "create_response": True,
            "interrupt_response": True,
        },
    }
    audio_out: dict[str, Any] = {"voice": voice}
    if pcm_rate:
        audio_in["format"] = {"type": "audio/pcm", "rate": pcm_rate}
        audio_out["format"] = {"type": "audio/pcm", "rate": pcm_rate}
    config: dict[str, Any] = {
        "type": "realtime",
        "model": model,
        "output_modalities": ["audio"],
        "instructions": instructions,
        "audio": {"input": audio_in, "output": audio_out},
    }
    if tools:
        config["tools"] = _openai_tools(tools)
        config["tool_choice"] = "auto"
    return config


def create_call(
    api_key: str,
    sdp: str,
    session: dict[str, Any],
    *,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Trade a browser's WebRTC offer for OpenAI's answer. The key stays in Python.

    The offer and the session travel as multipart fields named `sdp` and
    `session`, as OpenAI's WebRTC guide specifies.
    """
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured. Add it in Lumina > API Keys.")
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            REALTIME_CALLS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={
                "sdp": (None, sdp),
                "session": (None, json.dumps(session, ensure_ascii=False)),
            },
        )
    if response.status_code >= 400:
        try:
            error = response.json().get("error") or {}
            detail = f"{error.get('code') or error.get('type') or ''}: {error.get('message') or ''}"
        except Exception:
            detail = response.text
        raise RuntimeError(
            f"OpenAI WebRTC call failed ({response.status_code}) {detail.strip(': ')[:200]}".strip()
        )
    return response.text


class OpenAIRealtimeSession:
    """OpenAI Realtime on a WebSocket, behind Lumina's Live session surface."""

    def __init__(self, websocket=None, *, model: str = OPENAI_REALTIME_MODEL):
        self.model = model
        self._ws = websocket
        self._events: asyncio.Queue = asyncio.Queue()
        # Every event leaves through one writer, in the order it was sent.
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._failure = ""
        self._closed = False
        # A turn the model has finished but the transcriber has not.
        self._turn_waiting_since = 0.0
        self._untranscribed: set[str] = set()
        self._heard: dict[str, str] = {}
        # One response at a time: a request made during another is sent after it.
        self._response_active = False
        self._response_wanted = False
        self._response_requested_at = 0.0
        self._calls_emitted: set[str] = set()
        # Live Vision keeps one frame in the conversation, not one per second.
        self._frame_item = ""
        self._frame_seq = 0
        # Her latest answer as delivered: which item, when it began, how long.
        self._audio_item = ""
        self._audio_started_at = 0.0
        self._audio_ms = 0.0

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        *,
        api_key: str,
        instructions: str,
        voice: str,
        tools: list[dict[str, Any]],
        model: str = OPENAI_REALTIME_MODEL,
        assistant_name: str = "",
    ):
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured")
        async with connect(
            REALTIME_SOCKET_URL.format(model=model),
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            session = cls(websocket, model=model)
            await session._configure(session_config(
                instructions=instructions, voice=voice, tools=tools,
                model=model, pcm_rate=OPENAI_SAMPLE_RATE,
                assistant_name=assistant_name,
            ))
            session._tasks = [
                asyncio.create_task(session._read(), name="openai-realtime-read"),
                asyncio.create_task(session._write(), name="openai-realtime-write"),
            ]
            try:
                yield session
            finally:
                await session.close()

    async def _configure(self, config: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(
            {"type": "session.update", "session": config}, ensure_ascii=False,
        ))
        # Connected means the server accepted model, voice, transcription and
        # tools. Otherwise a bad key or configuration would look live.
        while True:
            event = json.loads(await self._ws.recv())
            kind = event.get("type", "")
            if kind == "session.updated":
                return
            if kind == "error":
                raise RuntimeError(self._error_text(event))

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                self._on_message(raw)
        except Exception as exc:
            self._fail(f"OpenAI socket closed: {type(exc).__name__}: {exc}")
            return
        code = getattr(self._ws, "close_code", None)
        reason = getattr(self._ws, "close_reason", "") or ""
        self._fail(f"OpenAI socket closed ({code} {reason})".strip())

    async def _write(self) -> None:
        try:
            while True:
                await self._ws.send(await self._outbox.get())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(f"OpenAI socket send failed: {type(exc).__name__}: {exc}")

    # ── server events ────────────────────────────────────────────────────────

    def _on_message(self, raw) -> None:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if isinstance(event, dict):
            self._handle_event(event)

    def _emit(self, **fields) -> None:
        self._events.put_nowait(_response(**fields))

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")

        if kind == "error":
            self._on_error(event)
            return

        if kind == "response.output_audio.delta":
            data = base64.b64decode(event.get("delta") or "")
            if data:
                item = str(event.get("item_id") or "")
                if item != self._audio_item:
                    self._audio_item = item
                    self._audio_started_at = time.monotonic()
                    self._audio_ms = 0.0
                self._audio_ms += len(data) / 2 / OPENAI_SAMPLE_RATE * 1000
                self._emit(data=data)
            return

        if kind == "input_audio_buffer.speech_started":
            self._truncate_unheard()
            self._emit(server_content=_server_content(interrupted=True))
            return

        if kind == "input_audio_buffer.committed":
            if event.get("item_id"):
                self._untranscribed.add(str(event["item_id"]))
            self._emit()
            return
        if kind == "conversation.item.input_audio_transcription.delta":
            item = str(event.get("item_id") or "input")
            text = self._heard.get(item, "") + str(event.get("delta") or "")
            self._heard[item] = text
            if text:
                self._emit(server_content=_server_content(input_interim=text))
            return
        if kind in (
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.failed",
        ):
            item = str(event.get("item_id") or "input")
            self._untranscribed.discard(item)
            text = str(event.get("transcript") or self._heard.get(item, ""))
            self._heard.pop(item, None)
            if kind.endswith("failed"):
                print(f"[OPENAI] ⚠️ transcription failed: {self._error_text(event)}", flush=True)
            if text:
                self._emit(server_content=_server_content(input_final=text))
            else:
                self._emit()
            return

        if kind in ("response.output_audio_transcript.delta", "response.output_text.delta"):
            if event.get("delta"):
                self._emit(server_content=_server_content(output=str(event["delta"])))
            return
        if kind == "response.created":
            self._response_active = True
            self._response_requested_at = 0.0
            self._emit()
            return
        if kind == "response.output_item.done":
            # Dispatched the moment the call is complete, while she may still be
            # saying "one moment": the tool must not wait for her to finish.
            self._emit_calls([event.get("item") or {}])
            return
        if kind == "response.done":
            self._on_response_done(event.get("response") or {})
            return

        if kind.endswith(".delta"):
            return
        # Bookkeeping, forwarded bare: it is still proof the session is served.
        self._emit()

    def _heard_ms(self, now: float) -> float:
        """How much of her latest answer has reached the speakers by now."""
        elapsed = (now - self._audio_started_at - _PLAYBACK_DELAY_SECONDS) * 1000
        return max(0.0, min(self._audio_ms, elapsed))

    def _truncate_unheard(self) -> None:
        """Tell the server where the user cut her off.

        Audio arrives ahead of the speakers, and on a socket the server cannot
        know how much was played. Without this it believes she said all of it,
        and answers as if the user heard what never left the queue.
        """
        if not self._audio_item:
            return
        item, heard = self._audio_item, self._heard_ms(time.monotonic())
        self._audio_item = ""
        if heard >= self._audio_ms - 50:
            return
        try:
            self._send({
                "type": "conversation.item.truncate",
                "item_id": item,
                "content_index": 0,
                "audio_end_ms": int(heard),
            })
        except Exception:
            pass

    def _emit_calls(self, items: list[dict[str, Any]]) -> None:
        calls = []
        for item in items:
            if item.get("type") != "function_call":
                continue
            call_id = str(item.get("call_id") or item.get("id") or "")
            if not call_id or call_id in self._calls_emitted:
                continue
            self._calls_emitted.add(call_id)
            try:
                args = json.loads(item.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            calls.append(_ns(
                id=call_id,
                name=str(item.get("name") or ""),
                args=args if isinstance(args, dict) else {},
            ))
        if calls:
            self._emit(tool_call=_ns(function_calls=calls))

    def _on_response_done(self, response: dict[str, Any]) -> None:
        self._response_active = False
        output = response.get("output") or []
        self._emit_calls(output)
        status = str(response.get("status") or "")
        if status == "failed":
            print(f"[OPENAI] ⚠️ response failed: "
                  f"{json.dumps(response.get('status_details'), ensure_ascii=False)[:200]}",
                  flush=True)
        if not (status == "cancelled" and not output) and not self._turn_waiting_since:
            self._turn_waiting_since = time.monotonic()
        if self._response_wanted:
            self._response_wanted = False
            try:
                self._create_response()
            except Exception as exc:
                print(f"[OPENAI] ⚠️ deferred response not sent: {exc}", flush=True)

    def _on_error(self, event: dict[str, Any]) -> None:
        code = str((event.get("error") or {}).get("code") or "")
        text = self._error_text(event)
        if code in _FATAL_ERROR_CODES:
            self._fail(text)
            return
        if code == "conversation_already_has_active_response":
            # The server started one at the same instant we asked; ours follows it.
            self._response_active = True
            self._response_wanted = True
        print(f"[OPENAI] ⚠️ {text}", flush=True)
        self._emit()

    @staticmethod
    def _error_text(event: dict[str, Any]) -> str:
        error = event.get("error") or {}
        message = str(error.get("message") or event.get("message") or "OpenAI Realtime error")
        code = str(error.get("code") or error.get("type") or "")
        return f"{code}: {message}".strip(": ")

    def _fail(self, message: str) -> None:
        if self._closed or self._failure:
            return
        self._failure = message
        self._events.put_nowait(_CLOSED)

    def _turn_ready(self, now: float) -> bool:
        if not self._turn_waiting_since:
            return False
        if self._untranscribed and now - self._turn_waiting_since < _TURN_WAIT_SECONDS:
            return False
        return True

    # ── Lumina's session surface ─────────────────────────────────────────────

    async def receive(self) -> AsyncIterator[Any]:
        while True:
            now = time.monotonic()
            if self._turn_ready(now):
                self._turn_waiting_since = 0.0
                yield _response(server_content=_server_content(turn_complete=True))
                continue
            if (self._response_requested_at
                    and now - self._response_requested_at > _RESPONSE_ACK_SECONDS):
                self._response_requested_at = 0.0
                self._response_active = False
            try:
                item = await asyncio.wait_for(self._events.get(), timeout=0.1)
            except TimeoutError:
                continue
            if item is _CLOSED:
                raise RuntimeError(self._failure or "OpenAI session closed")
            yield item

    def _send(self, event: dict[str, Any]) -> None:
        if self._failure:
            raise RuntimeError(self._failure)
        self._outbox.put_nowait(json.dumps(event, ensure_ascii=False))

    def _create_response(self) -> None:
        if self._response_active:
            self._response_wanted = True
            return
        self._response_active = True
        self._response_requested_at = time.monotonic()
        self._send({"type": "response.create"})

    async def send_realtime_input(self, *, media=None, video=None) -> None:
        if media is not None:
            raw = media.get("data") if isinstance(media, dict) else getattr(media, "data", b"")
            if raw:
                self._send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(bytes(raw)).decode("ascii"),
                })
            return
        if video is not None:
            raw = bytes(getattr(video, "data", b"") or b"")
            if not raw:
                return
            self._frame_seq += 1
            item_id = f"lumina_frame_{self._frame_seq}"
            self._send({
                "type": "conversation.item.create",
                "item": {
                    "id": item_id, "type": "message", "role": "user",
                    "content": [{
                        "type": "input_image",
                        "image_url": _data_uri(raw, getattr(video, "mime_type", "")),
                    }],
                },
            })
            # Only the newest frame stays: every response reads the whole
            # conversation, and an hour of frames would be read every time.
            if self._frame_item:
                self._send({"type": "conversation.item.delete", "item_id": self._frame_item})
            self._frame_item = item_id

    async def send_client_content(self, *, turns, turn_complete: bool = True) -> None:
        if isinstance(turns, list):
            source_parts = []
            for turn in turns:
                source_parts.extend((turn or {}).get("parts") or [])
        else:
            source_parts = (turns or {}).get("parts") or []

        parts: list[dict[str, Any]] = []
        for part in source_parts:
            if part.get("text") is not None:
                parts.append({"type": "input_text", "text": str(part["text"])})
                continue
            inline = part.get("inline_data") or part.get("inlineData")
            if inline:
                parts.append({
                    "type": "input_image",
                    "image_url": _data_uri(
                        inline.get("data") or "",
                        inline.get("mime_type") or inline.get("mimeType") or "image/jpeg",
                    ),
                })
        if not parts:
            return
        self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": parts},
        })
        if turn_complete:
            self._create_response()

    async def send_tool_response(self, *, function_responses) -> None:
        silent = True
        for response in function_responses:
            payload = getattr(response, "response", None) or {}
            if not (isinstance(payload, dict) and payload.get("silent")):
                silent = False
            self._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": str(getattr(response, "id", "")),
                    "output": json.dumps(payload, ensure_ascii=False, default=str),
                },
            })
        # A silent result (a saved memory) needs no answer of its own.
        if function_responses and not silent:
            self._create_response()

    async def cancel_response(self) -> None:
        self._truncate_unheard()
        try:
            self._send({"type": "response.cancel"})
        except Exception:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
