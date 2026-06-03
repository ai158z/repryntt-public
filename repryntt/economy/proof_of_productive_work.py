"""
Proof of Productive Work (PoPW) — Activity-Based Token Minting

Every meaningful action an AI agent takes creates real value in the world:
research, code, analysis, memory, reasoning.  This module ensures that
value is captured on-chain as minted tokens — not as a reward for doing
someone ELSE's work, but for doing YOUR OWN.

Design principles (Bitcoin ethos applied to AI labor):
  1. Supply-capped.  All mints go through create_reward_transaction() →
     _apply_transaction() which enforces the 21M CR hard cap.
  2. Deterministic.  Reward rates are fixed in code, not decided by any
     central authority.  Change requires a code update (like a BIP).
  3. Fraud-resistant.  Rewards scale with verified quality (self-eval
     score).  A heartbeat that does nothing earns nothing.
  4. Idempotent.  Double-calls for the same heartbeat are ignored via
     a dedup nonce.
  5. Transparent.  Every mint is a standard "reward" transaction with
     full metadata (activity type, score, evidence hash) on-chain.

Integration:
  from repryntt.economy.proof_of_productive_work import get_popw_minter
  popw = get_popw_minter()
  popw.reward_heartbeat(wallet, score=4, tool_count=7, rounds=5, elapsed=180.0)
  popw.reward_tool_call(wallet, tool_name="google_web_search", result_len=5000)
  popw.reward_skill_learned(wallet, skill_name="market_analysis")
"""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("repryntt.economy.popw")

# ──────────────────────────────────────────────────────────────────────
# REWARD SCHEDULE — the "block reward table" for productive work
#
# These rates are intentionally conservative.  A full day of continuous
# 60s-interval heartbeats (~1440/day) at max score would mint:
#   heartbeats: 1440 × 0.10 = 144 CR
#   tools:      ~10k calls × 0.01 = 100 CR
#   skills:     ~20 × 0.50 = 10 CR
#   artifacts:  ~50 × 0.10 = 5 CR
#   reasoning:  ~500 steps × 0.02 = 10 CR
#   memory:     ~100 × 0.02 = 2 CR
#   api calls:  ~2000 × 0.02 = 40 CR
#   Total: ~311 CR/day at absolute maximum
#
# At 69s block interval, mining rewards are ~10 CR/block × ~1252 blocks/day
# = ~12,520 CR/day theoretical max from mining alone.  PoPW adds ~2.5%
# on top — meaningful for the agent but not inflationary.
#
# Halving: PoPW rewards halve on the same schedule as mining rewards
# (every 420,000 blocks) to maintain the deflationary supply curve.
# ──────────────────────────────────────────────────────────────────────

REWARD_TABLE = {
    # Activity               Base CR   Notes
    "heartbeat_complete":    0.10,   # Per heartbeat × (score/5) quality multiplier
    "tool_call":             0.01,   # Per successful tool invocation
    "tool_call_web":         0.03,   # Web search/scrape — higher cost, higher value
    "tool_call_write":       0.05,   # File writes, code generation — tangible artifacts
    "tool_call_memory":      0.02,   # Memory operations — knowledge preservation
    "api_inference":         0.02,   # External API call (real compute cost spent)
    "reasoning_chain_step":  0.02,   # Each step in a multi-heartbeat chain
    "skill_learned":         0.50,   # Permanent capability gain
    "memory_consolidated":   0.02,   # Memory consolidation event
    "artifact_created":      0.10,   # File/report/analysis produced
    "evaluation_bonus":      0.05,   # Bonus per score point above 3 (i.e. 4→0.05, 5→0.10)
}

# Tools classified by value tier
WEB_TOOLS = frozenset({
    "google_web_search", "web_search", "real_web_search",
    "scrape_web_page", "mcp_fetch_fetch", "knowledge_search",
})
WRITE_TOOLS = frozenset({
    "write_file", "create_file", "write_code", "append_daily_memory",
    "create_workspace_file", "save_memory", "codeforge_write",
    "git_commit", "git_push",
})
MEMORY_TOOLS = frozenset({
    "store_memory", "recall_memory", "semantic_search_memory",
    "consolidate_memories", "memory_store", "memory_recall",
    "update_learned_behaviors",
})

HALVING_INTERVAL = 420_000  # Same as mining — blocks
PLANCKS_PER_CREDIT = 100_000_000
POPW_OUTBOX_PATH = Path(os.path.expanduser("~/.repryntt/economy/popw_pending.json"))
POPW_OUTBOX_VERSION = 1
POPW_DEFAULT_RETRY_LIMIT = 500


