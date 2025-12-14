# app.py — "Porcupine" laptop voice assistant (prototype)
# -------------------------------------------------------
# What it does (safe, demo-friendly):
#  - Wake word detection (Porcupine)
#  - Record a short command window
#  - Speech-to-text (faster-whisper)
#  - Run ONLY allowlisted actions:
#       * open an app (allowlist)
#       * open a website / search web
#       * take a note (append to local file)
#       * set a timer (blocks until done, then speaks)
#       * tell time/date
#  - No arbitrary shell execution (by design)
#
# Install:
#   pip install pvporcupine pvrecorder faster-whisper pyttsx3
#
# Set your Picovoice AccessKey:
#   Windows (PowerShell):  setx PICOVOICE_ACCESS_KEY "YOUR_KEY"
#   macOS/Linux:           export PICOVOICE_ACCESS_KEY="YOUR_KEY"
#
# Run:
#   python app.py
#
# Wake word:
#   Default is built-in keyword: "porcupine"
#   (Say: "porcupine", then speak your command)
#
# Optional: use a custom wake word .ppn file:
#   Set KEYWORD_PATH below to your .ppn path and leave BUILTIN_KEYWORD unused.

from __future__ import annotations

import os
import re
import sys
import time
import wave
import queue
import signal
import tempfile
import threading
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import datetime

import pyttsx3
from pvrecorder import PvRecorder
import pvporcupine
from faster_whisper import WhisperModel


# -----------------------------
# CONFIG
# -----------------------------

@dataclass
class Config:
    # App identity
    assistant_name: str = "Porcupine"  # your app name
    prompt_name: str = "PORCUPINE"

    # Wake word
    access_key_env: str = "PICOVOICE_ACCESS_KEY"
    builtin_keyword: str = "porcupine"     # built-in keyword to wake
    keyword_path: str | None = None        # path to custom .ppn if you have one

    # Audio + STT
    stt_model_size: str = "small"          # tiny/base/small/medium/large-v3 etc.
    stt_device: str = "cpu"                # cpu or cuda
    stt_compute_type: str = "int8"         # int8 = faster on CPU
    command_record_seconds: float = 4.0    # how long to record after wake

    # Notes
    notes_file: str = "porcupine_notes.txt"

    # Safety: allowlisted apps per OS
    allowed_apps: dict = None  # set below in __post_init__


CFG = Config()


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


# A small allowlist you can expand for your demo
DEFAULT_ALLOWED_APPS = {
    "chrome":   {"win": "chrome",         "mac": "Google Chrome", "linux": "google-chrome"},
    "calculator": {"win": "calc",         "mac": "Calculator",    "linux": "gnome-calculator"},
    "notepad":  {"win": "notepad",        "mac": "TextEdit",      "linux": "gedit"},
    "vscode":   {"win": "code",           "mac": "Visual Studio Code", "linux": "code"},
}


CFG.allowed_apps = DEFAULT_ALLOWED_APPS


# -----------------------------
# TTS (offline)
# -----------------------------

class Speaker:
    def __init__(self):
        self._tts = pyttsx3.init()
        # Optional tweaks:
        # rate = self._tts.getProperty("rate")
        # self._tts.setProperty("rate", max(120, rate - 20))

        self._lock = threading.Lock()

    def say(self, text: str):
        print(f"{CFG.prompt_name}: {text}")
        with self._lock:
            self._tts.say(text)
            self._tts.runAndWait()


SPEAKER = Speaker()


# -----------------------------
# Utilities
# -----------------------------

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_app(app_key: str):
    app_key = app_key.lower().strip()
    if app_key not in CFG.allowed_apps:
        raise ValueError(f"App '{app_key}' not in allowlist. Allowed: {', '.join(CFG.allowed_apps.keys())}")

    p = _platform_key()
    target = CFG.allowed_apps[app_key][p]

    if p == "win":
        # Use shell=True for common Windows app commands (calc, notepad, chrome)
        subprocess.Popen([target], shell=True)
    elif p == "mac":
        subprocess.Popen(["open", "-a", target])
    else:
        subprocess.Popen([target])


def open_website(url: str):
    if not re.match(r"^https?://", url):
        url = "https://" + url
    webbrowser.open(url)


def web_search(query: str):
    q = query.strip().replace(" ", "+")
    webbrowser.open(f"https://www.google.com/search?q={q}")


def save_note(text: str) -> str:
    path = os.path.abspath(CFG.notes_file)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{now_str()} - {text}\n")
    return path


def seconds_from_text(n: int, unit: str) -> int:
    unit = (unit or "seconds").lower()
    if unit.startswith("min"):
        return n * 60
    if unit.startswith("hour"):
        return n * 3600
    return n


# -----------------------------
# Command parsing (simple + demo-friendly)
# -----------------------------

