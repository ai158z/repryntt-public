"""
repryntt.paid_features.forge — CodeForge (Pro tree: local implementation).

The Pro tree includes the full ``repryntt.codeforge`` package locally and
this module is a thin re-export over it. The operator running this tree
has unlimited local access — no API key required, no HTTPS round-trip.

The OSS distribution's ``paid_features/forge.py`` has the SAME public
surface but a completely different implementation (HTTPS client to
api.repryntt.com). The two files are deliberately divergent so the OSS
release ships zero local-fallback logic that could be mistaken for a
paywall bypass vector. See MIRROR_EXCLUDE.md for the never-mirror list.

Public surface (kept in lock-step with the OSS variant):
    propose_project(description, **kw)
    get_proposals(**kw)
    get_proposal(proposal_id, **kw)
    approve_proposal(proposal_id, **kw)
    reject_proposal(proposal_id, reason="", **kw)
    start_approved_project(proposal_id, **kw)
    forge_status(project_id="", **kw)
    forge_benchmark(provider="", **kw)
    judge_architecture(project, provider_info, config=None)

Tool aliases (back-compat with registry.py):
    forge_project = propose_project
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("repryntt.paid_features.forge")


# ── Proposal lifecycle ───────────────────────────────────────────────


def propose_project(description: str = "",
                     provider: str = "",
                     model: str = "",
                     **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import propose_project as _local
    return _local(description=description, provider=provider, model=model, **kw)


def get_proposals(**kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import get_proposals as _local
    return _local(**kw)


def get_proposal(proposal_id: str, **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import get_proposal as _local
    return _local(proposal_id, **kw)


def approve_proposal(proposal_id: str, **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import approve_proposal as _local
    return _local(proposal_id, **kw)


def reject_proposal(proposal_id: str, reason: str = "", **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import reject_proposal as _local
    return _local(proposal_id, reason=reason, **kw)


def start_approved_project(proposal_id: str, **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.governance import start_approved_project as _local
    return _local(proposal_id, **kw)


# ── Build status / benchmark ─────────────────────────────────────────


def forge_status(project_id: str = "", **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.forge import get_forge
    forge = get_forge()
    if project_id:
        detail = forge.get_project_detail(project_id) or {}
        return {"success": bool(detail), **detail}
    return {"success": True, "projects": forge.list_projects()}


def forge_benchmark(provider: str = "", **kw: Any) -> Dict[str, Any]:
    from repryntt.codeforge.benchmark import (
        run_benchmark, get_cached_benchmark, save_benchmark,
    )
    cached = get_cached_benchmark(provider) if provider else None
    if cached:
        return {"success": True, "benchmark": cached, "cached": True}

    from repryntt.llm import _load_ai_config, _resolve_provider, _call_llm
    config = _load_ai_config()
    pinfo = _resolve_provider(config, provider)
    def _call(prompt: str) -> str:
        return _call_llm([{"role": "user", "content": prompt}],
                         pinfo, max_tokens=2000, temperature=0.2) or ""
    result = run_benchmark(_call, node_id="local",
                           model_name=pinfo.get("model", "unknown"),
                           provider=pinfo.get("provider", "unknown"))
    save_benchmark(result)
    return {"success": True, **result.to_dict()}


# ── Architecture judge ───────────────────────────────────────────────


def judge_architecture(project: Any, provider_info: Dict[str, str],
                       config: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    from repryntt.codeforge.generator import judge_architecture as _local
    return _local(project, provider_info, config=config)


# ── Back-compat alias ────────────────────────────────────────────────

forge_project = propose_project
