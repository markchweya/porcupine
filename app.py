# app.py — Porcupine Assistant (Windows-friendly prototype)
# --------------------------------------------------------
# This version fixes your exact problem:
# - PvRecorder (wake word) fails on some Intel Smart Sound mic drivers
# - sounddevice sd.rec() fails with MME error 11 on some devices
#
# ✅ Default mode: Push-to-talk (PTT)
#    - Press ENTER, it auto-finds a working mic device + sample rate + channels
#    - Records a few seconds, transcribes with faster-whisper, runs safe actions
#
# ✅ Optional: Wake-word mode (Porcupine)
#    - Requires a mic that can open at 16kHz mono (often works with USB headset mic)
#
# Install:
#   pip install faster-whisper pyttsx3 sounddevice numpy pvporcupine pvrecorder
#
# Run PTT (recommended on your laptop):
#   python app.py
#   python app.py --mode ptt
#
# Run Wake mode (only if you have a compatible mic):
#   $env:PICOVOICE_ACCESS_KEY="YOUR_KEY"
#   python app.py --mode wake
#
# Tips:
# - If wake mode fails, keep using PTT for your presentation.
# - If PTT still fails, Windows mic permissions / exclusive mode is blocking.
# - Plugging a USB headset mic usually fixes wake mode + ptt.

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import wave
import tempfile
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Tuple

import pyttsx3
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

# Wake-word mode deps
import pvporcupine
from pvrecorder import PvRecorder


# -----------------------------
# CONFIG
# -----------------------------
@dataclass
class Config:
    assistant_name: str = "Porcupine"
    access_key_env: str = "PICOVOICE_ACCESS_KEY"

    # Wake word
    builtin_keyword: str = "porcupine"      # say "porcupine" to wake
    keyword_path: Optional[str] = None      # custom .ppn (optional)

    # STT
    stt_model_size: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"

    # Recording
    command_seconds: float = 4.0

    # Notes
    notes_file: str = "porcupine_notes.txt"

    # Safe allowlist apps
    allowed_apps: dict = None


CFG = Config()
CFG.allowed_apps = {
    "chrome":      {"win": "chrome", "mac": "Google Chrome", "linux": "google-chrome"},
    "calculator":  {"win": "calc", "mac": "Calculator", "linux": "gnome-calculator"},
    "notepad":     {"win": "notepad", "mac": "TextEdit", "linux": "gedit"},
    "vscode":      {"win": "code", "mac": "Visual Studio Code", "linux": "code"},
}


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


# -----------------------------
# TTS
# -----------------------------
class Speaker:
    def __init__(self):
        self.engine = pyttsx3.init()

    def say(self, text: str):
        print(f"{CFG.assistant_name.upper()}: {text}")
        self.engine.say(text)
        self.engine.runAndWait()


SPEAKER = Speaker()


# -----------------------------
# SAFE ACTIONS
# -----------------------------
def open_app(app_key: str):
    app_key = app_key.lower().strip()
    if app_key not in CFG.allowed_apps:
        raise ValueError(f"'{app_key}' not allowed. Allowed: {', '.join(CFG.allowed_apps.keys())}")

    p = platform_key()
    target = CFG.allowed_apps[app_key][p]

    if p == "win":
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
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {text}\n")
    return path


def parse_seconds(n: int, unit: str) -> int:
    unit = (unit or "seconds").lower()
    if unit.startswith("min"):
        return n * 60
    if unit.startswith("hour"):
        return n * 3600
    return n


def handle_command(text: str) -> bool:
    t = text.strip()
    low = t.lower()

    if re.search(r"\b(quit|exit|stop assistant|goodbye)\b", low):
        SPEAKER.say("Goodbye.")
        return False

    if re.search(r"\b(help|what can you do)\b", low):
        SPEAKER.say("Try: open chrome, search something, note your text, timer for 10 seconds, or ask time.")
        print(f"[allowed apps] {', '.join(CFG.allowed_apps.keys())}")
        return True

    if re.search(r"\btime\b", low):
        SPEAKER.say("The time is " + datetime.now().strftime("%I:%M %p").lstrip("0"))
        return True

    if re.search(r"\b(date|day)\b", low):
        SPEAKER.say("Today is " + datetime.now().strftime("%A, %B %d").replace(" 0", " "))
        return True

    m = re.search(r"\b(open|launch|start)\s+(chrome|calculator|notepad|vscode)\b", low)
    if m:
        app = m.group(2)
        try:
            open_app(app)
            SPEAKER.say(f"Opening {app}.")
        except Exception as e:
            SPEAKER.say(f"I couldn't open {app}. {e}")
        return True

    m = re.search(r"\bopen\s+(website\s+)?([a-z0-9\.\-]+\.[a-z]{2,})(/[\S]*)?\b", low)
    if m:
        domain = m.group(2)
        path = m.group(3) or ""
        SPEAKER.say(f"Opening {domain}.")
        open_website(domain + path)
        return True

    m = re.search(r"\bsearch\s+(.+)$", low)
    if m:
        q = m.group(1).strip()
        if q:
            SPEAKER.say(f"Searching for {q}.")
            web_search(q)
            return True

    m = re.search(r"\b(note|take\s+note)\s+(.+)$", low)
    if m:
        note_text = m.group(2).strip()
        if note_text:
            path = save_note(note_text)
            SPEAKER.say("Saved.")
            print(f"[notes] {path}")
            return True

    m = re.search(r"\b(timer)\b.*\bfor\s+(\d+)\s*(seconds|second|minutes|minute|hours|hour)?\b", low)
    if m:
        n = int(m.group(2))
        unit = m.group(3) or "seconds"
        secs = parse_seconds(n, unit)
        SPEAKER.say(f"Timer set for {n} {unit}.")
        time.sleep(secs)
        SPEAKER.say("Time is up!")
        return True

    SPEAKER.say("I heard you, but that action isn't installed yet. Say 'help'.")
    return True


