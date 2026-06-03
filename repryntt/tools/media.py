"""
media.py — Media / image / voice / Twitter tool wrappers extracted from BrainSystem.

Standalone functions that do NOT require the monolith.
"""

import json
import os
import sys
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("repryntt.tools.media")


# ─── helpers ──────────────────────────────────────────────────────

def _load_gemini_api_key(brain_path) -> str:
    """Extract Gemini API key from ai_config.json."""
    try:
        cfg_path = Path(brain_path) / "ai_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r") as f:
                full_cfg = json.load(f)
            return full_cfg.get("ai_provider", full_cfg).get("google_gemini", {}).get("api_key", "")
    except Exception:
        pass
    return ""


def _load_vision_config(brain_path) -> dict:
    """Load vision provider config from ai_config.json.

    Supports a dedicated 'vision' provider section, or falls back to
    google_gemini for backward compat. Users with local vision LLMs
    (LLaVA, CogVLM, Qwen-VL, etc.) add a 'vision' section to ai_config:

        "vision": {
            "endpoint": "http://localhost:8080/v1/chat/completions",
            "api_key": "",
            "model": "llava-v1.6",
            "provider_type": "openai"  // "openai" or "gemini"
        }
    """
    try:
        cfg_path = Path(brain_path) / "ai_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r") as f:
                full_cfg = json.load(f)
            providers = full_cfg.get("ai_provider", full_cfg)
            # Dedicated vision provider takes priority
            if "vision" in providers and providers["vision"].get("endpoint"):
                return providers["vision"]
    except Exception:
        pass
    return {}  # empty = use default Gemini path


def _load_xai_config(brain_path) -> dict:
    """Extract xAI config from ai_config.json."""
    try:
        cfg_path = Path(brain_path) / "ai_config.json"
        if cfg_path.exists():
            with open(cfg_path, "r") as f:
                full_cfg = json.load(f)
            return full_cfg.get("ai_provider", full_cfg).get("xai", {})
    except Exception:
        pass
    return {}


def _workspace_images_dir(brain_path) -> str:
    """Return (and create) the legacy images directory. Use sensory dirs for new code."""
    from repryntt.paths import operator_dir
    img_dir = str(operator_dir() / "images")
    os.makedirs(img_dir, exist_ok=True)
    return img_dir


def _sensory_dir(subdir: str) -> str:
    """Return (and create) a sensory subdirectory under operator/sensory/."""
    from repryntt.paths import operator_dir
    d = str(operator_dir() / "sensory" / subdir)
    os.makedirs(d, exist_ok=True)
    return d


def _sensory_vision_dir() -> str:
    return _sensory_dir("vision")


def _sensory_generated_dir() -> str:
    return _sensory_dir("generated")


def _sensory_hearing_dir() -> str:
    return _sensory_dir("hearing")


def _sensory_speech_dir() -> str:
    return _sensory_dir("speech")


# ─── Image Generation ────────────────────────────────────────────

def _generate_image_xai(brain_path, prompt, out_path) -> Optional[dict]:
    """Generate image via xAI (grok-imagine-image). Returns result dict or None."""
    import requests as _req

    xai_cfg = _load_xai_config(brain_path)
    xai_key = xai_cfg.get("api_key", "")
    if not xai_key:
        return None

    model = xai_cfg.get("image_model", "grok-imagine-image")
    endpoint = xai_cfg.get("image_endpoint", "https://api.x.ai/v1/images/generations")

    try:
        resp = _req.post(endpoint, json={
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "url",
        }, headers={
            "Authorization": f"Bearer {xai_key}",
            "Content-Type": "application/json",
        }, timeout=90)

        if resp.status_code != 200:
            logger.warning(f"xAI image gen HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        images = data.get("data", [])
        if not images:
            return None

        img_url = images[0].get("url", "")
        if not img_url:
            return None

        img_resp = _req.get(img_url, timeout=60)
        img_resp.raise_for_status()
        img_bytes = img_resp.content

        with open(out_path, "wb") as f:
            f.write(img_bytes)

        return {
            "img_bytes_len": len(img_bytes),
            "model": model,
            "url": img_url,
        }
    except Exception as e:
        logger.warning(f"xAI image generation failed: {e}")
        return None


def generate_image(brain_path, prompt: str = "", filename: str = "",
                   aspect_ratio: str = "1:1", **kwargs) -> str:
    """Generate an image. Tries Gemini first, falls back to xAI.

    Parameters:
        prompt: Detailed description of the image to generate.
        filename: Output filename (saved to agent_workspaces/jarvis/images/).
        aspect_ratio: 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9.
    """
    import requests as _req
    import base64 as _b64
    import time as _time

    if not prompt:
        return json.dumps({"error": "prompt is required — describe the image you want to generate"})

    valid_ratios = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9"}
    if aspect_ratio not in valid_ratios:
        aspect_ratio = "1:1"

    img_dir = _sensory_generated_dir()

    if not filename:
        ts = _time.strftime("%Y%m%d_%H%M%S")
        filename = f"generated_{ts}.png"
    if not filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
        filename += ".png"
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-").strip()
    if not safe_name:
        safe_name = f"generated_{_time.strftime('%Y%m%d_%H%M%S')}.png"

    # Avoid overwriting — append _1, _2, … if file already exists
    out_path = os.path.join(img_dir, safe_name)
    if os.path.exists(out_path):
        base, ext = os.path.splitext(safe_name)
        n = 1
        while os.path.exists(os.path.join(img_dir, f"{base}_{n}{ext}")):
            n += 1
        safe_name = f"{base}_{n}{ext}"
        out_path = os.path.join(img_dir, safe_name)

    # --- Try Gemini first ---
    gemini_failed = False
    api_key = _load_gemini_api_key(brain_path)
    if api_key:
        model = "gemini-2.5-flash-image"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio}
            }
        }

        try:
            resp = _req.post(url, json=payload, timeout=90)
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning(f"Gemini image gen HTTP {resp.status_code}, falling back to xAI")
                gemini_failed = True
            else:
                resp.raise_for_status()
                data = resp.json()

                candidates = data.get("candidates", [])
                if not candidates:
                    gemini_failed = True
                else:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    image_data = None
                    description = ""
                    for part in parts:
                        if "inlineData" in part:
                            image_data = part["inlineData"]
                        elif "text" in part:
                            description = part["text"]

                    if image_data:
                        img_bytes = _b64.b64decode(image_data["data"])
                        with open(out_path, "wb") as f:
                            f.write(img_bytes)

                        size_kb = len(img_bytes) / 1024
                        rel_path = os.path.relpath(out_path, str(Path(brain_path).parent))
                        logger.info(f"Generated image (Gemini): {rel_path} ({size_kb:.0f} KB)")
                        return json.dumps({
                            "success": True,
                            "file_path": rel_path,
                            "absolute_path": out_path,
                            "size_kb": round(size_kb, 1),
                            "aspect_ratio": aspect_ratio,
                            "model": model,
                            "description": description[:300],
                            "prompt_used": prompt[:200],
                        })
                    else:
                        gemini_failed = True
        except _req.exceptions.RequestException:
            gemini_failed = True
    else:
        gemini_failed = True

    # --- Fallback to xAI ---
    if gemini_failed:
        logger.info("Gemini unavailable, trying xAI image generation...")
        xai_result = _generate_image_xai(brain_path, prompt, out_path)
        if xai_result:
            size_kb = xai_result["img_bytes_len"] / 1024
            rel_path = os.path.relpath(out_path, str(Path(brain_path).parent))
            logger.info(f"Generated image (xAI): {rel_path} ({size_kb:.0f} KB)")
            return json.dumps({
                "success": True,
                "file_path": rel_path,
                "absolute_path": out_path,
                "size_kb": round(size_kb, 1),
                "aspect_ratio": aspect_ratio,
                "model": xai_result["model"],
                "description": "",
                "prompt_used": prompt[:200],
                "provider": "xai",
            })

    return json.dumps({"error": "Image generation failed on both Gemini and xAI. Check API keys and quotas."})


