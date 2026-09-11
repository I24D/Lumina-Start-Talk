"""
Two-way bridge from Lumina Start Talk to OpenClaw.

The endpoint and lifecycle below were verified against the local
``I24D/Lumina-Openclaw`` checkout at ``C:\\I24D_WhatsApp\\openclaw-main``:

* The gateway listens on ``ws://127.0.0.1:18789``. Its HTTP dashboard is served
  from ``http://127.0.0.1:18789/``; agent calls use the WebSocket protocol, not
  an HTTP chat route.
* Authentication mode is ``token``. The token lives in
  ``~/.openclaw/openclaw.json`` under ``gateway.auth.token`` and must never be
  copied into this project. The official CLI reads it automatically.
* On Windows the gateway is registered as the ``OpenClaw Gateway`` Scheduled
  Task. ``openclaw gateway start`` starts it; the installed task may take a
  while to open port 18789 while its agent databases initialize.
* ``openclaw agent --json`` sends a turn through the gateway and returns spoken
  text in ``payloads[].text``. If the gateway cannot start and no other gateway
  owns the state directory, ``openclaw agent --local`` is the supported local
  fallback and uses the same configured agent, model, and credentials.

This module intentionally invokes the OpenClaw CLI. It never automates the
OpenClaw dashboard or any desktop window, so it does not steal keyboard focus
and does not depend on UI layout.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from typing import Any


PLUGIN = {
    "name": "openclaw_bridge",
    "description": (
        "Talks directly to OpenClaw through its official CLI: connects to or starts its gateway, "
        "sends a question, waits for OpenClaw's answer, and returns that answer to be read aloud. "
        "Use this for ANY request involving OpenClaw or Open Claw. Do NOT use open_app or "
        "computer_control for these requests; those tools cannot communicate with the gateway or "
        "read its reply. "
        "action='ask' (default) sends the text and returns OpenClaw's answer: 'pregúntale a Open "
        "Claw qué puede hacer', 'dile a Open Claw que...', 'ask Open Claw what it can do', "
        "'tell OpenClaw to...'. Put the question itself in 'text'. "
        "action='connect' prepares the connection without asking anything: 'conéctate con Open "
        "Claw', 'conéctate a Open Claw', 'connect to Open Claw', 'connect to OpenClaw'. "
        "action='read' repeats the most recent answer: 'léeme la respuesta de Open Claw', "
        "'qué respondió Open Claw', 'read Open Claw's answer', 'what did OpenClaw say'. "
        "action='close' disconnects the bridge: 'desconéctate de Open Claw', 'cierra la conexión "
        "con Open Claw', 'disconnect from Open Claw', 'close the OpenClaw connection'."
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
                "description": "The question or message to send to OpenClaw. Required for action='ask'.",
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
_SPOKEN_LIMIT = 1500
_ACTIONS = {"ask", "connect", "open", "read", "close", "disconnect"}

_bridge_connected = False
_gateway_started_by_bridge = False
_transport = ""
_last_answer = ""


def _cli_path() -> str | None:
    """Return the installed OpenClaw CLI path without assuming a repo checkout."""
    names = ("openclaw.cmd", "openclaw") if platform.system() == "Windows" else ("openclaw",)
    for name in names:
        path = shutil.which(name)
        if path:
            return path
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
    for index, character in enumerate(output):
        if character != "{" or (index > 0 and output[index - 1] not in "\r\n"):
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _nested_dicts(value: Any):
    """Yield every dictionary in a decoded response, including wrapped results."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_dicts(child)


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
        if _transport == "local" or _gateway_health()[0]:
            return True, ""
        _bridge_connected = False
        _transport = ""

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
            return True, ""
        start_error = health_error or start_error
        _stop_owned_gateway()

    if any(marker in start_error.lower() for marker in ("state ownership", "already running")):
        healthy, health_error = _wait_for_gateway(_GATEWAY_OWNERSHIP_WAIT)
        if healthy:
            _bridge_connected = True
            _transport = "gateway"
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
            texts = [
                str(item.get("text", "")).strip()
                for item in items
                if isinstance(item, dict) and str(item.get("text", "")).strip()
            ]
            if texts:
                return "\n".join(texts), ""
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
    if _transport == "gateway":
        return "Connected to the OpenClaw gateway."
    return "Connected to OpenClaw through its local CLI fallback."


def _act_ask(question: str, timeout: int, player=None) -> str:
    global _last_answer, _transport
    if not question:
        return "What would you like me to ask OpenClaw?"

    connected, error = _ensure_connection(player)
    if not connected:
        return f"I could not connect to OpenClaw: {error}"

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

    if not answer:
        return f"OpenClaw could not answer: {error}"

    _last_answer = answer
    return answer


def _act_read() -> str:
    if not _last_answer:
        return "OpenClaw has not answered a question in this Lumina session yet."
    return _last_answer


def _act_close() -> str:
    global _bridge_connected, _transport
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

    if len(result) > _SPOKEN_LIMIT:
        shortened = result[:_SPOKEN_LIMIT].rsplit(" ", 1)[0]
        return shortened + "... That is as far as I will read aloud."
    return result
