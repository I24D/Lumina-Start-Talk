# Open bug: live voice transcription is unreliable

**Status: not solved.** Claude spent 2026-09-11 into 2026-09-12 on this and
fixed four real faults along the way, none of which was the whole story. This
is the handover: what the user sees, everything that was measured, everything
that was ruled out, and where the evidence points now.

**Read `[VOICE REC]` first.** The 2026-09-12 pass added a flight recorder that
answers, every fifteen seconds, the four questions this bug has always turned
on. Until then every silence had to be argued about, and every argument about
it was wrong. See *The flight recorder* below.

Read it before changing anything. Most of a day was lost to fixes aimed at
parts that turned out to be working perfectly.

## What the user needs

Exactly this, in his words: he speaks, **his words appear in the chat as he
says them**, and only then does she read them and answer. Live, every time,
regardless of what she is doing — reading a long answer from ChatGPT, waiting
on Copilot, anything. That is the definition of a voice assistant and it is
not negotiable.

## What actually happens

It works, sometimes for many turns in a row, and then stops. Once it stops it
never recovers on its own. Restarting the app fixes it, for a while.

The best run measured after the fixes below: **seventeen consecutive spoken
turns**, each transcribed word by word, fragments landing about every 150 ms
while he was still speaking, including two turns that dispatched work to
ChatGPT and Copilot. Then it stopped.

## How to see what is happening

The app already prints everything needed. Start it so the output is kept —
`Abrir LUMINA.bat` uses `pythonw.exe` and writes nowhere, which is its own
trap:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "main.py" `
  -WorkingDirectory "C:\Lumina Start Talk" -RedirectStandardOutput lumina.log `
  -RedirectStandardError lumina.err -WindowStyle Hidden
```

| marker | meaning |
|---|---|
| `⇢ N audio blocks sent` | audio really reached the model (once a minute) |
| `⛔ audio send failed` | the send task died — nothing would ever be heard again |
| `🎧 HH:MM:SS.mmm text` | one transcript fragment, timestamped |
| `🗣  Heard:` | a complete utterance |
| `♻️ receive stream ended — opening #N` | one receive stream finished, next opened |
| `💀 nothing from the server for Ns` | the dead-session watchdog fired |
| `👋 server is ending the session` | the server sent `go_away` |
| `[UI] main thread stalled Ns` | the interface froze |
| `[VOICE REC] ...` | the flight recorder, once every 15 s — see below |

Counting `🎧` over one real conversation is the test for nearly everything.

## The flight recorder

Four faults produce the one symptom the user reports — he talks and nothing
comes back — and for a day none of them could be told apart, because none of
the four was ever printed. `[VOICE REC]` prints all four, every fifteen
seconds, for the life of every session:

```
[VOICE REC] 2:32 | srv 0.2s ago | rx audio×61 heard×12 turn×1 | mic 234 peak 0.52 | snd 234 q0 | play q2 | lag 14ms
```

| field | what it answers |
|---|---|
| `srv` | how long since the receive stream yielded **anything at all** |
| `rx` | what it yielded, by kind: `heard` `interim` `said` `audio` `turn` `tool` `resume` `usage` `other` |
| `mic` | blocks the device delivered, counted **before** the speaking gate |
| `peak` | loudest block actually forwarded — was anyone talking |
| `snd` | blocks that reached the model, and the depth of the send queue |
| `play` | chunks of her voice waiting for the sound card |
| `lag` | worst overshoot on a 250 ms sleep — this event loop being held |

Read one line and the silence names itself:

- **`rx` empty, `mic` counting, `peak` high.** He is speaking, the audio is
  leaving, the server has stopped answering. Nothing local is wrong and
  nothing local will fix it.
- **`rx` carrying `usage` or `resume` but no `heard`.** The session is being
  served and is not transcribing. That points at the configuration it was
  opened with, not at the network.
- **`lag` in the hundreds of milliseconds.** Something is holding the event
  loop, and every other number on the line is late rather than true. That one
  is a bug in this process.
