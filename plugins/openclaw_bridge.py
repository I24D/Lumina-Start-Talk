"""
Two-way bridge from Lumina Start Talk to OpenClaw.

Uses a persistent WebSocket connection to the OpenClaw Gateway for instant
message delivery. Falls back to the CLI when the WebSocket is unavailable.

The endpoint and lifecycle below were verified against the local
``I24D/Lumina-Openclaw`` checkout at ``C:\\I24D_WhatsApp\\openclaw-main``:

* The gateway listens on ``ws://127.0.0.1:18789``. Its HTTP dashboard is served
  from ``http://127.0.0.1:18789/``; agent calls use the WebSocket protocol, not
  an HTTP chat route.
* Authentication mode is ``token``. ``gateway.auth.token`` in
  ``~/.openclaw/openclaw.json`` is a SecretRef into OpenClaw's own secret store,
  which Python cannot read, and the CLI refuses to print the value outside an
  interactive terminal. The persistent WebSocket therefore takes the token from
  ``GATEWAY_AUTH_TOKEN`` in the environment or in this project's git-ignored
  ``.env``; the CLI fallback keeps resolving it on its own.
* On Windows the gateway is registered as the ``OpenClaw Gateway`` Scheduled
  Task. ``openclaw gateway start`` starts it; the installed task may take a
  while to open port 18789 while its agent databases initialize.
* ``openclaw agent --json`` sends a turn through the gateway and returns spoken
  text in ``payloads[].text``. If the gateway cannot start and no other gateway
  owns the state directory, ``openclaw agent --local`` is the supported local
  fallback and uses the same configured agent, model, and credentials.

This module intentionally invokes the OpenClaw CLI only as a fallback. The
primary path is a persistent WebSocket that stays open for the lifetime of
Lumina Start Talk, eliminating per-message CLI startup overhead.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import pathlib
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any

from plugins._spoken_answer import spoken_answer

# ── Optional WebSocket support ────────────────────────────────────────────────
# The persistent connection uses the `websockets` library when available.
# It is a common dependency, but we fall back to the CLI if it is missing
# rather than making it a hard requirement.
_ws_available = False
_WsState = None
try:
    import asyncio as _asyncio
    import websockets as _websockets
    _ws_available = True
    try:
        from websockets.protocol import State as _WsState
    except ImportError:  # websockets < 11 exposes open/closed instead of state
        _WsState = None
except ImportError:
    pass


PLUGIN = {
    "name": "openclaw_bridge",
    "description": (
        "Talks directly to OpenClaw through its official CLI: connects to or starts its gateway, "
        "sends a question and reads OpenClaw's answer aloud when it arrives. The answer does NOT "
        "come back with this tool call: OpenClaw can take several minutes on a real task, so the "
        "call returns straight away and Lumina speaks the answer by herself later. Never send the "
        "same question twice because nothing came back yet, and never tell the user it failed or "
        "timed out — it is still working. "
        "Use this for ANY request involving OpenClaw or Open Claw. Do NOT use open_app or "
        "computer_control for these requests; those tools cannot communicate with the gateway or "
        "read its reply. "
        "YOU ARE THE MESSENGER, NOT THE JUDGE. OpenClaw is a full agent with its own browser, "
        "files, terminal, memory, channels and skills — far more than you can see from here, and "
        "more than this description could list. You do NOT know what it can and cannot do. Never "
        "refuse a request, and never tell the user OpenClaw lacks some capability, because you "
        "believe it is beyond OpenClaw: pass the instruction through exactly as the user gave it "
        "and let OpenClaw answer for itself. If it truly cannot do the thing, its own answer will "
        "say so, and that answer is the one to read out. Deciding on its behalf is how the user "
        "gets told something is impossible when it is not. "
        "action='ask' (default) sends the text and returns OpenClaw's answer: 'pregúntale a Open "
        "Claw qué puede hacer', 'dile a Open Claw que...', 'ask Open Claw what it can do', "
        "'tell OpenClaw to...'. Put the question itself in 'text'. "
        "action='connect' prepares the connection without asking anything: 'conéctate con Open "
        "Claw', 'conéctate a Open Claw', 'connect to Open Claw', 'connect to OpenClaw'. "
        "action='read' repeats the most recent answer, and recovers it from OpenClaw's own "
        "stored conversation when Lumina has been restarted since — so it is the right tool "
        "for 'what did Open Claw answer?' even at the start of a session: 'léeme la respuesta de Open Claw', "
        "'qué respondió Open Claw', 'read Open Claw's answer', 'what did OpenClaw say'. "
        "action='close' disconnects the bridge: 'desconéctate de Open Claw', 'cierra la conexión "
        "con Open Claw', 'disconnect from Open Claw', 'close the OpenClaw connection'. "
        "Answers the user gets in OpenClaw's own chat are announced by Lumina on her own as "
        "soon as they arrive; no tool call is needed for that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "ask (default) | connect | read | close",
            },
            "text": {
                "type": "STRING",
                "description": (
                    "The question or instruction to send to OpenClaw. Required for action='ask'. "
                    "Relay what the user actually asked for, in their own language and without "
                    "trimming it down to the part you think OpenClaw can manage — the whole "
                    "instruction is what it needs in order to act on it."
                ),
            },
        },
        "required": [],
    },
}


_AGENT_ID = "main"
_SESSION_KEY = "lumina-start-talk"
_GATEWAY_HOST = "127.0.0.1"
_GATEWAY_PORT = 18789
_GATEWAY_START_TIMEOUT = 180
_GATEWAY_OWNERSHIP_WAIT = 90
_DEFAULT_ANSWER_TIMEOUT = 180

_ACTIONS = {"ask", "connect", "open", "read", "close", "disconnect"}

_bridge_connected = False
_gateway_started_by_bridge = False
_transport = ""
_last_answer = ""

# ── waiting without blocking ─────────────────────────────────────────────────
_job_lock = threading.Lock()
_job_question = ""            # non-empty while an answer is still being waited for
_job_started = 0.0

# Nothing blocks on this any more, so it guards against a hung CLI rather than
# rationing the user's patience.
_BACKGROUND_TIMEOUT = 1800

# ── Persistent WebSocket connection ──────────────────────────────────────────
# A single long-lived WebSocket to the Gateway. Created on first use (or on
# explicit connect), kept alive with a heartbeat, and reused for every
# subsequent message. This eliminates the 5-10s CLI startup overhead per
# message and makes Lumina Start Talk → OpenClaw feel instant.
_ws_lock = threading.Lock()
_ws_ws = None              # type: ignore[assignment]
_ws_loop = None            # type: ignore[assignment]
_ws_thread = None          # type: ignore[assignment]
_ws_connected = False
_ws_heartbeat_stop = threading.Event()
_ws_pending_lock = threading.Lock()
# Request id -> Future resolved by the matching `res` frame.
_ws_pending: dict[str, Future] = {}
# chat.send run id -> Future resolved by that run's terminal `chat` event.
_ws_runs: dict[str, Future] = {}
# The Gateway only sends a run's `chat` events to connections subscribed to it.
_SESSION_SUBSCRIPTION_KEY = f"agent:{_AGENT_ID}:{_SESSION_KEY}"
# Every run id this bridge has sent. OpenClaw stamps it on the answer it
# stores, which is how the chat watcher knows _deliver already speaks that one.
_bridge_run_ids: deque[str] = deque(maxlen=200)
_TERMINAL_CHAT_STATES = frozenset({"final", "error", "aborted"})


def _cli_path() -> str | None:
    """Return the installed OpenClaw CLI path without assuming a repo checkout."""
    names = ("openclaw.cmd", "openclaw") if platform.system() == "Windows" else ("openclaw",)
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _project_dotenv_path() -> pathlib.Path:
    """This project's git-ignored .env, next to main.py."""
    return pathlib.Path(__file__).resolve().parent.parent / ".env"


