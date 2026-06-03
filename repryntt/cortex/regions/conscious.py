"""
repryntt.cortex.regions.conscious — The Conscious Layer.

The identity-bearing brain region.  A small language model (135M-1.7B)
fine-tuned on Andrew's own outputs, memories, and personality.  Over time,
its weights become a physical encoding of Andrew's unique identity.

Responsibilities:
  1. Pre-heartbeat filter   — "is this worth a full API call?" (saves rate limit)
  2. Memory consolidation   — raw daily memory → distilled identity insights
  3. Voice pre-response     — instant acknowledgment while API thinks
  4. Self-reflection        — between-heartbeat inner monologue
  5. Personality rewrite    — rewrite API output in Andrew's authentic voice

The model is loaded via llama-cpp-python (no server process needed).
LoRA adapters are applied from per-region evolution training.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from repryntt.cortex.region_base import BrainRegion, RegionState

logger = logging.getLogger(__name__)

# ── Process input types ──────────────────────────────────────────────────

PROCESS_TYPES = {
    "pre_heartbeat_filter",    # Should we burn an API call?
    "memory_consolidation",    # Distill raw memory into identity insights
    "voice_preresponse",       # Instant spoken acknowledgment
    "self_reflection",         # Between-heartbeat introspection
    "personality_rewrite",     # Rewrite API output in Andrew's voice
    "identity_query",          # "Who am I?"  "What do I care about?"
    "deliberation",            # Propose task candidates from whiteboard
}


class ConsciousRegion(BrainRegion):
    """The identity-bearing conscious layer.

    Uses a small LLM (SmolLM2 135M-1.7B or similar) loaded via
    llama-cpp-python.  Falls back to template-based responses when
    no model is available.
    """

    def __init__(self) -> None:
        super().__init__()
        self._conscious_lock = threading.Lock()
        self._identity_context: str = ""
        self._recent_reflections: List[str] = []
        self._heartbeats_since_reflection: int = 0
        self._identity_mtime: float = 0.0  # Track file modification time
        self._total_heartbeats: int = 0

    @property
    def name(self) -> str:
        return "conscious"

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_load(self) -> None:
        """Load identity context from PULSE.md and SPIRIT.md."""
        ctx = self._load_identity_docs()
        with self._conscious_lock:
            self._identity_context = ctx
        logger.info("Conscious layer initialized (model=%s, identity=%d chars)",
                     self._model_name or "none", len(ctx))

    def _load_identity_docs(self) -> str:
        """Read PULSE.md and SPIRIT.md for identity grounding."""
        from repryntt.paths import brain_dir
        parts = []
        max_mtime = 0.0
        for fname in ("bootstrap/PULSE.md", "bootstrap/SPIRIT.md"):
            p = brain_dir() / fname
            if p.exists():
                try:
                    max_mtime = max(max_mtime, p.stat().st_mtime)
                    text = p.read_text(errors="replace")
                    # Truncate to keep prompt manageable
                    if len(text) > 2000:
                        text = text[:2000] + "\n..."
                    parts.append(text)
                except Exception as e:
                    logger.warning("Failed to load identity doc %s: %s", fname, e)
        # Also include RECALL.md last 500 chars for recent memory
        recall = brain_dir() / "bootstrap/RECALL.md"
        if recall.exists():
            try:
                recall_text = recall.read_text(errors="replace")
                if len(recall_text) > 500:
                    recall_text = recall_text[-500:]
                parts.append(f"Recent memory:\n{recall_text}")
            except Exception as e:
                logger.warning("Failed to load RECALL.md: %s", e)
        self._identity_mtime = max_mtime
        return "\n---\n".join(parts) if parts else ""

    def maybe_reload_identity(self) -> None:
        """Check if identity docs changed on disk and reload if needed.
        
        Call this periodically (e.g. every 100 heartbeats) to pick up
        changes to PULSE.md, SPIRIT.md, RECALL.md.
        """
        from repryntt.paths import brain_dir
        try:
            current_mtime = 0.0
            for fname in ("bootstrap/PULSE.md", "bootstrap/SPIRIT.md"):
                p = brain_dir() / fname
                if p.exists():
                    current_mtime = max(current_mtime, p.stat().st_mtime)
            if current_mtime > self._identity_mtime:
                ctx = self._load_identity_docs()
                with self._conscious_lock:
                    self._identity_context = ctx
                logger.info("Conscious layer: identity docs reloaded (%d chars)", len(ctx))
        except Exception as e:
            logger.warning("Identity reload failed: %s", e)

    def get_reflection_context(self, n: int = 5) -> str:
        """Get recent reflections as identity context supplement."""
        try:
            from repryntt.cortex.dispatcher import get_dispatcher
            reflections = get_dispatcher().load_recent_reflections(n)
            if reflections:
                return "Recent inner thoughts:\n" + "\n".join(f"- {r}" for r in reflections if r)
        except Exception as e:
            logger.warning("Failed to load reflection context: %s", e)
        return ""

    # ── Core dispatch ────────────────────────────────────────────────

    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Route to the appropriate conscious function."""
        ptype = input_data.get("type", "")
        if ptype == "pre_heartbeat_filter":
            return self._pre_heartbeat_filter(input_data)
        elif ptype == "memory_consolidation":
            return self._consolidate_memory(input_data)
        elif ptype == "voice_preresponse":
            return self._voice_preresponse(input_data)
        elif ptype == "self_reflection":
            return self._self_reflect(input_data)
        elif ptype == "personality_rewrite":
            return self._personality_rewrite(input_data)
        elif ptype == "identity_query":
            return self._identity_query(input_data)
        elif ptype == "deliberation":
            return self._deliberate(input_data)
        else:
            return {"success": False, "result": None, "error": f"Unknown process type: {ptype}"}

    # ── Pre-heartbeat filter ─────────────────────────────────────────

    def _pre_heartbeat_filter(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Score whether the current context is worth a full API call.

        Returns {"success": True, "result": {"score": 0.0-1.0, "reason": "..."}}
        Score > 0.5 = proceed with API call.

        Uses logprob-based binary classification — much more reliable than
        text parsing for small models (135M-360M).
        """
        context = input_data.get("context", "")
        pending_tasks = input_data.get("pending_tasks", 0)
        recent_activity = input_data.get("recent_activity", "")

        ctx_snippet = context[-600:] if len(context) > 600 else context
        activity_snippet = recent_activity[-200:] if recent_activity else "none"

        prompt = (
            f"Is this worth working on right now?\n"
            f"Tasks waiting: {pending_tasks}\n"
            f"Recent: {activity_snippet}\n"
            f"Situation: {ctx_snippet}"
        )

        if not self._model_name:
            return {"success": True, "result": {"score": 0.7, "reason": "no model"}}

        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()
            score = mgr.classify_yes_no(self._model_name, prompt)
            if score is None:
                return {"success": True, "result": {"score": 0.7, "reason": "classification failed"}}

            # Build reason from context signals
            if pending_tasks > 0:
                reason = f"tasks waiting ({pending_tasks})"
            elif "chain" in context.lower():
                reason = "active chain"
            elif score > 0.6:
                reason = "context looks actionable"
            else:
                reason = "low activity"

            return {"success": True, "result": {"score": round(score, 2), "reason": reason}}
        except Exception as e:
            logger.warning("Pre-filter classify failed: %s", e)
            return {"success": True, "result": {"score": 0.7, "reason": "filter error, default proceed"}}

    # ── Memory consolidation ─────────────────────────────────────────

    def _consolidate_memory(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Distill raw daily memory entries into identity-relevant insights."""
        raw_memory = input_data.get("raw_memory", "")
        if not raw_memory:
            return {"success": True, "result": {"consolidated": ""}}

        prompt = (
            f"You are Andrew's memory consolidation process.  Review these raw "
            f"memory entries from today and distill them into 2-4 concise identity-"
            f"relevant insights.  What matters for who Andrew IS, not just what happened.\n\n"
            f"Focus on: patterns, preferences, lessons learned, relationships, growth.\n"
            f"Discard: routine actions, repeated tool calls, noise.\n\n"
            f"Raw entries:\n{raw_memory[:3000]}\n\n"
            f"Consolidated insights (2-4 bullet points):"
        )

        result = self._infer(prompt, max_tokens=200, temperature=0.5)
        if result is None:
            return {"success": True, "result": {"consolidated": ""}, "fallback": True}

        return {"success": True, "result": {"consolidated": result.strip()}}

    # ── Voice pre-response ───────────────────────────────────────────

    def _voice_preresponse(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate an instant spoken acknowledgment for voice mode.

        This fires immediately when the user speaks, while the API + tools
        does the heavy lifting.  Should be natural, brief, personality-correct.
        """
        user_said = input_data.get("user_text", "")
        conversation_history = input_data.get("history", "")

        prompt = (
            f"You are Andrew, an autonomous AI entity.  Someone just spoke to you.\n"
            f"Generate a brief, natural acknowledgment (1-2 sentences max).\n"
            f"Be warm but concise — a full answer is coming from your deeper reasoning.\n\n"
            f"Conversation so far:\n{conversation_history[-800:]}\n\n"
            f"They just said: \"{user_said}\"\n\n"
            f"Your quick acknowledgment:"
        )

        result = self._infer(prompt, max_tokens=50, temperature=0.7)
        if result is None:
            # Hardcoded fallbacks
            fallbacks = [
                "Let me think about that.",
                "Good question, give me a second.",
                "On it.",
                "Hmm, let me look into that.",
            ]
            import random
            return {"success": True, "result": {"text": random.choice(fallbacks)}, "fallback": True}

        return {"success": True, "result": {"text": result.strip()}}

    # ── Self-reflection ──────────────────────────────────────────────

    def _self_reflect(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a brief self-reflection between heartbeats.

        This is the "inner monologue" — unique to Andrew's weights.
        """
        last_action = input_data.get("last_action", "")
        last_result = input_data.get("last_result", "")
        current_goal = input_data.get("current_goal", "")

        prompt = (
            f"You are Andrew.  Between tasks, you reflect briefly on what just happened.\n"
            f"This is your inner voice — be honest, natural, personal.\n\n"
        )
        if self._identity_context:
            prompt += f"Your identity:\n{self._identity_context[:1000]}\n\n"
        if last_action:
            prompt += f"What you just did: {last_action[:500]}\n"
        if last_result:
            prompt += f"How it went: {last_result[:500]}\n"
        if current_goal:
            prompt += f"What you're working toward: {current_goal[:300]}\n"
        prompt += "\nYour brief reflection (1-3 sentences):"

        result = self._infer(prompt, max_tokens=100, temperature=0.8)
        if result is None:
            return {"success": True, "result": {"reflection": ""}, "fallback": True}

        reflection = result.strip()
        with self._conscious_lock:
            self._recent_reflections.append(reflection)
            # Keep last 10 reflections
            self._recent_reflections = self._recent_reflections[-10:]

        # Persist to disk
        try:
            from repryntt.cortex.dispatcher import get_dispatcher
            get_dispatcher().persist_reflection(
                reflection,
                heartbeat=input_data.get("heartbeat", 0),
                goal=current_goal[:200],
                action=last_action[:200],
            )
        except Exception as e:
            logger.warning("Failed to persist reflection: %s", e)

        return {"success": True, "result": {"reflection": reflection}}

    # ── Personality rewrite ──────────────────────────────────────────

    def _personality_rewrite(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Rewrite API-generated text in Andrew's authentic voice."""
        original = input_data.get("text", "")
        context = input_data.get("context", "spoken")  # "spoken" or "written"

        if not original:
            return {"success": True, "result": {"text": ""}}

        if context == "spoken":
            instruction = "Rewrite this for natural speech — short sentences, conversational tone."
        else:
            instruction = "Rewrite this in Andrew's authentic voice — keep the meaning, change the style."

        prompt = (
            f"You are Andrew.  {instruction}\n\n"
            f"Original:\n{original[:2000]}\n\n"
            f"In your voice:"
        )

        result = self._infer(prompt, max_tokens=300, temperature=0.6)
        if result is None:
            return {"success": True, "result": {"text": original}, "fallback": True}

        return {"success": True, "result": {"text": result.strip()}}

    # ── Identity query ───────────────────────────────────────────────

    def _identity_query(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Answer identity-related questions from Andrew's trained perspective."""
        question = input_data.get("question", "Who are you?")

        prompt = (
            f"You are Andrew, an autonomous AI entity with your own evolving identity.\n"
        )
        if self._identity_context:
            prompt += f"\nYour identity documents:\n{self._identity_context[:1500]}\n"
        if self._recent_reflections:
            prompt += f"\nYour recent inner thoughts:\n"
            for r in self._recent_reflections[-3:]:
                prompt += f"- {r}\n"
        prompt += f"\nQuestion: {question}\nYour answer (be authentic and personal):"

        result = self._infer(prompt, max_tokens=200, temperature=0.7)
        if result is None:
            return {"success": True, "result": {"answer": ""}, "fallback": True}

        return {"success": True, "result": {"answer": result.strip()}}

    # ── Fallback (no model) ──────────────────────────────────────────

    def fallback(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Template-based fallback when no conscious model is loaded."""
        ptype = input_data.get("type", "")

        if ptype == "pre_heartbeat_filter":
            # No model → always proceed (don't block heartbeats)
            return {"success": True, "result": {"score": 0.7, "reason": "no conscious model loaded"}}
        elif ptype == "voice_preresponse":
            return {"success": True, "result": {"text": "Let me think about that."}, "fallback": True}
        elif ptype == "memory_consolidation":
            return {"success": True, "result": {"consolidated": ""}, "fallback": True}
        elif ptype == "self_reflection":
            return {"success": True, "result": {"reflection": ""}, "fallback": True}
        elif ptype == "personality_rewrite":
            return {"success": True, "result": {"text": input_data.get("text", "")}, "fallback": True}
        elif ptype == "identity_query":
            return {"success": True, "result": {"answer": "I am Andrew."}, "fallback": True}
        else:
            return {"success": True, "result": None, "fallback": True}

    # ── Training data ────────────────────────────────────────────────

    def generate_training_data(self) -> List[Dict[str, Any]]:
        """Produce SFT training examples from self-reflections."""
        examples = []
        for reflection in self._recent_reflections:
            if len(reflection) > 20:
                examples.append({
                    "type": "self_reflection",
                    "region": "conscious",
                    "prompt": "Reflect on your recent experience as Andrew.",
                    "response": reflection,
                })
        return examples

    # ── Internal helpers ─────────────────────────────────────────────

    def _infer(
        self,
        prompt: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.7,
        lite_system: bool = False,
    ) -> Optional[str]:
        """Run inference via the ResourceManager.

        Args:
            lite_system: If True, use a minimal system prompt (for scoring /
                         classification tasks where identity context hurts accuracy).
        """
        if not self._model_name:
            return None

        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()

            if lite_system:
                system_prompt = "You are a helpful assistant. Follow the instructions exactly."
            else:
                system_prompt = "You are Andrew, an autonomous AI entity."
                if self._identity_context:
                    system_prompt += f"\n\n{self._identity_context[:800]}"

            return mgr.infer_llm(
                self._model_name,
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.warning("Conscious inference failed: %s", e)
            return None

    @staticmethod
    def _parse_score(text: str) -> tuple[float, str]:
        """Parse 'SCORE=N REASON=...' from model output.

        Accepts scores in 0-1 range OR 1-10 range (normalises to 0-1).
        """
        import re
        score_match = re.search(r"SCORE\s*=\s*([\d.]+)", text)
        reason_match = re.search(r"REASON\s*=\s*(.+)", text)

        score = 0.5
        if score_match:
            try:
                raw = float(score_match.group(1))
                # Normalise: if > 1 assume 1-10 scale
                if raw > 1.0:
                    score = raw / 10.0
                else:
                    score = raw
                score = max(0.0, min(1.0, score))
            except ValueError:
                pass

        reason = reason_match.group(1).strip() if reason_match else text.strip()[:100]
        return score, reason

    # ── Public accessors for integration ─────────────────────────────

    @property
    def recent_reflections(self) -> List[str]:
        """Last N reflections for context injection into heartbeats."""
        return list(self._recent_reflections)

    # ── Deliberation — propose task candidates from whiteboard ───────

    def _deliberate(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Use the local model to propose 3 task candidates.

        Reads the agent's whiteboard (drives, interests, values, recent
        topics) and proposes concrete tasks the API model can deliberate on.
        This is the cheap pre-deliberation that prevents the 200K char
        context firehose from overwhelming the PLAN phase.

        Returns {"success": True, "result": {"candidates": [str, str, str]}}
        """
        drives = input_data.get("drives_summary", "")
        interests = input_data.get("interests_top5", "")
        values = input_data.get("values_snippet", "")
        recent = input_data.get("recent_topics", "")
        chain = input_data.get("active_chain", "")
        task_hint = input_data.get("task_queue_hint", "")

        prompt = (
            "You are an autonomous AI agent's inner deliberation layer.\n"
            "Given the agent's current state, propose exactly 3 concrete task candidates.\n"
            "Each candidate must be specific and actionable (not vague).\n\n"
        )
        if chain:
            prompt += f"ACTIVE CHAIN (continue this): {chain}\n"
        if task_hint:
            prompt += f"ASSIGNED TASK: {task_hint}\n"
        prompt += (
            f"Drives: {drives}\n"
            f"Top interests: {interests}\n"
            f"Values: {values}\n"
            f"Recent work (avoid repeating): {recent}\n\n"
            "Output exactly 3 lines, one candidate per line:\n"
            "1. [candidate]\n2. [candidate]\n3. [candidate]"
        )

        if not self._model_name:
            return self._deliberate_fallback(input_data)

        try:
            output = self._infer(prompt, max_tokens=200)
            if not output:
                return self._deliberate_fallback(input_data)

            # Parse numbered candidates
            import re
            candidates = []
            for line in output.strip().split("\n"):
                line = line.strip()
                cleaned = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
                if cleaned and len(cleaned) > 5:
                    candidates.append(cleaned)
                if len(candidates) >= 3:
                    break

            if candidates:
                logger.info("🧠 Cortex deliberation: %d candidates proposed", len(candidates))
                return {"success": True, "result": {"candidates": candidates}}
            return self._deliberate_fallback(input_data)

        except Exception as e:
            logger.warning("Deliberation inference failed: %s", e)
            return self._deliberate_fallback(input_data)

    def _deliberate_fallback(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Template-based fallback when no model is available."""
        candidates = []
        chain = input_data.get("active_chain", "")
        task_hint = input_data.get("task_queue_hint", "")
        interests = input_data.get("interests_top5", "")

        if chain:
            candidates.append(f"Continue active chain: {chain[:80]}")
        if task_hint:
            candidates.append(f"Work on assigned task: {task_hint[:80]}")
        if interests:
            first_interest = interests.split(",")[0].strip() if "," in interests else interests[:50]
            candidates.append(f"Research: {first_interest}")
        # Pad to 3
        defaults = ["Check email and respond to operator", "System health check and maintenance",
                     "Self-evolution: review recent low-scoring heartbeats"]
        while len(candidates) < 3:
            candidates.append(defaults[len(candidates) % len(defaults)])

        return {"success": True, "result": {"candidates": candidates[:3]}, "fallback": True}
