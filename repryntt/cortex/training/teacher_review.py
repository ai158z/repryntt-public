"""
Nightly Teacher Review — the frontier mind grades the small mind's day.

The live executive escalations (~15/day) produce a trickle of gold; this produces
the river. Once per night, a single batched frontier call reviews the day's LOCAL
decisions (the reflex brain's actual outputs, sampled from the training collector):

  grade ≥ 4 → the local answer was frontier-grade; it becomes a positive example
  grade ≤ 3 → the teacher writes the correction, producing BOTH:
                • a high-quality training example (the correction)
                • a DPO preference pair (chosen=correction, rejected=local answer)

So every mediocre local decision becomes preference-learning signal — exactly the
labels sleep-time DPO needs, grounded in frontier judgment instead of heuristics.
Spend: 1-2 batched calls/night on the executive provider (own tiny budget, separate
from the live 15/day). Kill switch: REPRYNTT_TEACHER_REVIEW=0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BRAIN_DIR = Path.home() / ".repryntt" / "brain"
STATE_FILE = BRAIN_DIR / "teacher_review_state.json"
MAX_ITEMS_PER_NIGHT = 24        # decisions reviewed per night
MAX_CALLS_PER_NIGHT = 2


def due() -> bool:
    """Once per UTC day, only when enabled."""
    if os.environ.get("REPRYNTT_TEACHER_REVIEW", "1") == "0":
        return False
    try:
        st = json.loads(STATE_FILE.read_text())
        return st.get("last_date") != date.today().isoformat()
    except Exception:
        return True


def _mark_done(summary: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(
            {"last_date": date.today().isoformat(), "summary": summary}))
    except Exception:
        logger.debug("teacher review state save failed", exc_info=True)


def _todays_local_decisions() -> List[Dict[str, Any]]:
    """The day's reflex-brain outputs from the collector (never the executive's
    own answers — the teacher must grade the STUDENT, not itself)."""
    try:
        from repryntt.cortex.training.data_router import get_data_router
        ds = get_data_router().get_dataset("conscious")
    except Exception:
        return []
    # Rolling 24h window (the review runs just after midnight — a calendar-day
    # filter would review an empty day). Timestamps appear BOTH as epoch floats
    # (our tap) and ISO strings (the collector) — parse either.
    cutoff = time.time() - 24 * 3600
    out = []
    for e in ds:
        try:
            if e.get("type") in ("executive_distillation", "teacher_correction"):
                continue
            raw_ts = e.get("timestamp") or 0
            if isinstance(raw_ts, str):
                ts = datetime.fromisoformat(raw_ts).timestamp()
            else:
                ts = float(raw_ts)
            if ts >= cutoff and len(str(e.get("response", ""))) > 60:
                out.append(e)
        except Exception:
            continue
    return out[-MAX_ITEMS_PER_NIGHT:]


def _executive_provider() -> Optional[Dict[str, Any]]:
    """The frontier teacher = the configured executive brain (needs its own key)."""
    try:
        cfg_path = Path(os.environ.get(
            "REPRYNTT_AI_CONFIG", str(BRAIN_DIR / "ai_config.json")))
        cfg = json.loads(cfg_path.read_text()).get("ai_provider", {})
        # TIER STACK: the TEACHER seat may outrank the live executive — teaching
        # quality compounds into the weights forever, so the best model belongs
        # here (e.g. teacher=Fable, live executive=Opus, reflex=nvidia, self=cortex).
        prov = (cfg.get("teacher_provider") or cfg.get("executive_provider") or "").strip()
        model = (cfg.get("teacher_model") or cfg.get("executive_model") or "").strip()
        section = cfg.get(prov) or {}
        if not prov or not model or not section.get("api_key"):
            return None
        return {"provider": prov, "model": model, **section, "model": model}
    except Exception:
        return None


def _call_teacher(provider: Dict[str, Any], prompt: str) -> str:
    """One frontier call via the provider router. Returns text ('' on failure)."""
    try:
        from repryntt.routing.provider_router import route_ai_call
        config = {"provider": provider["provider"], provider["provider"]: provider}
        r = route_ai_call(config, prompt, {"max_tokens": 3000, "temperature": 0.2})
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        logger.debug("teacher call failed", exc_info=True)
        return ""


def run() -> Dict[str, Any]:
    """The night's review. Returns a summary dict; never raises."""
    summary: Dict[str, Any] = {"reviewed": 0, "kept": 0, "corrected": 0, "pairs": 0}
    try:
        items = _todays_local_decisions()
        if not items:
            summary["note"] = "no local decisions to review today"
            _mark_done(summary)
            return summary
        provider = _executive_provider()
        if not provider:
            summary["note"] = "no executive provider configured"
            return summary

        from repryntt.cortex.training.data_router import get_data_router
        router = get_data_router()
        batch_size = max(1, (len(items) + MAX_CALLS_PER_NIGHT - 1) // MAX_CALLS_PER_NIGHT)
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            numbered = "\n\n".join(
                f"### {i+1}\nSITUATION:\n{str(e.get('prompt',''))[-1200:]}\n"
                f"STUDENT'S RESPONSE:\n{str(e.get('response',''))[:900]}"
                for i, e in enumerate(batch))
            prompt = (
                "You are the overnight TEACHER for a small on-device AI that runs an "
                "autonomous entity. Below are its real decisions from today. Grade each "
                "1-5 (5 = exactly what a frontier mind would do; deduct for invented "
                "facts, claimed-but-not-done actions, ignoring context, rambling). For "
                "any grade <= 3, write the CORRECTED response — what the student SHOULD "
                "have said, same voice, grounded only in the given situation.\n"
                "Reply ONLY a JSON array: "
                '[{"i": <number>, "grade": <1-5>, "correction": "<empty if grade>=4>"}]'
                f"\n\n{numbered}")
            raw = _call_teacher(provider, prompt)
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                continue
            try:
                verdicts = json.loads(m.group(0))
            except Exception:
                continue
            for v in verdicts:
                try:
                    idx = int(v.get("i", 0)) - 1
                    if not (0 <= idx < len(batch)):
                        continue
                    e = batch[idx]
                    grade = int(v.get("grade", 0))
                    summary["reviewed"] += 1
                    corr = str(v.get("correction") or "").strip()
                    if grade >= 4 or not corr:
                        summary["kept"] += 1
                        continue
                    summary["corrected"] += 1
                    router.route({
                        "region": "conscious", "type": "teacher_correction",
                        "prompt": str(e.get("prompt", ""))[-6000:],
                        "response": corr[:4000], "quality": 5,
                        "timestamp": time.time()})
                    if router.route_preference_pair({
                            "region": "conscious", "type": "teacher_preference",
                            "prompt": str(e.get("prompt", ""))[-6000:],
                            "chosen": corr[:4000],
                            "rejected": str(e.get("response", ""))[:4000]}):
                        summary["pairs"] += 1
                except Exception:
                    continue
        _mark_done(summary)
        logger.info(f"🎓 Teacher review: {summary}")
    except Exception:
        logger.exception("teacher review failed")
    return summary