- **`snd` far below `mic`, or `drop` appearing.** Audio is captured faster than
  it is sent, so the model is answering the past.

It only watches. It sends nothing and rebuilds nothing — three watchdogs have
now destroyed conversations that were working, and an instrument that can do
that is not an instrument.

## The failure, measured

Captured with all of the above in place, at the moment it stopped:

```
last transcription:     00:26:31
receive streams:        stuck at #18, never advanced again
audio sent:             4300 blocks, 275 seconds, uninterrupted
local VAD:              still detecting the user speaking
errors:                 none.  UI stalls: none.  go_away: never sent
```

The receive loop is parked inside `session.receive()` on a stream that **does
not yield, does not end, and does not raise**. Before that it had advanced
after every single turn — #16, #17, #18 — so reopening works normally. The
socket stays open, audio keeps being accepted block after block, the meters
move, and nothing ever comes back.

There is nothing inside the process that can see this. That is why it survived
so many attempts.

## Ruled out, with measurements — do not re-investigate

- **The microphone.** Captures throughout. Opened explicitly at 16 kHz; a
  device that cannot do that raises rather than silently resampling.
- **The audio send path.** Instrumented: 4300 blocks delivered to
  `send_realtime_input` with no exception while the failure was happening.
- **The interface thread.** A 500 ms heartbeat reported zero stalls across the
  whole failure.
- **The UI signal chain.** Instrumented at the slot: it receives each fragment
  immediately (`🎧 20.808 "O"` → UI at once, `20.968 "kay"` → UI at once).
- **The model.** `gemini-2.5-flash-native-audio-preview-12-2025`, identical to
  upstream.
- **The receive-loop shape.** `while True: turn = session.receive(); async for
  …` is exactly what upstream does.
- **`go_away`.** Now handled, and it was never sent before a death. The server
  gives no warning.
- **The network.** Worth re-checking each time (see AGENTS.md) but it was not
  the cause here: audio kept being accepted throughout.

## Fixed along the way — keep these

Four separate faults, all producing the same symptom, which is why each fix
looked like it worked and then "failed again". It was not failing again; it
was the next one.

1. **`realtime_input_config` with `turn_coverage=TURN_INCLUDES_ONLY_ACTIVITY`**
   (added by an earlier commit, removed in `b2e9de0`). It admits only the
   audio the server's own detector marks as activity and discards the rest, so
   a misfire threw away whole sentences before the model saw them. Upstream
   sets no `realtime_input_config` at all. **Do not add one back.**
2. **Client-side `audio_stream_end` after every 640 ms pause.** It announced
   the end of an audio stream that had not ended, dozens of times a minute,
   triggered by the room's own noise floor. Removed.
3. **The typewriter animation owned the log document** while it ran, and the
   live transcript was stashed until it let go — several hundred characters at
   6 ms each. The user spoke, saw nothing, spoke again, then watched every
   sentence arrive at once. It now gives way instead.
4. **`Listening...` was written over words already on the line**, on every
   pause longer than 640 ms — which is every gap between two phrases.

Also: audio goes as `media=`, never `audio=types.Blob` — see AGENTS.md item 1,
that one deafens the session completely.

## The watchdogs, and a warning about them

Two exist, and both have already caused damage by firing on healthy sessions.
A third false positive should be answered by deleting the watchdog, not by
tuning it a fourth time.

- **Speech watchdog** (`🙉`): fires only when a session has *never* transcribed
  anything, is older than 90 s, and has had 20 s of clear speech. It has been
  wrong three times; the bars are high for that reason.
- **Dead-session watchdog** (`💀`): fires when the session **has** transcribed
  before, the user has spoken since the last received message, and the server
  has said nothing at all for 25 s. Its first version armed on room noise and
  put a brand-new idle session into a connect-kill-connect loop — see
  `bae0584`. A healthy session answers in about three seconds, so total
  silence after real speech is a fact, not an inference. Keep it that way.

