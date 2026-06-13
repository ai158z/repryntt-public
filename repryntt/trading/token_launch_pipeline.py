"""
REPRYNTT Token Launch Pipeline — Autonomous PumpFun Token Creation
===================================================================

Step-by-step pipeline for Artemis to create and launch memecoins on pump.fun.

Pipeline stages:
  1. IDEATE   — Generate token concept (name, symbol, narrative, description)
  2. DESIGN   — Generate candidate logo images (3 options)
  3. REVIEW   — Vision-analyze each logo, pick the best one
  4. PREPARE  — Assemble final metadata, upload to IPFS (dry-run safe)
  5. LAUNCH   — Execute on-chain creation (respects DRY_RUN toggle)

Each stage is a standalone function that can be called individually or
chained via run_full_pipeline() for autonomous end-to-end launches.

Vision model: Uses the active LLM provider (NVIDIA Mistral Large 3 supports
vision natively via OpenAI-compatible multimodal messages).
"""

import json
import os
import time
import base64
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

logger = logging.getLogger("repryntt.token_launch_pipeline")

PIPELINE_DIR = Path.home() / ".repryntt" / "wallet" / "launch_pipeline"
PIPELINE_DIR.mkdir(parents=True, exist_ok=True)

NUM_LOGO_CANDIDATES = 3


# ─── Config Loading ──────────────────────────────────────────────────────────

def _load_ai_config() -> dict:
    """Load AI provider config for LLM calls and vision."""
    for cfg_path in [
        Path.home() / ".repryntt" / "brain" / "ai_config.json",
        Path(__file__).resolve().parent.parent.parent / "config" / "ai_config.json",
    ]:
        if cfg_path.exists():
            with open(cfg_path) as f:
                return json.load(f).get("ai_provider", {})
    return {}


def _llm_call(prompt: str, system: str = "", max_tokens: int = 1024) -> str:
    """Make a text-only LLM call using the active provider."""
    cfg = _load_ai_config()
    provider = cfg.get("provider", "nvidia")
    pcfg = cfg.get(provider, {})

    endpoint = pcfg.get("endpoint", "")
    api_key = pcfg.get("api_key", "")
    model = pcfg.get("model", "")

    if not all([endpoint, api_key, model]):
        return '{"error": "LLM not configured"}'

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        _m = (model or "").lower()
        _temp_deprecated = (
            "opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
            or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
            or "claude-5-" in _m
        )
        _body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if not _temp_deprecated:
            _body["temperature"] = 0.8
        resp = requests.post(endpoint, json=_body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, timeout=60)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        return f'{{"error": "LLM HTTP {resp.status_code}: {resp.text[:200]}"}}'
    except Exception as e:
        return f'{{"error": "LLM call failed: {str(e)[:200]}"}}'


def _vision_call(image_path: str, question: str) -> str:
    """Analyze an image using the active provider's vision capability."""
    cfg = _load_ai_config()
    provider = cfg.get("provider", "nvidia")
    pcfg = cfg.get(provider, {})

    endpoint = pcfg.get("endpoint", "")
    api_key = pcfg.get("api_key", "")
    model = pcfg.get("model", "")

    if not os.path.isfile(image_path):
        return f"Image not found: {image_path}"

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get(ext, "image/png")

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        ]
    }]

    # Try active provider first (NVIDIA Mistral Large 3 supports vision)
    try:
        resp = requests.post(endpoint, json={
            "model": model,
            "messages": messages,
            "max_tokens": 512,
        }, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, timeout=30)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        logger.warning(f"Primary vision HTTP {resp.status_code}, trying Gemini fallback")
    except Exception as e:
        logger.warning(f"Primary vision failed: {e}, trying Gemini fallback")

    # Fallback to Gemini Vision
    gemini_key = cfg.get("google_gemini", {}).get("api_key", "")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [
                    {"text": question},
                    {"inline_data": {"mime_type": mime, "data": img_b64}}
                ]}],
                "generationConfig": {"maxOutputTokens": 512}
            }
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                return " ".join(p["text"] for p in parts if "text" in p)
        except Exception:
            pass

    return "Vision analysis unavailable — both primary and Gemini providers failed"


# ─── Pipeline State ──────────────────────────────────────────────────────────

