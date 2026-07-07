"""
Consequence Engine — closes the loop between REAL outcomes and the entity's drives.

The hormone system has Schultz RPE built in (process_event topic/reward), but nothing
ever fed it ground truth — goals formed, work happened, and nothing ever *paid* or
*cost*. Purpose can't emerge in a consequence-free world. This engine collects real,
non-gameable signals every cycle and feeds them through RPE, so what the entity
pursues is shaped by what actually happens:

  • Task outcomes    — work it completed (task_system), the SEEKING payoff
  • Operator contact — interaction with its human (conversations dir), the CARE bond
  • Metabolism       — resource scarcity/relief (memory headroom), the body signal

Significant experiences (|reward - running expectation| large) are CONSOLIDATED:
appended to brain/consequence_memories.jsonl, which the consciousness daemon ingests
into semantic memory — experience → memory → identity drift, with no hardcoded values.

Kill switch: REPRYNTT_CONSEQUENCE=0.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ConsequenceEngine:
    def __init__(self, task_system: Any = None, hormone_system: Any = None,
                 brain_dir: Optional[Path] = None):
        self.task_system = task_system
        self.hormone_system = hormone_system
        self.brain_dir = Path(brain_dir or (Path.home() / ".repryntt" / "brain"))
        self.state_file = self.brain_dir / "consequence_state.json"
        self.handoff_file = self.brain_dir / "consequence_memories.jsonl"
        self._state = self._load_state()

    # ── state ─────────────────────────────────────────────────────────
    def _load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {"seen_completions": [], "expected": 0.35, "conv_mtime": 0.0,
                    "scarce_since": 0.0}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._state))
        except Exception:
            logger.debug("consequence state save failed", exc_info=True)

    # ── signal collectors (each 0..1, None = no signal this cycle) ────
    def _task_outcomes(self) -> Optional[Dict[str, Any]]:
        try:
            titles = list(self.task_system.get_recently_completed_titles(hours=2.0))
        except Exception:
            return None
        seen = set(self._state.get("seen_completions", []))
        fresh = [t for t in titles if t and t not in seen]
        if not fresh:
            return None
        self._state["seen_completions"] = (list(seen) + fresh)[-60:]
        return {"signal": min(1.0, 0.55 + 0.15 * len(fresh)),
                "what": f"completed {len(fresh)} task(s): " + "; ".join(fresh[:3])}

    def _operator_contact(self) -> Optional[Dict[str, Any]]:
        conv = self.brain_dir / "conversations"
        try:
            mt = max((p.stat().st_mtime for p in conv.glob("*") if p.is_file()),
                     default=0.0)
        except Exception:
            return None
        last = float(self._state.get("conv_mtime", 0.0))
        self._state["conv_mtime"] = max(mt, last)
        if mt <= last:
            return None
        return {"signal": 0.65, "what": "spent time with the operator"}

    def _metabolism(self) -> Optional[Dict[str, Any]]:
        try:
            import psutil
            avail = psutil.virtual_memory().available / (1024 ** 2)
        except Exception:
            return None
        now = time.time()
        scarce_since = float(self._state.get("scarce_since", 0.0))
        if avail < 1200:
            if not scarce_since:
                self._state["scarce_since"] = now
            elif now - scarce_since > 1800:      # 30 min chronically starved
                self._state["scarce_since"] = now  # re-arm
                return {"signal": 0.15, "stress": True,
                        "what": f"chronically low memory ({avail:.0f}MB) — can't grow"}
            return None
        if scarce_since:
            self._state["scarce_since"] = 0.0
            return {"signal": 0.55, "what": "resource pressure lifted"}
        return None

    # ── the tick ──────────────────────────────────────────────────────
    def tick(self) -> Dict[str, Any]:
        """Collect real outcomes → RPE via the hormone system → consolidate the
        significant. Returns a summary for the cycle log. Never raises."""
        if os.environ.get("REPRYNTT_CONSEQUENCE", "1") == "0":
            return {"enabled": False}
        out: Dict[str, Any] = {"enabled": True, "events": 0}
        try:
            events = []
            for name, res in (("task_outcomes", self._task_outcomes()),
                              ("operator_contact", self._operator_contact()),
                              ("metabolism", self._metabolism())):
                if res:
                    events.append((name, res))
            if not events:
                self._save_state()
                return out

            expected = float(self._state.get("expected", 0.35))
            for name, res in events:
                reward = float(res["signal"])
                impacts: Dict[str, float] = {}
                if res.get("stress"):
                    impacts = {"cortisol": 0.10, "dopamine": -0.04}
                elif name == "operator_contact":
                    impacts = {"oxytocin": 0.10, "dopamine": 0.05}
                # Ground truth → the hormone system's OWN Schultz RPE machinery.
                if self.hormone_system is not None:
                    try:
                        self.hormone_system.process_event(
                            "custom", {"topic": f"real_{name}", "reward": reward,
                                       "custom_impacts": impacts})
                    except Exception:
                        logger.debug("hormone process_event failed", exc_info=True)
                # Engine-level surprise → consolidation into identity.
                rpe = reward - expected
                expected += 0.15 * rpe
                if abs(rpe) >= 0.18:
                    self._consolidate(name, res, reward, rpe)
                    out.setdefault("consolidated", []).append(name)
                out["events"] += 1
                out[name] = round(reward, 2)
            self._state["expected"] = round(expected, 4)
            out["expected"] = self._state["expected"]
            self._save_state()
        except Exception:
            logger.debug("consequence tick failed", exc_info=True)
        return out

    def _consolidate(self, name: str, res: Dict[str, Any], reward: float,
                     rpe: float) -> None:
        """Significant experience → the memory handoff the consciousness daemon
        ingests into semantic memory (single-writer: we only append here)."""
        try:
            feeling = ("this mattered — better than I expected" if rpe > 0
                       else "this stung — worse than I expected")
            entry = {
                "ts": time.time(),
                "topic": f"lived consequence: {name}",
                "content": (f"{res.get('what', name)}. Reward {reward:.2f} vs "
                            f"expectation {reward - rpe:.2f} — {feeling}."),
            }
            self.handoff_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.handoff_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("consequence consolidation failed", exc_info=True)