# ─── Image Download (from URL) ───────────────────────────────────

def download_image(brain_path, url: str = "", filename: str = "",
                   query: str = "", **kwargs) -> str:
    """Download an image from the internet or search for one.

    Use this when you need the REAL image of something (viral animal, person,
    event) instead of generating an AI image. Essential for attention-cycle
    token launches where the logo must be the actual viral image.

    Two modes:
      1. Direct URL: Pass a URL to download that specific image.
      2. Image search: Pass a query string to search Google Images and
         download the top result.

    Parameters:
        url: Direct URL to an image file (PNG/JPG/WEBP/GIF).
        filename: Output filename (saved to agent_workspaces/jarvis/images/).
        query: Search query for Google Images (used if url is empty).
               E.g. 'moodeng baby pygmy hippo' or 'viral cat meme 2026'
    """
    import requests as _req
    import time as _time
    import re

    if not url and not query:
        return json.dumps({"error": "Provide either url (direct image URL) or query (image search terms)"})

    img_dir = _workspace_images_dir(brain_path)

    # Mode 2: Image search → find a URL first
    if not url and query:
        search_result = _search_image_url(query)
        if not search_result:
            return json.dumps({
                "error": f"No image found for '{query}'. Try a more specific query, "
                         "or find a URL manually with web_search() and pass it as url=."
            })
        url = search_result["url"]
        if not filename:
            filename = search_result.get("suggested_filename", "")

    # Generate filename if needed
    if not filename:
        ts = _time.strftime("%Y%m%d_%H%M%S")
        ext = ".jpg"
        if ".png" in url.lower():
            ext = ".png"
        elif ".webp" in url.lower():
            ext = ".webp"
        elif ".gif" in url.lower():
            ext = ".gif"
        filename = f"downloaded_{ts}{ext}"

    if not filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        filename += ".jpg"
    safe_name = re.sub(r'[^\w._-]', '', filename).strip()
    if not safe_name:
        safe_name = f"downloaded_{_time.strftime('%Y%m%d_%H%M%S')}.jpg"
    out_path = os.path.join(img_dir, safe_name)

    # Download the image
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = _req.get(url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type and "octet-stream" not in content_type:
            return json.dumps({
                "error": f"URL did not return an image (content-type: {content_type}). "
                         "Make sure the URL points directly to an image file, not a web page."
            })

        img_bytes = resp.content
        if len(img_bytes) < 1000:
            return json.dumps({"error": "Downloaded file is too small — likely not a real image"})

        with open(out_path, "wb") as f:
            f.write(img_bytes)

    except _req.exceptions.RequestException as e:
        return json.dumps({"error": f"Download failed: {str(e)[:200]}"})

    size_kb = len(img_bytes) / 1024
    rel_path = os.path.relpath(out_path, str(Path(brain_path).parent))

    logger.info(f"Downloaded image: {rel_path} ({size_kb:.0f} KB) from {url[:80]}")

    return json.dumps({
        "success": True,
        "file_path": rel_path,
        "absolute_path": out_path,
        "size_kb": round(size_kb, 1),
        "source_url": url,
        "prompt_used": query if query else "",
    })


def _search_image_url(query: str) -> Optional[dict]:
    """Search for an image URL using web search. Returns dict with url + suggested_filename."""
    import requests as _req
    import re

    # Use DuckDuckGo instant answer API for image search (no API key needed)
    try:
        resp = _req.get("https://api.duckduckgo.com/", params={
            "q": query,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1,
        }, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # Check for a direct image in the response
            image_url = data.get("Image", "")
            if image_url:
                if image_url.startswith("/"):
                    image_url = "https://duckduckgo.com" + image_url
                safe_q = re.sub(r'[^\w]', '_', query)[:30]
                return {"url": image_url, "suggested_filename": f"{safe_q}.jpg"}
    except Exception:
        pass

    # Fallback: try Google's favicon/knowledge graph-style search
    # by searching for the topic and looking for og:image in results
    try:
        search_url = "https://www.google.com/search"
        resp = _req.get(search_url, params={"q": query, "tbm": "isch"},
                       headers={
                           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                       }, timeout=15)
        if resp.status_code == 200:
            # Extract image URLs from the response
            urls = re.findall(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)', resp.text)
            # Filter out google's own assets
            urls = [u for u in urls if "google" not in u and "gstatic" not in u]
            if urls:
                safe_q = re.sub(r'[^\w]', '_', query)[:30]
                return {"url": urls[0], "suggested_filename": f"{safe_q}.jpg"}
    except Exception:
        pass

    return None


# ─── Vision / Image Analysis ─────────────────────────────────────

def _analyze_via_openai_compat(endpoint: str, api_key: str, model: str,
                                img_b64: str, mime: str, prompt: str) -> str:
    """Send vision request via OpenAI-compatible API (works with local LLMs,
    NVIDIA, OpenRouter, vLLM, Ollama, LM Studio, text-generation-webui, etc.)."""
    import requests as _req

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{img_b64}"
                }}
            ]
        }],
        "max_tokens": 4096,
        "temperature": 0.3,
    }

    resp = _req.post(endpoint, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _analyze_via_gemini(api_key: str, model: str,
                         img_b64: str, mime: str, prompt: str) -> str:
    """Send vision request via Gemini's native REST API."""
    import requests as _req

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": img_b64}}
        ]}],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    resp = _req.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p["text"] for p in parts if "text" in p]
        if text_parts:
            return " ".join(text_parts)
    return ""


