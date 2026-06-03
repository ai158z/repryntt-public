"""
Evolution Bootstrap — Persistent Memory & Deliberative Reasoning for the Local LLM
===================================================================================

Gives the local LLM evolution loop the same persistent cognition that Jarvis has:

1. **Bootstrap Loading** — reads IDENTITY.md + PRIORITIES.md + MEMORY_BRIEF.md
   every cycle, injecting persistent context before all AI calls.

2. **Memory Brief Generation** — every N cycles, compresses recent work into
   MEMORY_BRIEF.md so the model has continuity across restarts.

3. **Self-Edit** — the model can update PRIORITIES.md and MEMORY_BRIEF.md
   to reflect new discoveries and shifted focus.

4. **Deliberative Wrapper** (optional) — Plan/Evaluate phases around task
   execution, enabled by config flag for capable models (70B+).

Designed for the ceiling: small models (Phi-3) benefit from the task list
and memory loading. Large models (70B+) also get the full deliberative
reasoning loop. The system degrades gracefully.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("brain.evolution_bootstrap")

# ─── Paths ───────────────────────────────────────────────────────────────

BOOTSTRAP_DIR = Path(__file__).parent / "evolution_bootstrap"
IDENTITY_FILE = BOOTSTRAP_DIR / "IDENTITY.md"
PRIORITIES_FILE = BOOTSTRAP_DIR / "PRIORITIES.md"
MEMORY_BRIEF_FILE = BOOTSTRAP_DIR / "MEMORY_BRIEF.md"

# Main bootstrap directory (shared with agent daemon — the OpenClaw-style workspace files)
MAIN_BOOTSTRAP_DIR = Path(__file__).parent / "bootstrap"

# ─── Configuration ───────────────────────────────────────────────────────

# How often to regenerate the memory brief (in evolution cycles)
MEMORY_BRIEF_INTERVAL = 25

# Enable deliberative Plan/Evaluate wrapper (set True for capable models)
# Phi-3 should leave this False; 70B+ models can set True
ENABLE_DELIBERATIVE = os.environ.get("SAIGE_DELIBERATIVE", "false").lower() == "true"

# Max chars to load from each bootstrap file (prevent context bloat)
MAX_IDENTITY_CHARS = 3000
MAX_PRIORITIES_CHARS = 2500
MAX_MEMORY_BRIEF_CHARS = 2000

# ─── Main Bootstrap Budgets ─────────────────────────────────────────────
# OpenClaw-style: load the same bootstrap files the agent daemon uses.
# Tight budgets because the local LLM has a small context window (4096 tokens).
# SPIRIT.md = identity/soul, OPERATOR.md = who we help, PROTOCOL.md = session rules
MAIN_BOOTSTRAP_FILES = {
    "SPIRIT.md":   1500,   # Core identity — truncated to essentials
    "OPERATOR.md": 1200,   # Small file, usually fits entirely
    "PROTOCOL.md": 1500,   # Session rules, safety, memory policy
    "PULSE.md":    1200,   # Heartbeat checklist (what to work on)
}


# ═════════════════════════════════════════════════════════════════════════
# 1. BOOTSTRAP LOADING
# ═════════════════════════════════════════════════════════════════════════

def load_bootstrap_context(personality_brain: Optional[Dict] = None,
                           hormone_summary: str = "",
                           learning_context: str = "") -> str:
    """
    Load all bootstrap files and assemble them into a single context block
    that gets prepended to the system prompt each cycle.

    Args:
        personality_brain: Optional ava_brain.json dict to inject current
                          personality snapshot into IDENTITY context.
        hormone_summary:   One-line hormone state string (e.g. "SEEKING(0.82)")
        learning_context:  Compact lessons from semantic/procedural memory
                          (~150-250 tokens) injected after MEMORY BRIEF.

    Returns:
        A formatted string ready to prepend to any AI prompt.
    """
    sections = []

    # ══ MAIN BOOTSTRAP FILES (OpenClaw-style — shared with agent daemon) ══
    # Load SPIRIT.md, OPERATOR.md, PROTOCOL.md, PULSE.md from brain/bootstrap/
    # These are the SAME files the Jarvis agent daemon uses, ensuring the local
    # LLM evolution loop has the same identity, rules, and context as the cloud.
    for fname, max_chars in MAIN_BOOTSTRAP_FILES.items():
        fpath = MAIN_BOOTSTRAP_DIR / fname
        text = _read_file_safe(fpath, max_chars)
        if text:
            sections.append(text)

    # ══ EVOLUTION-SPECIFIC FILES (supplements, not replacements) ══

    # ── IDENTITY ──
    identity_text = _read_file_safe(IDENTITY_FILE, MAX_IDENTITY_CHARS)
    if identity_text:
        # Inject live personality data into the identity section
        if personality_brain:
            personality = personality_brain.get("personality", {})
            name = personality.get("name", "")
            traits = personality.get("traits", [])
            clean_traits = [t for t in traits if len(t) < 50 and "{" not in t][:8]
            if name or clean_traits:
                live_block = "\n## Live Personality State\n"
                if name:
                    live_block += f"- Current name: {name}\n"
                if clean_traits:
                    live_block += f"- Current traits: {', '.join(clean_traits)}\n"
                if hormone_summary:
                    live_block += f"- Dominant drive: {hormone_summary}\n"
                identity_text += live_block

        sections.append(identity_text)

    # ── PRIORITIES ──
    priorities_text = _read_file_safe(PRIORITIES_FILE, MAX_PRIORITIES_CHARS)
    if priorities_text:
        sections.append(priorities_text)

    # ── MEMORY BRIEF ──
    memory_text = _read_file_safe(MEMORY_BRIEF_FILE, MAX_MEMORY_BRIEF_CHARS)
    if memory_text:
        sections.append(memory_text)

    # ── LEARNING CONTEXT (semantic + procedural lessons) ──
    if learning_context:
        sections.append(learning_context)

    if not sections:
        return ""

    header = "═══ PERSISTENT CONTEXT (loaded every cycle) ═══\n"
    footer = "\n═══ END PERSISTENT CONTEXT ═══"
    return header + "\n\n---\n\n".join(sections) + footer


def _read_file_safe(path: Path, max_chars: int) -> str:
    """Read a file, returning empty string on any error."""
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... [truncated]"
            return text
    except Exception as e:
        logger.warning(f"Could not read {path.name}: {e}")
    return ""


# ═════════════════════════════════════════════════════════════════════════
# 2. MEMORY BRIEF GENERATION
# ═════════════════════════════════════════════════════════════════════════

def generate_memory_brief(
    brain_system,
    task_system,
    hormone_system,
    personality_brain: Optional[Dict] = None,
    recent_task_limit: int = 8,
) -> bool:
    """
    Compress recent work into MEMORY_BRIEF.md for cross-restart continuity.

    Called every MEMORY_BRIEF_INTERVAL cycles by the evolution loop.
    Uses the AI to summarize if available, falls back to structured dump.

    Returns True if brief was written successfully.
    """
    try:
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M")

        # ── Gather data ──
        # Recent completed tasks
        completed_titles = task_system.get_recently_completed_titles(hours=8.0)
        if not completed_titles:
            completed_file = Path("brain/completed_tasks.json")
            if completed_file.exists():
                try:
                    completed = json.loads(completed_file.read_text())
                    completed_titles = [t.get("title", "") for t in completed[-recent_task_limit:]]
                except Exception:
                    completed_titles = []

        # Active task
        active = task_system.get_active_task()
        active_info = f"{active.title} (step {active.steps_taken}/{active.max_steps})" if active else "None"

        # Queued tasks
        queue = task_system.get_queue()
        queued_titles = [t.title for t in queue[:5]] if queue else []

        # Hormone state
        dominant_circuit, dom_level = hormone_system.get_dominant_circuit()
        drives = hormone_system.get_drive_priorities()
        top_drive = drives[0] if drives else None

        # Personality
        personality_name = ""
        personality_traits = []
        if personality_brain:
            p = personality_brain.get("personality", {})
            personality_name = p.get("name", "")
            personality_traits = [t for t in p.get("traits", []) if len(t) < 50][:6]

        # Active chains
        active_chains = []
        chains_dir = Path("brain/chains")
        if chains_dir.exists():
            for cf in sorted(chains_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]:
                try:
                    cd = json.loads(cf.read_text())
                    if not cd.get("goal_achieved", False):
                        active_chains.append({
                            "topic": cd.get("topic", "?"),
                            "steps": len(cd.get("segments", [])),
                        })
                except Exception:
                    continue

        # ── Build the brief ──
        brief = f"""# Memory Brief — What I Did Recently