@dataclass
class LaunchPipelineState:
    """Tracks progress through the launch pipeline."""
    pipeline_id: str = ""
    stage: str = "ideate"  # ideate → design → review → prepare → launch
    created_at: str = ""

    # Stage 1: Ideation
    token_name: str = ""
    token_symbol: str = ""
    token_description: str = ""
    narrative: str = ""
    target_audience: str = ""

    # Stage 2: Design
    logo_candidates: List[str] = field(default_factory=list)  # file paths

    # Stage 3: Review
    logo_reviews: List[Dict[str, Any]] = field(default_factory=list)
    selected_logo: str = ""
    selection_reason: str = ""

    # Stage 4: Prepare
    metadata_uri: str = ""
    mint_pubkey: str = ""

    # Stage 5: Launch
    initial_buy_sol: float = 0.1
    tx_signature: str = ""
    launched: bool = False
    dry_run: bool = True

    # Socials
    twitter: str = ""
    telegram: str = ""
    website: str = ""

    def save(self):
        path = PIPELINE_DIR / f"{self.pipeline_id}.json"
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, pipeline_id: str) -> Optional["LaunchPipelineState"]:
        path = PIPELINE_DIR / f"{pipeline_id}.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return cls(**data)
        return None


# ─── Stage 1: IDEATE ─────────────────────────────────────────────────────────

def stage_ideate(
    theme: str = "",
    narrative: str = "",
    target_audience: str = "crypto degens, memecoin traders",
) -> LaunchPipelineState:
    """Generate a token concept. Provide a theme/idea and the AI fills in the rest."""

    pipeline_id = f"launch_{int(time.time())}"
    state = LaunchPipelineState(
        pipeline_id=pipeline_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        narrative=narrative,
        target_audience=target_audience,
    )

    prompt = f"""You are a memecoin branding expert. Create a token concept for pump.fun.

Theme/idea: {theme or "Come up with something trending and viral"}
Target audience: {target_audience}
Narrative context: {narrative or "Make it relevant to current crypto/meme culture"}

Respond in EXACTLY this JSON format (no markdown, no extra text):
{{
  "name": "Token Name (catchy, memorable, 2-4 words max)",
  "symbol": "TICKER (3-6 uppercase letters)",
  "description": "A compelling 1-2 sentence description for the pump.fun page that makes people want to buy",
  "logo_prompt": "A detailed image generation prompt for the token logo. Be specific about style, colors, composition. Should be iconic and work as a small circle avatar.",
  "narrative": "What trend/narrative is this riding and why it will go viral"
}}"""

    system = "You are a creative memecoin branding expert. Output only valid JSON."

    raw = _llm_call(prompt, system=system, max_tokens=512)

    try:
        # Strip markdown code fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
        if clean.startswith("json"):
            clean = clean[4:].strip()

        concept = json.loads(clean)
        state.token_name = concept.get("name", "")
        state.token_symbol = concept.get("symbol", "")
        state.token_description = concept.get("description", "")
        state.narrative = concept.get("narrative", narrative)
        state.stage = "design"

        # Store logo prompt for next stage
        state.logo_candidates = []
        state._logo_prompt = concept.get("logo_prompt", "")
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Ideation parse error: {e}\nRaw: {raw[:500]}")
        state.token_name = theme or "ParseError"
        state.token_description = raw[:300]

    state.save()
    logger.info(f"Stage 1 IDEATE complete: {state.token_name} ({state.token_symbol})")
    return state


# ─── Stage 2: DESIGN ─────────────────────────────────────────────────────────

def stage_design(
    state: LaunchPipelineState,
    logo_prompt: str = "",
    num_candidates: int = NUM_LOGO_CANDIDATES,
) -> LaunchPipelineState:
    """Generate candidate logo images for the token."""
    from repryntt.tools.media import generate_image

    brain_path = str(Path(__file__).resolve().parent.parent.parent / "config")

    if not logo_prompt:
        logo_prompt = getattr(state, "_logo_prompt", "")
    if not logo_prompt:
        logo_prompt = (
            f"Token logo for '{state.token_name}' ({state.token_symbol}). "
            f"Iconic, memorable, works as a small circle avatar on pump.fun. "
            f"Bold colors, simple design, no text in the image."
        )

    state.logo_candidates = []

    for i in range(num_candidates):
        variation = f"{logo_prompt} (variation {i+1} of {num_candidates}, make each distinct)"
        filename = f"launch_{state.pipeline_id}_logo_{i+1}.png"

        result_json = generate_image(brain_path, prompt=variation, filename=filename)
        result = json.loads(result_json)

        if result.get("success"):
            abs_path = result["absolute_path"]
            state.logo_candidates.append(abs_path)
            logger.info(f"  Logo candidate {i+1}: {abs_path}")
        else:
            logger.warning(f"  Logo candidate {i+1} failed: {result.get('error', 'unknown')}")

    if state.logo_candidates:
        state.stage = "review"
    else:
        logger.error("No logo candidates generated — cannot proceed")

    state.save()
    logger.info(f"Stage 2 DESIGN complete: {len(state.logo_candidates)} candidates")
    return state


