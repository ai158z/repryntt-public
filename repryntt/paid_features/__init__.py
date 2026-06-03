"""
repryntt.paid_features — Local-first routers for premium hosted features.

Each module in this package exposes the same public surface that the
underlying premium feature would, but routes via a three-step resolver:

  1. Try a local implementation (operator running the full "Pro" tree
     with the implementation modules present locally).
  2. Try a hosted HTTPS endpoint at api.repryntt.com using a configured
     ``REPRYNTT_API_KEY``.
  3. Return a paywall response pointing the user at the signup URL.

The behavior difference between the "Pro" install and the public OSS
install comes entirely from which files are *present* — there are no
``if private`` branches in the code itself. The Pro install ships with
the local implementation modules; the OSS install does not.

Modules:
  forge      — CodeForge (build projects, propose, status, benchmark, judge)
  video      — Video Production pipeline (multi-clip / production-grade video)
  coherence  — Coherence Cloud (frontier-model critic/judge upgrade over the free local critic_gate)

Public surface (re-exported here for convenience):
  from repryntt.paid_features import forge, video, coherence
"""
from __future__ import annotations

from . import _http      # noqa: F401  (imported for side-effect-free utility load)
from . import forge      # noqa: F401
from . import video      # noqa: F401
from . import coherence  # noqa: F401

__all__ = ["forge", "video", "coherence"]
