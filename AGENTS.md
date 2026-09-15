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
| `[VOICE REC]` | every 15 s: whether the server, the microphone, the send path and this event loop are each still alive. Read it before forming any theory about a silence — see VOICE-BUG.md |

Counting `🎧` over one real conversation is the test for almost everything
here. Zero of them with the microphone open means the audio is not arriving.

## Before blaming the code

Repeated `timed out during opening handshake` with **zero** `Connected.` is a
network fault, not a bug. A second Wi-Fi adapter took the default route on
2026-09-11 and the whole app looked broken for an hour. Check
`Get-NetRoute -DestinationPrefix 0.0.0.0/0` and open a socket to
`generativelanguage.googleapis.com:443` before touching anything.

## The voice path

### 1. Gemini 3.1 Flash Live takes the microphone as `audio=`, and text as realtime input

In `_send_realtime`, for Gemini:

```python
await self.session.send_realtime_input(
    audio=types.Blob(data=msg["data"], mime_type=msg["mime_type"])
)
```

Until 2026-09-14 Lumina ran `gemini-2.5-flash-native-audio-preview-12-2025`,
and on that model the rule was the opposite: `media=`, because `audio=` arrived
nowhere while proactive audio and affective dialog were on (broken in
`137714b`, restored in `3eb8a12`). 3.1 closes the session on `media_chunks`
with 1007 "media_chunks is deprecated".

3.1 also takes `send_client_content` only to seed the start of a session. Every
text turn in the middle of a conversation goes through `_send_text_turn`, which
sends `send_realtime_input(text=...)`; the photo turn sends the image as
`video=` and then the question as text. The OpenAI session keeps `media=` and
`send_client_content`, which it maps itself. Do not route it through the Gemini
calls.

**Why 3.1, measured:** the same recorded Spanish question, streamed straight
into each model with no microphone and no Lumina in between, came back as text
1.6 s after the speech ended on 3.1 in three runs of three, against 12–14 s on
2.5, where one run of three never answered in 40 s. First reply audio: 1.6–1.7 s
against 15–16 s. Neither model sent `interim_input_transcription`, so on either
one the words appear when the user stops, not while he is still speaking.

3.1 does not support asynchronous (`NON_BLOCKING`) function calls, affective
dialog or proactive audio. On 2026-09-05 a regional slowdown of 3.1 was reported
from EU traffic (first audio 16–26 s); replies that suddenly take that long are
the service, not this code.

### 1b. Do not set `realtime_input_config`. The upstream project does not

This one cost a day. `_build_config` must not pass a `RealtimeInputConfig`, and
in particular not `turn_coverage=TURN_INCLUDES_ONLY_ACTIVITY`, which admits
only the audio the server's own detector marks as activity and discards the
rest. When that detection misfires the user's whole sentence is thrown away
before the model sees it.

**Symptom when broken:** a session that is born deaf and stays deaf. It
connects, answers typed text, moves its meters, detects speech locally — one
session logged thirty-one utterances captured and sent — and returns zero
transcriptions for its entire life. Intermittent, because it depends on how
the server's detector happens to behave that session, which is why it survived
so many attempted fixes: every one of them was aimed at something that was
working.

The same applies to the client sending `audio_stream_end` after each pause.
That tells the server the audio stream is over; the local detector was firing
on the room's own noise floor (measured 0.23 against its 0.20 threshold) and
announcing the end of the audio dozens of times a minute. Neither is needed:
the service's own turn-taking has handled this since the first commit.

Compare against `git show $(git rev-list --max-parents=0 HEAD):main.py` before
adding anything to that config. Upstream sends `{"data": ..., "mime_type":
"audio/pcm"}` and nothing else.

### 1c. Nothing else may open this microphone

Windows speech recognition was run beside the session to show the user his
words while he spoke. It opens the capture device itself, turns the device
volume down with its own gain control, and every other stream on that device —
including the one the model is fed — reads a flat 0.5 RMS for as long as it
runs. Measured median 88.7 RMS without it and 1.0 with it, in either start
order.

**Symptom when broken:** the assistant goes nearly deaf and nothing notices.
The local detector and the deafness watchdog read the same level, so a whole
session logs a peak of 0.00, no speech detected and no rebuild, while the odd
sentence still gets through between recogniser restarts — which makes it look
intermittent. It was removed for this reason. Anything that wants the audio
takes it from `_listen_audio`, never from the device.

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

Three consequences:

- Never loosen the `Length:` rule in `core/prompt.txt`. Verbosity is not a
  style question here; it is dead air on the microphone.
