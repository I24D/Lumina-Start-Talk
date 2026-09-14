"""Local engines for Learning English: LanguageTool and OpenPronounce.

Both are heavy, so neither lives in the repository or in Lumina's own Python:

* LanguageTool is a Java program with about 250 MB of rules. It runs on a
  portable Java 21 runtime as an HTTP server that only listens on loopback.
* OpenPronounce needs PyTorch and two speech models of about 1.2 GB each. It
  runs in its own virtual environment as a worker process, so the models never
  load into the process that carries Lumina's voice.

Everything sits under %LOCALAPPDATA%\\LuminaStartTalk\\engines. The engines start
when a class opens and stop when it ends. Install them once with:

    .venv\\Scripts\\python.exe -m learning_english.engines install
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests


JAVA_FEATURE_VERSION = 21
LANGUAGETOOL_URL = "https://languagetool.org/download/LanguageTool-stable.zip"
ESPEAK_URL = "https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi"
# eSpeak NG publishes no checksum; this is the digest of the 1.52.0 installer
# downloaded on 2026-09-14, so a different file at that URL is refused.
ESPEAK_SHA256 = "7f673c709ea5dd579d3b5ebb98688cc575328a6ab7438d2bc405b88cedaeafb9"
OPENPRONOUNCE_VERSION = "0.3.0"
WORKER_SCRIPT = Path(__file__).with_name("pronounce_worker.py")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def engines_home() -> Path:
    override = os.environ.get("LUMINA_ENGINES_DIR", "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "LuminaStartTalk" / "engines"


def _first(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    return next(iter(sorted(root.glob(pattern))), None)


def java_executable(home: Path) -> Path | None:
    return _first(home / "java", "*/bin/java.exe")


def languagetool_jar(home: Path) -> Path | None:
    return _first(home / "languagetool", "*/languagetool-server.jar")


def espeak_library(home: Path) -> Path | None:
    return _first(home / "espeak-ng", "**/libespeak-ng.dll")


def pronounce_python(home: Path) -> Path | None:
    venv = home / "pronounce-venv"
    python = venv / "Scripts" / "python.exe"
    package = venv / "Lib" / "site-packages" / "openpronounce"
    return python if python.exists() and package.exists() else None


def worker_command(python: Path, *arguments: str) -> list[str]:
    # -P keeps the worker's own folder off sys.path: learning_english/types.py
    # would otherwise shadow the standard library's types module and the
    # worker would die on its first import.
    return [str(python), "-P", str(WORKER_SCRIPT), *arguments]


_job: Any = None


def _tie_to_lumina(process: subprocess.Popen) -> None:
    """Make Windows close the engine with Lumina, even after a crash."""
    global _job
    try:
        import win32api
        import win32con
        import win32job

        if _job is None:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info
            )
            _job = job
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        win32job.AssignProcessToJobObject(_job, handle)
    except Exception:
        # Without pywin32 the engines still stop when a class or Lumina ends.
        pass


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    # A virtual environment's python.exe is a launcher that runs the real
    # interpreter as its child; ending only the launcher left that child, and
    # its 2.4 GB of models, running. taskkill /T ends the whole tree.
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True, creationflags=_NO_WINDOW,
    )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LanguageToolServer:
    """LanguageTool's HTTP server on a free loopback port, for one class."""

    def __init__(self, home: Path | None = None):
        self.home = home or engines_home()
        self.port = 0
        self.ready = threading.Event()
        self.on_ready: Callable[[], None] | None = None
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def installed(self) -> bool:
        return bool(java_executable(self.home) and languagetool_jar(self.home))

    @property
    def starting(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self.ready.is_set()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v2/check"

    def start(self) -> bool:
        """Launch in the background; ``ready`` is set once it answers."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            java, jar = java_executable(self.home), languagetool_jar(self.home)
            if not (java and jar):
                return False
            self.ready.clear()
            self.port = _free_port()
            # Without --public the server accepts loopback connections only.
            with (self.home / "languagetool.log").open("ab") as log:
                self._process = subprocess.Popen(
                    [
                        str(java), "-Xms64m", "-Xmx768m", "-cp", str(jar),
                        "org.languagetool.server.HTTPServer", "--port", str(self.port),
                    ],
                    cwd=str(jar.parent),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=_NO_WINDOW,
                )
            _tie_to_lumina(self._process)
            threading.Thread(
                target=self._wait_until_ready,
                args=(self._process, self.port),
                name="languagetool-start",
                daemon=True,
            ).start()
            return True

    def _wait_until_ready(self, process: subprocess.Popen, port: int) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if requests.get(f"http://127.0.0.1:{port}/v2/languages", timeout=2).ok:
                    # The first check loads the English rules and took over
                    # 10 s (measured); pay for it here, not on a student's turn.
                    requests.post(
                        f"http://127.0.0.1:{port}/v2/check",
                        data={"text": "This are a warm-up sentence.", "language": "en-US"},
                        timeout=90,
                    )
                    if process is self._process:
                        self.ready.set()
                        if self.on_ready:
                            self.on_ready()
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        if process is self._process:
            print("[Learning English] LanguageTool did not start; see languagetool.log")

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self.ready.clear()
        _terminate(process)


class PronunciationWorker:
    """OpenPronounce in its own Python, scoring one recording at a time.

    Requests and replies are single JSON lines on the worker's stdin and
    stdout; see pronounce_worker.py for the other side."""

    def __init__(self, home: Path | None = None):
        self.home = home or engines_home()
        self.ready = threading.Event()
        self.on_ready: Callable[[], None] | None = None
        self.last_error = ""
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._replies: dict[int, queue.Queue] = {}
        self._ids = itertools.count(1)

    @property
    def installed(self) -> bool:
        return pronounce_python(self.home) is not None

    @property
    def starting(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self.ready.is_set()

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # The speech models download here, not into the user's profile cache.
        env["HF_HOME"] = str(self.home / "huggingface")
        # Windows without Developer Mode has no symlinks, so the cache keeps a
        # second copy of each model (4.8 GB on disk here); the warning adds nothing.
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        library = espeak_library(self.home)
        if library:
            env["PHONEMIZER_ESPEAK_LIBRARY"] = str(library)
            data = library.parent / "espeak-ng-data"
            if data.exists():
                env["ESPEAK_DATA_PATH"] = str(data)
        return env

    def start(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            python = pronounce_python(self.home)
            if python is None:
                return False
            self.ready.clear()
            with (self.home / "openpronounce.log").open("ab") as log:
                self._process = subprocess.Popen(
                    worker_command(python),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    env=self.environment(), creationflags=_NO_WINDOW,
                )
            _tie_to_lumina(self._process)
            threading.Thread(
                target=self._read, args=(self._process,),
                name="openpronounce-replies", daemon=True,
            ).start()
            return True

    def _read(self, process: subprocess.Popen) -> None:
        for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("ready"):
                if process is self._process:
                    self.ready.set()
                    if self.on_ready:
                        self.on_ready()
            elif "id" in message:
                box = self._replies.get(message["id"])
                if box is not None:
                    box.put(message)
        if process is self._process:
            self.ready.clear()
        for box in list(self._replies.values()):
            box.put({"error": "OpenPronounce stopped"})

    def analyze(self, audio_path: str | Path, expected_text: str, timeout: float = 90.0) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.poll() is not None or not self.ready.is_set():
            return None
        with self._request_lock:
            request_id = next(self._ids)
            box: queue.Queue = queue.Queue(maxsize=1)
            self._replies[request_id] = box
            try:
                line = json.dumps({"id": request_id, "audio": str(audio_path), "expected": expected_text})
                process.stdin.write((line + "\n").encode("utf-8"))
                process.stdin.flush()
                reply = box.get(timeout=timeout)
            except (OSError, ValueError, queue.Empty):
                self.last_error = "OpenPronounce did not answer"
                return None
            finally:
                self._replies.pop(request_id, None)
        if isinstance(reply.get("result"), dict):
            self.last_error = ""
            return reply["result"]
        self.last_error = str(reply.get("error") or "OpenPronounce failed")
        return None

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self.ready.clear()
        if process is not None and process.stdin:
            try:
                # End of input lets the worker leave its loop and exit cleanly.
                process.stdin.close()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        _terminate(process)


# ── Installation ─────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, *, sha256: str | None = None, log: Callable[[str], None] = print) -> Path:
    if target.exists() and (sha256 is None or _sha256(target) == sha256):
        log(f"   using the downloaded {target.name}")
        return target
    log(f"   downloading {target.name}…")
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
                digest.update(chunk)
    if sha256 and digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{target.name} failed its SHA-256 check")
    partial.replace(target)
    return target


def _extract(archive: Path, target: Path) -> None:
    staging = target.with_name(target.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError(f"{archive.name} is corrupt")
        bundle.extractall(staging)
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)


def install_languagetool(home: Path, log: Callable[[str], None] = print) -> None:
    downloads = home / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    if java_executable(home) is None:
        asset = requests.get(
            f"https://api.adoptium.net/v3/assets/latest/{JAVA_FEATURE_VERSION}/hotspot",
            params={"architecture": "x64", "image_type": "jre", "os": "windows", "vendor": "eclipse"},
            timeout=30,
        ).json()[0]["binary"]["package"]
        archive = _download(asset["link"], downloads / asset["name"], sha256=asset["checksum"], log=log)
        _extract(archive, home / "java")
    log(f"   Java: {java_executable(home)}")
    if languagetool_jar(home) is None:
        archive = _download(LANGUAGETOOL_URL, downloads / "LanguageTool-stable.zip", log=log)
        _extract(archive, home / "languagetool")
    log(f"   LanguageTool: {languagetool_jar(home)}")


def install_pronunciation(home: Path, log: Callable[[str], None] = print) -> None:
    downloads = home / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    if espeak_library(home) is None:
        msi = _download(ESPEAK_URL, downloads / "espeak-ng.msi", sha256=ESPEAK_SHA256, log=log)
        # An administrative install only unpacks the files: no admin rights,
        # nothing registered in Windows.
        subprocess.run(
            ["msiexec", "/a", str(msi), "/qn", f"TARGETDIR={home / 'espeak-ng'}"],
            check=True, timeout=600,
        )
        if espeak_library(home) is None:
            raise RuntimeError("eSpeak NG did not unpack")
    log(f"   eSpeak NG: {espeak_library(home)}")
    if pronounce_python(home) is None:
        venv = home / "pronounce-venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / "Scripts" / "python.exe"
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", f"openpronounce=={OPENPRONOUNCE_VERSION}"], check=True)
    python = pronounce_python(home)
    log("   loading the speech models (a 2.4 GB download the first time)…")
    subprocess.run(
        worker_command(python, "--warmup"),
        env=PronunciationWorker(home).environment(), check=True, timeout=3 * 3600,
    )
    log("   OpenPronounce: ready")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "install":
        print("usage: python -m learning_english.engines install [languagetool|pronunciation]")
        return 2
    wanted = set(args[1:]) or {"languagetool", "pronunciation"}
    home = engines_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"Learning English engines in {home}")
    if "languagetool" in wanted:
        print("LanguageTool (portable Java 21 and LanguageTool)")
        install_languagetool(home)
    if "pronunciation" in wanted:
        print("OpenPronounce (eSpeak NG, its own Python environment and speech models)")
        install_pronunciation(home)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
