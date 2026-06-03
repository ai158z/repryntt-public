#!/usr/bin/env python3
"""
Node2040 Brain Manager — Preload/overlay brain for concurrent operations.

Migrated from SAIGE/brain/brain_system.py Phase 7.
Handles loading, saving, and updating node2040_brain.json with recent
memories synced from the main brain network.
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class Node2040Manager:
    """Manages the preload/overlay brain (node2040_brain.json)."""

    def __init__(self, brain_system):
        self.brain = brain_system
        self.brain_path: Path = Path(brain_system.brain_path)
        self.node2040_brain_path: Path = Path(
            getattr(brain_system, "node2040_brain_path", self.brain_path / "node2040_brain.json")
        )

    # ------------------------------------------------------------------ #
    #  LOAD / SAVE                                                        #
    # ------------------------------------------------------------------ #

    def load_node2040_brain(self) -> Dict[str, Any]:
        """Load the preload/overlay brain from database or JSON."""
        try:
            use_database = getattr(self.brain, "use_database", False)
            if use_database:
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        node2040_memory = db.query(BrainMemory).filter_by(
                            memory_id="node2040_brain", memory_type="system"
                        ).first()
                        if node2040_memory:
                            self.brain.node2040_brain = json.loads(node2040_memory.content)
                            logger.info("⚡ Loaded preload brain overlay from database")
                            return self.brain.node2040_brain
                except Exception as e:
                    logger.warning(f"Database load failed for node2040 brain: {e}")

            if self.node2040_brain_path.exists():
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        with open(self.node2040_brain_path, "r") as f:
                            self.brain.node2040_brain = json.load(f)
                        logger.info("⚡ Loaded preload brain overlay from node2040_brain.json")
                        return self.brain.node2040_brain
                    except json.JSONDecodeError as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"JSON decode error (attempt {attempt + 1}/{max_retries}): {e}")
                            time.sleep(0.1)
                        else:
                            logger.error(f"Failed to load node2040 brain after {max_retries} attempts: {e}")
                            raise
            else:
                self.brain.node2040_brain = {
                    "preload": {
                        "recent_context": [],
                        "active_operations": [],
                        "immediate_memory": [],
                    },
                    "metadata": {
                        "description": "SAIGE Preload Brain - Recent operations and immediate context",
                        "max_recent_items": 50,
                        "last_updated": datetime.now().isoformat(),
                    },
                }
                logger.info("⚡ Initialized new preload brain structure")

            return self.brain.node2040_brain
        except Exception as e:
            logger.error(f"Error loading node2040 brain: {e}")
            self.brain.node2040_brain = {}
            return {}

    def save_node2040_brain(self) -> None:
        """Save node2040 brain to database and/or JSON file."""
        try:
            use_database = getattr(self.brain, "use_database", False)
            if use_database:
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        existing = db.query(BrainMemory).filter_by(
                            memory_id="node2040_brain", memory_type="system"
                        ).first()
                        if existing:
                            existing.content = json.dumps(self.brain.node2040_brain)
                            existing.last_accessed = datetime.utcnow()
                        else:
                            db.add(BrainMemory(
                                memory_id="node2040_brain", memory_type="system",
                                content=json.dumps(self.brain.node2040_brain), importance=1.0,
                            ))
                        db.commit()
                        return
                except Exception as e:
                    logger.warning(f"Database save failed for node2040 brain: {e}")

            with open(self.node2040_brain_path, "w") as f:
                json.dump(self.brain.node2040_brain, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving node2040 brain: {e}")

    # ------------------------------------------------------------------ #
    #  UPDATE (SYNC FROM BRAIN NETWORK)                                    #
    # ------------------------------------------------------------------ #

    def update_node2040_brain(self) -> None:
        """Sync recent memories from the brain network into node2040_brain.json."""
        try:
            _safe = getattr(self.brain, "_safe_mem_attr", lambda m, k, d=None: getattr(m, k, d) if hasattr(m, k) else (m.get(k, d) if isinstance(m, dict) else d))

            # Ensure autonomous_thoughts list exists
            if "autonomous_thoughts" not in self.brain.node2040_brain:
                self.brain.node2040_brain["autonomous_thoughts"] = []

            thoughts = self.brain.node2040_brain["autonomous_thoughts"]

            # Sync episodic memories
            episodic = getattr(self.brain, "episodic_cache", [])
            for memory in list(episodic)[-10:]:
                mem_metadata = _safe(memory, "metadata", {})
                if isinstance(mem_metadata, str):
                    mem_metadata = {}
                thoughts.append({
                    "timestamp": _safe(memory, "timestamp", time.time()),
                    "prompt": _safe(memory, "user_input", ""),
                    "response": _safe(memory, "ai_response", ""),
                    "source": "brain_network_sync",
                    "emotions": {"curiosity": 0.5, "confidence": 0.7},
                    "theme": mem_metadata.get("theme", "general") if isinstance(mem_metadata, dict) else "general",
                    "cycle": len(thoughts) + 1,
                })

            # Sync semantic memories
            semantic = getattr(self.brain, "semantic_cache", {})
            recent_semantic = sorted(
                semantic.values(),
                key=lambda x: _safe(x, "timestamp", 0),
                reverse=True,
            )[:5]
            for memory in recent_semantic:
                content = _safe(memory, "content", "")
                thoughts.append({
                    "timestamp": _safe(memory, "timestamp", time.time()),
                    "prompt": f"Knowledge: {_safe(memory, 'topic', 'unknown')}",
                    "response": content[:300] + "..." if len(content) > 300 else content,
                    "source": "semantic_memory_sync",
                    "emotions": {"curiosity": 0.8, "confidence": _safe(memory, "confidence", 0.5)},
                    "theme": _safe(memory, "domain", "general"),
                    "cycle": len(thoughts) + 1,
                })

            # Sync procedural memories
            procedural = getattr(self.brain, "procedural_cache", {})
            recent_procedural = sorted(
                procedural.values(),
                key=lambda x: _safe(x, "timestamp", 0),
                reverse=True,
            )[:3]
            for memory in recent_procedural:
                steps = _safe(memory, "steps", [])
                success_rate = _safe(memory, "success_rate", 0.5)
                try:
                    success_rate = float(success_rate)
                except (TypeError, ValueError):
                    success_rate = 0.5
                thoughts.append({
                    "timestamp": _safe(memory, "timestamp", time.time()),
                    "prompt": f"Procedure learned: {_safe(memory, 'task_type', 'unknown')}",
                    "response": f"Steps: {steps[:3] if isinstance(steps, list) else steps} | Success rate: {success_rate:.2f}",
                    "source": "procedural_memory_sync",
                    "emotions": {"alertness": 0.6, "confidence": success_rate},
                    "theme": "procedure",
                    "cycle": len(thoughts) + 1,
                })

            # Cap at 100 thoughts
            if len(thoughts) > 100:
                self.brain.node2040_brain["autonomous_thoughts"] = thoughts[-100:]

            # Update metadata
            metadata = self.brain.node2040_brain.setdefault("metadata", {})
            metadata["last_brain_sync"] = time.time()
            get_stats = getattr(self.brain, "get_brain_stats", None)
            if get_stats:
                metadata["brain_network_stats"] = get_stats()

            # Save
            with open(self.node2040_brain_path, "w") as f:
                json.dump(self.brain.node2040_brain, f, indent=2, default=str)

            logger.debug("📝 Synced recent memories from brain network to node2040_brain.json")
        except Exception as e:
            logger.error(f"Error updating node2040_brain.json: {e}")
