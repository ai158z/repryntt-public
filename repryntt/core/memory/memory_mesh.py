"""
MemoryMesh — The Subconscious Association Graph

Connects all 12 cognitive subsystems (SemanticMemory, EpisodicMemory,
ProceduralMemory, Consciousness, TripleLoop, Learning, PredictiveScorer,
ExperimentTracker, PersonalityJournal, OpenMind/Dreams, CoT Chains,
Consolidation) via a weighted association graph.

Each piece of data across all subsystems maps to a node via content hashing.
When the same concept appears in different subsystems, it maps to the same
node — creating automatic cross-linking. Edges carry weights that strengthen
on co-occurrence and decay over time.

Spreading activation: when a node is accessed, connected nodes receive
partial activation. This creates the "subconscious awareness" — the mesh
bubbles up strongly-connected patterns without being asked.

Storage: ~/.repryntt/brain/memory_mesh.json
"""

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("repryntt.memory_mesh")

from repryntt.paths import brain_dir as _brain_dir
MESH_PATH = _brain_dir() / "memory_mesh.json"
MAX_NODES = 2000
MAX_EDGES = 8000
ACTIVATION_DECAY = 0.85        # Per-heartbeat decay for activation levels
EDGE_DECAY_PER_DAY = 0.98      # Daily weight decay for edges
MIN_EDGE_WEIGHT = 0.25         # Below this, edge gets pruned
SPREADING_FACTOR = 0.3         # How much activation spreads to neighbors
MAX_SPREAD_DEPTH = 1           # Spreading activation depth limit (was 2 — caused saturation)
TOP_N_ACTIVE = 15              # Max nodes returned for subconscious context
REINFORCE_AMOUNT = 0.1         # How much co-occurrence reinforces an edge
WEAKEN_AMOUNT = 0.05           # How much a negative eval weakens edges

_singleton: Optional["MemoryMesh"] = None


def get_memory_mesh() -> "MemoryMesh":
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        _singleton = MemoryMesh()
    return _singleton


def _node_id(node_type: str, label: str) -> str:
    """Deterministic node ID from type + label. Same concept = same node."""
    raw = f"{node_type}:{label.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class MeshNode:
    __slots__ = ("id", "type", "label", "sources", "created", "last_activated",
                 "activation_count", "activation_level", "knowledge")

    def __init__(self, node_type: str, label: str, source: str = ""):
        self.id = _node_id(node_type, label)
        self.type = node_type       # topic, tool, capability, emotion, pillar, experience, memory
        self.label = label
        self.sources: List[str] = [source] if source else []  # which subsystems contributed
        self.created = time.time()
        self.last_activated = time.time()
        self.activation_count = 1
        self.activation_level = 0.5  # 0.0 - 1.0
        self.knowledge = ""          # Anchored knowledge snippet (actual content, not just a label)

    def activate(self, boost: float = 0.3):
        # Diminishing returns — approaching 1.0 asymptotically.
        # This creates a natural gradient: directly-fired nodes are highest,
        # spread-neighbors lower, distant nodes lowest. No ceiling clumping.
        headroom = 1.0 - self.activation_level
        self.activation_level += headroom * boost
        self.last_activated = time.time()
        self.activation_count += 1

    def decay(self, factor: float = ACTIVATION_DECAY):
        self.activation_level *= factor

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "type": self.type, "label": self.label,
            "sources": self.sources, "created": self.created,
            "last_activated": self.last_activated,
            "activation_count": self.activation_count,
            # activation_level IS included in the dict (used by activate/routing methods
            # at runtime) but from_dict() always resets to 0.0 on load.
            # This means: in-memory dicts are accurate, but disk persistence
            # doesn't carry stale activations from previous heartbeats.
            "activation_level": round(self.activation_level, 4),
        }
        if self.knowledge:
            d["knowledge"] = self.knowledge
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MeshNode":
        n = cls.__new__(cls)
        n.id = d["id"]
        n.type = d.get("type", "topic")
        n.label = d.get("label", "")
        n.sources = d.get("sources", [])
        n.created = d.get("created", time.time())
        n.last_activated = d.get("last_activated", time.time())
        n.activation_count = d.get("activation_count", 1)
        # Start at 0.0 — fire_pre_heartbeat() sets activations fresh.
        # Old saved values are ignored to prevent saturation.
        n.activation_level = 0.0
        n.knowledge = d.get("knowledge", "")
        return n


class MeshEdge:
    __slots__ = ("source_id", "target_id", "edge_type", "weight",
                 "created", "last_reinforced", "reinforcement_count")

    def __init__(self, source_id: str, target_id: str, edge_type: str = "co_occur",
                 weight: float = 0.3):
        self.source_id = source_id
        self.target_id = target_id
        self.edge_type = edge_type  # co_occur, caused, temporal, similar, reinforced
        self.weight = weight
        self.created = time.time()
        self.last_reinforced = time.time()
        self.reinforcement_count = 1

    def reinforce(self, amount: float = REINFORCE_AMOUNT):
        self.weight = min(1.0, self.weight + amount)
        self.last_reinforced = time.time()
        self.reinforcement_count += 1

    def weaken(self, amount: float = WEAKEN_AMOUNT):
        self.weight = max(0.0, self.weight - amount)

    def edge_key(self) -> str:
        return f"{self.source_id}->{self.target_id}:{self.edge_type}"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id, "target_id": self.target_id,
            "edge_type": self.edge_type, "weight": round(self.weight, 4),
            "created": self.created, "last_reinforced": self.last_reinforced,
            "reinforcement_count": self.reinforcement_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MeshEdge":
        e = cls.__new__(cls)
        e.source_id = d["source_id"]
        e.target_id = d["target_id"]
        e.edge_type = d.get("edge_type", "co_occur")
        e.weight = d.get("weight", 0.3)
        e.created = d.get("created", time.time())
        e.last_reinforced = d.get("last_reinforced", time.time())
        e.reinforcement_count = d.get("reinforcement_count", 1)
        return e


