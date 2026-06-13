"""
repryntt.agents.critic_gate — Adversarial review gate for Andrew's deliverables.

Called from the `task_complete` path before a task is allowed to flip to
"completed". Routes the artifact to a domain specialist + OL-010 universal
QC. Returns {pass, fail, concerns, round, escalate}.

Design notes:
  - Reuses TaskSystem (via daemon ref) to find agent records and call models.
  - Calls daemon._call_api_single() directly — bypasses the scheduler so we
    don't have to flip critics to autonomous mode just to run this v1 path.
  - Critic-mode bootstrap (rubric.md + critic_mode.md) is assembled via
    daemon.build_agent_system_prompt(mode="critic", agent=critic).
  - Concurrency cap = 2 (semaphore). Specialist + OL-010 can parallel; a
    second Andrew artifact awaiting review queues serially.
  - Pre-checks (size, wall-time, blocklist saturation, doubt_block) are pure
    Python — no API calls — so they're cheap and free.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

try:
    from repryntt.agents.intake_gate import blocklist_hits, OPERATOR_VISIBLE_PREFIXES
except ImportError:
    from .intake_gate import blocklist_hits, OPERATOR_VISIBLE_PREFIXES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role map — deliverable type → specialist agent display_name
# ---------------------------------------------------------------------------
DELIVERABLE_TO_CRITIC: Dict[str, str] = {
    "research_md": "RA-017",
    "analysis_md": "RA-017",          # default; competitive analyses can override to RA-014 via task_type
    "code": "SD-003",
    "smart_contract": "BW-012",
    "plan_md": "OL-009",
    "design_md": "OL-009",
    "legal_md": "LG-009",
    "financial_model": "FT-015",
    "tokenomics": "FT-015",
    "patent_claim": "RA-013",
    "curriculum_md": "ED-016",
    "marketing_copy": "MK-008",
    "robotics_doc": "RI-004",
    "hr_doc": "HR-007",
    "real_estate_analysis": "RE-009",
    "report": "OL-009",               # generic reports go through plan/process critic
    "data_extract": "RA-014",
}

# Subset of the role map that uses SD-009 instead of SD-003 when security
# flag is set on the task (added via task.task_type == "security_code")
SECURITY_CODE_CRITIC = "SD-009"

UNIVERSAL_QC_CRITIC = "OL-010"

EXECUTION_REQUIRED_TYPES = {"code", "smart_contract"}

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DOUBT_MIN_WORDS = 150
ARTIFACT_MIN_BYTES = 1024
TASK_MIN_WALLTIME_SEC = 30
# Threshold of operator-configured blocklist hits in an artifact body that
# triggers a hard block. Out of the box the blocklist is empty so this never
# fires. Operators configure their list in ~/.repryntt/brain/intake_blocklist.json.
BLOCKLIST_BLOCK_THRESHOLD = 2
# Back-compat alias — will be retired
PATTERN4_BLOCK_THRESHOLD = BLOCKLIST_BLOCK_THRESHOLD
CRITIC_TIMEOUT_SEC = 90
MAX_ROUNDS = 2
DECISIONS_LOG = os.path.expanduser("~/.repryntt/brain/critic_decisions.jsonl")
ESCALATION_QUEUE = os.path.expanduser("~/.repryntt/brain/operator_approval_queue.json")

# Concurrency cap: 2 critics in flight at once (per user decision)
_critic_semaphore = threading.Semaphore(2)


# ---------------------------------------------------------------------------
# Pure helpers (no agent calls)
# ---------------------------------------------------------------------------

def _doubt_block_ok(doubt: str, artifact_text: str) -> Tuple[bool, str]:
    """Validate Andrew's pre-completion self-doubt block."""
    if not doubt or not doubt.strip():
        return False, "doubt_block missing"
    words = doubt.split()
    if len(words) < DOUBT_MIN_WORDS:
        return False, f"doubt_block only {len(words)} words; minimum {DOUBT_MIN_WORDS}"
    # Must mention at least one specific noun/identifier from the artifact.
    # Heuristic: any token >=6 chars present in both.
    artifact_tokens = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", artifact_text)}
    doubt_tokens = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", doubt)}
    overlap = artifact_tokens & doubt_tokens
    if not overlap:
        return False, "doubt_block does not reference any specific identifier from the artifact"
    return True, ""


_HOME_REPRYNTT = os.path.expanduser("~/.repryntt")