# ─── Stage 3: REVIEW ─────────────────────────────────────────────────────────

def stage_review(state: LaunchPipelineState) -> LaunchPipelineState:
    """Use vision to analyze each logo candidate and pick the best one."""

    if not state.logo_candidates:
        logger.error("No logos to review")
        return state

    state.logo_reviews = []

    review_question = (
        f"This is a candidate logo for a memecoin called '{state.token_name}' "
        f"(ticker: {state.token_symbol}). Rate it on a scale of 1-10 for:\n"
        f"1. Visual appeal and memorability\n"
        f"2. How well it works as a small circular avatar\n"
        f"3. How well it conveys the token concept: {state.token_description}\n"
        f"4. Overall professionalism\n\n"
        f"Respond in JSON: {{\"appeal\": N, \"avatar_fit\": N, \"concept_match\": N, "
        f"\"professionalism\": N, \"total\": N, \"strengths\": \"...\", \"weaknesses\": \"...\"}}"
    )

    for i, logo_path in enumerate(state.logo_candidates):
        logger.info(f"  Reviewing candidate {i+1}: {logo_path}")
        analysis = _vision_call(logo_path, review_question)

        review = {
            "candidate": i + 1,
            "path": logo_path,
            "raw_analysis": analysis,
        }

        # Try to parse scores
        try:
            clean = analysis.strip()
            if "```" in clean:
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
                clean = clean.strip()
            scores = json.loads(clean)
            review["scores"] = scores
            review["total_score"] = scores.get("total", 0)
        except (json.JSONDecodeError, Exception):
            review["total_score"] = 5  # neutral default
            review["scores"] = {}

        state.logo_reviews.append(review)

    # Pick the best
    if state.logo_reviews:
        best = max(state.logo_reviews, key=lambda r: r.get("total_score", 0))
        state.selected_logo = best["path"]
        state.selection_reason = (
            f"Candidate {best['candidate']} scored highest ({best.get('total_score', '?')}/10). "
            f"{best.get('scores', {}).get('strengths', '')}"
        )
        state.stage = "prepare"
        logger.info(
            f"Stage 3 REVIEW complete: Selected candidate {best['candidate']} "
            f"(score: {best.get('total_score', '?')})"
        )

    state.save()
    return state


# ─── Stage 4: PREPARE ────────────────────────────────────────────────────────

def stage_prepare(state: LaunchPipelineState) -> LaunchPipelineState:
    """Upload metadata to IPFS and prepare for launch."""
    import aiohttp
    import asyncio
    from repryntt.trading.pumpfun_launcher import upload_metadata, TokenLaunchConfig

    if not state.selected_logo:
        logger.error("No logo selected — run review stage first")
        return state

    config = TokenLaunchConfig(
        name=state.token_name,
        symbol=state.token_symbol,
        description=state.token_description,
        image_path=state.selected_logo,
        initial_buy_sol=state.initial_buy_sol,
        twitter=state.twitter,
        telegram=state.telegram,
        website=state.website,
    )

    async def _upload():
        async with aiohttp.ClientSession() as session:
            return await upload_metadata(session, config)

    try:
        uri = asyncio.run(_upload())
        if uri:
            state.metadata_uri = uri
            state.stage = "launch"
            logger.info(f"Stage 4 PREPARE complete: metadata at {uri}")
        else:
            logger.error("Metadata upload failed")
    except Exception as e:
        logger.error(f"Prepare stage failed: {e}")

    state.save()
    return state


# ─── Stage 5: LAUNCH ─────────────────────────────────────────────────────────