def _dotenv_value(name: str) -> str:
    """Read one KEY=value entry from the project .env without a dotenv dependency."""
    try:
        lines = _project_dotenv_path().read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip()
    return ""


def _read_gateway_token() -> str | None:
    """Read the gateway auth token for the WebSocket handshake.

    Checked in order: the ``GATEWAY_AUTH_TOKEN`` environment variable, the same
    key in this project's ``.env``, then ``gateway.auth.token`` in openclaw.json
    when it is a plain string, or the environment variable its SecretRef names.

    OpenClaw keeps a SecretRef's value in its own SQLite secret store, which is
    not an interface for other processes, and ``openclaw gateway auth-token
    --show`` refuses to print outside an interactive terminal. The project .env
    is the channel left for a separately launched Lumina.
    """
    env_token = os.environ.get("GATEWAY_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    dotenv_token = _dotenv_value("GATEWAY_AUTH_TOKEN")
    if dotenv_token:
        return dotenv_token

    try:
        config_path = pathlib.Path.home() / ".openclaw" / "openclaw.json"
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        token = config.get("gateway", {}).get("auth", {}).get("token", {})

        if isinstance(token, str) and token.strip():
            return token.strip()

        if isinstance(token, dict):
            token_id = str(token.get("id", "")).strip()
            if token_id and token_id != "__OPENCLAW_REDACTED__":
                for name in (token_id, f"OPENCLAW_{token_id}"):
                    env_val = os.environ.get(name, "").strip()
                    if env_val:
                        return env_val
    except Exception:
        pass

    return None


def _run_cli(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run the official CLI without opening a console window."""
    cli = _cli_path()
    if not cli:
        raise FileNotFoundError("The OpenClaw CLI is not installed or is not on PATH.")

    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    creation_flags = 0
    if platform.system() == "Windows":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    return subprocess.run(
        [cli, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
        creationflags=creation_flags,
        check=False,
    )


def _json_objects(output: str) -> list[dict[str, Any]]:
    """Decode JSON objects from CLI output that may also contain status lines."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index in range(len(output)):
        if output[index] != "{":
            continue
        try:
            obj, end = decoder.raw_decode(output[index:])
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            continue
    return objects


def _nested_dicts(obj: Any):
    """Yield every dict nested anywhere inside obj (DFS, pre-order)."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _nested_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _nested_dicts(item)


def _result_error(result: subprocess.CompletedProcess[str]) -> str:
    """Extract a safe, concise error from a CLI result."""
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for root in reversed(_json_objects(combined)):
        for payload in _nested_dicts(root):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"]).strip()
            if isinstance(error, str) and error.strip():
                return error.strip()
            if payload.get("status") in {"error", "failed"} and payload.get("message"):
                return str(payload["message"]).strip()
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    return lines[-1][:300] if lines else f"OpenClaw exited with code {result.returncode}."


def _gateway_port_is_open() -> bool:
    try:
        with socket.create_connection((_GATEWAY_HOST, _GATEWAY_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _gateway_health() -> tuple[bool, str]:
    try:
        result = _run_cli(
            ["gateway", "health", "--json", "--timeout", "10000"],
            timeout=20,
        )
    except Exception as exc:
        return False, str(exc)

    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for payload in reversed(_json_objects(combined)):
        if payload.get("ok") is True:
            return True, ""
    return False, _result_error(result)


def _wait_for_gateway(timeout: int) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_error = "The gateway did not open its configured port."
    while time.monotonic() < deadline:
        if _gateway_port_is_open():
            healthy, error = _gateway_health()
            if healthy:
                return True, ""
            if error:
                last_error = error
        time.sleep(2.0)
    return False, last_error


def _ensure_connection(player=None) -> tuple[bool, str]:
    """Select a gateway connection when possible, otherwise a safe local fallback."""
    global _bridge_connected, _gateway_started_by_bridge, _transport

    if _bridge_connected:
        if _transport == "websocket":
            # A live socket is its own health check; asking the CLI first would
            # add the multi-second startup this transport exists to avoid.
            if _ws_connected and _ws_is_open(_ws_ws):
                return True, ""
        elif _transport == "local" or _gateway_health()[0]:
            return True, ""
        _bridge_connected = False
        _transport = ""

    # Try WebSocket first if available
    if _ws_available:
        ws_ok, ws_error = _ensure_ws_connection(player)
        if ws_ok:
            _bridge_connected = True
            _transport = "websocket"
            return True, ""

    healthy, _ = _gateway_health()
    if healthy:
        _bridge_connected = True
        _transport = "gateway"
        return True, ""

    start_timed_out = False
    try:
        start = _run_cli(["gateway", "start", "--json"], timeout=30)
    except subprocess.TimeoutExpired:
        start = None
        start_error = "The gateway start command is still initializing."
        start_timed_out = True
    except Exception as exc:
        start = None
        start_error = str(exc)
    else:
        start_error = _result_error(start) if start.returncode else ""

    if start_timed_out or (start is not None and start.returncode == 0):
        _gateway_started_by_bridge = True
        _say(player, "Tell the user briefly that Open Claw is starting and may take a moment.")
        healthy, health_error = _wait_for_gateway(_GATEWAY_START_TIMEOUT)
        if healthy:
            _bridge_connected = True
            _transport = "gateway"
            # Try to upgrade to WebSocket now that the gateway is up
            if _ws_available:
                ws_ok, _ = _ensure_ws_connection(player)
                if ws_ok:
                    _transport = "websocket"
            return True, ""
        start_error = health_error or start_error
        _stop_owned_gateway()

    if any(marker in start_error.lower() for marker in ("state ownership", "already running")):
        healthy, health_error = _wait_for_gateway(_GATEWAY_OWNERSHIP_WAIT)
        if healthy:
            _bridge_connected = True
            _transport = "gateway"
            if _ws_available:
                ws_ok, _ = _ensure_ws_connection(player)
                if ws_ok:
                    _transport = "websocket"
            return True, ""
        start_error = health_error or start_error

    if _cli_path():
        _bridge_connected = True
        _transport = "local"
        return True, ""

    return False, start_error or "The OpenClaw CLI is unavailable."


def _stop_owned_gateway() -> None:
    global _gateway_started_by_bridge
    if not _gateway_started_by_bridge:
        return
    try:
        _run_cli(["gateway", "stop", "--force", "--json"], timeout=45)
    except Exception:
        pass
    deadline = time.monotonic() + 15
    while _gateway_port_is_open() and time.monotonic() < deadline:
        time.sleep(0.5)
    _gateway_started_by_bridge = False


# ── WebSocket persistent connection ──────────────────────────────────────────

def _ws_url() -> str:
    return f"ws://{_GATEWAY_HOST}:{_GATEWAY_PORT}"


def _ws_run_loop(loop: Any) -> None:
    """Background asyncio event loop for the persistent WebSocket."""
    _asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except Exception:
        pass


def _ws_is_open(ws: Any) -> bool:
    """Whether a websockets connection is open, across library generations.

    websockets 11+ (Lumina ships 16.x) replaced ``open``/``closed`` with
    ``state``. Reading the removed attributes raises AttributeError, which broke
    every call into this bridge after the first one.
    """
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is not None and _WsState is not None:
        return state is _WsState.OPEN
    if hasattr(ws, "open"):
        return bool(ws.open)
    return not bool(getattr(ws, "closed", True))


def _frame_error(frame: dict[str, Any]) -> str:
    """The human-readable reason carried by a failed Gateway response."""
    error = frame.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"]).strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "unknown error"


def _ws_fail_waiters(reason: str) -> None:
    """Wake everyone still waiting on a connection that has gone away."""
    with _ws_pending_lock:
        waiters = [*_ws_pending.values(), *_ws_runs.values()]
        _ws_pending.clear()
        _ws_runs.clear()
    for future in waiters:
        if not future.done():
            future.set_exception(ConnectionError(reason))


# Last assistant message seen on the subscribed session, keyed by runId.
# The Gateway delivers the answer text as a session.message event and only
# signals run completion in the chat event — so we keep the latest assistant
# text and hand it to the waiter when the terminal chat event arrives.
_ws_last_assistant: dict[str, str] = {}


def _dispatch_ws_message(msg: dict[str, Any]) -> None:
    """Route one Gateway frame to whoever is waiting for it.

    A ``res`` frame only answers the request with the same id. For ``chat.send``
    that is the acceptance receipt ({runId, status}), never the answer.
    The answer text arrives as ``session.message`` events; the run's terminal
    ``chat`` event signals completion.
    """
    kind = msg.get("type")
    if kind == "res":
        with _ws_pending_lock:
            future = _ws_pending.pop(str(msg.get("id") or ""), None)
        if future is not None and not future.done():
            future.set_result(msg)
        return
    if kind != "event":
        return

    event_name = msg.get("event")
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return

    # Capture assistant messages from session.message events
    if event_name == "session.message":
        message = payload.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            said = content.strip() if isinstance(content, str) else _describe_parts(content)
            if said:
                run_id = str(payload.get("runId") or "")
                with _ws_pending_lock:
                    _ws_last_assistant[run_id] = said
        return

    # Terminal chat event: resolve the waiter with the captured answer
    if event_name == "chat" and payload.get("state") in _TERMINAL_CHAT_STATES:
        run_id = str(payload.get("runId") or "")
        with _ws_pending_lock:
            future = _ws_runs.pop(run_id, None)
            last_text = _ws_last_assistant.pop(run_id, "")
        if last_text:
            payload["message"] = {"content": last_text}
        if future is not None and not future.done():
            future.set_result(payload)


def _answer_from_chat_event(payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Turn a terminal ``chat`` event into (answer, error, started=True)."""
    if payload.get("state") == "error":
        return "", str(payload.get("errorMessage") or "OpenClaw reported an error."), True
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    said = content.strip() if isinstance(content, str) else _describe_parts(content)
    if said:
        return said, "", True
    # No message in the terminal event — fetch the last assistant reply from history
    session_key = str(payload.get("sessionKey") or "")
    if session_key:
        try:
            history = _ws_request(
                "chat.history",
                {"sessionKey": session_key, "limit": 5},
                timeout=15,
            )
            if history.get("ok"):
                messages = history.get("payload", {}).get("messages", [])
                for entry in reversed(messages):
                    if isinstance(entry, dict) and entry.get("role") == "assistant":
                        text = entry.get("text", "")
                        if text:
                            return text.strip(), "", True
                        parts = entry.get("content")
                        said2 = _describe_parts(parts)
                        if said2:
                            return said2, "", True
        except Exception as exc:
            print(f"[OpenClaw] chat.history fallback failed: {exc}")
    if payload.get("state") == "aborted":
        return "", "OpenClaw stopped before it answered.", True
    return "", "OpenClaw finished without an answer to read out.", True


def _ensure_ws_connection(player=None) -> tuple[bool, str]:
    """Ensure the persistent WebSocket is connected. Returns (ok, error)."""
    global _ws_ws, _ws_loop, _ws_thread, _ws_connected, _ws_heartbeat_stop

    if not _ws_available:
        return False, "websockets library not installed"

    with _ws_lock:
        if _ws_connected and _ws_is_open(_ws_ws):
            return True, ""

        # Retire any stale connection, its heartbeat, and whoever still waits on it.
        _ws_connected = False
        _ws_heartbeat_stop.set()
        stale = _ws_ws
        _ws_ws = None
        if stale is not None and _ws_loop is not None and not _ws_loop.is_closed():
            try:
                _asyncio.run_coroutine_threadsafe(stale.close(), _ws_loop).result(timeout=5)
            except Exception:
                pass
        _ws_fail_waiters("The OpenClaw WebSocket was reconnected.")

        # Ensure the event loop is running
        if _ws_loop is None or _ws_loop.is_closed():
            _ws_loop = _asyncio.new_event_loop()
            _ws_thread = threading.Thread(
                target=_ws_run_loop, args=(_ws_loop,), name="lumina-openclaw-ws", daemon=True
            )
            _ws_thread.start()

        # Connect, authenticate and subscribe (each step waits up to 15 s).
        try:
            future = _asyncio.run_coroutine_threadsafe(_ws_connect_async(), _ws_loop)
            ok, error = future.result(timeout=45)
        except Exception as exc:
            return False, f"WebSocket connect failed: {exc}"

        if not ok:
            return False, error

        _ws_connected = True
        _ws_heartbeat_stop = threading.Event()
        threading.Thread(
            target=_ws_heartbeat,
            args=(_ws_heartbeat_stop,),
            name="lumina-openclaw-ws-heartbeat",
            daemon=True,
        ).start()

    print(f"[OpenClaw] persistent WebSocket connected to {_ws_url()}")
    return True, ""


async def _ws_connect_async() -> tuple[bool, str]:
    """Open the socket, authenticate, and subscribe to this bridge's session.

    The Gateway only delivers a run's ``chat`` events to connections subscribed
    to that session, so without the subscription no answer would ever arrive.
    """
    global _ws_ws

    token = _read_gateway_token()

    try:
        ws = await _websockets.connect(
            _ws_url(),
            max_size=26_214_400,  # 25 MiB, matching gateway default
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
    except Exception as exc:
        return False, f"Could not connect to {_ws_url()}: {exc}"

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        await ws.send(
            json.dumps({"type": "req", "id": request_id, "method": method, "params": params})
        )
        while True:
            frame = json.loads(await _asyncio.wait_for(ws.recv(), timeout=15))
            if isinstance(frame, dict) and frame.get("type") == "res" and frame.get("id") == request_id:
                return frame

    try:
        try:
            # The Gateway greets every socket with a connect.challenge event.
            await _asyncio.wait_for(ws.recv(), timeout=10)
        except _asyncio.TimeoutError:
            pass

        connected = await request(
            "connect",
            {
                "minProtocol": 4,
                "maxProtocol": 4,
                "client": {
                    "id": "cli",
                    "version": "1.0.0",
                    "platform": "windows" if platform.system() == "Windows" else "linux",
                    "mode": "cli",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [],
                "commands": [],
                "permissions": {},
                "auth": {"token": token} if token else {},
                "locale": "en-US",
                "userAgent": "lumina-start-talk/1.0.0",
            },
        )
        if not connected.get("ok"):
            await ws.close()
            return False, f"Gateway rejected handshake: {_frame_error(connected)}"

        subscribed = await request("sessions.messages.subscribe", {"key": _SESSION_SUBSCRIPTION_KEY})
        if not subscribed.get("ok"):
            await ws.close()
            return False, f"Gateway refused the session subscription: {_frame_error(subscribed)}"
    except Exception as exc:
        try:
            await ws.close()
        except Exception:
            pass
        return False, f"Handshake error: {exc}"

    _ws_ws = ws
    _asyncio.get_running_loop().create_task(_ws_reader_loop())
    return True, ""


async def _ws_reader_loop() -> None:
    """Background task: read Gateway frames and hand each one to its waiter."""
    global _ws_connected
    try:
        async for raw in _ws_ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                _dispatch_ws_message(msg)
    except Exception as exc:
        print(f"[OpenClaw] WebSocket reader stopped: {exc}")
    finally:
        _ws_connected = False
        _ws_fail_waiters("The OpenClaw WebSocket closed.")


def _ws_request(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Send one request over the live socket and wait for its ``res`` frame."""
    ws, loop = _ws_ws, _ws_loop
    if ws is None or loop is None:
        raise ConnectionError("The OpenClaw WebSocket is not connected.")
    request_id = str(uuid.uuid4())
    future: Future = Future()
    with _ws_pending_lock:
        _ws_pending[request_id] = future
    frame = json.dumps({"type": "req", "id": request_id, "method": method, "params": params})
    try:
        _asyncio.run_coroutine_threadsafe(ws.send(frame), loop).result(timeout=10)
        return future.result(timeout=timeout)
    finally:
        with _ws_pending_lock:
            _ws_pending.pop(request_id, None)


def _ws_heartbeat(stop: threading.Event) -> None:
    """Ping every 30 s so a silently dead connection is noticed and replaced."""
    global _ws_connected
    while not stop.wait(30):
        if not _ws_connected or _ws_ws is None:
            return
        try:
            _asyncio.run_coroutine_threadsafe(_ws_ping(), _ws_loop).result(timeout=15)
        except Exception:
            _ws_connected = False
            return


async def _ws_ping() -> None:
    """Ping and wait for the pong; an unsolicited pong proves nothing."""
    ws = _ws_ws
    if not _ws_is_open(ws):
        raise ConnectionError("The OpenClaw WebSocket is not open.")
    pong_waiter = await ws.ping()
    await _asyncio.wait_for(pong_waiter, timeout=10)


def _ws_send_message(question: str, timeout: int) -> tuple[str, str, bool]:
    """Ask through the persistent WebSocket and wait for the run to finish.

    Returns (answer, error, started). ``started`` turns True once the question
    may be running on the Gateway; from then on the caller must not retry
    through the CLI, because that would carry out the same request twice.
    """
    run_id = str(uuid.uuid4())
    _bridge_run_ids.append(run_id)
    run_future: Future = Future()
    with _ws_pending_lock:
        _ws_runs[run_id] = run_future

    try:
        receipt = _ws_request(
            "chat.send",
            {"sessionKey": _SESSION_SUBSCRIPTION_KEY, "message": question, "idempotencyKey": run_id},
            timeout=15,
        )
    except FutureTimeout:
        with _ws_pending_lock:
            _ws_runs.pop(run_id, None)
        # The question may have left without its receipt coming back.
        return "", "OpenClaw did not confirm the question, so it was not sent again.", True
    except Exception as exc:
        with _ws_pending_lock:
            _ws_runs.pop(run_id, None)
        return "", f"WebSocket send failed: {exc}", False

    if not receipt.get("ok"):
        with _ws_pending_lock:
            _ws_runs.pop(run_id, None)
        return "", f"Gateway rejected the question: {_frame_error(receipt)}", False

    accepted = receipt.get("payload")
    accepted_run = str(accepted.get("runId") or run_id) if isinstance(accepted, dict) else run_id
    if accepted_run != run_id:
        _bridge_run_ids.append(accepted_run)
        with _ws_pending_lock:
            _ws_runs.pop(run_id, None)
            _ws_runs[accepted_run] = run_future

    try:
        event = run_future.result(timeout=max(1, timeout))
    except FutureTimeout:
        with _ws_pending_lock:
            _ws_runs.pop(accepted_run, None)
        return "", f"OpenClaw is still working after {timeout} seconds.", True
    except Exception as exc:
        return "", f"The connection dropped while OpenClaw was working: {exc}", True
    return _answer_from_chat_event(event)


def _ws_close() -> None:
    """Close the persistent WebSocket connection."""
    global _ws_ws, _ws_connected

    _ws_heartbeat_stop.set()
    with _ws_lock:
        _ws_connected = False
        stale = _ws_ws
        _ws_ws = None
        if stale is not None and _ws_loop is not None and not _ws_loop.is_closed():
            try:
                _asyncio.run_coroutine_threadsafe(stale.close(), _ws_loop).result(timeout=5)
            except Exception:
                pass
    _ws_fail_waiters("The OpenClaw bridge was disconnected.")


# ── CLI fallback (used when WebSocket is not available) ─────────────────────

def _describe_parts(items: Any) -> str:
    """Turn OpenClaw's reply parts into something that can be said out loud."""
    if not isinstance(items, list):
        return ""

    said: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if text:
            said.append(text)
            continue
        kind = str(item.get("type", "")).strip().lower()
        if kind in ("image", "photo", "picture"):
            said.append("OpenClaw produced an image.")
        elif kind in ("audio", "video", "file", "document"):
            said.append(f"OpenClaw produced {kind} content.")
    return "\n".join(said)


def _agent_answer(question: str, timeout: int, local: bool) -> tuple[str, str]:
    arguments = ["agent"]
    if local:
        arguments.append("--local")
    arguments.extend(
        [
            "--agent",
            _AGENT_ID,
            "--session-key",
            _SESSION_KEY,
            "--message",
            question,
            "--json",
            "--timeout",
            str(timeout),
        ]
    )
    try:
        result = _run_cli(arguments, timeout=timeout + 75)
    except subprocess.TimeoutExpired:
        return "", f"OpenClaw did not answer within {timeout} seconds."
    except Exception as exc:
        return "", str(exc)

    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for root in reversed(_json_objects(combined)):
        for payload in _nested_dicts(root):
            items = payload.get("payloads")
            if not isinstance(items, list):
                continue
            spoken = _describe_parts(items)
            if spoken:
                return spoken, ""
    return "", _result_error(result)


def _say(player, instruction: str) -> None:
    """Ask Lumina to speak while this plugin continues working."""
    try:
        say = getattr(player, "request_say", None)
        if callable(say):
            say(instruction)
    except Exception:
        pass


def _act_connect(player=None) -> str:
    connected, error = _ensure_connection(player)
    if not connected:
        return f"I could not connect to OpenClaw: {error}"
    if _transport == "websocket":
        return "Connected to OpenClaw through a persistent WebSocket link."
    if _transport == "gateway":
        return "Connected to the OpenClaw gateway."
    return "Connected to OpenClaw through its local CLI fallback."


def _wait_for_answer(question: str, timeout: int, player=None) -> tuple[str, str]:
    """Ask OpenClaw and wait for it, on a thread nothing is blocked on."""
    global _transport

    connected, error = _ensure_connection(player)
    if not connected:
        return "", f"I could not connect to OpenClaw: {error}"

    # Prefer WebSocket
    if _transport == "websocket":
        answer, error, started = _ws_send_message(question, timeout)
        if answer or started:
            # Once the Gateway may be running the turn, retrying through the CLI
            # would carry out the same request a second time.
            return answer, error
        print(f"[OpenClaw] WebSocket failed before the question was accepted ({error}), falling back to CLI")
        if _gateway_health()[0]:
            _transport = "gateway"
        elif _cli_path():
            _transport = "local"
        else:
            return "", error

    if _transport == "local" and _gateway_health()[0]:
        _transport = "gateway"

    _say(player, "Tell the user you have put the question to Open Claw and are waiting.")
    answer, error = _agent_answer(question, timeout, local=_transport == "local")

    if not answer and _transport == "local" and any(
        marker in error.lower() for marker in ("gateway is running", "state ownership")
    ):
        healthy, _ = _wait_for_gateway(_GATEWAY_OWNERSHIP_WAIT)
        if healthy:
            _transport = "gateway"
            answer, error = _agent_answer(question, timeout, local=False)

    if not answer and _transport == "gateway" and _gateway_started_by_bridge:
        _stop_owned_gateway()
        _transport = "local"
        answer, error = _agent_answer(question, timeout, local=True)

    return answer, error


def _deliver(question: str, answer: str, player=None) -> None:
    """Speak an answer that arrived long after its tool call returned."""
    global _last_answer
    _last_answer = answer

    print(f"[OpenClaw] answered after the fact: {len(answer)} chars")
    if player:
        try:
            player.write_log(f"[OpenClaw] answer received ({len(answer)} chars)")
        except Exception:
            pass

    _say(
        player,
        "[DELAYED_ANSWER] OpenClaw has finished the question you sent it "
        f"earlier ('{question[:120]}'). Its answer follows.\n\n"
        + spoken_answer(answer),
    )


def _run_job(question: str, timeout: int, player=None) -> None:
    """Background worker: wait for OpenClaw, then speak whatever came back."""
    global _job_question
    try:
        answer, error = _wait_for_answer(question, timeout, player)
    except Exception as exc:
        answer, error = "", f"{type(exc).__name__}: {exc}"
    finally:
        with _job_lock:
            _job_question = ""

    if answer:
        _deliver(question, answer, player)
        return

    print(f"[OpenClaw] ask failed: {error}")
    _say(
        player,
        "OpenClaw could not answer the question you sent it earlier. Tell the "
        f"user so in one sentence. The reason it gave was: {error}",
    )


def _act_ask(question: str, timeout: int, player=None) -> str:
    global _job_question, _job_started
    if not question:
        return "What would you like me to ask OpenClaw?"

    with _job_lock:
        if _job_question:
            waited = int(time.monotonic() - _job_started)
            return (
                f"I am still waiting on OpenClaw for '{_job_question[:80]}' — "
                f"{waited} seconds so far. I will read that answer out the moment "
                "it arrives; ask me again afterwards and I will send the new one."
            )
        _job_question = question
        _job_started = time.monotonic()

    threading.Thread(
        target=_run_job,
        args=(question, max(timeout, _BACKGROUND_TIMEOUT), player),
        name="lumina-openclaw-ask",
        daemon=True,
    ).start()

    return (
        "I have put the question to OpenClaw. A real task can take it several "
        "minutes, so I will not keep you waiting — carry on, and I will read the "
        "answer out loud the moment it arrives."
    )


def _session_store() -> pathlib.Path:
    """Where OpenClaw keeps this agent's conversations on disk."""
    root = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    base = pathlib.Path(root) if root else pathlib.Path.home() / ".openclaw"
    return base / "agents" / _AGENT_ID / "agent" / "openclaw-agent.sqlite"


def _stored_answer() -> str:
    """The last thing OpenClaw actually said, read back from its own session."""
    store = _session_store()
    if not store.exists():
        return ""

    key = f"agent:{_AGENT_ID}:{_SESSION_KEY}"
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{store.as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        row = connection.execute(
            "select current_session_id from session_nodes where session_key = ?",
            (key,),
        ).fetchone()
        if not row or not row[0]:
            return ""

        for (event_json,) in connection.execute(
            "select event_json from transcript_events "
            "where session_id = ? order by seq desc limit 100",
            (row[0],),
        ):
            try:
                message = (json.loads(event_json) or {}).get("message")
            except Exception:
                continue
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            said = _describe_parts(message.get("content"))
            if said:
                return said
        return ""
    except Exception as exc:
        print(f"[OpenClaw] could not read the stored session: {type(exc).__name__}: {exc}")
        return ""
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


# ── announcing answers from OpenClaw's own chat ──────────────────────────────
#
# _deliver speaks the answers to questions Lumina sent. The user also works in
# OpenClaw's own chat, and start() watches for those answers: each new finished
# turn in the main agent's store is queued for Lumina to announce once she is
# free.
#
# The store holds more than that chat, and most of it is nobody's news. Replies
# the bot sends to Telegram contacts belong to a conversation with a channel,
# scheduled jobs run under a ":cron:" session key, and OpenClaw's own Talk voice
# marks what it said. Questions this bridge sent are skipped too: _deliver
# already speaks them, and a second reading would say the same answer twice.
# Every row is also checked against the watcher's start, so a store that
# rewrites a transcript never replays old answers.

_WATCH_SECONDS = 2.0
_FINISHED_STOP_REASONS = frozenset({"stop", "end_turn"})

_watch_lock = threading.Lock()
_watch_thread: threading.Thread | None = None

_NEW_ROWS_SQL = (
    "select t.rowid, t.created_at, t.event_json, w.session_key,"
    " (select c.channel from session_conversations sc join conversations c"
    "   on c.conversation_id = sc.conversation_id"
    "   where sc.session_id = t.session_id limit 1)"
    " from transcript_events t left join session_windows w on w.session_id = t.session_id"
    " where t.rowid > ? order by t.rowid limit 500"
)


def _chat_answer(event_json: str) -> tuple[str, str, float]:
    """(text, run id, timestamp) of a finished assistant turn, or ("", "", 0.0)."""
    try:
        event = json.loads(event_json)
    except (TypeError, ValueError):
        return "", "", 0.0
    message = event.get("message") if isinstance(event, dict) and event.get("type") == "message" else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return "", "", 0.0
    if message.get("stopReason") not in _FINISHED_STOP_REASONS:
        return "", "", 0.0

    provenance = message.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    if (
        message.get("api") == "realtime"
        or str(message.get("idempotencyKey") or "").startswith("talk-")
        or provenance.get("kind") == "realtime_voice"
        or provenance.get("sourceChannel") == "talk"
    ):
        return "", "", 0.0    # OpenClaw's Talk voice said this out loud already

    content = message.get("content")
    text = content.strip() if isinstance(content, str) else _describe_parts(content)
    marks = message.get("__openclaw")
    run_id = str(marks.get("runId") or "") if isinstance(marks, dict) else ""
    stamp = message.get("timestamp")
    return text, run_id, stamp / 1000 if isinstance(stamp, (int, float)) else 0.0


def _answered_through_bridge(session_key: str, text: str, run_id: str) -> bool:
    """True when _deliver speaks this answer itself."""
    if run_id and run_id in _bridge_run_ids:
        return True
    if session_key != _SESSION_SUBSCRIPTION_KEY:
        return False
    # The CLI fallback leaves no run id behind. A question still being waited
    # on, or the answer _deliver has just spoken, is what gives it away.
    with _job_lock:
        waiting = bool(_job_question)
    return waiting or " ".join(text.split()) == " ".join(_last_answer.split())


def _finished_since(after_rowid: int | None, started: float) -> tuple[list[str], int | None]:
    """New chat answers stored after ``after_rowid``, and the rowid to go on from.

    ``None`` means this is the first look: the store's current end becomes the
    starting point, so nothing already in it is announced.
    """
    store = _session_store()
    if not store.exists():
        return [], after_rowid

    connection = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True, timeout=2.0)
    try:
        newest = connection.execute(
            "select coalesce(max(rowid), 0) from transcript_events"
        ).fetchone()[0]
        if after_rowid is None or newest < after_rowid:
            return [], newest

        answers: list[str] = []
        for rowid, created_at, event_json, session_key, channel in connection.execute(
            _NEW_ROWS_SQL, (after_rowid,)
        ):
            after_rowid = max(after_rowid, rowid)
            if channel or ":cron:" in (session_key or ""):
                continue
            text, run_id, stamp = _chat_answer(event_json)
            if not text or (stamp or (created_at or 0) / 1000) < started:
                continue
            if _answered_through_bridge(session_key or "", text, run_id):
                continue
            answers.append(text)
        return answers, after_rowid
    finally:
        connection.close()


def _announce_chat_answer(answer: str, player=None) -> None:
    global _last_answer
    _last_answer = answer    # so "read" repeats this one, not an older answer

    print(f"[OpenClaw] finished a task in its chat ({len(answer)} chars); announcing")
    try:
        if player:
            player.write_log(f"[OpenClaw] finished a task in its chat ({len(answer)} chars)")
        announce = getattr(player, "request_announce", None)
        if callable(announce):
            announce(
                "[CHAT_FINISHED] OpenClaw has just finished a task in its chat. "
                "Its answer follows.\n\n" + spoken_answer(answer)
            )
    except Exception as exc:
        print(f"[OpenClaw] could not announce: {exc}")


def _enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_enabled
        return get_plugin_enabled(PLUGIN["name"])
    except Exception:
        return True


def _watch(player) -> None:
    started = time.time()
    after_rowid: int | None = None
    last_error = ""
    while True:
        try:
            # A disabled plugin still looks, so switching it back on does not
            # announce everything that finished in the meantime.
            answers, after_rowid = _finished_since(after_rowid, started)
            if answers and _enabled():
                for answer in answers:
                    _announce_chat_answer(answer, player)
            last_error = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != last_error:
                print(f"[OpenClaw] chat watcher: {error}")
            last_error = error
        time.sleep(_WATCH_SECONDS)


def start(player=None) -> None:
    """Begin announcing answers from OpenClaw's own chat. Called once by the plugin loader."""
    global _watch_thread
    with _watch_lock:
        if _watch_thread and _watch_thread.is_alive():
            return
        _watch_thread = threading.Thread(
            target=_watch, args=(player,), name="openclaw-chat-watch", daemon=True
        )
        _watch_thread.start()


def _act_read() -> str:
    with _job_lock:
        pending = _job_question
        waited = int(time.monotonic() - _job_started) if pending else 0

    if pending:
        note = (
            f"OpenClaw is still working on '{pending[:80]}' — {waited} seconds so "
            "far. I will read that answer out the moment it arrives."
        )
        if not _last_answer:
            return note
        return note + " The previous answer was:\n" + spoken_answer(_last_answer)

    if not _last_answer:
        recovered = _stored_answer()
        if recovered:
            globals()["_last_answer"] = recovered
            print(f"[OpenClaw] recovered the last answer from its session store "
                  f"({len(recovered)} chars)")
            return spoken_answer(recovered)
        return "OpenClaw has not answered a question in this Lumina session yet."
    return spoken_answer(_last_answer)


def _act_close() -> str:
    global _bridge_connected, _transport
    _ws_close()
    _stop_owned_gateway()
    _bridge_connected = False
    _transport = ""
    return "Disconnected from OpenClaw."


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Execute an OpenClaw bridge action and always return speech-safe text."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "ask").strip().lower()
    text = str(parameters.get("text") or "").strip()
    if action not in _ACTIONS:
        action = "ask" if text else "connect"

    try:
        timeout = max(30, min(int(parameters.get("timeout") or _DEFAULT_ANSWER_TIMEOUT), 600))
    except (TypeError, ValueError):
        timeout = _DEFAULT_ANSWER_TIMEOUT

    if player:
        try:
            player.write_log(f"[OpenClaw] {action}")
        except Exception:
            pass

    try:
        if action in ("connect", "open"):
            result = _act_connect(player)
        elif action in ("close", "disconnect"):
            result = _act_close()
        elif action == "read":
            result = _act_read()
        else:
            result = _act_ask(text, timeout, player)
    except Exception as exc:
        print(f"[OpenClaw] {type(exc).__name__}: {exc}")
        return f"The OpenClaw bridge failed: {exc}"

    print(f"[OpenClaw] {action} -> {result[:120]}")
    if player:
        try:
            player.write_log(f"LUMINA: {result[:200]}")
        except Exception:
            pass

    return result