`_DEAF_VOICE_LEVEL` is 0.40 because this room was measured at **0.231 average,
0.334 peak** with nobody talking. Anything lower counts an empty room as
speech. Measure before changing it.

## The 2026-09-12 session, measured

`enable_affective_dialog` and `proactive_audio` are gone (`162f11d`), the
transport is v1beta, and **the fault survived all three**. That closes the
hypothesis the previous handover ended on. What one full session
(`lumina-baseline-20260912-011819.log`, eight minutes) actually shows:

- **Four consecutive turns were perfect.** Fragments every 120–180 ms while he
  spoke, replies 1.2–1.9 s after he stopped.
- **Then both directions slowed at once.** The turn at 01:20:12 delivered 5.7 s
  of *her own* speech over roughly 14 s of wall clock. The very next stretch
  returned no transcript for 114 s, then fragments 5–20 s apart: `Per` at
  01:22:07, `fect` five seconds later, `o` ten seconds after that. One sentence
  took over a minute and came back truncated.
- **It recovered twice.** 01:23:45 and 01:26:19 were real-time again, at full
  speed, with no reconnect in between.

That the *output* degraded in the same turn as the input is the new fact. This
is not the microphone, not the transcription, and not the interface: the whole
session slows down and speeds back up.

**Ruled out by this run, additionally:**

- **Session resumption.** This was the first connect of the process, so
  `handle` was `None` and the configuration was upstream's exactly, apart from
  one field. It degraded anyway.
- **A local audio backlog.** 7000 blocks of audio left the process in 470 s of
  wall clock — real time, throughout the failure.

Note that the old `⇢ N audio blocks sent (64s)` line could never have shown a
backlog: the seconds were arithmetic on the block count, so they read `64s` per
1000 blocks whether the stream was live or an hour behind. It now prints the
wall clock beside them.

## Where the evidence points

Against the first commit, which the user reports streamed reliably, the whole
of what this session now asks for and that one did not is:

```python
context_window_compression=ContextWindowCompressionConfig(sliding_window=...)
speech_config=SpeechConfig(...)                       # the chosen voice
tools=[... + self._plugin_registry.get_tool_declarations()]
session_resumption=SessionResumptionConfig(handle=...)  # ruled out above
```

Of those, **context window compression is the only one whose cost grows with
the length of the session**, and session length is the axis this fault lives
on: it works for four minutes and then does not. That is a suspect, not a
finding — the arithmetic says a native-audio session should not be near the
compression trigger in eight minutes, so it may well be innocent.

**The experiment is one variable and a stopwatch.** It needs no edit:

```powershell
$env:LUMINA_COMPRESSION="off"; .venv\Scripts\python.exe -u main.py
```

The session line says which arm ran (`context compression: on|off`). Talk for
fifteen minutes under each and compare the `[VOICE REC]` lines. If compression
is the cause, the cost of removing it is that a very long conversation ends in
a reconnect rather than being compressed in place — a few seconds, and session
resumption already carries the conversation across it.

If both arms degrade identically, compression is innocent and the recorder will
say what to look at next. Do not change a second variable before that answer.

## Known and unfixed, separate from the above

- **Copilot's bridge still blocks the turn** while it waits, unlike ChatGPT's
  and OpenClaw's, which return immediately and speak the answer on arrival.
- **On speakers the microphone closes while she speaks**, so a 30-second
  reply is 30 seconds of not hearing him, and voice barge-in cannot work —
  `START_OF_ACTIVITY_INTERRUPTS` is configured but never receives audio to
  fire on. On headphones the gate is off entirely and both problems vanish.
  Full duplex on open speakers needs real echo cancellation: her own voice was
  measured returning into this microphone at up to full scale.
- **The memory summariser hits a 429 quota** on the free tier
  (`gemini-3.8-flash`, 20 requests a day). Unrelated to voice, fails daily.

## One last thing

Every number here came from running it and reading the output, not from
reasoning about the code. Every time this bug was reasoned about instead, the
conclusion was wrong. Instrument first.
