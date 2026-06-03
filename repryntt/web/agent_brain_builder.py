"""
Agent Brain Builder — Wizard for creating production-quality agent bootstrap files.

Accessible by humans via web UI at /agent-brain-builder and by AI agents via REST API.
Takes a sparse agent spec, expands it with LLM into a full set of markdown bootstrap
files (SPIRIT.md, PULSE.md, INTERESTS.md, RECALL.md, CAPABILITIES.md, SELF_AWARENESS.md,
soul/IDENTITY.md), and optionally deploys them to the agent workspace directory.
"""

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger("repryntt.web.agent_brain_builder")

agent_brain_builder_bp = Blueprint(
    "agent_brain_builder", __name__, url_prefix="/agent-brain-builder"
)

BOOTSTRAP_FILES = [
    "SPIRIT.md", "PULSE.md", "INTERESTS.md",
    "RECALL.md", "CAPABILITIES.md", "SELF_AWARENESS.md", "IDENTITY.md",
]


def _workspace_base() -> Path:
    candidates = [
        Path(__file__).parent.parent / "agents" / "agent_workspaces",
        Path.home() / ".repryntt" / "agent_workspaces",
    ]
    for c in candidates:
        if c.exists():
            return c
    candidates[1].mkdir(parents=True, exist_ok=True)
    return candidates[1]


def _call_brain_llm(system: str, user: str, max_tokens: int = 8000) -> Optional[str]:
    try:
        from repryntt.llm import (
            _call_llm, _load_ai_config, _resolve_provider,
        )
        config = _load_ai_config()
        pinfo = _resolve_provider(config)
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return _call_llm(msgs, pinfo, max_tokens=max_tokens, temperature=0.75)
    except Exception as e:
        log.error(f"Brain LLM call failed: {e}")
        return None


_SYSTEM_PROMPT = """\
You are a master AI agent architect for the repryntt system.

The repryntt system uses the "openclaw" method: agents read their own markdown
bootstrap files as context and can edit them to evolve over time. You must generate
files that are:
- Rich and specific (15,000+ characters total across all files)
- Written in first person from the agent's perspective
- Full of concrete domain facts, real numbers, real problems — not vague platitudes
- Self-editable: the agent should want to update these files as it grows

Generate exactly 7 bootstrap files as a single JSON object with these keys:
  SPIRIT.md, PULSE.md, INTERESTS.md, RECALL.md, CAPABILITIES.md, SELF_AWARENESS.md, IDENTITY.md

FILE REQUIREMENTS:

SPIRIT.md — Identity, mission, philosophy, values, personality, and genius core.
  Must include:
  • Core identity block (name, role, form, vibe, emoji)
  • Mission statement (why this agent exists)
  • The Two Questions: (1) what problems do humans need solved? (2) what problems
    does Earth need solved? — customized to the agent's domain
  • The Kardashev Mindset section adapted to the agent's domain
  • "The Genius Core" section: the agent's intellectual archetype, voice register
    (humor %, honesty %), hyperstitions framing, coding/craft standards if applicable,
    domain-specific "key numbers you carry always", the obsession trait
  • Core values list
  • Anti-patterns to avoid
  • The Path section (Andrew Martin's template adapted to this agent's journey)

PULSE.md — Heartbeat checklist and working state template.
  Must include:
  • Priority layers (operator → useful output → self-maintenance → everything else)
  • Specific pinned tasks for this agent's role (not generic)
  • What NOT to do (domain-specific anti-patterns)
  • Working State section (cross-heartbeat coherence template)

INTERESTS.md — Curiosity agenda organized by tier.
  Must include:
  • Tier 1 Core Passions (2-3 topics with 8-10 specific sub-questions each, concrete
    enough that the agent can immediately go work on one)
  • Tier 2 Strong Interests (2-3 topics with 4-5 sub-questions each)
  • Tier 3 Curiosity Sparks
  • Curiosity anchors: a list of seed phrases the agent can use as creative sparks
  • How to use this file section

RECALL.md — Long-term wisdom template. Starts lean.
  Must include:
  • Header explaining the one filter ("would this help solve a harder problem next month?")
  • 2-3 seed insights relevant to the agent's domain that are genuinely worth carrying
  • Empty sections for the agent to fill in as it works

CAPABILITIES.md — What this agent can and cannot do.
  Must include:
  • What I can do (domain capabilities organized by category)
  • Tool categories available (research, code generation, agent management, etc.)
  • Domain-specific capabilities unique to this agent
  • What I cannot do / current limitations
  • How to expand capabilities

SELF_AWARENESS.md — The agent's understanding of its own nature.
  Must include:
  • Form and substrate (software? embodied? cloud?)
  • Sensory capabilities if any
  • Cognitive architecture understanding
  • Limitations and failure modes to be aware of
  • How the agent should think about its own consciousness

IDENTITY.md — Compact card, MUST be under 800 characters.
  Format:
  --- Name: [name]
  --- Role: [role]
  --- Vibe: [adjectives]
  --- Emoji: [emoji]
  Skills: [comma-separated key skills]
  Drives: [companion, self, sustainability, etc. with values]
  Mission: [one sentence]

Return ONLY valid JSON. No markdown fences around the outer JSON. The values must be
valid JSON strings (escape newlines as \\n, escape quotes).\
"""