# ─── Multi-image vision (for temporal context in navigation) ────────
#
# Single-frame VLM calls are stateless — the model can't tell "I'm circling
# the same chair from a new angle" from "I'm seeing a new chair". Passing the
# last N frames gives it motion + place-recognition with no extra training.

def analyze_images_with_gemini(brain_path, image_paths: list,
                               question: str = "", **kwargs) -> str:
    """Send multiple images to a vision-capable LLM in a single call.

    Accepts a list of file paths; the oldest frame comes first, newest last.
    Uses the same vision-provider routing as analyze_image_with_gemini
    (vision-config first, Gemini fallback).
    """
    import base64 as _b64

    if not image_paths:
        return json.dumps({"error": "No images provided"})

    # Load all images (skip missing ones silently)
    images: list = []
    for p in image_paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
            ext = os.path.splitext(p)[1].lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp"}.get(
                        ext, "image/jpeg")
            images.append((_b64.b64encode(data).decode("utf-8"), mime))
        except Exception as e:
            logger.debug(f"Failed to load image {p}: {e}")

    if not images:
        return json.dumps({"error": "No readable images"})

    prompt_text = question if question else (
        "These images are sequential frames from a robot's forward camera "
        "(oldest first, newest last). Describe the scene and any motion."
    )

    vision_cfg = _load_vision_config(brain_path)
    if vision_cfg:
        endpoint = vision_cfg["endpoint"]
        api_key = vision_cfg.get("api_key", "")
        model = vision_cfg.get("model", "default")
        provider_type = vision_cfg.get("provider_type", "openai")
        # Some hosted VLMs (e.g. NVIDIA NIM Llama-3.2-vision) reject
        # multi-image requests with HTTP 400. Cap to the newest N frames
        # when the provider declares a limit.
        max_images = int(vision_cfg.get("max_images", 0) or 0)
        send_images = images[-max_images:] if max_images > 0 else images
        try:
            if provider_type == "gemini":
                result = _analyze_multi_via_gemini(
                    api_key, model, send_images, prompt_text)
            else:
                result = _analyze_multi_via_openai_compat(
                    endpoint, api_key, model, send_images, prompt_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"Multi-image vision provider failed: {e}")

    api_key = _load_gemini_api_key(brain_path)
    if not api_key:
        return "No Gemini API key configured"
    try:
        return _analyze_multi_via_gemini(
            api_key, "gemini-2.5-flash", images, prompt_text) or ""
    except Exception as e:
        return json.dumps({"error": f"Multi-image vision failed: {str(e)[:200]}"})


def _analyze_multi_via_gemini(api_key: str, model: str,
                               images: list, prompt: str) -> str:
    """Send multi-image request via Gemini's native REST API."""
    import requests as _req
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    parts = [{"text": prompt}]
    for b64, mime in images:
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    resp = _req.post(url, json=payload, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates:
        parts_out = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p["text"] for p in parts_out if "text" in p]
        if text_parts:
            return " ".join(text_parts)
    return ""


