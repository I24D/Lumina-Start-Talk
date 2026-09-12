# Open bug: live voice transcription is unreliable

**Status: not solved.** Claude spent 2026-09-11 into 2026-09-12 on this and
fixed four real faults along the way, none of which was the whole story. This
is the handover: what the user sees, everything that was measured, everything
that was ruled out, and where the evidence points now.

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

Counting `🎧` over one real conversation is the test for nearly everything.

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

## Where the evidence points

The remaining difference from the upstream project that works:

```python
cfg["enable_affective_dialog"] = True                       # upstream: absent
cfg["proactivity"] = ProactivityConfig(proactive_audio=True) # upstream: absent
```

Both are hard-coded on (`main.py`, around line 1248) and both force the
connection onto the **v1alpha** endpoint. Upstream uses neither and, per the
user, streams reliably.

That is a hypothesis, not a finding. It has not been tested, because the user
asked for `proactive_audio` to be kept and it is not Claude's call to remove.
**The experiment is one flag and a stopwatch**: disable `enable_affective_dialog`
alone, count turns to failure; then disable `proactivity` as well, which drops
the session to the stable endpoint, and count again. Three runs each. If the
stable endpoint survives and v1alpha does not, that is the answer, and the
trade-off then belongs to the user.

Ask him before turning `proactive_audio` off, even temporarily. He has said he
wants it.

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
