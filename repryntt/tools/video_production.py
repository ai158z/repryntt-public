"""
video_production.py
═══════════════════════════════════════════════════════════════════
Agentic Video Production Pipeline for SAIGE

End-to-end video content creation: screenplay → shot list → video
generation → editing → audio → final render. Each stage is a tool
callable by the VP-department agents or Jarvis directly.

Supports:
  - xAI grok-imagine-video (primary)
  - Google Gemini image generation (thumbnails, stills)
  - TTS narration (Google Cloud TTS / local piper)
  - FFmpeg-based editing, assembly, and rendering
  - AI music generation (placeholder for Suno/Udio APIs)

Directory layout:
  agent_workspaces/jarvis/video_projects/<project_id>/
    ├── screenplay.json      # Structured screenplay
    ├── shot_list.json        # Director's shot breakdown
    ├── clips/                # Raw generated clips
    ├── audio/                # Music, SFX, narration tracks
    ├── frames/               # Reference frames, stills
    ├── edits/                # Intermediate edits
    ├── renders/              # Final rendered output
    └── project.json          # Master project state
═══════════════════════════════════════════════════════════════════
"""

import base64
import json
import os
import shutil
import subprocess
import time
import uuid
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from repryntt.paths import brain_dir, operator_dir


def _disp_path(p) -> str:
    """Display path for API responses. Relative to the data dir when
    possible, else relative to the project's own base, else absolute.
    Never raises (the old code used relative_to(package_dir) which threw
    when projects live under ~/.repryntt instead of the package)."""
    from pathlib import Path as _P
    try:
        from repryntt.paths import get_data_dir
        return str(_P(p).relative_to(get_data_dir()))
    except Exception:
        try:
            return str(_P(p).relative_to(_P(__file__).parent.parent))
        except Exception:
            return str(p)


logger = logging.getLogger("brain.video_production")

# ════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════

WORKSPACE_ROOT = operator_dir()
PROJECTS_DIR = WORKSPACE_ROOT / "video_projects"

# Video generation defaults
DEFAULT_VIDEO_MODEL = "grok-imagine-video"
DEFAULT_VEO_MODEL = "veo-3.1-generate-preview"
DEFAULT_VEO_FAST_MODEL = "veo-3.1-fast-generate-preview"
VIDEO_COST_PER_SEC_480P = 0.05   # $0.05/sec at 480p (xAI)
VIDEO_COST_PER_SEC_720P = 0.07   # $0.07/sec at 720p (xAI)
VIDEO_COST_PER_SEC = 0.07        # Default to 720p for quality
VEO_COST_PER_CLIP_STANDARD = 0.40  # Veo 3.1 standard per clip
VEO_COST_PER_CLIP_FAST = 0.15      # Veo 3.1 fast per clip
VEO_COST_PER_CLIP_4K = 0.60        # Veo 3.1 4K per clip
MAX_CLIP_DURATION_XAI = 15      # xAI supports up to 15s per generation
MAX_CLIP_DURATION_VEO = 8       # Veo supports up to 8s per generation
MAX_CLIP_DURATION = 15           # Dynamic — updated by _get_video_provider()
DEFAULT_RESOLUTION_VIDEO = "720p"  # 720p or 480p
VIDEO_POLL_INTERVAL = 5          # seconds between status polls
VIDEO_POLL_TIMEOUT = 600         # 10 min max wait per clip
DEFAULT_FPS = 24
DEFAULT_RESOLUTION = "1280x720"

# TTS defaults
DEFAULT_TTS_ENGINE = "google"  # "elevenlabs", "google", or "piper"
DEFAULT_TTS_VOICE = "en-US-Neural2-D"  # Deep, professional male voice (Google)
# ElevenLabs default voice ("Adam" — public stock voice, always available)
DEFAULT_ELEVENLABS_VOICE = "pNInz6obpgDQGcFmaJgB"
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"
DEFAULT_TTS_RATE = 1.0

# Audio defaults
DEFAULT_AUDIO_SAMPLE_RATE = 44100
DEFAULT_AUDIO_CHANNELS = 2

# Quality thresholds for QA
QA_MIN_COHERENCE_SCORE = 7.0   # Out of 10
QA_MIN_AUDIO_SYNC = 0.95       # 95% sync accuracy


def _get_config() -> Dict:
    """Load ai_config.json for API keys."""
    cfg_path = brain_dir() / "ai_config.json"
    if not cfg_path.exists():
        # Fallback to config/ directory in the repo
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "ai_config.json"
    if cfg_path.exists():
        with open(cfg_path, "r") as f:
            raw = json.load(f)
        return raw.get("ai_provider", raw)
    return {}



def _byok_google_key() -> str:
    """Google/Gemini key: env (cloud BYOK) first, then ai_config.json."""
    import os as _os
    return (_os.environ.get("GOOGLE_API_KEY") or _os.environ.get("GEMINI_API_KEY")
            or _get_config().get("google_gemini", {}).get("api_key", ""))


def _byok_xai_key() -> str:
    """xAI key: env (cloud BYOK) first, then ai_config.json."""
    import os as _os
    return (_os.environ.get("XAI_API_KEY")
            or _get_config().get("xai", {}).get("api_key", ""))


def _byok_elevenlabs_key() -> str:
    """ElevenLabs key: env (cloud BYOK) first, then ai_config.json."""
    import os as _os
    return (_os.environ.get("ELEVENLABS_API_KEY")
            or _get_config().get("elevenlabs", {}).get("api_key", ""))


def _byok_falai_key() -> str:
    """fal.ai key: env (cloud BYOK) first, then ai_config.json."""
    import os as _os
    return (_os.environ.get("FAL_KEY") or _os.environ.get("FAL_API_KEY")
            or _get_config().get("falai", {}).get("api_key", ""))


def _project_dir(project_id: str) -> Path:
    """Get project directory, creating structure if needed."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', project_id)
    pdir = PROJECTS_DIR / safe_id
    for sub in ["clips", "audio", "frames", "edits", "renders"]:
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    return pdir


def _load_project(project_id: str) -> Optional[Dict]:
    """Load project state."""
    pdir = _project_dir(project_id)
    proj_file = pdir / "project.json"
    if proj_file.exists():
        with open(proj_file, "r") as f:
            return json.load(f)
    return None


def _save_project(project_id: str, data: Dict):
    """Save project state."""
    pdir = _project_dir(project_id)
    with open(pdir / "project.json", "w") as f:
        json.dump(data, f, indent=2)


def _ffmpeg_available() -> bool:
    """Check if ffmpeg is installed."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _extract_last_frame(clip_path: Path) -> str:
    """Extract the last frame from a video clip and return as base64 data URI.

    Uses ffmpeg to grab the final frame, saves as JPEG, encodes to base64.
    Returns 'data:image/jpeg;base64,...' suitable for xAI image_url parameter.
    Returns empty string on failure.
    """
    if not clip_path.exists():
        logger.warning(f"Cannot extract frame: {clip_path} not found")
        return ""

    frame_dir = clip_path.parent.parent / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frame_dir / f"{clip_path.stem}_last_frame.jpg"

    try:
        # Use ffmpeg to extract the very last frame
        # -sseof -0.1 seeks to 0.1s before end of file
        result = subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(clip_path),
             "-frames:v", "1", "-q:v", "2", str(frame_path)],
            capture_output=True, timeout=30
        )
        if result.returncode != 0 or not frame_path.exists():
            logger.warning(f"ffmpeg last-frame extraction failed: {result.stderr[:200]}")
            return ""

        with open(frame_path, "rb") as f:
            img_bytes = f.read()

        b64 = base64.b64encode(img_bytes).decode("ascii")
        data_uri = f"data:image/jpeg;base64,{b64}"
        logger.info(f"📸 Extracted last frame from {clip_path.name} ({len(img_bytes)//1024}KB)")
        return data_uri

    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Frame extraction error: {e}")
        return ""


def _get_video_provider() -> Dict:
    """Get the active video provider config.

    Returns dict with:
        provider: "veo" or "xai"
        max_clip_sec: max seconds per clip
        cost_fn: callable(duration_sec, resolution) -> cost
        model: model name string
    """
    config = _get_config()
    vp = config.get("video_production", {})
    provider = vp.get("provider", "veo")

    if provider == "veo":
        use_fast = vp.get("veo_use_fast", True)
        model = vp.get("veo_fast_model", DEFAULT_VEO_FAST_MODEL) if use_fast else vp.get("veo_model", DEFAULT_VEO_MODEL)
        cost_per_clip = VEO_COST_PER_CLIP_FAST if use_fast else VEO_COST_PER_CLIP_STANDARD
        return {
            "provider": "veo",
            "max_clip_sec": MAX_CLIP_DURATION_VEO,
            "model": model,
            "cost_per_clip": cost_per_clip,
            "use_fast": use_fast,
        }
    else:
        return {
            "provider": "xai",
            "max_clip_sec": MAX_CLIP_DURATION_XAI,
            "model": vp.get("xai_model", DEFAULT_VIDEO_MODEL),
            "cost_per_clip": None,  # xAI uses per-second pricing
        }


def _set_video_provider(provider: str, use_fast: bool = True) -> bool:
    """Switch the active video provider. Returns True on success."""
    if provider not in ("veo", "xai"):
        return False

    cfg_path = brain_dir() / "ai_config.json"
    if not cfg_path.exists():
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "ai_config.json"
    if not cfg_path.exists():
        return False

    with open(cfg_path, "r") as f:
        raw = json.load(f)

    vp = raw.get("ai_provider", raw).get("video_production", {})
    vp["provider"] = provider
    vp["veo_use_fast"] = use_fast

    # Write back
    target = raw.get("ai_provider", raw)
    target["video_production"] = vp
    with open(cfg_path, "w") as f:
        json.dump(raw, f, indent=4)

    logger.info(f"🔄 Video provider switched to: {provider}" +
                (f" (fast={use_fast})" if provider == "veo" else ""))
    return True