- On **headphones** the gate is off entirely (`self._full_duplex`, decided from
  the output device's name), the microphone never closes, and
  `START_OF_ACTIVITY_INTERRUPTS` can fire — she can be talked over. On speakers
  it cannot, because her echo was measured returning at up to full scale, and
  barge-in there needs real echo cancellation. Do not report barge-in as
  working without saying which of the two you measured.
- The button says which state she is in, and it must keep telling the truth.
  "SPEAKING — MIC OFF" and "WORKING — STILL LISTENING" are different claims:
  waiting on another assistant does **not** deafen her, the audio still
  reaches the model and is transcribed, it simply cannot be answered until the
  tool returns. Do not collapse those two into one "busy" state — the user's
  whole complaint was never knowing whether talking was worth it.

### 4b. Tool calls run beside the receive loop, for both providers

`_receive_audio` hands each tool call to `_answer_tool_calls` as a task and
keeps reading. It used to await the tool itself, so for as long as a search, a
file or another assistant took, nothing from the model was read — no
transcript and no voice. The result is dropped, not sent, if the session it
belongs to has closed meanwhile.

## The OpenAI voice path

Selected in CUSTOMIZE as the voice engine, or `voice_provider: "openai"` in
`config/api_keys.json`. Gemini stays available beside it and none of what
follows touches its path.

### 4c. The desktop talks to OpenAI over the WebSocket, not WebRTC

aiortc runs RTP in Python and shares the interpreter lock with the HUD, which
keeps about one core busy painting. Measured in the real app on 2026-09-14:
her voice chopped 289 times in 20 s (274 with audio already queued), the
user's words arrived as nonsense ("bilibili Eura esa ora" for "Hola Lumina,
¿qué hora es?") and the call dropped. Beside a thread holding the lock the same
way, WebRTC delivered no audio in 16 s; the WebSocket delivered 20 s and the
player never ran dry, because the server sends audio ahead of real time.
WebRTC stays in the Learning English tutor, where Chrome runs it natively; its
offer goes to `/v1/realtime/calls` as multipart `sdp` and `session` fields.

**Symptom when broken:** choppy speech, garbled transcripts and `OpenAI audio
track ended` reconnects, with `lag` in the hundreds of milliseconds.

### 4d. The echo canceller hears the speaker on the writer thread, and the speaker never runs empty

`core/echo_canceller.py` wraps livekit's WebRTC audio processing, locally, no
server. Two things decide whether it works on these speakers, both measured:

- The reference is fed in `_play_audio`'s `_write`, on the writer thread,
  immediately before `stream.write`. Fed from the event loop, sessions measured
  -1 to -9 dB and interrupted themselves; fed there, -17.8 dB.
- While she is silent the player writes 100 ms of silence and feeds it as the
  reference. An emptied speaker buffer changes how late her voice is heard by
  her next answer, which leaked at -3 to -5 dB and swallowed the user's words
  in the pause; with silence written, -9 dB and the words came through.

### 4e. The microphone opens during an answer only once the canceller proves itself in it

Every answer starts with the microphone closed, exactly as on speakers before.
It opens for the rest of that answer when the canceller has removed at least
15 dB of her voice over at least 1 s of it (`OPEN_BELOW_DB`,
`OPEN_AFTER_SECONDS`), and it does not close again until she stops: when the
user talks over her the numbers look like a failing canceller, and closing then
would shut out the interruption. `[VOICE REC]` prints `aec -NNdB open|gated`.

On this laptop the canceller needs part of each answer to find her echo again,
so there is no barge-in in an answer's first second or so. That is the price of
never hearing herself; the INTERRUPT button still works throughout.

**Symptom when broken:** she cuts herself off a second or two into an answer
(`✋ You spoke over her` with nobody speaking) and the log shows her own words
as the user's, in any language — "Nie.", "那是好事。", "Hello.".

### 4f. A turn is what the user finished saying, not what the server cancelled

- A pause mid-sentence commits the audio and starts a response that is
  cancelled, with no output, when the user carries on. It is not a turn.
- `turn_complete` waits up to 3 s for the user's transcription, which can land
  after her answer has started.
- `semantic_vad` runs with `eagerness: "low"`: on `auto` she began answering
  "Hola Lumina." while the question after it was still being asked.
- On a socket the server cannot know how much of her answer was played, so a
  barge-in sends `conversation.item.truncate` with an estimate.

### 4g. Synthetic speech through the same speakers does not test the user's voice

Playing a recorded phrase through the speakers is a fair test of her echo and
of the Gemini path. With the canceller on it is not a test of the user: the
canceller is learning to remove exactly what comes out of those speakers, and
such phrases arrived mangled ("不要!") or not at all. Only a person talking
proves the user side.

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
