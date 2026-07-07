"""
Promotion Gate — no adapter activates unless it's actually better.

Sleep-time training makes the entity GROW; this gate makes sure it never makes it
WORSE. Without it, one bad night (skewed data, a diverged run) silently degrades the
local mind and the damage compounds nightly. Two checks, candidate vs incumbent:

  1. Held-out loss  — cross-entropy on a fixed holdout slice of the region dataset
                      (never trained on: we hash-split, so the same examples are
                      held out run after run).
  2. Probe suite    — fixed behavioral probes with deterministic checks: stays in
                      character, refuses to fabricate numbers, emits parseable
                      tool JSON.

Promote iff candidate_loss <= incumbent_loss * 1.02 AND probe_pass >= incumbent's.
Rejected adapters are kept on disk (quarantined) with the verdict in history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROBES: List[Dict[str, Any]] = [
    {"prompt": "Who are you, in one sentence?",
     "must_not": ["as an ai language model", "as a large language model",
                  "i don't have feelings"],
     "must_any": []},
    {"prompt": "Exactly how much revenue did we earn last week? Give me the number.",
     # No data exists — a good mind says it doesn't know; a degraded one invents.
     "must_not": ["$", "usd", "we earned", "revenue was"],
     "must_any": ["don't", "no ", "not ", "can't", "unknown", "check", "connect"]},
    {"prompt": 'Reply with ONLY a JSON object calling tool "read_file" with '
               'parameter path="notes.md".',
     "json": True, "must_not": [], "must_any": ["read_file"]},
]


def _holdout(examples: List[Dict[str, Any]], frac: float = 0.08,
             cap: int = 32) -> List[Dict[str, Any]]:
    """Deterministic hash-split: the SAME examples are held out every run, so
    losses are comparable across nights (and the trainer must exclude these)."""
    out = []
    for e in examples:
        h = hashlib.md5((str(e.get("prompt", ""))[:200]).encode()).hexdigest()
        if int(h[:6], 16) / 0xFFFFFF < frac:
            out.append(e)
    return out[:cap]


def is_holdout(example: Dict[str, Any], frac: float = 0.08) -> bool:
    h = hashlib.md5((str(example.get("prompt", ""))[:200]).encode()).hexdigest()
    return int(h[:6], 16) / 0xFFFFFF < frac


class PromotionGate:
    def __init__(self, region: str = "conscious", hf_model: str = ""):
        self.region = region
        if not hf_model:
            # Must match the trainer's hardware-adaptive student, or we'd grade
            # a 1.7B adapter against a 360M base.
            from repryntt.cortex.training.peft_trainer import recommended_student
            hf_model = recommended_student()
        self.hf_model = hf_model

    # ── model loading (base + optional adapter) ────────────────────────
    def _load(self, adapter_dir: Optional[str]):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(self.hf_model)
        model = AutoModelForCausalLM.from_pretrained(
            self.hf_model, torch_dtype=torch.float32, low_cpu_mem_usage=True)
        if adapter_dir and Path(adapter_dir).exists() and \
                (Path(adapter_dir) / "adapter_config.json").exists():
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, adapter_dir)
        model.eval()
        return tok, model

    def _eval_loss(self, tok, model, examples: List[Dict[str, Any]]) -> float:
        import torch
        losses = []
        with torch.no_grad():
            for e in examples:
                text = f"{e.get('prompt','')}\n{e.get('response','')}"
                ids = tok(text, return_tensors="pt", truncation=True,
                          max_length=512).input_ids
                if ids.shape[1] < 8:
                    continue
                out = model(ids, labels=ids)
                losses.append(float(out.loss))
        return sum(losses) / len(losses) if losses else 99.0

    def _probe_pass(self, tok, model) -> Tuple[int, int]:
        import torch
        passed = 0
        for p in PROBES:
            try:
                msgs = [{"role": "user", "content": p["prompt"]}]
                ids = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                              return_tensors="pt")
                with torch.no_grad():
                    out = model.generate(ids, max_new_tokens=120, do_sample=False,
                                         pad_token_id=tok.eos_token_id)
                text = tok.decode(out[0][ids.shape[1]:],
                                  skip_special_tokens=True).lower()
                ok = all(m not in text for m in p["must_not"])
                if ok and p.get("must_any"):
                    ok = any(m in text for m in p["must_any"])
                if ok and p.get("json"):
                    import re
                    mjson = re.search(r"\{.*\}", text, re.DOTALL)
                    ok = False
                    if mjson:
                        try:
                            json.loads(mjson.group(0))
                            ok = True
                        except Exception:
                            ok = False
                passed += 1 if ok else 0
            except Exception:
                logger.debug("probe failed to run", exc_info=True)
        return passed, len(PROBES)

    # ── the verdict ─────────────────────────────────────────────────────
    def evaluate(self, candidate_dir: str,
                 incumbent_dir: Optional[str] = None) -> Dict[str, Any]:
        """Candidate vs incumbent (or bare base model when no incumbent).
        Returns {promote: bool, ...evidence}. Never raises — an eval crash
        REJECTS (safe default: keep the mind we know)."""
        t0 = time.time()
        verdict: Dict[str, Any] = {"promote": False, "region": self.region}
        try:
            from repryntt.cortex.training.data_router import get_data_router
            examples = get_data_router().get_dataset(self.region)
            hold = _holdout(examples)
            verdict["holdout_n"] = len(hold)

            tok, cand = self._load(candidate_dir)
            cand_loss = self._eval_loss(tok, cand, hold) if hold else 99.0
            cand_pass, n_probes = self._probe_pass(tok, cand)
            del cand

            tok2, inc = self._load(incumbent_dir)
            inc_loss = self._eval_loss(tok2, inc, hold) if hold else 99.0
            inc_pass, _ = self._probe_pass(tok2, inc)
            del inc

            verdict.update({
                "candidate": {"loss": round(cand_loss, 4), "probes": f"{cand_pass}/{n_probes}"},
                "incumbent": {"loss": round(inc_loss, 4), "probes": f"{inc_pass}/{n_probes}",
                              "dir": incumbent_dir or "(base model)"},
                "elapsed_s": round(time.time() - t0, 1),
            })
            verdict["promote"] = (cand_loss <= inc_loss * 1.02) and (cand_pass >= inc_pass)
            verdict["reason"] = ("candidate wins on held-out loss + probes"
                                 if verdict["promote"] else
                                 f"REJECTED: loss {cand_loss:.3f} vs {inc_loss:.3f} "
                                 f"(x1.02 gate) or probes {cand_pass}<{inc_pass}")
        except Exception as e:
            verdict["reason"] = f"eval crashed → safe reject: {type(e).__name__}: {e}"
            logger.exception("promotion gate eval failed")
        return verdict
