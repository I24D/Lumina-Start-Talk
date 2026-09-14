"""OpenPronounce worker for Learning English, run in the engines' own Python.

learning_english/engines.py starts this script with the virtual environment
that holds OpenPronounce and talks to it in JSON lines:

    stdin : {"id": 1, "audio": "C:\\...\\turn.wav", "expected": "I study English"}
    stdout: {"id": 1, "result": {...}}   or   {"id": 1, "error": "ValueError"}

The first line it writes is {"ready": true}, once both speech models and eSpeak
answer. Libraries print freely, so anything else goes to stderr and stdout only
carries the protocol. ``--warmup`` loads the models (downloading about 2.4 GB
the first time) and exits.

It must not import anything from Lumina: this interpreter only has OpenPronounce.
"""

import json
import sys


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return str(value)


def _compact(result):
    """What Lumina shows and stores; the prosody curves are thousands of numbers."""
    differences = result.get("differences") or {}
    return {
        "score": result.get("score"),
        "transcribe": result.get("transcribe"),
        "feedback": result.get("feedback"),
        "differences": {
            key: differences[key]
            for key in ("errors", "words_with_errors", "phoneme_error_rate", "word_error_rate")
            if key in differences
        },
    }


def _load_models():
    # OpenPronounce loads its models lazily on the first score; loading them here
    # keeps that minute off the student's first attempt.
    from openpronounce import get_language, phones, speech

    speech._load_models(get_language("en").asr_model)
    if phones.is_enabled():
        phones._load_model()
    speech.get_phonemes("hello")


def _warm_up_scoring():
    # The first score also compiled librosa's pitch tracker and ran each model
    # for the first time: 48 s measured, against 6 s for the next one. A second
    # of quiet noise pays that before a student is waiting, without the network.
    try:
        import numpy as np
        from openpronounce import phones, speech

        audio = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype("float32")
        speech.extract_embeddings(audio)
        speech.transcribe(audio)
        if phones.is_enabled():
            phones.recognize_phones(audio)
        speech.extract_f0(audio)
        speech.extract_energy(audio)
    except Exception as exc:
        print(f"[openpronounce] warm-up skipped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr
    _load_models()
    _warm_up_scoring()
    if "--warmup" in sys.argv:
        return 0

    from openpronounce import compare_audio_with_text, load_audio

    protocol.write(json.dumps({"ready": True}) + "\n")
    protocol.flush()
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        reply = {"id": request.get("id")}
        try:
            result = compare_audio_with_text(load_audio(request["audio"]), request["expected"])
            reply["result"] = _json_safe(_compact(result))
        except Exception as exc:
            reply["error"] = type(exc).__name__
            print(f"[openpronounce] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        # ASCII escapes keep the protocol independent of the pipe's code page.
        protocol.write(json.dumps(reply) + "\n")
        protocol.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