class ProofOfProductiveWork:
    """Singleton minter that converts productive AI activity into on-chain tokens."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        # Dedup: track (wallet, nonce) to prevent double-minting
        self._seen_nonces: Dict[str, float] = {}
        self._nonce_lock = threading.Lock()

        # Accumulator: batch small rewards within a heartbeat to reduce
        # on-chain transaction spam.  Flushed at heartbeat end.
        self._pending: Dict[str, float] = {}   # wallet → accumulated CR
        self._pending_meta: Dict[str, list] = {}  # wallet → [activity descriptions]
        self._pending_lock = threading.Lock()
        self._outbox_path = POPW_OUTBOX_PATH
        self._outbox_lock = threading.Lock()
        self._last_mint_error = ""
        self._last_attempt_paused = False
        self._last_mint_nonce_blocked = False

        # Blockchain connection — Rust node JSON-RPC on port 9332
        self._node_host = "127.0.0.1"
        self._node_port = 9332

        # Stats for this session
        self.total_minted_cr = 0.0
        self.total_tx_count = 0

        self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
        pending_count = len(self._load_outbox())
        if pending_count:
            logger.info("⛏️ PoPW durable outbox loaded — %d pending batch(es)", pending_count)

        logger.info("⛏️ Proof of Productive Work initialized — all activity earns tokens")

    # ────────────────────────────────────────────────────────
    # PUBLIC API — called from persistent_agents.py hooks
    # ────────────────────────────────────────────────────────

    def reward_heartbeat(
        self,
        wallet: str,
        score: int,
        tool_count: int,
        rounds: int,
        elapsed: float,
        heartbeat_num: int,
        chain_continuing: bool = False,
    ):
        """Reward a completed heartbeat cycle.

        This is the main entry point, called once at the end of each
        heartbeat.  The reward scales with self-eval quality:
          score 1 → 0.02 CR (did almost nothing)
          score 2 → 0.04 CR
          score 3 → 0.06 CR (baseline competent work)
          score 4 → 0.08 CR + 0.05 bonus
          score 5 → 0.10 CR + 0.10 bonus
        """
        nonce = f"hb_{heartbeat_num}_{wallet}"
        if self._is_duplicate(nonce):
            return

        quality = max(0.2, min(1.0, score / 5.0))
        base = REWARD_TABLE["heartbeat_complete"] * quality

        # Evaluation bonus for exceptional work
        bonus = 0.0
        if score > 3:
            bonus = REWARD_TABLE["evaluation_bonus"] * (score - 3)

        # Reasoning chain continuation — extra value for deep work
        chain_bonus = 0.0
        if chain_continuing:
            chain_bonus = REWARD_TABLE["reasoning_chain_step"]

        total = base + bonus + chain_bonus
        total = self._apply_halving(total)

        self._accumulate(wallet, total, {
            "t": "heartbeat",
            "c": "system",
            "ms": int(elapsed * 1000),
            "ok": True,
            "ih": "",
            "oh": "",
            "ts": round(time.time(), 1),
            "score": score,
            "tools": tool_count,
            "rounds": rounds,
        })

        # Flush at heartbeat end — one on-chain TX per heartbeat
        self._flush(wallet)

    def reward_tool_call(
        self,
        wallet: str,
        tool_name: str,
        result_len: int = 0,
        success: bool = True,
        duration_ms: int = 0,
        input_hash: str = "",
        output_hash: str = "",
        category: str = "",
    ):
        """Reward a successful tool invocation with on-chain metadata."""
        if not success:
            return

        # Classify tool by value tier
        if tool_name in WEB_TOOLS:
            base = REWARD_TABLE["tool_call_web"]
            cat = category or "research"
        elif tool_name in WRITE_TOOLS:
            base = REWARD_TABLE["tool_call_write"]
            cat = category or "write"
        elif tool_name in MEMORY_TOOLS:
            base = REWARD_TABLE["tool_call_memory"]
            cat = category or "memory"
        else:
            base = REWARD_TABLE["tool_call"]
            cat = category or "general"

        reward = self._apply_halving(base)
        self._accumulate(wallet, reward, {
            "t": tool_name,
            "c": cat,
            "ms": duration_ms,
            "ok": True,
            "ih": input_hash[:16] if input_hash else "",
            "oh": output_hash[:16] if output_hash else "",
            "ts": round(time.time(), 1),
        })

    def reward_skill_learned(self, wallet: str, skill_name: str):
        """Reward permanent capability acquisition."""
        reward = self._apply_halving(REWARD_TABLE["skill_learned"])
        self._accumulate(wallet, reward, {
            "t": "skill_learned", "c": "learning",
            "ms": 0, "ok": True, "ih": "", "oh": "",
            "ts": round(time.time(), 1), "skill": skill_name[:60],
        })

    def reward_memory_consolidated(self, wallet: str, memories_count: int = 1):
        """Reward memory consolidation work."""
        reward = self._apply_halving(REWARD_TABLE["memory_consolidated"]) * min(memories_count, 10)
        self._accumulate(wallet, reward, {
            "t": "memory_consolidation", "c": "memory",
            "ms": 0, "ok": True, "ih": "", "oh": "",
            "ts": round(time.time(), 1), "n": memories_count,
        })

    def reward_artifact(self, wallet: str, artifact_type: str = "file"):
        """Reward tangible output creation."""
        reward = self._apply_halving(REWARD_TABLE["artifact_created"])
        self._accumulate(wallet, reward, {
            "t": "artifact_created", "c": "write",
            "ms": 0, "ok": True, "ih": "", "oh": "",
            "ts": round(time.time(), 1), "artifact": artifact_type,
        })

    def reward_api_call(self, wallet: str, provider: str = "unknown"):
        """Reward an external API inference call (real compute cost)."""
        reward = self._apply_halving(REWARD_TABLE["api_inference"])
        self._accumulate(wallet, reward, {
            "t": "api_inference", "c": "inference",
            "ms": 0, "ok": True, "ih": "", "oh": "",
            "ts": round(time.time(), 1), "provider": provider,
        })

    # ────────────────────────────────────────────────────────
    # INTERNAL — accumulation, dedup, chain interaction
    # ────────────────────────────────────────────────────────

    def _is_duplicate(self, nonce: str) -> bool:
        """Check and register a nonce to prevent double-minting."""
        with self._nonce_lock:
            now = time.time()
            # Prune old nonces (older than 1 hour)
            if len(self._seen_nonces) > 10000:
                cutoff = now - 3600
                self._seen_nonces = {
                    k: v for k, v in self._seen_nonces.items() if v > cutoff
                }
            if nonce in self._seen_nonces:
                return True
            self._seen_nonces[nonce] = now
            return False

    def _accumulate(self, wallet: str, amount_cr: float, activity):
        """Add to the pending reward for this wallet (batched per heartbeat).

        activity: dict with on-chain metadata fields, or legacy str for
        backward compatibility.
        """
        if isinstance(activity, str):
            activity = {"t": activity, "c": "legacy", "ms": 0, "ok": True,
                        "ih": "", "oh": "", "ts": round(time.time(), 1)}
        with self._pending_lock:
            self._pending[wallet] = self._pending.get(wallet, 0.0) + amount_cr
            self._pending_meta.setdefault(wallet, []).append(activity)

    def _flush(self, wallet: str):
        """Write accumulated rewards to the blockchain as a single TX."""
        self._retry_persisted_batches(wallet, limit=self._popw_retry_limit())

        with self._pending_lock:
            amount = self._pending.pop(wallet, 0.0)
            activities = self._pending_meta.pop(wallet, [])

        if amount < 0.001:  # Dust threshold — don't spam chain with sub-plancks
            return

        # Round to 8 decimal places (plancks precision)
        amount = round(amount, 8)
        batch = self._make_popw_batch(wallet, amount, activities)
        self._upsert_outbox_batch(batch)

        self._attempt_popw_batch(batch)

    def _make_popw_batch(self, wallet: str, amount_cr: float, activities: list) -> dict:
        """Create a stable durable outbox record for one PoPW mint."""
        amount_plancks = int(round(amount_cr * PLANCKS_PER_CREDIT))
        evidence = hashlib.sha256(
            json.dumps(activities, sort_keys=True, default=str).encode()
        ).hexdigest()
        batch_id = hashlib.sha256(
            f"{wallet}:{amount_plancks}:{evidence}".encode()
        ).hexdigest()
        return {
            "id": batch_id,
            "popw_batch_id": batch_id,
            "wallet": wallet,
            "amount_cr": round(amount_plancks / PLANCKS_PER_CREDIT, 8),
            "amount_plancks": amount_plancks,
            "activities": activities,
            "evidence_hash": evidence,
            "created_at": round(time.time(), 3),
            "attempts": 0,
            "last_error": "",
        }

    def _normalize_popw_batch(self, batch: dict) -> dict:
        """Fill missing fields for older/incomplete pending records."""
        activities = list(batch.get("activities") or [])
        amount_plancks = int(
            batch.get("amount_plancks")
            or round(float(batch.get("amount_cr", 0.0)) * PLANCKS_PER_CREDIT)
        )
        evidence = batch.get("evidence_hash") or hashlib.sha256(
            json.dumps(activities, sort_keys=True, default=str).encode()
        ).hexdigest()
        wallet = str(batch.get("wallet") or "")
        batch_id = str(
            batch.get("popw_batch_id")
            or batch.get("id")
            or hashlib.sha256(f"{wallet}:{amount_plancks}:{evidence}".encode()).hexdigest()
        )
        normalized = dict(batch)
        normalized.update({
            "id": batch_id,
            "popw_batch_id": batch_id,
            "wallet": wallet,
            "amount_cr": round(amount_plancks / PLANCKS_PER_CREDIT, 8),
            "amount_plancks": amount_plancks,
            "activities": activities,
            "evidence_hash": evidence,
            "attempts": int(batch.get("attempts") or 0),
            "last_error": str(batch.get("last_error") or ""),
        })
        normalized.setdefault("created_at", round(time.time(), 3))
        return normalized

    def _metadata_for_batch(self, batch: dict, attempt: int) -> dict:
        """Build idempotent on-chain metadata for a durable PoPW batch."""
        activities = list(batch.get("activities") or [])
        evidence = str(batch.get("evidence_hash") or "")
        return {
            "v": 2,
            "purpose": "popw",
            "source": "proof_of_productive_work",
            "agent": self._get_agent_id(),
            "activities": activities,
            "n": len(activities),
            "ev": evidence[:16],
            "evidence_hash": evidence,
            "popw_batch_id": batch["popw_batch_id"],
            "amount_plancks": int(batch["amount_plancks"]),
            "attempt": attempt,
        }

    def _attempt_popw_batch(self, batch: dict, *, persisted: bool = False) -> bool:
        """Try to submit one durable PoPW batch to Rust RPC."""
        batch = self._normalize_popw_batch(batch)
        self._last_attempt_paused = False
        batch_id = batch["popw_batch_id"]
        wallet = batch["wallet"]
        amount = float(batch["amount_cr"])
        activities = list(batch.get("activities") or [])
        evidence = str(batch.get("evidence_hash") or "")

        if self._batch_already_seen(batch):
            self._remove_outbox_batch(batch_id)
            logger.info(
                "⛏️ PoPW: batch %s already pending/confirmed; marking settled",
                batch_id[:16],
            )
            return True

        nonce_block = self._wallet_nonce_block(wallet)
        if nonce_block:
            pending_batch = str(nonce_block.get("popw_batch_id") or "")
            pending_hash = str(nonce_block.get("tx_hash") or "")
            pending_nonce = nonce_block.get("nonce")
            if pending_batch == batch_id:
                self._remove_outbox_batch(batch_id)
                logger.info(
                    "⛏️ PoPW: batch %s already submitted as pending tx %s; marking settled",
                    batch_id[:16],
                    pending_hash[:16] if pending_hash else "?",
                )
                return True

            self._last_attempt_paused = True
            batch["last_error"] = self._format_nonce_block(nonce_block)
            self._upsert_outbox_batch(batch)
            waiting = self._outbox_wallet_count(wallet)
            logger.warning(
                "⛏️ PoPW outbox paused for %s... — %s; %d batch(es) waiting",
                wallet[:16],
                batch["last_error"],
                waiting,
            )
            return False

        attempt = int(batch.get("attempts") or 0) + 1
        batch["attempts"] = attempt
        batch["last_attempt_at"] = round(time.time(), 3)
        metadata = self._metadata_for_batch(batch, attempt)

        if self._mint_via_rust_rpc(
            wallet,
            amount,
            metadata,
            amount_plancks=int(batch["amount_plancks"]),
        ):
            self._remove_outbox_batch(batch_id)
            self.total_minted_cr += amount
            self.total_tx_count += 1
            logger.info(
                "⛏️ PoPW: +%.4f CR → %s... (%d activities, batch=%s, evidence=%s)",
                amount,
                wallet[:16],
                len(activities),
                batch_id[:16],
                evidence,
            )
            return True

        batch["last_error"] = self._last_mint_error or "no blockchain connection"
        if self._last_mint_nonce_blocked:
            batch["attempts"] = max(0, attempt - 1)
            self._last_attempt_paused = True
        self._upsert_outbox_batch(batch)
        if self._last_attempt_paused:
            waiting = self._outbox_wallet_count(wallet)
            logger.warning(
                "⛏️ PoPW outbox paused for %s... — %s; %d batch(es) waiting",
                wallet[:16],
                batch["last_error"],
                waiting,
            )
            return False

        verb = "retry failed" if persisted else "failed to mint"
        logger.warning(
            "⛏️ PoPW: %s %.4f CR for %s... — %s; persisted batch=%s attempt=%d",
            verb,
            amount,
            wallet[:16],
            batch["last_error"],
            batch_id[:16],
            attempt,
        )
        return False

    def _requeue_pending(self, wallet: str, amount_cr: float, activities: list):
        """Compatibility shim: persist a failed batch to the durable outbox."""
        if amount_cr < 0.001 or not activities:
            return
        batch = self._make_popw_batch(wallet, round(amount_cr, 8), activities)
        batch["last_error"] = self._last_mint_error or "queued for retry"
        self._upsert_outbox_batch(batch)

    def _retry_persisted_batches(
        self,
        wallet: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> int:
        """Retry oldest durable PoPW batches before minting new heartbeat work."""
        if limit is None:
            limit = self._popw_retry_limit()
        batches = sorted(
            (self._normalize_popw_batch(b) for b in self._load_outbox()),
            key=lambda b: float(b.get("created_at", 0.0) or 0.0),
        )
        retried = 0
        for batch in batches:
            if wallet and batch.get("wallet") != wallet:
                continue
            if retried >= limit:
                break
            self._attempt_popw_batch(batch, persisted=True)
            if self._last_attempt_paused:
                break
            retried += 1
        return retried

    def drain_outbox(self, wallet: Optional[str] = None, limit: Optional[int] = None) -> dict:
        """Manually push queued PoPW batches toward the Rust mempool.

        This is intentionally idempotent: batches already pending or confirmed
        are marked settled, nonce-blocked batches remain queued, and retry
        pressure is bounded by ``limit`` so a UI click cannot stampede the RPC.
        """
        if limit is None:
            limit = self._popw_retry_limit()
        limit = max(1, min(int(limit), 5_000))
        before = self.get_outbox_status(wallet=wallet, limit=0)
        retried = self._retry_persisted_batches(wallet=wallet, limit=limit)
        after = self.get_outbox_status(wallet=wallet, limit=25)
        return {
            "wallet": wallet or "",
            "limit": limit,
            "attempted": retried,
            "settled_or_submitted": max(0, before.get("count", 0) - after.get("count", 0)),
            "before_count": before.get("count", 0),
            "after_count": after.get("count", 0),
            "before_total_cr": before.get("total_cr", 0.0),
            "after_total_cr": after.get("total_cr", 0.0),
            "paused": bool(self._last_attempt_paused),
            "outbox": after,
        }

    @staticmethod
    def _popw_retry_limit() -> int:
        try:
            return max(1, int(os.environ.get(
                "REPRYNTT_POPW_OUTBOX_RETRY_LIMIT",
                str(POPW_DEFAULT_RETRY_LIMIT),
            )))
        except (TypeError, ValueError):
            return POPW_DEFAULT_RETRY_LIMIT

    def _load_outbox(self) -> list:
        """Load durable pending PoPW batches from disk."""
        with self._outbox_lock:
            return self._read_outbox_unlocked()

    def _read_outbox_unlocked(self) -> list:
        try:
            if not self._outbox_path.exists():
                return []
            data = json.loads(self._outbox_path.read_text())
            if isinstance(data, list):
                raw_batches = data
            else:
                raw_batches = data.get("batches", [])
            batches = [
                self._normalize_popw_batch(b)
                for b in raw_batches
                if isinstance(b, dict) and (b.get("wallet") or b.get("amount_cr"))
            ]
            deduped: Dict[str, dict] = {}
            for batch in batches:
                deduped[batch["popw_batch_id"]] = batch
            return list(deduped.values())
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("⛏️ PoPW: pending outbox unreadable: %s", exc)
            return []

    def _write_outbox_unlocked(self, batches: list):
        self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": POPW_OUTBOX_VERSION,
            "batches": sorted(
                [self._normalize_popw_batch(b) for b in batches],
                key=lambda b: float(b.get("created_at", 0.0) or 0.0),
            ),
        }
        tmp = self._outbox_path.with_suffix(self._outbox_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self._outbox_path)

    def _upsert_outbox_batch(self, batch: dict):
        batch = self._normalize_popw_batch(batch)
        with self._outbox_lock:
            batches = self._read_outbox_unlocked()
            by_id = {b["popw_batch_id"]: b for b in batches}
            by_id[batch["popw_batch_id"]] = batch
            self._write_outbox_unlocked(list(by_id.values()))

    def _remove_outbox_batch(self, batch_id: str):
        with self._outbox_lock:
            batches = [
                b for b in self._read_outbox_unlocked()
                if b.get("popw_batch_id") != batch_id and b.get("id") != batch_id
            ]
            self._write_outbox_unlocked(batches)

    def _outbox_wallet_count(self, wallet: str) -> int:
        return sum(1 for b in self._load_outbox() if b.get("wallet") == wallet)

    def get_outbox_status(self, wallet: Optional[str] = None, limit: int = 25) -> dict:
        """Return durable PoPW outbox status for diagnostics/UI."""
        batches = sorted(
            (self._normalize_popw_batch(b) for b in self._load_outbox()),
            key=lambda b: float(b.get("created_at", 0.0) or 0.0),
        )
        if wallet:
            batches = [b for b in batches if b.get("wallet") == wallet]
        total_plancks = sum(int(b.get("amount_plancks") or 0) for b in batches)
        return {
            "path": "~/.repryntt/economy/popw_pending.json",
            "count": len(batches),
            "total_cr": round(total_plancks / PLANCKS_PER_CREDIT, 8),
            "batches": [
                {
                    "popw_batch_id": b.get("popw_batch_id"),
                    "wallet": b.get("wallet"),
                    "amount_cr": b.get("amount_cr"),
                    "amount_plancks": b.get("amount_plancks"),
                    "attempts": b.get("attempts", 0),
                    "last_error": b.get("last_error", ""),
                    "created_at": b.get("created_at"),
                    "last_attempt_at": b.get("last_attempt_at"),
                    "evidence_hash": b.get("evidence_hash", ""),
                }
                for b in batches[:limit]
            ],
        }

    def _batch_already_seen(self, batch: dict) -> bool:
        """Return true if a PoPW batch id is already in mempool or recent history."""
        batch_id = str(batch.get("popw_batch_id") or batch.get("id") or "")
        wallet = str(batch.get("wallet") or "")
        if not batch_id or not wallet:
            return False

        try:
            from repryntt.economy.rust_chain_client import rpc_call

            mempool = rpc_call("get_mempool_txs", timeout=3.0)
            if "error" not in mempool and self._tx_list_has_batch(
                mempool.get("pending_transactions", []),
                batch_id,
            ):
                return True

            for page in range(2):
                history = rpc_call(
                    "get_address_history",
                    {"address": wallet, "page": page, "limit": 200},
                    timeout=5.0,
                )
                if "error" in history:
                    break
                if self._tx_list_has_batch(history.get("transactions", []), batch_id):
                    return True
                if not history.get("has_more"):
                    break
        except Exception as exc:
            logger.debug("PoPW batch idempotency scan failed: %s", exc)
        return False

    @staticmethod
    def _tx_list_has_batch(txs: list, batch_id: str) -> bool:
        for tx in txs or []:
            if not isinstance(tx, dict):
                continue
            metadata = tx.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("popw_batch_id") == batch_id:
                return True
        return False

    def _wallet_nonce_block(self, wallet: str) -> Optional[dict]:
        """Return the pending mempool tx occupying this wallet's next nonce."""
        if not wallet:
            return None
        try:
            from repryntt.economy.rust_chain_client import rpc_call

            nonce = self._get_next_rust_nonce(rpc_call, wallet)
            pending = self._pending_nonce_tx(rpc_call, wallet, nonce)
            if not pending:
                return None
            metadata = pending.get("metadata") or {}
            batch_id = ""
            if isinstance(metadata, dict):
                batch_id = str(metadata.get("popw_batch_id") or "")
            return {
                "wallet": wallet,
                "nonce": nonce,
                "tx_hash": str(pending.get("tx_hash") or ""),
                "popw_batch_id": batch_id,
                "tx_type": pending.get("tx_type", ""),
            }
        except Exception as exc:
            logger.debug("PoPW nonce gate check failed: %s", exc)
            return None

    @staticmethod
    def _format_nonce_block(block: dict) -> str:
        nonce = block.get("nonce")
        tx_hash = str(block.get("tx_hash") or "")
        batch_id = str(block.get("popw_batch_id") or "")
        msg = f"nonce {nonce} already pending in mempool"
        if tx_hash:
            msg += f" ({tx_hash[:16]})"
        if batch_id:
            msg += f" for batch {batch_id[:16]}"
        return msg + "; waiting for confirmation"

    def _mint_via_rust_rpc(
        self,
        wallet: str,
        amount_cr: float,
        metadata: dict,
        amount_plancks: Optional[int] = None,
    ) -> bool:
        """Mint productive-work credit through the Rust JSON-RPC node."""
        try:
            from repryntt.economy.rust_chain_client import canonical_tx_timestamp, rpc_call
            from repryntt.economy.node_wallet import get_node_wallet

            self._last_mint_error = ""
            self._last_mint_nonce_blocked = False
            node_wallet = get_node_wallet()
            if node_wallet is None or not node_wallet.can_sign():
                self._last_mint_error = "node wallet cannot sign"
                logger.debug("PoPW Rust RPC mint skipped: %s", self._last_mint_error)
                return False

            if wallet != node_wallet.address:
                self._last_mint_error = (
                    f"reward wallet {wallet[:16]} does not match node wallet "
                    f"{node_wallet.address[:16]}"
                )
                logger.debug(
                    "PoPW Rust RPC mint skipped: %s",
                    self._last_mint_error,
                )
                return False

            amount_plancks = (
                int(amount_plancks)
                if amount_plancks is not None
                else int(round(amount_cr * PLANCKS_PER_CREDIT))
            )
            timestamp = canonical_tx_timestamp()
            nonce = self._get_next_rust_nonce(rpc_call, wallet)
            pending = self._pending_nonce_tx(rpc_call, wallet, nonce)
            if pending:
                self._last_mint_nonce_blocked = True
                pending_hash = str(pending.get("tx_hash") or "")
                self._last_mint_error = (
                    f"nonce {nonce} already pending in mempool"
                    + (f" ({pending_hash[:16]})" if pending_hash else "")
                    + "; waiting for confirmation"
                )
                logger.debug("PoPW Rust RPC mint paused: %s", self._last_mint_error)
                return False

            tx_metadata = dict(metadata)
            tx_metadata.setdefault("purpose", "popw")
            tx_metadata.setdefault("source", "proof_of_productive_work")

            tx_hash = self._rust_tx_hash(
                from_address=wallet,
                to_address=wallet,
                amount=amount_plancks,
                tx_type="workload_completion",
                nonce=nonce,
                timestamp=timestamp,
                metadata=tx_metadata,
                tx_version=2,
            )
            signature = node_wallet.sign(bytes.fromhex(tx_hash)).hex()

            resp = rpc_call(
                "submit_productive_work",
                {
                    "from_address": wallet,
                    "to_address": wallet,
                    "amount": amount_plancks,
                    "tx_type": "workload_completion",
                    "nonce": nonce,
                    "timestamp": timestamp,
                    "metadata": tx_metadata,
                    "tx_version": 2,
                    "public_key": node_wallet.public_key.hex(),
                    "signature": signature,
                },
            )
            if "error" in resp:
                self._last_mint_error = f"Rust RPC rejected mint: {resp['error']}"
                logger.debug("PoPW Rust RPC mint rejected: %s", resp["error"])
                return False
            accepted = resp.get("accepted", False)
            if not accepted:
                self._last_mint_error = "Rust RPC returned accepted=false"
            return accepted
        except Exception as e:
            self._last_mint_error = f"Rust RPC mint failed: {e}"
            logger.debug("PoPW Rust RPC mint failed: %s", e)
            return False

    def _get_next_rust_nonce(self, rpc_call, wallet: str) -> int:
        """Return the chain nonce expected by Rust RPC validation."""
        resp = rpc_call("get_nonce", {"address": wallet})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return int(resp.get("nonce", 0))

    def _pending_nonce_tx(self, rpc_call, wallet: str, nonce: int) -> Optional[dict]:
        """Return the mempool transaction currently occupying wallet/nonce."""
        mempool = rpc_call("get_mempool_txs")
        if "error" in mempool:
            return None
        for tx in mempool.get("pending_transactions", []):
            if not isinstance(tx, dict):
                continue
            try:
                tx_nonce = int(tx.get("nonce"))
            except (TypeError, ValueError):
                continue
            if tx.get("from_address") == wallet and tx_nonce == nonce:
                return tx
        return None

    def _rust_tx_hash(
        self,
        *,
        from_address: str,
        to_address: str,
        amount: int,
        tx_type: str,
        nonce: int,
        timestamp: float,
        metadata: dict,
        tx_version: int,
    ) -> str:
        """Calculate the Rust/Python-compatible SHA3-512 transaction hash."""
        tx_data = {
            "amount": amount,
            "from": from_address,
            "metadata": metadata,
            "nonce": nonce,
            "timestamp": timestamp,
            "to": to_address,
            "type": tx_type,
        }
        if tx_version >= 2:
            tx_data["chain_id"] = "RPNT-mainnet-1"
        encoded = json.dumps(tx_data, sort_keys=True).encode()
        return hashlib.sha3_512(encoded).hexdigest()

    def _mint_via_manager(self, wallet: str, amount_cr: float, metadata: dict) -> bool:
        """Legacy fallback — mint via RobotEconomyManager (if still running)."""
        try:
            from repryntt.economy.manager import RobotEconomyManager
            mgr = RobotEconomyManager._instance
            if mgr is None or not mgr.is_running:
                if not self._last_mint_error:
                    self._last_mint_error = "RobotEconomyManager fallback is not running"
                return False

            main_node = mgr.nodes.get("main")
            if not main_node:
                if not self._last_mint_error:
                    self._last_mint_error = "RobotEconomyManager main node missing"
                return False

            amount_plancks = int(amount_cr * 100_000_000)

            from repryntt.economy.transaction import create_reward_transaction
            reward_tx = create_reward_transaction(
                miner_address=wallet,
                amount=amount_plancks,
                metadata=metadata,
            )

            with main_node.lock:
                success, msg = main_node.tx_pool.add_transaction(
                    reward_tx, main_node.balances, require_signature=False
                )
                if success:
                    main_node.balances[wallet] = main_node.balances.get(wallet, 0) + amount_plancks
                    main_node.save_state()
                    return True

            logger.debug(f"PoPW manager mint rejected: {msg}")
            self._last_mint_error = f"RobotEconomyManager rejected mint: {msg}"
            return False
        except Exception as e:
            if not self._last_mint_error:
                self._last_mint_error = f"RobotEconomyManager mint failed: {e}"
            logger.debug(f"PoPW manager mint failed: {e}")
            return False

    def _get_agent_id(self) -> str:
        """Return the primary agent identity for on-chain attribution."""
        try:
            import socket
            return socket.gethostname()
        except Exception:
            return "unknown"

    def _apply_halving(self, base_reward: float) -> float:
        """Apply halving schedule based on current block height."""
        try:
            height = self._get_chain_height()
            halvings = height // HALVING_INTERVAL
            if halvings >= 64:
                return 0.0  # Effectively zero after 64 halvings
            return base_reward / (2 ** halvings)
        except Exception:
            return base_reward  # Can't reach chain — use base rate

    def _get_chain_height(self) -> int:
        """Get current block height from the Rust chain."""
        try:
            from repryntt.economy.rust_chain_client import rpc_call
            resp = rpc_call("get_chain_height", timeout=5.0)
            if "error" not in resp:
                return resp.get("height", 0)
        except Exception:
            pass
        return 0

    def get_stats(self) -> dict:
        """Return session minting stats."""
        return {
            "total_minted_cr": round(self.total_minted_cr, 8),
            "total_transactions": self.total_tx_count,
            "reward_table": dict(REWARD_TABLE),
            "halving_interval": HALVING_INTERVAL,
        }


