"""
Speech-to-Text engines.

Whisper  – offline transcription via faster-whisper (VAD-buffered)
Vosk     – offline streaming transcription (lighter)
Windows  – the operating system own recogniser, used for the live line
"""
import json
import threading
import time

import numpy as np


class WhisperSTT:
    """Offline transcription using faster-whisper."""

    def __init__(self, model_name: str = "base", language: str | None = None):
        import os
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        try:
            self._model = WhisperModel(model_name, device=device, compute_type=compute)
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print(f"[STT] Whisper '{model_name}' not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._model = WhisperModel(model_name, device=device, compute_type=compute)
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Whisper '{model_name}' model download failed.\n"
                        f"Internet access is required the first time to download the speech model (~75–290 MB).\n"
                        f"After the first download it runs fully offline.\n"
                        f"Details: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        print(f"[STT] Whisper '{model_name}' ready ({device})")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns transcript string."""
        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # greedy — 2-3x faster
                best_of=1,
                condition_on_previous_text=False,  # no hallucinations, faster
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            raise


class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False



class WindowsDictation:
    """Windows' own speech recogniser, running beside the live session.

    The thing the user actually asks for — his words appearing in the chat as
    he says them — has until now been a by-product of the conversation. Gemini
    returns `input_audio_transcription` on the same socket that is also doing
    turn-taking, accumulating context, generating her voice and running tools,
    so the transcript sits downstream of all of it. When that session degrades
    the transcript dies with it, and that is measured rather than supposed: on
    2026-09-12 her speech and his transcript slowed in the same turn, from 1.2 s
    replies to a sentence that took over a minute to arrive.

    This is the same words from a source that has nothing to degrade. It holds
    no conversation, no context and no history: audio in, text out, on this
    machine. It never speaks to the model and the model never waits for it.

    Gemini's transcript stays the authority whenever it arrives — it is better,
    and it is what she actually heard. This only fills the line while nothing
    else has, which is exactly the case that used to show the user an empty
    screen while he talked.

    Windows will not let a recogniser be fed audio: it opens the microphone
    itself. Measured with Lumina already holding the device, a second
    continuous session opens fine, so the two coexist — but it means this
    recogniser hears her through the speakers too, and must be muted while she
    talks. See `set_enabled`.
    """

    _RESTART_BACKOFF = 2.0        # never spin when the session refuses to run

    def __init__(self, on_text, language: str | None = None):
        """`on_text(text, final)` is called from a worker thread.

        With no language given it takes the user's first one, strictly. Falling
        back to whatever recogniser happens to be installed is how a machine
        with only the English pack writes confident English nonsense under the
        name of someone speaking Spanish — and the point of this class is a
        line the user can trust when the model's has stopped arriving.
        """
        want = (language or "").strip()
        if not want:
            langs = self._user_languages()
            want = langs[0] if langs else ""
        self._on_text  = on_text
        self._want     = want
        self._enabled  = True
        self._stopping = False
        self._loop     = None
        self._thread   = None
        self.language  = ""       # the tag actually recognised, once started

    # ── availability ────────────────────────────────────────────────────────

    @staticmethod
    def languages() -> list[str]:
        """Language tags this machine can actually dictate in.

        Empty is the normal answer on a machine whose speech packs were never
        installed — having a language for the interface is a different thing
        from having its recogniser, and the settings screen says `basic typing`
        rather than `speech recognition` when only the first is present.
        """
        try:
            from winrt.windows.media.speechrecognition import SpeechRecognizer
            return [l.language_tag for l in SpeechRecognizer.supported_topic_languages]
        except Exception:
            return []

    @staticmethod
    def _user_languages() -> list[str]:
        """The user's own language order, most preferred first."""
        try:
            from winrt.windows.system.userprofile import GlobalizationPreferences
            return list(GlobalizationPreferences.languages)
        except Exception:
            return []

    @classmethod
    def pick_language(cls, prefer: str = "") -> str:
        """Best available tag for `prefer` ("es-MX", "es", ""), or "" for none."""
        tags = cls.languages()
        if not tags:
            return ""
        prefer = (prefer or "").strip().lower()
        if not prefer:
            # Nobody said which, so ask the user rather than the machine. The
            # system default here is en-US on a desktop whose owner speaks
            # Spanish to it all day, because the display language and the
            # language being spoken are simply different things.
            for tag in cls._user_languages():
                for have in tags:
                    if have.lower() == tag.lower():
                        return have
                head = tag.lower().split("-")[0]
                for have in tags:
                    if have.lower().split("-")[0] == head:
                        return have
            return tags[0]
        for tag in tags:                           # exact, then same language
            if tag.lower() == prefer:
                return tag
        head = prefer.split("-")[0]
        for tag in tags:
            if tag.lower().split("-")[0] == head:
                return tag
        # Asked for a language this machine cannot recognise. Falling back to
        # whatever is installed is the worst answer available: an English
        # recogniser fed Spanish does not fail, it returns confident nonsense
        # and writes it in the chat under the user name. Silence is better,
        # and the model transcript still fills the line when it is working.
        return ""

    # ── lifecycle ───────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        """Drop or accept results without tearing the session down.

        Used to mute it while she is speaking on open speakers, where her own
        voice returns into this microphone at up to full scale and would
        otherwise be transcribed and shown as the user's words. Dropping the
        callback is deliberately preferred to pausing the recogniser: pausing
        and resuming it around every sentence is what makes a continuous
        session stop coming back.
        """
        self._enabled = bool(enabled)

    def start(self) -> bool:
        """Begin recognising. False if this machine cannot, which is not fatal."""
        tag = self.pick_language(self._want)
        if not tag:
            # Say which language is missing and which are present. "Speech
            # recognition is unavailable" sends people to check their
            # microphone; naming the gap sends them to the one screen that
            # closes it.
            have = ", ".join(self.languages()) or "none"
            print(f"[Dictation] No speech recogniser for {self._want or 'your language'} "
                  f"(installed: {have}) — the live line will come from the model "
                  f"only. Add it in Settings > Time & language > Language & "
                  f"region > your language > Language options > Speech.",
                  flush=True)
            return False
        self.language  = tag
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="lumina-dictation", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stopping = True
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    # ── worker ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._recognise_forever())
        except Exception as exc:
            print(f"[Dictation] stopped: {type(exc).__name__}: {exc}", flush=True)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _recognise_forever(self) -> None:
        import asyncio
        from winrt.windows.globalization import Language
        from winrt.windows.media.speechrecognition import SpeechRecognizer

        print(f"[Dictation] Live transcription in {self.language}", flush=True)

        while not self._stopping:
            started = 0.0
            try:
                rec = SpeechRecognizer(Language(self.language))
                # Default constraints are free-form dictation, which is what a
                # conversation is. A grammar would be faster and would also
                # refuse every sentence it had not been told to expect.
                result = await rec.compile_constraints_async()
                if int(getattr(result, "status", 0)) != 0:
                    raise RuntimeError(f"constraints refused: {result.status}")

                # Partial words, which is the whole point: they arrive while he
                # is still talking. The final result replaces them.
                rec.add_hypothesis_generated(
                    lambda _s, a: self._emit(a.hypothesis.text, False)
                )
                session = rec.continuous_recognition_session
                session.add_result_generated(
                    lambda _s, a: self._emit(a.result.text, True)
                )

                # A continuous session ends on its own — a long silence, an
                # audio device change, the recogniser deciding it is done. It
                # has to be reopened, or the live line quietly stops updating
                # an hour into a conversation and looks exactly like the bug
                # this exists to route around.
                ended = asyncio.Event()
                loop = asyncio.get_running_loop()
                session.add_completed(
                    lambda _s, _a: loop.call_soon_threadsafe(ended.set)
                )

                await session.start_async()
                started = time.monotonic()
                await ended.wait()
            except Exception as exc:
                if not self._stopping:
                    print(f"[Dictation] session ended: {type(exc).__name__}: {exc}",
                          flush=True)

            if self._stopping:
                break
            # A session that ran for a while and finished is ordinary; one that
            # dies instantly is a fault, and reopening it as fast as possible
            # would spin.
            if time.monotonic() - started < self._RESTART_BACKOFF:
                await asyncio.sleep(self._RESTART_BACKOFF)

    def _emit(self, text: str, final: bool) -> None:
        if not self._enabled or self._stopping:
            return
        text = (text or "").strip()
        if not text:
            return
        try:
            self._on_text(text, final)
        except Exception:
            # A recogniser that can break the interface is worse than no
            # recogniser: this path exists precisely for when things are
            # already going wrong.
            pass
