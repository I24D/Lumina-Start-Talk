# What not to break

Everything below was broken at least once, cost a full day to find, and was
fixed with a measurement rather than an argument. Each item says what the
symptom looked like, because the symptom never points at the cause.

If you are about to reverse one of these, do not reason about it. Run the app,
talk to it, and read the console. The evidence is one session away and it
settles every one of these questions in under a minute.

## Reading the console

Start it with `.venv\Scripts\python.exe main.py` and watch the marks:

| mark | meaning |
|---|---|
| `Connected.` | the live session opened. Without it nothing else can work |
| `🎧` | one fragment of the user's speech, transcribed as they speak |
| `🗣  Heard:` | a complete utterance |
| `💬 replying...` | she began answering, and how long after the user stopped |
| `🔈 Spoke` | audio actually reached the speakers, and for how long |
| `🙉` | the deafness watchdog rebuilt the session |

Counting `🎧` over one real conversation is the test for almost everything
here. Zero of them with the microphone open means the audio is not arriving.

## Before blaming the code

Repeated `timed out during opening handshake` with **zero** `Connected.` is a
network fault, not a bug. A second Wi-Fi adapter took the default route on
2026-09-11 and the whole app looked broken for an hour. Check
`Get-NetRoute -DestinationPrefix 0.0.0.0/0` and open a socket to
`generativelanguage.googleapis.com:443` before touching anything.

## The voice path

### 1. Microphone audio goes to Gemini as `media=`, never `audio=types.Blob`

In `_send_realtime`:

```python
await self.session.send_realtime_input(media=msg)
# msg = {"data": ..., "mime_type": "audio/pcm;rate=16000"}
```

The typed `Blob` path is the documented modern transport and it is the right
answer for most configurations. It is the wrong answer for this one: the
session runs with `proactive_audio` and `enable_affective_dialog`, and with
those enabled the audio never arrives.

**Symptom when broken:** total deafness. The microphone captures, every block
is handed to the session, no exception is raised anywhere, and Gemini
transcribes nothing for the entire life of the process. It looks like a model
problem, a quota problem or a microphone problem. It is none of those.

Measured under `media=`: live fragments in the console and answers 1.1–3.9 s
after the user stops speaking. Broken in `137714b`, restored in `3eb8a12`.

### 2. The deafness watchdog may only rebuild a session that has never heard anything

`self._heard_this_session` counts transcriptions since the session connected.
The watchdog fires only when **all** of these hold:

- the count is zero — a session that has transcribed once is listening, by
  proof rather than inference, and must never be torn down on suspicion;
- the session is older than `_DEAF_MIN_SESSION` (90 s) — every brand-new
  session has heard nothing yet, healthy ones included;
- more than `_DEAF_VOICE_SECONDS` (20 s) of clear speech was forwarded;
- that speech was not her own voice still arriving after she stopped
  (`_DEAF_ECHO_TAIL`), and no tool was running.

It exists for one measured failure: a session that connects, answers typed
text, and never transcribes one word for its whole life. It has now produced
false positives three times, and each one destroyed a session the user was
mid-conversation in. **If it fires on a working session again, delete it
rather than tune it a fourth time.** The cure has been worse than the disease.

### 3. `Listening...` must never overwrite words already on the live line

The local voice detector announces a speech start after any pause longer than
640 ms, and 640 ms is an ordinary gap between two phrases. Writing the
placeholder on every announcement cleared the transcript as fast as it filled.
`self._live_has_text` guards it; a finished turn clears the flag.

**Symptom when broken:** the LIVE TRANSCRIPT panel sits on `Listening...` for a
whole sentence while she answers the user correctly — so it reads as "she
hears me but does not write it", which sounds impossible and is not.

### 4. The microphone is closed while she speaks, so response length is a latency feature

The callback in `_listen_audio` forwards nothing while `self._is_speaking`.
That is correct — without it she hears herself through the speakers and acts on
her own words — and it has a price: every second she talks is a second the user
cannot reach her. Measured in one two-minute session: 71 seconds speaking, in
turns of up to 21, and none of the user's speech in those 71 seconds was
transcribed.

Two consequences:

- Never loosen the `Length:` rule in `core/prompt.txt`. Verbosity is not a
  style question here; it is dead air on the microphone.
- `START_OF_ACTIVITY_INTERRUPTS` is configured but **cannot fire**, because the
  model receives no audio during her speech. Voice barge-in does not work and
  will not until the gate opens. Do not report it as working.

## The ChatGPT desktop bridge

### 5. Copy is requested with `invoke()`, and a sentinel proves it happened

The Copy button belongs to a hover toolbar: it is in the accessibility tree
with `visible=False` over blank screen, so a click lands on nothing. The
failure is silent — the clipboard is read afterwards and returns whatever was
already on it, and this bridge pastes in order to send, so that is usually the
last message sent.

**Symptom when broken:** reading a 1697-character answer returns
`SEGUNDA PRUEBA Codex`. Write `_COPY_SENTINEL` first, invoke, poll for a
change, and fall back to the accessibility text when none comes.

### 6. A tray-hidden window is revived with the shell activation verb, never `ShowWindow`

`ShowWindow(SW_SHOW)` paints the window without giving it back its input queue.
UIA still reads the whole conversation and a screenshot still shows it, while
every click and keystroke is dropped and the composer reports
`has_keyboard_focus` False. The shell activation verb — what the tray icon
itself uses — restores it properly.

**Symptom when broken:** the message is pasted into nothing, the wait runs its
full timeout for an answer to something never sent, and the assistant is silent
for minutes.

Reading needs no activation at all: a hidden window's tree gives up every reply.

### 7. `ask` returns immediately and speaks the answer later

Blocking the turn for the length of another system's answer is what froze her:
two calls at 120 s each, after which the live session had dropped its
resumption handle. `_act_ask` starts a thread and returns at once, and
`_deliver` speaks the result under `[DELAYED_ANSWER]`. OpenClaw's bridge works
the same way and for the same reason. Copilot's does not yet.

## Prompt markers

`core/prompt.txt` recognises bracketed markers at the start of injected text:
`[SYSTEM_ALERT]`, `[STARTUP_BRIEFING]`, `[ANSWER_IN_DEPTH]`, `[DELAYED_ANSWER]`.
Text injected without one lands as an ordinary user turn and the model answers
it as conversation — a delayed answer sent bare came back as "I have already
put the question to it and am waiting", in the wrong language.

## Two habits that would have saved the day

**Do not reverse a comment that records a measurement.** `media=` was changed to
`audio=Blob` on the grounds that `media=` is deprecated. Two commits earlier the
same file recorded, in a comment, that `audio=` does not arrive with this
configuration. Both cannot be true, and a single run would have said which.

**Keep the log.** `pythonw.exe` from `Abrir LUMINA.bat` writes nowhere, so a
crash leaves no trace. When something needs diagnosing, start it with
`python.exe` and redirect stdout, or the next hour is spent guessing.
