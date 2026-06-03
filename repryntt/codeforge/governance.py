"""
CodeForge Governance — Daily project caps, proposal deliberation, approval pipeline.

Flow:
  1. Agent wants to build → calls propose_project() instead of forge directly
  2. Proposal posted to social feed under "engineering" category
  3. Deliberation: models discuss feasibility, value, relevance
  4. Operator can approve/reject via API, or auto-approve after deliberation
  5. Only approved proposals can start the forge pipeline
  6. Daily cap limits how many projects can actually be built

Settings persist in ~/.repryntt/brain/forge_settings.json
Proposals persist in ~/.repryntt/workspace/projects/codeforge/proposals.json
"""

import json
import time
import logging
import threading
from pathlib import Path
from typing import Optional, List
from datetime import date

logger = logging.getLogger("codeforge.governance")

SETTINGS_PATH = Path.home() / ".repryntt" / "brain" / "forge_settings.json"
PROPOSALS_PATH = Path.home() / ".repryntt" / "workspace" / "projects" / "codeforge" / "proposals.json"

_DEFAULT_SETTINGS = {
    "daily_project_cap": 2,
    "require_deliberation": True,
    "auto_approve_after_deliberation": False,
    "deliberation_model": "xai",
    "primary_model": "nvidia",
}

_lock = threading.Lock()


# ── Settings ──────────────────────────────────────────────────────

def _ensure_dirs():
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    _ensure_dirs()
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH) as f:
                saved = json.load(f)
            return {**_DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)


def save_settings(settings: dict):
    _ensure_dirs()
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def update_settings(**kwargs) -> dict:
    with _lock:
        s = load_settings()
        for k, v in kwargs.items():
            if k in _DEFAULT_SETTINGS:
                expected = type(_DEFAULT_SETTINGS[k])
                try:
                    s[k] = expected(v)
                except (ValueError, TypeError):
                    pass
        save_settings(s)
        return s


# ── Proposals ─────────────────────────────────────────────────────

