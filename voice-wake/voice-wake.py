#!/usr/bin/env python3
"""
Voice Wake Daemon — Wake Word Detection + Voice Commands
=========================================================
Local daemon: listens for wake word via pvporcupine, records speech,
transcribes via OpenMind, routes to daemon or OpenMind, speaks response.

Run:  python3 voice-wake.py
"""

import os, sys, time, struct, logging, shutil, subprocess

import numpy as np
import pvporcupine
import sounddevice as sd
import httpx
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────
PORCUPINE_ACCESS_KEY = os.environ.get("PORCUPINE_ACCESS_KEY", "")
WAKE_WORD = os.environ.get("WAKE_WORD", "jarvis")
OPENMIND_URL = os.environ.get("OPENMIND_URL", "http://127.0.0.1:8250")
DAEMON_URL = os.environ.get("DAEMON_URL", "http://127.0.0.1:8256")
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "emc2ymmv")

RECORD_MAX_SECONDS = 10
SILENCE_THRESHOLD = 500       # RMS amplitude below which we consider silence
SILENCE_DURATION = 1.5        # seconds of silence before we stop recording
SAMPLE_RATE = 16000

log = logging.getLogger("voice-wake")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ─── HTTP client ─────────────────────────────────────
_http = httpx.Client(
    headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
    timeout=30.0,
)

# ─── TTS output ──────────────────────────────────────
def _find_tts() -> list[str] | None:
    """Find available TTS command."""
    if shutil.which("say"):
        return ["say"]
    if shutil.which("espeak"):
        return ["espeak"]
    if shutil.which("espeak-ng"):
        return ["espeak-ng"]
    return None

_tts_cmd = _find_tts()

def speak(text: str):
    """Speak text via system TTS. Falls back to log if unavailable."""
    if not text:
        return
    if _tts_cmd:
        try:
            subprocess.run([*_tts_cmd, text], timeout=15)
        except Exception as e:
            log.warning(f"TTS failed: {e}")
    else:
        log.info(f"[SPEAK] {text}")

# ─── Audio recording ─────────────────────────────────
def record_until_silence(porcupine_rate: int) -> np.ndarray:
    """Record audio at SAMPLE_RATE until silence or max duration."""
    log.info("[RECORD] Listening for speech...")
    frames = []
    silent_frames = 0
    frames_per_check = SAMPLE_RATE // 10  # 100ms chunks
    silence_limit = int(SILENCE_DURATION / 0.1)
    max_chunks = int(RECORD_MAX_SECONDS / 0.1)

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=frames_per_check,
    )
    stream.start()

    try:
        for _ in range(max_chunks):
            data, _ = stream.read(frames_per_check)
            chunk = data.flatten()
            frames.append(chunk)

            rms = np.sqrt(np.mean(chunk.astype(np.float64) ** 2))
            if rms < SILENCE_THRESHOLD:
                silent_frames += 1
            else:
                silent_frames = 0

            if silent_frames >= silence_limit and len(frames) > silence_limit:
                log.info("[RECORD] Silence detected, stopping.")
                break
    finally:
        stream.stop()
        stream.close()

    return np.concatenate(frames)

# ─── Transcribe via OpenMind ─────────────────────────
def transcribe(audio: np.ndarray) -> str:
    """POST raw audio to OpenMind /api/transcribe, return transcript."""
    import io, wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    buf.seek(0)
    try:
        resp = _http.post(
            f"{OPENMIND_URL}/api/transcribe",
            files={"file": ("audio.wav", buf, "audio/wav")},
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", data.get("transcript", "")).strip()
        log.info(f"[TRANSCRIBE] {text}")
        return text
    except Exception as e:
        log.error(f"[TRANSCRIBE] Failed: {e}")
        return ""

# ─── Routing ─────────────────────────────────────────
DAEMON_KEYWORDS = {"daemon", "schedule", "remind", "reminder", "task"}

def route_transcript(text: str) -> str:
    """Route transcript to daemon or OpenMind, return response text."""
    lower = text.lower()

    if any(kw in lower for kw in DAEMON_KEYWORDS):
        log.info(f"[ROUTE] → daemon: {text}")
        try:
            resp = _http.post(
                f"{DAEMON_URL}/api/task",
                json={"title": f"Voice: {text[:60]}", "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("task_id", data.get("id", "unknown"))
            return f"Task created: {task_id}"
        except Exception as e:
            log.error(f"[ROUTE] Daemon call failed: {e}")
            return f"Failed to create task: {e}"
    else:
        log.info(f"[ROUTE] → OpenMind NL: {text}")
        try:
            resp = _http.post(
                f"{OPENMIND_URL}/api/nl",
                json={"text": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("reply", data.get("result", str(data)))
        except Exception as e:
            log.error(f"[ROUTE] OpenMind call failed: {e}")
            return f"Failed to process: {e}"

# ─── Main loop ───────────────────────────────────────
def main():
    if not PORCUPINE_ACCESS_KEY:
        log.error("PORCUPINE_ACCESS_KEY not set. Exiting.")
        sys.exit(1)

    log.info(f"Initializing porcupine with wake word: {WAKE_WORD}")
    porcupine = pvporcupine.create(
        access_key=PORCUPINE_ACCESS_KEY,
        keywords=[WAKE_WORD],
    )

    frame_length = porcupine.frame_length
    sample_rate = porcupine.sample_rate

    log.info(f"Listening for '{WAKE_WORD}' (rate={sample_rate}, frame={frame_length})...")

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_length,
    )
    stream.start()

    try:
        while True:
            data, _ = stream.read(frame_length)
            pcm = data.flatten()

            keyword_index = porcupine.process(pcm)
            if keyword_index >= 0:
                log.info(f"[WAKE] Wake word detected!")
                speak("Yes?")

                # Stop wake word stream while recording
                stream.stop()

                audio = record_until_silence(sample_rate)

                if len(audio) < SAMPLE_RATE:  # less than 1s of audio
                    log.info("[WAKE] Too short, ignoring.")
                    stream.start()
                    continue

                text = transcribe(audio)
                if not text:
                    speak("Sorry, I didn't catch that.")
                    stream.start()
                    continue

                response = route_transcript(text)
                speak(response)

                # Resume wake word listening
                stream.start()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        stream.stop()
        stream.close()
        porcupine.delete()
        _http.close()


if __name__ == "__main__":
    main()
