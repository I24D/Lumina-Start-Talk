# LUMINA START TALK
### Voice-first desktop assistant — part of the **Lumina IA** family

**Principal developer:** **Dal Nijaruq** ([@I24D](https://github.com/I24D)) · LUMINA IA

[![Part of Lumina IA](https://img.shields.io/badge/Lumina%20IA-family-0aa?style=flat-square)](https://github.com/I24D/Public-Lumina)
[![Licence: CC BY-NC 4.0](https://img.shields.io/badge/licence-CC%20BY--NC%204.0-lightgrey?style=flat-square)](LICENSE)
[![Contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)

A real-time voice AI that can hear, see, understand and control your computer. It talks through **Gemini 3.1 Flash Live** or **OpenAI Realtime** (`gpt-realtime-2.1`), whichever you choose, and runs on Windows, macOS and Linux; the bridges to other desktop apps (Copilot, ChatGPT, Phone Link) are Windows-only.

---

## 🌐 The Lumina IA family

Lumina Start Talk is the **desktop voice assistant** of a wider platform built by Dal Nijaruq.
Each project stands alone, and together they form one assistant across the places you work.

| Project | What it is |
|---|---|
| **Lumina Start Talk** (this repo) | Voice-first desktop assistant — real-time speech, screen vision, system control, phone notifications |
| [Lumina Code](https://github.com/I24D/Lumina_Code) | AI coding agent for VS Code on Windows — native voice, bounded autonomous goals, MCP |
| [Lumina OpenClaw](https://github.com/I24D/Lumina-Openclaw) | Gateway of the family — routing and integrations |
| [Public Lumina](https://github.com/I24D/Public-Lumina) | Architecture, design decisions and roadmap for the platform |
| [Lumina Novela](https://github.com/I24D/Lumina-Novela) | *La promesa de un sueño* — the bilingual novel behind the name |

---

## 🤝 Contributing

Contributions are welcome — from anyone, in **English or Spanish**. Bug reports, new plugins,
translations and documentation all help. The plugin system means you can add a whole new skill
without touching the core: one file in `plugins/`.

Start with **[CONTRIBUTING.md](CONTRIBUTING.md)**, open an
[issue](https://github.com/I24D/Lumina-Start-Talk/issues) or join the
[discussions](https://github.com/I24D/Lumina-Start-Talk/discussions).

---

## 📜 Credits & licence

Lumina Start Talk is a modified version of **[MARK LII](https://github.com/FatihMakes/Mark-LII)**,
created by **[FatihMakes](https://github.com/FatihMakes)** and released under
**[CC BY-NC 4.0](LICENSE)**. Full credit for the original engine goes to them.

**Changes made in this fork** (the commit history has the full record):

- Renamed and re-themed as Lumina Start Talk, spoken to in Spanish and English.
- A second voice engine, OpenAI Realtime, beside Gemini, with local echo cancellation so the
  microphone can stay open on speakers; the Gemini voice moved to 3.1 Flash Live.
- Bridges to Microsoft Copilot, the ChatGPT desktop app and OpenClaw, and a reader and writer
  for the Codex and Claude Code chats in VS Code.
- A `phone_notifications` plugin that reads and clears the notifications mirrored from the
  phone through Windows Phone Link (Enlace Móvil).
- Learning English, a browser studio that teaches English from zero to C2.
- Copilot-style Live Vision for a window, the whole screen or the camera.
- Supabase-backed memory and conversation history, Tavily search and an API KEYS panel.
- Reliability work on the live voice session, with the measurements behind it in `AGENTS.md`
  and `VOICE-BUG.md`.

The CC BY-NC licence carries over to this project: **use it freely, but not commercially**, and
keep the attribution to FatihMakes if you build on it.

---

## ✨ Overview

LUMINA is a personal assistant that becomes *yours*. Pick the engine and the voice it speaks with, and tune the colour of the whole HUD. The interface breathes with you too — the waveform and core pulse to your **real** voice while you speak and to LUMINA's own voice while it answers.

It combines a plugin engine you can extend without touching the core, two real-time voice engines, and conversations that can run for hours.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎓 Learning English | Browser studio for children, teens and adults, from zero English to C2: live voice lessons on Gemini or OpenAI, placement test, CEFR route, scenarios, guided reading, corrections and FSRS reviews |
| 🎙️ Voice Engines | Gemini 3.1 Flash Live with 5 voices or OpenAI Realtime with 10, chosen and switched live from the UI — no restart |
| 🔇 Echo Cancellation | On OpenAI Realtime, WebRTC's echo canceller runs locally, so the microphone stays open while she speaks and you can talk over her |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — applied instantly across every panel |
| 〰️ Reactive HUD | Waveform and reactor core pulse to real audio — your mic while listening, LUMINA while speaking |
| 🧠 Recallable Memory | No size limit and nothing silently forgotten — the prompt carries what fits, the rest is looked up on demand from a local search |
| 👁️ Memory Panel | See every fact LUMINA has stored about you, when it learned it, and delete any of it in one click |
| ↩️ Undo | Take back what the assistant did — files it moved, renamed, created or wrote, and settings it changed |
| ⚠️ Real Confirmation | Shutdown, restart and WiFi wait for a button **you** press — the model cannot confirm its own irreversible actions |
| 🎧 Audio Device Picker | Choose the microphone and speakers by name, filtered to the short list your OS shows — and measured, so every entry actually works |
| 🔗 Session Continuity | A dropped connection, a voice change or a device change no longer wipes the conversation |
| 🧩 Plugin System | Drop a single `.py` file into `plugins/` — LUMINA learns a new skill on next launch |
| 🎙️ Real-time Voice | Conversation in any language; on Gemini 3.1 your words come back as text about 1.6 s after you stop speaking |
| 🗣️ Wake Phrase | “Lumina, despierta” or “Lumina activate” brings back an assistant that has stopped answering |
| ♾️ Unlimited Sessions | Sliding-window context compression — one conversation can last for hours |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🏗️ Dev Agent | Builds a multi-file project from a description: plans it, writes the files, installs dependencies, opens VS Code, runs it and fixes errors |
| 🤝 Assistant Bridges | Asks Microsoft Copilot, the ChatGPT desktop app or OpenClaw for you and reads the answer out loud, in full up to 2,000 words |
| 🧑‍💻 Codex & Claude Code | Reads and writes the Codex and Claude Code chats in VS Code, and tells you when they finish a task |
| 📱 Phone Notifications | Reads and clears the notifications your phone mirrors through Windows Phone Link |
| 👁️ Live Vision | Copilot-style screen sharing and camera vision in the main voice session, with visible source icons, status and STOP control |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini grounded search, then Tavily, then DuckDuckGo; news leads with Google News |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Wallpaper, and organising, cleaning, listing and measuring the desktop |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🪪 Assistant Customization | Change the assistant name, your name, voice, and colour from the UI — takes effect immediately |

---

## 🆕 LUMINA highlights

LUMINA is designed to feel like *your own* machine, with portable behavior and no assumptions about your language or operating system.

### 🎙️ Two voice engines — and the voice you want
Open **⚙ Customise Assistant** and pick the engine and its voice. **Gemini Live** runs on `gemini-3.1-flash-live-preview` with five voices — **Charon, Puck, Kore, Fenrir, Aoede**. **OpenAI Realtime** runs `gpt-realtime-2.1` with ten, **Shimmer** (shown as *Sol*) by default, and needs an OpenAI key in **API KEYS**. The switch is live: the session rebuilds itself the instant you apply, without restarting anything.

The Gemini engine moved from 2.5 native audio to 3.1 Flash Live after both were measured with the same recorded question, streamed straight into each model: 3.1 returned the words 1.6 s after the speech ended and began answering at 1.7 s, where 2.5 took 12–14 s and 15–16 s. Neither Gemini model streams words while you are still speaking; the sentence appears when you stop.

OpenAI's voice travels over a WebSocket, with WebRTC's echo canceller running on this PC through the `livekit` package — no LiveKit server or account. Each answer starts with the microphone closed, and it opens once the canceller has shown it removes her voice, so on speakers you can talk over her. On Gemini the microphone closes while she speaks.

### 🎨 Live Theming — Recolour the Entire Interface
LUMINA starts with a red visual identity inspired by its mascot. Drag the hue wheel (or type an exact hex code) to re-theme the HUD in real time, or use **Night Mode** in the controls drawer for black surfaces with restrained accent lighting. Your choice is saved and restored on the next launch.

### 〰️ Reactive HUD — The Interface Breathes With the Room
The waveform and the core respond to **real audio**, not a random animation. While LUMINA listens, they pulse to your microphone; while LUMINA speaks, they pulse to its own voice — louder speech, taller bars and a brighter, wider core. When the room goes quiet, everything settles back into a gentle idle ripple.

The **🧩 Plugin System** and **♾️ Unlimited Sessions** work together as LUMINA's foundation.

---

## 🔄 Reliable foundation

These improvements form one reliable base across supported operating systems.

No hardcoded language, and nothing in the core that assumes one operating system.

### 🎓 Learning English — a separate teaching workspace

Open **⚙ Controls → 🎓 Learning English** to launch **English Learning Studio**
in the browser, or say “Lumina, quiero aprender inglés”, “Enséñame inglés”,
“Teach me English” or “Let's practice English”.

The class runs its own live voice session inside the studio page, on the engine
chosen under **Motor de voz**: Gemini on `gemini-3.1-flash-live-preview`, or
OpenAI `gpt-realtime-2.1` over WebRTC. Either way it uses the browser's microphone
with echo cancellation, the tutor's own voice and no tools, and the API key never
reaches the browser. For Gemini, Lumina's server mints a single-use ephemeral
token with the model, teacher prompt, voice and transcription locked inside it;
for OpenAI, the browser's WebRTC offer goes through Lumina's server, which holds
the key. While a class is
open, Lumina's general assistant keeps its tools but pauses its microphone and
voice. Each finished student turn is analysed into validated structured
corrections, vocabulary and progress.

**For every learner.** The class adapts to a child (6–12), a teenager (13–17)
or an adult: tone, topics, scenarios and pace change, children are never asked
for personal data, and the studio switches to larger type and stars. Nobody
needs to know any English: the route starts at **Pre-A1 “Desde cero”** (sounds,
first words, greetings) with explanations in the student's own language, and
climbs through an original Lumina curriculum of 21 units up to C2.

**Course tools in the studio:**

- **Tu siguiente paso** — one recommendation decided by the course, not by
  Gemini: the placement test, due reviews, the current unit or the level
  assessment.
- **Prueba de nivel** — an adaptive written placement test (Pre-A1 to C1) that
  settles in a few questions. The answer key and the grading stay on Lumina's
  server; the tutor then confirms the level by conversation.
- **Ruta CEFR** — a unit completes after its lessons (one per class that reaches
  100%) or when its **Prueba de unidad** is passed with 80%. Finishing a level's
  units never promotes on its own; the level assessment does.
- **Lectura guiada** — a graded reading with a tappable glossary, a translation
  on demand, the tutor reading it aloud and comprehension questions. Tapped
  words go straight into review.
- **Escenarios** — 16 role-plays, from the classroom and pets for children to
  online gaming for teenagers and job interviews for adults. Choosing a mode,
  unit or activity, the ✕ on the scenario pill, or the end of the class ends the
  role-play.
- **Repasar** — flashcards scheduled by the official Python FSRS package; the
  meaning stays hidden until the student has tried to recall it.
- **Listening Lab**, nine learning modes including reading and writing, class
  history, XP (stars for children), streaks and a weekly goal.

Checkpoints and readings are written by Gemini inside the unit's limits, then
validated and graded by Lumina, so a score never comes from the model's opinion.
Free-tier text quotas are per model and per day (`gemini-3.6-flash` allowed 20
requests a day), so analysis and activities move on to `gemini-3.5-flash-lite`
and then `gemini-2.5-flash-lite` when a quota is spent, and turns where only the
tutor spoke are not analysed at all. The browser microphone and the tutor's Live
session remain separate by design; the general assistant still pauses its own
input for the whole class.

**Local engines.** LanguageTool and OpenPronounce run on this PC, never on their
public services, each in its own process so neither shares memory with Lumina's
voice. They live outside the repository, in
`%LOCALAPPDATA%\LuminaStartTalk\engines`, and are installed once with:

```powershell
.venv\Scripts\python.exe -m learning_english.engines install
```

- **LanguageTool** runs on a portable, checksum-verified Java 21 as a server that
  only listens on loopback. It starts with each class, warms up before the first
  turn (its first check took over 10 s) and stops when the class ends. Its
  objective findings join the corrections. `LUMINA_LANGUAGETOOL_URL` points at a
  server run elsewhere instead.
- **OpenPronounce** runs in its own Python environment with eSpeak NG and two
  Wav2Vec2 speech models (a 2.4 GB download; about 4.8 GB on disk on Windows
  without Developer Mode, whose cache cannot use symlinks). When the tutor asks for a phrase to be
  repeated, the spoken attempt gets a real 0–100 score and the sounds that were
  off. The models were trained on adult voices, so children's scores are shown as
  approximate. `LUMINA_OPENPRONOUNCE_URL` points at a server run elsewhere.

**Gemma 4 helps Gemini.** With `OLLAMA_CLOUD_ENABLED`, `OLLAMA_CLOUD_BASE_URL` and
`OLLAMA_CLOUD_API_KEY` in `.env`, `gemma4:31b` on Ollama Cloud analyses each
student turn first (2 to 4 s, measured), and Gemini's models take over whenever it
fails or leaves out part of the analysis. For generated activities the order is
reversed, because Gemma stalled for minutes writing a whole checkpoint.

**Voice turns in Supabase.** Each spoken student turn is saved to Lumina's
Supabase: the audio (Opus through ffmpeg, otherwise WAV) in the private
`learning-voice` bucket, and a row in `public.learning_voice_recordings` with the
transcript, the tutor's reply, the class context and any pronunciation score.
Only the service role can reach either (schema in `memory/supabase_schema.sql`).
The studio shows **Grabando** while this is on, and its settings turn it off or
delete every recording. With children, keep recordings only with a parent's or
guardian's permission. Course progress keeps syncing through the memory document
in `lumina_state_documents`.

The studio opens on `http://127.0.0.1:8002`, a loopback-only twin of the
dashboard: the dashboard's HTTPS certificate is self-signed, and the browser
would stop the studio at a privacy warning.

The first class asks conversationally for the student's primary language,
approximate CEFR level and goal. Later classes restore the saved profile,
frequent corrections, vocabulary, skill progress and last lesson. Use the web
studio to switch among conversation, pronunciation, grammar, vocabulary,
listening, quick lesson and level assessment.

A spoken request never ends a class: the tutor answers that it cannot close the
class by voice and points to **Volver a Lumina**. Press **Volver a Lumina** or
**Terminar clase** in the studio (or type the order in Lumina's command input).
The class summary is saved and Lumina's microphone comes back.

### 👁️ Live Vision — screen and camera

The **LIVE VISION** card beside the command input has the same explicit control
pattern as Copilot Vision:

1. Press **SELECT SOURCE** to open a visual share picker with **WINDOW** and
   **ENTIRE SCREEN** tabs, real thumbnails, a selected-card state, and an
   explicit **SHARE** button; or choose **CAMERA**.
2. The chosen application window is captured at its current bounds and follows
   every move or resize. A click-through yellow border marks exactly what Lumina
   can see, while the border and controls are excluded from the model's frames.
3. A compact always-on-top bar keeps the source, truthful microphone state,
   **PAUSE / RESUME**, and **STOP** available even when Lumina is covered.
4. Keep talking normally. Fresh frames enter the live voice session, on either
   engine, so follow-up questions refer to what is visible now. Ask “show me
   where” and Lumina can place a temporary pointer on the shared source without
   clicking or controlling the computer.

The **SHARE** action is the explicit screen-sharing opt-in and states that frames
are sent to Gemini until Stop; camera has its own first-use consent notice.
Windows are captured through their native window surface when supported, so a
different foreground window does not replace the selected app in the stream;
protected surfaces fall back to visible-region capture. Camera mode shows a
fluid local preview, but the model feed never exceeds one frame per second.

Smart capture compares each sample with the last frame the model received,
skips visually unchanged frames, and sends a periodic keyframe so long static
sessions do not lose context. Only the latest camera frame is retained—there is
no backlog. Frames stay in memory and Live Vision never writes them to disk. If
the voice transport reconnects, the source and paused state are restored and
the status remains explicit throughout.

### 🧠 A memory that actually remembers

The store was capped at **2,200 characters — the whole memory, not per entry** — because all of it was pasted into the system prompt on every connect, so growing the memory grew every request. When it filled, the oldest entries were deleted and one line was printed to a console nobody reads. An assistant advertised as remembering "projects, preferences and personal context" was in practice a two-page notepad that quietly forgot your sister's name after a few weeks.

Storage and prompt budget are now separate problems:

* **Nothing is deleted.** The cap is a runaway guard normal use never approaches, and if it is ever hit it says so in the activity log instead of on stdout.
* **The prompt carries a core, not a dump.** Identity in full, then the most recently updated facts, budgeted — measured at **under 1,000 characters on a memory holding 61 stored facts.** That is *smaller* than the old whole-store cap, so sessions now connect with fewer tokens than before.
* **The rest is fetched on demand.** A `recall_memory` tool searches the full local cache — no network, no second model, well under a millisecond.
* **Supabase adds recovery without becoming a dependency.** When configured, the local JSON document is mirrored to a private Supabase row. Startup resolves the newest copy, while saves are queued in one background thread so audio stays responsive. If Supabase or the network is unavailable, the local memory continues to work normally.

The part that is easy to get wrong: **a model cannot look something up if it doesn't know the thing exists.** So the prompt also carries an **index of the keys** it had no room for. Without it, "who is Lucía?" gets "I don't know" while `lucia_sister` sits on disk unread. That index interleaves categories rather than sorting by recency — sorted like the core, a memory with forty preferences pushed the one entry the index existed for off the end.

⚙ → **🧠 MEMORY** shows every stored fact, when it was learned, and a ✕ to forget it. The fast offline copy stays in `memory/long_term.json` on your machine.

Optional Supabase persistence reads these values from the repository `.env` file:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-local-service-role-key
LUMINA_SUPABASE_SCHEMA=public
LUMINA_SUPABASE_ALLOW_WRITES=true
```

The service-role key is only for a trusted local installation. Never commit `.env`, embed the key in a public build, or expose it to browser code.
Apply `memory/supabase_schema.sql` once when connecting a new Supabase project.

### ↩️ Undo — it can take back what it did

LUMINA moves files, renames them, writes to them and changes your settings. Every supported change is designed with a safe path back.

Say **"undo"** — in any language — and it reverses its own last action:

| | |
|---|---|
| **Files** | move · rename · create · copy · write · delete · organize desktop |
| **Settings** | volume · brightness · dark mode |

Three things it deliberately does *not* do:

* **It does not guess.** Settings undo reads the current value *before* changing it. Where a platform won't report that value, nothing is registered — an undo that restores a guess is worse than no undo.
* **It does not hoard.** Undoing a write means keeping the old contents in memory, so files over 1 MB are excluded and it says so rather than holding a 200 MB log for the session.
* **It does not delete your files to undo a copy.** The reverse of a copy is removing the copy; the reverse of "create a folder" is removing it *only while it's still empty*.

`organize_desktop` gets special treatment — one command that moves dozens of files, which made it the least reversible thing the assistant could do. It journals every move and puts all of them back in one go, cleaning up the folders it created if they're still empty.

**Undo costs nothing at runtime.** It appends a closure to a list; nothing in it runs unless you ask.

### ⚠️ A confirmation the model can't forge

The old gate read like this:

```python
if action in _DANGEROUS_ACTIONS:            # {"restart", "shutdown"}
    confirmed = str(params.get("confirmed", "")).lower()
```

`confirmed` is a **tool parameter, which means the model fills it in.** Nothing stopped it sending `confirmed=yes` on the first call and nothing checked that a human was ever involved. It was a convention, not a gate. And its coverage was two actions — so `toggle_wifi`, which cuts the assistant's own connection to the Live API and therefore *cannot be asked to undo itself*, went through with no gate at all.

The token is issued by the interface. Shutdown, restart and WiFi put a banner on the HUD and **return immediately**; the action runs only if you press CONFIRM. Nothing blocks — LUMINA keeps talking while the banner is up.

> The split between the two mechanisms is about reversibility, not about how alarming a word sounds. Anything undoable is done at once; only the genuinely irreversible asks. An assistant that checks with you before turning the volume down is one you stop talking to.

### 🎧 It finally asks which microphone

Audio streams can follow the devices selected in the UI instead of whichever endpoint the OS happens to call "default". If a saved device disappears, LUMINA safely falls back to the system default.

⚙ → **🎧 AUDIO DEVICES** lets you pick the microphone and the speakers by name. Two things matter more than the dropdown:

**The list is short.** `query_devices()` returns one entry per *device × host API*, not per device — measured on an ordinary Windows machine, **41 entries for what the sound settings show as 4 microphones and 4 speakers.** The same microphone appears four times, under MME, DirectSound, WASAPI and WDM-KS, with nothing to say which is which. That is not a choice, it's a quiz. The picker takes one host API per direction, drops the "Sound Mapper" and "Primary Sound Driver" pseudo-devices that just mean "default", and deduplicates. **41 → 8.**

**Every entry has been measured, not assumed.** The obvious approach is to pick the host API with the nicest names — WASAPI on Windows, which in shared mode **doesn't resample**, so with 16 kHz in and 24 kHz out against 48 kHz hardware every open failed. Adding a rate check and moving to DirectSound passes that test on both sides, and PortAudio's DirectSound **output is a silent sink**: the stream opens, every write returns success in ~0 ms, and not one sample reaches the speakers.

| | write(2.0 s) took | |
|---|---|---|
| MME | **2.02 s** | consumed in real time |
| DirectSound | **0.00 s** | swallowed instantly |

No capability flag reports that. So the app measures it — once per host API per direction, on a background thread at startup, using silence. Two consequences worth stating plainly:

* **Each direction picks its own host API.** On Windows this lands on DirectSound for the microphone and MME for the speakers — a split no amount of reasoning would have produced.
* **The probe runs in the mode the app actually ships.** DirectSound input passes a callback stream and fails a blocking read; probing the wrong mode rejected a microphone that works perfectly.

Your choice is stored **by name, not by index** — indices shift whenever something is plugged in. If the saved device is gone, it falls back to the system default and says so in the log rather than failing to start.

### 🔗 It stops forgetting the conversation when the connection drops

`session_resumption` was switched on in the config and the handle the server sent back was **never read** — so every reconnect started an empty session. A dropped packet, or simply changing the voice, wiped the conversation. "Unlimited sessions" leaked through exactly this hole.

The handle is captured and replayed now. A network blip, or switching your microphone, keeps the conversation intact.

It is held in memory only, deliberately: writing it to disk would make a fresh launch continue yesterday's chat, which sounds appealing but breaks the session-summary flow — a conversation that never ends never produces a summary, and the "yesterday we talked about…" line in the morning briefing silently disappears. Changing the **voice** also starts clean on purpose, since resuming restores the server's session state and would likely bring the old voice back with it.

### 🩹 Fixes that came with it

* **The assistant could die on a log line.** Status lines carry emoji and arrows (`📤 file_controller → Moved: a.txt → Documents/`). On a non-UTF-8 console — cp1254 on a Turkish Windows, cp1251 on a Russian one, cp932 on a Japanese one — printing one raises `UnicodeEncodeError`, and because that print sits *after* the tool's own `try/except`, it escaped into the receive loop and took the session down.
* **Every computer command paid for two model round trips.** `computer_settings` made an *entire second Gemini call, inside the tool*, purely to translate the request into one of its own action names — because the declaration only said "The action to perform", so the model rarely filled it in. When that second call failed, the fallback was `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it down" into `sesi_kis` and straight into "Unknown action". The declaration now names all 56 actions and the rest is spelling tolerance handled locally by `difflib` in microseconds. When nothing matches it suggests real action names instead of dead-ending.
* An unresolvable saved audio device, or one the driver refuses to open, falls back to the system default and says so — on both the microphone and the speakers.
* A rejected session-resumption handle is dropped after one attempt, so an expired handle can never be replayed on every retry and prevent the reconnect it exists to protect.



---

## ⚡ Quick Start

On Windows, from the project folder:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe setup.py   # installs requirements.txt and Playwright's browsers
```

Then start it with **Abrir LUMINA.bat**, or with `.venv\Scripts\python.exe main.py` to see its
console output. On macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python setup.py
.venv/bin/python main.py
```

The first launch asks for a Gemini API key. OpenAI, for its Realtime voice, and Tavily, for search,
are optional and go in **⚙ → API KEYS**. `requirements-lock.txt` pins the exact versions Lumina is
developed on (Windows 11, Python 3.14), if you want that environment instead.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS or Linux — the Copilot, ChatGPT and Phone Link bridges are Windows-only |
| **Python** | 3.11 or newer (developed on 3.14) |
| **Microphone** | Required for voice interaction |
| **Speakers** | Required for voice replies |
| **API keys** | Gemini (required); OpenAI (optional, for its Realtime voice); Tavily (optional, for search) |

---

## 🗂️ Project Structure

```
Lumina Start Talk/
├── main.py                   # Core loop — live voice session (Gemini or OpenAI), audio I/O, live audio levels, tool dispatch
├── ui.py                     # PyQt6 HUD — waveform, Live Vision controls/consent, log, camera preview
├── setup.py                  # Installs requirements.txt and Playwright's browsers
├── Abrir LUMINA.bat          # Windows launcher — checks for .venv, then starts Lumina without a console
├── requirements.txt          # Dependencies; requirements-lock.txt pins the versions it is developed on
├── AGENTS.md                 # Rules for changing the voice path, with the measurements behind them
├── plugins/
│   ├── _template.py          # Copy this to write a new plugin — one file, drop in, done
│   ├── copilot_bridge.py     # Microsoft Copilot desktop app — asks, then reads the answer aloud when it arrives
│   ├── chatgpt_bridge.py     # ChatGPT desktop app — asks, reads answers, starts new chats
│   ├── openclaw_bridge.py    # OpenClaw gateway over a persistent WebSocket, with a CLI fallback
│   ├── developer_chat_reader.py # Codex and Claude Code chats in VS Code — reads, writes, announces finished work
│   ├── phone_notifications.py   # Phone Link notifications — reads and clears them
│   └── _spoken_answer.py     # Relayed answers read in full up to 2,000 words, summarised past that
├── actions/
│   ├── web_search.py         # Gemini → Tavily → DDG fallback chain (research, price, compare); news: Google News → Tavily → DDG → Gemini
│   ├── screen_processor.py   # Compressed screen & webcam capture used by Live Vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check, no crypto
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations, kept out of credential folders
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Builds multi-file projects: plan, write, run, fix
│   └── desktop.py            # Wallpaper and desktop organising
├── core/
│   ├── prompt.txt            # Assistant personality and tool-routing rules
│   ├── openai_realtime.py    # OpenAI Realtime session over WebSocket, and the browser tutor's WebRTC call
│   ├── echo_canceller.py     # Local WebRTC echo cancellation for the OpenAI voice
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   └── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
├── memory/
│   ├── memory_manager.py     # Offline-first memory, recall, sessions, and remote reconciliation
│   ├── conversation_log.py   # Conversation turns kept in Supabase for recall
│   ├── supabase_store.py     # Private Supabase JSONB persistence through PostgREST
│   ├── supabase_schema.sql   # Idempotent tables, RLS policies, and least-privilege grants
│   ├── config_manager.py     # api_keys.json access — keys, voice engine and voices, name, colour, plugin toggles
│   └── long_term.json        # Created at runtime: identity, preferences, projects, sessions, monitors
├── learning_english/         # English Learning Studio — course, tutor services, local LanguageTool and OpenPronounce
├── dashboard/                # Phone dashboard with QR pairing, and the Learning English studio page
├── tests/                    # Unit tests — python -m unittest discover -s tests
└── config/
    └── api_keys.json         # Created on first launch: keys, OS, names, voice engine and voices, UI colour, audio devices
```

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.
