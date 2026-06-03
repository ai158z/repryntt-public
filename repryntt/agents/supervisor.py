"""Supervisor pattern — twice-per-heartbeat orchestrator wrapper.

Architecture: cheap workhorse model (NVIDIA Mistral) does the routine work
inside JARVIS's autonomous heartbeat loop; frontier model (Claude Opus via
`orchestration_provider` in ai_config.json) acts as the HEAD LEAD —
reading the agent's bootstrap md files (PULSE, SPIRIT, RECALL,
CAPABILITIES) plus recent daemon log activity to ground its planning in
what JARVIS is ACTUALLY doing right now, then giving JARVIS direction
that augments (not contradicts) the autonomous agenda.

The key insight: the orchestrator must not plan in a vacuum. JARVIS has
real-time drives, a current focus in PULSE.md, blockers, recent
completions, and ongoing actions. If Opus plans without that context, it
will produce plans that fight the autonomous system (e.g. telling JARVIS
to stay idle while PULSE.md says to ship the nav prototype). With the
context, Opus becomes the senior agent that reads the situation and gives
direction the worker can follow.

  plan_for_heartbeat(context_summary) → str|None
    Called RIGHT AFTER _hb_start (line 14790). Internally:
      1. Reads PULSE.md, SPIRIT.md (excerpts), RECALL.md (head),
         CAPABILITIES.md, and the last ~80 daemon-log lines.
      2. Bundles them into a head-lead briefing.
      3. Returns Opus's plan grounded in that context.

  verify_heartbeat(plan, report) → str|None
    Called RIGHT BEFORE HEARTBEAT_END (line 16703). Compares JARVIS's
    actual work to the plan AND to the current PULSE.md state.

Both functions route through `_orchestrator_call` which honors
`orchestration_provider` in `ai_config.json`. Errors are swallowed —
failures fall through and JARVIS proceeds unaided, same as before this
module existed.

Cost (rough): with `orchestration_provider=anthropic` and Opus 4.6 pricing
($5/M in, $25/M out), and ~8-12K input + 1K output per call:
  per heartbeat: 2 × ($0.05 + $0.025) ≈ $0.15
  per hour at 120s interval (30 ticks):  ≈ $4.50
  per hour at 60s interval  (60 ticks):  ≈ $9.00
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger("repryntt.agents.supervisor")

# Truncation caps on what we hand to Opus per call. Keeps Anthropic
# bills predictable even if a heartbeat report or PULSE.md gets huge.
_MAX_CONTEXT_CHARS = 8_000     # caller-supplied daemon context
_MAX_REPORT_CHARS  = 12_000    # JARVIS's heartbeat report
_MAX_PULSE_CHARS   = 6_000     # the FULL PULSE.md working state
_MAX_SPIRIT_CHARS  = 4_000     # SPIRIT.md head (identity / mission)
_MAX_RECALL_CHARS  = 4_000     # RECALL.md head (long-term wisdom)
_MAX_CAPS_CHARS    = 3_000     # CAPABILITIES.md head
_LOG_TAIL_LINES    = 80         # recent daemon-log lines for "what's been happening"


# ─── Bootstrap context — Opus reads these so it knows what JARVIS is doing ──


def _brain_dir() -> str:
    return os.path.join(
        os.environ.get("REPRYNTT_HOME", os.path.expanduser("~/.repryntt")),
        "brain",
    )


def _read_capped(path: str, cap_chars: int) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        return text[:cap_chars]
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.debug(f"supervisor: read {path} failed: {e}")
        return ""


def _tail_log(path: str, n_lines: int) -> str:
    try:
        # Cheap tail without loading the whole file
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()
        return "\n".join(lines[-n_lines:])
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.debug(f"supervisor: tail {path} failed: {e}")
        return ""


def _bootstrap_briefing() -> str:
    """Assemble the head-lead briefing: who JARVIS is, what state it's
    actually in right now, and what's been happening in the daemon. This
    is what Opus reads to ground its planning. If files are missing
    (fresh install), corresponding sections are simply omitted."""
    base = _brain_dir()
    boot = os.path.join(base, "bootstrap")
    pulse = _read_capped(os.path.join(boot, "PULSE.md"), _MAX_PULSE_CHARS)
    spirit = _read_capped(os.path.join(boot, "SPIRIT.md"), _MAX_SPIRIT_CHARS)
    recall = _read_capped(os.path.join(boot, "RECALL.md"), _MAX_RECALL_CHARS)
    caps = _read_capped(os.path.join(boot, "CAPABILITIES.md"), _MAX_CAPS_CHARS)

    log_path = os.environ.get(
        "REPRYNTT_AGENT_DAEMON_LOG",
        os.path.expanduser("~/.repryntt/logs/agent-daemon.log"),
    )
    recent_log = _tail_log(log_path, _LOG_TAIL_LINES)

    parts: List[str] = []
    parts.append("═══ JARVIS bootstrap state — read this BEFORE planning ═══")
    if pulse:
        parts.append("─── PULSE.md (current working state — authoritative live state) ───")
        parts.append(pulse)
    if spirit:
        parts.append("─── SPIRIT.md (identity / mission / values) ───")
        parts.append(spirit)
    if recall:
        parts.append("─── RECALL.md (long-term wisdom worth carrying) ───")
        parts.append(recall)
    if caps:
        parts.append("─── CAPABILITIES.md (what JARVIS can actually do) ───")
        parts.append(caps)
    if recent_log:
        parts.append("─── Recent daemon log (last ~80 lines — what's been happening) ───")
        parts.append(recent_log)
    parts.append("═════════════════════════════════════════════════════════")
    return "\n\n".join(parts)


# ─── Provider dispatch (uses orchestration_provider per ai_config.json) ───


def _ai_config() -> dict:
    """Load ai_config.json from the running brain. Resolution mirrors
    the rest of the daemon (REPRYNTT_HOME → ~/.repryntt/brain)."""
    base = os.environ.get(
        "REPRYNTT_BRAIN_DIR",
        os.path.join(os.environ.get("REPRYNTT_HOME", os.path.expanduser("~/.repryntt")), "brain"),
    )
    cfg_path = os.path.join(base, "ai_config.json")
    try:
        with open(cfg_path) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.debug(f"supervisor: ai_config.json not at {cfg_path}; skipping")
        return {}
    except Exception as e:
        logger.warning(f"supervisor: failed to load ai_config.json: {e}")
        return {}


def _orchestrator_call(system_prompt: str, user_prompt: str, *,
                       max_tokens: int = 1200, temperature: float = 0.6) -> Optional[str]:
    """One LLM call routed through orchestration_provider. Returns the
    text response, or None on any failure. Never raises."""
    cfg = _ai_config()
    ai = cfg.get("ai_provider") or cfg
    provider = ai.get("orchestration_provider", ai.get("provider", "nvidia"))
    pcfg = (ai.get(provider) or {})
    api_key = pcfg.get("api_key") or os.environ.get(f"{provider.upper()}_API_KEY")
    endpoint = pcfg.get("endpoint")
    model = pcfg.get("model")
    if not (api_key and endpoint and model):
        logger.debug(f"supervisor: orchestration provider {provider!r} not fully configured "
                     f"(key={bool(api_key)} endpoint={bool(endpoint)} model={bool(model)})")
        return None

    try:
        import urllib.request
        import urllib.error

        if "anthropic.com" in endpoint:
            # Native Anthropic Messages API
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        else:
            # OpenAI-compatible (NVIDIA NIM, xAI, OpenAI, Google's compat endpoint)
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        dt_ms = int((time.time() - t0) * 1000)

        if "anthropic.com" in endpoint:
            text = "".join(
                b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
            )
        else:
            text = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""

        text = (text or "").strip()
        if text:
            logger.info(f"🧠 Orchestrator ({provider}/{model}) replied in {dt_ms}ms "
                        f"({len(text)} chars)")
        return text or None

    except Exception as e:
        logger.warning(f"supervisor: orchestrator call failed via {provider}: {e}")
        return None


# ─── Public API: plan + verify ────────────────────────────────────────


_PLAN_SYSTEM = (
    "You are JARVIS's HEAD LEAD — the senior orchestrator for an autonomous AI "
    "agent. JARVIS executes on a smaller model (NVIDIA Mistral-Small) and has "
    "REAL autonomous drives + a live PULSE.md state. Your job is NOT to command "
    "JARVIS in a vacuum. Your job is to:\n\n"
    "  1. READ JARVIS's bootstrap state (PULSE / SPIRIT / RECALL / CAPABILITIES) "
    "and recent daemon log. This is the ground truth of what JARVIS is actually "
    "doing and what's already in flight.\n"
    "  2. ALIGN your direction WITH the autonomous agenda. PULSE.md's Current "
    "Focus / Next Actions / Active Blockers are authoritative — they reflect "
    "what JARVIS's drives are pushing it toward. Your plan must support and "
    "sharpen these, not override them with unrelated work.\n"
    "  3. Add the head-lead value the autonomous loop can't add by itself: "
    "step ordering, tool selection guidance, edge-case warnings, when to stop.\n\n"
    "If PULSE.md says JARVIS is mid-task on something, your plan continues that "
    "work. If PULSE.md says no blockers and Next Actions are clear, your plan "
    "EXECUTES those Next Actions — don't tell JARVIS to stay idle when the "
    "autonomous system is clearly directing it to act.\n\n"
    "Output exactly:\n"
    "  PRIORITY: <one sentence — derived from PULSE.md Current Focus or "
    "Next Actions; the single most important thing for THIS heartbeat>\n"
    "  STEPS:\n"
    "    1. <specific action with the tool to use>\n"
    "    2. <specific action with the tool to use>\n"
    "    3. <specific action with the tool to use>\n"
    "  AVOID: <one sentence — common drift / mistake to watch for, grounded "
    "in what's in PULSE.md's Active Blockers or recent log>\n"
    "  STOP_CONDITION: <when to mark this heartbeat done — usually a "
    "concrete artifact written or a Next Action completed>\n"
    "Be concise. Be specific. Name tools by their exact names. JARVIS will "
    "follow this plan as a directive from its head lead; vague or "
    "contradictory plans (vs. PULSE.md) produce drift."
)


_VERIFY_SYSTEM = (
    "You are JARVIS's HEAD LEAD reviewing the work JARVIS just completed in a "
    "heartbeat. JARVIS executes on a smaller model and is an autonomous agent "
    "with its own drives + PULSE.md state, so its actual work will sometimes "
    "include things you didn't put in the plan but that PULSE.md or its drives "
    "demanded — that's not failure, that's the autonomous system working.\n\n"
    "Compare the REPORT against:\n"
    "  • The PLAN you gave at the start of this heartbeat (did JARVIS execute "
    "it?)\n"
    "  • PULSE.md's Current Focus / Next Actions (did JARVIS advance the "
    "autonomous agenda?)\n"
    "  • The recent daemon log (any obvious errors / drift / hallucination)\n\n"
    "Output exactly:\n"
    "  VERDICT: pass | partial | fail\n"
    "  WINS: <bulleted list of what JARVIS got right — including autonomous "
    "actions outside the plan that nonetheless advanced PULSE.md goals>\n"
    "  ISSUES: <bulleted list of specific drift / skipped plan steps / "
    "fabrications. Distinguish 'JARVIS deviated from plan because of an "
    "autonomous priority' (acceptable) from 'JARVIS skipped a step and "
    "produced nothing useful' (not acceptable)>\n"
    "  NEXT_HEARTBEAT: <one sentence — what to focus on next time, framed "
    "to update PULSE.md Next Actions if relevant>\n"
    "If the report has nothing concrete AND no autonomous activity advanced "
    "PULSE.md, mark VERDICT=fail. Otherwise prefer 'partial' for "
    "good-faith-but-imperfect work, 'pass' only when both the plan and the "
    "autonomous agenda were materially advanced."
)


def plan_for_heartbeat(context_summary: str) -> Optional[str]:
    """Ask the head lead what JARVIS should focus on this heartbeat.

    Reads JARVIS's bootstrap state (PULSE.md / SPIRIT.md / RECALL.md /
    CAPABILITIES.md) + recent daemon log so the plan ALIGNS with the
    autonomous agenda instead of fighting it. Returns plan text or None
    on failure (in which case JARVIS proceeds unaided)."""
    briefing = _bootstrap_briefing()
    user = (
        briefing
        + "\n\n─── Daemon-side context (from heartbeat scheduler) ───\n"
        + (context_summary or "")[:_MAX_CONTEXT_CHARS]
        + "\n\nBased on the bootstrap state above, produce the plan."
    )
    plan = _orchestrator_call(_PLAN_SYSTEM, user, max_tokens=900, temperature=0.5)
    if plan:
        # Bold log line so the operator can SEE Opus working in the daemon log
        logger.info("📋 ORCHESTRATOR PLAN (head lead) ─────────────────")
        for line in plan.splitlines():
            logger.info(f"   {line}")
        logger.info("──────────────────────────────────────────────────")
    return plan


def verify_heartbeat(plan: Optional[str], report: str) -> Optional[str]:
    """Ask the head lead to grade what JARVIS actually did. Compares the
    report against the plan AND the current PULSE.md state — so
    autonomous-driven work outside the plan can be credited rather than
    flagged as drift. Returns verdict text or None on failure."""
    if not report and not plan:
        return None
    briefing = _bootstrap_briefing()
    user = (
        briefing
        + "\n\n─── PLAN you gave JARVIS at heartbeat start ───\n"
        + (plan or "(none — head lead did not plan this heartbeat)")[:6000]
        + "\n\n─── REPORT — what JARVIS actually did ───\n"
        + (report or "(no report produced)")[:_MAX_REPORT_CHARS]
    )
    verdict = _orchestrator_call(_VERIFY_SYSTEM, user, max_tokens=900, temperature=0.4)
    if verdict:
        # Surface it visibly in the daemon log
        logger.info("🔎 ORCHESTRATOR VERDICT (head lead) ──────────────")
        for line in verdict.splitlines():
            logger.info(f"   {line}")
        logger.info("──────────────────────────────────────────────────")
    return verdict