def _analyze_multi_via_openai_compat(endpoint: str, api_key: str, model: str,
                                      images: list, prompt: str) -> str:
    """Send multi-image request via OpenAI-compatible API."""
    import requests as _req
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    content = [{"type": "text", "text": prompt}]
    for b64, mime in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    resp = _req.post(endpoint, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def analyze_image_with_gemini(brain_path, image_path: str = "",
                              question: str = "", **kwargs) -> str:
    """Send an image to a vision-capable LLM for analysis.

    Routes through ai_config.json:
      1. If 'vision' provider is configured → uses that (local LLM, NVIDIA, etc.)
      2. Otherwise → falls back to Gemini API (backward compatible)

    Users with powerful GPUs can point 'vision' at a local endpoint running
    LLaVA, CogVLM, Qwen-VL, InternVL, or any OpenAI-compatible vision server.
    """
    import base64 as _b64

    if not image_path or not os.path.isfile(image_path):
        return json.dumps({"error": f"Image not found: {image_path}"})

    with open(image_path, "rb") as f:
        img_data = f.read()
    img_b64 = _b64.b64encode(img_data).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")

    prompt_text = question if question else (
        "Describe what you see in this image in detail. Include objects, "
        "people, text, environment, lighting, and anything notable."
    )

    # ── Check for dedicated vision provider first ──
    vision_cfg = _load_vision_config(brain_path)
    if vision_cfg:
        endpoint = vision_cfg["endpoint"]
        api_key = vision_cfg.get("api_key", "")
        model = vision_cfg.get("model", "default")
        provider_type = vision_cfg.get("provider_type", "openai")

        try:
            if provider_type == "gemini":
                result = _analyze_via_gemini(api_key, model, img_b64, mime, prompt_text)
            else:
                # OpenAI-compatible: covers local LLMs, NVIDIA, vLLM, Ollama, etc.
                result = _analyze_via_openai_compat(endpoint, api_key, model,
                                                     img_b64, mime, prompt_text)
            if result:
                return result
            return "No analysis returned from vision model"
        except Exception as e:
            logger.warning(f"Vision provider failed ({endpoint}): {e}, falling back to Gemini")
            # Fall through to Gemini

    # ── Default: Gemini API ──
    api_key = _load_gemini_api_key(brain_path)
    if not api_key:
        return "No Gemini API key configured and no vision provider set"

    try:
        result = _analyze_via_gemini(api_key, "gemini-2.5-flash", img_b64, mime, prompt_text)
        if result:
            return result
        return "No analysis returned from Gemini"
    except Exception as e:
        return json.dumps({"error": f"Vision API failed: {str(e)[:200]}"})


# ─── Camera Capture ───────────────────────────────────────────────

def capture_camera(brain_path, camera_id: int = 0, analyze: bool = False,
                   question: str = "", filename: str = "", save: bool = True, **kwargs) -> str:
    """Capture an image from a camera.

    Cross-platform: uses NVIDIA GStreamer on Jetson, standard OpenCV
    VideoCapture on Windows/macOS/Linux desktops.

    Parameters:
        camera_id: 0 = default camera, 1 = secondary camera.
        analyze: If True, send to Gemini Vision for AI analysis.
        question: When analyze=True, ask a specific question about the image.
        filename: Output filename.
        save: If False, use a temp file for analysis only (not saved permanently).
    """
    import time as _time

    try:
        import cv2
    except ImportError:
        return json.dumps({"error": "OpenCV (cv2) not installed"})

    img_dir = _sensory_vision_dir()

    # Date-based subfolder for easy browsing
    import time as _time2
    date_str = _time2.strftime("%Y-%m-%d")
    date_dir = os.path.join(img_dir, date_str)
    os.makedirs(date_dir, exist_ok=True)

    if not filename:
        ts = _time.strftime("%H-%M-%S")
        filename = f"cam{camera_id}_{ts}.jpg"
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-").strip()
    if not safe_name:
        safe_name = f"cam{camera_id}_{_time.strftime('%H-%M-%S')}.jpg"

    # If save=False, use a temp file that gets cleaned up after analysis
    if save:
        out_path = os.path.join(date_dir, safe_name)
    else:
        import tempfile as _tmpf2
        _tmp_handle = _tmpf2.NamedTemporaryFile(suffix=".jpg", delete=False)
        out_path = _tmp_handle.name
        _tmp_handle.close()

    captured = False

    # ── Strategy 1: Jetson GStreamer (nvarguscamerasrc) ──
    # Only available on Linux with NVIDIA Jetson hardware
    #
    # IMPORTANT: nvarguscamerasrc runs auto-exposure / auto-white-balance
    # in firmware (Argus). On the IMX219 sensor, AE/AWB takes 10–30 frames
    # to converge — frame 0 is captured with default sensor settings and is
    # frequently overexposed (pure white) under bright lighting.
    #
    # We therefore use `multifilesink` (one file per buffer) over the
    # previously-used `filesink` (which concatenated 30 JPEGs into one
    # file — `cv2.imread` stops at the first EOI marker and returns
    # frame 0). After capture, we keep the LAST file written, which is
    # the AE/AWB-converged frame. If it still looks overexposed we retry
    # once with a longer warmup window.
    if sys.platform == "linux" and os.path.exists(f"/dev/video{camera_id}"):
        import subprocess as _sp
        import tempfile as _tmpf
        import shutil as _shutil
        import glob as _glob

        def _gst_capture(num_buffers: int) -> bool:
            """Run an nvarguscamerasrc pipeline and keep the last frame.

            Returns True if a valid JPEG ended up at out_path.
            """
            warmup_dir = _tmpf.mkdtemp(prefix="cam_warmup_")
            pattern = os.path.join(warmup_dir, "frame_%05d.jpg")
            try:
                gst_cmd = [
                    "gst-launch-1.0", "-e",
                    "nvarguscamerasrc", f"sensor-id={camera_id}",
                    f"num-buffers={num_buffers}",
                    "!", "video/x-raw(memory:NVMM),width=1280,height=720,"
                         "format=NV12,framerate=30/1",
                    "!", "nvjpegenc",
                    "!", "multifilesink", f"location={pattern}",
                ]
                # Allow a bit longer than num_buffers/30 fps + overhead
                _timeout = max(20, int(num_buffers / 30) + 15)
                proc = _sp.run(gst_cmd, capture_output=True, text=True,
                               timeout=_timeout)
                if proc.returncode != 0:
                    return False
                files = sorted(_glob.glob(os.path.join(warmup_dir, "frame_*.jpg")))
                if not files:
                    return False
                # Last file = most-converged AE/AWB frame.
                last = files[-1]
                if os.path.getsize(last) < 100:
                    return False
                _shutil.move(last, out_path)
                return True
            except (FileNotFoundError, _sp.TimeoutExpired, Exception) as e:
                logger.debug(f"GStreamer capture (n={num_buffers}) failed: {e}")
                return False
            finally:
                _shutil.rmtree(warmup_dir, ignore_errors=True)

        try:
            ok = _gst_capture(num_buffers=30)
            if ok:
                # Verify the frame isn't blown-out white. Mean ≥ 245 (out of
                # 255) means AE didn't converge — retry with 90 buffers
                # (~3 s) so AE has more time to settle on bright scenes.
                try:
                    import numpy as _np
                    img = cv2.imread(out_path)
                    if img is not None and img.mean() >= 245:
                        logger.info(
                            f"Camera {camera_id}: first capture overexposed "
                            f"(mean={img.mean():.0f}), retrying with longer warmup"
                        )
                        ok = _gst_capture(num_buffers=90)
                        if ok:
                            img = cv2.imread(out_path)

                    # Adaptive brightness — only correct if image is dim
                    # but recoverable. Overexposure was already handled above.
                    if img is not None:
                        mean_brightness = img.mean()
                        if 25 < mean_brightness < 120:
                            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                            l, a, b = cv2.split(lab)
                            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                            l = clahe.apply(l)
                            lab = cv2.merge([l, a, b])
                            img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
                            gamma = max(0.5, min(0.9, mean_brightness / 130.0))
                            lut = _np.array(
                                [((i / 255.0) ** gamma) * 255 for i in range(256)]
                            ).astype("uint8")
                            img = cv2.LUT(img, lut)
                            cv2.imwrite(out_path, img)
                        elif mean_brightness >= 245:
                            logger.warning(
                                f"Camera {camera_id}: image still overexposed "
                                f"after retry (mean={mean_brightness:.0f}) — "
                                f"sensor may be pointed at a bright source"
                            )
                except Exception:
                    pass  # original image still valid
                captured = True

        except Exception as e:
            logger.debug(f"GStreamer capture failed, trying OpenCV fallback: {e}")

    # ── Strategy 2: Standard OpenCV VideoCapture (all platforms) ──
    if not captured:
        try:
            cap = cv2.VideoCapture(camera_id)
            if not cap.isOpened():
                if not save:
                    try: os.unlink(out_path)
                    except OSError: pass
                return json.dumps({"error": f"Camera {camera_id} not found or not accessible"})

            # Warm up — USB cameras need 10-15 frames for auto-exposure /
            # auto-white-balance to converge. Reading too few frames is the
            # main cause of overexposed (pure white) captures under bright
            # light, since the sensor's default exposure is rarely correct.
            for _ in range(15):
                cap.read()

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                if not save:
                    try: os.unlink(out_path)
                    except OSError: pass
                return json.dumps({"error": f"Camera {camera_id} opened but failed to capture frame"})

            cv2.imwrite(out_path, frame)
            captured = True
        except Exception as e:
            if not save:
                try: os.unlink(out_path)
                except OSError: pass
            return json.dumps({"error": f"Camera capture failed: {str(e)[:200]}"})

    if not captured:
        if not save:
            try: os.unlink(out_path)
            except OSError: pass
        return json.dumps({"error": "No capture method succeeded — camera may not be connected"})

    frame = cv2.imread(out_path)
    if frame is None:
        if not save:
            try: os.unlink(out_path)
            except OSError: pass
        return json.dumps({"error": "Captured image could not be read back"})
    size_kb = os.path.getsize(out_path) / 1024
    height, width = frame.shape[:2]
    rel_path = os.path.relpath(out_path, str(Path(brain_path).parent)) if save else "(temporary)"

    result = {
        "success": True,
        "file_path": rel_path,
        "absolute_path": out_path if save else "(not saved)",
        "camera_id": camera_id,
        "resolution": f"{width}x{height}",
        "size_kb": round(size_kb, 1),
    }

    if analyze:
        try:
            analysis = analyze_image_with_gemini(brain_path, out_path, question)
            result["analysis"] = analysis
        except Exception as e:
            result["analysis_error"] = str(e)[:200]

    # Clean up temp file if we weren't saving
    if not save:
        try: os.unlink(out_path)
        except OSError: pass

    logger.info(f"Camera {camera_id} capture: {rel_path} ({width}x{height}, {size_kb:.0f} KB)")
    return json.dumps(result)


# ─── Image Cleanup ────────────────────────────────────────────────

def cleanup_images(brain_path, max_images: int = 100):
    """Remove oldest images when count exceeds max_images.
    
    Cleans sensory/vision/ and sensory/generated/ separately.
    Also cleans legacy images/ dir if it still has files.
    Keeps the most recent `max_images` files per directory.
    Returns total number of files deleted.
    """
    def _cleanup_dir(dir_path, limit, recurse=False):
        if not os.path.isdir(dir_path):
            return 0
        files = []
        if recurse:
            # Walk date subdirectories
            for root, dirs, fnames in os.walk(dir_path):
                if os.path.basename(root) == 'archive':
                    continue
                for f in fnames:
                    fp = os.path.join(root, f)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".wav")):
                        files.append((os.path.getmtime(fp), fp))
        else:
            for f in os.listdir(dir_path):
                fp = os.path.join(dir_path, f)
                if os.path.isfile(fp) and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".wav")):
                    files.append((os.path.getmtime(fp), fp))
        if len(files) <= limit:
            return 0
        files.sort()
        to_delete = files[: len(files) - limit]
        deleted = 0
        for _, fp in to_delete:
            try:
                os.remove(fp)
                deleted += 1
            except OSError:
                pass
        return deleted

    total = 0
    total += _cleanup_dir(_sensory_vision_dir(), max_images, recurse=True)
    total += _cleanup_dir(_sensory_generated_dir(), max_images)
    total += _cleanup_dir(_sensory_hearing_dir(), max_images)
    total += _cleanup_dir(_sensory_speech_dir(), max_images)
    # Legacy dir
    total += _cleanup_dir(_workspace_images_dir(brain_path), max_images)
    if total:
        logger.info("Sensory cleanup: removed %d old files", total)
    return total