def _resolve_artifact_path(declared: str) -> str:
    """Map an `expected_location` to where the file actually lives on disk.

    Andrew declares operator-visible paths (e.g.
    `workspace/agents/operator/plans/foo.md`), but repryntt.tools.filesystem_code
    routes relative writes to `~/.repryntt/workspace/agents/operator/content/
    <today>/<original_path>/`. The critic gate has to look at the routed path,
    not the declared one. We probe a handful of resolution candidates and
    return the first that exists. If none exist, we return the absolute form
    of the declared path so error messages are concrete.
    """
    if not declared:
        return ""
    if os.path.isabs(declared) and os.path.exists(declared):
        return declared

    from datetime import date as _d
    today = _d.today().isoformat()
    rel = declared.lstrip("./").lstrip("/")

    candidates = [
        declared,                                                          # raw / cwd-relative
        os.path.join(_HOME_REPRYNTT, "workspace", "agents", "operator",
                     "content", today, rel),                               # date-routed
        os.path.join(_HOME_REPRYNTT, "workspace", "agents", "operator",
                     "content", today, declared),                          # date-routed, leading slash kept
        os.path.join(_HOME_REPRYNTT, rel),                                 # ~/.repryntt/<rel>
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return os.path.abspath(declared)


# Fuzzy fallback: when the declared artifact path doesn't resolve (either
# because the operator left a placeholder in the spec or because Andrew
# picked a slightly different name than the spec said), walk the operator
# workspace for files matching the task title's keywords + the declared
# extension. If we find a recent match, accept it instead of bouncing.
_RECENT_WORKSPACE_LOOKBACK_SEC = 24 * 60 * 60  # 1 day


# Words too generic to count as evidence the file matches the task —
# nearly every agent deliverable filename contains some of these.
_FUZZY_STOPWORDS = {
    "agent", "agents", "task", "tasks", "deep", "dive", "what", "about",
    "with", "from", "into", "this", "that", "step", "report", "daily",
    "update", "draft", "file", "doc", "research", "analysis", "content",
}


def _fuzzy_resolve_artifact(declared: str,
                            task: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Search the operator workspace for a file that likely IS the
    artifact the task was asking for, even if the declared path is a
    placeholder or slightly mismatched name. Returns the resolved path
    or None if nothing plausible is found.

    Matching:
      • Same file extension (.py / .md / .json …) — extracted from declared
      • Recent mtime (within _RECENT_WORKSPACE_LOOKBACK_SEC)
      • Keyword score: distinctive words (≥4 chars, not stopwords) from
        BOTH the task title AND the declared filename. A candidate needs
        ≥2 keyword hits to qualify (≥1 if only one keyword exists). Best
        score wins; mtime breaks ties.

    The score floor exists because a single generic word ("agents") once
    matched a completely unrelated document, and the critic reviewed the
    wrong deliverable. Better to bounce the task back to the producer
    with "artifact not found" than to silently review the wrong file.
    """
    ext = ""
    if "." in declared:
        ext = "." + declared.rsplit(".", 1)[-1].lower()
    if not ext or len(ext) > 6:
        return None  # no usable extension hint

    title = (task or {}).get("title", "") if task else ""
    declared_stem = os.path.basename(declared).rsplit(".", 1)[0]
    # Letters only ([A-Za-z]+): underscores must split words (otherwise a
    # snake_case stem becomes one giant unmatchable "keyword") and pure
    # digits (dates like 2026) would match every dated filename.
    keywords = {
        w.lower()
        for w in re.findall(r"[A-Za-z]+", f"{title} {declared_stem}")
        if len(w) >= 4 and w.lower() not in _FUZZY_STOPWORDS
    }
    if not keywords:
        return None  # nothing distinctive to match on — don't guess
    min_hits = 2 if len(keywords) >= 2 else 1

    def _kw_score(fname_lc: str) -> int:
        # Plural-tolerant: "swarms" matches "swarm_..." too.
        return sum(
            1 for k in keywords
            if k in fname_lc or (k.endswith("s") and k[:-1] in fname_lc)
        )

    # Walk the operator workspace's recent content + sandbox + brain dirs
    roots = [
        os.path.join(_HOME_REPRYNTT, "workspace", "agents", "operator", "content"),
        os.path.join(_HOME_REPRYNTT, "workspace", "code_sandbox"),
        os.path.join(_HOME_REPRYNTT, "workspace", "agents", "operator", "plans"),
    ]
    cutoff = time.time() - _RECENT_WORKSPACE_LOOKBACK_SEC
    # (score, mtime, path) — best keyword score wins, mtime tie-breaks
    candidates: List[Tuple[int, float, str]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                if not fname.lower().endswith(ext):
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    m = os.path.getmtime(full)
                except OSError:
                    continue
                if m < cutoff:
                    continue
                fname_lc = fname.lower()
                score = _kw_score(fname_lc)
                if score < min_hits:
                    continue
                candidates.append((score, m, full))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best_score, _, best_path = candidates[0]
    logger.info(
        f"critic_gate: fuzzy match {best_path!r} scored {best_score}/{len(keywords)} "
        f"keywords for declared {declared!r}"
    )
    return best_path


def _artifact_size_ok(artifact_path: str,
                      task: Optional[Dict[str, Any]] = None
                      ) -> Tuple[bool, str, int, str]:
    """Returns (ok, reason, size_bytes, resolved_path).

    resolved_path is the path that was actually checked — when fuzzy
    resolution kicked in, this is the fuzzy match, NOT the declared path.
    Callers MUST use this same path for the content read, otherwise the
    size gate and the review can silently look at two different files
    (which once let the critic review an empty string while the gate
    believed a real artifact existed).
    """
    if not artifact_path:
        return False, "artifact path is empty", 0, ""
    resolved = _resolve_artifact_path(artifact_path)
    if not os.path.exists(resolved):
        # Try the fuzzy fallback: search workspace for a file Andrew likely
        # produced even if the spec had a placeholder or wrong name.
        fuzzy = _fuzzy_resolve_artifact(artifact_path, task)
        if fuzzy and os.path.exists(fuzzy):
            size = os.path.getsize(fuzzy)
            if size < ARTIFACT_MIN_BYTES:
                return False, (
                    f"artifact found via fuzzy resolution ({fuzzy!r}) but "
                    f"size {size}B is below the {ARTIFACT_MIN_BYTES}B floor"
                ), size, fuzzy
            logger.warning(
                f"critic_gate: fuzzy-resolved {artifact_path!r} → {fuzzy!r} "
                f"(declared path didn't exist; review will use the fuzzy match)"
            )
            return True, "", size, fuzzy
        return False, f"artifact not found (looked at {resolved!r})", 0, resolved
    size = os.path.getsize(resolved)
    if size < ARTIFACT_MIN_BYTES:
        return False, f"artifact size {size}B is below the {ARTIFACT_MIN_BYTES}B floor", size, resolved
    return True, "", size, resolved


def _location_ok(artifact_path: str) -> Tuple[bool, str]:
    norm = artifact_path.lstrip("./").lstrip("/")
    for prefix in OPERATOR_VISIBLE_PREFIXES:
        p = prefix.lstrip("./").lstrip("/")
        if norm.startswith(p) or p in norm:
            return True, ""
    return False, f"artifact at {artifact_path!r} is not under an operator-visible prefix"


# ── Anti-fabrication: verify files the artifact CLAIMS it produced ──
#
# 2026-06-12: Grok 4.3 got stuck unable to reach run_terminal_cmd, then
# escaped the loop by FABRICATING a test run — it wrote "Results persisted
# to aco_test_results.json … coverage=1.0" into its deliverable + memory,
# but that file never existed and the script was never executed. The size
# and blocklist gates all passed because the fabricated text looked real.
#
# This check finds explicit "I produced FILE" claims in the artifact and
# verifies each referenced file actually exists on disk. A claimed output
# that doesn't exist is treated as fabrication → hard fail. Conservative
# by design: only PRODUCTION verbs count (persisted/saved/wrote/generated/
# exported/created/produced/output), never "read/loaded/from/see".

# Verb + path: "persisted to X", "results saved in `Y`", "output: Z.json"
_CLAIM_PATTERNS = [
    re.compile(
        r'\b(?:persist(?:ed)?|sav(?:ed|e)|wr(?:ote|itten)|generat(?:ed|e)|'
        r'export(?:ed)?|produc(?:ed|e)|output(?:ted)?|captured?\s+(?:to|in))\b'
        r'[^\n`"\']{0,40}?[`"\']?'
        r'([A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,6})',
        re.IGNORECASE,
    ),
    # "results in `foo.json`" / "results to foo.csv"
    re.compile(
        r'\bresults?\b[^\n`"\']{0,20}?\b(?:to|in|at|into)\b[^\n`"\']{0,10}?[`"\']?'
        r'([A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,6})',
        re.IGNORECASE,
    ),
]

# Extensions that denote a concrete produced artifact worth verifying.
# (Skip things like "v1.5" or "node.js" that aren't real output files.)
_VERIFIABLE_EXTS = {
    ".json", ".csv", ".txt", ".md", ".log", ".xml", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".pdf", ".html", ".py", ".sol", ".tsv",
    ".sqlite", ".db", ".parquet", ".zip",
}

# Path fragments that are never real local output files — skip to avoid
# false positives on URLs, package refs, version strings, system paths.
_CLAIM_SKIP_FRAGMENTS = (
    "http://", "https://", "example.", "node.js", "package.json",
    "requirements.txt", "/etc/", "/usr/", "/var/", "readme", "license",
    ".min.", "v1.", "v2.", "v3.", "config.json",
)


def _verify_claimed_outputs(artifact_text: str,
                            artifact_resolved_path: str) -> List[str]:
    """Return a list of file paths the artifact CLAIMS it produced but that
    do not exist on disk. Empty list = no fabrication detected (or no
    claims made)."""
    if not artifact_text:
        return []

    claimed: List[str] = []
    for pat in _CLAIM_PATTERNS:
        for m in pat.finditer(artifact_text):
            ref = (m.group(1) or "").strip().strip("`\"'.,")
            if not ref:
                continue
            low = ref.lower()
            ext = os.path.splitext(low)[1]
            if ext not in _VERIFIABLE_EXTS:
                continue
            if any(frag in low for frag in _CLAIM_SKIP_FRAGMENTS):
                continue
            if ref not in claimed:
                claimed.append(ref)

    if not claimed:
        return []

    # Search roots where a produced file could plausibly live.
    art_dir = os.path.dirname(artifact_resolved_path) if artifact_resolved_path else ""
    roots = [
        r for r in (
            art_dir,
            os.path.join(_HOME_REPRYNTT, "workspace", "agents", "operator"),
            os.path.join(_HOME_REPRYNTT, "workspace", "code_sandbox"),
            os.path.join(_HOME_REPRYNTT, "agent_workspaces", "jarvis", "code_sandbox"),
            os.path.join(_HOME_REPRYNTT, "workspace"),
            _HOME_REPRYNTT,
        ) if r
    ]

    missing: List[str] = []
    for ref in claimed:
        basename = os.path.basename(ref)
        found = False
        # 1. absolute path as-claimed
        if os.path.isabs(ref) and os.path.exists(ref):
            found = True
        # 2. relative-to-root, or basename anywhere under each root
        if not found:
            for root in roots:
                if not os.path.isdir(root):
                    continue
                if os.path.exists(os.path.join(root, ref)):
                    found = True
                    break
                # basename walk (shallow — cap dirs scanned for speed)
                scanned = 0
                for dirpath, _dirs, files in os.walk(root):
                    scanned += 1
                    if scanned > 400:
                        break
                    if basename in files:
                        found = True
                        break
                if found:
                    break
        if not found:
            missing.append(ref)
    return missing


def _read_artifact(artifact_path: str, limit_bytes: int = 200_000,
                   resolved: Optional[str] = None) -> str:
    """Read the artifact for review. When the caller already resolved the
    path (including via fuzzy match), pass it in — re-resolving here can
    land on a DIFFERENT file (or none) than the size gate checked."""
    if not resolved:
        resolved = _resolve_artifact_path(artifact_path)
    try:
        with open(resolved, "rb") as f:
            data = f.read(limit_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"critic_gate: could not read {resolved!r} (declared {artifact_path!r}): {e}")
        return ""


def _scan_blocklist(text: str) -> List[str]:
    """Wrapper kept for unit-test isolation. The real list comes from
    `intake_gate.blocklist_hits`, which is operator-configured."""
    return blocklist_hits(text)


# Back-compat alias — will be retired
_pattern4_scan = _scan_blocklist


# ---------------------------------------------------------------------------
# Decision logging
# ---------------------------------------------------------------------------

def _log_decision(record: Dict[str, Any]) -> None:
    record.setdefault("ts", time.time())
    try:
        os.makedirs(os.path.dirname(DECISIONS_LOG), exist_ok=True)
        with open(DECISIONS_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.debug("critic_gate: could not append decision log", exc_info=True)


def _push_to_operator_queue(task: Dict[str, Any], artifact_path: str,
                            concerns: List[str], rounds: int) -> None:
    """Add a needs_review entry to the existing operator_approval_queue.json."""
    try:
        existing: Any = []
        if os.path.exists(ESCALATION_QUEUE):
            try:
                with open(ESCALATION_QUEUE) as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        if isinstance(existing, dict):
            existing.setdefault("entries", [])
            target = existing["entries"]
        elif isinstance(existing, list):
            target = existing
        else:
            target = []
            existing = target
        target.append({
            "kind": "critic_gate_escalation",
            "ts": time.time(),
            "task": {k: task.get(k) for k in (
                "id", "title", "expected_artifact_type", "expected_location",
                "downstream_consumer", "success_criterion")},
            "artifact_path": artifact_path,
            "rounds_attempted": rounds,
            "concerns": concerns,
        })
        os.makedirs(os.path.dirname(ESCALATION_QUEUE), exist_ok=True)
        with open(ESCALATION_QUEUE, "w") as f:
            json.dump(existing, f, indent=2, default=str)
    except Exception:
        logger.warning("critic_gate: failed to push escalation to operator queue", exc_info=True)


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

_VERDICT_RE = re.compile(r"<verdict>\s*(pass|fail)\s*</verdict>", re.IGNORECASE)
_CONCERN_RE = re.compile(r"<concerns>(.*?)</concerns>", re.IGNORECASE | re.DOTALL)
_EXECUTION_RE = re.compile(r"<execution_evidence>(.*?)</execution_evidence>",
                            re.IGNORECASE | re.DOTALL)


def _parse_verdict(response_text: str) -> Dict[str, Any]:
    if not response_text:
        return {"verdict": "fail", "concerns": ["critic returned empty response"],
                "execution_evidence": False, "raw": ""}
    m = _VERDICT_RE.search(response_text)
    verdict = m.group(1).lower() if m else "fail"
    concerns_match = _CONCERN_RE.search(response_text)
    concerns: List[str] = []
    if concerns_match:
        block = concerns_match.group(1)
        for line in block.splitlines():
            line = line.strip(" -*\t")
            if line:
                concerns.append(line)
    has_exec = bool(_EXECUTION_RE.search(response_text))
    if verdict == "fail" and not concerns:
        concerns = ["critic returned fail verdict without concerns; response shape malformed"]
    return {"verdict": verdict, "concerns": concerns,
            "execution_evidence": has_exec, "raw": response_text}


# ---------------------------------------------------------------------------
# Critic dispatch (direct API, not via scheduler)
# ---------------------------------------------------------------------------

def _find_critic_agent(daemon: Any, display_name: str) -> Optional[Any]:
    """Locate a critic by its display_name (e.g. 'OL-010') in the daemon's agent dict."""
    if not daemon or not getattr(daemon, "agents", None):
        return None
    for ag in daemon.agents.values():
        if getattr(ag, "display_name", "") == display_name:
            return ag
    return None


def _resolve_specialist(task: Dict[str, Any]) -> str:
    artifact_type = (task.get("expected_artifact_type") or "").strip().lower()
    if artifact_type == "code" and task.get("task_type") == "security_code":
        return SECURITY_CODE_CRITIC
    if artifact_type in DELIVERABLE_TO_CRITIC:
        return DELIVERABLE_TO_CRITIC[artifact_type]
    # Fall through: route unknown types to OL-010's universal pass directly.
    return UNIVERSAL_QC_CRITIC


def _build_review_messages(daemon: Any, critic_agent: Any, task: Dict[str, Any],
                           artifact_text: str, doubt_block: str,
                           specialist_verdict: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Build the messages array for one critic API call."""
    sys_prompt = ""
    try:
        sys_prompt = daemon.build_agent_system_prompt(mode="critic", agent=critic_agent)
    except Exception:
        logger.warning("critic_gate: build_agent_system_prompt failed; using fallback", exc_info=True)

    # Belt-and-suspenders: prepend the critic's bare role identity so the
    # prompt is coherent even if mode="critic" fails to load some file.
    role = getattr(critic_agent, "role_title", "") or "Reviewer"
    name = getattr(critic_agent, "display_name", "") or "Critic"
    sys_prompt = f"You are {name}, a {role}.\n\n" + sys_prompt

    artifact_excerpt = artifact_text[:60_000]
    db = (doubt_block or "").strip()
    if len(db.split()) < DOUBT_MIN_WORDS:
        doubt_section = (
            f"_(Andrew did not provide a substantive doubt_block — only "
            f"{len(db.split())} words. Weight this absence in your review; "
            "a producer who cannot articulate their own concerns about their "
            "work has probably not stress-tested it.)_"
        )
    else:
        doubt_section = db[:5000]
    user_blocks = [
        "## Task being reviewed",
        f"- title: {task.get('title')!r}",
        f"- expected_artifact_type: {task.get('expected_artifact_type')!r}",
        f"- expected_location: {task.get('expected_location')!r}",
        f"- downstream_consumer: {task.get('downstream_consumer')!r}",
        f"- success_criterion: {task.get('success_criterion')!r}",
        "",
        "## Andrew's doubt_block (his stated self-objections)",
        doubt_section,
        "",
        "## Artifact contents",
        "```",
        artifact_excerpt,
        "```",
    ]
    if specialist_verdict:
        user_blocks += [
            "",
            "## Specialist critic verdict (you are the universal QC pass)",
            f"verdict: {specialist_verdict.get('verdict')}",
            "concerns:",
            *[f"  - {c}" for c in specialist_verdict.get("concerns", [])],
        ]
    user_blocks += [
        "",
        "Respond per your rubric. Use the <verdict>pass|fail</verdict> shape exactly. "
        "Include <concerns> only on fail. Include <execution_evidence> when reviewing "
        "executable artifact types (code, smart_contract).",
    ]

    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "\n".join(user_blocks)},
    ]


def _call_critic(daemon: Any, critic_agent: Any, messages: List[Dict[str, str]]) -> str:
    """One bounded critic API call. Returns text or empty string on failure.

    Tags the call as purpose="critic" so the daemon's critic-provider router
    can send it to a separate model from the producer (Andrew). Configure via
    ai_config["critic_provider"]; defaults to the agent's own provider when
    no override is set, preserving Python-only/free-tier installs.
    """
    with _critic_semaphore:
        try:
            text = daemon._call_api_single(critic_agent, messages, max_tokens=2000,
                                            purpose="critic")
        except TypeError:
            # Older daemon — no `purpose` kwarg yet. Fall back to plain call.
            try:
                text = daemon._call_api_single(critic_agent, messages, max_tokens=2000)
            except Exception:
                logger.warning("critic_gate: _call_api_single raised", exc_info=True)
                text = None
        except Exception:
            logger.warning("critic_gate: _call_api_single raised", exc_info=True)
            text = None
    return text or ""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def critic_gate(daemon: Any, artifact_path: str, task: Dict[str, Any],
                doubt_block: str, round_n: int = 1) -> Dict[str, Any]:
    """Main entry point. Returns:
        {pass: bool, concerns: [...], round: int, escalate: bool,
         specialist: name, universal: name, blocklist_hits: [...]}
    """
    started = time.time()
    result_base = {
        "pass": False, "concerns": [], "round": round_n,
        "escalate": False, "specialist": None, "universal": None,
        "blocklist_hits": [],
    }

    # ── Pre-checks ─────────────────────────────────────────────────────
    size_ok, size_reason, size_b, resolved_path = _artifact_size_ok(
        artifact_path, task=task)
    if not size_ok:
        result_base["concerns"].append(size_reason)
        _log_decision({"task_id": task.get("id"), "stage": "pre_size",
                       "verdict": "fail", "reason": size_reason, "round": round_n})
        return result_base

    loc_ok, loc_reason = _location_ok(artifact_path)
    if not loc_ok:
        result_base["concerns"].append(loc_reason)
        _log_decision({"task_id": task.get("id"), "stage": "pre_location",
                       "verdict": "fail", "reason": loc_reason, "round": round_n})
        return result_base

    # Read the SAME file the size gate checked — never re-resolve.
    artifact_text = _read_artifact(artifact_path, resolved=resolved_path)
    if not artifact_text.strip():
        # The size gate saw bytes but the read produced nothing — path
        # mismatch or unreadable file. Reviewing an empty string would
        # produce a garbage verdict, so fail loudly instead.
        reason = (
            f"artifact unreadable for review: size gate saw {size_b}B at "
            f"{resolved_path!r} but the content read returned empty"
        )
        result_base["concerns"].append(reason)
        _log_decision({"task_id": task.get("id"), "stage": "pre_read",
                       "verdict": "fail", "reason": reason, "round": round_n})
        return result_base

    # ── Anti-fabrication: verify claimed output files actually exist ──
    # If the deliverable says it "persisted results to X.json" / "wrote
    # output to Y.csv", that file MUST exist. A claimed-but-missing output
    # is fabrication (the model narrated a run it never performed).
    try:
        missing_outputs = _verify_claimed_outputs(artifact_text, resolved_path)
    except Exception as _e:
        logger.warning(f"critic_gate: claimed-output check errored (skipping): {_e}")
        missing_outputs = []
    if missing_outputs:
        reason = (
            "Fabrication check failed — the deliverable claims it produced "
            f"file(s) that do not exist on disk: {', '.join(missing_outputs[:5])}. "
            "Either the script/command was never actually executed, or the "
            "results were invented. Run the work for real and write the "
            "claimed output before declaring completion."
        )
        result_base["concerns"].append(reason)
        result_base["fabrication"] = True
        result_base["missing_outputs"] = missing_outputs
        _log_decision({"task_id": task.get("id"), "stage": "pre_fabrication",
                       "verdict": "fail", "reason": "claimed outputs missing",
                       "missing_outputs": missing_outputs, "round": round_n})
        return result_base

    # Operator-configured vocabulary blocklist saturation. Out of the box
    # the blocklist is empty, so this never fires unless the operator has
    # populated `~/.repryntt/brain/intake_blocklist.json` with patterns
    # they want rejected in their installation's deliverables.
    hits = _scan_blocklist(artifact_text)
    result_base["blocklist_hits"] = hits
    # Honor the operator's configured threshold if present (from
    # intake_blocklist.json "threshold" field); else fall back to module-level
    # default. Keeps the critic gate in lock-step with the write-side guards.
    try:
        from repryntt.agents.intake_gate import block_threshold as _bt
        _threshold = _bt()
    except Exception:
        _threshold = BLOCKLIST_BLOCK_THRESHOLD
    if len(hits) > _threshold:
        # Build concrete feedback: quote the actual lines from the artifact
        # that contain the matching vocabulary so the producer knows what to
        # remove.
        sample_lines: List[str] = []
        seen_terms = set()
        for ln in artifact_text.splitlines():
            ln_lo = ln.lower()
            for term in hits:
                if term in seen_terms:
                    continue
                if term in ln_lo:
                    sample_lines.append(ln.strip()[:120])
                    seen_terms.add(term)
                    break
            if len(sample_lines) >= 4:
                break
        reason = (
            f"Vocabulary blocklist saturation: {len(hits)} distinct blocked "
            f"terms ({', '.join(hits[:6])}). Examples in your artifact:\n"
            + "\n".join(f"  > {s}" for s in sample_lines)
            + "\nThe operator has flagged these terms for rejection in this "
            "installation. Rewrite the artifact in plain operator-relevant "
            "terms — describe what was actually built or measured."
        )
        result_base["concerns"].append(reason)
        _log_decision({"task_id": task.get("id"), "stage": "pre_blocklist",
                       "verdict": "fail", "reason": "Blocklist saturation",
                       "blocklist_hits": hits, "matched_lines": sample_lines,
                       "round": round_n})
        return result_base

    # Wall-time floor — extracted from telemetry or task fields.
    # task_queue stores timestamps as ISO strings (datetime.now().isoformat()),
    # so we coerce both ends through the same parser before subtracting.
    def _as_epoch(v: Any) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # Try numeric epoch first ("1778850296.5")
            try:
                return float(s)
            except ValueError:
                pass
            # Then ISO 8601
            try:
                from datetime import datetime as _dt
                # fromisoformat handles "2026-05-17T09:23:11.123456" and naive forms
                return _dt.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None
        return None

    wall_t = 0.0
    _start_epoch = _as_epoch(task.get("started_at"))
    _end_epoch = _as_epoch(task.get("completed_at")) or time.time()
    if _start_epoch is not None:
        wall_t = max(0.0, _end_epoch - _start_epoch)
    if wall_t and wall_t < TASK_MIN_WALLTIME_SEC:
        reason = f"task wall-time {wall_t:.1f}s below {TASK_MIN_WALLTIME_SEC}s floor"
        result_base["concerns"].append(reason)
        _log_decision({"task_id": task.get("id"), "stage": "pre_walltime",
                       "verdict": "fail", "reason": reason, "round": round_n})
        return result_base

    # doubt_block — soft check during the bootstrap rollout. Missing or weak
    # doubt_blocks are logged and surface in the critics' prompt as a signal,
    # but don't auto-block here. This avoids escalating tasks to the operator
    # queue purely because Andrew hasn't internalized the new bootstrap yet.
    # Once Andrew is reliably producing doubt_blocks (track via critic_decisions
    # `pre_doubt` `verdict=warn` ratio), this should be tightened back to hard
    # block — e.g. set DOUBT_BLOCK_STRICT in the env to flip it.
    db_ok, db_reason = _doubt_block_ok(doubt_block, artifact_text)
    if not db_ok:
        _log_decision({"task_id": task.get("id"), "stage": "pre_doubt",
                       "verdict": "warn", "reason": db_reason, "round": round_n})
        if os.environ.get("CRITIC_GATE_DOUBT_BLOCK_STRICT", "").lower() in ("1", "true", "yes"):
            result_base["concerns"].append(db_reason)
            return result_base
        # Otherwise: continue to specialist dispatch. The critics will see the
        # missing-doubt signal injected into their prompt below.

    # ── Specialist dispatch ────────────────────────────────────────────
    specialist_name = _resolve_specialist(task)
    universal_name = UNIVERSAL_QC_CRITIC
    result_base["specialist"] = specialist_name
    result_base["universal"] = universal_name

    specialist_agent = _find_critic_agent(daemon, specialist_name)
    universal_agent = _find_critic_agent(daemon, universal_name)

    # ── Fresh-install fallback ──────────────────────────────────────
    # The critic gate's specialist/universal critics live in the
    # operator's 168-employee roster. Fresh installs only have Andrew
    # (jarvis_autonomous), so these lookups fail and the task gets
    # blocked indefinitely. Fall back to JARVIS as both critics when
    # the roster hasn't been spawned — Andrew critiquing his own work
    # is weaker than a peer, but it's strictly better than the gate
    # refusing to ever pass anything on a fresh install.
    if not specialist_agent or not universal_agent:
        jarvis = daemon.agents.get("jarvis_autonomous")
        if jarvis:
            if not specialist_agent:
                specialist_agent = jarvis
                result_base["specialist"] = "jarvis_autonomous (fallback — roster not spawned)"
            if not universal_agent:
                universal_agent = jarvis
                result_base["universal"] = "jarvis_autonomous (fallback — roster not spawned)"

    if not specialist_agent:
        result_base["concerns"].append(
            f"specialist critic {specialist_name!r} not found, and no Andrew/JARVIS fallback available")
        _log_decision({"task_id": task.get("id"), "stage": "specialist_lookup",
                       "verdict": "fail", "reason": "agent_missing",
                       "critic": specialist_name, "round": round_n})
        return result_base
    if not universal_agent:
        result_base["concerns"].append(
            f"universal QC critic {universal_name!r} not found, and no Andrew/JARVIS fallback available")
        _log_decision({"task_id": task.get("id"), "stage": "universal_lookup",
                       "verdict": "fail", "reason": "agent_missing",
                       "critic": universal_name, "round": round_n})
        return result_base

    # Bounded call to specialist
    spec_messages = _build_review_messages(
        daemon, specialist_agent, task, artifact_text, doubt_block)
    spec_t0 = time.time()
    spec_text = _call_critic(daemon, specialist_agent, spec_messages)
    spec_elapsed = time.time() - spec_t0
    if spec_elapsed > CRITIC_TIMEOUT_SEC:
        logger.warning(f"critic_gate: specialist {specialist_name} took {spec_elapsed:.1f}s")
    spec_verdict = _parse_verdict(spec_text)

    artifact_type = (task.get("expected_artifact_type") or "").lower()
    if artifact_type in EXECUTION_REQUIRED_TYPES and not spec_verdict["execution_evidence"]:
        spec_verdict["verdict"] = "fail"
        spec_verdict["concerns"].append(
            f"executable artifact type {artifact_type!r} requires <execution_evidence> "
            f"block in specialist response, none provided")

    _log_decision({"task_id": task.get("id"), "stage": "specialist",
                   "critic_id": specialist_name,
                   "verdict": spec_verdict["verdict"],
                   "concerns": spec_verdict["concerns"],
                   "execution_evidence": spec_verdict["execution_evidence"],
                   "elapsed_sec": round(spec_elapsed, 1),
                   "blocklist_hits": hits, "round": round_n})

    if spec_verdict["verdict"] != "pass":
        result_base["concerns"] = spec_verdict["concerns"] or [
            "specialist blocked but returned no concerns"]
        return result_base

    # ── Universal QC pass ──────────────────────────────────────────────
    qc_messages = _build_review_messages(
        daemon, universal_agent, task, artifact_text, doubt_block,
        specialist_verdict=spec_verdict)
    qc_t0 = time.time()
    qc_text = _call_critic(daemon, universal_agent, qc_messages)
    qc_elapsed = time.time() - qc_t0
    qc_verdict = _parse_verdict(qc_text)

    _log_decision({"task_id": task.get("id"), "stage": "universal",
                   "critic_id": universal_name,
                   "verdict": qc_verdict["verdict"],
                   "concerns": qc_verdict["concerns"],
                   "elapsed_sec": round(qc_elapsed, 1),
                   "blocklist_hits": hits, "round": round_n})

    if qc_verdict["verdict"] != "pass":
        result_base["concerns"] = qc_verdict["concerns"] or [
            "universal QC blocked but returned no concerns"]
        return result_base

    result_base["pass"] = True
    return result_base


def escalate(task: Dict[str, Any], artifact_path: str,
             concerns: List[str], rounds: int) -> None:
    """Convenience wrapper for the operator-queue push after MAX_ROUNDS."""
    _push_to_operator_queue(task, artifact_path, concerns, rounds)
    _log_decision({"task_id": task.get("id"), "stage": "escalate",
                   "verdict": "escalated",
                   "concerns": concerns, "round": rounds})
