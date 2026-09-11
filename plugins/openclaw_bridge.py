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
import threading
import time
from typing import Any


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

# A runaway guard, not an editorial limit: a real OpenClaw answer that got cut
# at 1500 characters was the user asking a question and receiving its first
# page.
_SPOKEN_LIMIT = 12_000

# Recognised by the "Answering in depth" rule in core/prompt.txt: cover every
# substantial point rather than reducing the answer to a headline.
_ANSWER_IN_DEPTH = "[ANSWER_IN_DEPTH]\n"

_ACTIONS = {"ask", "connect", "open", "read", "close", "disconnect"}

_bridge_connected = False
_gateway_started_by_bridge = False
_transport = ""
_last_answer = ""

# ── waiting without blocking ─────────────────────────────────────────────────
# OpenClaw takes minutes on a real task, and this bridge used to wait for it
# inside the tool call itself: the turn stayed open, the model received nothing
# until the CLI returned, and anything slower than the timeout was thrown away.
# The user asked a long question and simply never got an answer.
#
# But nothing about a tool response requires the work to be finished. The
# question goes to a background thread and the tool returns at once; when the
# answer finally lands it is pushed into the live session the same way
# phone_notifications announces an arriving message, and Lumina reads it out
# unprompted however long it took. The answer is also kept, so it survives a
# session that dropped while OpenClaw was thinking — action='read' still has it.
_job_lock = threading.Lock()
_job_question = ""            # non-empty while an answer is still being waited for
_job_started = 0.0

# Nothing blocks on this any more, so it guards against a hung CLI rather than
# rationing the user's patience.
_BACKGROUND_TIMEOUT = 1800


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


def _wait_for_answer(question: str, timeout: int, player=None) -> tuple[str, str]:
    """Ask OpenClaw and wait for it, on a thread nothing is blocked on."""
    global _transport

    connected, error = _ensure_connection(player)
    if not connected:
        return "", f"I could not connect to OpenClaw: {error}"

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

    spoken = answer
    if len(spoken) > _SPOKEN_LIMIT:
        spoken = spoken[:_SPOKEN_LIMIT].rsplit(" ", 1)[0] + "…"

    print(f"[OpenClaw] answered after the fact: {len(answer)} chars")
    if player:
        try:
            player.write_log(f"[OpenClaw] answer received ({len(answer)} chars)")
        except Exception:
            pass

    _say(
        player,
        "OpenClaw has just answered the question you sent it a while ago "
        f"('{question[:120]}'). Tell the user its answer has arrived, then give "
        "it to them.\n\n" + _ANSWER_IN_DEPTH + spoken,
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

    # The caller's timeout was sized for a wait someone was sitting through.
    # Nobody is sitting through this one, so it only has to be long enough that
    # a hung CLI is eventually given up on.
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
        return note + " The previous answer was:\n" + _ANSWER_IN_DEPTH + _last_answer

    if not _last_answer:
        return "OpenClaw has not answered a question in this Lumina session yet."
    return _ANSWER_IN_DEPTH + _last_answer


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