class CommandRouter:
    """
    Keep parsing intentionally simple and deterministic for demos.
    Add more patterns as you grow the prototype.
    """

    def handle(self, text: str) -> bool:
        """
        Returns True if the app should keep running, False to exit.
        """
        t = text.strip()
        low = t.lower()

        # Exit
        if re.search(r"\b(quit|exit|stop assistant|goodbye)\b", low):
            SPEAKER.say("Goodbye.")
            return False

        # Time / date
        if re.search(r"\b(time)\b", low):
            SPEAKER.say("The time is " + datetime.now().strftime("%I:%M %p").lstrip("0"))
            return True

        if re.search(r"\b(date|day)\b", low):
            SPEAKER.say("Today is " + datetime.now().strftime("%A, %B %d").replace(" 0", " "))
            return True

        # Open app: "open chrome", "launch calculator"
        m = re.search(r"\b(open|launch|start)\s+(chrome|calculator|notepad|vscode)\b", low)
        if m:
            app = m.group(2)
            try:
                open_app(app)
                SPEAKER.say(f"Opening {app}.")
            except Exception as e:
                SPEAKER.say(f"I couldn't open {app}. {e}")
            return True

        # Open website: "open website example.com" / "open youtube.com"
        m = re.search(r"\b(open)\s+(website\s+)?([a-z0-9\.\-]+\.[a-z]{2,})(/[\S]*)?\b", low)
        if m:
            domain = m.group(3)
            path = m.group(4) or ""
            url = domain + path
            SPEAKER.say(f"Opening {domain}.")
            open_website(url)
            return True

        # Search: "search something"
        m = re.search(r"\bsearch\s+(.*)$", low)
        if m and m.group(1).strip():
            q = m.group(1).strip()
            SPEAKER.say(f"Searching for {q}.")
            web_search(q)
            return True

        # Note: "note buy milk", "take note that ..."
        m = re.search(r"\b(note|take\s+note)\s+(.*)$", low)
        if m and m.group(2).strip():
            note_text = m.group(2).strip()
            path = save_note(note_text)
            SPEAKER.say("Saved.")
            print(f"[notes] {path}")
            return True

        # Timer: "timer for 10 seconds" / "set a timer for 2 minutes"
        m = re.search(r"\b(timer)\b.*\bfor\s+(\d+)\s*(seconds|second|minutes|minute|hours|hour)?\b", low)
        if m:
            n = int(m.group(2))
            unit = m.group(3) or "seconds"
            secs = seconds_from_text(n, unit)
            SPEAKER.say(f"Timer set for {n} {unit}.")
            time.sleep(secs)
            SPEAKER.say("Time is up!")
            return True

        # Help
        if re.search(r"\b(help|what can you do)\b", low):
            allowed = ", ".join(CFG.allowed_apps.keys())
            SPEAKER.say(
                "Try: open chrome, search something, note your text, timer for ten seconds, or ask for time."
            )
            print(f"[allowed apps] {allowed}")
            return True

        # Fallback
        SPEAKER.say(
            "I heard you, but that action isn't installed yet. Say 'help' to see what I can do."
        )
        return True


ROUTER = CommandRouter()


# -----------------------------
# Audio: Wake word + record command
# -----------------------------

class PorcupineAssistant:
    def __init__(self):
        self._stop_event = threading.Event()

        access_key = os.getenv(CFG.access_key_env, "").strip()
        if not access_key:
            raise RuntimeError(
                f"Missing AccessKey. Set {CFG.access_key_env} environment variable first."
            )

        # Create wake word engine
        if CFG.keyword_path:
            self._porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[CFG.keyword_path])
            self._wake_label = "custom wake word"
        else:
            self._porcupine = pvporcupine.create(access_key=access_key, keywords=[CFG.builtin_keyword])
            self._wake_label = CFG.builtin_keyword

        # Recorder must match porcupine frame length
        self._recorder = PvRecorder(device_index=-1, frame_length=self._porcupine.frame_length)
        self._recorder.start()

        # STT model
        self._stt = WhisperModel(
            CFG.stt_model_size,
            device=CFG.stt_device,
            compute_type=CFG.stt_compute_type
        )

        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            try:
                self._recorder.stop()
            except Exception:
                pass
            try:
                self._recorder.delete()
            except Exception:
                pass
            try:
                self._porcupine.delete()
            except Exception:
                pass

    def stop(self):
        self._stop_event.set()

    def _record_wav(self, seconds: float) -> str:
        """
        Record a fixed window of audio to a temp WAV file.
        """
        frames_needed = int(seconds * self._recorder.sample_rate / self._recorder.frame_length)
        audio = []

        for _ in range(frames_needed):
            if self._stop_event.is_set():
                break
            pcm = self._recorder.read()  # list[int16]
            audio.extend(pcm)

        # Convert int16 list -> bytes little-endian
        pcm_bytes = bytearray()
        for x in audio:
            pcm_bytes += int(x).to_bytes(2, byteorder="little", signed=True)

        fd, wav_path = tempfile.mkstemp(prefix="porcupine_cmd_", suffix=".wav")
        os.close(fd)

        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self._recorder.sample_rate)
            wf.writeframes(bytes(pcm_bytes))

        return wav_path

    def _transcribe(self, wav_path: str) -> str:
        segments, _info = self._stt.transcribe(wav_path, beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text

    def run(self):
        SPEAKER.say(f"{CFG.assistant_name} is ready. Say the wake word: {self._wake_label}.")

        keep_running = True

        while keep_running and not self._stop_event.is_set():
            try:
                pcm = self._recorder.read()
                keyword_index = self._porcupine.process(pcm)

                if keyword_index >= 0:
                    SPEAKER.say("Yes?")
                    wav_path = self._record_wav(CFG.command_record_seconds)

                    try:
                        cmd = self._transcribe(wav_path)
                    finally:
                        try:
                            os.remove(wav_path)
                        except Exception:
                            pass

                    if not cmd:
                        SPEAKER.say("I didn't catch that.")
                        continue

                    print(f"YOU: {cmd}")
                    keep_running = ROUTER.handle(cmd)

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Non-fatal: speak error and continue
                SPEAKER.say(f"Something went wrong: {e}")

        self.stop()
        SPEAKER.say("Shutting down.")


# -----------------------------
# Main
# -----------------------------

def main():
    assistant = None

    def _handle_sigint(_sig, _frame):
        if assistant:
            assistant.stop()

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        assistant = PorcupineAssistant()
        assistant.run()
    finally:
        if assistant:
            assistant.close()


if __name__ == "__main__":
    main()
