"""
repryntt.paid_features.coherence — Coherence Cloud (Pro tree: local).

Coherence Cloud is the *upgrade* path on top of the free local critic
infrastructure that already ships in OSS as ``repryntt.agents.critic_gate``.

  Free tier (OSS, no API key)
    → ``critic_gate.critic_gate(...)`` runs locally against the operator's
      configured LLM. Catches obvious failures using whatever model the
      user has set up. Continues to work as today.

  Paid tier (Coherence Cloud, with REPRYNTT_API_KEY)
    → ``paid_features.coherence.critic_judge(...)`` routes the call to
      api.repryntt.com which runs a frontier-model critic against the
      same artifact. Catches subtler failures the user's own model can't
      detect against itself.

This file is the Pro variant — when the operator runs the private dev
tree they reach the local critic infrastructure directly (using whatever
``critic_provider`` they have configured in ai_config.json, typically a
frontier model). The OSS distribution has a divergent HTTPS-client
version of this file.

Public surface (kept in lock-step with the OSS variant):
    critic_judge(daemon, artifact_path, task, doubt_block, round_n=1)
        → ``{pass, concerns, round, escalate, ...}``
    judge_architecture(project, provider_info, config=None)
        → ``{ok, concerns, reasoning}``
    coherence_eval(task, artifact_root=None, model_scores=None, model_details=None)
        → ``CoherenceVerdict``
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("repryntt.paid_features.coherence")


def critic_judge(daemon: Any, artifact_path: str, task: Dict[str, Any],
                 doubt_block: str = "", round_n: int = 1) -> Dict[str, Any]:
    """Run the critic gate. Pro tree uses local critic_gate with whatever
    critic_provider the operator has configured."""
    from repryntt.agents.critic_gate import critic_gate as _local
    return _local(daemon, artifact_path, task, doubt_block, round_n)


def judge_architecture(project: Any, provider_info: Dict[str, str],
                       config: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Architecture sanity-check judge for forge projects. Pro tree
    delegates to the local generator's implementation."""
    from repryntt.codeforge.generator import judge_architecture as _local
    return _local(project, provider_info, config=config)


def coherence_eval(task: Any, artifact_root: Optional[str] = None,
                   model_scores: Optional[Dict[str, int]] = None,
                   model_details: Optional[Dict[str, str]] = None) -> Any:
    """Deterministic 5-axis coherence verdict for an artifact. Pro tree
    runs the local evaluate_artifact (free in OSS, identical here)."""
    from repryntt.agents.coherence_eval import evaluate_artifact
    return evaluate_artifact(task, artifact_root=artifact_root,
                              model_scores=model_scores,
                              model_details=model_details)
