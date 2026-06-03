#!/usr/bin/env python3
"""
voice_clone.py — Voice Cloning Toolkit for REPRYNTT

Clone a voice from audio files (movie clips, voice recordings, etc.) and train
a custom Piper TTS model that can be used as the AI's voice.

This is a USER-FACING tool, not something the AI runs autonomously.

Pipeline:
  1. extract   — Isolate vocals from audio/video files (removes music/SFX)
  2. segment   — Split long audio into short training clips (5-15s each)
  3. transcribe — Auto-transcribe clips using Whisper
  4. prepare   — Format data for Piper training (JSONL + phonemization)
  5. train     — Train a Piper VITS model (requires GPU)
  6. export    — Export trained model to ONNX for fast inference
  7. install   — Install the voice into REPRYNTT voice profiles

Quick start:
  python scripts/voice_clone.py pipeline --name andrew --input movie.mkv
  python scripts/voice_clone.py install --name andrew
  python scripts/voice_clone.py list

Requirements:
  - ffmpeg (audio extraction)
  - demucs (vocal isolation) — pip install demucs
  - faster-whisper (transcription) — already installed
  - piper-train + pytorch-lightning (training) — pip install pytorch-lightning
  - piper-phonemize — already installed
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice_clone")

# ─── Paths ────────────────────────────────────────────────────────

VOICES_DIR = Path.home() / ".repryntt" / "voices"
PIPER_TRAIN_DIR = Path.home() / "piper" / "src" / "python"
PIPER_BIN = Path.home() / ".local" / "bin" / "piper"
VOICE_PROFILES_FILE = VOICES_DIR / "profiles.json"


def _ensure_dirs(name: str) -> dict:
    """Create and return working directories for a voice project."""
    base = VOICES_DIR / name
    dirs = {
        "base": base,
        "raw_audio": base / "raw_audio",
        "vocals": base / "vocals",
        "segments": base / "segments",
        "transcripts": base / "transcripts",
        "training": base / "training",
        "output": base / "output",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


# ─── Step 1: Extract Audio ────────────────────────────────────────

def cmd_extract(args):
    """Extract audio from video/audio files using ffmpeg."""
    dirs = _ensure_dirs(args.name)
    input_path = Path(args.input)

    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        log.error("ffmpeg not found. Install with: sudo apt install ffmpeg")
        sys.exit(1)

    output = dirs["raw_audio"] / f"{input_path.stem}.wav"
    log.info(f"Extracting audio from {input_path.name} → {output.name}")

    cmd = [
        "ffmpeg", "-i", str(input_path),
        "-vn",                    # no video
        "-acodec", "pcm_s16le",   # 16-bit PCM
        "-ar", "22050",           # 22.05kHz (Piper native rate)
        "-ac", "1",               # mono
        "-y",                     # overwrite
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg failed: {result.stderr[-500:]}")
        sys.exit(1)

    log.info(f"Extracted: {output} ({output.stat().st_size / 1024 / 1024:.1f} MB)")
    return output


def cmd_isolate(args):
    """Isolate vocals from audio using demucs (removes music, SFX, background)."""
    dirs = _ensure_dirs(args.name)

    # Find audio to process
    raw_files = list(dirs["raw_audio"].glob("*.wav"))
    if not raw_files:
        log.error(f"No WAV files in {dirs['raw_audio']}. Run 'extract' first.")
        sys.exit(1)

    try:
        import demucs
    except ImportError:
        log.error("demucs not installed. Install with: pip install demucs")
        log.info("Demucs uses AI to separate vocals from music/SFX — essential for movie audio.")
        sys.exit(1)

    for audio_file in raw_files:
        log.info(f"Isolating vocals from {audio_file.name} (this takes a while)...")

        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems", "vocals",   # just vocals vs everything else
            "-n", "htdemucs",          # best quality model
            "--out", str(dirs["vocals"]),
            str(audio_file),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"demucs failed: {result.stderr[-500:]}")
            sys.exit(1)

        # demucs outputs to vocals/htdemucs/<filename>/vocals.wav
        vocal_file = dirs["vocals"] / "htdemucs" / audio_file.stem / "vocals.wav"
        if vocal_file.exists():
            final = dirs["vocals"] / f"{audio_file.stem}_vocals.wav"
            shutil.move(str(vocal_file), str(final))
            log.info(f"Isolated vocals: {final}")
            # Clean up demucs temp structure
            shutil.rmtree(dirs["vocals"] / "htdemucs", ignore_errors=True)
        else:
            log.warning(f"Expected vocal file not found at {vocal_file}")

    log.info(f"Vocal isolation complete. Files in {dirs['vocals']}/")


# ─── Step 2: Segment Audio ────────────────────────────────────────

def cmd_segment(args):
    """Split audio into short clips suitable for TTS training."""
    dirs = _ensure_dirs(args.name)

    # Use vocals if available, fall back to raw
    audio_dir = dirs["vocals"] if list(dirs["vocals"].glob("*.wav")) else dirs["raw_audio"]
    wav_files = list(audio_dir.glob("*.wav"))
    if not wav_files:
        log.error(f"No WAV files found. Run 'extract' or 'isolate' first.")
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        log.error("ffmpeg not found.")
        sys.exit(1)

    min_dur = args.min_duration
    max_dur = args.max_duration
    silence_thresh = args.silence_threshold

    log.info(f"Segmenting {len(wav_files)} file(s) into {min_dur}-{max_dur}s clips...")

    segment_count = 0
    for wav_file in wav_files:
        log.info(f"Processing {wav_file.name}...")

        # Use ffmpeg silencedetect to find silence boundaries
        cmd = [
            "ffmpeg", "-i", str(wav_file),
            "-af", f"silencedetect=noise={silence_thresh}dB:d=0.5",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr

        # Parse silence boundaries
        silence_starts = []
        silence_ends = []
        for line in stderr.split("\n"):
            if "silence_start:" in line:
                try:
                    val = float(line.split("silence_start:")[1].strip().split()[0])
                    silence_starts.append(val)
                except (ValueError, IndexError):
                    pass
            if "silence_end:" in line:
                try:
                    val = float(line.split("silence_end:")[1].strip().split()[0])
                    silence_ends.append(val)
                except (ValueError, IndexError):
                    pass

        # Get total duration
        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(wav_file)]
        dur_result = subprocess.run(dur_cmd, capture_output=True, text=True)
        try:
            total_duration = float(dur_result.stdout.strip())
        except ValueError:
            log.warning(f"Could not get duration for {wav_file.name}, skipping")
            continue

        # Build segment boundaries from silence points
        boundaries = [0.0]
        for se in silence_ends:
            if se > boundaries[-1] + min_dur:
                boundaries.append(se)
        if total_duration - boundaries[-1] > min_dur:
            boundaries.append(total_duration)

        # Extract segments, splitting long ones
        segments = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            duration = end - start

            if duration < min_dur:
                continue
            elif duration > max_dur:
                # Split into chunks
                pos = start
                while pos < end:
                    chunk_end = min(pos + max_dur, end)
                    if chunk_end - pos >= min_dur:
                        segments.append((pos, chunk_end))
                    pos = chunk_end
            else:
                segments.append((start, end))

        # Extract each segment
        for i, (start, end) in enumerate(segments):
            seg_name = f"{wav_file.stem}_{i:04d}.wav"
            seg_path = dirs["segments"] / seg_name
            cmd = [
                "ffmpeg", "-y",
                "-i", str(wav_file),
                "-ss", str(start),
                "-to", str(end),
                "-acodec", "pcm_s16le",
                "-ar", "22050",
                "-ac", "1",
                str(seg_path),
            ]
            subprocess.run(cmd, capture_output=True, text=True)
            segment_count += 1

    log.info(f"Created {segment_count} segments in {dirs['segments']}/")
    log.info(f"Review the segments and delete any that are noisy or don't contain the target speaker.")


# ─── Step 3: Transcribe ───────────────────────────────────────────

def cmd_transcribe(args):
    """Auto-transcribe segments using faster-whisper."""
    dirs = _ensure_dirs(args.name)

    segments = sorted(dirs["segments"].glob("*.wav"))
    if not segments:
        log.error(f"No segments found. Run 'segment' first.")
        sys.exit(1)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.error("faster-whisper not installed. Install with: pip install faster-whisper")
        sys.exit(1)

    model_size = args.whisper_model
    log.info(f"Loading Whisper model ({model_size})...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    transcript_file = dirs["transcripts"] / "transcripts.csv"
    results = []

    log.info(f"Transcribing {len(segments)} segments...")
    for i, seg in enumerate(segments):
        try:
            segs, info = model.transcribe(str(seg), language="en")
            text = " ".join(s.text.strip() for s in segs).strip()

            if text and len(text) > 2:
                results.append({
                    "file": seg.name,
                    "text": text,
                    "duration": info.duration,
                })

            if (i + 1) % 50 == 0:
                log.info(f"  {i + 1}/{len(segments)} transcribed...")
        except Exception as e:
            log.warning(f"Failed to transcribe {seg.name}: {e}")

    # Write CSV for review
    with open(transcript_file, "w") as f:
        f.write("file|text\n")
        for r in results:
            # Escape pipes in text
            clean_text = r["text"].replace("|", " ")
            f.write(f"{r['file']}|{clean_text}\n")

    # Also write JSON for programmatic use
    json_file = dirs["transcripts"] / "transcripts.json"
    with open(json_file, "w") as f:
        json.dump(results, f, indent=2)

    log.info(f"Transcribed {len(results)}/{len(segments)} segments")
    log.info(f"CSV: {transcript_file}")
    log.info(f"JSON: {json_file}")
    log.info(f"")
    log.info(f"IMPORTANT: Review {transcript_file} and fix any transcription errors.")
    log.info(f"           Delete rows for clips that are noisy or wrong-speaker.")
    log.info(f"           Quality of transcripts directly affects voice quality.")


# ─── Step 4: Prepare for Piper Training ───────────────────────────

def cmd_prepare(args):
    """Format data for Piper training (create dataset structure)."""
    dirs = _ensure_dirs(args.name)

    transcript_json = dirs["transcripts"] / "transcripts.json"
    transcript_csv = dirs["transcripts"] / "transcripts.csv"

    # Prefer JSON, fall back to CSV
    if transcript_json.exists():
        with open(transcript_json) as f:
            entries = json.load(f)
    elif transcript_csv.exists():
        entries = []
        with open(transcript_csv) as f:
            header = f.readline()
            for line in f:
                parts = line.strip().split("|", 1)
                if len(parts) == 2:
                    entries.append({"file": parts[0], "text": parts[1]})
    else:
        log.error("No transcripts found. Run 'transcribe' first.")
        sys.exit(1)

    # Create Piper dataset structure
    dataset_dir = dirs["training"] / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    # Piper expects: wav files + metadata.csv (LJSpeech format: id|text|text)
    wav_dir = dataset_dir / "wav"
    wav_dir.mkdir(exist_ok=True)

    metadata_lines = []
    copied = 0

    for entry in entries:
        src = dirs["segments"] / entry["file"]
        if not src.exists():
            continue

        # Use stem as ID
        uid = src.stem
        dst = wav_dir / f"{uid}.wav"
        shutil.copy2(str(src), str(dst))

        text = entry["text"].strip()
        metadata_lines.append(f"{uid}|{text}|{text}")
        copied += 1

    # Write metadata
    metadata_file = dataset_dir / "metadata.csv"
    with open(metadata_file, "w") as f:
        f.write("\n".join(metadata_lines) + "\n")

    # Write Piper config
    config = {
        "dataset": str(dataset_dir),
        "quality": args.quality,
        "language": {
            "code": "en-us",
            "family": "en",
            "region": "US",
            "name_native": "English",
            "name_english": "English",
            "country_english": "United States",
        },
        "audio": {
            "sample_rate": 22050,
        },
        "num_speakers": 1,
        "speaker_id_map": {},
    }

    config_file = dirs["training"] / "config.json"
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    log.info(f"Prepared {copied} utterances in {dataset_dir}/")
    log.info(f"Metadata: {metadata_file}")
    log.info(f"Config: {config_file}")
    log.info(f"Quality: {args.quality}")
    log.info(f"")
    log.info(f"Ready for training. Run: python scripts/voice_clone.py train --name {args.name}")


# ─── Step 5: Train ────────────────────────────────────────────────

def cmd_train(args):
    """Train a Piper VITS model. Requires GPU + pytorch-lightning."""
    dirs = _ensure_dirs(args.name)

    config_file = dirs["training"] / "config.json"
    dataset_dir = dirs["training"] / "dataset"

    if not config_file.exists():
        log.error("No training config found. Run 'prepare' first.")
        sys.exit(1)

    with open(config_file) as f:
        config = json.load(f)

    quality = config.get("quality", "medium")
    dataset = config.get("dataset", str(dataset_dir))

    # Check pytorch-lightning
    try:
        import pytorch_lightning
    except ImportError:
        log.error("pytorch-lightning not installed.")
        log.info("Install with: pip install pytorch-lightning")
        sys.exit(1)

    # Preprocess first
    log.info("Step 1/2: Preprocessing (phonemization)...")
    preprocess_cmd = [
        sys.executable, "-m", "piper_train.preprocess",
        "--language", "en-us",
        "--input-dir", dataset,
        "--output-dir", str(dirs["training"] / "preprocessed"),
        "--dataset-format", "ljspeech",
        "--single-speaker",
        "--sample-rate", "22050",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PIPER_TRAIN_DIR) + ":" + env.get("PYTHONPATH", "")

    result = subprocess.run(preprocess_cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"Preprocessing failed: {result.stderr[-500:]}")
        sys.exit(1)

    log.info("Step 2/2: Training (this will take a while)...")

    # Quality → batch size / epochs mapping
    quality_map = {
        "x_low":  {"batch": 32, "epochs": 5000},
        "low":    {"batch": 16, "epochs": 8000},
        "medium": {"batch": 8,  "epochs": 10000},
        "high":   {"batch": 4,  "epochs": 15000},
    }
    settings = quality_map.get(quality, quality_map["medium"])

    # Adjust batch size for smaller GPUs
    if args.low_memory:
        settings["batch"] = max(2, settings["batch"] // 4)
        log.info(f"Low-memory mode: batch_size={settings['batch']}")

    checkpoint_dir = dirs["training"] / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)

    train_cmd = [
        sys.executable, "-m", "piper_train",
        "--dataset-dir", str(dirs["training"] / "preprocessed"),
        "--accelerator", "gpu" if not args.cpu else "cpu",
        "--devices", "1",
        "--batch-size", str(settings["batch"]),
        "--validation-split", "0.05",
        "--max-epochs", str(args.max_epochs or settings["epochs"]),
        "--checkpoint-epochs", str(args.checkpoint_every),
        "--quality", quality,
        "--default-root-dir", str(checkpoint_dir),
    ]

    # Resume from checkpoint if available
    ckpts = sorted(checkpoint_dir.glob("**/*.ckpt"))
    if ckpts:
        latest = ckpts[-1]
        log.info(f"Resuming from checkpoint: {latest.name}")
        train_cmd.extend(["--resume_from_checkpoint", str(latest)])

    log.info(f"Training command: {' '.join(train_cmd)}")
    log.info(f"Batch size: {settings['batch']}, Max epochs: {args.max_epochs or settings['epochs']}")
    log.info(f"Checkpoints saved every {args.checkpoint_every} epochs to {checkpoint_dir}/")
    log.info(f"")
    log.info(f"Press Ctrl+C to stop training. You can resume later.")
    log.info(f"─" * 60)

    result = subprocess.run(train_cmd, env=env)
    if result.returncode != 0:
        log.warning(f"Training exited with code {result.returncode}")
        log.info("You can resume training by running this command again.")
    else:
        log.info("Training complete!")
        log.info(f"Run: python scripts/voice_clone.py export --name {args.name}")


# ─── Step 6: Export to ONNX ───────────────────────────────────────

def cmd_export(args):
    """Export trained checkpoint to ONNX for Piper inference."""
    dirs = _ensure_dirs(args.name)
    checkpoint_dir = dirs["training"] / "checkpoints"

    ckpts = sorted(checkpoint_dir.glob("**/*.ckpt"))
    if not ckpts:
        log.error(f"No checkpoints found in {checkpoint_dir}. Run 'train' first.")
        sys.exit(1)

    # Use specified or latest checkpoint
    if args.checkpoint:
        ckpt = Path(args.checkpoint)
    else:
        ckpt = ckpts[-1]

    output_onnx = dirs["output"] / f"{args.name}.onnx"
    log.info(f"Exporting {ckpt.name} → {output_onnx.name}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PIPER_TRAIN_DIR) + ":" + env.get("PYTHONPATH", "")

    export_cmd = [
        sys.executable, "-m", "piper_train.export_onnx",
        str(ckpt),
        str(output_onnx),
    ]

    result = subprocess.run(export_cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"Export failed: {result.stderr[-500:]}")
        sys.exit(1)

    # Also create the JSON config that Piper needs alongside the ONNX
    config_json = dirs["output"] / f"{args.name}.onnx.json"
    piper_config = {
        "audio": {"sample_rate": 22050, "quality": "medium"},
        "espeak": {"voice": "en-us"},
        "language": {"code": "en_US", "family": "en", "region": "US",
                     "name_native": "English", "name_english": "English",
                     "country_english": "United States"},
        "inference": {"noise_scale": 0.667, "length_scale": 1.0, "noise_w": 0.8},
        "phoneme_type": "espeak",
        "num_speakers": 1,
        "speaker_id_map": {},
        "dataset": args.name,
        "piper_version": "1.0.0",
    }
    with open(config_json, "w") as f:
        json.dump(piper_config, f, indent=2)

    log.info(f"Exported: {output_onnx}")
    log.info(f"Config:   {config_json}")
    log.info(f"Size:     {output_onnx.stat().st_size / 1024 / 1024:.1f} MB")
    log.info(f"")
    log.info(f"Test it:  echo 'Hello world' | piper --model {output_onnx} --output_file /tmp/test.wav && aplay /tmp/test.wav")
    log.info(f"Install:  python scripts/voice_clone.py install --name {args.name}")


# ─── Step 7: Install Voice Profile ────────────────────────────────

def cmd_install(args):
    """Install a trained voice as a REPRYNTT voice profile."""
    dirs = _ensure_dirs(args.name)

    # Find the ONNX model
    onnx_file = dirs["output"] / f"{args.name}.onnx"
    if not onnx_file.exists():
        # Check if user wants to install a pre-existing model file
        if args.model_path:
            onnx_file = Path(args.model_path)
        else:
            log.error(f"No exported model found at {onnx_file}")
            log.info("Run 'export' first, or use --model-path to install an existing .onnx file.")
            sys.exit(1)

    if not onnx_file.exists():
        log.error(f"Model file not found: {onnx_file}")
        sys.exit(1)

    # Copy model to voices directory
    install_dir = VOICES_DIR / "models"
    install_dir.mkdir(parents=True, exist_ok=True)

    installed_model = install_dir / f"{args.name}.onnx"
    shutil.copy2(str(onnx_file), str(installed_model))

    # Copy JSON config if it exists
    json_config = onnx_file.with_suffix(".onnx.json")
    if json_config.exists():
        shutil.copy2(str(json_config), str(installed_model.with_suffix(".onnx.json")))

    # Update voice profiles
    profiles = _load_profiles()
    profiles[args.name] = {
        "name": args.name,
        "display_name": args.display_name or args.name.replace("_", " ").title(),
        "model_path": str(installed_model),
        "description": args.description or f"Custom voice: {args.name}",
        "type": "piper",
    }
    _save_profiles(profiles)

    log.info(f"Installed voice '{args.name}'")
    log.info(f"  Model: {installed_model}")
    log.info(f"  Display: {profiles[args.name]['display_name']}")
    log.info(f"")
    log.info(f"Activate it with:")
    log.info(f"  python scripts/voice_clone.py activate --name {args.name}")
    log.info(f"  OR set PIPER_MODEL={installed_model}")


def cmd_activate(args):
    """Set a voice profile as the active voice for REPRYNTT."""
    profiles = _load_profiles()

    if args.name not in profiles:
        log.error(f"Voice '{args.name}' not found. Available: {', '.join(profiles.keys())}")
        sys.exit(1)

    profile = profiles[args.name]
    model_path = profile["model_path"]

    if not Path(model_path).exists():
        log.error(f"Model file missing: {model_path}")
        sys.exit(1)

    # Update active voice in profiles
    for name in profiles:
        profiles[name]["active"] = (name == args.name)
    _save_profiles(profiles)

    log.info(f"Activated voice: {args.name} ({profile['display_name']})")
    log.info(f"Model: {model_path}")
    log.info(f"")
    log.info(f"The AI will use this voice on next restart, or you can set:")
    log.info(f"  export PIPER_MODEL={model_path}")


def cmd_list(args):
    """List all available voice profiles."""
    profiles = _load_profiles()

    # Also list built-in models
    builtin_dir = Path.home() / "SAIGE" / "models" / "piper"
    builtins = list(builtin_dir.glob("*.onnx")) if builtin_dir.exists() else []

    print("\n─── Voice Profiles ───")
    if not profiles and not builtins:
        print("  No voices installed yet.")
        print(f"  Clone one: python scripts/voice_clone.py pipeline --name myvoice --input audio.wav")
    else:
        for name, p in sorted(profiles.items()):
            active = " ★ ACTIVE" if p.get("active") else ""
            exists = "✓" if Path(p["model_path"]).exists() else "✗ MISSING"
            print(f"  [{exists}] {name}: {p.get('display_name', name)}{active}")
            print(f"       {p['model_path']}")

    if builtins:
        print("\n─── Built-in Voices ───")
        for b in sorted(builtins):
            print(f"  [✓] {b.stem}: {b}")

    print()


# ─── Full Pipeline ─────────────────────────────────────────────────

def cmd_pipeline(args):
    """Run the full voice cloning pipeline end-to-end."""
    log.info(f"═══ Voice Cloning Pipeline: {args.name} ═══")
    log.info(f"Input: {args.input}")
    log.info(f"")

    # Step 1: Extract audio
    log.info("━━━ Step 1/6: Extract Audio ━━━")
    cmd_extract(args)

    # Step 2: Vocal isolation (if requested)
    if not args.skip_isolation:
        log.info("\n━━━ Step 2/6: Isolate Vocals ━━━")
        try:
            cmd_isolate(args)
        except SystemExit:
            log.warning("Vocal isolation failed/skipped. Using raw audio.")
    else:
        log.info("\n━━━ Step 2/6: Skipping vocal isolation ━━━")

    # Step 3: Segment
    log.info("\n━━━ Step 3/6: Segment Audio ━━━")
    cmd_segment(args)

    # Step 4: Transcribe
    log.info("\n━━━ Step 4/6: Transcribe ━━━")
    cmd_transcribe(args)

    # Step 5: Prepare
    log.info("\n━━━ Step 5/6: Prepare Training Data ━━━")
    cmd_prepare(args)

    log.info(f"\n━━━ Preparation Complete! ━━━")
    log.info(f"")
    log.info(f"Your training data is ready at: {VOICES_DIR / args.name / 'training'}")
    log.info(f"")
    log.info(f"REVIEW STEP (recommended):")
    log.info(f"  1. Listen to segments in: {VOICES_DIR / args.name / 'segments'}/")
    log.info(f"  2. Delete any bad clips (noise, wrong speaker, music)")
    log.info(f"  3. Check transcripts: {VOICES_DIR / args.name / 'transcripts' / 'transcripts.csv'}")
    log.info(f"  4. Fix any wrong transcriptions")
    log.info(f"  5. Re-run 'prepare' if you made changes")
    log.info(f"")
    log.info(f"WHEN READY TO TRAIN:")
    log.info(f"  python scripts/voice_clone.py train --name {args.name}")
    log.info(f"")
    log.info(f"  Add --low-memory if training on Jetson Orin Nano")
    log.info(f"  Add --cpu if you have no GPU (very slow)")


# ─── Profile Helpers ───────────────────────────────────────────────

def _load_profiles() -> dict:
    if VOICE_PROFILES_FILE.exists():
        with open(VOICE_PROFILES_FILE) as f:
            return json.load(f)
    return {}


def _save_profiles(profiles: dict):
    VOICE_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(VOICE_PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)


def get_active_voice_model() -> Optional[str]:
    """Get the active voice model path. Used by media.py to load the right voice."""
    profiles = _load_profiles()
    for name, p in profiles.items():
        if p.get("active") and Path(p["model_path"]).exists():
            return p["model_path"]
    return None


# ─── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Voice Cloning Toolkit for REPRYNTT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline from movie file:
  python scripts/voice_clone.py pipeline --name andrew --input ~/movies/bicentennial_man.mkv

  # Step by step:
  python scripts/voice_clone.py extract --name andrew --input movie.mkv
  python scripts/voice_clone.py isolate --name andrew
  python scripts/voice_clone.py segment --name andrew
  python scripts/voice_clone.py transcribe --name andrew
  python scripts/voice_clone.py prepare --name andrew
  python scripts/voice_clone.py train --name andrew --low-memory
  python scripts/voice_clone.py export --name andrew
  python scripts/voice_clone.py install --name andrew

  # Manage voices:
  python scripts/voice_clone.py list
  python scripts/voice_clone.py activate --name andrew
  python scripts/voice_clone.py install --name grandma --model-path /path/to/model.onnx --display-name "Grandma Rose"
        """,
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # ── extract ──
    p = subs.add_parser("extract", help="Extract audio from video/audio file")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--input", required=True, help="Input video/audio file path")

    # ── isolate ──
    p = subs.add_parser("isolate", help="Isolate vocals (remove music/SFX)")
    p.add_argument("--name", required=True, help="Voice project name")

    # ── segment ──
    p = subs.add_parser("segment", help="Split audio into short training clips")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--min-duration", type=float, default=3.0, help="Min clip seconds (default: 3)")
    p.add_argument("--max-duration", type=float, default=15.0, help="Max clip seconds (default: 15)")
    p.add_argument("--silence-threshold", type=int, default=-35, help="Silence threshold in dB (default: -35)")

    # ── transcribe ──
    p = subs.add_parser("transcribe", help="Auto-transcribe segments with Whisper")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--whisper-model", default="small", help="Whisper model size (default: small)")

    # ── prepare ──
    p = subs.add_parser("prepare", help="Format data for Piper training")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--quality", default="medium", choices=["x_low", "low", "medium", "high"],
                    help="Voice quality tier (default: medium)")

    # ── train ──
    p = subs.add_parser("train", help="Train Piper VITS model (requires GPU)")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--low-memory", action="store_true", help="Reduce batch size for low-VRAM GPUs (Jetson, etc)")
    p.add_argument("--cpu", action="store_true", help="Train on CPU (very slow, not recommended)")
    p.add_argument("--max-epochs", type=int, default=None, help="Override max training epochs")
    p.add_argument("--checkpoint-every", type=int, default=500, help="Save checkpoint every N epochs (default: 500)")

    # ── export ──
    p = subs.add_parser("export", help="Export trained model to ONNX")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--checkpoint", default=None, help="Specific checkpoint file (default: latest)")

    # ── install ──
    p = subs.add_parser("install", help="Install a voice into REPRYNTT profiles")
    p.add_argument("--name", required=True, help="Voice profile name")
    p.add_argument("--model-path", default=None, help="Path to .onnx model (if not from training)")
    p.add_argument("--display-name", default=None, help="Human-readable name (e.g., 'Grandma Rose')")
    p.add_argument("--description", default=None, help="Description of the voice")

    # ── activate ──
    p = subs.add_parser("activate", help="Set a voice as the active REPRYNTT voice")
    p.add_argument("--name", required=True, help="Voice profile name to activate")

    # ── list ──
    subs.add_parser("list", help="List all available voice profiles")

    # ── pipeline ──
    p = subs.add_parser("pipeline", help="Run full pipeline: extract → isolate → segment → transcribe → prepare")
    p.add_argument("--name", required=True, help="Voice project name")
    p.add_argument("--input", required=True, help="Input video/audio file path")
    p.add_argument("--skip-isolation", action="store_true", help="Skip demucs vocal isolation")
    p.add_argument("--min-duration", type=float, default=3.0)
    p.add_argument("--max-duration", type=float, default=15.0)
    p.add_argument("--silence-threshold", type=int, default=-35)
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--quality", default="medium", choices=["x_low", "low", "medium", "high"])

    args = parser.parse_args()

    commands = {
        "extract": cmd_extract,
        "isolate": cmd_isolate,
        "segment": cmd_segment,
        "transcribe": cmd_transcribe,
        "prepare": cmd_prepare,
        "train": cmd_train,
        "export": cmd_export,
        "install": cmd_install,
        "activate": cmd_activate,
        "list": cmd_list,
        "pipeline": cmd_pipeline,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