# -----------------------------
# WAV helpers
# -----------------------------
def write_wav_int16(path: str, pcm_int16: np.ndarray, sample_rate: int):
    pcm_int16 = np.asarray(pcm_int16, dtype=np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())


# -----------------------------
# Robust PTT recorder (auto device + auto rate + auto channels)
# -----------------------------
def _input_devices() -> List[Tuple[int, dict]]:
    devs = sd.query_devices()
    return [(i, d) for i, d in enumerate(devs) if d.get("max_input_channels", 0) > 0]


def record_ptt(seconds: float, preferred_rate: int) -> str:
    """
    Robust Windows mic recorder:
    - tries multiple input devices
    - tries multiple sample rates
    - tries 1ch then 2ch (some drivers won't open mono)
    - uses InputStream (more reliable than sd.rec on some setups)
    """
    input_devs = _input_devices()
    if not input_devs:
        raise RuntimeError("No input devices found by sounddevice. Check Windows microphone permissions.")

    # Candidate sample rates (preferred first)
    rates: List[int] = []
    if preferred_rate:
        rates.append(int(preferred_rate))
    rates += [44100, 48000, 32000, 24000, 22050, 16000]

    last_err = None
    print("[ptt] Trying to open a working microphone stream...")

    for dev_index, dev in input_devs:
        dev_name = dev["name"]
        dev_default_sr = int(dev.get("default_samplerate") or 0)

        candidate_rates: List[int] = []
        if dev_default_sr:
            candidate_rates.append(dev_default_sr)
        candidate_rates += rates

        # De-duplicate while preserving order
        seen = set()
        candidate_rates = [r for r in candidate_rates if not (r in seen or seen.add(r))]

        max_ch = int(dev.get("max_input_channels", 1))
        channel_candidates = [1]
        if max_ch >= 2:
            channel_candidates.append(2)

        for sr in candidate_rates:
            for ch in channel_candidates:
                try:
                    frames = int(seconds * sr)
                    print(f"[ptt] Trying device {dev_index} ({dev_name}) @ {sr} Hz, {ch}ch")

                    recorded = np.empty((frames, ch), dtype=np.int16)

                    with sd.InputStream(device=dev_index, samplerate=sr, channels=ch, dtype="int16") as stream:
                        idx = 0
                        block = 4096
                        while idx < frames:
                            n = min(block, frames - idx)
                            data, _overflowed = stream.read(n)
                            recorded[idx:idx + n, :] = data
                            idx += n

                    mono = recorded[:, 0].copy()

                    fd, wav_path = tempfile.mkstemp(prefix="porcupine_ptt_", suffix=".wav")
                    os.close(fd)
                    write_wav_int16(wav_path, mono, sr)

                    print(f"[ptt] ✅ Using device {dev_index} ({dev_name}) @ {sr} Hz, {ch}ch")
                    return wav_path

                except Exception as e:
                    last_err = e
                    continue

    raise RuntimeError(
        "Could not open any microphone input stream with sounddevice.\n"
        f"Last error: {repr(last_err)}\n\n"
        "Windows fixes to try:\n"
        "1) Settings → Privacy & security → Microphone → allow Desktop apps\n"
        "2) Close Teams/Zoom/Discord/OBS\n"
        "3) Control Panel → Sound → Recording → Microphone → Advanced → disable Exclusive Mode\n"
    )


