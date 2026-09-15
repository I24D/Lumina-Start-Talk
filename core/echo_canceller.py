"""WebRTC echo cancellation, so the microphone can stay open on speakers.

On speakers her own voice comes back through the microphone at up to full
scale, which is why the microphone used to close whenever she spoke: every
second she talked was a second the user could not reach her. This is the
canceller browsers use for calls. It is told what the speakers are about to
play and removes that sound from what the microphone hears, leaving the user's
voice.

It runs on this PC through the native library bundled with the ``livekit``
package. Nothing is sent to LiveKit; no server or account is involved.

A canceller is only as good as the speakers and microphone it runs on, and on
these ones it has to find her echo again at the start of every answer. So it
measures itself: each answer begins with the microphone closed, as it always
did, and it opens for the rest of that answer once the canceller has proven it
removes enough of her voice in it.
"""

from __future__ import annotations

import math
import threading

import numpy as np


def _db(numerator: float, denominator: float) -> float:
    return 10 * math.log10((numerator + 1.0) / (denominator + 1.0))


class EchoCanceller:
    """Re-chunks both directions into the 10 ms frames the processor requires."""

    # Her voice goes on arriving after the last write: the speaker holds about
    # 235 ms of queued audio on top of 213 ms of latency (both measured on MME),
    # and the room adds its reverberation.
    TAIL_SECONDS = 0.6
    # Measured on 2026-09-14 on the laptop's own speakers: sessions that
    # interrupted themselves read -1 to -9 dB, while a canceller that had found
    # her echo removed 16 to 28 dB. The microphone opens during an answer only
    # past this bar, and not before she has spoken this long in it.
    OPEN_BELOW_DB = -15.0
    OPEN_AFTER_SECONDS = 1.0
    # Per 1024-sample block, a memory of about one and a half seconds, so the
    # first moments of an answer do not hold the verdict back.
    _DECAY = 0.97

    def __init__(self, sample_rate: int, processor, frame_type):
        self.sample_rate = int(sample_rate)
        self._samples = self.sample_rate // 100
        self._frame_bytes = self._samples * 2          # 16-bit mono
        self._processor = processor
        self._frame_type = frame_type
        # Capture runs on the PortAudio thread and render on the speaker's
        # writer thread. Each call is microseconds, so one lock costs nothing.
        self._lock = threading.Lock()
        self._render_rest = bytearray()
        self._capture_rest = bytearray()
        self._delay_ms = 0
        # Whether the microphone may stay open for the rest of this answer.
        self.open = False
        self._in_answer = False
        self._answer_raw = 0.0
        self._answer_clean = 0.0
        self._answer_samples = 0
        self._interval_raw = 0.0
        self._interval_clean = 0.0

    @classmethod
    def create(cls, sample_rate: int) -> "EchoCanceller | None":
        """A working canceller, or None when the native library cannot load.

        None is a real outcome, not an error: the caller falls back to closing
        the microphone while she speaks, which is how it always worked.
        """
        try:
            from livekit import rtc

            processor = rtc.AudioProcessingModule(
                echo_cancellation=True,
                noise_suppression=True,
                high_pass_filter=True,
                # The device's own gain stays where the user left it; the
                # browser tutor turns this off for the same reason.
                auto_gain_control=False,
            )
            return cls(sample_rate, processor, rtc.AudioFrame)
        except Exception as exc:
            print(f"[AEC] Echo cancellation unavailable: {type(exc).__name__}: {exc}",
                  flush=True)
            return None

    def set_delay(self, seconds: float) -> None:
        """Speaker latency plus microphone latency: how late the echo arrives."""
        self._delay_ms = max(0, min(500, int(round(seconds * 1000))))

    def render(self, pcm: bytes) -> None:
        """Feed what the speakers are about to play, silence included."""
        with self._lock:
            self._render_rest += pcm
            while len(self._render_rest) >= self._frame_bytes:
                chunk = bytearray(self._render_rest[:self._frame_bytes])
                del self._render_rest[:self._frame_bytes]
                self._processor.process_reverse_stream(
                    self._frame_type(chunk, self.sample_rate, 1, self._samples)
                )

    def capture(self, pcm: bytes, *, her_voice: bool = False) -> bytes:
        """The microphone audio with the speakers' sound removed.

        Only whole 10 ms frames come back; a remainder waits for the next
        block, so the output length varies by up to one frame per call.
        `her_voice` says whether she can be heard in the room; it is what
        separates one answer from the next.
        """
        cleaned = bytearray()
        with self._lock:
            self._capture_rest += pcm
            while len(self._capture_rest) >= self._frame_bytes:
                frame = self._frame_type(
                    bytearray(self._capture_rest[:self._frame_bytes]),
                    self.sample_rate, 1, self._samples,
                )
                del self._capture_rest[:self._frame_bytes]
                self._processor.set_stream_delay_ms(self._delay_ms)
                self._processor.process_stream(frame)
                cleaned += frame.data.tobytes()
        if her_voice:
            if not self._in_answer:
                self._in_answer = True
                self._answer_raw = self._answer_clean = 0.0
                self._answer_samples = 0
            if pcm:
                self._measure(pcm, bytes(cleaned))
        else:
            self._in_answer = False
            self.open = False
        return bytes(cleaned)

    def _measure(self, raw_pcm: bytes, clean_pcm: bytes) -> None:
        raw = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float64)
        clean = np.frombuffer(clean_pcm, dtype=np.int16).astype(np.float64)
        raw_energy, clean_energy = float(np.dot(raw, raw)), float(np.dot(clean, clean))
        self._interval_raw += raw_energy
        self._interval_clean += clean_energy
        self._answer_raw = self._answer_raw * self._DECAY + raw_energy
        self._answer_clean = self._answer_clean * self._DECAY + clean_energy
        self._answer_samples += raw.size
        # Once open it stays open until the answer ends: when the user talks
        # over her the numbers look like a failing canceller, and closing then
        # would shut out exactly the interruption this exists for.
        if (not self.open
                and self._answer_samples >= self.OPEN_AFTER_SECONDS * self.sample_rate
                and self.reduction_db() <= self.OPEN_BELOW_DB):
            self.open = True

    def reduction_db(self) -> float:
        """How much of her voice the canceller removes lately, in dB."""
        return _db(self._answer_clean, self._answer_raw)

    def interval_db(self) -> float | None:
        """The same since the last call, or None if her voice was not playing."""
        if self._interval_raw <= 0:
            return None
        db = _db(self._interval_clean, self._interval_raw)
        self._interval_raw = self._interval_clean = 0.0
        return db