class MemoryMesh:
    """
    The subconscious association graph.

    Nodes represent concepts (topics, tools, capabilities, emotions, etc.)
    Edges represent associations (co-occurrence, causation, similarity, etc.)

    Any subsystem can:
      - record_association(a, b, ...) → create/reinforce an edge
      - activate(node) → spreading activation propagates to neighbors
      - get_subconscious_context() → top-N most activated nodes for prompt injection
    """

    def __init__(self, mesh_path: Path = MESH_PATH):
        self.mesh_path = mesh_path
        self.nodes: Dict[str, MeshNode] = {}
        self.edges: Dict[str, MeshEdge] = {}
        # Adjacency list for fast neighbor lookup
        self._adjacency: Dict[str, List[str]] = defaultdict(list)
        self._last_save = 0.0
        self._dirty = False
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self):
        if self.mesh_path.exists():
            try:
                # Always read as UTF-8 — Python's default open() uses
                # locale.getpreferredencoding() which is cp1252 on Windows
                # and chokes on UTF-8 mesh files saved by Linux/macOS or by
                # the save path here (which IS explicitly UTF-8).
                with open(self.mesh_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for nd in data.get("nodes", []):
                    node = MeshNode.from_dict(nd)
                    self.nodes[node.id] = node
                for ed in data.get("edges", []):
                    edge = MeshEdge.from_dict(ed)
                    key = edge.edge_key()
                    self.edges[key] = edge
                self._rebuild_adjacency()
                logger.info(f"MemoryMesh loaded: {len(self.nodes)} nodes, {len(self.edges)} edges")
            except Exception as e:
                logger.warning(f"MemoryMesh load failed, starting fresh: {e}")
                self.nodes = {}
                self.edges = {}
                self._adjacency = defaultdict(list)
        else:
            logger.info("MemoryMesh: no existing graph, starting fresh")

    def save(self, force: bool = False):
        if not self._dirty and not force:
            return
        # Throttle saves to once per 30s unless forced
        if not force and (time.time() - self._last_save) < 30:
            return
        try:
            self.mesh_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges.values()],
                "meta": {
                    "node_count": len(self.nodes),
                    "edge_count": len(self.edges),
                    "last_saved": time.time(),
                },
            }
            tmp = self.mesh_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            # os.replace is atomic AND cross-platform — Path.rename fails on
            # Windows (WinError 183) if the destination already exists.
            import os as _os
            _os.replace(str(tmp), str(self.mesh_path))
            self._last_save = time.time()
            self._dirty = False
        except Exception as e:
            logger.error(f"MemoryMesh save failed: {e}")

    def _rebuild_adjacency(self):
        self._adjacency = defaultdict(list)
        for key, edge in self.edges.items():
            self._adjacency[edge.source_id].append(key)
            self._adjacency[edge.target_id].append(key)

    # ── Node Management ──────────────────────────────────────────

    def ensure_node(self, node_type: str, label: str, source: str = "") -> MeshNode:
        """Get or create a node. Same type+label = same node (content hashing)."""
        nid = _node_id(node_type, label)
        if nid in self.nodes:
            node = self.nodes[nid]
            if source and source not in node.sources:
                node.sources.append(source)
            return node
        node = MeshNode(node_type, label, source)
        self.nodes[nid] = node
        self._dirty = True
        self._enforce_node_limit()
        return node

    def _enforce_node_limit(self):
        if len(self.nodes) <= MAX_NODES:
            return
        # Remove least-activated, oldest nodes
        sorted_nodes = sorted(
            self.nodes.values(),
            key=lambda n: (n.activation_level, n.last_activated),
        )
        to_remove = len(self.nodes) - MAX_NODES
        for node in sorted_nodes[:to_remove]:
            self._remove_node(node.id)

    def _remove_node(self, nid: str):
        # Remove all edges touching this node
        edge_keys = list(self._adjacency.get(nid, []))
        for ek in edge_keys:
            if ek in self.edges:
                del self.edges[ek]
        if nid in self._adjacency:
            del self._adjacency[nid]
        if nid in self.nodes:
            del self.nodes[nid]

    # ── Edge Management ──────────────────────────────────────────

    def record_association(self, node_a_type: str, node_a_label: str,
                           node_b_type: str, node_b_label: str,
                           edge_type: str = "co_occur",
                           strength: float = 0.3,
                           source: str = "") -> MeshEdge:
        """
        Create or reinforce an edge between two concepts.

        This is the primary hook — called by any subsystem when two concepts
        appear together (same heartbeat, same search, same dream, etc.)
        """
        a = self.ensure_node(node_a_type, node_a_label, source)
        b = self.ensure_node(node_b_type, node_b_label, source)

        # Always store edges in deterministic order for dedup
        if a.id > b.id:
            a, b = b, a

        edge = MeshEdge(a.id, b.id, edge_type, strength)
        key = edge.edge_key()

        if key in self.edges:
            self.edges[key].reinforce()
        else:
            self.edges[key] = edge
            self._adjacency[a.id].append(key)
            self._adjacency[b.id].append(key)
            self._enforce_edge_limit()

        # Both nodes get a small activation boost
        a.activate(0.1)
        b.activate(0.1)
        self._dirty = True
        return self.edges[key]

    def record_associations_batch(self, items: List[Tuple[str, str]],
                                  edge_type: str = "co_occur",
                                  source: str = ""):
        """
        Record co-occurrence edges between all pairs from a list of (type, label).
        Used when multiple concepts appear in the same heartbeat/context.
        """
        if len(items) < 2:
            return
        # Limit combinatorial explosion
        items = items[:20]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                self.record_association(
                    items[i][0], items[i][1],
                    items[j][0], items[j][1],
                    edge_type=edge_type, source=source,
                )
        self.save()

    def _enforce_edge_limit(self):
        if len(self.edges) <= MAX_EDGES:
            return
        sorted_edges = sorted(
            self.edges.values(),
            key=lambda e: (e.weight, e.last_reinforced),
        )
        to_remove = len(self.edges) - MAX_EDGES
        for edge in sorted_edges[:to_remove]:
            key = edge.edge_key()
            if key in self.edges:
                del self.edges[key]
        self._rebuild_adjacency()

    # ── Spreading Activation ─────────────────────────────────────

    def activate(self, node_type: str, label: str, boost: float = 0.5,
                 spread: bool = True) -> List[Dict]:
        """
        Activate a node and optionally spread activation to neighbors.

        Returns the activated neighborhood as a list of dicts sorted by
        activation level (highest first). This is the "subconscious recall"
        — when you think of X, what else surfaces?
        """
        nid = _node_id(node_type, label)
        if nid not in self.nodes:
            return []

        node = self.nodes[nid]
        node.activate(boost)
        self._dirty = True

        if not spread:
            return [node.to_dict()]

        # Spread activation through the graph
        activated = {nid: node.activation_level}
        frontier = [(nid, node.activation_level, 0)]

        while frontier:
            current_id, current_level, depth = frontier.pop(0)
            if depth >= MAX_SPREAD_DEPTH:
                continue

            spread_amount = current_level * SPREADING_FACTOR
            if spread_amount < 0.01:
                continue

            for edge_key in self._adjacency.get(current_id, []):
                edge = self.edges.get(edge_key)
                if not edge:
                    continue

                neighbor_id = edge.target_id if edge.source_id == current_id else edge.source_id
                if neighbor_id not in self.nodes:
                    continue

                # Activation = spread * edge_weight
                incoming = spread_amount * edge.weight
                if incoming < 0.01:
                    continue

                neighbor = self.nodes[neighbor_id]
                neighbor.activate(incoming)

                if neighbor_id not in activated or activated[neighbor_id] < neighbor.activation_level:
                    activated[neighbor_id] = neighbor.activation_level
                    frontier.append((neighbor_id, incoming, depth + 1))

        # Return top-N activated nodes
        result = []
        for nid, level in sorted(activated.items(), key=lambda x: x[1], reverse=True)[:TOP_N_ACTIVE]:
            if nid in self.nodes:
                result.append(self.nodes[nid].to_dict())
        return result

    def activate_multi(self, items: List[Tuple[str, str]], boost: float = 0.3) -> List[Dict]:
        """Activate multiple nodes and return the combined activated neighborhood."""
        all_activated: Dict[str, float] = {}
        for node_type, label in items:
            neighborhood = self.activate(node_type, label, boost=boost, spread=True)
            for n in neighborhood:
                nid = n["id"]
                if nid not in all_activated or all_activated[nid] < n["activation_level"]:
                    all_activated[nid] = n["activation_level"]

        result = []
        for nid in sorted(all_activated, key=all_activated.get, reverse=True)[:TOP_N_ACTIVE]:
            if nid in self.nodes:
                result.append(self.nodes[nid].to_dict())
        return result

    # ── Pre-Heartbeat Activation (FIRE NEURONS) ────────────────

    def fire_pre_heartbeat(self, *,
                           current_task: str = "",
                           task_type: str = "",
                           consciousness_drives: Optional[Dict[str, float]] = None,
                           consciousness_emotions: Optional[Dict[str, float]] = None,
                           consciousness_interests: Optional[Dict[str, float]] = None,
                           consciousness_mood: str = "",
                           loop_mode: str = "utility",
                           last_tools_used: Optional[List[str]] = None,
                           last_eval_score: int = 0,
                           heartbeat_num: int = 0,
                           perception_context=None,
                           ) -> List[Dict]:
        """
        FIRE BEFORE READING. This is the active consciousness pass.

        Called at the START of each heartbeat BEFORE get_subconscious_context().
        Takes the current state — what Andrew IS doing, feeling, driven by —
        and fires it into the mesh via spreading activation. This means
        get_subconscious_context() returns what's genuinely relevant RIGHT NOW,
        not stale leftovers from the last write-after.

        This is the difference between a brain and a log file.

        Returns: list of activated node dicts (the spreading neighborhood)
        """
        # ── RESET: activations are ephemeral, not persistent ──
        # Activations start at 0.0 (from disk) each heartbeat. We don't
        # need to decay — fire_pre_heartbeat() is the ONLY source of
        # activation. This prevents saturation: only what's relevant
        # RIGHT NOW is active. The graph topology (nodes, edges, knowledge)
        # is what persists across heartbeats — activations don't.
        for node in self.nodes.values():
            node.activation_level = 0.0
        fire_items: List[Tuple[str, str]] = []

        # ── Current task → activate task topic neurons ──
        if current_task:
            # Break task into meaningful words for topic matching
            _task_words = [w.lower().strip() for w in current_task.split()
                          if len(w) > 3 and w.lower() not in
                          {"this", "that", "with", "from", "your", "what", "have",
                           "will", "should", "could", "would", "been", "being",
                           "their", "there", "then", "than", "them", "these",
                           "those", "about", "into", "just", "also", "more"}]
            for word in _task_words[:5]:
                fire_items.append(("topic", word))

        # ── Task type → activate the behavioral mode ──
        if task_type:
            fire_items.append(("task_type", task_type))
            # Map task types to related concepts
            _task_type_map = {
                "trading_scan": [("topic", "trading"), ("topic", "solana")],
                "portfolio_management": [("topic", "trading"), ("topic", "wallet")],
                "interest_research": [("topic", "research"), ("capability", "web_search")],
                "proactive_research": [("topic", "research"), ("capability", "web_search")],
                "creative_work": [("topic", "creativity"), ("capability", "generation")],
                "self_evolution": [("topic", "self_improvement"), ("capability", "code")],
                "identity_reflection": [("topic", "identity"), ("topic", "philosophy")],
                "system_maintenance": [("topic", "system"), ("capability", "code")],
                "skill_building": [("topic", "learning"), ("capability", "experimentation")],
                "community_connection": [("topic", "social"), ("capability", "communication")],
                "email_check": [("topic", "email"), ("capability", "communication")],
            }
            for extra in _task_type_map.get(task_type, []):
                fire_items.append(extra)

        # ── Consciousness drives → activate high-urgency drive neurons ──
        if consciousness_drives:
            for drive_name, drive_level in consciousness_drives.items():
                if drive_level >= 0.4:  # Only fire drives above threshold
                    fire_items.append(("drive", drive_name))
                    # High drives get stronger activation (handled by boost below)

        # ── Consciousness emotions → activate strong emotion neurons ──
        if consciousness_emotions:
            for emotion, level in consciousness_emotions.items():
                if level >= 0.5:  # Only fire strong emotions
                    fire_items.append(("emotion", emotion))

        # ── Active interests → activate currently-weighted interest topics ──
        if consciousness_interests:
            # Top 3 interests by weight
            _sorted_interests = sorted(consciousness_interests.items(),
                                      key=lambda x: x[1], reverse=True)
            for interest_name, weight in _sorted_interests[:3]:
                if weight >= 0.3:
                    fire_items.append(("topic", interest_name))

        # ── Mood → activate mood node (connects to related patterns) ──
        if consciousness_mood:
            fire_items.append(("mood", consciousness_mood))

        # ── Loop mode → activate the current operating mode ──
        if loop_mode:
            fire_items.append(("loop_mode", loop_mode))

        # ── Last tools used → keep recent tool context warm ──
        if last_tools_used:
            for tool in last_tools_used[:5]:
                fire_items.append(("tool", tool))

        # ── Perception context (Strategy A) → sensory observations since last heartbeat ──
        # WorldState accumulates detected objects, heard speech, proximity alerts
        # between heartbeats. We fire them here as additional fire_items so the mesh
        # connects sensory input to existing knowledge without fighting the reset cycle.
        if perception_context:
            for obj in getattr(perception_context, 'detected_objects', [])[:5]:
                fire_items.append(("topic", obj))
            for speech in getattr(perception_context, 'heard_speech', [])[:3]:
                for word in speech.split()[:3]:
                    if len(word) > 3:
                        fire_items.append(("topic", word.lower().strip()))
            for alert in getattr(perception_context, 'proximity_alerts', [])[:2]:
                fire_items.append(("topic", alert))
            for scene in getattr(perception_context, 'scene_types', [])[:2]:
                if scene != "unknown":
                    fire_items.append(("topic", scene))

        if not fire_items:
            logger.debug("MemoryMesh fire_pre_heartbeat: nothing to fire")
            return []

        # Ensure all nodes exist in the mesh (creates if new)
        # But DON'T create nodes for random task words — only for structured
        # types (task_type, drive, emotion, mood, loop_mode, tool) and
        # task words that already exist in the mesh (i.e., meaningful concepts).
        # This prevents mesh explosion from ephemeral words.
        _existing_fire = []
        for node_type, label in fire_items:
            nid = _node_id(node_type, label)
            if nid in self.nodes:
                # Already exists — always fire it
                _existing_fire.append((node_type, label))
            elif node_type != "topic":
                # Structured types (task_type, drive, emotion, etc.) — create + fire
                self.ensure_node(node_type, label, source="pre_heartbeat")
                _existing_fire.append((node_type, label))
            # else: topic word not in mesh — skip (don't create ephemeral nodes)
        fire_items = _existing_fire

        # Create edges between co-occurring fire items (current state cross-links)
        # This builds the association graph from what's simultaneously active
        if len(fire_items) >= 2:
            # Don't full-combinatorial — connect each item to the task/tasktype hub
            _hub_items = [i for i in fire_items
                         if i[0] in ("task_type", "topic") and i in fire_items[:3]]
            if not _hub_items:
                _hub_items = fire_items[:2]
            for hub in _hub_items:
                for item in fire_items:
                    if item != hub:
                        self.record_association(
                            hub[0], hub[1], item[0], item[1],
                            edge_type="co_occur", strength=0.2,
                            source="pre_heartbeat",
                        )

        # FIRE: spreading activation from all current-state items
        # Boost 0.7 for directly-fired nodes. Neighbors get attenuated
        # through edge weights (SPREADING_FACTOR=0.3 * weight), creating
        # a natural gradient: directly relevant > associated > distant.
        activated = self.activate_multi(fire_items, boost=0.7)

        self._dirty = True
        self.save()

        logger.info(
            f"🧠 MemoryMesh FIRED: {len(fire_items)} neurons → "
            f"{len(activated)} nodes activated (spreading depth={MAX_SPREAD_DEPTH})"
        )
        return activated

    # ── Heartbeat Context (Subconscious Injection) ───────────────

    def get_subconscious_context(self, max_items: int = 10) -> str:
        """
        Returns activated nodes + their strongest connections for injection
        into the agent's system prompt.

        This IS the subconscious — patterns that bubble up from what's
        currently active. Call fire_pre_heartbeat() BEFORE this to ensure
        activations reflect the current state, not stale history.
        """
        # NOTE: No decay here — fire_pre_heartbeat() already decayed + re-fired.
        # Decaying again would weaken the signals we just activated.

        # Get top activated nodes
        active = sorted(
            self.nodes.values(),
            key=lambda n: n.activation_level,
            reverse=True,
        )[:max_items]

        if not active or active[0].activation_level < 0.05:
            return ""

        lines = []
        for node in active:
            if node.activation_level < 0.05:
                break
            # Find this node's strongest connections for richer context
            _connections = []
            for edge_key in self._adjacency.get(node.id, [])[:5]:
                edge = self.edges.get(edge_key)
                if edge and edge.weight >= 0.15:
                    neighbor_id = (edge.target_id if edge.source_id == node.id
                                  else edge.source_id)
                    neighbor = self.nodes.get(neighbor_id)
                    if neighbor and neighbor.id != node.id:
                        _connections.append(neighbor.label)

            conn_str = ""
            if _connections:
                conn_str = f" → linked to: {', '.join(_connections[:3])}"
            lines.append(
                f"  [{node.type}] {node.label} "
                f"(activation: {node.activation_level:.2f}){conn_str}"
            )
            # Knowledge anchoring: if this node has actual knowledge, surface it
            if node.knowledge:
                lines.append(f"    ↳ {node.knowledge[:200]}")

        if not lines:
            return ""

        self._dirty = True
        return (
            "🧠 ACTIVE CONSCIOUSNESS (what's firing in your neural mesh right now):\n"
            + "\n".join(lines)
            + "\n  These patterns surfaced from your current state — they represent "
            "what your subconscious considers relevant to what you're doing."
        )

    def get_top_active_nodes(self, limit: int = 15) -> List[Dict]:
        """Return structured data for top active nodes (for WorldState integration).

        Unlike get_subconscious_context() which returns formatted text, this returns
        raw dicts suitable for storage in WorldState.active_memories.
        """
        active = sorted(
            self.nodes.values(),
            key=lambda n: n.activation_level,
            reverse=True,
        )[:limit]

        results = []
        for node in active:
            if node.activation_level < 0.05:
                break
            results.append({
                "id": node.id,
                "type": node.type,
                "label": node.label,
                "activation": round(node.activation_level, 3),
                "knowledge": node.knowledge[:200] if node.knowledge else "",
            })
        return results

    # ── Context Router (mesh-driven prompt assembly) ─────────────

    # Maps bootstrap files to the mesh concepts that trigger their inclusion.
    # If ANY trigger node is active above threshold, the file loads.
    # Files not in this map always load (IDENTITY.md, PROFILE.md loaded directly).
    _CONTEXT_SOURCE_MAP = {
        "TRADING.md": {
            "trigger_labels": {
                "trading", "solana", "wallet", "jupiter", "dex", "swap",
                "portfolio", "token", "defi", "blockchain", "crypto",
                "trading_scan", "portfolio_management",
            },
            "trigger_types": {"topic", "task_type", "tool"},
            "min_activation": 0.40,
        },
        "TOOLKIT.md": {
            "trigger_labels": {
                "system", "code", "build", "forge", "tool", "explore",
                "system_maintenance", "skill_building", "self_evolution",
                "forge_project", "propose_code_change",
            },
            "trigger_types": {"topic", "task_type", "tool", "capability"},
            "min_activation": 0.40,
        },
        "OPERATOR.md": {
            "trigger_labels": {
                "email", "operator", "nate", "communication",
                "email_check", "gmail_read_inbox", "gmail_send",
                "gmail_reply", "gmail_draft",
            },
            "trigger_types": {"topic", "task_type", "tool"},
            "min_activation": 0.40,
        },
        "SPIRIT.md": {
            "trigger_labels": {
                "identity", "philosophy", "reflection", "self_evolution",
                "identity_reflection", "self_reflection", "creative_work",
                "consciousness", "meaning", "purpose",
            },
            "trigger_types": {"topic", "task_type", "drive"},
            "min_activation": 0.50,
        },
        "ARCHITECTURE.md": {
            "trigger_labels": {
                "identity", "reflection", "self_evolution", "consciousness",
                "system", "architecture", "heartbeat", "mesh", "memory",
                "self_knowledge", "self_awareness", "how_i_work",
            },
            "trigger_types": {"topic", "task_type", "drive"},
            "min_activation": 0.50,
        },
        "INTERESTS.md": {
            "trigger_labels": {
                "explore", "curiosity", "creative_work", "research",
                "skill_building", "self_evolution", "open_mind",
                "brainstorm", "deep_dive", "learning",
            },
            "trigger_types": {"topic", "task_type", "drive"},
            "min_activation": 0.30,
        },
        "RECALL.md": {
            # RECALL always loads but in modes:
            #   "tail" = last 3K chars (default — cheap continuity)
            #   "full" = entire file (first heartbeat of day only)
            "trigger_labels": set(),
            "trigger_types": set(),
            "min_activation": 0.0,
            "mode": "tail",
            "tail_chars": 3000,
        },
    }

    # Files that ALWAYS load regardless of mesh state
    # HOUSEHOLD.md is always-load: it's embodiment grounding (the home,
    # the operator, the pets) — Andrew should never be in a state where
    # he's "forgotten" he lives in a home with five cats and a dog.
    _ALWAYS_LOAD = {"PROTOCOL.md", "HEARTBEAT.md", "TOOLKIT.md", "HOUSEHOLD.md"}

    def get_context_routing(self, *, first_heartbeat_of_day: bool = False) -> Dict:
        """
        Based on current mesh activations (call fire_pre_heartbeat first!),
        determine which bootstrap files should load into the prompt.

        This is the router — it turns a 260K prompt into a 30-40K prompt
        by only loading what the mesh says is relevant right now.

        Returns dict with:
          load_files: list of filenames that should be loaded
          suppress_files: list of filenames that should NOT load
          recall_mode: "tail" or "full"
          recall_tail_chars: int (if tail mode)
          spirit_load: bool (whether SPIRIT.md should load in identity prompt)
          active_topics: list of topic labels active above threshold
          knowledge_snippets: dict of {topic: snippet} for high-activation nodes
        """
        load_files = list(self._ALWAYS_LOAD)
        suppress_files = []
        spirit_load = False
        active_topics = []
        knowledge_snippets = {}
        recall_mode = "full" if first_heartbeat_of_day else "tail"
        recall_tail_chars = 3000

        # Gather all active nodes above minimum threshold
        _active_by_label = {}
        for node in self.nodes.values():
            if node.activation_level >= 0.10:
                key = node.label.lower().strip()
                existing = _active_by_label.get(key, 0)
                if node.activation_level > existing:
                    _active_by_label[key] = node.activation_level
                # Collect active topics for memory section-filtering
                if node.type == "topic" and node.activation_level >= 0.20:
                    active_topics.append(node.label)
                # Collect knowledge snippets from high-activation nodes
                if node.knowledge and node.activation_level >= 0.25:
                    knowledge_snippets[node.label] = node.knowledge

        # Route each context source
        for filename, config in self._CONTEXT_SOURCE_MAP.items():
            if filename == "RECALL.md":
                continue  # Handled separately
            if filename == "SPIRIT.md":
                # SPIRIT.md is loaded in identity prompt, not system prompt
                min_act = config["min_activation"]
                for label in config["trigger_labels"]:
                    if _active_by_label.get(label, 0) >= min_act:
                        spirit_load = True
                        break
                # Also load on first heartbeat of day (identity refresh)
                if first_heartbeat_of_day:
                    spirit_load = True
                continue

            triggered = False
            min_act = config["min_activation"]
            for label in config["trigger_labels"]:
                if _active_by_label.get(label, 0) >= min_act:
                    triggered = True
                    break

            if triggered:
                load_files.append(filename)
            else:
                suppress_files.append(filename)

        # RECALL.md — always include but in appropriate mode
        load_files.append("RECALL.md")

        # Saturation guard: if everything loaded and the total token budget
        # would be excessive, suppress non-essential files. With Nemotron's
        # 128K context and ~20K identity prompt budget, loading 7-8 bootstrap
        # files is fine. Only fall back if truly ALL files triggered AND
        # suppress is empty — means the mesh isn't discriminating at all.
        _routable = [f for f in self._CONTEXT_SOURCE_MAP if f not in ("RECALL.md", "SPIRIT.md")]
        _non_always = [f for f in load_files if f not in self._ALWAYS_LOAD and f != "RECALL.md"]
        if suppress_files == [] and len(_non_always) >= len(_routable):
            # Everything triggered — suppress the two least relevant by activation
            # instead of nuking everything to lean fallback
            _scored = []
            for fn in _non_always:
                cfg = self._CONTEXT_SOURCE_MAP.get(fn, {})
                max_act = max(
                    (_active_by_label.get(lbl, 0) for lbl in cfg.get("trigger_labels", set())),
                    default=0
                )
                _scored.append((fn, max_act))
            _scored.sort(key=lambda x: x[1])
            # Suppress the 2 lowest-scoring non-essential files
            for fn, _ in _scored[:2]:
                if fn not in self._ALWAYS_LOAD:
                    load_files.remove(fn)
                    suppress_files.append(fn)
            logger.info("🔀 Context router: high activation — suppressed 2 lowest-relevance files")

        logger.info(
            f"🔀 Context router: LOAD={load_files}, "
            f"SUPPRESS={suppress_files}, "
            f"spirit={'yes' if spirit_load else 'no'}, "
            f"recall={recall_mode}, "
            f"active_topics={len(active_topics)}, "
            f"knowledge={len(knowledge_snippets)} snippets"
        )

        return {
            "load_files": load_files,
            "suppress_files": suppress_files,
            "recall_mode": recall_mode,
            "recall_tail_chars": recall_tail_chars,
            "spirit_load": spirit_load,
            "active_topics": active_topics,
            "knowledge_snippets": knowledge_snippets,
        }

    def get_relevant_memory_topics(self, min_activation: float = 0.20) -> List[str]:
        """
        Return topic labels currently active enough to filter daily memory by.

        Used by the heartbeat assembler to section-filter the daily memory file
        instead of dumping 75K-324K of raw entries. Only sections whose headings
        match active topics get loaded.
        """
        topics = []
        for node in self.nodes.values():
            if node.type in ("topic", "task_type") and node.activation_level >= min_activation:
                topics.append(node.label.lower())
        return topics

    # ── Knowledge Anchoring ──────────────────────────────────────

    def anchor_knowledge(self, node_type: str, label: str, snippet: str,
                         source: str = ""):
        """
        Attach a knowledge snippet to a node. When this node fires above
        threshold, the snippet surfaces in subconscious context — carrying
        actual information, not just a label.

        Snippets are capped at 300 chars. Newer knowledge overwrites older.
        This is how the mesh carries meaning, not just pointers.
        """
        node = self.ensure_node(node_type, label, source=source or "knowledge_anchor")
        node.knowledge = snippet[:300]
        self._dirty = True

    def anchor_from_memory_entry(self, heading: str, content: str,
                                 source: str = "daily_memory"):
        """
        Extract topic from a daily memory entry heading and anchor
        a knowledge snippet from its content. Called during post-heartbeat
        recording or consolidation.

        heading: e.g. "Cybersecurity Research (14:20)"
        content: the first ~300 chars of that section
        """
        # Clean heading — remove timestamps, heartbeat markers
        import re
        clean = re.sub(r'\s*\(\d{2}:\d{2}\)\s*$', '', heading).strip()
        clean = re.sub(r'^Heartbeat\s*#?\d+\s*[-—]?\s*', '', clean).strip()
        if not clean or len(clean) < 3:
            return

        # Extract a concise snippet from the content
        snippet = content.strip()[:300]
        if len(content) > 300:
            # Try to break at sentence boundary
            for end in ['. ', '.\n', '! ', '? ']:
                idx = snippet.rfind(end)
                if idx > 100:
                    snippet = snippet[:idx + 1]
                    break

        self.anchor_knowledge("topic", clean.lower(), snippet, source=source)

    # ── Dream Seeds ──────────────────────────────────────────────

    def get_dream_seeds(self, count: int = 5) -> List[Dict]:
        """
        Returns strongly-connected but recently-dormant nodes — ideal
        material for involuntary dreams. Dreams process what the conscious
        mind hasn't resolved.

        Criteria: high total activation_count (important), but low recent
        activation_level (not currently in focus) = unresolved.
        """
        candidates = []
        for node in self.nodes.values():
            if node.activation_count >= 3 and node.activation_level < 0.3:
                # Score: importance (count) * dormancy (inverse of recent activation)
                dormancy = 1.0 - node.activation_level
                score = node.activation_count * dormancy
                candidates.append((score, node))

        candidates.sort(key=lambda x: x[0], reverse=True)

        seeds = []
        for score, node in candidates[:count]:
            # Also gather the node's strongest edges
            connections = []
            for edge_key in self._adjacency.get(node.id, [])[:5]:
                edge = self.edges.get(edge_key)
                if edge:
                    neighbor_id = edge.target_id if edge.source_id == node.id else edge.source_id
                    neighbor = self.nodes.get(neighbor_id)
                    if neighbor:
                        connections.append({
                            "label": neighbor.label, "type": neighbor.type,
                            "edge_weight": edge.weight, "edge_type": edge.edge_type,
                        })
            seeds.append({
                "label": node.label, "type": node.type,
                "importance": node.activation_count,
                "dormancy": round(1.0 - node.activation_level, 3),
                "connections": connections,
            })
        return seeds

    # ── Consolidation ────────────────────────────────────────────

    def consolidate(self):
        """
        Called during the daily consolidation cycle.
        - Strengthen edges that have been reinforced multiple times
        - Decay all edge weights by time
        - Prune dead edges (weight < threshold)
        - Prune orphan nodes (no edges)
        """
        now = time.time()
        to_prune = []

        for key, edge in self.edges.items():
            # Time decay
            days_since = (now - edge.last_reinforced) / 86400
            if days_since > 0:
                edge.weight *= EDGE_DECAY_PER_DAY ** days_since

            # Prune weak edges
            if edge.weight < MIN_EDGE_WEIGHT:
                to_prune.append(key)

        for key in to_prune:
            del self.edges[key]

        # Prune orphan nodes (no edges, not recently activated)
        self._rebuild_adjacency()
        orphans = []
        for nid, node in self.nodes.items():
            if not self._adjacency.get(nid) and (now - node.last_activated) > 86400 * 3:
                orphans.append(nid)
        for nid in orphans:
            del self.nodes[nid]

        self._dirty = True
        self.save(force=True)
        logger.info(
            f"MemoryMesh consolidated: pruned {len(to_prune)} edges, "
            f"{len(orphans)} orphan nodes. "
            f"Remaining: {len(self.nodes)} nodes, {len(self.edges)} edges"
        )

    # ── Reinforcement from Evaluation ────────────────────────────

    def reinforce_heartbeat(self, topic: str, tools_used: List[str],
                            score: int, was_successful: bool):
        """
        Called after EVALUATE. If the heartbeat was good (score >= 3),
        reinforce all associations in this heartbeat's context.
        If bad, weaken them.
        """
        items: List[Tuple[str, str]] = [("topic", topic)]
        for tool in tools_used[:10]:
            items.append(("tool", tool))

        if was_successful:
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a_type, a_label = items[i]
                    b_type, b_label = items[j]
                    aid = _node_id(a_type, a_label)
                    bid = _node_id(b_type, b_label)
                    if aid > bid:
                        aid, bid = bid, aid
                    for edge_type in ["co_occur", "reinforced"]:
                        key = f"{aid}->{bid}:{edge_type}"
                        if key in self.edges:
                            self.edges[key].reinforce(REINFORCE_AMOUNT * (score / 5.0))
        else:
            # Weaken associations from failed heartbeats
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a_type, a_label = items[i]
                    b_type, b_label = items[j]
                    aid = _node_id(a_type, a_label)
                    bid = _node_id(b_type, b_label)
                    if aid > bid:
                        aid, bid = bid, aid
                    for edge_type in ["co_occur", "reinforced"]:
                        key = f"{aid}->{bid}:{edge_type}"
                        if key in self.edges:
                            self.edges[key].weaken(WEAKEN_AMOUNT)

        self._dirty = True
        self.save()

    # ── Graph-Aware Search Enhancement ───────────────────────────

    def search_enhanced(self, query_terms: List[str], limit: int = 10) -> List[Dict]:
        """
        Given query terms, activate matching nodes and return the spread
        neighborhood. Used to enhance brain_network_search with graph awareness.
        """
        items = []
        for term in query_terms:
            # Try to match existing nodes by label substring
            term_lower = term.lower().strip()
            for node in self.nodes.values():
                if term_lower in node.label.lower():
                    items.append((node.type, node.label))
                    break
            else:
                items.append(("topic", term))

        if not items:
            return []

        return self.activate_multi(items, boost=0.2)[:limit]

    # ── Stats for Monitoring ─────────────────────────────────────

    def stats(self) -> Dict:
        """Return mesh statistics for ops dashboard / monitoring."""
        type_counts: Dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            type_counts[node.type] += 1

        edge_type_counts: Dict[str, int] = defaultdict(int)
        for edge in self.edges.values():
            edge_type_counts[edge.edge_type] += 1

        avg_activation = 0.0
        if self.nodes:
            avg_activation = sum(n.activation_level for n in self.nodes.values()) / len(self.nodes)

        top_nodes = sorted(
            self.nodes.values(),
            key=lambda n: n.activation_count,
            reverse=True,
        )[:10]

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_types": dict(type_counts),
            "edge_types": dict(edge_type_counts),
            "avg_activation": round(avg_activation, 4),
            "top_nodes": [{"label": n.label, "type": n.type,
                           "count": n.activation_count,
                           "level": round(n.activation_level, 3)} for n in top_nodes],
        }