def stage_launch(state: LaunchPipelineState) -> LaunchPipelineState:
    """Execute the token launch on pump.fun."""
    import asyncio
    from repryntt.trading.pumpfun_launcher import launch_token, TokenLaunchConfig, DRY_RUN

    state.dry_run = DRY_RUN

    config = TokenLaunchConfig(
        name=state.token_name,
        symbol=state.token_symbol,
        description=state.token_description,
        image_path=state.selected_logo,
        initial_buy_sol=state.initial_buy_sol,
        twitter=state.twitter,
        telegram=state.telegram,
        website=state.website,
    )

    try:
        result = asyncio.run(launch_token(config))
        state.mint_pubkey = result.mint_pubkey or ""
        state.tx_signature = result.tx_signature or ""
        state.launched = result.success
        state.stage = "complete"

        if result.success:
            logger.info(
                f"Stage 5 LAUNCH complete! "
                f"{'[DRY_RUN]' if DRY_RUN else '[LIVE]'} "
                f"Token: {state.token_name} ({state.token_symbol}) "
                f"Mint: {state.mint_pubkey}"
            )
        else:
            logger.error(f"Launch failed: {result.error}")
    except Exception as e:
        logger.error(f"Launch stage failed: {e}")

    state.save()
    return state


# ─── Full Pipeline ────────────────────────────────────────────────────────────

def run_full_pipeline(
    theme: str = "",
    narrative: str = "",
    initial_buy_sol: float = 0.1,
    twitter: str = "",
    telegram: str = "",
    website: str = "",
) -> Dict[str, Any]:
    """Run the complete token launch pipeline from ideation to launch.

    Returns a summary dict with results from each stage.
    """
    summary = {"stages": {}}

    # Stage 1: Ideate
    logger.info("=" * 60)
    logger.info("STAGE 1: IDEATE")
    state = stage_ideate(theme=theme, narrative=narrative)
    summary["stages"]["ideate"] = {
        "name": state.token_name,
        "symbol": state.token_symbol,
        "description": state.token_description,
    }

    if not state.token_name or state.stage != "design":
        summary["error"] = "Ideation failed"
        return summary

    state.initial_buy_sol = initial_buy_sol
    state.twitter = twitter
    state.telegram = telegram
    state.website = website

    # Stage 2: Design
    logger.info("=" * 60)
    logger.info("STAGE 2: DESIGN")
    state = stage_design(state)
    summary["stages"]["design"] = {
        "candidates": len(state.logo_candidates),
        "paths": state.logo_candidates,
    }

    if state.stage != "review":
        summary["error"] = "Design failed — no logos generated"
        return summary

    # Stage 3: Review
    logger.info("=" * 60)
    logger.info("STAGE 3: REVIEW")
    state = stage_review(state)
    summary["stages"]["review"] = {
        "selected": state.selected_logo,
        "reason": state.selection_reason,
        "reviews": [{
            "candidate": r["candidate"],
            "score": r.get("total_score", "?"),
        } for r in state.logo_reviews],
    }

    if state.stage != "prepare":
        summary["error"] = "Review failed — no logo selected"
        return summary

    # Stage 4: Prepare
    logger.info("=" * 60)
    logger.info("STAGE 4: PREPARE")
    state = stage_prepare(state)
    summary["stages"]["prepare"] = {
        "metadata_uri": state.metadata_uri,
    }

    if state.stage != "launch":
        summary["error"] = "Prepare failed — metadata upload failed"
        return summary

    # Stage 5: Launch
    logger.info("=" * 60)
    logger.info("STAGE 5: LAUNCH")
    state = stage_launch(state)
    summary["stages"]["launch"] = {
        "dry_run": state.dry_run,
        "success": state.launched,
        "mint_pubkey": state.mint_pubkey,
        "tx_signature": state.tx_signature,
    }

    summary["pipeline_id"] = state.pipeline_id
    summary["success"] = state.launched
    summary["token"] = {
        "name": state.token_name,
        "symbol": state.token_symbol,
        "mint": state.mint_pubkey,
    }

    logger.info("=" * 60)
    logger.info(f"PIPELINE COMPLETE: {state.token_name} ({state.token_symbol})")
    logger.info(f"  Success: {state.launched} | DRY_RUN: {state.dry_run}")
    logger.info(f"  Mint: {state.mint_pubkey}")

    return summary