def _generate_clip_veo(prompt: str, duration_sec: int = 8,
                       resolution: str = "720p",
                       aspect_ratio: str = "16:9",
                       image_data: str = "",
                       last_frame_data: str = "",
                       clip_path: Path = None,
                       model: str = "") -> Dict:
    """Generate a video clip using Google Veo 3.1 via the genai SDK.

    Args:
        prompt: Text prompt
        duration_sec: 4, 6, or 8 seconds
        resolution: "720p" or "1080p" (4k only for 8s)
        aspect_ratio: "16:9" or "9:16"
        image_data: Base64 data URI for image-to-video (first frame)
        last_frame_data: Base64 data URI for interpolation (last frame)
        clip_path: Where to save the output .mp4

    Returns:
        Dict with success, file_path, cost_usd, duration_sec, generation_time_sec, etc.
    """
    from google import genai
    from google.genai import types

    import os as _os
    # Cloud/BYOK: worker injects the customer's Google key as env var.
    # Self-hosted: fall back to ai_config.json google_gemini key.
    config = _get_config()
    gemini_key = (
        _os.environ.get("GOOGLE_API_KEY")
        or _os.environ.get("GEMINI_API_KEY")
        or _byok_google_key()
    )
    if not gemini_key:
        return {"success": False, "error": "No Gemini API key configured"}

    # Model selection precedence:
    #   1. explicit `model` arg (from the dashboard / API call)
    #   2. REPRYNTT_VEO_MODEL env (cloud per-request override)
    #   3. ai_config.json video_production (self-hosted) — fast by default
    vp = config.get("video_production", {})
    model = model or _os.environ.get("REPRYNTT_VEO_MODEL", "")
    if not model:
        model = (vp.get("veo_fast_model", DEFAULT_VEO_FAST_MODEL)
                 if vp.get("veo_use_fast", True)
                 else vp.get("veo_model", DEFAULT_VEO_MODEL))
    # The genai SDK wants the bare model id, not the "models/..." resource name.
    if model.startswith("models/"):
        model = model[len("models/"):]
    # Pricing follows the resolved model (always defined regardless of how the
    # model was chosen — fixes UnboundLocalError when a model is passed in).
    use_fast = "fast" in model.lower()

    # Veo durations: 4, 6, or 8 seconds
    if duration_sec <= 4:
        duration_sec = 4
    elif duration_sec <= 6:
        duration_sec = 6
    else:
        duration_sec = 8

    client = genai.Client(api_key=gemini_key)

    gen_config = types.GenerateVideosConfig(
        aspect_ratio=aspect_ratio,
        person_generation="allow_all",
    )

    # Only set resolution for non-fast models or if explicitly 720p
    if resolution in ("720p", "1080p"):
        gen_config.resolution = resolution

    # Build kwargs for generate_videos
    gen_kwargs = {
        "model": model,
        "prompt": prompt,
        "config": gen_config,
    }

    # Image-to-video: use first frame for scene continuity
    first_frame_image = None
    last_frame_image = None
    if image_data and image_data.startswith("data:image/"):
        # Decode base64 data URI to bytes
        header, b64_str = image_data.split(",", 1)
        mime = header.split(":")[1].split(";")[0]  # e.g. "image/jpeg"
        img_bytes = base64.b64decode(b64_str)
        first_frame_image = types.Image(image_bytes=img_bytes, mime_type=mime)
        gen_kwargs["image"] = first_frame_image
        logger.info(f"🔗 Veo: using reference frame as first frame ({len(img_bytes)//1024}KB)")

    if last_frame_data and last_frame_data.startswith("data:image/"):
        header, b64_str = last_frame_data.split(",", 1)
        mime = header.split(":")[1].split(";")[0]
        img_bytes = base64.b64decode(b64_str)
        last_frame_image = types.Image(image_bytes=img_bytes, mime_type=mime)
        gen_config.last_frame = last_frame_image
        logger.info(f"🔗 Veo: using last frame for interpolation ({len(img_bytes)//1024}KB)")

    # ── Submit and poll ──
    poll_start = time.time()
    try:
        logger.info(f"🎬 Veo generation starting: {model} ({duration_sec}s, {resolution}, {aspect_ratio})")
        operation = client.models.generate_videos(**gen_kwargs)

        # Poll until done (SDK handles this with operation.done)
        while not operation.done:
            time.sleep(VIDEO_POLL_INTERVAL)
            operation = client.operations.get(operation)
            elapsed = int(time.time() - poll_start)
            if elapsed > VIDEO_POLL_TIMEOUT:
                return {"success": False, "error": f"Veo generation timed out after {VIDEO_POLL_TIMEOUT}s"}
            logger.debug(f"  Veo polling... {elapsed}s elapsed")

    except Exception as e:
        return {"success": False, "error": f"Veo generation failed: {str(e)[:300]}"}

    gen_time = int(time.time() - poll_start)

    # ── Download video ──
    try:
        generated_video = operation.response.generated_videos[0]
        video_file = generated_video.video

        # Download using SDK
        client.files.download(file=video_file)
        video_file.save(str(clip_path))

        size_kb = clip_path.stat().st_size / 1024
    except Exception as e:
        return {"success": False, "error": f"Veo download failed: {str(e)[:300]}"}

    cost = VEO_COST_PER_CLIP_FAST if use_fast else VEO_COST_PER_CLIP_STANDARD
    actual_duration = getattr(generated_video, 'duration', duration_sec) or duration_sec

    logger.info(f"🎥 Veo clip: {clip_path.name} ({size_kb:.0f}KB, {actual_duration}s, ${cost}, took {gen_time}s)")

    return {
        "success": True,
        "file_path": str(clip_path),
        "size_kb": round(size_kb, 1),
        "duration_sec": actual_duration,
        "cost_usd": cost,
        "generation_time_sec": gen_time,
        "model": model,
        "provider": "veo",
        "has_audio": True,
    }


def _generate_clip_falai(prompt: str, model: str, clip_path: Path,
                         duration_sec: int = 5, aspect_ratio: str = "16:9",
                         resolution: str = "720p",
                         image_url: str = "") -> Dict:
    """Generate a clip via fal.ai's queue API.

    fal.ai is a single gateway to many video models (Kling, Seedance, Luma,
    Hunyuan, Wan, …). `model` is the fal model id, e.g.
    "fal-ai/kling-video/v2.1/master/text-to-video".

    We keep the request body minimal (just `prompt`, plus `image_url` for
    image-to-video models) because fal validates input schemas strictly and
    rejects unknown fields — model defaults handle duration/resolution.
    """
    import requests as _req

    api_key = _byok_falai_key()
    if not api_key:
        return {"success": False, "error": "No fal.ai API key configured"}
    if not model:
        return {"success": False, "error": "No fal.ai model id provided"}

    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
    base = f"https://queue.fal.run/{model}"
    body: Dict[str, Any] = {"prompt": prompt}
    if image_url and image_url.startswith(("http://", "https://", "data:")):
        body["image_url"] = image_url

    poll_start = time.time()
    try:
        logger.info(f"🎬 fal.ai generation starting: {model}")
        sub = _req.post(base, headers=headers, json=body, timeout=60)
        if sub.status_code >= 400:
            return {"success": False,
                    "error": f"fal.ai submit failed: {sub.status_code} {sub.text[:300]}"}
        sub_data = sub.json()
        request_id = sub_data.get("request_id", "")
        status_url = sub_data.get("status_url") or f"{base}/requests/{request_id}/status"
        response_url = sub_data.get("response_url") or f"{base}/requests/{request_id}"
        if not request_id:
            return {"success": False, "error": "fal.ai returned no request_id"}
    except Exception as e:
        return {"success": False, "error": f"fal.ai submit error: {str(e)[:300]}"}

    # ── Poll until COMPLETED ──
    while (time.time() - poll_start) < VIDEO_POLL_TIMEOUT:
        time.sleep(VIDEO_POLL_INTERVAL)
        try:
            st = _req.get(status_url, headers=headers, timeout=15)
            st.raise_for_status()
            status = st.json().get("status", "")
        except Exception as e:
            logger.warning(f"fal.ai poll error (will retry): {e}")
            continue
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR", "CANCELLED"):
            return {"success": False, "error": f"fal.ai job {status.lower()}"}

    # ── Fetch result + extract a video URL ──
    try:
        res = _req.get(response_url, headers=headers, timeout=30)
        res.raise_for_status()
        result = res.json()
    except Exception as e:
        return {"success": False, "error": f"fal.ai result fetch failed: {str(e)[:300]}"}

    video_url = ""
    vid = result.get("video")
    if isinstance(vid, dict):
        video_url = vid.get("url", "")
    elif isinstance(vid, str):
        video_url = vid
    if not video_url:
        videos = result.get("videos")
        if isinstance(videos, list) and videos:
            first = videos[0]
            video_url = first.get("url", "") if isinstance(first, dict) else str(first)
    if not video_url:
        return {"success": False,
                "error": f"fal.ai result had no video url: {json.dumps(result)[:300]}"}

    try:
        clip_resp = _req.get(video_url, timeout=180)
        clip_resp.raise_for_status()
        with open(clip_path, "wb") as f:
            f.write(clip_resp.content)
        size_kb = clip_path.stat().st_size / 1024
    except Exception as e:
        return {"success": False, "error": f"fal.ai clip download failed: {str(e)[:300]}"}

    gen_time = int(time.time() - poll_start)
    cost = 0.30  # rough per-clip estimate; fal bills the customer directly
    logger.info(f"🎥 fal.ai clip: {clip_path.name} ({size_kb:.0f}KB, ~${cost}, took {gen_time}s, {model})")

    return {
        "success": True,
        "file_path": str(clip_path),
        "size_kb": round(size_kb, 1),
        "duration_sec": duration_sec,
        "cost_usd": cost,
        "generation_time_sec": gen_time,
        "model": model,
        "provider": "falai",
        "has_audio": True,
    }


# ════════════════════════════════════════════════════════════════════
# TOOL 1: create_video_project
# ════════════════════════════════════════════════════════════════════

def create_video_project(title: str = "", genre: str = "documentary",
                         episodes: int = 1, episode_duration: int = 300,
                         style: str = "", target_audience: str = "",
                         **kwargs) -> str:
    """Create a new video production project with full pipeline setup.

    This initializes the project workspace, calculates budget, and sets up
    the production framework. Must be called before any other video tools.

    Parameters:
        title: Project title (e.g. "The Future of AI")
        genre: Content genre — documentary, explainer, narrative, commercial,
               music_video, tutorial, short_film, trailer, social_content
        episodes: Number of episodes (1-50)
        episode_duration: Target duration per episode in seconds (30-600)
        style: Visual style guide (e.g. "cinematic, dark moody lighting,
               sci-fi aesthetic, 4K film grain")
        target_audience: Who this is for (helps guide tone and pacing)

    Returns:
        JSON with project_id, budget estimate, and production plan.
    """
    if not title:
        return json.dumps({"error": "title is required"})

    episodes = max(1, min(50, int(episodes)))
    episode_duration = max(30, min(600, int(episode_duration)))

    project_id = f"{re.sub(r'[^a-z0-9]', '_', title.lower())[:30]}_{int(time.time()) % 100000}"
    pdir = _project_dir(project_id)

    total_seconds = episodes * episode_duration
    estimated_cost = total_seconds * VIDEO_COST_PER_SEC
    # Clips are 5-10s each; ~30% re-rolls expected for quality
    estimated_clips = int(total_seconds / 7) * 1.3
    estimated_api_calls = int(estimated_clips)

    project = {
        "project_id": project_id,
        "title": title,
        "genre": genre,
        "style": style or _default_style(genre),
        "target_audience": target_audience,
        "episodes": episodes,
        "episode_duration": episode_duration,
        "total_seconds": total_seconds,
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "budget": {
            "estimated_video_cost": round(estimated_cost, 2),
            "estimated_clips": estimated_api_calls,
            "estimated_rerolls": int(estimated_clips * 0.3),
            "cost_per_second": VIDEO_COST_PER_SEC,
        },
        "pipeline_state": {
            "screenplay": "pending",
            "shot_list": "pending",
            "video_generation": "pending",
            "audio_production": "pending",
            "editing": "pending",
            "qa_review": "pending",
            "final_render": "pending",
        },
        "episodes_data": [],
        "quality_settings": {
            "resolution": DEFAULT_RESOLUTION,
            "fps": DEFAULT_FPS,
            "min_coherence": QA_MIN_COHERENCE_SCORE,
            "style_guide": style or _default_style(genre),
            "color_palette": "",
            "character_refs": {},
        },
    }

    _save_project(project_id, project)
    logger.info(f"🎬 Created video project: {title} ({project_id}) — "
                f"{episodes} eps × {episode_duration}s = {total_seconds}s total, "
                f"est. ${estimated_cost:.2f}")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "title": title,
        "genre": genre,
        "episodes": episodes,
        "episode_duration_sec": episode_duration,
        "total_runtime_sec": total_seconds,
        "estimated_cost_usd": round(estimated_cost, 2),
        "estimated_clips": estimated_api_calls,
        "style_guide": project["quality_settings"]["style_guide"],
        "project_dir": _disp_path(pdir),
        "next_step": "Use write_screenplay() to create the screenplay",
    })


def _default_style(genre: str) -> str:
    """Generate default visual style guide for a genre."""
    styles = {
        "documentary": "cinematic, natural lighting, shallow depth of field, steady camera, muted color grading, professional 4K footage",
        "explainer": "clean, bright, modern motion graphics, smooth transitions, bold colors, minimal distractions, professional typography",
        "narrative": "cinematic, dramatic lighting, film grain, wide and close-up shots, emotional color grading, professional cinematography",
        "commercial": "polished, vibrant colors, dynamic camera movement, product-focused, high contrast, aspirational mood",
        "music_video": "stylized, bold colors, rhythmic cuts synced to beat, creative camera angles, artistic lighting, high energy",
        "tutorial": "clean, well-lit, screen-capture style, face-to-camera, step-by-step visual flow, professional but approachable",
        "short_film": "cinematic, narrative-driven, atmospheric lighting, careful shot composition, film-quality color grading",
        "trailer": "high-impact, fast cuts, dramatic lighting, bass-heavy audio, text overlays, tension-building pacing",
        "social_content": "vertical-first, attention-grabbing, bright saturated colors, fast-paced, text overlays, trend-aware",
    }
    return styles.get(genre, styles["documentary"])


# ════════════════════════════════════════════════════════════════════
# TOOL 2: write_screenplay
# ════════════════════════════════════════════════════════════════════