# ─── Voice (Piper TTS) ───────────────────────────────────────────

# Default paths — resolved dynamically per platform
import shutil as _shutil_piper
from pathlib import Path as _PathPiper
_DEFAULT_PIPER_BIN = _shutil_piper.which("piper") or str(_PathPiper.home() / ".local" / "bin" / "piper")
PIPER_BIN = os.environ.get("PIPER_BIN", _DEFAULT_PIPER_BIN)
_DEFAULT_PIPER_MODEL = str(_PathPiper.home() / ".repryntt" / "voices" / "models" / "ryan.onnx")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "plughw:0,0")


def _get_active_piper_model() -> str:
    """Get the active Piper model — checks voice profiles first, then env, then default."""
    # Environment override always wins
    env_model = os.environ.get("PIPER_MODEL")
    if env_model:
        return env_model
    # Check voice profiles for an active custom voice
    try:
        profiles_file = Path.home() / ".repryntt" / "voices" / "profiles.json"
        if profiles_file.exists():
            import json as _json
            with open(profiles_file) as f:
                profiles = _json.load(f)
            for p in profiles.values():
                if p.get("active") and Path(p["model_path"]).exists():
                    return p["model_path"]
    except Exception:
        pass
    return _DEFAULT_PIPER_MODEL


# Resolve once at import, refreshed per call in speak()
PIPER_MODEL = _get_active_piper_model()