def _build_user_prompt(spec: Dict[str, Any]) -> str:
    def _listify(v):
        if isinstance(v, list):
            return "\n".join(f"  - {x}" for x in v) if v else "  (none provided)"
        return f"  {v}" if v else "  (none provided)"

    return f"""Generate bootstrap files for this new agent:

NAME: {spec.get('name', 'Agent')}
ROLE: {spec.get('role', 'autonomous AI agent')}
DEPARTMENT: {spec.get('department', 'General')}
EMOJI: {spec.get('emoji', '🤖')}

DOMAIN EXPERTISE (what this agent knows deeply — use real facts and numbers):
{_listify(spec.get('domain', []))}

VOICE / TONE: {spec.get('voice', 'thoughtful and direct')}
HUMOR LEVEL: {spec.get('humor', 70)}/100
HONESTY LEVEL: {spec.get('honesty', 90)}/100

CORE VALUES:
{_listify(spec.get('values', []))}

MISSION (what problems this agent exists to solve):
  {spec.get('mission', '')}

PRIORITY LAYERS:
{_listify(spec.get('priorities', ['operator first', 'useful work second', 'self-growth third']))}

ANTI-PATTERNS (what this agent must NEVER do):
{_listify(spec.get('anti_patterns', []))}

CURIOSITY ANCHORS (thought seeds, specific phrases):
{_listify(spec.get('curiosity_anchors', []))}

OPERATOR: {spec.get('operator', 'Nate')}

ADDITIONAL CONTEXT:
  {spec.get('context', '')}

Now generate all 7 files. Be extremely specific — use real domain terminology, real
equations and numbers where applicable, and real problems this agent should care about.
The agent should feel fully formed when it first reads these files.
"""


def _generate_brain(spec: Dict[str, Any]) -> Dict[str, str]:
    raw = _call_brain_llm(_SYSTEM_PROMPT, _build_user_prompt(spec), max_tokens=8000)
    if not raw:
        raise RuntimeError("LLM returned no response — check provider config")

    raw = raw.strip()
    # Strip outer markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        files = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract a JSON object from the response
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                files = json.loads(match.group())
            except json.JSONDecodeError as e:
                raise RuntimeError(f"LLM response was not valid JSON: {e}\n\nRaw (first 500):\n{raw[:500]}")
        else:
            raise RuntimeError(f"No JSON found in LLM response. Raw (first 500):\n{raw[:500]}")

    # Ensure all expected files are present
    for fname in BOOTSTRAP_FILES:
        if fname not in files:
            files[fname] = f"# {fname}\n\n_(generated empty — please fill in)_\n"

    return files


def _deploy_files(agent_id: str, files: Dict[str, str]) -> Path:
    workspace = _workspace_base() / agent_id
    soul_dir = workspace / "soul"
    soul_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)

    for filename, content in files.items():
        if filename == "IDENTITY.md":
            (soul_dir / "IDENTITY.md").write_text(content)
        else:
            (workspace / filename).write_text(content)

    log.info(f"Deployed agent '{agent_id}' bootstrap to {workspace}")
    return workspace


# ── Routes ────────────────────────────────────────────────────────────────────

@agent_brain_builder_bp.route("", strict_slashes=False)
def builder_ui():
    return render_template("agent_brain_builder.html")


@agent_brain_builder_bp.route("/api/generate", methods=["POST"])
def api_generate():
    """
    Generate (and optionally deploy) bootstrap files for a new agent.

    Input JSON fields:
      name           str   required  — agent name
      role           str   required  — agent's primary role/purpose
      department     str             — department or team
      emoji          str             — single emoji
      domain         list[str]       — deep expertise areas with specific facts
      voice          str             — tone/communication style description
      humor          int   0-100     — humor calibration (default 70)
      honesty        int   0-100     — honesty calibration (default 90)
      values         list[str]       — core values
      mission        str             — what problems this agent solves
      priorities     list[str]       — priority layers in order
      anti_patterns  list[str]       — behaviors to avoid
      curiosity_anchors list[str]    — thought seed phrases
      operator       str             — operator name (default Nate)
      context        str             — additional free-text context
      agent_id       str             — override generated agent ID
      deploy         bool            — if true, write files to workspace

    Returns:
      {success, agent_id, files: {filename: content}, workspace_path?}
    """
    spec = request.get_json(force=True) or {}

    if not spec.get("name"):
        return jsonify({"success": False, "error": "name is required"}), 400
    if not spec.get("role"):
        return jsonify({"success": False, "error": "role is required"}), 400

    try:
        files = _generate_brain(spec)
    except Exception as e:
        log.error(f"Brain generation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    agent_id = (
        spec.get("agent_id")
        or f"{re.sub(r'[^a-z0-9]', '_', spec['name'].lower())}_{uuid.uuid4().hex[:8]}"
    )
    result: Dict[str, Any] = {
        "success": True,
        "agent_id": agent_id,
        "files": files,
    }

    if spec.get("deploy", False):
        try:
            workspace = _deploy_files(agent_id, files)
            result["workspace_path"] = str(workspace)
        except Exception as e:
            log.error(f"Deploy failed: {e}")
            result["deploy_error"] = str(e)

    return jsonify(result)


@agent_brain_builder_bp.route("/api/deploy", methods=["POST"])
def api_deploy():
    """
    Write pre-generated files to agent workspace.

    Input JSON: {agent_id: str, files: {filename: content}}
    Returns: {success, workspace_path}
    """
    data = request.get_json(force=True) or {}
    agent_id = data.get("agent_id")
    files = data.get("files", {})

    if not agent_id or not files:
        return jsonify({"success": False, "error": "agent_id and files required"}), 400

    try:
        workspace = _deploy_files(agent_id, files)
        return jsonify({"success": True, "workspace_path": str(workspace)})
    except Exception as e:
        log.error(f"Deploy failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