def write_screenplay(project_id: str = "", episode: int = 1,
                     screenplay_text: str = "", **kwargs) -> str:
    """Write or update the screenplay for an episode.

    The screenplay should be a structured JSON-compatible format with scenes,
    each containing visual description, dialogue/narration, duration, and mood.

    Parameters:
        project_id: Project ID from create_video_project()
        episode: Episode number (1-based)
        screenplay_text: Structured screenplay as JSON string. Format:
            {
              "episode_title": "The Dawn of Intelligence",
              "episode_number": 1,
              "total_duration_sec": 300,
              "scenes": [
                {
                  "scene_number": 1,
                  "title": "Opening — The Question",
                  "duration_sec": 30,
                  "visual_description": "Slow aerial shot of a vast server farm at dawn...",
                  "narration": "What does it mean to think?",
                  "dialogue": [],
                  "mood": "contemplative, mysterious",
                  "music_cue": "ambient electronic, low drone, building tension",
                  "sfx": ["wind", "distant humming of servers"],
                  "transition_in": "fade_from_black",
                  "transition_out": "dissolve"
                }
              ]
            }

    Returns:
        JSON with screenplay validation and scene count.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    if not screenplay_text:
        return json.dumps({"error": "screenplay_text is required — provide structured JSON screenplay"})

    # Parse screenplay
    try:
        if isinstance(screenplay_text, str):
            screenplay = json.loads(screenplay_text)
        else:
            screenplay = screenplay_text
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in screenplay: {str(e)[:200]}"})

    # Validate structure
    scenes = screenplay.get("scenes", [])
    if not scenes:
        return json.dumps({"error": "Screenplay must contain at least one scene"})

    # Validate each scene
    required_fields = ["scene_number", "visual_description", "duration_sec"]
    for i, scene in enumerate(scenes):
        missing = [f for f in required_fields if f not in scene]
        if missing:
            return json.dumps({"error": f"Scene {i+1} missing required fields: {missing}"})

    total_duration = sum(s.get("duration_sec", 0) for s in scenes)
    screenplay["total_duration_sec"] = total_duration
    screenplay["episode_number"] = episode
    screenplay["scene_count"] = len(scenes)
    screenplay["validated_at"] = datetime.now(timezone.utc).isoformat()

    # Save screenplay file
    pdir = _project_dir(project_id)
    sp_file = pdir / f"screenplay_ep{episode:02d}.json"
    with open(sp_file, "w") as f:
        json.dump(screenplay, f, indent=2)

    # Update project state
    project["pipeline_state"]["screenplay"] = "complete"
    project["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Track episode data
    ep_data = {
        "episode": episode,
        "title": screenplay.get("episode_title", f"Episode {episode}"),
        "scenes": len(scenes),
        "duration_sec": total_duration,
        "screenplay_file": _disp_path(sp_file),
    }
    # Update or append episode
    existing = [e for e in project["episodes_data"] if e["episode"] == episode]
    if existing:
        existing[0].update(ep_data)
    else:
        project["episodes_data"].append(ep_data)

    _save_project(project_id, project)

    logger.info(f"📝 Screenplay saved: {project['title']} Ep{episode} — "
                f"{len(scenes)} scenes, {total_duration}s")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "scene_count": len(scenes),
        "total_duration_sec": total_duration,
        "screenplay_file": _disp_path(sp_file),
        "scenes_summary": [
            {"scene": s.get("scene_number"), "title": s.get("title", ""),
             "duration": s.get("duration_sec"), "mood": s.get("mood", "")}
            for s in scenes
        ],
        "next_step": "Use create_shot_list() to break scenes into individual shots",
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 3: create_shot_list
# ════════════════════════════════════════════════════════════════════

def create_shot_list(project_id: str = "", episode: int = 1,
                     shot_list_text: str = "", **kwargs) -> str:
    """Create the director's shot list from the screenplay.

    Each shot becomes an individual video generation prompt. The director
    breaks scenes into 3-10 second clips with specific visual prompts
    optimized for the AI video generation model.

    Parameters:
        project_id: Project ID
        episode: Episode number
        shot_list_text: Structured shot list as JSON string. Format:
            {
              "shots": [
                {
                  "shot_id": "ep01_s01_001",
                  "scene": 1,
                  "duration_sec": 5,
                  "shot_type": "wide_establishing",
                  "camera_movement": "slow_dolly_forward",
                  "prompt": "Cinematic aerial shot of a massive server farm at golden hour, rows of cooling towers releasing steam, warm sunlight through clouds, professional 4K footage, shallow depth of field",
                  "negative_prompt": "text, watermark, low quality, blurry, cartoon",
                  "style_override": "",
                  "reference_frame": "",
                  "audio_sync": "narration_start",
                  "transition_in": "fade_from_black",
                  "transition_out": "dissolve",
                  "priority": 1
                }
              ]
            }

    Returns:
        JSON with shot count and estimated generation cost.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    if not shot_list_text:
        return json.dumps({"error": "shot_list_text is required"})

    try:
        if isinstance(shot_list_text, str):
            shot_list = json.loads(shot_list_text)
        else:
            shot_list = shot_list_text
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {str(e)[:200]}"})

    shots = shot_list.get("shots", [])
    if not shots:
        return json.dumps({"error": "Shot list must contain at least one shot"})

    # Validate and enrich shots
    for i, shot in enumerate(shots):
        if "prompt" not in shot:
            return json.dumps({"error": f"Shot {i+1} missing 'prompt' field"})
        if "duration_sec" not in shot:
            shot["duration_sec"] = 5
        if "shot_id" not in shot:
            shot["shot_id"] = f"ep{episode:02d}_shot_{i+1:03d}"
        # Scene continuity: first shot always false, others default to true
        if "continues_previous" not in shot:
            shot["continues_previous"] = (i > 0)
        if i == 0:
            shot["continues_previous"] = False
        shot["status"] = "pending"
        shot["attempts"] = 0
        shot["clip_file"] = ""

    total_duration = sum(s["duration_sec"] for s in shots)
    estimated_cost = total_duration * VIDEO_COST_PER_SEC

    shot_list["episode"] = episode
    shot_list["shot_count"] = len(shots)
    shot_list["total_duration_sec"] = total_duration
    shot_list["estimated_cost"] = round(estimated_cost, 2)
    shot_list["created_at"] = datetime.now(timezone.utc).isoformat()

    # Save
    pdir = _project_dir(project_id)
    sl_file = pdir / f"shot_list_ep{episode:02d}.json"
    with open(sl_file, "w") as f:
        json.dump(shot_list, f, indent=2)

    project["pipeline_state"]["shot_list"] = "complete"
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    logger.info(f"🎬 Shot list created: {project['title']} Ep{episode} — "
                f"{len(shots)} shots, {total_duration}s, est. ${estimated_cost:.2f}")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "shot_count": len(shots),
        "total_duration_sec": total_duration,
        "estimated_cost_usd": round(estimated_cost, 2),
        "shot_list_file": _disp_path(sl_file),
        "shot_types": list(set(s.get("shot_type", "unspecified") for s in shots)),
        "next_step": "Use generate_video_clip() for each shot, or generate_all_clips() for batch",
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 4: generate_video_clip
# ════════════════════════════════════════════════════════════════════