# -----------------------------
# STT core
# -----------------------------
class STTCore:
    def __init__(self, model_size: str):
        self.model = WhisperModel(model_size, device=CFG.stt_device, compute_type=CFG.stt_compute_type)

    def transcribe(self, wav_path: str) -> str:
        segments, _info = self.model.transcribe(wav_path, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()


# -----------------------------
# Modes
# -----------------------------
class PushToTalkMode:
    def __init__(self, model_size: str, seconds: float, preferred_rate: int):
        self.core = STTCore(model_size)
        self.seconds = seconds
        self.preferred_rate = preferred_rate

    def run(self):
        SPEAKER.say(f"{CFG.assistant_name} (push-to-talk) ready. Press ENTER to speak. Type 'q' + ENTER to quit.")
        while True:
            s = input("\n[ptt] Press ENTER to record (or type q to quit): ").strip().lower()
            if s in ("q", "quit", "exit"):
                SPEAKER.say("Goodbye.")
                return

            wav_path = record_ptt(self.seconds, self.preferred_rate)
            try:
                cmd = self.core.transcribe(wav_path)
            finally:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

            if not cmd:
                SPEAKER.say("I didn't catch that.")
                continue

            print(f"YOU: {cmd}")
            keep = handle_command(cmd)
            if not keep:
                return


class WakeWordMode:
    def __init__(self, access_key: str, keyword: str, keyword_path: Optional[str], model_size: str, seconds: float):
        self.core = STTCore(model_size)
        self.seconds = seconds

        if keyword_path:
            self.porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[keyword_path])
            self.wake_label = "custom wake word"
        else:
            self.porcupine = pvporcupine.create(access_key=access_key, keywords=[keyword])
            self.wake_label = keyword

        # NOTE: This often fails on Intel Smart Sound mics if 16k mono isn't supported.
        self.recorder = PvRecorder(device_index=-1, frame_length=self.porcupine.frame_length)
        self.recorder.start()

    def close(self):
        try:
            self.recorder.stop()
        except Exception:
            pass
        try:
            self.recorder.delete()
        except Exception:
            pass
        try:
            self.porcupine.delete()
        except Exception:
            pass

    def record_command_16k(self) -> str:
        total_samples = int(self.seconds * self.recorder.sample_rate)
        buf: List[int] = []
        while len(buf) < total_samples:
            buf.extend(self.recorder.read())

        data = np.array(buf[:total_samples], dtype=np.int16)

        fd, wav_path = tempfile.mkstemp(prefix="porcupine_cmd_", suffix=".wav")
        os.close(fd)
        write_wav_int16(wav_path, data, self.recorder.sample_rate)
        return wav_path

    def run(self):
        SPEAKER.say(f"{CFG.assistant_name} (wake mode) ready. Say: {self.wake_label}.")
        try:
            while True:
                pcm = self.recorder.read()
                if self.porcupine.process(pcm) >= 0:
                    SPEAKER.say("Yes?")
                    wav_path = self.record_command_16k()
                    try:
                        cmd = self.core.transcribe(wav_path)
                    finally:
                        try:
                            os.remove(wav_path)
                        except Exception:
                            pass

                    if not cmd:
                        SPEAKER.say("I didn't catch that.")
                        continue

                    print(f"YOU: {cmd}")
                    keep = handle_command(cmd)
                    if not keep:
                        return
        finally:
            self.close()


# -----------------------------
# CLI
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Porcupine laptop voice assistant prototype")
    ap.add_argument("--mode", choices=["ptt", "wake"], default="ptt", help="ptt recommended on your laptop")
    ap.add_argument("--model", default="small", help="Whisper model size (tiny/base/small/...)")
    ap.add_argument("--seconds", type=float, default=4.0, help="Seconds to record after prompt/wake word")
    ap.add_argument("--ptt-rate", type=int, default=48000, help="Preferred PTT sample rate (auto-fallback will try others)")
    ap.add_argument("--keyword", default="porcupine", help="Wake keyword (wake mode only)")
    ap.add_argument("--keyword-path", default=None, help="Custom wake word .ppn (wake mode only)")
    args = ap.parse_args()

    CFG.command_seconds = args.seconds
    CFG.stt_model_size = args.model
    CFG.keyword_path = args.keyword_path
    CFG.builtin_keyword = args.keyword

    if args.mode == "ptt":
        PushToTalkMode(model_size=args.model, seconds=args.seconds, preferred_rate=args.ptt_rate).run()
        return

    # Wake mode requires AccessKey
    access_key = os.getenv(CFG.access_key_env, "").strip()
    if not access_key:
        raise RuntimeError(
            f"Missing AccessKey. Set {CFG.access_key_env} first.\n"
            f"PowerShell: $env:{CFG.access_key_env}='YOUR_KEY'"
        )

    WakeWordMode(
        access_key=access_key,
        keyword=args.keyword,
        keyword_path=args.keyword_path,
        model_size=args.model,
        seconds=args.seconds,
    ).run()


if __name__ == "__main__":
    main()