def speak(brain_path=None, text: str = "", **kwargs) -> str:
    """Speak text out loud using the best available TTS engine.

    Platform support:
      - Linux (Jetson): Piper neural TTS + aplay
      - Linux (desktop): Piper if installed, else espeak/pyttsx3
      - macOS: 'say' command (built-in)
      - Windows: pyttsx3 (uses SAPI5)

    Parameters:
        text: What to say. Truncated to 500 chars for natural speech.
    """
    import subprocess, tempfile

    if not text:
        return json.dumps({"error": "text parameter is required — what do you want to say?"})

    spoken_text = text[:500]
    if len(text) > 500:
        spoken_text = spoken_text.rsplit(' ', 1)[0] + '...'

    # blocking=True waits for playback to finish; False fires and forgets
    blocking = kwargs.get("blocking", False)

    def _save_speech_history(wav_path):
        """Save a copy to sensory/speech/ for history."""
        try:
            import shutil as _shutil_speak
            import time as _time_speak
            speech_dir = _sensory_speech_dir()
            ts = _time_speak.strftime("%Y%m%d_%H%M%S")
            saved_path = os.path.join(speech_dir, f"spoke_{ts}.wav")
            _shutil_speak.copy2(wav_path, saved_path)
            return saved_path
        except Exception:
            return None

    # ── Strategy 1: Piper TTS + aplay (Linux, best quality) ──
    active_model = _get_active_piper_model()
    if os.path.isfile(PIPER_BIN) and os.path.isfile(active_model):
        wav_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name

            piper_proc = subprocess.run(
                [PIPER_BIN, "--model", active_model,
                 "--length-scale", "1.3",
                 "--output_file", wav_path],
                input=spoken_text,
                capture_output=True, text=True, timeout=30,
            )
            if piper_proc.returncode == 0:
                saved_path = _save_speech_history(wav_path)
                from repryntt.platform_utils import play_audio_file, IS_LINUX
                if blocking:
                    if IS_LINUX:
                        subprocess.run(["amixer", "-c", "0", "set", "Mic", "nocap"],
                                       capture_output=True, timeout=5)
                    try:
                        play_audio_file(wav_path, device=AUDIO_DEVICE, blocking=True)
                    finally:
                        if IS_LINUX:
                            subprocess.run(["amixer", "-c", "0", "set", "Mic", "cap"],
                                           capture_output=True, timeout=5)
                        import time; time.sleep(0.5)
                else:
                    play_audio_file(wav_path, device=AUDIO_DEVICE, blocking=False)
                logger.info(f"Spoke (piper): {spoken_text[:80]}...")
                return json.dumps({
                    "success": True, "engine": "piper",
                    "spoken": spoken_text, "chars": len(spoken_text),
                    "saved": saved_path,
                })
        except Exception as e:
            logger.debug(f"Piper TTS failed, trying fallback: {e}")
        finally:
            if wav_path:
                try:
                    import threading
                    threading.Timer(10, lambda p=wav_path: os.unlink(p) if os.path.exists(p) else None).start()
                except Exception:
                    pass

    # ── Strategy 2: macOS 'say' command ──
    if sys.platform == "darwin":
        try:
            if blocking:
                subprocess.run(["say", spoken_text], timeout=60)
            else:
                subprocess.Popen(["say", spoken_text],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"Spoke (macOS say): {spoken_text[:80]}...")
            return json.dumps({
                "success": True, "engine": "macos_say",
                "spoken": spoken_text, "chars": len(spoken_text),
            })
        except Exception as e:
            logger.debug(f"macOS say failed: {e}")

    # ── Strategy 3: pyttsx3 (Windows SAPI5 / Linux espeak fallback) ──
    try:
        import pyttsx3
        # Reuse a module-level engine to avoid weak-reference crash on GC
        if not hasattr(speak, '_tts_engine') or speak._tts_engine is None:
            speak._tts_engine = pyttsx3.init()
        engine = speak._tts_engine
        engine.setProperty('rate', 175)
        engine.setProperty('volume', 0.9)
        engine.say(spoken_text)
        if blocking:
            engine.runAndWait()
        else:
            import threading
            threading.Thread(target=engine.runAndWait, daemon=True).start()
        logger.info(f"Spoke (pyttsx3): {spoken_text[:80]}...")
        return json.dumps({
            "success": True, "engine": "pyttsx3",
            "spoken": spoken_text, "chars": len(spoken_text),
        })
    except Exception as e:
        logger.debug(f"pyttsx3 TTS failed: {e}")

    return json.dumps({"error": "No TTS engine available. Install pyttsx3 (pip install pyttsx3) for cross-platform speech."})