def generate_video_clip(project_id: str = "", shot_id: str = "",
                        prompt: str = "", duration_sec: int = 5,
                        model: str = "", resolution: str = "",
                        aspect_ratio: str = "16:9",
                        image_url: str = "",
                        provider: str = "", **kwargs) -> str:
    """Generate a single video clip using the configured video provider.

    Dynamically dispatches to either Google Veo 3.1 or xAI Grok Imagine Video
    based on the 'provider' parameter or the config in ai_config.json.

    Parameters:
        project_id: Project ID (required if using shot list)
        shot_id: Shot ID from the shot list (auto-fills prompt/duration)
        prompt: Text prompt for video generation (overrides shot list prompt)
        duration_sec: Clip duration in seconds (1-15 for xAI, 4/6/8 for Veo)
        model: Override video model
        resolution: Video resolution — "720p" (HD) or "480p" (faster)
        aspect_ratio: Aspect ratio — "16:9", "9:16", "1:1", "4:3", "3:2"
        image_url: Reference image (base64 data URI) for scene continuity
        provider: Override provider — "veo" or "xai" (default: from config)

    Returns:
        JSON with clip file path, cost, and quality metadata.
    """
    import requests as _req

    config = _get_config()
    vprov = _get_video_provider()
    active_provider = provider or vprov["provider"]
    resolution = resolution or DEFAULT_RESOLUTION_VIDEO

    # Load shot from shot list if available
    sl_file = None
    sl = None
    shot_data = None
    project = None
    if project_id:
        pdir = _project_dir(project_id)
        project = _load_project(project_id)
        if not project:
            return json.dumps({"error": f"Project '{project_id}' not found"})

        if shot_id:
            for ep_num in range(1, project.get("episodes", 1) + 1):
                sl_path = pdir / f"shot_list_ep{ep_num:02d}.json"
                if sl_path.exists():
                    with open(sl_path) as f:
                        sl = json.load(f)
                    for shot in sl.get("shots", []):
                        if shot.get("shot_id") == shot_id:
                            shot_data = shot
                            sl_file = sl_path
                            break
                    if shot_data:
                        break

        if shot_data:
            if not prompt:
                prompt = shot_data["prompt"]
                style = project.get("quality_settings", {}).get("style_guide", "")
                if style and style not in prompt:
                    prompt = f"{prompt}, {style}"
            if duration_sec == 5:
                duration_sec = shot_data.get("duration_sec", 5)

    if not prompt:
        return json.dumps({"error": "prompt is required — describe the video clip to generate"})

    # Determine clip path
    clip_id = f"clip_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    if project_id:
        pdir = _project_dir(project_id)
        clip_filename = f"{shot_id or clip_id}.mp4"
        clip_path = pdir / "clips" / clip_filename
    else:
        os.makedirs(WORKSPACE_ROOT / "video_clips", exist_ok=True)
        clip_filename = f"{clip_id}.mp4"
        clip_path = WORKSPACE_ROOT / "video_clips" / clip_filename

    # ════════════════════════════════════════════════════════════════
    # DISPATCH: Veo provider
    # ════════════════════════════════════════════════════════════════
    if active_provider == "veo":
        duration_sec = max(4, min(MAX_CLIP_DURATION_VEO, int(duration_sec)))
        veo_result = _generate_clip_veo(
            prompt=prompt,
            duration_sec=duration_sec,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            image_data=image_url,
            clip_path=clip_path,
            model=model,
        )

        if not veo_result.get("success"):
            return json.dumps({
                "success": False,
                "error": veo_result.get("error", "Veo generation failed"),
                "provider": "veo",
                "shot_id": shot_id,
            })

        # Update shot list status
        if project_id and shot_id and shot_data and sl_file and sl:
            shot_data["status"] = "generated"
            shot_data["attempts"] = shot_data.get("attempts", 0) + 1
            shot_data["clip_file"] = _disp_path(clip_path)
            with open(sl_file, "w") as f:
                json.dump(sl, f, indent=2)

        return json.dumps({
            "success": True,
            "clip_id": clip_id,
            "shot_id": shot_id,
            "file_path": _disp_path(clip_path),
            "size_kb": veo_result.get("size_kb", 0),
            "duration_sec": veo_result.get("duration_sec", duration_sec),
            "resolution": resolution,
            "cost_usd": veo_result.get("cost_usd", 0),
            "generation_time_sec": veo_result.get("generation_time_sec", 0),
            "model": veo_result.get("model", "veo-3.1"),
            "provider": "veo",
            "has_audio": True,
            "prompt_used": prompt[:200],
        })

    # ════════════════════════════════════════════════════════════════
    # DISPATCH: fal.ai provider (Kling / Seedance / Luma / Hunyuan / Wan …)
    # ════════════════════════════════════════════════════════════════
    if active_provider == "falai":
        fal_result = _generate_clip_falai(
            prompt=prompt,
            model=model,
            clip_path=clip_path,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            image_url=image_url,
        )
        if not fal_result.get("success"):
            return json.dumps({
                "success": False,
                "error": fal_result.get("error", "fal.ai generation failed"),
                "provider": "falai",
                "shot_id": shot_id,
            })
        if project_id and shot_id and shot_data and sl_file and sl:
            shot_data["status"] = "generated"
            shot_data["attempts"] = shot_data.get("attempts", 0) + 1
            shot_data["clip_file"] = _disp_path(clip_path)
            with open(sl_file, "w") as f:
                json.dump(sl, f, indent=2)
        return json.dumps({
            "success": True,
            "clip_id": clip_id,
            "shot_id": shot_id,
            "file_path": _disp_path(clip_path),
            "size_kb": fal_result.get("size_kb", 0),
            "cost_usd": fal_result.get("cost_usd", 0),
            "generation_time_sec": fal_result.get("generation_time_sec", 0),
            "model": fal_result.get("model", model),
            "provider": "falai",
            "has_audio": fal_result.get("has_audio", True),
            "prompt_used": prompt[:200],
        })

    # ════════════════════════════════════════════════════════════════
    # DISPATCH: xAI provider (original)
    # ════════════════════════════════════════════════════════════════
    xai_cfg = config.get("xai", {})
    xai_key = (__import__("os").environ.get("XAI_API_KEY") or xai_cfg.get("api_key", ""))
    if xai_key == "YOUR_XAI_API_KEY_HERE":
        xai_key = ""
    xai_endpoint = xai_cfg.get("video_endpoint",
                   "https://api.x.ai/v1/videos/generations")

    duration_sec = max(1, min(MAX_CLIP_DURATION_XAI, int(duration_sec)))
    cost_rate = VIDEO_COST_PER_SEC_720P if resolution == "720p" else VIDEO_COST_PER_SEC_480P
    cost = round(duration_sec * cost_rate, 2)

    if not xai_key:
        result = {
            "success": False,
            "error": "No xAI API key configured. Add xai.api_key to brain/ai_config.json",
            "clip_id": clip_id,
            "prompt": prompt[:200],
            "duration_sec": duration_sec,
            "estimated_cost_usd": cost,
            "dry_run": True,
        }
        return json.dumps(result)

    headers = {
        "Authorization": f"Bearer {xai_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model or DEFAULT_VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration_sec,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }

    if image_url:
        payload["image_url"] = image_url
        logger.info(f"🔗 Using reference frame for scene continuity (image_url={len(image_url)} chars)")

    try:
        resp = _req.post(xai_endpoint, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            return json.dumps({
                "error": "Rate limited by video API",
                "retry_after_sec": retry_after,
                "shot_id": shot_id,
            })
        resp.raise_for_status()
        start_data = resp.json()
    except _req.exceptions.RequestException as e:
        return json.dumps({"error": f"Video API start request failed: {str(e)[:200]}"})

    request_id = start_data.get("request_id", "")
    if not request_id:
        return json.dumps({"error": "API returned no request_id",
                           "response": json.dumps(start_data)[:500]})

    logger.info(f"🎬 xAI generation started: {request_id} ({duration_sec}s, {resolution})")

    poll_url = f"https://api.x.ai/v1/videos/{request_id}"
    poll_start = time.time()
    video_url = ""

    while (time.time() - poll_start) < VIDEO_POLL_TIMEOUT:
        time.sleep(VIDEO_POLL_INTERVAL)
        try:
            poll_resp = _req.get(poll_url, headers=headers, timeout=15)
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
        except _req.exceptions.RequestException as e:
            logger.warning(f"Poll error (will retry): {e}")
            continue

        status = poll_data.get("status", "")
        if status == "done":
            video_info = poll_data.get("video", {})
            video_url = video_info.get("url", "")
            break
        elif status == "expired":
            return json.dumps({"error": "Video generation request expired",
                               "request_id": request_id, "shot_id": shot_id})
        elapsed = int(time.time() - poll_start)
        logger.debug(f"  Polling {request_id}... {elapsed}s elapsed")

    if not video_url:
        return json.dumps({"error": "Video generation timed out",
                           "request_id": request_id, "shot_id": shot_id,
                           "timeout_sec": VIDEO_POLL_TIMEOUT})

    try:
        clip_resp = _req.get(video_url, timeout=120)
        clip_resp.raise_for_status()
        clip_bytes = clip_resp.content
    except _req.exceptions.RequestException as e:
        return json.dumps({"error": f"Failed to download clip: {str(e)[:200]}",
                           "video_url_expired": True})

    with open(clip_path, "wb") as f:
        f.write(clip_bytes)

    if project_id and shot_id and shot_data and sl_file and sl:
        shot_data["status"] = "generated"
        shot_data["attempts"] = shot_data.get("attempts", 0) + 1
        shot_data["clip_file"] = _disp_path(clip_path)
        with open(sl_file, "w") as f:
            json.dump(sl, f, indent=2)

    size_kb = len(clip_bytes) / 1024
    gen_time = int(time.time() - poll_start)

    logger.info(f"🎥 xAI clip: {clip_filename} ({size_kb:.0f} KB, {duration_sec}s, "
                f"${cost}, took {gen_time}s)")

    return json.dumps({
        "success": True,
        "clip_id": request_id,
        "shot_id": shot_id,
        "file_path": _disp_path(clip_path),
        "size_kb": round(size_kb, 1),
        "duration_sec": duration_sec,
        "resolution": resolution,
        "cost_usd": cost,
        "generation_time_sec": gen_time,
        "model": model or DEFAULT_VIDEO_MODEL,
        "provider": "xai",
        "has_audio": False,
        "prompt_used": prompt[:200],
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 5: generate_all_clips
# ════════════════════════════════════════════════════════════════════

def generate_all_clips(project_id: str = "", episode: int = 1,
                       max_parallel: int = 1, skip_completed: bool = True,
                       provider: str = "", model: str = "",
                       **kwargs) -> str:
    """Batch-generate all clips for an episode from the shot list.

    Iterates through the shot list and generates each pending clip.
    Respects rate limits (1 RPS for grok-imagine-video).

    Scene Continuity:
      When a shot has "continues_previous": true, the last frame of the
      previous clip is extracted and passed as image_url to the xAI API.
      This creates smooth visual transitions between sequential scenes.
      Hard scene changes (new location, time jump) skip the reference frame.

    Parameters:
        project_id: Project ID
        episode: Episode number
        max_parallel: Max concurrent generations (default 1 for rate limits)
        skip_completed: Skip shots already marked as generated

    Returns:
        JSON with generation progress and cost summary.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    pdir = _project_dir(project_id)
    sl_file = pdir / f"shot_list_ep{episode:02d}.json"
    if not sl_file.exists():
        return json.dumps({"error": f"No shot list for episode {episode}. Use create_shot_list() first."})

    with open(sl_file) as f:
        shot_list = json.load(f)

    shots = shot_list.get("shots", [])
    results = []
    total_cost = 0.0
    generated = 0
    skipped = 0
    failed = 0
    prev_clip_path = None  # Track last successfully generated clip for continuity

    for i, shot in enumerate(shots):
        if skip_completed and shot.get("status") == "generated":
            skipped += 1
            # Even for skipped (already generated) clips, track the file
            # so the NEXT clip can use it as a reference frame
            existing_clip = pdir / "clips" / f"{shot.get('shot_id', '')}.mp4"
            if existing_clip.exists():
                prev_clip_path = existing_clip
            continue

        # ── Scene Continuity Logic ──
        # If this shot continues from the previous scene AND we have a
        # previous clip, extract its last frame for image-to-video generation.
        ref_image_url = ""
        continues = shot.get("continues_previous", False)
        if continues and prev_clip_path and prev_clip_path.exists():
            logger.info(f"🔗 Shot {shot.get('shot_id')}: continues from previous — extracting reference frame")
            ref_image_url = _extract_last_frame(prev_clip_path)
            if ref_image_url:
                logger.info(f"📸 Reference frame ready ({len(ref_image_url)//1024}KB base64)")
            else:
                logger.warning(f"⚠️ Frame extraction failed, generating without reference")
        elif not continues and i > 0:
            logger.info(f"🎬 Shot {shot.get('shot_id')}: hard scene change — no reference frame")

        result_str = generate_video_clip(
            project_id=project_id,
            shot_id=shot.get("shot_id", ""),
            duration_sec=shot.get("duration_sec", 5),
            image_url=ref_image_url,
            provider=provider,
            model=model,
        )
        result = json.loads(result_str)

        if result.get("success"):
            generated += 1
            total_cost += result.get("cost_usd", 0)
            # Track this clip for potential continuity with next shot
            clip_file = result.get("file_path", "")
            if clip_file:
                prev_clip_path = Path(__file__).parent.parent / clip_file
            else:
                prev_clip_path = None
        elif result.get("dry_run"):
            generated += 1  # Count dry runs as "processed"
            total_cost += result.get("estimated_cost_usd", 0)
            prev_clip_path = None
        else:
            failed += 1
            prev_clip_path = None  # Can't chain from a failed clip

        results.append({
            "shot_id": shot.get("shot_id"),
            "success": result.get("success", False),
            "dry_run": result.get("dry_run", False),
            "used_reference_frame": bool(ref_image_url),
            "continues_previous": continues,
            "error": result.get("error"),
        })

        # Rate limit: 1 RPS
        time.sleep(1.0)

    project["pipeline_state"]["video_generation"] = (
        "complete" if failed == 0 else "partial"
    )
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    continuity_used = sum(1 for r in results if r.get("used_reference_frame"))
    return json.dumps({
        "success": failed == 0,
        "project_id": project_id,
        "episode": episode,
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "total_cost_usd": round(total_cost, 2),
        "continuity_frames_used": continuity_used,
        "results": results,
        "next_step": "Use generate_narration() for voiceover, then assemble_edit() to combine",
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 6: generate_narration
# ════════════════════════════════════════════════════════════════════

def generate_narration(project_id: str = "", episode: int = 1,
                       text: str = "", scene: int = 0,
                       voice: str = "", rate: float = 1.0,
                       engine: str = "", **kwargs) -> str:
    """Generate narration audio from text using TTS.

    Supports Google Cloud TTS (high quality) or local piper TTS.
    Outputs WAV files synced to screenplay scene timings.

    Parameters:
        project_id: Project ID
        episode: Episode number
        text: Narration text to synthesize. If empty, pulls from screenplay.
        scene: Scene number to generate narration for (0 = all scenes)
        voice: TTS voice ID (default: en-US-Neural2-D for professional male)
        rate: Speech rate multiplier (0.5-2.0, default 1.0)
        engine: TTS engine — "google" or "piper" (default: google)

    Returns:
        JSON with audio file paths and durations.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    pdir = _project_dir(project_id)
    rate = max(0.5, min(2.0, float(rate)))
    # Pick the best engine if the caller didn't force one: ElevenLabs (best
    # quality, just needs a key) → Google Cloud TTS → local piper.
    if not engine:
        if _byok_elevenlabs_key():
            engine = "elevenlabs"
        elif _byok_google_key():
            engine = "google"
        else:
            engine = "piper"
    voice = voice or (DEFAULT_ELEVENLABS_VOICE if engine == "elevenlabs"
                      else DEFAULT_TTS_VOICE)

    # If no text provided, pull from screenplay
    narration_segments = []
    if not text:
        sp_file = pdir / f"screenplay_ep{episode:02d}.json"
        if not sp_file.exists():
            return json.dumps({"error": f"No screenplay for episode {episode}"})

        with open(sp_file) as f:
            screenplay = json.load(f)

        for s in screenplay.get("scenes", []):
            if scene > 0 and s.get("scene_number") != scene:
                continue
            narr = s.get("narration", "")
            if narr:
                narration_segments.append({
                    "scene": s["scene_number"],
                    "text": narr,
                    "duration_target": s.get("duration_sec", 30),
                })
    else:
        narration_segments = [{"scene": scene or 1, "text": text, "duration_target": 0}]

    if not narration_segments:
        return json.dumps({"error": "No narration text found in screenplay scenes"})

    audio_files = []
    config = _get_config()

    for seg in narration_segments:
        output_file = pdir / "audio" / f"narration_ep{episode:02d}_s{seg['scene']:02d}.wav"

        # Try the selected engine, falling back down the chain on failure:
        # elevenlabs → google → piper.
        success = False
        if engine == "elevenlabs":
            success = _generate_elevenlabs_tts(seg["text"], str(output_file), voice, rate)
            if not success:
                engine = "google" if _byok_google_key() else "piper"

        if not success and engine == "google":
            g_voice = voice if "-" in voice else DEFAULT_TTS_VOICE
            success = _generate_google_tts(seg["text"], str(output_file),
                                           g_voice, rate, config)
            if not success:
                engine = "piper"

        if not success and engine == "piper":
            success = _generate_piper_tts(seg["text"], str(output_file), rate)

        if success and output_file.exists():
            # Get duration via ffprobe
            duration = _get_audio_duration(str(output_file))
            audio_files.append({
                "scene": seg["scene"],
                "file": _disp_path(output_file),
                "duration_sec": round(duration, 2),
                "engine": engine,
                "voice": voice,
            })
        else:
            audio_files.append({
                "scene": seg["scene"],
                "error": f"TTS generation failed for scene {seg['scene']}",
                "engine": engine,
            })

    successes = [a for a in audio_files if "file" in a]
    project["pipeline_state"]["audio_production"] = (
        "complete" if len(successes) == len(narration_segments) else "partial"
    )
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    return json.dumps({
        "success": len(successes) == len(narration_segments),
        "project_id": project_id,
        "episode": episode,
        "narration_files": audio_files,
        "segments_processed": len(narration_segments),
        "segments_succeeded": len(successes),
        "total_narration_sec": round(sum(a.get("duration_sec", 0) for a in successes), 2),
        "next_step": "Use generate_music() for background music, then assemble_edit()",
    })


def _generate_google_tts(text: str, output_path: str, voice: str,
                         rate: float, config: Dict) -> bool:
    """Generate TTS audio via Google Cloud TTS API."""
    import requests as _req

    api_key = _byok_google_key()
    if not api_key:
        return False

    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}"
    payload = {
        "input": {"text": text},
        "voice": {
            "languageCode": voice.split("-")[0] + "-" + voice.split("-")[1] if "-" in voice else "en-US",
            "name": voice,
        },
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": DEFAULT_AUDIO_SAMPLE_RATE,
            "speakingRate": rate,
        },
    }

    try:
        resp = _req.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            import base64
            audio_content = base64.b64decode(resp.json()["audioContent"])
            with open(output_path, "wb") as f:
                f.write(audio_content)
            return True
        else:
            logger.warning(f"Google TTS failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Google TTS error: {e}")
        return False


def _generate_elevenlabs_tts(text: str, output_path: str, voice: str,
                             rate: float) -> bool:
    """Generate narration via ElevenLabs TTS.

    ElevenLabs returns MP3; we transcode to WAV with ffmpeg so it slots
    into the rest of the pipeline (which concatenates/mixes WAV).
    `voice` may be an ElevenLabs voice_id, or a Google-style name (which
    we ignore and fall back to the default ElevenLabs voice).
    """
    import requests as _req

    api_key = _byok_elevenlabs_key()
    if not api_key:
        return False

    # Google voice names contain "-"; if so, use the default EL voice.
    voice_id = voice if voice and "-" not in voice else DEFAULT_ELEVENLABS_VOICE
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": DEFAULT_ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    try:
        resp = _req.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"ElevenLabs TTS failed: {resp.status_code} {resp.text[:200]}")
            return False
        mp3_path = output_path + ".mp3"
        with open(mp3_path, "wb") as f:
            f.write(resp.content)
        # Transcode MP3 → WAV at the pipeline's sample rate; apply rate via
        # atempo when not 1.0 (atempo valid range is 0.5–2.0).
        af = []
        if rate and abs(rate - 1.0) > 0.01:
            af = ["-filter:a", f"atempo={max(0.5, min(2.0, rate))}"]
        cmd = ["ffmpeg", "-y", "-i", mp3_path, *af,
               "-ar", str(DEFAULT_AUDIO_SAMPLE_RATE), "-ac", "1", output_path]
        subprocess.run(cmd, capture_output=True, timeout=60)
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        return os.path.exists(output_path)
    except Exception as e:
        logger.warning(f"ElevenLabs TTS error: {e}")
        return False


def _generate_piper_tts(text: str, output_path: str, rate: float) -> bool:
    """Generate TTS audio via local piper TTS."""
    try:
        # Check piper availability
        result = subprocess.run(
            ["piper", "--help"], capture_output=True, timeout=5
        )
        if result.returncode != 0:
            logger.warning("Piper TTS not installed")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("Piper TTS not available")
        return False

    try:
        proc = subprocess.run(
            ["piper", "--model", "en_US-lessac-medium", "--output_file", output_path],
            input=text, capture_output=True, text=True, timeout=60
        )
        return proc.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        logger.warning(f"Piper TTS error: {e}")
        return False


def _get_audio_duration(filepath: str) -> float:
    """Get audio duration via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════
# TOOL 7: generate_music
# ════════════════════════════════════════════════════════════════════

def generate_music(project_id: str = "", episode: int = 1,
                   mood: str = "", duration_sec: int = 60,
                   genre: str = "cinematic", scene: int = 0,
                   **kwargs) -> str:
    """Generate background music for an episode or scene.

    Uses AI music generation or selects from royalty-free library.
    Falls back to generating a simple ambient track via FFmpeg if
    no music API is configured.

    Parameters:
        project_id: Project ID
        episode: Episode number
        mood: Musical mood (e.g. "tense, building, dark electronic")
        duration_sec: Target duration in seconds
        genre: Music genre (cinematic, electronic, ambient, orchestral,
               lo-fi, hip-hop, rock, jazz)
        scene: Scene number (0 = full episode background track)

    Returns:
        JSON with music file path and metadata.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    pdir = _project_dir(project_id)
    config = _get_config()

    # Pull mood from screenplay if not provided
    if not mood and scene > 0:
        sp_file = pdir / f"screenplay_ep{episode:02d}.json"
        if sp_file.exists():
            with open(sp_file) as f:
                sp = json.load(f)
            for s in sp.get("scenes", []):
                if s.get("scene_number") == scene:
                    mood = s.get("music_cue", s.get("mood", ""))
                    break

    if not mood:
        mood = "ambient, atmospheric, professional"

    output_file = pdir / "audio" / f"music_ep{episode:02d}_s{scene:02d}.wav"

    # Check for music API (Suno, Udio, etc.)
    music_api_key = config.get("music_api", {}).get("api_key", "")

    if music_api_key:
        # Future: call Suno/Udio API
        pass

    # FFmpeg-generated ambient tone as fallback
    if not output_file.exists():
        generated = _generate_ambient_track(str(output_file), duration_sec, mood)
        if not generated:
            return json.dumps({
                "error": "No music API configured and FFmpeg ambient generation failed",
                "config_needed": "Add music_api section to ai_config.json for Suno/Udio",
            })

    duration = _get_audio_duration(str(output_file))

    logger.info(f"🎵 Music generated: {output_file.name} ({duration:.1f}s, {mood})")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "scene": scene,
        "file_path": _disp_path(output_file),
        "duration_sec": round(duration, 2),
        "mood": mood,
        "genre": genre,
        "next_step": "Use assemble_edit() to combine clips, narration, and music",
    })


