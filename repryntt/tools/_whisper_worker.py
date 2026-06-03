#!/usr/bin/env python3
"""Standalone Whisper transcription worker.

Run as a subprocess to avoid loading ~1.1 GB of torch + faster-whisper
into the main daemon process.  The subprocess loads the model, transcribes
the given WAV file, prints a single JSON line to stdout, and exits —
freeing all memory.

Usage:
    python _whisper_worker.py /path/to/audio.wav
"""

import json
import sys


def transcribe(wav_path: str) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        wav_path,
        language="en",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
    )
    text = " ".join(seg.text for seg in segments).strip()
    return {"text": text, "silence": not bool(text)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: _whisper_worker.py <wav_path>"}))
        sys.exit(1)
    try:
        result = transcribe(sys.argv[1])
    except Exception as e:
        result = {"error": str(e)}
    print(json.dumps(result))
