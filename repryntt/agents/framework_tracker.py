"""
Framework Tracker — Procedural Activity Framework System
=========================================================
Manages structured multi-step frameworks for complex activities.
Each framework defines a sequence of steps with required data.
State persists across heartbeats so the agent can resume where it left off.

Frameworks:
  - new_trade:       7-step procedure for buying a token
  - position_review: 4-step procedure for daily portfolio review
  - sell_decision:   4-step procedure for sell/exit decisions
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("repryntt.framework")

# ── Framework Definitions ─────────────────────────────────────────────
# Each framework defines its steps in order. Steps have:
#   name:     step identifier
#   label:    human-readable description
#   required: keys that must be present in the data to advance past this step

FRAMEWORKS = {
    "new_trade": {
        "label": "New Trade",
        "steps": [
            {"name": "identify",           "label": "Identify Candidate",
             "required": ["token", "address", "source"]},
            {"name": "research_narrative",  "label": "Research Narrative",
             "required": ["narrative", "narrative_strength", "searches_done"]},
            {"name": "verify_onchain",      "label": "Verify On-Chain",
             "required": ["holder_concentration", "lp_status", "mcap"]},
            {"name": "check_social",        "label": "Check Social Proof",
             "required": ["social_sentiment"]},
            {"name": "form_thesis",         "label": "Form Thesis",
             "required": ["conviction", "position_size_usd", "thesis_summary"]},
            {"name": "execute",             "label": "Execute Trade",
             "required": ["executed"]},
            {"name": "journal",             "label": "Journal Entry",
             "required": ["journaled"]},
        ],
    },
    "position_review": {
        "label": "Position Review",
        "steps": [
            {"name": "check_positions",  "label": "Check Positions",
             "required": ["position_count", "total_value"]},
            {"name": "verify_thesis",    "label": "Verify Each Thesis",
             "required": ["positions_reviewed"]},
            {"name": "decide_and_act",   "label": "Decide & Act",
             "required": []},
            {"name": "journal",          "label": "Journal Review",
             "required": ["journaled"]},
        ],
    },
    "sell_decision": {
        "label": "Sell Decision",
        "steps": [
            {"name": "assess_trigger",     "label": "Assess Trigger",
             "required": ["token", "trigger"]},
            {"name": "verify_state",       "label": "Verify Current State",
             "required": ["assessment"]},
            {"name": "execute_decision",   "label": "Execute Decision",
             "required": ["action"]},
            {"name": "post_mortem",        "label": "Post-Mortem",
             "required": ["lesson"]},
        ],
    },
}


FRAMEWORK_EXPIRY_MINUTES = 30  # Auto-expire stuck frameworks after 30 minutes


class FrameworkTracker:
    """Manages active framework instances with state persistence."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.state_path = os.path.join(workspace_dir, "framework_state.json")
        self.state = self._load_state()
        self._auto_expire_stale()
        self._auto_close_orphaned_sells()

    def _load_state(self) -> Dict:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load framework state: {e}")
        return {"active": None, "history": []}

    def _auto_expire_stale(self):
        """Auto-expire frameworks stuck longer than FRAMEWORK_EXPIRY_MINUTES."""
        active = self.state.get("active")
        if not active:
            return
        started = active.get("started_at", "")
        if not started:
            return
        try:
            start_time = datetime.fromisoformat(started)
            elapsed = (datetime.now() - start_time).total_seconds() / 60
            if elapsed > FRAMEWORK_EXPIRY_MINUTES:
                fw_name = active.get("framework", "unknown")
                step = active.get("current_step", 0)
                total = active.get("total_steps", 0)
                logger.info(
                    f"Auto-expiring stale framework '{fw_name}' "
                    f"(step {step}/{total}, {elapsed:.0f}m old)"
                )
                active["status"] = "expired"
                active["ended_at"] = datetime.now().isoformat()
                self.state.setdefault("history", []).append(active)
                self.state["active"] = None
                self._save_state()
        except (ValueError, TypeError):
            pass

    def _get_portfolio_positions(self) -> Dict:
        """Load current sim portfolio positions. Returns {} on failure."""
        try:
            portfolio_path = os.path.join(self.workspace_dir, "sim_portfolio.json")
            if os.path.exists(portfolio_path):
                with open(portfolio_path, "r") as f:
                    return json.load(f).get("positions", {})
        except Exception:
            pass
        return {}

    def _auto_close_orphaned_sells(self):
        """Auto-close sell_decision frameworks whose position no longer exists."""
        active = self.state.get("active")
        if not active or active.get("framework") != "sell_decision":
            return
        token = active.get("step_data", {}).get("assess_trigger", {}).get("token", "")
        if not token:
            return
        positions = self._get_portfolio_positions()
        if token not in positions:
            logger.info(
                f"Auto-closing sell_decision for '{token}' — "
                f"position no longer in portfolio (already sold or closed)"
            )
            active["status"] = "auto_closed_no_position"
            active["ended_at"] = datetime.now().isoformat()
            self.state.setdefault("history", []).append(active)
            self.state["active"] = None
            self._save_state()

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"Failed to save framework state: {e}")

    # ── Public API ────────────────────────────────────────────────────

    def start(self, framework_name: str, initial_data: Dict = None) -> str:
        """Start a new framework instance.

        Parameters:
            framework_name: One of: new_trade, position_review, sell_decision
            initial_data: Initial data for the first step (e.g. token address, source)

        Returns:
            Status message.
        """
        framework_name = framework_name.lower().strip()
        if framework_name not in FRAMEWORKS:
            return (f"❌ Unknown framework '{framework_name}'. "
                    f"Available: {', '.join(FRAMEWORKS.keys())}")

        # sell_decision: reject if position doesn't exist in portfolio
        if framework_name == "sell_decision" and initial_data:
            token = initial_data.get("token", "")
            if token:
                positions = self._get_portfolio_positions()
                if token not in positions:
                    return (
                        f"❌ Cannot start sell_decision for '{token}' — "
                        f"no position found in portfolio. "
                        f"Position was likely already sold or auto-closed."
                    )

        # If there's already an active framework, warn but allow override
        if self.state.get("active"):
            old = self.state["active"]
            old_name = old.get("framework", "unknown")
            old_step = old.get("current_step", 0)
            old_def = FRAMEWORKS.get(old_name, {})
            old_total = len(old_def.get("steps", []))
            # Archive the abandoned framework
            old["status"] = "abandoned"
            old["ended_at"] = datetime.now().isoformat()
            self.state.setdefault("history", []).append(old)
            logger.info(f"Abandoned framework {old_name} at step {old_step}/{old_total}")

        defn = FRAMEWORKS[framework_name]
        steps = defn["steps"]

        instance = {
            "framework": framework_name,
            "label": defn["label"],
            "current_step": 0,
            "total_steps": len(steps),
            "started_at": datetime.now().isoformat(),
            "step_data": {},
            "status": "active",
        }

        # Record initial data for step 0
        if initial_data:
            step_name = steps[0]["name"]
            instance["step_data"][step_name] = initial_data
            # Check if step 0 requirements are met — if so, auto-advance
            required = set(steps[0].get("required", []))
            provided = set(initial_data.keys())
            if required.issubset(provided):
                instance["current_step"] = 1

        self.state["active"] = instance
        self._save_state()

        step_idx = instance["current_step"]
        if step_idx < len(steps):
            next_step = steps[step_idx]
            return (f"✅ Started **{defn['label']}** framework "
                    f"({len(steps)} steps).\n"
                    f"Current step: **{step_idx + 1}/{len(steps)} — "
                    f"{next_step['label']}**\n"
                    f"Required data: {', '.join(next_step.get('required', ['none']))}")
        else:
            return f"✅ Started and completed **{defn['label']}** framework."

    def advance(self, step_data: Dict = None) -> str:
        """Advance the active framework to the next step.

        Parameters:
            step_data: Data collected at the current step. Must include required keys.

        Returns:
            Status message with next step info or completion message.
        """
        active = self.state.get("active")
        if not active:
            return "❌ No active framework. Use framework_start() to begin one."

        framework_name = active["framework"]
        defn = FRAMEWORKS.get(framework_name)
        if not defn:
            return f"❌ Framework definition '{framework_name}' not found."

        steps = defn["steps"]
        current_idx = active["current_step"]

        if current_idx >= len(steps):
            return "✅ Framework already complete. Start a new one with framework_start()."

        current_step = steps[current_idx]
        step_name = current_step["name"]

        # Merge new data with any existing data for this step
        existing = active["step_data"].get(step_name, {})
        if step_data:
            existing.update(step_data)
        active["step_data"][step_name] = existing

        # Check required keys
        required = set(current_step.get("required", []))
        provided = set(existing.keys())
        missing = required - provided
        if missing:
            self._save_state()
            return (f"⚠️ Step {current_idx + 1}/{len(steps)} "
                    f"**{current_step['label']}** — missing data: "
                    f"{', '.join(sorted(missing))}.\n"
                    f"Provide the missing data with framework_advance().")

        # Advance to next step
        active["current_step"] = current_idx + 1

        if active["current_step"] >= len(steps):
            # Framework complete!
            active["status"] = "completed"
            active["ended_at"] = datetime.now().isoformat()
            self.state.setdefault("history", []).append(active)
            self.state["active"] = None
            self._save_state()

            # Build completion summary
            token = active["step_data"].get("identify", {}).get("token", "")
            or_token = active["step_data"].get("assess_trigger", {}).get("token", "")
            label = token or or_token or defn["label"]
            return (f"🏁 **{defn['label']}** framework COMPLETE for {label}!\n"
                    f"Steps completed: {len(steps)}/{len(steps)}.\n"
                    f"This structured approach is now part of your learning history.")

        # Show next step
        next_step = steps[active["current_step"]]
        self._save_state()
        return (f"✅ Step {current_idx + 1}/{len(steps)} "
                f"**{current_step['label']}** — complete.\n"
                f"Next: **{active['current_step'] + 1}/{len(steps)} — "
                f"{next_step['label']}**\n"
                f"Required data: {', '.join(next_step.get('required', ['none']))}")

    def status(self) -> str:
        """Get current framework status.

        Returns:
            Status of active framework (if any) and recent history.
        """
        lines = []

        active = self.state.get("active")
        if active:
            framework_name = active["framework"]
            defn = FRAMEWORKS.get(framework_name, {})
            steps = defn.get("steps", [])
            current_idx = active["current_step"]

            lines.append(f"**Active Framework: {defn.get('label', framework_name)}**")
            lines.append(f"Progress: step {current_idx + 1}/{len(steps)}")
            lines.append(f"Started: {active.get('started_at', 'unknown')}")

            # Show completed steps
            for i, step in enumerate(steps):
                if i < current_idx:
                    data = active["step_data"].get(step["name"], {})
                    summary = ", ".join(f"{k}={v}" for k, v in list(data.items())[:3])
                    lines.append(f"  ✅ {i+1}. {step['label']}: {summary}")
                elif i == current_idx:
                    lines.append(f"  🔄 {i+1}. {step['label']} ← YOU ARE HERE")
                    required = step.get("required", [])
                    if required:
                        lines.append(f"     Required: {', '.join(required)}")
                else:
                    lines.append(f"  ⬜ {i+1}. {step['label']}")
        else:
            lines.append("No active framework.")

        # Show recent history (last 5)
        history = self.state.get("history", [])
        if history:
            lines.append(f"\n**Recent history** (last 5 of {len(history)}):")
            for entry in history[-5:]:
                name = FRAMEWORKS.get(entry.get("framework"), {}).get("label", entry.get("framework", "?"))
                status = entry.get("status", "?")
                token = entry.get("step_data", {}).get("identify", {}).get("token", "")
                or_token = entry.get("step_data", {}).get("assess_trigger", {}).get("token", "")
                label = token or or_token or ""
                ended = entry.get("ended_at", "?")[:16]
                lines.append(f"  {'✅' if status == 'completed' else '❌'} {name}"
                             f"{' — ' + label if label else ''} [{status}] {ended}")

        return "\n".join(lines)

    def get_heartbeat_injection(self) -> str:
        """Get a compact string for injection into the heartbeat prompt.

        Returns empty string if no active framework.
        Only returns content when there's something the agent needs to continue.
        """
        active = self.state.get("active")
        if not active:
            return ""

        framework_name = active["framework"]
        defn = FRAMEWORKS.get(framework_name, {})
        steps = defn.get("steps", [])
        current_idx = active["current_step"]

        if current_idx >= len(steps):
            return ""

        # sell_decision: verify position still exists before re-injecting
        if framework_name == "sell_decision":
            token = active.get("step_data", {}).get("assess_trigger", {}).get("token", "")
            if token and token not in self._get_portfolio_positions():
                logger.info(
                    f"Heartbeat: auto-closing sell_decision for '{token}' — "
                    f"position no longer in portfolio"
                )
                active["status"] = "auto_closed_no_position"
                active["ended_at"] = datetime.now().isoformat()
                self.state.setdefault("history", []).append(active)
                self.state["active"] = None
                self._save_state()
                return ""

        current_step = steps[current_idx]
        token = active["step_data"].get("identify", {}).get("token", "")
        or_token = active["step_data"].get("assess_trigger", {}).get("token", "")
        token_label = token or or_token or ""

        # Build compact status line
        parts = [
            f"⚙️ **ACTIVE FRAMEWORK: {defn.get('label', framework_name)}**"
            f"{' for $' + token_label if token_label else ''}",
            f"Step {current_idx + 1}/{len(steps)}: **{current_step['label']}**",
        ]

        # Show what data is needed
        required = current_step.get("required", [])
        if required:
            parts.append(f"Required: {', '.join(required)}")

        # Show key data collected so far (compact)
        collected = []
        for step_name, data in active["step_data"].items():
            for k, v in data.items():
                if k in ("token", "narrative", "conviction", "trigger", "assessment"):
                    collected.append(f"{k}={v}")
        if collected:
            parts.append(f"Collected: {', '.join(collected[:6])}")

        parts.append("Use `framework_advance({...})` with the required data to proceed.")
        return "\n".join(parts)

    def check_trade_research(self, token_address: str) -> Optional[Dict]:
        """Check if a NEW_TRADE framework was completed for a given token.

        Used by sim_buy soft gate to verify research was done.

        Returns:
            Dict with research data if framework was completed, None otherwise.
        """
        # Check active framework
        active = self.state.get("active")
        if active and active.get("framework") == "new_trade":
            addr = active.get("step_data", {}).get("identify", {}).get("address", "")
            if addr and addr.lower() == token_address.lower():
                step = active.get("current_step", 0)
                if step >= 5:  # Past FORM_THESIS
                    return active.get("step_data", {})

        # Check recent history (last 20)
        for entry in reversed(self.state.get("history", [])[-20:]):
            if entry.get("framework") != "new_trade":
                continue
            if entry.get("status") != "completed":
                continue
            addr = entry.get("step_data", {}).get("identify", {}).get("address", "")
            if addr and addr.lower() == token_address.lower():
                return entry.get("step_data", {})

        return None


# ── Module-level singleton ────────────────────────────────────────────
_tracker: Optional[FrameworkTracker] = None


def get_tracker(workspace_dir: str = None) -> FrameworkTracker:
    """Get or create the framework tracker singleton."""
    global _tracker
    if _tracker is None:
        if workspace_dir is None:
            # Default to operator workspace
            workspace_dir = str(Path.home() / ".repryntt" / "workspace" / "agents" / "operator")
        _tracker = FrameworkTracker(workspace_dir)
    return _tracker
