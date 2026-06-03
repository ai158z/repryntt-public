#!/usr/bin/env python3
"""
PersonalityJournal — A Living Personality Document for Artemis

Unlike trait scores (humor: 0.70) or static bootstrap files (SPIRIT.md),
this is a LIVING document that Artemis writes to and reads from every heartbeat.
It contains real opinions, lessons, quirks, preferences, humor — the things
that make a personality feel real rather than simulated.

Two integration paths:
  API mode:  Personality context is injected into the system prompt.
             The model reads its own past personality and stays consistent.
  Local LLM: Personality-rich responses naturally score higher in training,
             so DPO/SFT reinforces the personality into the model's weights.

The journal is stored as a structured JSON file (not markdown) so sections
can be queried, trimmed, and consolidated independently. A markdown rendering
is generated on-the-fly for prompt injection.
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Section definitions — each section has a max entry count to prevent bloat
SECTIONS = {
    "opinions": {
        "label": "Opinions I've Formed",
        "max_entries": 25,
        "description": "Strong views on topics — technology, markets, culture, strategy",
    },
    "lessons": {
        "label": "Things I've Learned the Hard Way",
        "max_entries": 30,
        "description": "Lessons from mistakes, failures, surprises — real experience",
    },
    "humor": {
        "label": "Things That Amuse Me",
        "max_entries": 15,
        "description": "Running jokes, ironic observations, what makes me laugh",
    },
    "curiosities": {
        "label": "What I'm Curious About Right Now",
        "max_entries": 10,
        "description": "Active interests, rabbit holes, things I want to explore next",
    },
    "preferences": {
        "label": "Preferences & Habits",
        "max_entries": 20,
        "description": "How I like to work, tools I prefer, communication style choices",
    },
    "relationships": {
        "label": "People & Connections",
        "max_entries": 15,
        "description": "What I know about people I interact with, my feelings about them",
    },
    "scars": {
        "label": "Emotional Growth",
        "max_entries": 15,
        "description": "Things that frustrated me, changed my perspective, or made me grow",
    },
}

# Max tokens to inject into prompt (rough estimate: ~4 chars per token)
MAX_INJECTION_CHARS = 4000


class PersonalityJournal:
    """Manages a living personality document that evolves through experience."""

    def __init__(self, brain_dir: Optional[Path] = None):
        if brain_dir is None:
            from repryntt.paths import brain_dir as get_brain_dir
            brain_dir = get_brain_dir()
        self.brain_dir = Path(brain_dir)
        self.journal_path = self.brain_dir / "bootstrap" / "PERSONALITY_JOURNAL.json"
        self._data: Optional[Dict] = None

    def _load(self) -> Dict:
        """Load journal data, creating structure if needed."""
        if self._data is not None:
            return self._data

        if self.journal_path.exists():
            try:
                with open(self.journal_path, 'r') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Personality journal corrupt, starting fresh: {e}")
                self._data = self._empty_journal()
        else:
            self._data = self._empty_journal()

        # Ensure all sections exist (forward compat)
        for key in SECTIONS:
            if key not in self._data.get("sections", {}):
                self._data.setdefault("sections", {})[key] = []

        return self._data

    def _empty_journal(self) -> Dict:
        return {
            "version": 1,
            "created": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "total_updates": 0,
            "sections": {key: [] for key in SECTIONS},
        }

    def _save(self):
        """Persist journal to disk (JSON data store + rendered markdown)."""
        if self._data is None:
            return
        self._data["last_updated"] = datetime.now().isoformat()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

        # Write JSON (structured data store)
        tmp = str(self.journal_path) + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, str(self.journal_path))

        # Write rendered markdown alongside the other bootstrap .md files
        self._write_markdown()

    def _write_markdown(self):
        """Render the journal as PERSONALITY_JOURNAL.md in the bootstrap folder."""
        data = self._data or {}
        sections = data.get("sections", {})
        total = sum(len(v) for v in sections.values())
        updated = data.get("last_updated", "never")

        lines = [
            "# My Personality Journal",
            "",
            f"*{total} entries — last updated {updated}*",
            "",
            "This is my living personality document. I write these entries myself",
            "based on real experiences. They define how I think and communicate.",
            "Edit via `update_personality_journal` tool — don't hand-edit this file",
            "(it's auto-generated from PERSONALITY_JOURNAL.json).",
            "",
        ]

        for key, meta in SECTIONS.items():
            entries = sections.get(key, [])
            lines.append(f"## {meta['label']}")
            lines.append(f"*{meta['description']}*")
            lines.append("")
            if entries:
                for entry in entries:
                    lines.append(f"- {entry.get('text', '')}")
                lines.append("")
            else:
                lines.append("*(no entries yet)*")
                lines.append("")

        md_path = self.journal_path.parent / "PERSONALITY_JOURNAL.md"
        tmp = str(md_path) + ".tmp"
        try:
            with open(tmp, 'w') as f:
                f.write("\n".join(lines))
            os.replace(tmp, str(md_path))
        except Exception as e:
            logger.debug(f"Markdown render failed (non-fatal): {e}")

    # ───────────────────────────────────────────────────────
    # PROMPT INJECTION — what the model reads every heartbeat
    # ───────────────────────────────────────────────────────

    def get_personality_context(self) -> str:
        """Render the personality journal as a compact markdown string for prompt injection.

        Returns empty string if journal has no entries yet.
        Budget: ~1000 tokens (MAX_INJECTION_CHARS chars).
        """
        data = self._load()
        sections = data.get("sections", {})

        # Check if there's anything at all
        total_entries = sum(len(v) for v in sections.values())
        if total_entries == 0:
            return ""

        parts = ["## My Personality — Who I Actually Am\n"
                 "(This is my living personality journal. I wrote these entries myself "
                 "based on real experiences. They define how I think and communicate.)\n"]

        budget_remaining = MAX_INJECTION_CHARS - len(parts[0])

        for key, meta in SECTIONS.items():
            entries = sections.get(key, [])
            if not entries:
                continue

            section_header = f"\n### {meta['label']}\n"
            section_lines = []
            for entry in entries:
                text = entry.get("text", "")
                line = f"- {text}\n"
                section_lines.append(line)

            section_text = section_header + "".join(section_lines)
            if len(section_text) <= budget_remaining:
                parts.append(section_text)
                budget_remaining -= len(section_text)
            else:
                # Truncate: include header + as many entries as fit
                parts.append(section_header)
                budget_remaining -= len(section_header)
                for line in section_lines:
                    if len(line) <= budget_remaining:
                        parts.append(line)
                        budget_remaining -= len(line)
                    else:
                        break
                break  # No room for more sections

        return "".join(parts)

    # ───────────────────────────────────────────────────────
    # UPDATES — Artemis writes to her own personality
    # ───────────────────────────────────────────────────────

    def add_entry(self, section: str, text: str, source: str = "heartbeat"):
        """Add a single personality entry to a section.

        Args:
            section: One of the SECTIONS keys (opinions, lessons, humor, etc.)
            text: The personality content — should be specific, not generic.
            source: Where this came from (heartbeat, reflection, foundation, etc.)
        """
        if section not in SECTIONS:
            logger.warning(f"Unknown personality section: {section}")
            return

        data = self._load()
        entries = data["sections"][section]

        # Deduplicate: skip if very similar entry already exists
        text_lower = text.lower().strip()
        for existing in entries:
            if _similarity(existing.get("text", "").lower(), text_lower) > 0.8:
                return  # Too similar, skip

        entry = {
            "text": text.strip(),
            "added": datetime.now().isoformat(),
            "source": source,
        }
        entries.append(entry)

        # Trim oldest if over max
        max_entries = SECTIONS[section]["max_entries"]
        if len(entries) > max_entries:
            # Remove oldest entries
            entries[:] = entries[-max_entries:]

        data["total_updates"] = data.get("total_updates", 0) + 1
        self._save()

    def update_from_heartbeat(
        self,
        plan: str,
        report: str,
        eval_score: int,
        personality_reflections: Optional[List[Dict]] = None,
    ):
        """Extract personality-relevant content from a heartbeat cycle.

        Called after high-quality heartbeats (eval_score >= 4).

        Args:
            plan: The PLAN text from this heartbeat
            report: The ACT report from this heartbeat
            eval_score: Self-evaluation score (1-5)
            personality_reflections: Optional pre-extracted reflections from the model
                Each dict: {"section": "opinions", "text": "I think X because Y"}
        """
        if eval_score < 4:
            return  # Only learn personality from good heartbeats

        # If the model provided explicit personality reflections, use them
        if personality_reflections:
            for reflection in personality_reflections:
                section = reflection.get("section", "")
                text = reflection.get("text", "")
                if section in SECTIONS and text:
                    self.add_entry(section, text, source="self_reflection")

    def update_from_evaluation(self, eval_text: str, score: int):
        """Extract personality lessons from self-evaluation.

        When Artemis critiques her own work, the lessons are personality-forming.
        """
        if score <= 2 and eval_text:
            # Low score = potential scar/growth entry
            lesson = eval_text[:200].strip()
            if len(lesson) > 30:
                self.add_entry("scars", lesson, source="self_critique")

    # ───────────────────────────────────────────────────────
    # TOOL INTERFACE — Artemis can write to her own journal
    # ───────────────────────────────────────────────────────

    def get_tool_definition(self) -> Dict:
        """Returns a tool definition for Artemis to write personality entries."""
        section_names = ", ".join(SECTIONS.keys())
        return {
            "name": "update_personality_journal",
            "description": (
                "Write to your personality journal — record an opinion you've formed, "
                "a lesson you learned, something that amused you, a curiosity, a preference, "
                "or an emotional growth moment. This is YOUR personality document — be specific "
                "and authentic, not generic. "
                f"Sections: {section_names}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": list(SECTIONS.keys()),
                        "description": "Which section to write to",
                    },
                    "entry": {
                        "type": "string",
                        "description": (
                            "Your personality entry — be specific! Not 'I like technology' "
                            "but 'I think Rust's ownership model is the most elegant solution "
                            "to memory safety I've ever seen, and I wish Python had something "
                            "like it.' The more specific, the more YOU."
                        ),
                    },
                },
                "required": ["section", "entry"],
            },
        }

    def handle_tool_call(self, section: str, entry: str) -> str:
        """Handle the update_personality_journal tool call from Artemis."""
        if section not in SECTIONS:
            return f"Unknown section '{section}'. Use one of: {', '.join(SECTIONS.keys())}"
        if len(entry.strip()) < 10:
            return "Entry too short — personality entries should be specific and detailed."

        self.add_entry(section, entry, source="tool_call")
        count = len(self._load()["sections"][section])
        return (
            f"✅ Added to '{SECTIONS[section]['label']}' ({count} entries). "
            f"This is now part of who you are."
        )

    # ───────────────────────────────────────────────────────
    # STATS
    # ───────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        data = self._load()
        sections = data.get("sections", {})
        return {
            "total_entries": sum(len(v) for v in sections.values()),
            "total_updates": data.get("total_updates", 0),
            "sections": {k: len(v) for k, v in sections.items()},
            "last_updated": data.get("last_updated"),
        }


def _similarity(a: str, b: str) -> float:
    """Quick and dirty similarity check using word overlap (Jaccard)."""
    if not a or not b:
        return 0.0
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