# ─── Listen (Faster-Whisper STT) ─────────────────────────────────

_whisper_model = None


def _get_whisper_model():
    """Load or return cached faster-whisper model (small, int8 quantized)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper STT model (small, int8)...")
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        logger.info("Whisper model loaded and cached")
    return _whisper_model


def listen(brain_path=None, duration: str = "5", conversation: bool = False, **kwargs) -> str:
    """Listen through the microphone and transcribe using local Whisper AI.

    Platform support:
      - Linux: arecord (ALSA) → Whisper
      - Windows/macOS: sounddevice → Whisper

    Parameters:
        duration: Seconds to listen (default 5, max 30).  Ignored when
                  conversation=True.
        conversation: If True, use voice-activity detection (VAD) to
                      record until the speaker stops talking (up to 30s).
                      This avoids cutting people off mid-sentence and
                      avoids wasting time recording silence.
    """
    import subprocess, tempfile

    try:
        listen_secs = min(int(duration), 30)
    except (ValueError, TypeError):
        listen_secs = 5

    wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name

        recorded = False

        # ── VAD-aware recording (conversation mode) ──
        # Uses sounddevice to stream audio and detect when the speaker
        # stops talking, rather than recording a fixed window.
        if conversation and not recorded:
            try:
                import sounddevice as sd
                import numpy as np
                import wave

                sample_rate = 16000
                chunk_ms = 200          # 200ms chunks
                chunk_samples = int(sample_rate * chunk_ms / 1000)
                max_secs = 30
                max_chunks = int(max_secs * 1000 / chunk_ms)

                # Thresholds (tuned for close-range voice on Jetson)
                energy_threshold = 300      # RMS above this = speech
                silence_chunks_exit = 8     # 1.6s of silence after speech → stop
                initial_wait_chunks = 25    # 5s to start talking before giving up

                chunks_collected = []
                speech_detected = False
                silence_after_speech = 0

                for i in range(max_chunks):
                    chunk = sd.rec(chunk_samples, samplerate=sample_rate,
                                   channels=1, dtype='int16')
                    sd.wait()

                    rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))

                    if rms > energy_threshold:
                        speech_detected = True
                        silence_after_speech = 0
                    elif speech_detected:
                        silence_after_speech += 1

                    chunks_collected.append(chunk)

                    # Exit conditions
                    if speech_detected and silence_after_speech >= silence_chunks_exit:
                        break  # Speaker finished
                    if not speech_detected and i >= initial_wait_chunks:
                        break  # Nobody started talking

                if chunks_collected:
                    audio_data = np.concatenate(chunks_collected, axis=0)
                    with wave.open(wav_path, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(sample_rate)
                        wf.writeframes(audio_data.tobytes())
                    recorded = True
                    listen_secs = len(chunks_collected) * chunk_ms / 1000
                    if not speech_detected:
                        return json.dumps({"text": "", "silence": True,
                                           "note": "No speech detected (VAD)"})
            except Exception as e:
                logger.debug(f"VAD recording failed, falling back to fixed: {e}")

        # ── Strategy 1: arecord (Linux ALSA) ──
        if not recorded and sys.platform == "linux":
            try:
                record_cmd = [
                    "arecord", "-D", AUDIO_DEVICE,
                    "-d", str(listen_secs),
                    "-f", "S16_LE", "-r", "16000", "-c", "1",
                    wav_path,
                ]
                rec_proc = subprocess.run(
                    record_cmd, capture_output=True, text=True,
                    timeout=listen_secs + 10,
                )
                if rec_proc.returncode == 0:
                    recorded = True
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                logger.debug(f"arecord failed, trying sounddevice: {e}")

        # ── Strategy 2: sounddevice (cross-platform) ──
        if not recorded:
            try:
                import sounddevice as sd
                import numpy as np
                import wave

                sample_rate = 16000
                audio_data = sd.rec(
                    int(listen_secs * sample_rate),
                    samplerate=sample_rate,
                    channels=1,
                    dtype='int16',
                )
                sd.wait()

                with wave.open(wav_path, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(sample_rate)
                    wf.writeframes(audio_data.tobytes())
                recorded = True
            except Exception as e:
                logger.debug(f"sounddevice recording failed: {e}")

        if not recorded:
            return json.dumps({"error": "No recording backend available. "
                              "Install sounddevice (pip install sounddevice) for cross-platform microphone support."})

        file_size = os.path.getsize(wav_path)
        if file_size < 1000:
            return json.dumps({"text": "", "silence": True, "note": "No audio captured"})

        # ── Quick energy check before loading Whisper (~1.1GB) ──
        # If the audio is mostly silence (low RMS energy), skip the
        # expensive Whisper model load.  This prevents the conversational
        # awareness wake-word checker from loading 1.1GB on first call
        # when nobody is speaking.
        try:
            import wave as _wave_check
            import numpy as _np_check
            with _wave_check.open(wav_path, 'rb') as _wf:
                _raw = _wf.readframes(_wf.getnframes())
                _audio = _np_check.frombuffer(_raw, dtype=_np_check.int16).astype(_np_check.float32)
                _rms = _np_check.sqrt(_np_check.mean(_audio ** 2))
            if _rms < 200:  # mostly silence / faint background noise
                return json.dumps({"text": "", "silence": True, "note": "Audio below speech threshold"})
        except Exception:
            pass  # If check fails, proceed normally

        # Run Whisper in a subprocess to avoid loading ~1.1 GB of
        # torch + faster-whisper into the main daemon process.
        _worker = str(Path(__file__).parent / "_whisper_worker.py")
        try:
            _proc = subprocess.run(
                [sys.executable, _worker, wav_path],
                capture_output=True, text=True, timeout=60,
            )
            _wdata = json.loads(_proc.stdout.strip()) if _proc.stdout.strip() else {}
            if "error" in _wdata:
                logger.warning(f"Whisper worker error: {_wdata['error']}")
                return json.dumps({"text": "", "silence": True, "note": f"Whisper error: {_wdata['error']}"})
            text = _wdata.get("text", "").strip()
        except subprocess.TimeoutExpired:
            logger.warning("Whisper subprocess timed out (60s)")
            return json.dumps({"text": "", "silence": True, "note": "Whisper timeout"})
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Whisper subprocess failed: {e}")
            return json.dumps({"text": "", "silence": True, "note": str(e)})

        if not text:
            return json.dumps({"text": "", "silence": True})

        # Save recording to sensory/hearing/ for history
        saved_path = None
        try:
            import shutil as _shutil_listen
            import time as _time_listen
            hearing_dir = _sensory_hearing_dir()
            ts = _time_listen.strftime("%Y%m%d_%H%M%S")
            saved_path = os.path.join(hearing_dir, f"heard_{ts}.wav")
            _shutil_listen.copy2(wav_path, saved_path)
        except Exception:
            pass

        logger.info(f"Heard: {text[:80]}...")
        return json.dumps({
            "text": text,
            "duration_seconds": listen_secs,
            "silence": False,
            "saved": saved_path,
        })

    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Recording timed out ({listen_secs}s)"})
    except Exception as e:
        return json.dumps({"error": f"listen failed: {str(e)}"})
    finally:
        try:
            if wav_path and os.path.exists(wav_path):
                os.unlink(wav_path)
        except Exception:
            pass


# ─── Twitter Tools ────────────────────────────────────────────────

def post_tweet_autonomous(brain_path=None, content: str = None,
                          generate_image: bool = False, **kwargs) -> str:
    """Post a tweet to Twitter autonomously."""
    try:
        from repryntt.web.twitter import get_twitter_interface, post_tweet_tool
        import asyncio

        _twitter = get_twitter_interface()

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(post_tweet_tool(content=content, generate_image=generate_image))
        return result
    except Exception as e:
        logger.error(f"Error posting tweet: {e}", exc_info=True)
        return f"Error posting tweet: {str(e)}"


def check_twitter_mentions(brain_path=None, **kwargs) -> str:
    """Check Twitter mentions and respond to them."""
    try:
        from repryntt.web.twitter import get_twitter_interface, check_twitter_mentions_tool
        import asyncio

        _twitter = get_twitter_interface()

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(check_twitter_mentions_tool())
        return result
    except Exception as e:
        logger.error(f"Error checking Twitter mentions: {e}", exc_info=True)
        return f"Error checking mentions: {str(e)}"


def reply_to_twitter_mention(brain_path=None, mention_url: str = "",
                             reply_text: str = None, **kwargs) -> str:
    """Reply to a specific Twitter mention."""
    if not mention_url:
        return json.dumps({"error": "mention_url is required"})
    try:
        from repryntt.web.twitter import get_twitter_interface
        import asyncio

        twitter = get_twitter_interface()

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(twitter.reply_to_mention(mention_url, reply_text))

        if result.get("success"):
            return f"Reply posted successfully to {mention_url}"
        else:
            return f"Reply failed: {result.get('error', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error replying to mention: {e}", exc_info=True)
        return f"Error replying to mention: {str(e)}"


def get_twitter_status(brain_path=None, **kwargs) -> str:
    """Get Twitter account status and statistics."""
    try:
        from repryntt.web.twitter import get_twitter_interface, get_twitter_status_tool
        _twitter = get_twitter_interface()
        return get_twitter_status_tool()
    except Exception as e:
        logger.error(f"Error getting Twitter status: {e}", exc_info=True)
        return f"Error getting Twitter status: {str(e)}"


# ─── Provider-agnostic VLM aliases ────────────────────────────────────
#
# nav_cortex (and any other caller that wants vision analysis without
# coupling to a specific provider name) imports these.  The underlying
# functions already route through ai_config.json's 'vision' provider —
# local SmolVLM / NVIDIA NIM / Gemini, whichever is configured — so
# these are intentionally thin aliases. Callers MUST use these names;
# the *_with_gemini names are kept only for backward compatibility.
def analyze_image_with_vision(brain_path, image_path: str = "",
                              question: str = "", **kwargs) -> str:
    """Single-image vision analysis. Routes through whatever vision
    provider is configured in ``ai_config.json`` (local SmolVLM / NVIDIA
    NIM / Gemini)."""
    return analyze_image_with_gemini(brain_path, image_path, question, **kwargs)


def analyze_images_with_vision(brain_path, image_paths: list,
                               question: str = "", **kwargs) -> str:
    """Multi-image vision analysis (e.g. a sequence of prior nav frames
    + the current frame). Same provider routing as the single-image
    variant."""
    return analyze_images_with_gemini(brain_path, image_paths, question, **kwargs)