This file is auto-generated every {MEMORY_BRIEF_INTERVAL} cycles to give me
continuity across restarts. It summarizes recent work so I don't repeat
myself or lose context.

## Last Updated
{timestamp}

## Recent Work Summary
"""
        if completed_titles:
            for i, title in enumerate(completed_titles[-recent_task_limit:], 1):
                brief += f"{i}. {title}\n"
        else:
            brief += "No completed tasks in the last 8 hours.\n"

        brief += f"\n## Currently Active\n{active_info}\n"

        if queued_titles:
            brief += "\n## Queued Next\n"
            for title in queued_titles:
                brief += f"- {title}\n"

        brief += "\n## Key Discoveries\n"
        brief += "(Review completed task outputs for notable findings)\n"

        if active_chains:
            brief += "\n## Active Threads\n"
            brief += "These chains are mid-investigation — pick them up first after restart:\n"
            for chain in active_chains:
                brief += f"- {chain['topic']} ({chain['steps']} steps so far)\n"
        else:
            brief += "\n## Active Threads\nNo active reasoning chains.\n"

        brief += f"\n## Personality Snapshot\n"
        if personality_name:
            brief += f"- Name: {personality_name}\n"
        if personality_traits:
            brief += f"- Traits: {', '.join(personality_traits)}\n"
        brief += f"- Dominant circuit: {dominant_circuit} ({dom_level:.2f})\n"
        if top_drive:
            brief += f"- Top drive: {top_drive['drive_name']} (deficit={top_drive['deficit']:.3f})\n"
            brief += f"  → {top_drive['recommended_action'][:80]}\n"

        # ── Write it ──
        MEMORY_BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_BRIEF_FILE.write_text(brief, encoding="utf-8")
        logger.info(f"📝 Memory brief updated ({len(brief)} chars)")
        return True

    except Exception as e:
        logger.error(f"Failed to generate memory brief: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════
# 3. SELF-EDIT (model updates its own bootstrap files)
# ═════════════════════════════════════════════════════════════════════════

def update_evolution_bootstrap(file_name: str, new_content: str = "",
                               section: str = "", section_content: str = "") -> str:
    """
    Tool-callable function: let the local LLM update its own bootstrap files.

    Two modes:
    1. Full replace: update_evolution_bootstrap(file_name="PRIORITIES.md", new_content="...")
    2. Section update: update_evolution_bootstrap(file_name="PRIORITIES.md",
                       section="Current Focus", section_content="Working on X")

    Only PRIORITIES.md and MEMORY_BRIEF.md are writable. IDENTITY.md is
    operator-controlled (like Jarvis's SPIRIT.md).

    Returns JSON status.
    """
    file_name = file_name.strip()
    WRITABLE = {"PRIORITIES.md": PRIORITIES_FILE, "MEMORY_BRIEF.md": MEMORY_BRIEF_FILE}

    if file_name not in WRITABLE:
        return json.dumps({
            "error": f"Cannot edit '{file_name}'. Writable files: {list(WRITABLE.keys())}",
            "reason": "IDENTITY.md is operator-controlled and not self-editable."
        })

    target = WRITABLE[file_name]

    try:
        if new_content:
            # Full replace
            if len(new_content) > 5000:
                return json.dumps({"error": "Content too long (max 5000 chars)"})
            target.write_text(new_content.strip() + "\n", encoding="utf-8")
            logger.info(f"📝 Evolution bootstrap '{file_name}' fully replaced ({len(new_content)} chars)")
            return json.dumps({"success": True, "file": file_name, "mode": "full_replace",
                              "chars": len(new_content)})

        elif section and section_content:
            # Section update — find `## {section}` and replace until next `##` or EOF
            # Strip leading # and whitespace from section name (model may include them)
            section = section.lstrip('#').strip()
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            import re
            pattern = rf"(## {re.escape(section)}\n)(.*?)(\n## |\Z)"
            match = re.search(pattern, current, re.DOTALL)
            if match:
                replacement = f"{match.group(1)}{section_content.strip()}\n{match.group(3)}"
                updated = current[:match.start()] + replacement + current[match.end():]
            else:
                # Section not found — append it
                updated = current.rstrip() + f"\n\n## {section}\n{section_content.strip()}\n"

            if len(updated) > 6000:
                return json.dumps({"error": "Resulting file too large (max 6000 chars)",
                                  "current_size": len(updated)})

            target.write_text(updated, encoding="utf-8")
            logger.info(f"📝 Evolution bootstrap '{file_name}' section '{section}' updated")
            return json.dumps({"success": True, "file": file_name, "mode": "section_update",
                              "section": section})

        else:
            return json.dumps({"error": "Provide either 'new_content' (full replace) or "
                              "'section' + 'section_content' (section update)"})

    except Exception as e:
        logger.error(f"Failed to update evolution bootstrap '{file_name}': {e}")
        return json.dumps({"error": str(e)})


# ═════════════════════════════════════════════════════════════════════════
# 4. DELIBERATIVE WRAPPER (Plan/Evaluate around task execution)
# ═════════════════════════════════════════════════════════════════════════

def deliberative_plan(task_title: str, task_description: str,
                      bootstrap_context: str, hormone_state: str,
                      generate_fn) -> Optional[str]:
    """
    Phase 1: PLAN — inner monologue before task execution.

    Asks the AI (no tools) to:
    1. Assess what the bootstrap context and hormone drives suggest
    2. List 2-3 approaches for this task
    3. Commit to ONE approach with success criteria

    Args:
        task_title:        Current task title
        task_description:  Current task description/deliverable
        bootstrap_context: Output of load_bootstrap_context()
        hormone_state:     One-line hormone state
        generate_fn:       Callable(prompt, include_tools=False) -> str

    Returns:
        The plan text, or None if AI fails.
    """
    if not ENABLE_DELIBERATIVE:
        return None

    prompt = f"""{bootstrap_context}

─── INNER PLANNING (no tools — just think) ───

You are about to work on: **{task_title}**
Description: {task_description}
Hormone state: {hormone_state}

Before you act, PLAN:
1. What does my PRIORITIES file say I should focus on?
2. What does my MEMORY BRIEF say I've already done on this topic?
3. List 2-3 concrete approaches I could take.
4. Commit to ONE approach. State what "done well" looks like.

Keep this under 200 words. Be specific, not vague.

MY PLAN:"""

    try:
        plan = generate_fn(prompt, include_tools=False)
        if plan and len(plan.strip()) > 20:
            logger.info(f"📋 Deliberative plan generated ({len(plan)} chars)")
            return plan.strip()
    except Exception as e:
        logger.warning(f"Deliberative plan failed (non-fatal): {e}")

    return None


def deliberative_evaluate(task_title: str, task_result: str,
                          plan: Optional[str], bootstrap_context: str,
                          generate_fn) -> Optional[Dict]:
    """
    Phase 3: EVALUATE — self-critique after task execution.

    Asks the AI (no tools) to rate its work 1-5 and assess depth.

    Returns:
        Dict with 'score' (int 1-5) and 'evaluation' (str), or None.
    """
    if not ENABLE_DELIBERATIVE:
        return None

    plan_section = f"\nMY PLAN WAS:\n{plan}\n" if plan else ""

    prompt = f"""{bootstrap_context}

─── SELF-EVALUATION (no tools — just reflect) ───

Task: **{task_title}**
{plan_section}
RESULT SUMMARY (last 500 chars):
{task_result[-500:] if task_result else '(no output)'}

Rate your work honestly:
- 1 = Nothing useful produced. Wasted cycle.
- 2 = Surface-level. Some searching but no real output.
- 3 = Decent. Produced something useful but could be deeper.
- 4 = Good. Solid research/output with real substance.
- 5 = Excellent. Deep work, novel connections, tangible artifact.

Format your response EXACTLY as:
SCORE: <number>
EVALUATION: <2-3 sentences on what went well and what could improve>
NEXT: <what should I do next cycle on this topic, or "done">"""

    try:
        response = generate_fn(prompt, include_tools=False)
        if not response:
            return None

        # Parse score
        import re
        score_match = re.search(r"SCORE:\s*(\d)", response)
        score = int(score_match.group(1)) if score_match else 3

        eval_match = re.search(r"EVALUATION:\s*(.+?)(?=\nNEXT:|\Z)", response, re.DOTALL)
        evaluation = eval_match.group(1).strip() if eval_match else response[:200]

        next_match = re.search(r"NEXT:\s*(.+)", response, re.DOTALL)
        next_step = next_match.group(1).strip() if next_match else ""

        result = {
            "score": min(5, max(1, score)),
            "evaluation": evaluation[:300],
            "next_step": next_step[:200],
        }
        logger.info(f"📊 Deliberative evaluation: score={result['score']}/5")
        return result

    except Exception as e:
        logger.warning(f"Deliberative evaluation failed (non-fatal): {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════
# 5. INTEGRATION HELPERS (called by saige_evolution_loop.py)
# ═════════════════════════════════════════════════════════════════════════

class EvolutionBootstrapManager:
    """
    Manager object held by SAIGEEvolutionLoop. Provides a clean interface
    for the loop to call without importing individual functions.

    Usage in __init__:
        from repryntt.core.evolution.bootstrap_manager import EvolutionBootstrapManager
        self.bootstrap_mgr = EvolutionBootstrapManager(
            brain_system=self.brain_system,
            task_system=self.task_system,
            hormone_system=self.hormone_system,
        )

    Usage each cycle:
        context = self.bootstrap_mgr.get_cycle_context()
        # ... prepend context to prompts ...

        if cycle_count % 25 == 0:
            self.bootstrap_mgr.update_memory_brief()
    """

    def __init__(self, brain_system, task_system, hormone_system):
        self.brain_system = brain_system
        self.task_system = task_system
        self.hormone_system = hormone_system
        self._last_brief_cycle = 0
        self._cached_context = ""
        self._cached_cycle = -1

    @property
    def personality_brain(self) -> Optional[Dict]:
        """Get personality brain from BrainSystem."""
        try:
            return getattr(self.brain_system, "personality_brain", None)
        except Exception:
            return None

    def get_cycle_context(self, cycle_count: int = 0, task_hint: str = "") -> str:
        """
        Load bootstrap context for this cycle. Cached within the same cycle
        to avoid re-reading files multiple times.

        Args:
            cycle_count: Current evolution cycle number.
            task_hint:   Title/topic of the upcoming task (used to search
                        relevant memories for injection).
        """
        if cycle_count == self._cached_cycle and self._cached_context:
            return self._cached_context

        # Hormone one-liner
        try:
            dom_circuit, dom_level = self.hormone_system.get_dominant_circuit()
            hormone_str = f"{dom_circuit}({dom_level:.2f})"
        except Exception:
            hormone_str = ""

        # ── Build compact learning context from memory ──
        learning_context = self._build_learning_context(task_hint)

        self._cached_context = load_bootstrap_context(
            personality_brain=self.personality_brain,
            hormone_summary=hormone_str,
            learning_context=learning_context,
        )
        self._cached_cycle = cycle_count
        return self._cached_context

    def _build_learning_context(self, task_hint: str = "") -> str:
        """
        Query semantic + procedural memory and build a compact lessons block
        that fits within ~250 tokens for the 4K context window.
        """
        lines = []
        try:
            mem = self.brain_system._memory

            # ── Relevant semantic memories (keyword search) ──
            query = task_hint or "recent learning outcomes tasks"
            results = mem.search_semantic_memory(query, limit=3)
            for r in results:
                topic = getattr(r, "topic", "") or ""
                content = getattr(r, "content", "") or ""
                if topic and content and "__test" not in topic:
                    # One-line compact summary
                    summary = content.replace("\n", " ").strip()[:120]
                    lines.append(f"- [{topic[:40]}] {summary}")

            # ── Top procedural lessons (success rates) ──
            proc_cache = getattr(self.brain_system, "procedural_cache", {})
            scored = []
            for task_type, proc in proc_cache.items():
                if task_type.startswith("__"):
                    continue
                sr = getattr(proc, "success_rate", None)
                if sr is None and isinstance(proc, dict):
                    sr = proc.get("success_rate")
                attempts = 0
                if hasattr(proc, "metadata"):
                    attempts = len(getattr(proc.metadata, "attempts", []) if hasattr(proc, "metadata") else [])
                elif isinstance(proc, dict):
                    attempts = len(proc.get("metadata", {}).get("attempts", []))
                if sr is not None and attempts > 0:
                    scored.append((task_type, sr, attempts))
            # Sort by attempts descending (most-practiced first)
            scored.sort(key=lambda x: x[2], reverse=True)
            for task_type, sr, attempts in scored[:3]:
                pct = int(sr * 100)
                lines.append(f"- Skill [{task_type}]: {pct}% success ({attempts} attempts)")

        except Exception as e:
            logger.debug(f"Learning context build failed (non-fatal): {e}")

        if not lines:
            return ""
        header = "## Recent Learning & Skills\n"
        return header + "\n".join(lines)

    def update_memory_brief(self) -> bool:
        """Generate and write the memory brief."""
        return generate_memory_brief(
            brain_system=self.brain_system,
            task_system=self.task_system,
            hormone_system=self.hormone_system,
            personality_brain=self.personality_brain,
        )

    def should_update_brief(self, cycle_count: int) -> bool:
        """Check if it's time to regenerate the memory brief."""
        if cycle_count == 0:
            return False
        if cycle_count - self._last_brief_cycle >= MEMORY_BRIEF_INTERVAL:
            self._last_brief_cycle = cycle_count
            return True
        return False

    def plan_task(self, task_title: str, task_description: str,
                  generate_fn) -> Optional[str]:
        """Run deliberative planning if enabled."""
        try:
            dom_circuit, dom_level = self.hormone_system.get_dominant_circuit()
            hormone_str = f"{dom_circuit}({dom_level:.2f})"
        except Exception:
            hormone_str = ""

        return deliberative_plan(
            task_title=task_title,
            task_description=task_description,
            bootstrap_context=self.get_cycle_context(),
            hormone_state=hormone_str,
            generate_fn=generate_fn,
        )

    def evaluate_task(self, task_title: str, task_result: str,
                      plan: Optional[str], generate_fn) -> Optional[Dict]:
        """Run deliberative evaluation if enabled."""
        return deliberative_evaluate(
            task_title=task_title,
            task_result=task_result,
            plan=plan,
            bootstrap_context=self.get_cycle_context(),
            generate_fn=generate_fn,
        )