def _generate_ambient_track(output_path: str, duration: int, mood: str) -> bool:
    """Generate a simple ambient pad via FFmpeg synthesis."""
    if not _ffmpeg_available():
        return False

    # Map mood keywords to frequency/filter combos
    base_freq = 110  # A2
    if any(w in mood.lower() for w in ["tense", "dark", "dramatic"]):
        base_freq = 80
    elif any(w in mood.lower() for w in ["bright", "uplifting", "happy"]):
        base_freq = 220
    elif any(w in mood.lower() for w in ["calm", "peaceful", "ambient"]):
        base_freq = 130

    # Generate layered sine waves with tremolo for ambient pad
    try:
        filter_complex = (
            f"sine=f={base_freq}:d={duration}[s1];"
            f"sine=f={base_freq*1.5}:d={duration}[s2];"
            f"sine=f={base_freq*2}:d={duration}[s3];"
            f"[s1][s2][s3]amix=inputs=3:duration=longest,"
            f"tremolo=f=0.1:d=0.4,"
            f"afade=t=in:d=3,afade=t=out:st={max(0,duration-3)}:d=3,"
            f"volume=0.3"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", filter_complex,
             "-ar", str(DEFAULT_AUDIO_SAMPLE_RATE), "-ac", "2",
             output_path],
            capture_output=True, timeout=30
        )
        return os.path.exists(output_path)
    except Exception as e:
        logger.warning(f"Ambient track generation failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
# TOOL 8: assemble_edit
# ════════════════════════════════════════════════════════════════════

def assemble_edit(project_id: str = "", episode: int = 1,
                  include_narration: bool = True,
                  include_music: bool = True,
                  narration_volume: float = 1.0,
                  music_volume: float = 0.3,
                  **kwargs) -> str:
    """Assemble all generated clips, narration, and music into a final video.

    Uses FFmpeg to:
    1. Concatenate video clips in shot list order
    2. Mix narration audio at specified volume
    3. Mix background music (ducked under narration)
    4. Apply transitions between clips
    5. Render final video

    Parameters:
        project_id: Project ID
        episode: Episode number
        include_narration: Include narration audio track
        include_music: Include background music track
        narration_volume: Narration volume (0.0-1.5, default 1.0)
        music_volume: Music volume (0.0-1.0, default 0.3)

    Returns:
        JSON with rendered video path, duration, and file size.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    if not _ffmpeg_available():
        return json.dumps({"error": "FFmpeg is not installed. Run: sudo apt install ffmpeg"})

    pdir = _project_dir(project_id)

    # Load shot list for ordering
    sl_file = pdir / f"shot_list_ep{episode:02d}.json"
    if not sl_file.exists():
        return json.dumps({"error": f"No shot list for episode {episode}"})

    with open(sl_file) as f:
        shot_list = json.load(f)

    # Collect video clips in order. clip_file is stored as a _disp_path
    # (relative to the data dir), so resolve against several candidate roots.
    try:
        from repryntt.paths import get_data_dir as _gdd
        _data_root = _gdd()
    except Exception:
        _data_root = Path(__file__).parent.parent

    def _resolve_clip(rel: str) -> Optional[Path]:
        p = Path(rel)
        if p.is_absolute() and p.exists():
            return p
        for root in (_data_root, pdir, Path(__file__).parent.parent):
            cand = root / rel
            if cand.exists():
                return cand
        # Last resort: match by filename inside the project's clips/ dir
        hit = list((pdir / "clips").glob(Path(rel).name))
        return hit[0] if hit else None

    clips = []
    for shot in shot_list.get("shots", []):
        clip_file = shot.get("clip_file", "")
        if clip_file:
            resolved = _resolve_clip(clip_file)
            if resolved:
                clips.append(str(resolved))

    # Fallback: if the shot_list has no usable clip_file entries, pick up any
    # clips actually present on disk in sorted order.
    if not clips:
        disk_clips = sorted((pdir / "clips").glob("*.mp4"))
        clips = [str(c) for c in disk_clips]

    if not clips:
        return json.dumps({
            "error": "No generated clips found. Run generate_all_clips() first.",
            "shots_checked": len(shot_list.get("shots", [])),
        })

    # Create concat file for FFmpeg
    concat_file = pdir / "edits" / f"concat_ep{episode:02d}.txt"
    with open(concat_file, "w") as f:
        for clip in clips:
            f.write(f"file '{clip}'\n")

    output_file = pdir / "renders" / f"{project['title'].replace(' ', '_')}_ep{episode:02d}.mp4"
    resolution = project.get("quality_settings", {}).get("resolution", DEFAULT_RESOLUTION)
    fps = project.get("quality_settings", {}).get("fps", DEFAULT_FPS)

    # Build FFmpeg command
    cmd = ["ffmpeg", "-y"]

    # Input 1: concatenated video
    cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_file)]

    # Input 2: narration audio (if exists)
    narration_files = sorted(pdir.glob(f"audio/narration_ep{episode:02d}_*.wav"))
    has_narration = include_narration and len(narration_files) > 0

    # Input 3: music (if exists)
    music_files = sorted(pdir.glob(f"audio/music_ep{episode:02d}_*.wav"))
    has_music = include_music and len(music_files) > 0

    if has_narration:
        # Concatenate all narration files first
        narr_concat = pdir / "edits" / f"narration_concat_ep{episode:02d}.wav"
        if len(narration_files) > 1:
            narr_list = pdir / "edits" / f"narr_list_ep{episode:02d}.txt"
            with open(narr_list, "w") as f:
                for nf in narration_files:
                    f.write(f"file '{nf}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(narr_list), "-c", "copy", str(narr_concat)],
                capture_output=True, timeout=60
            )
        else:
            shutil.copy2(narration_files[0], narr_concat)
        cmd += ["-i", str(narr_concat)]

    if has_music:
        cmd += ["-i", str(music_files[0])]

    # Build filter complex for audio mixing
    if has_narration or has_music:
        filters = []
        audio_inputs = []
        input_idx = 1  # 0 is video

        if has_narration:
            filters.append(f"[{input_idx}:a]volume={narration_volume}[narr]")
            audio_inputs.append("[narr]")
            input_idx += 1

        if has_music:
            vol = music_volume
            if has_narration:
                # Duck music under narration
                vol = min(vol, 0.2)
            filters.append(f"[{input_idx}:a]volume={vol}[music]")
            audio_inputs.append("[music]")

        if len(audio_inputs) > 1:
            mix_inputs = "".join(audio_inputs)
            filters.append(f"{mix_inputs}amix=inputs={len(audio_inputs)}:duration=longest[aout]")
            audio_out = "[aout]"
        elif audio_inputs:
            audio_out = audio_inputs[0]
        else:
            audio_out = None

        if filters:
            cmd += ["-filter_complex", ";".join(filters)]

        # Output mapping
        cmd += ["-map", "0:v"]
        if audio_out:
            cmd += ["-map", audio_out]
    else:
        cmd += ["-map", "0:v"]

    # Output settings
    w, h = resolution.split("x")
    cmd += [
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",  # High quality
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    # End the output with the video track so trailing music/narration that
    # overruns the footage doesn't leave a frozen/black tail.
    if has_narration or has_music:
        cmd += ["-shortest"]
    cmd += [str(output_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return json.dumps({
                "error": f"FFmpeg render failed: {result.stderr[:500]}",
                "command": " ".join(cmd[:10]) + "...",
            })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Render timed out (10 min limit)"})

    if not output_file.exists():
        return json.dumps({"error": "Render produced no output file"})

    # Get output info
    size_mb = output_file.stat().st_size / (1024 * 1024)
    duration = _get_audio_duration(str(output_file))

    project["pipeline_state"]["editing"] = "complete"
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    logger.info(f"🎞️ Rendered: {output_file.name} ({size_mb:.1f} MB, {duration:.1f}s)")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "output_file": _disp_path(output_file),
        "size_mb": round(size_mb, 2),
        "duration_sec": round(duration, 2),
        "resolution": resolution,
        "fps": fps,
        "clips_used": len(clips),
        "has_narration": has_narration,
        "has_music": has_music,
        "codec": "H.264 / AAC",
        "next_step": "Use qa_review_video() to check quality, then render_final()",
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 9: qa_review_video
# ════════════════════════════════════════════════════════════════════

def qa_review_video(project_id: str = "", episode: int = 1,
                    review_notes: str = "", scores: str = "",
                    **kwargs) -> str:
    """QA review a rendered video — score quality and flag issues.

    The QA reviewer evaluates the rendered video and provides scores
    across multiple quality dimensions. Issues get flagged for re-work.

    Parameters:
        project_id: Project ID
        episode: Episode number
        review_notes: QA reviewer's notes on the video quality
        scores: JSON string with quality scores. Format:
            {
              "visual_coherence": 8.5,
              "audio_sync": 9.0,
              "pacing": 7.5,
              "story_flow": 8.0,
              "color_consistency": 8.5,
              "overall": 8.3,
              "issues": [
                {"timestamp_sec": 45, "type": "visual_glitch", "severity": "minor",
                 "description": "Brief artifact at scene transition"},
                {"timestamp_sec": 120, "type": "audio_desync", "severity": "major",
                 "description": "Narration 0.5s ahead of visual cue"}
              ],
              "reroll_shots": ["ep01_s03_002", "ep01_s05_001"],
              "approved": false
            }

    Returns:
        JSON with QA status and recommended actions.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    if not scores:
        return json.dumps({"error": "scores JSON is required"})

    try:
        if isinstance(scores, str):
            score_data = json.loads(scores)
        else:
            score_data = scores
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid scores JSON: {str(e)[:200]}"})

    pdir = _project_dir(project_id)

    # Save QA report
    qa_report = {
        "episode": episode,
        "review_date": datetime.now(timezone.utc).isoformat(),
        "scores": score_data,
        "review_notes": review_notes,
        "approved": score_data.get("approved", False),
        "overall_score": score_data.get("overall", 0),
    }

    qa_file = pdir / f"qa_review_ep{episode:02d}.json"
    with open(qa_file, "w") as f:
        json.dump(qa_report, f, indent=2)

    # Determine actions
    issues = score_data.get("issues", [])
    rerolls = score_data.get("reroll_shots", [])
    approved = score_data.get("approved", False)
    overall = score_data.get("overall", 0)

    major_issues = [i for i in issues if i.get("severity") == "major"]
    actions = []

    if rerolls:
        actions.append(f"Re-generate {len(rerolls)} shots: {', '.join(rerolls)}")
    if major_issues:
        actions.append(f"Fix {len(major_issues)} major issues before final render")
    if not approved and overall >= QA_MIN_COHERENCE_SCORE:
        actions.append("Score meets threshold — consider approving")
    if approved:
        actions.append("APPROVED — ready for render_final()")

    project["pipeline_state"]["qa_review"] = "approved" if approved else "needs_rework"
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    logger.info(f"🔍 QA Review: {project['title']} Ep{episode} — "
                f"Score: {overall}/10, {'APPROVED' if approved else 'NEEDS REWORK'}")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "approved": approved,
        "overall_score": overall,
        "scores": {
            "visual_coherence": score_data.get("visual_coherence", 0),
            "audio_sync": score_data.get("audio_sync", 0),
            "pacing": score_data.get("pacing", 0),
            "story_flow": score_data.get("story_flow", 0),
            "color_consistency": score_data.get("color_consistency", 0),
        },
        "issues_count": len(issues),
        "major_issues": len(major_issues),
        "reroll_shots": rerolls,
        "recommended_actions": actions,
        "qa_report_file": _disp_path(qa_file),
        "next_step": ("Use render_final() for final export"
                      if approved else
                      "Fix issues and re-run assemble_edit(), then qa_review_video() again"),
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 10: render_final
# ════════════════════════════════════════════════════════════════════

def render_final(project_id: str = "", episode: int = 1,
                 resolution: str = "", codec: str = "h264",
                 add_intro: bool = False, add_outro: bool = False,
                 add_subtitles: bool = False,
                 **kwargs) -> str:
    """Final render with professional encoding settings.

    Takes the assembled edit and renders with broadcast-quality settings.
    Optionally adds intro/outro cards and burned-in subtitles.

    Parameters:
        project_id: Project ID
        episode: Episode number
        resolution: Override resolution (e.g. "1920x1080", "3840x2160")
        codec: Video codec — h264 (universal), h265 (smaller files),
               prores (editing)
        add_intro: Add title card intro (3 seconds)
        add_outro: Add end card outro (5 seconds)
        add_subtitles: Burn subtitles from narration text

    Returns:
        JSON with final file path, technical specs, and delivery info.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    if not _ffmpeg_available():
        return json.dumps({"error": "FFmpeg is not installed"})

    pdir = _project_dir(project_id)
    resolution = resolution or project.get("quality_settings", {}).get("resolution", DEFAULT_RESOLUTION)

    # Find assembled edit
    edit_pattern = f"{project['title'].replace(' ', '_')}_ep{episode:02d}.mp4"
    edit_file = pdir / "renders" / edit_pattern
    if not edit_file.exists():
        # Look for any render
        renders = list((pdir / "renders").glob("*.mp4"))
        if renders:
            edit_file = renders[0]
        else:
            return json.dumps({"error": "No assembled edit found. Run assemble_edit() first."})

    # Build final render command
    final_name = f"{project['title'].replace(' ', '_')}_ep{episode:02d}_FINAL.mp4"
    final_path = pdir / "renders" / final_name

    w, h = resolution.split("x")
    cmd = ["ffmpeg", "-y", "-i", str(edit_file)]

    # Subtitle generation
    if add_subtitles:
        srt_file = _generate_srt(project_id, episode, pdir)
        if srt_file:
            cmd += ["-vf", f"scale={w}:{h},subtitles={srt_file}:force_style='FontSize=22,PrimaryColour=&Hffffff&,OutlineColour=&H000000&,Outline=2'"]
        else:
            cmd += ["-vf", f"scale={w}:{h}"]
    else:
        cmd += ["-vf", f"scale={w}:{h}"]

    # Codec settings
    if codec == "h265":
        cmd += ["-c:v", "libx265", "-preset", "slow", "-crf", "20", "-tag:v", "hvc1"]
    elif codec == "prores":
        cmd += ["-c:v", "prores_ks", "-profile:v", "3"]  # ProRes HQ
    else:
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16",
                "-profile:v", "high", "-level", "4.1"]

    cmd += [
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        "-metadata", f"title={project['title']} - Episode {episode}",
        "-metadata", f"comment=Generated by SAIGE Video Production Pipeline",
        str(final_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            return json.dumps({"error": f"Final render failed: {result.stderr[:500]}"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Final render timed out (15 min limit)"})

    if not final_path.exists():
        return json.dumps({"error": "Final render produced no output"})

    size_mb = final_path.stat().st_size / (1024 * 1024)
    duration = _get_audio_duration(str(final_path))

    project["pipeline_state"]["final_render"] = "complete"
    project["status"] = "complete"
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_project(project_id, project)

    logger.info(f"✅ FINAL RENDER: {final_name} ({size_mb:.1f} MB, {duration:.1f}s, {resolution})")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "final_file": _disp_path(final_path),
        "absolute_path": str(final_path),
        "size_mb": round(size_mb, 2),
        "duration_sec": round(duration, 2),
        "resolution": resolution,
        "codec": codec,
        "has_subtitles": add_subtitles,
        "specs": {
            "video": f"{codec.upper()}, {resolution}, CRF 16-20",
            "audio": "AAC 256kbps",
            "container": "MP4 (faststart)",
        },
        "delivery_ready": True,
    })


def _generate_srt(project_id: str, episode: int, pdir: Path) -> Optional[str]:
    """Generate SRT subtitle file from screenplay narration."""
    sp_file = pdir / f"screenplay_ep{episode:02d}.json"
    if not sp_file.exists():
        return None

    with open(sp_file) as f:
        screenplay = json.load(f)

    srt_path = pdir / "edits" / f"subtitles_ep{episode:02d}.srt"
    running_time = 0.0

    with open(srt_path, "w") as f:
        idx = 1
        for scene in screenplay.get("scenes", []):
            narration = scene.get("narration", "")
            if not narration:
                running_time += scene.get("duration_sec", 0)
                continue

            duration = scene.get("duration_sec", 10)
            # Break narration into subtitle chunks (~10 words each)
            words = narration.split()
            chunk_size = 10
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i+chunk_size])
                chunk_dur = (len(chunk.split()) / len(words)) * duration if words else duration

                start = running_time
                end = running_time + chunk_dur

                f.write(f"{idx}\n")
                f.write(f"{_srt_time(start)} --> {_srt_time(end)}\n")
                f.write(f"{chunk}\n\n")

                running_time = end
                idx += 1

            # Account for remaining scene time
            narr_dur = duration
            if running_time < running_time + duration - narr_dur:
                running_time += duration - narr_dur

    return str(srt_path)


def _srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ════════════════════════════════════════════════════════════════════
# TOOL 11: video_project_status
# ════════════════════════════════════════════════════════════════════

def video_project_status(project_id: str = "", **kwargs) -> str:
    """Get the current status of a video production project.

    Shows pipeline progress, costs so far, and next actions needed.

    Parameters:
        project_id: Project ID (if empty, lists all projects)

    Returns:
        JSON with project status, pipeline state, and pending actions.
    """
    if not project_id:
        # List all projects
        if not PROJECTS_DIR.exists():
            return json.dumps({"projects": [], "message": "No video projects yet"})

        projects = []
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                proj = _load_project(d.name)
                if proj:
                    projects.append({
                        "project_id": proj["project_id"],
                        "title": proj["title"],
                        "status": proj["status"],
                        "episodes": proj["episodes"],
                        "created": proj["created_at"],
                    })
        return json.dumps({"projects": projects, "count": len(projects)})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    pipeline = project.get("pipeline_state", {})
    pending = [k for k, v in pipeline.items() if v == "pending"]
    complete = [k for k, v in pipeline.items() if v in ("complete", "approved")]

    # Calculate actual costs
    pdir = _project_dir(project_id)
    clip_count = len(list((pdir / "clips").glob("*.mp4")))
    render_count = len(list((pdir / "renders").glob("*.mp4")))
    audio_count = len(list((pdir / "audio").glob("*.wav")))

    next_actions = []
    if pipeline.get("screenplay") == "pending":
        next_actions.append("write_screenplay() — Create the screenplay")
    elif pipeline.get("shot_list") == "pending":
        next_actions.append("create_shot_list() — Break into individual shots")
    elif pipeline.get("video_generation") == "pending":
        next_actions.append("generate_all_clips() — Generate video clips")
    elif pipeline.get("audio_production") == "pending":
        next_actions.append("generate_narration() — Create voiceover")
    elif pipeline.get("editing") == "pending":
        next_actions.append("assemble_edit() — Combine clips + audio")
    elif pipeline.get("qa_review") == "pending":
        next_actions.append("qa_review_video() — Quality review")
    elif pipeline.get("qa_review") == "needs_rework":
        next_actions.append("Fix flagged issues, re-edit, then qa_review_video() again")
    elif pipeline.get("final_render") == "pending":
        next_actions.append("render_final() — Final high-quality render")

    return json.dumps({
        "project_id": project_id,
        "title": project["title"],
        "genre": project["genre"],
        "status": project["status"],
        "episodes": project["episodes"],
        "total_runtime_sec": project["total_seconds"],
        "pipeline": pipeline,
        "progress": f"{len(complete)}/{len(pipeline)} stages complete",
        "pending_stages": pending,
        "completed_stages": complete,
        "files": {
            "clips": clip_count,
            "renders": render_count,
            "audio_tracks": audio_count,
        },
        "budget": project.get("budget", {}),
        "next_actions": next_actions,
        "style_guide": project.get("quality_settings", {}).get("style_guide", ""),
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 12: generate_thumbnail
# ════════════════════════════════════════════════════════════════════

def generate_thumbnail(project_id: str = "", episode: int = 1,
                       prompt: str = "", style: str = "",
                       text_overlay: str = "", **kwargs) -> str:
    """Generate a professional thumbnail for a video episode.

    Uses image generation (Gemini) to create an eye-catching thumbnail
    optimized for YouTube/social media dimensions.

    Parameters:
        project_id: Project ID
        episode: Episode number
        prompt: Thumbnail image prompt (auto-generated from screenplay if empty)
        style: Visual style override
        text_overlay: Text to overlay on thumbnail (e.g. episode title)

    Returns:
        JSON with thumbnail file path and specs.
    """
    if not project_id:
        return json.dumps({"error": "project_id is required"})

    project = _load_project(project_id)
    if not project:
        return json.dumps({"error": f"Project '{project_id}' not found"})

    pdir = _project_dir(project_id)

    # Auto-generate prompt from screenplay if not provided
    if not prompt:
        sp_file = pdir / f"screenplay_ep{episode:02d}.json"
        if sp_file.exists():
            with open(sp_file) as f:
                sp = json.load(f)
            scenes = sp.get("scenes", [])
            if scenes:
                # Use the most visually striking scene
                prompt = (f"Professional YouTube thumbnail for '{project['title']}', "
                         f"based on: {scenes[0].get('visual_description', '')}. "
                         f"Style: {style or project.get('quality_settings', {}).get('style_guide', '')}, "
                         f"eye-catching, high contrast, 16:9 aspect ratio, no text")

    if not prompt:
        return json.dumps({"error": "prompt required — describe the thumbnail"})

    import requests as _req
    import base64 as _b64

    config = _get_config()
    img_bytes = None
    used_model = "unknown"

    # --- Try Gemini first ---
    api_key = _byok_google_key()
    if api_key:
        model = "gemini-2.5-flash-image"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "16:9"},
            },
        }
        try:
            resp = _req.post(url, json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
                        if "inlineData" in part:
                            img_bytes = _b64.b64decode(part["inlineData"]["data"])
                            used_model = model
                            break
            else:
                logger.warning(f"Gemini thumbnail HTTP {resp.status_code}, trying xAI")
        except Exception as e:
            logger.warning(f"Gemini thumbnail failed: {e}, trying xAI")

    # --- Fallback to xAI ---
    if img_bytes is None:
        xai_cfg = config.get("xai", {})
        xai_key = (__import__("os").environ.get("XAI_API_KEY") or xai_cfg.get("api_key", ""))
        if xai_key:
            xai_model = xai_cfg.get("image_model", "grok-imagine-image")
            try:
                resp = _req.post(
                    xai_cfg.get("image_endpoint", "https://api.x.ai/v1/images/generations"),
                    json={"model": xai_model, "prompt": prompt, "n": 1},
                    headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
                    timeout=90,
                )
                if resp.status_code == 200:
                    images = resp.json().get("data", [])
                    if images and images[0].get("url"):
                        img_resp = _req.get(images[0]["url"], timeout=60)
                        img_resp.raise_for_status()
                        img_bytes = img_resp.content
                        used_model = xai_model
            except Exception as e:
                logger.warning(f"xAI thumbnail also failed: {e}")

    if img_bytes is None:
        return json.dumps({"error": "Thumbnail generation failed on both Gemini and xAI"})

    # Save thumbnail
    thumb_file = pdir / "frames" / f"thumbnail_ep{episode:02d}.png"
    with open(thumb_file, "wb") as f:
        f.write(img_bytes)

    # Add text overlay via FFmpeg if requested
    if text_overlay and _ffmpeg_available():
        overlaid = pdir / "frames" / f"thumbnail_ep{episode:02d}_titled.png"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(thumb_file),
                "-vf", (f"drawtext=text='{text_overlay}':fontsize=48:"
                        f"fontcolor=white:borderw=3:bordercolor=black:"
                        f"x=(w-text_w)/2:y=h-th-40"),
                str(overlaid),
            ], capture_output=True, timeout=15)
            if overlaid.exists():
                thumb_file = overlaid
        except Exception:
            pass  # Non-critical, use un-overlaid version

    size_kb = thumb_file.stat().st_size / 1024

    logger.info(f"🖼️ Thumbnail generated: {thumb_file.name} ({size_kb:.0f} KB)")

    return json.dumps({
        "success": True,
        "project_id": project_id,
        "episode": episode,
        "file_path": _disp_path(thumb_file),
        "size_kb": round(size_kb, 1),
        "dimensions": "1280x720 (16:9)",
        "has_text_overlay": bool(text_overlay),
    })


# ════════════════════════════════════════════════════════════════════
# TOOL 13: auto_produce_video  (Full Autonomous Pipeline)
# ════════════════════════════════════════════════════════════════════

# LLM config for creative ideation — resolved from active provider in ai_config
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"  # legacy fallback
_GEMINI_MODEL = "gemini-2.0-flash"  # legacy fallback


def _resolve_creative_llm() -> tuple:
    """Resolve endpoint, model, and API key for creative ideation.

    Cloud/BYOK path (preferred): if REPRYNTT_CREATIVE_* env vars are set
    (injected by the cloud video worker from the customer's BYOK key),
    use those. Falls back to the local ai_config.json provider for the
    self-hosted daemon.
    """
    import os as _os
    env_key = _os.environ.get("REPRYNTT_CREATIVE_API_KEY", "")
    if env_key:
        endpoint = _os.environ.get("REPRYNTT_CREATIVE_ENDPOINT", _GEMINI_ENDPOINT)
        model = _os.environ.get("REPRYNTT_CREATIVE_MODEL", "claude-sonnet-4-5")
        if endpoint and not endpoint.endswith("/chat/completions") and not endpoint.endswith("/messages"):
            endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
        return endpoint, model, env_key

    config = _get_config()
    provider_name = (config.get("andrew_provider")
                     or config.get("artemis_provider")
                     or config.get("provider", "google_gemini"))
    settings = config.get(provider_name, {})
    api_key = settings.get("api_key", "")
    model = settings.get("model", _GEMINI_MODEL)
    endpoint = settings.get("endpoint", "")
    if endpoint and not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}/v1/chat/completions"
    elif not endpoint:
        endpoint = _GEMINI_ENDPOINT
    elif not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
    return endpoint, model, api_key


def _call_gemini_creative(system_prompt: str, user_prompt: str,
                          max_tokens: int = 4096, temperature: float = 0.8) -> str:
    """Call active LLM provider for creative text generation (screenplay/shot list).

    Supports two API shapes:
      • Anthropic native (/v1/messages) — when endpoint points at Anthropic
      • OpenAI-compatible (/chat/completions) — OpenAI, xAI, Google
    Retries once on 5xx (providers occasionally 503 under load).
    """
    import requests as _req
    import time as _time

    endpoint, model, api_key = _resolve_creative_llm()
    if not api_key:
        raise RuntimeError("No LLM API key configured for creative ideation")

    is_anthropic = "anthropic.com" in endpoint or model.lower().startswith("claude")

    # Adaptive-thinking flagships (Opus 4.7+, Fable 5+, Mythos 5+) reject
    # the `temperature` param on Anthropic — sending it returns 400.
    _m = (model or "").lower()
    _temp_deprecated = (
        "opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
        or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
        or "claude-5-" in _m
    )

    def _do_request():
        if is_anthropic:
            ep = "https://api.anthropic.com/v1/messages"
            _body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            if not _temp_deprecated:
                _body["temperature"] = temperature
            resp = _req.post(ep, headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }, json=_body, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text").strip()
        else:
            resp = _req.post(endpoint, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }, timeout=120)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

    try:
        return _do_request()
    except _req.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code >= 500:
            _time.sleep(3)
            return _do_request()
        raise


def auto_produce_video(prompt: str = "", duration: int = 30,
                       genre: str = "documentary", style: str = "",
                       resolution: str = "480p",
                       skip_assembly: bool = False,
                       provider: str = "", veo_model: str = "",
                       model: str = "",
                       progress_callback=None,
                       **kwargs) -> str:
    """Fully autonomous video production from a single text prompt.

    Give it one sentence and it creates an entire video — screenplay,
    shot list, generated clips, assembled edit, and final render.
    Uses Gemini for creative ideation and Veo/xAI for video generation.

    Parameters:
        prompt: Creative brief in plain English. Examples:
            - "A documentary about the future of AI and robotics"
            - "A 30-second commercial for a coffee brand"
            - "A cinematic tour of ancient Rome at sunset"
            - "An explainer about how black holes work"
        duration: Target video duration in seconds (5-300)
        genre: Content genre (documentary, commercial, music_video,
               short_film, tutorial, social_media, animation)
        style: Visual style override (auto-generated if empty)
        resolution: "480p" ($0.05/s xAI) or "720p" ($0.07/s xAI). Veo uses per-clip pricing.
        skip_assembly: If True, stop after clip generation (no FFmpeg)
        provider: "veo" or "xai" — empty uses config default
        progress_callback: Optional callable(stage, detail) for status updates

    Returns:
        JSON with project_id, all generated files, cost breakdown,
        and paths to preview every asset.
    """
    if not prompt:
        return json.dumps({"error": "prompt is required — describe what video you want"})

    duration = max(5, min(300, int(duration)))

    # ── Resolve video provider ──
    vprov = _get_video_provider()
    active_provider = provider if provider in ("veo", "xai", "falai") else vprov["provider"]
    max_clip = vprov["max_clip_sec"] if active_provider == vprov["provider"] else (
        MAX_CLIP_DURATION_VEO if active_provider == "veo" else MAX_CLIP_DURATION_XAI
    )
    cost_per_clip = vprov["cost_per_clip"] if active_provider == vprov["provider"] else (
        VEO_COST_PER_CLIP_FAST if active_provider == "veo" else 0
    )
    cost_per_sec = VIDEO_COST_PER_SEC_480P if resolution == "480p" else VIDEO_COST_PER_SEC_720P

    def _progress(stage, detail=""):
        logger.info(f"🎬 [{stage}] {detail}")
        if progress_callback:
            try:
                progress_callback(stage, detail)
            except Exception:
                pass

    _progress("init", f"Starting autonomous production: '{prompt[:80]}' ({duration}s, {resolution}, provider={active_provider})")

    # ── Step 1: Create project ──
    _progress("project", "Creating project workspace...")
    proj_result = json.loads(create_video_project(
        title=prompt[:60], genre=genre, episodes=1,
        episode_duration=duration, style=style
    ))
    if not proj_result.get("success"):
        return json.dumps({"error": f"Project creation failed: {proj_result.get('error', 'unknown')}"})

    project_id = proj_result["project_id"]
    style_guide = proj_result["style_guide"]

    # ── Step 2: AI-generate screenplay ──
    _progress("screenplay", "Gemini generating screenplay...")

    import math
    num_scenes = max(2, min(12, math.ceil(duration / max_clip)))
    per_scene = duration // num_scenes

    screenplay_system = """You are a professional screenwriter and video director for an AI video production studio.
Generate a structured screenplay as valid JSON. No markdown, no code fences — raw JSON only.
The screenplay must be compelling, visually rich, and optimized for AI video generation.
Each scene needs a vivid visual_description (what the camera sees), optional narration, and mood."""

    screenplay_user = f"""Create a screenplay for this video:

BRIEF: {prompt}
GENRE: {genre}
STYLE: {style_guide}
TOTAL DURATION: {duration} seconds
NUMBER OF SCENES: {num_scenes}
SECONDS PER SCENE: ~{per_scene}

Return ONLY valid JSON in this exact format:
{{
  "episode_title": "compelling title here",
  "scenes": [
    {{
      "scene_number": 1,
      "title": "short scene title",
      "duration_sec": {per_scene},
      "visual_description": "detailed description of what the camera sees — be specific about setting, lighting, subjects, movement, colors",
      "narration": "voiceover text for this scene (or empty string if none)",
      "mood": "one or two words describing the emotional tone",
      "music_cue": "brief music direction",
      "transition_in": "fade_from_black",
      "transition_out": "dissolve"
    }}
  ]
}}

Make scenes flow naturally with varied shot types. Visual descriptions should be 2-3 sentences, cinematically detailed.
Ensure durations total approximately {duration} seconds."""

    try:
        raw_screenplay = _call_gemini_creative(screenplay_system, screenplay_user)
        # Strip any markdown fences the model might add
        raw_screenplay = re.sub(r'^```(?:json)?\s*', '', raw_screenplay, flags=re.MULTILINE)
        raw_screenplay = re.sub(r'```\s*$', '', raw_screenplay, flags=re.MULTILINE)
        screenplay = json.loads(raw_screenplay.strip())
    except (json.JSONDecodeError, RuntimeError) as e:
        return json.dumps({"error": f"Screenplay generation failed: {str(e)[:200]}",
                           "project_id": project_id, "raw": raw_screenplay[:500] if 'raw_screenplay' in dir() else ""})

    # Validate and fix durations
    scenes = screenplay.get("scenes", [])
    if not scenes:
        return json.dumps({"error": "Gemini returned no scenes", "project_id": project_id})

    # Adjust durations to exactly match target
    total_so_far = sum(s.get("duration_sec", per_scene) for s in scenes)
    if total_so_far != duration:
        diff = duration - total_so_far
        scenes[-1]["duration_sec"] = max(3, scenes[-1].get("duration_sec", per_scene) + diff)

    sp_result = json.loads(write_screenplay(
        project_id=project_id, episode=1,
        screenplay_text=json.dumps(screenplay)
    ))
    if not sp_result.get("success"):
        return json.dumps({"error": f"Screenplay save failed: {sp_result.get('error', 'unknown')}",
                           "project_id": project_id})

    _progress("screenplay", f"Created {len(scenes)} scenes, {sp_result['total_duration_sec']}s total")

    # ── Step 3: AI-generate shot list ──
    _progress("shot_list", "Gemini generating shot list with optimized prompts...")

    shots_system = """You are a director of photography creating a shot list for AI video generation.
Each shot becomes a prompt for an AI video model (like Grok Imagine Video).
Write prompts that are vivid, specific, and optimized for AI generation quality.
Include camera type, movement, lighting, colors, and composition details.

IMPORTANT — Scene Continuity:
For each shot, set "continues_previous" to true or false.
- TRUE: This shot visually continues from where the previous one ended (same location, same subjects, camera just moves or time flows forward). The system will use the last frame of the previous clip as a starting reference image, so describe MOTION and PROGRESSION, not a completely new scene.
- FALSE: This is a hard scene change — new location, new subjects, or a major time jump. The system will generate this shot purely from text.
The FIRST shot is always false (nothing to continue from).

Return ONLY valid JSON — no markdown fences."""

    scenes_desc = "\n".join([
        f"Scene {s['scene_number']}: [{s.get('duration_sec', per_scene)}s] {s.get('visual_description', '')}"
        for s in scenes
    ])

    shots_user = f"""Create a shot list for these scenes:

{scenes_desc}

VISUAL STYLE: {style_guide}
MAX DURATION PER SHOT: {max_clip} seconds

Rules:
- One shot per scene (to match exact durations)
- Each prompt should be 2-3 sentences of rich visual description
- Include style keywords: cinematic, 4K, professional footage, etc.
- Each shot_id format: ep01_sNN_001 (where NN is the scene number, zero-padded)
- Set "continues_previous" based on whether this shot visually flows from the previous scene
  (true = same location/subjects continuing, false = new location or hard cut)

Return ONLY valid JSON:
{{
  "shots": [
    {{
      "shot_id": "ep01_s01_001",
      "scene": 1,
      "duration_sec": {scenes[0].get('duration_sec', per_scene)},
      "shot_type": "wide_establishing",
      "prompt": "detailed AI video generation prompt — be specific about subjects, lighting, camera angle, movement, colors, style",
      "aspect_ratio": "16:9",
      "continues_previous": false
    }}
  ]
}}"""

    try:
        raw_shots = _call_gemini_creative(shots_system, shots_user)
        raw_shots = re.sub(r'^```(?:json)?\s*', '', raw_shots, flags=re.MULTILINE)
        raw_shots = re.sub(r'```\s*$', '', raw_shots, flags=re.MULTILINE)
        shot_list = json.loads(raw_shots.strip())
    except (json.JSONDecodeError, RuntimeError) as e:
        return json.dumps({"error": f"Shot list generation failed: {str(e)[:200]}",
                           "project_id": project_id})

    sl_result = json.loads(create_shot_list(
        project_id=project_id, episode=1,
        shot_list_text=json.dumps(shot_list)
    ))
    if not sl_result.get("success"):
        return json.dumps({"error": f"Shot list save failed: {sl_result.get('error', 'unknown')}",
                           "project_id": project_id})

    _progress("shot_list", f"Created {sl_result['shot_count']} shots, est. ${sl_result['estimated_cost_usd']}")

    # ── Step 4: Generate all video clips ──
    _progress("generation", f"Generating {sl_result['shot_count']} video clips via {active_provider.upper()} at {resolution}...")

    clip_model = model or veo_model
    gen_result = json.loads(generate_all_clips(
        project_id=project_id, episode=1, resolution=resolution,
        provider=active_provider, model=clip_model
    ))

    clips_new = gen_result.get("generated", 0)
    clips_skipped = gen_result.get("skipped", 0)
    clips_fail = gen_result.get("failed", 0)
    clips_ok = clips_new + clips_skipped  # skipped = already completed
    total_cost = float(gen_result.get("total_cost_usd", 0))

    _progress("generation", f"Generated {clips_new} new + {clips_skipped} existing, {clips_fail} failed, ${total_cost:.2f}")
    continuity_count = gen_result.get("continuity_frames_used", 0)
    if continuity_count:
        _progress("generation", f"🔗 Scene continuity: {continuity_count} shots used reference frames from previous clips")

    # Count actual clip files on disk as the source of truth
    clip_dir = _project_dir(project_id) / "clips"
    actual_clips = list(clip_dir.glob("*.mp4")) if clip_dir.exists() else []
    if not actual_clips:
        return json.dumps({
            "error": "No clips generated successfully",
            "project_id": project_id,
            "generation_details": gen_result,
        })

    # ── Step 5: Narration + Music + Assemble into ONE final video ──
    assembly_result = None
    render_result = None

    if not skip_assembly and _ffmpeg_available() and len(actual_clips) >= 1:
        # 5a. Narration (best-effort — Google TTS, falls back to piper/none)
        have_narration = False
        try:
            _progress("narration", "Generating narration...")
            narr = json.loads(generate_narration(project_id=project_id, episode=1))
            have_narration = bool(narr.get("success")) and bool(narr.get("audio_files"))
            _progress("narration", "Narration ready" if have_narration else "Narration skipped (no TTS)")
        except Exception as e:
            _progress("narration", f"Narration skipped: {str(e)[:80]}")

        # 5b. Music (ffmpeg ambient — reliable, no key needed)
        have_music = False
        try:
            _progress("music", "Generating music score...")
            mus = json.loads(generate_music(project_id=project_id, episode=1, duration_sec=duration))
            have_music = bool(mus.get("success"))
            _progress("music", "Music ready" if have_music else "Music skipped")
        except Exception as e:
            _progress("music", f"Music skipped: {str(e)[:80]}")

        # 5c. Assemble clips + (narration) + (music) into one video
        _progress("assembly", "Assembling final video...")
        try:
            assembly_result = json.loads(assemble_edit(
                project_id=project_id, episode=1,
                include_narration=have_narration,
                include_music=have_music,
            ))
            _progress("assembly", f"Assembled: {assembly_result.get('output_file', 'done')}")
        except Exception as e:
            _progress("assembly", f"Assembly warning: {str(e)[:120]}")

        # 5d. Final render (clean encode + optional subtitles)
        try:
            _progress("render", "Final render...")
            render_result = json.loads(render_final(project_id=project_id, episode=1))
            _progress("render", f"Rendered: {render_result.get('output_file', 'done')}")
        except Exception as e:
            _progress("render", f"Render warning: {str(e)[:120]}")

    # ── Compile results ──
    pdir = _project_dir(project_id)
    clip_files = sorted((pdir / "clips").glob("*.mp4")) if (pdir / "clips").exists() else []
    render_files = sorted((pdir / "renders").glob("*.mp4")) if (pdir / "renders").exists() else []
    edit_files = sorted((pdir / "edits").glob("*.mp4")) if (pdir / "edits").exists() else []

    # The single unified video to surface for download: prefer final render,
    # then assembled edit, then (last resort) the first clip.
    final_video = None
    for candidate_list in (render_files, edit_files, clip_files):
        if candidate_list:
            final_video = candidate_list[-1]
            break

    result = {
        "success": True,
        "project_id": project_id,
        "final_video": _disp_path(final_video) if final_video else None,
        "prompt": prompt,
        "genre": genre,
        "style": style_guide,
        "video_provider": active_provider,
        "duration_target_sec": duration,
        "max_clip_duration_sec": max_clip,
        "scenes_created": len(scenes),
        "screenplay": {
            "title": screenplay.get("episode_title", prompt[:40]),
            "scenes": [{"number": s["scene_number"], "title": s.get("title", ""),
                        "duration": s["duration_sec"]} for s in scenes],
        },
        "shots_created": sl_result["shot_count"],
        "clips_generated": len(actual_clips),
        "clips_failed": clips_fail,
        "continuity_frames_used": gen_result.get("continuity_frames_used", 0),
        "total_cost_usd": round(total_cost, 2),
        "cost_breakdown": {
            "video_generation": round(total_cost, 2),
            "llm_ideation": 0.0,  # Gemini Flash is free
            "total": round(total_cost, 2),
        },
        "files": {
            "clips": [_disp_path(f) for f in clip_files],
            "edits": [_disp_path(f) for f in edit_files],
            "renders": [_disp_path(f) for f in render_files],
            "project_dir": _disp_path(pdir),
        },
        "assembled": assembly_result is not None and assembly_result.get("success", False),
        "rendered": render_result is not None and render_result.get("success", False),
    }

    _progress("complete", f"Done! {clips_ok} clips, ${total_cost:.2f}, project: {project_id}")
    return json.dumps(result)
