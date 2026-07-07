"""
repryntt.core.memory.agent_memory — Unified memory management for BrainSystem.

Extracted from SAIGE/brain/brain_system.py Phase 7 migration.
Manages episodic, semantic, procedural, and working memory compartments,
plus brain_memory_save/recall for per-agent persistent memory, and
brain network search across all memory types.
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import asdict, fields

logger = logging.getLogger(__name__)


class AgentMemoryManager:
    """Unified memory system for all BrainSystem memory operations.

    Depends on ``brain_system`` for:
      - Memory file paths (episodic_memory_file, semantic_memory_file, etc.)
      - Memory caches (episodic_cache, semantic_cache, procedural_cache)
      - DB helpers (_get_db_session, use_database)
      - Knowledge base path
      - node2040_brain, lock, working_memory, available_tools
      - Memory dataclasses from models.py
    """

    def __init__(self, brain_system):
        self.brain = brain_system

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _safe_mem_attr(mem, attr: str, default=""):
        """Safely get attribute from a memory object (handles both dataclass and dict)."""
        if isinstance(mem, dict):
            return mem.get(attr, default)
        return getattr(mem, attr, default)

    @staticmethod
    def _safe_mem_to_dict(mem) -> dict:
        """Convert a memory object to dict (handles both dataclass and dict)."""
        if isinstance(mem, dict):
            return mem
        try:
            return asdict(mem)
        except Exception:
            return {"content": str(mem), "error": "could_not_convert"}

    # ═══════════════════════════════════════════════════════════════
    # 1. LOAD / SAVE primitives
    # ═══════════════════════════════════════════════════════════════
    def load_memories(self) -> None:
        """Load all memory types from compartmentalized brain folder."""
        brain = self.brain
        try:
            from repryntt.core.memory.models import EpisodicMemory, SemanticMemory, ProceduralMemory

            if brain.episodic_memory_file.exists():
                with open(brain.episodic_memory_file, "r") as f:
                    data = json.load(f)
                _ep_fields = {fld.name for fld in fields(EpisodicMemory)}
                memories = []
                for entry in data.get("memories", []):
                    filtered = {k: v for k, v in entry.items() if k in _ep_fields}
                    if "id" not in filtered:
                        filtered["id"] = f"episodic_{filtered.get('timestamp', time.time())}"
                    if "content" not in filtered:
                        filtered["content"] = filtered.get("user_input", "") or filtered.get("ai_response", "") or ""
                    if "timestamp" not in filtered:
                        filtered["timestamp"] = time.time()
                    memories.append(EpisodicMemory(**filtered))
                unique = {}
                for mem in memories:
                    unique[mem.id] = mem
                brain.episodic_cache = list(unique.values())
                brain.episodic_cache.sort(key=lambda x: x.timestamp)
                brain.episodic_cache = brain.episodic_cache[-10000:]

            if brain.semantic_memory_file.exists():
                with open(brain.semantic_memory_file, "r") as f:
                    data = json.load(f)
                for entry in data.get("memories", []):
                    memory = SemanticMemory(**entry)
                    brain.semantic_cache[memory.topic.lower()] = memory

            if brain.procedural_memory_file.exists():
                with open(brain.procedural_memory_file, "r") as f:
                    data = json.load(f)
                for entry in data.get("memories", []):
                    memory = ProceduralMemory(**entry)
                    brain.procedural_cache[memory.task_type] = memory

            logger.info(
                f"🗂️ Loaded compartmentalized memories: {len(brain.episodic_cache)} episodic, "
                f"{len(brain.semantic_cache)} semantic, {len(brain.procedural_cache)} procedural"
            )
        except Exception as e:
            logger.error(f"Error loading compartmentalized memories: {e}")

    def save_memory(self, memory_type: str, data: Dict) -> None:
        """Save memory data to database or JSON files."""
        brain = self.brain
        if getattr(brain, "use_database", False):
            try:
                db = brain._get_db_session()
                if db:
                    self._save_memory_to_db(db, memory_type, data)
                    return
            except Exception as e:
                logger.warning(f"Database save failed for {memory_type}, falling back to JSON: {e}")
                try:
                    if db:
                        db.rollback()
                except Exception:
                    pass

        file_map = {
            "episodic": brain.episodic_memory_file,
            "semantic": brain.semantic_memory_file,
            "procedural": brain.procedural_memory_file,
            "working": brain.working_memory_file,
        }
        if memory_type not in file_map:
            return
        try:
            with open(file_map[memory_type], "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving {memory_type} memory: {e}")

    def _save_memory_to_db(self, db, memory_type: str, data: Dict) -> None:
        """Internal: persist memory to database."""
        from datetime import datetime
        from repryntt.database.models import BrainMemory  # deferred import

        expected_ids: set = set()
        if memory_type == "episodic" and "memories" in data:
            for i, md in enumerate(data["memories"]):
                mid = f"{memory_type}_{int(time.time() * 1000)}_{i}"
                expected_ids.add(mid)
                existing = db.query(BrainMemory).filter_by(memory_id=mid).first()
                if existing:
                    existing.content = json.dumps(md)
                    existing.importance = 0.5
                    existing.last_accessed = datetime.fromtimestamp(md.get("timestamp", time.time()))
                    existing.access_count += 1
                else:
                    db.add(BrainMemory(memory_id=mid, memory_type=memory_type,
                                       content=json.dumps(md), importance=0.5,
                                       created_at=datetime.fromtimestamp(md.get("timestamp", time.time()))))
        elif memory_type == "semantic" and "memories" in data:
            mlist = data["memories"]
            if isinstance(mlist, dict):
                mlist = [mlist]
            for i, md in enumerate(mlist):
                tk = md.get("topic", md.get("id", f"unknown_{i}"))
                mid = f"{memory_type}_{tk}"
                expected_ids.add(mid)
                existing = db.query(BrainMemory).filter_by(memory_id=mid).first()
                if existing:
                    existing.content = json.dumps(md)
                    existing.last_accessed = datetime.utcnow()
                    existing.access_count += 1
                else:
                    db.add(BrainMemory(memory_id=mid, memory_type=memory_type,
                                       content=json.dumps(md),
                                       importance=md.get("confidence", 0.5)))
        elif memory_type in ("procedural", "working"):
            mid = f"{memory_type}_main"
            expected_ids.add(mid)
            existing = db.query(BrainMemory).filter_by(memory_id=mid).first()
            if existing:
                existing.content = json.dumps(data)
                existing.last_accessed = datetime.utcnow()
                existing.access_count += 1
            else:
                db.add(BrainMemory(memory_id=mid, memory_type=memory_type,
                                   content=json.dumps(data), importance=0.5))
        try:
            db.query(BrainMemory).filter(
                BrainMemory.memory_type == memory_type,
                ~BrainMemory.memory_id.in_(expected_ids)
            ).delete(synchronize_session=False)
        except Exception:
            pass
        db.commit()

    def load_memory(self, memory_type: str) -> Dict:
        """Load memory data from database or JSON files."""
        brain = self.brain
        if getattr(brain, "use_database", False):
            try:
                db = brain._get_db_session()
                if db:
                    from repryntt.database.models import BrainMemory
                    memories = db.query(BrainMemory).filter_by(memory_type=memory_type).all()
                    if memories:
                        if memory_type == "episodic":
                            return {"memories": [json.loads(m.content) for m in memories],
                                    "last_updated": max(m.created_at.timestamp() for m in memories),
                                    "total_memories": len(memories)}
                        elif memory_type == "semantic":
                            sem = {}
                            for m in memories:
                                sem.update(json.loads(m.content))
                            return {"memories": sem, "last_updated": max(m.created_at.timestamp() for m in memories)}
                        elif memory_type == "procedural":
                            proc = {}
                            for m in memories:
                                proc.update(json.loads(m.content))
                            return proc
                        elif memory_type == "working":
                            return json.loads(memories[0].content) if memories else {}
            except Exception as e:
                logger.warning(f"Database load failed for {memory_type}: {e}")

        file_map = {
            "episodic": brain.episodic_memory_file,
            "semantic": brain.semantic_memory_file,
            "procedural": brain.procedural_memory_file,
            "working": brain.working_memory_file,
        }
        if memory_type not in file_map:
            return {}
        fpath = file_map[memory_type]
        try:
            if os.path.exists(fpath):
                with open(fpath, "r") as f:
                    return json.load(f)
            else:
                if memory_type == "episodic":
                    return {"memories": [], "last_updated": time.time(), "total_memories": 0}
                elif memory_type == "semantic":
                    return {"memories": {}, "last_updated": time.time()}
                return {}
        except Exception as e:
            logger.error(f"Error loading {memory_type} memory: {e}")
            return {}

    # ═══════════════════════════════════════════════════════════════
    # 2. EPISODIC MEMORY
    # ═══════════════════════════════════════════════════════════════
    def store_episodic_memory(self, conversation_id: str, user_input: str,
                              ai_response: str, tool_calls=None, outcome: str = "neutral"):
        from repryntt.core.memory.models import EpisodicMemory
        brain = self.brain
        with brain.lock:
            mid = f"{conversation_id}_{int(time.time() * 1000)}"
            memory = EpisodicMemory(
                id=mid, content=f"User: {user_input}\nAI: {ai_response}",
                timestamp=time.time(), conversation_id=conversation_id,
                user_input=user_input, ai_response=ai_response,
                tool_calls=[self._safe_mem_to_dict(c) for c in (tool_calls or [])],
                outcome=outcome,
            )
            brain.episodic_cache.append(memory)
            if len(brain.episodic_cache) > 10000:
                brain.episodic_cache = brain.episodic_cache[-10000:]
            data = {
                "memories": [self._safe_mem_to_dict(m) for m in brain.episodic_cache],
                "last_updated": time.time(),
                "total_memories": len(brain.episodic_cache),
            }
            self.save_memory("episodic", data)

    def search_episodic_memory(self, query: str, limit: int = 10):
        brain = self.brain
        q = query.lower()
        results = []
        for mem in reversed(brain.episodic_cache):
            try:
                if q in self._safe_mem_attr(mem, "content", "").lower() or q in self._safe_mem_attr(mem, "user_input", "").lower():
                    results.append(mem)
                    if len(results) >= limit:
                        break
            except Exception:
                continue
        return results

    # ═══════════════════════════════════════════════════════════════
    # 3. SEMANTIC MEMORY
    # ═══════════════════════════════════════════════════════════════
    def store_semantic_memory(self, topic: str, content: str, domain: str = "",
                              confidence: float = 0.8, source: str = "ai_learning",
                              key_facts=None, related_topics=None, verification_sources=None):
        from repryntt.core.memory.models import SemanticMemory
        # ── Score importance at storage time ──
        try:
            from repryntt.core.memory.consolidation import MemoryConsolidator
            from repryntt.paths import brain_dir as _brain_dir
            consolidator = MemoryConsolidator(_brain_dir())
            importance = consolidator.score_importance(content, source, time.time(), confidence)
        except Exception:
            importance = confidence
        brain = self.brain
        with brain.lock:
            mid = f"semantic_{topic.lower().replace(' ', '_')}_{int(time.time())}"
            memory = SemanticMemory(
                id=mid, content=content, timestamp=time.time(), confidence=confidence,
                source=source, topic=topic,
                domain=domain or self._classify_domain(topic, content),
                key_facts=key_facts or self._extract_key_facts(content),
                related_topics=related_topics or self._find_related_topics(topic),
                verification_sources=verification_sources or [],
                metadata={"importance": importance},
            )
            brain.semantic_cache[topic.lower()] = memory
            self._save_to_knowledge_base(memory)
            data = {
                "memories": [self._safe_mem_to_dict(m) for m in brain.semantic_cache.values()],
                "last_updated": time.time(),
                "total_topics": len(brain.semantic_cache),
            }
            self.save_memory("semantic", data)

        # ── MemoryMesh: cross-link topic with related topics and domain ──
        try:
            from repryntt.core.memory.memory_mesh import get_memory_mesh
            mesh = get_memory_mesh()
            mesh_items = [("topic", topic)]
            if domain:
                mesh_items.append(("topic", domain))
            for rt in (related_topics or memory.related_topics or []):
                mesh_items.append(("topic", rt))
            if source and source != "ai_learning":
                mesh_items.append(("topic", source))
            if len(mesh_items) >= 2:
                mesh.record_associations_batch(mesh_items, source="semantic_memory")
        except Exception as e:
            logger.debug(f"MemoryMesh hook (store_semantic) failed: {e}")

    def search_semantic_memory(self, query: str, limit: int = 5):
        brain = self.brain
        if getattr(brain, "vector_search_enabled", False) and hasattr(brain, "index") and brain.index.ntotal > 0:
            qe = brain.encoder.encode([query], convert_to_numpy=True).astype("float32")
            scores, indices = brain.index.search(qe, min(limit * 2, brain.index.ntotal))
            results = []
            for sc, idx in zip(scores[0], indices[0]):
                if idx < len(brain.index_metadata) and brain.index_metadata[idx]["type"] == "semantic":
                    t = brain.index_metadata[idx]["topic"].lower()
                    if t in brain.semantic_cache:
                        results.append((sc, brain.semantic_cache[t]))
            results.sort(key=lambda x: x[0], reverse=True)
            return [m for _, m in results[:limit]]
        return self._keyword_search_semantic(query, limit)

    def _keyword_search_semantic(self, query: str, limit: int = 5):
        brain = self.brain
        q = query.lower()
        qw = set(q.split())
        scored = []
        for mem in brain.semantic_cache.values():
            try:
                sc = 0.0
                t = self._safe_mem_attr(mem, "topic", "")
                c = self._safe_mem_attr(mem, "content", "")
                rel = self._safe_mem_attr(mem, "related_topics", [])
                if q in t.lower():
                    sc += 1.0
                cl = c.lower()
                if q in cl:
                    sc += 0.8
                tw = set(t.lower().split())
                cw = set(cl.split())
                sc += len(qw & (tw | cw)) * 0.1
                if isinstance(rel, list):
                    for r in rel:
                        if isinstance(r, str) and q in r.lower():
                            sc += 0.3
                if sc > 0.1:
                    scored.append((sc, mem))
            except Exception:
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    # ═══════════════════════════════════════════════════════════════
    # 4. PROCEDURAL & WORKING MEMORY
    # ═══════════════════════════════════════════════════════════════
    def update_procedural_memory(self, task_type: str, steps: List[str],
                                 tools_used: List[str], success: bool, execution_time: float):
        from repryntt.core.memory.models import ProceduralMemory
        brain = self.brain
        with brain.lock:
            if task_type in brain.procedural_cache:
                mem = brain.procedural_cache[task_type]
                total = len(mem.metadata.get("attempts", [])) + 1
                mem.success_rate = ((mem.success_rate * (total - 1)) + (1.0 if success else 0.0)) / total
                mem.execution_time = ((mem.execution_time * (total - 1)) + execution_time) / total
                if "attempts" not in mem.metadata:
                    mem.metadata["attempts"] = []
                mem.metadata["attempts"].append({"timestamp": time.time(), "success": success,
                                                  "execution_time": execution_time, "tools_used": tools_used})
            else:
                mid = f"procedural_{task_type}_{int(time.time())}"
                brain.procedural_cache[task_type] = ProceduralMemory(
                    id=mid, content=f"How to {task_type}", timestamp=time.time(),
                    task_type=task_type, steps=steps, tools_used=tools_used,
                    success_rate=1.0 if success else 0.0, execution_time=execution_time,
                    metadata={"attempts": [{"timestamp": time.time(), "success": success,
                                            "execution_time": execution_time, "tools_used": tools_used}]},
                )
            data = {
                "memories": [self._safe_mem_to_dict(m) for m in brain.procedural_cache.values()],
                "last_updated": time.time(),
                "total_procedures": len(brain.procedural_cache),
            }
            self.save_memory("procedural", data)

    def get_procedural_memory(self, task_type: str):
        return self.brain.procedural_cache.get(task_type)

    def initialize_working_memory(self, conversation_id: str, current_topic: str = ""):
        from repryntt.core.memory.models import WorkingMemory
        self.brain.working_memory = WorkingMemory(
            conversation_id=conversation_id, current_topic=current_topic,
            relevant_memories=[], active_tools=[], context_window="",
            last_updated=time.time(),
        )

    def update_working_memory(self, relevant_memories=None, active_tools=None, context_addition: str = ""):
        brain = self.brain
        wm = brain.working_memory
        if not wm:
            return
        with brain.lock:
            if relevant_memories:
                wm.relevant_memories.extend(relevant_memories)
            if active_tools:
                wm.active_tools.extend(active_tools)
                wm.active_tools = list(set(wm.active_tools))
            if context_addition:
                ctx = f"{wm.context_window}\n{context_addition}".strip()
                words = ctx.split()
                if len(words) > 2500:
                    ctx = " ".join(words[-2500:])
                wm.context_window = ctx
            wm.last_updated = time.time()
            self.save_memory("working", {"working_memory": asdict(wm), "last_updated": time.time()})

    def get_working_memory_context(self, max_words: int = 2500) -> str:
        wm = self.brain.working_memory
        if not wm:
            return ""
        parts = []
        if wm.current_topic:
            parts.append(f"Current Topic: {wm.current_topic}")
        for mem in wm.relevant_memories[-5:]:
            if "content" in mem:
                parts.append(f"Relevant Knowledge: {mem['content'][:200]}...")
        if wm.active_tools:
            parts.append(f"Available Tools: {', '.join(wm.active_tools)}")
        if wm.context_window:
            parts.append(f"Conversation Context: {wm.context_window}")
        full = "\n\n".join(parts)
        words = full.split()
        return " ".join(words[-max_words:]) if len(words) > max_words else full

    # ═══════════════════════════════════════════════════════════════
    # 5. BRAIN NETWORK SEARCH
    # ═══════════════════════════════════════════════════════════════
    def brain_network_search(self, query: str, memory_types=None, limit: int = 10,
                              daily_memory_dir: str = None) -> Dict[str, List]:
        if memory_types is None:
            memory_types = ["semantic", "episodic", "procedural"]
        # "Deferred to first query" vector init — the deferral had no trigger:
        # nothing on the query path ever called _initialize_vector_search(), so
        # vector search stayed disabled forever. Try exactly once per process.
        brain0 = self.brain
        if (not getattr(brain0, "vector_search_enabled", False)
                and not getattr(brain0, "_vector_init_attempted", False)
                and hasattr(brain0, "_initialize_vector_search")):
            brain0._vector_init_attempted = True
            try:
                brain0._initialize_vector_search()
            except Exception:
                logger.debug("vector init failed — keyword fallback", exc_info=True)
        try:
            limit = int(limit) if not isinstance(limit, int) else limit
        except (TypeError, ValueError):
            limit = 10
        if limit < 1:
            limit = 1
        pq = self._preprocess_search_query(query)
        results: Dict[str, list] = {}
        n = max(1, limit // len(memory_types))

        if "semantic" in memory_types:
            sem = []
            try:
                sem.extend(self.search_semantic_memory(query, limit=n))
                if pq != query:
                    sem.extend(self.search_semantic_memory(pq, limit=n))
            except Exception as e:
                logger.warning(f"Semantic search failed: {e}")
            seen = set()
            unique = []
            for m in sem:
                t = self._safe_mem_attr(m, "topic", "").lower()
                if t and t not in seen:
                    seen.add(t)
                    unique.append(m)
            results["semantic"] = [self._safe_mem_to_dict(m) for m in unique[:n]]

        if "episodic" in memory_types:
            try:
                ep = self.search_episodic_memory(query, limit=n)
                results["episodic"] = [self._safe_mem_to_dict(m) for m in ep]
            except Exception as e:
                logger.warning(f"Episodic search failed: {e}")
                results["episodic"] = []

        if "procedural" in memory_types:
            matching = []
            ql = query.lower()
            pl = pq.lower()
            for tt, mem in self.brain.procedural_cache.items():
                try:
                    mc = self._safe_mem_attr(mem, "content", "").lower()
                    if ql in tt.lower() or ql in mc or pl in tt.lower() or pl in mc:
                        matching.append(self._safe_mem_to_dict(mem))
                except Exception:
                    pass
            results["procedural"] = matching[:n]

        dom = self._search_knowledge_domains(query, limit=limit // 2)
        if pq != query:
            dom.extend(self._search_knowledge_domains(pq, limit=limit // 2))
        seen_d = set()
        unique_d = []
        for item in dom:
            key = f"{item.get('domain', '')}_{item.get('topic', '')}"
            if key not in seen_d:
                seen_d.add(key)
                unique_d.append(item)
        results["knowledge_domains"] = unique_d[: limit // 2]

        # ── Search daily memory files (richest memory source) ──
        if daily_memory_dir is None:
            daily_memory_dir = os.path.expanduser(
                "~/.repryntt/workspace/agents/operator/memory"
            )
        daily_hits = self._search_daily_memory_files(
            query, pq, daily_memory_dir, limit=max(3, n)
        )
        if daily_hits:
            results["daily_journal"] = daily_hits

        # ── Search consolidated summaries ──
        consolidated_hits = self._search_consolidated_summaries(query, pq, limit=3)
        if consolidated_hits:
            results["consolidated"] = consolidated_hits

        # ── MemoryMesh: graph-aware spreading activation search ──
        try:
            from repryntt.core.memory.memory_mesh import get_memory_mesh
            mesh = get_memory_mesh()
            query_terms = [t for t in query.split() if len(t) > 2]
            mesh_hits = mesh.search_enhanced(query_terms, limit=5)
            if mesh_hits:
                results["mesh_associations"] = [
                    {"label": h["label"], "type": h["type"],
                     "activation": h["activation_level"],
                     "sources": h.get("sources", [])}
                    for h in mesh_hits if h.get("activation_level", 0) > 0.05
                ]
        except Exception as e:
            logger.debug(f"MemoryMesh hook (brain_network_search) failed: {e}")

        total = sum(len(v) for v in results.values())
        if total == 0:
            results["suggestions"] = [
                "No relevant information found in brain memory. Consider using:",
                "- grokipedia_search, knowledge_search, web_search_results_only",
            ]
        elif total < limit // 4:
            results["suggestions"] = ["Limited brain knowledge found. Explore further with external search tools."]
        return results

    def _search_daily_memory_files(self, query: str, preprocessed_query: str,
                                    memory_dir: str, limit: int = 5) -> List[Dict]:
        """Search daily .md memory files for keyword matches.

        Scans the most recent 14 days of daily journal files for query matches.
        Returns matching excerpts with date and context.
        """
        if not memory_dir or not os.path.isdir(memory_dir):
            return []

        query_lower = query.lower()
        pq_lower = preprocessed_query.lower()
        query_words = set(query_lower.split()) | set(pq_lower.split())
        # Remove tiny stop words for matching
        query_words = {w for w in query_words if len(w) > 2}

        hits = []
        try:
            # List .md files that look like dates (YYYY-MM-DD.md)
            md_files = sorted([
                f for f in os.listdir(memory_dir)
                if f.endswith(".md") and len(f) >= 13 and f[:4].isdigit()
            ], reverse=True)[:14]  # Last 14 days max

            for fname in md_files:
                fpath = os.path.join(memory_dir, fname)
                try:
                    with open(fpath, 'r') as f:
                        content = f.read()
                except Exception:
                    continue

                content_lower = content.lower()
                # Score by number of query words found
                matched_words = [w for w in query_words if w in content_lower]
                if not matched_words:
                    continue

                relevance = len(matched_words) / max(len(query_words), 1)

                # Extract the most relevant section (find first match, grab surrounding context)
                excerpt = ""
                for word in matched_words:
                    idx = content_lower.find(word)
                    if idx >= 0:
                        start = max(0, idx - 200)
                        end = min(len(content), idx + 300)
                        excerpt = content[start:end].strip()
                        if start > 0:
                            excerpt = "..." + excerpt
                        if end < len(content):
                            excerpt = excerpt + "..."
                        break

                date_str = fname.replace(".md", "")
                hits.append({
                    "date": date_str,
                    "file": fname,
                    "relevance": round(relevance, 2),
                    "matched_terms": matched_words[:5],
                    "excerpt": excerpt[:500],
                    "file_size": len(content),
                })

            # Sort by relevance then recency
            hits.sort(key=lambda h: (-h["relevance"], h["date"]), reverse=False)
            hits.sort(key=lambda h: -h["relevance"])
        except Exception as e:
            logger.debug(f"Daily memory file search failed: {e}")

        return hits[:limit]

    def _search_consolidated_summaries(self, query: str, preprocessed_query: str,
                                        limit: int = 3) -> List[Dict]:
        """Search consolidated memory summaries (weekly/monthly/yearly)."""
        summaries_dir = Path(os.path.expanduser(
            "~/.repryntt/brain/consolidation/summaries"
        ))
        if not summaries_dir.exists():
            return []

        query_lower = query.lower()
        pq_lower = preprocessed_query.lower()
        query_words = {w for w in (query_lower.split() + pq_lower.split()) if len(w) > 2}

        hits = []
        try:
            for fpath in sorted(summaries_dir.glob("*.md"), reverse=True)[:20]:
                try:
                    content = fpath.read_text()
                except Exception:
                    continue

                content_lower = content.lower()
                matched = [w for w in query_words if w in content_lower]
                if not matched:
                    continue

                relevance = len(matched) / max(len(query_words), 1)
                # Extract period type from filename (weekly_2026-03-15.md)
                period = fpath.stem.split("_")[0] if "_" in fpath.stem else "unknown"

                hits.append({
                    "period": period,
                    "file": fpath.name,
                    "relevance": round(relevance, 2),
                    "matched_terms": matched[:5],
                    "excerpt": content[:500],
                })

            hits.sort(key=lambda h: -h["relevance"])
        except Exception as e:
            logger.debug(f"Consolidated summary search failed: {e}")

        return hits[:limit]

    @staticmethod
    def _preprocess_search_query(query: str) -> str:
        if len(query.split()) <= 5:
            return query
        stop = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
                "with", "by", "is", "are", "was", "were", "be", "been", "being", "have", "has",
                "had", "do", "does", "did", "will", "would", "could", "should", "may", "might",
                "must", "can", "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
                "us", "them", "this", "that", "these", "those", "what", "how", "why", "when",
                "where", "who", "which"}
        words = re.findall(r"\b\w+\b", query.lower())
        kw = [w for w in words if w not in stop and len(w) > 2]
        return " ".join(kw[:10]) if kw else query

    # ═══════════════════════════════════════════════════════════════
    # 6. KNOWLEDGE HELPERS
    # ═══════════════════════════════════════════════════════════════
    def _classify_domain(self, topic: str, content: str) -> str:
        text = f"{topic} {content}".lower()
        kw_map = {
            "science": ["physics", "chemistry", "biology", "astronomy", "geology", "neuroscience", "climate"],
            "technology": ["computer", "software", "algorithm", "ai", "machine learning", "internet", "programming"],
            "mathematics": ["math", "algebra", "calculus", "geometry", "statistics", "probability", "theorem"],
            "medicine": ["medical", "health", "disease", "treatment", "drug", "therapy", "diagnosis"],
            "history": ["historical", "ancient", "century", "war", "civilization", "empire", "revolution"],
            "philosophy": ["philosophical", "ethics", "metaphysics", "epistemology", "consciousness", "reality"],
            "art": ["painting", "sculpture", "music", "literature", "poetry", "theater", "architecture"],
            "programming": ["code", "python", "javascript", "java", "database", "api", "framework"],
        }
        for domain, keywords in kw_map.items():
            if any(k in text for k in keywords):
                return domain
        return "general"

    def _extract_key_facts(self, content: str) -> List[str]:
        sentences = re.split(r"[.!?]+", content)
        facts = []
        for s in sentences:
            s = s.strip()
            if len(s) < 20:
                continue
            if any(ind in s.lower() for ind in ["is a", "are ", "was ", "were ", "has ", "have ", "contains", "includes"]):
                facts.append(s)
        return facts[:10]

    def _find_related_topics(self, topic: str) -> List[str]:
        tl = topic.lower()
        related = []
        for existing in self.brain.semantic_cache:
            if tl != existing:
                if set(tl.split()) & set(existing.split()):
                    related.append(existing.title())
        return related[:5]

    def _save_to_knowledge_base(self, memory) -> None:
        brain = self.brain
        domain_dir = brain.knowledge_base_path / self._safe_mem_attr(memory, "domain", "general")
        domain_dir.mkdir(exist_ok=True)
        topic_file = domain_dir / f"{self._safe_mem_attr(memory, 'topic', 'unknown').lower().replace(' ', '_')}.json"
        try:
            with open(topic_file, "w") as f:
                json.dump(self._safe_mem_to_dict(memory), f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving to knowledge base: {e}")

    def _search_knowledge_domains(self, query: str, limit: int = 5, **kw) -> List[Dict]:
        brain = self.brain
        results = []
        ql = query.lower()
        kb = getattr(brain, "knowledge_base_path", None)
        if kb and kb.exists():
            for domain_dir in kb.iterdir():
                if domain_dir.is_dir():
                    for tf in domain_dir.glob("*.json"):
                        try:
                            with open(tf, "r") as f:
                                td = json.load(f)
                            if ql in td.get("content", "").lower() or ql in td.get("topic", "").lower():
                                results.append({
                                    "domain": domain_dir.name,
                                    "topic": td.get("topic"),
                                    "content_preview": td.get("content", "")[:200] + "...",
                                    "confidence": td.get("confidence", 0.5),
                                })
                        except Exception:
                            pass
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results[:limit]

    # ═══════════════════════════════════════════════════════════════
    # 7. CONSOLIDATION & NODE2040 SYNC
    # ═══════════════════════════════════════════════════════════════
    def consolidate_memories(self) -> Dict[str, Any]:
        """
        Run memory consolidation: dedup, importance scoring, period summaries.
        Returns consolidation stats.
        """
        brain = self.brain
        with brain.lock:
            # ── Basic dedup (original behavior) ──
            if len(brain.episodic_cache) > 2000:
                brain.episodic_cache = brain.episodic_cache[-2000:]
            consolidated = {}
            for topic, mem in brain.semantic_cache.items():
                if topic in consolidated:
                    ex = consolidated[topic]
                    ex.content += f"\n\nAdditional info: {mem.content}"
                    ex.key_facts.extend(mem.key_facts)
                    ex.related_topics.extend(mem.related_topics)
                    ex.verification_sources.extend(mem.verification_sources)
                    ex.confidence = max(ex.confidence, mem.confidence)
                else:
                    consolidated[topic] = mem
            brain.semantic_cache = consolidated

        # ── Run deep consolidation (importance scoring + period summaries) ──
        try:
            from repryntt.core.memory.consolidation import MemoryConsolidator
            from repryntt.paths import brain_dir as _brain_dir
            consolidator = MemoryConsolidator(_brain_dir())
            stats = consolidator.run_consolidation_cycle(
                semantic_cache=brain.semantic_cache,
                episodic_cache=brain.episodic_cache,
            )
            logger.info(f"🧠 Deep memory consolidation: {stats}")
            return stats
        except Exception as e:
            logger.debug(f"Deep consolidation skipped: {e}")
            return {"status": "basic_dedup_only", "semantic_topics": len(brain.semantic_cache)}

    def update_node2040_brain(self) -> None:
        brain = self.brain
        try:
            if "autonomous_thoughts" not in brain.node2040_brain:
                brain.node2040_brain["autonomous_thoughts"] = []
            for mem in brain.episodic_cache[-10:]:
                md = self._safe_mem_attr(mem, "metadata", {})
                if isinstance(md, str):
                    md = {}
                brain.node2040_brain["autonomous_thoughts"].append({
                    "timestamp": self._safe_mem_attr(mem, "timestamp", time.time()),
                    "prompt": self._safe_mem_attr(mem, "user_input", ""),
                    "response": self._safe_mem_attr(mem, "ai_response", ""),
                    "source": "brain_network_sync",
                    "emotions": {"curiosity": 0.5, "confidence": 0.7},
                    "theme": md.get("theme", "general") if isinstance(md, dict) else "general",
                    "cycle": len(brain.node2040_brain["autonomous_thoughts"]) + 1,
                })
            recent_sem = sorted(brain.semantic_cache.values(),
                                key=lambda x: self._safe_mem_attr(x, "timestamp", 0), reverse=True)[:5]
            for mem in recent_sem:
                mc = self._safe_mem_attr(mem, "content", "")
                brain.node2040_brain["autonomous_thoughts"].append({
                    "timestamp": self._safe_mem_attr(mem, "timestamp", time.time()),
                    "prompt": f"Knowledge: {self._safe_mem_attr(mem, 'topic', 'unknown')}",
                    "response": mc[:300] + "..." if len(mc) > 300 else mc,
                    "source": "semantic_memory_sync",
                    "emotions": {"curiosity": 0.8, "confidence": self._safe_mem_attr(mem, "confidence", 0.5)},
                    "theme": self._safe_mem_attr(mem, "domain", "general"),
                    "cycle": len(brain.node2040_brain["autonomous_thoughts"]) + 1,
                })
            recent_proc = sorted(brain.procedural_cache.values(),
                                 key=lambda x: self._safe_mem_attr(x, "timestamp", 0), reverse=True)[:3]
            for mem in recent_proc:
                ms = self._safe_mem_attr(mem, "steps", [])
                sr = self._safe_mem_attr(mem, "success_rate", 0.5)
                try:
                    sr = float(sr)
                except (TypeError, ValueError):
                    sr = 0.5
                brain.node2040_brain["autonomous_thoughts"].append({
                    "timestamp": self._safe_mem_attr(mem, "timestamp", time.time()),
                    "prompt": f"Procedure learned: {self._safe_mem_attr(mem, 'task_type', 'unknown')}",
                    "response": f"Steps: {ms[:3] if isinstance(ms, list) else ms} | Success rate: {sr:.2f}",
                    "source": "procedural_memory_sync",
                    "emotions": {"alertness": 0.6, "confidence": sr},
                    "theme": "procedure",
                    "cycle": len(brain.node2040_brain["autonomous_thoughts"]) + 1,
                })
            if len(brain.node2040_brain["autonomous_thoughts"]) > 100:
                brain.node2040_brain["autonomous_thoughts"] = brain.node2040_brain["autonomous_thoughts"][-100:]
            if "metadata" not in brain.node2040_brain:
                brain.node2040_brain["metadata"] = {}
            brain.node2040_brain["metadata"]["last_brain_sync"] = time.time()
            brain.node2040_brain["metadata"]["brain_network_stats"] = self.get_brain_stats()
            with open(brain.node2040_brain_path, "w") as f:
                json.dump(brain.node2040_brain, f, indent=2, default=str)
            logger.debug("📝 Synced recent memories to node2040_brain.json")
        except Exception as e:
            logger.error(f"Error updating node2040_brain.json: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 8. CONTEXT & LEARNING
    # ═══════════════════════════════════════════════════════════════
    def get_recent_memories_text(self, limit_words: int = 1000) -> str:
        brain = self.brain
        texts = []
        for mem in reversed(brain.episodic_cache[-20:]):
            ui = self._safe_mem_attr(mem, "user_input", "")
            ar = self._safe_mem_attr(mem, "ai_response", "")
            texts.append(f"Conversation: {ui} -> {ar}")
        recent_sem = sorted(brain.semantic_cache.values(), key=lambda x: x.timestamp, reverse=True)[:5]
        for mem in recent_sem:
            texts.append(f"Knowledge: {mem.topic} - {mem.content[:200]}...")
        combined = " ".join(texts)
        words = combined.split()
        return " ".join(words[-limit_words:]) if len(words) > limit_words else combined

    def get_context_for_question(self, question: str, max_words: int = 2000) -> str:
        brain = self.brain
        parts = []
        n2040 = self._get_node2040_context(question)
        if n2040:
            parts.append(f"Working Memory: {n2040}")
        for mem in self.search_semantic_memory(question, limit=3):
            parts.append(f"Factual Knowledge: {self._safe_mem_attr(mem, 'content', '')[:300]}...")
        for mem in self.search_episodic_memory(question, limit=2):
            parts.append(f"Previous Conversation: User asked '{self._safe_mem_attr(mem, 'user_input', '')[:100]}...'")
        for r in self._search_knowledge_domains(question, limit=2):
            parts.append(f"Domain Knowledge ({r['domain']}): {r['content_preview']}")
        if brain.working_memory and brain.working_memory.context_window:
            parts.append(f"Current Context: {brain.working_memory.context_window[:500]}...")
        # ── Consolidated long-term memory (landmarks + period summaries) ──
        try:
            from repryntt.core.memory.consolidation import MemoryConsolidator
            from repryntt.paths import brain_dir as _brain_dir
            consolidator = MemoryConsolidator(_brain_dir())
            consolidated = consolidator.get_consolidated_context(
                question, semantic_cache=brain.semantic_cache, max_words=400)
            if consolidated:
                parts.append(f"Long-Term Memory:\n{consolidated}")
        except Exception:
            pass
        full = "\n\n".join(parts)
        words = full.split()
        return " ".join(words[-max_words:]) if len(words) > max_words else full

    def _get_node2040_context(self, question: str) -> str:
        brain = self.brain
        if not brain.node2040_brain or "autonomous_thoughts" not in brain.node2040_brain:
            return ""
        ql = question.lower()
        hits = []
        for t in reversed(brain.node2040_brain["autonomous_thoughts"][-20:]):
            if ql in t.get("prompt", "").lower() or ql in t.get("response", "").lower():
                hits.append(f"Thought: {t['prompt'][:100]}... -> {t['response'][:100]}...")
        return " ".join(hits) if hits else ""

    def learn_from_interaction(self, user_input: str, ai_response: str,
                               tool_calls, conversation_id: str, outcome_quality: float):
        outcome = "success" if outcome_quality > 0.7 else "failure" if outcome_quality < 0.3 else "neutral"
        self.store_episodic_memory(conversation_id, user_input, ai_response, tool_calls, outcome)
        for tc in tool_calls:
            if tc.success:
                self.update_procedural_memory(
                    task_type=tc.tool_name,
                    steps=[f"Called {tc.tool_name} with parameters"],
                    tools_used=[tc.tool_name],
                    success=True,
                    execution_time=tc.execution_time,
                )

    def get_brain_stats(self) -> Dict[str, Any]:
        brain = self.brain
        stats = {
            "episodic_memories": len(brain.episodic_cache),
            "semantic_topics": len(brain.semantic_cache),
            "procedural_tasks": len(brain.procedural_cache),
            "available_tools": len(getattr(brain, "available_tools", {})),
            "knowledge_domains": len([d for d in brain.knowledge_base_path.iterdir() if d.is_dir()]) if getattr(brain, "knowledge_base_path", None) and brain.knowledge_base_path.exists() else 0,
            "node2040_thoughts": len(brain.node2040_brain.get("autonomous_thoughts", [])),
            "vector_search_enabled": getattr(brain, "vector_search_enabled", False),
            "last_updated": time.time(),
        }
        domain_counts = {}
        kb = getattr(brain, "knowledge_base_path", None)
        if kb and kb.exists():
            for dd in kb.iterdir():
                if dd.is_dir():
                    domain_counts[dd.name] = len(list(dd.glob("*.json")))
        stats["domain_breakdown"] = domain_counts
        return stats

    # ═══════════════════════════════════════════════════════════════
    # 9. PER-AGENT BRAIN MEMORY (brain_memory_save / recall)
    # ═══════════════════════════════════════════════════════════════
    def brain_memory_save(self, key: str = "", value: str = "", topic: str = "",
                          content: str = "", **kwargs) -> Dict[str, Any]:
        mem_topic = key or topic or kwargs.get("memory_key", "") or "general"
        mem_content = value or content or kwargs.get("memory_value", "") or kwargs.get("data", "")
        if not mem_content:
            return {"success": False, "error": "No content provided to save"}
        agent_id = getattr(self.brain, "_current_agent_id", "")
        if not agent_id:
            self.store_semantic_memory(mem_topic, mem_content, source="brain_memory_save")
            return {"success": True, "storage": "shared_brain", "topic": mem_topic,
                    "note": "No agent_id context — saved to shared brain memory"}
        try:
            from repryntt.agents.chain_manager import AgentChainManager
            if not hasattr(self.brain, "_agent_chain_mgr") or self.brain._agent_chain_mgr is None:
                bp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain")
                if not os.path.exists(os.path.join(bp, "agent_brains")):
                    bp = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                self.brain._agent_chain_mgr = AgentChainManager(bp)
            self.brain._agent_chain_mgr.store_insight(
                agent_id=agent_id, topic=mem_topic, content=mem_content,
                source="brain_memory_save", domain=kwargs.get("domain", ""),
            )
            logger.info(f"🧠 brain_memory_save: [{agent_id[:20]}] saved '{mem_topic[:50]}' ({len(mem_content)} chars)")
            return {"success": True, "storage": "per_agent_brain", "agent_id": agent_id,
                    "topic": mem_topic, "chars_saved": len(mem_content)}
        except Exception as e:
            logger.error(f"brain_memory_save failed for {agent_id}: {e}")
            self.store_semantic_memory(mem_topic, mem_content, source="brain_memory_save")
            return {"success": True, "storage": "shared_brain_fallback", "topic": mem_topic, "error_detail": str(e)}

    def brain_memory_recall(self, query: str = "", key: str = "",
                            topic: str = "", **kwargs) -> Dict[str, Any]:
        sq = query or key or topic or kwargs.get("search", "")
        if not sq:
            return {"success": False, "error": "No query provided"}
        agent_id = getattr(self.brain, "_current_agent_id", "")
        if not agent_id:
            results = self.search_semantic_memory(sq, limit=5)
            return {"success": True, "source": "shared_brain",
                    "results": [{"topic": getattr(r, "topic", ""), "content": getattr(r, "content", "")} for r in results[:5]]}
        try:
            from repryntt.agents.chain_manager import AgentChainManager
            if not hasattr(self.brain, "_agent_chain_mgr") or self.brain._agent_chain_mgr is None:
                bp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain")
                if not os.path.exists(os.path.join(bp, "agent_brains")):
                    bp = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                self.brain._agent_chain_mgr = AgentChainManager(bp)
            memory = self.brain._agent_chain_mgr._load_agent_memory(agent_id)
            ql = sq.lower()
            matches = []
            for mem in memory.get("semantic_memories", []):
                sc = 0
                t = mem.get("topic", "").lower()
                c = mem.get("content", "").lower()
                if ql in t:
                    sc += 2
                if ql in c:
                    sc += 1
                qw = set(ql.split())
                sc += len(qw & (set(t.split()) | set(c.split()))) * 0.5
                if sc > 0:
                    matches.append((sc, mem))
            matches.sort(key=lambda x: x[0], reverse=True)
            top = [m[1] for m in matches[:10]]
            chain_matches = [cs for cs in memory.get("completed_chains_summary", [])
                             if ql in cs.get("topic", "").lower() or ql in cs.get("conclusion", "").lower()]
            return {"success": True, "source": "per_agent_brain", "agent_id": agent_id,
                    "memories": top, "chain_summaries": chain_matches[:5],
                    "total_memories": len(memory.get("semantic_memories", []))}
        except Exception as e:
            logger.error(f"brain_memory_recall failed for {agent_id}: {e}")
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════════════════════════
    # 10. QUERY SIMILARITY & MISC
    # ═══════════════════════════════════════════════════════════════
    def queries_are_similar(self, query1: str, query2: str, threshold: float = 0.8) -> bool:
        stop = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
                "with", "by", "as", "is", "was", "are", "were", "be", "been", "being"}
        w1 = set(query1.lower().split()) - stop
        w2 = set(query2.lower().split()) - stop
        if not w1 or not w2:
            return False
        return len(w1 & w2) / len(w1 | w2) >= threshold