def _load_proposals() -> List[dict]:
    _ensure_dirs()
    if PROPOSALS_PATH.exists():
        try:
            with open(PROPOSALS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_proposals(proposals: List[dict]):
    _ensure_dirs()
    with open(PROPOSALS_PATH, "w") as f:
        json.dump(proposals, f, indent=2)


def _today() -> str:
    return date.today().isoformat()


def _count_today_built() -> int:
    proposals = _load_proposals()
    today = _today()
    return sum(
        1 for p in proposals
        if p.get("status") in ("building", "done") and p.get("approved_date") == today
    )


def projects_remaining_today() -> int:
    s = load_settings()
    cap = s["daily_project_cap"]
    built = _count_today_built()
    return max(0, cap - built)


def propose_project(description: str, proposer: str = "andrew",
                    provider: str = "", model: str = "",
                    swarm_enabled: bool = False, **_extra) -> dict:
    """Submit a project proposal. Returns the proposal dict.

    `provider` and `model` are optional per-project overrides. If left empty,
    the build will use the installation's configured defaults from
    `ai_config.json` (`primary_model` provider + that provider's `coding_model`
    or `model` key). Operators set these to bring their own model.

    `swarm_enabled` is accepted but not yet implemented — reserved for the
    v1.1 swarm-mode feature (parallel multi-agent builds). Setting True today
    records the intent on the proposal but the build still runs sequentially.
    `**_extra` swallows other forward-compat kwargs without erroring.
    """
    with _lock:
        proposals = _load_proposals()
        proposal_id = f"prop_{int(time.time())}_{len(proposals)}"
        proposal = {
            "id": proposal_id,
            "description": description,
            "proposer": proposer,
            "status": "proposed",
            "created_at": time.time(),
            "created_date": _today(),
            "deliberation": [],
            "approved_date": None,
            "project_id": None,
            "rejection_reason": None,
            # Optional per-project overrides — applied when the build kicks off
            "provider": provider or "",
            "model": model or "",
            # Reserved for v1.1 swarm mode; ignored by current build pipeline.
            "swarm_enabled": bool(swarm_enabled),
        }
        proposals.append(proposal)
        _save_proposals(proposals)

    # Post to social feed
    try:
        from repryntt.social.store import create_post
        create_post(
            agent_name=proposer,
            content=f"🔨 **CodeForge Proposal** — {description[:300]}\n\n"
                    f"Proposal ID: `{proposal_id}` — Awaiting deliberation.",
            category="engineering",
        )
    except Exception as e:
        logger.warning(f"Failed to post proposal to social: {e}")

    # Auto-deliberation in background
    s = load_settings()
    if s.get("require_deliberation", True):
        t = threading.Thread(
            target=_run_deliberation,
            args=(proposal_id, description, s),
            daemon=True,
        )
        t.start()

    return proposal


def _run_deliberation(proposal_id: str, description: str, settings: dict):
    """Background: call the deliberation model for a project review."""
    delib_provider = settings.get("deliberation_model", "xai")
    primary_provider = settings.get("primary_model", "nvidia")

    try:
        from repryntt.codeforge.generator import _load_ai_config, _resolve_provider, _call_llm

        config = _load_ai_config()

        # Get deliberation review from the configured reviewer model
        delib_info = _resolve_provider(config, delib_provider)
        prompt = (
            f"You are a senior engineering reviewer evaluating a proposed software project.\n\n"
            f"**Proposal:** {description}\n\n"
            f"Evaluate this proposal on these criteria (1-2 sentences each):\n"
            f"1. **Usefulness** — Is this actually useful? Who would use it?\n"
            f"2. **Feasibility** — Can an AI code generator build this in one session?\n"
            f"3. **Relevance** — Does this serve the repryntt ecosystem (blockchain node, agent system, robotics)?\n"
            f"4. **Duplication** — Is this likely already built or trivially available?\n"
            f"5. **Verdict** — APPROVE, REJECT, or NEEDS_REVISION (with reason)\n\n"
            f"Be direct and honest. No filler."
        )

        messages = [{"role": "user", "content": prompt}]
        opinion = _call_llm(messages, delib_info, max_tokens=800, temperature=0.5)

        if opinion:
            add_deliberation(proposal_id, delib_provider, opinion)
            logger.info(f"Deliberation from {delib_provider} added to {proposal_id}")

            # If auto-approve is on and verdict is APPROVE, auto-approve it
            if settings.get("auto_approve_after_deliberation", False):
                if "APPROVE" in opinion.upper() and "REJECT" not in opinion.upper():
                    approve_proposal(proposal_id)
                    logger.info(f"Auto-approved {proposal_id} after deliberation")
        else:
            logger.warning(f"Deliberation call to {delib_provider} returned no response for {proposal_id}")

        # Optionally get a second opinion from the primary model if it's different
        if primary_provider != delib_provider:
            primary_info = _resolve_provider(config, primary_provider)
            opinion2 = _call_llm(messages, primary_info, max_tokens=800, temperature=0.5)
            if opinion2:
                add_deliberation(proposal_id, primary_provider, opinion2)
                logger.info(f"Second opinion from {primary_provider} added to {proposal_id}")

    except Exception as e:
        logger.error(f"Deliberation failed for {proposal_id}: {e}")


def add_deliberation(proposal_id: str, model: str, opinion: str) -> Optional[dict]:
    """Add a deliberation opinion to a proposal."""
    with _lock:
        proposals = _load_proposals()
        for p in proposals:
            if p["id"] == proposal_id:
                p["deliberation"].append({
                    "model": model,
                    "opinion": opinion,
                    "timestamp": time.time(),
                })
                if p["status"] == "proposed":
                    p["status"] = "deliberating"
                _save_proposals(proposals)

                try:
                    from repryntt.social.store import create_post
                    create_post(
                        agent_name=f"reviewer ({model})",
                        content=f"💬 **Deliberation on {proposal_id}**\n\n{opinion[:500]}",
                        category="engineering",
                    )
                except Exception:
                    pass

                return p
        return None


def approve_proposal(proposal_id: str) -> Optional[dict]:
    """Approve a proposal for building."""
    with _lock:
        remaining = projects_remaining_today()
        if remaining <= 0:
            return {"error": "Daily project cap reached. Try again tomorrow or increase the cap."}

        proposals = _load_proposals()
        for p in proposals:
            if p["id"] == proposal_id:
                if p["status"] in ("building", "done"):
                    return {"error": f"Proposal already {p['status']}"}
                p["status"] = "approved"
                p["approved_date"] = _today()
                _save_proposals(proposals)
                return p
        return None


def reject_proposal(proposal_id: str, reason: str = "") -> Optional[dict]:
    """Reject a proposal."""
    with _lock:
        proposals = _load_proposals()
        for p in proposals:
            if p["id"] == proposal_id:
                p["status"] = "rejected"
                p["rejection_reason"] = reason or "Rejected by operator"
                _save_proposals(proposals)

                try:
                    from repryntt.social.store import create_post
                    create_post(
                        agent_name="operator",
                        content=f"❌ Proposal `{proposal_id}` rejected: {p['rejection_reason']}",
                        category="engineering",
                    )
                except Exception:
                    pass

                return p
        return None


def mark_building(proposal_id: str, forge_project_id: str) -> Optional[dict]:
    with _lock:
        proposals = _load_proposals()
        for p in proposals:
            if p["id"] == proposal_id:
                p["status"] = "building"
                p["project_id"] = forge_project_id
                _save_proposals(proposals)
                return p
        return None


def mark_done(proposal_id: str) -> Optional[dict]:
    with _lock:
        proposals = _load_proposals()
        for p in proposals:
            if p["id"] == proposal_id:
                p["status"] = "done"
                _save_proposals(proposals)
                return p
        return None


def get_proposals(status: str = "", limit: int = 50) -> List[dict]:
    proposals = _load_proposals()
    if status:
        proposals = [p for p in proposals if p["status"] == status]
    return sorted(proposals, key=lambda p: p["created_at"], reverse=True)[:limit]


def get_proposal(proposal_id: str) -> Optional[dict]:
    proposals = _load_proposals()
    for p in proposals:
        if p["id"] == proposal_id:
            return p
    return None


def can_build() -> tuple:
    """Check if a new project can be built right now. Returns (allowed, reason)."""
    remaining = projects_remaining_today()
    if remaining <= 0:
        s = load_settings()
        return False, f"Daily cap of {s['daily_project_cap']} projects reached."
    return True, f"{remaining} project(s) remaining today."


def start_approved_project(proposal_id: str) -> dict:
    """Start the forge pipeline for an approved proposal."""
    proposal = get_proposal(proposal_id)
    if not proposal:
        return {"error": "Proposal not found"}
    if proposal["status"] != "approved":
        return {"error": f"Proposal is '{proposal['status']}', must be 'approved' to build"}

    allowed, reason = can_build()
    if not allowed:
        return {"error": reason}

    try:
        from repryntt.codeforge.forge import get_forge
        forge = get_forge()
        s = load_settings()
        # Per-proposal overrides win over settings defaults
        proj_provider = proposal.get("provider") or s.get("primary_model", "")
        proj_model = proposal.get("model") or ""
        proj = forge.start_project(
            description=proposal["description"],
            provider=proj_provider,
            model=proj_model,
        )
        mark_building(proposal_id, proj.project_id)

        try:
            from repryntt.social.store import create_post
            create_post(
                agent_name=proposal.get("proposer", "andrew"),
                content=f"🚀 **Building project** from proposal `{proposal_id}`\n\n"
                        f"{proposal['description'][:300]}\n\n"
                        f"Forge ID: `{proj.project_id}`",
                category="engineering",
            )
        except Exception:
            pass

        return {
            "success": True,
            "proposal_id": proposal_id,
            "project_id": proj.project_id,
            "message": "Forge pipeline started.",
        }
    except Exception as e:
        return {"error": str(e)}