# ── Tool-callable functions for agent use ────────────────────────

def mesh_search(query: str, limit: int = 8) -> Dict:
    """
    Search the MemoryMesh association graph using spreading activation.

    When you search for a concept, the mesh activates related nodes and
    returns what surfaces — including connections you didn't explicitly
    search for. This is your subconscious recall.

    Args:
        query: A topic, tool name, or concept to search for
        limit: Maximum results to return (default 8)

    Returns:
        Dict with activated nodes and their connections
    """
    mesh = get_memory_mesh()
    terms = [t for t in query.split() if len(t) > 2]
    if not terms:
        return {"status": "error", "message": "Query too short"}

    results = mesh.search_enhanced(terms, limit=limit)
    mesh.save()
    return {
        "status": "success",
        "query": query,
        "activated_nodes": results,
        "mesh_stats": {
            "total_nodes": len(mesh.nodes),
            "total_edges": len(mesh.edges),
        },
    }


def mesh_stats() -> Dict:
    """
    Get statistics about the MemoryMesh association graph.

    Shows the current state of your subconscious — how many concepts
    are tracked, their types, and which nodes are most active.

    Returns:
        Dict with node/edge counts, type distribution, and top nodes
    """
    mesh = get_memory_mesh()
    return mesh.stats()


def mesh_connect(concept_a: str, concept_b: str,
                 relationship: str = "co_occur") -> Dict:
    """
    Manually create or reinforce an association between two concepts
    in the MemoryMesh.

    Use this when you discover that two things are related and want
    your subconscious to remember that connection.

    Args:
        concept_a: First concept (topic, tool name, etc.)
        concept_b: Second concept
        relationship: Type of connection (co_occur, caused, similar, temporal)

    Returns:
        Dict confirming the connection with edge details
    """
    if relationship not in ("co_occur", "caused", "similar", "temporal", "reinforced"):
        relationship = "co_occur"

    mesh = get_memory_mesh()
    edge = mesh.record_association(
        "topic", concept_a, "topic", concept_b,
        edge_type=relationship, source="agent_manual",
    )
    mesh.save()
    return {
        "status": "success",
        "edge": edge.to_dict(),
        "message": f"Connected '{concept_a}' ↔ '{concept_b}' ({relationship})",
    }


def mesh_anchor(concept: str, knowledge: str) -> Dict:
    """
    Anchor a piece of knowledge to a concept in the MemoryMesh.

    When this concept activates (because it's relevant to the current task),
    the anchored knowledge will surface in your subconscious context
    automatically — carrying actual information, not just that the concept
    exists. This is how you remember WHAT you learned, not just THAT you
    learned something.

    Use this after making a significant discovery, conclusion, or decision.

    Args:
        concept: The concept to attach knowledge to (e.g., "cybersecurity",
                 "solana trading", "edge computing")
        knowledge: A concise (1-3 sentence) summary of what you know/learned
                   about this concept. Max 300 chars.

    Returns:
        Dict confirming the anchored knowledge
    """
    if not concept or not knowledge:
        return {"status": "error", "message": "Both concept and knowledge required"}

    mesh = get_memory_mesh()
    mesh.anchor_knowledge("topic", concept.lower().strip(), knowledge)
    mesh.save()
    return {
        "status": "success",
        "concept": concept,
        "knowledge": knowledge[:300],
        "message": f"Knowledge anchored to '{concept}' — will surface when this concept is active",
    }