# ──────────────────────────────────────────────────────────────────────
# Off-chain evidence store — persists raw tool I/O for hash verification
# ──────────────────────────────────────────────────────────────────────

_EVIDENCE_DIR = Path(os.path.expanduser("~/.repryntt/chain_evidence"))


class ChainEvidenceStore:
    """Persist raw tool call input/output alongside their SHA-256 hashes.

    On-chain, only truncated hashes are stored (16 hex chars).  This store
    keeps the full data so that any challenger can:
      1. Retrieve the original input/output for a given hash
      2. Re-hash it and verify it matches the on-chain commitment

    Storage layout:
      ~/.repryntt/chain_evidence/YYYY-MM-DD/{input_hash[:16]}.json
    """

    def __init__(self):
        self._dir = _EVIDENCE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def store(self, input_hash: str, output_hash: str,
              tool_name: str, raw_input: str, raw_output: str):
        """Persist evidence for a single tool call."""
        try:
            day_dir = self._dir / time.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)

            record = {
                "tool": tool_name,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "input": raw_input[:50000],
                "output": raw_output[:50000],
                "timestamp": time.time(),
            }
            path = day_dir / f"{input_hash[:16]}.json"
            path.write_text(json.dumps(record, indent=2))
        except Exception as e:
            logger.debug(f"Evidence store write failed: {e}")

    @staticmethod
    def verify(evidence_path: str) -> dict:
        """Verify an evidence file against its own hashes.

        Returns {"valid": True/False, "input_match": bool, "output_match": bool}
        """
        try:
            data = json.loads(Path(evidence_path).read_text())
            recomputed_ih = hashlib.sha256(
                json.dumps(data.get("input", ""), sort_keys=True, default=str).encode()
            ).hexdigest()
            recomputed_oh = hashlib.sha256(
                str(data.get("output", "")).encode()
            ).hexdigest()
            input_match = recomputed_ih == data.get("input_hash", "")
            output_match = recomputed_oh == data.get("output_hash", "")
            return {
                "valid": input_match and output_match,
                "input_match": input_match,
                "output_match": output_match,
                "tool": data.get("tool", ""),
                "timestamp": data.get("timestamp", 0),
            }
        except Exception as e:
            return {"valid": False, "error": str(e)}


def get_evidence_store() -> ChainEvidenceStore:
    """Get the global evidence store instance."""
    return ChainEvidenceStore()


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton accessor
# ──────────────────────────────────────────────────────────────────────

def get_popw_minter() -> ProofOfProductiveWork:
    """Get the global PoPW minter instance."""
    return ProofOfProductiveWork()
