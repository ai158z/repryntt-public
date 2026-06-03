import json

import repryntt.economy.proof_of_productive_work as popw_module
from repryntt.economy.proof_of_productive_work import ProofOfProductiveWork
from repryntt.economy.rust_chain_client import canonical_tx_timestamp
from repryntt.economy.rust_chain_client import get_next_nonce, pending_nonce_tx


def _fresh_minter(tmp_path, monkeypatch):
    ProofOfProductiveWork._instance = None
    monkeypatch.setattr(
        popw_module,
        "POPW_OUTBOX_PATH",
        tmp_path / "popw_pending.json",
    )
    minter = ProofOfProductiveWork()
    minter._outbox_path = tmp_path / "popw_pending.json"
    minter._outbox_path.parent.mkdir(parents=True, exist_ok=True)
    minter.total_minted_cr = 0.0
    minter.total_tx_count = 0
    return minter


def test_canonical_tx_timestamp_rounds_to_milliseconds():
    assert canonical_tx_timestamp(123.456789) == 123.457
    assert canonical_tx_timestamp(123.4564) == 123.456


def test_failed_popw_batch_persists_to_durable_outbox(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    monkeypatch.setattr(minter, "_retry_persisted_batches", lambda wallet: 0)
    monkeypatch.setattr(minter, "_batch_already_seen", lambda batch: False)
    monkeypatch.setattr(minter, "_mint_via_rust_rpc", lambda *args, **kwargs: False)

    minter._last_mint_error = "Rust RPC rejected mint: Invalid transaction signature"
    minter._accumulate("wallet1", 0.61, {"t": "heartbeat", "ts": 1.0})
    minter._flush("wallet1")

    data = json.loads(minter._outbox_path.read_text())
    batches = data["batches"]
    assert len(batches) == 1
    assert batches[0]["wallet"] == "wallet1"
    assert batches[0]["amount_plancks"] == 61_000_000
    assert batches[0]["last_error"] == "Rust RPC rejected mint: Invalid transaction signature"
    assert batches[0]["popw_batch_id"]


def test_persisted_popw_batch_retries_and_removes_after_acceptance(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    batch = minter._make_popw_batch("wallet1", 0.84, [{"t": "tool_call", "ts": 2.0}])
    minter._upsert_outbox_batch(batch)
    monkeypatch.setattr(minter, "_batch_already_seen", lambda batch: False)
    monkeypatch.setattr(minter, "_mint_via_rust_rpc", lambda *args, **kwargs: True)

    assert minter._retry_persisted_batches("wallet1") == 1

    data = json.loads(minter._outbox_path.read_text())
    assert data["batches"] == []
    assert minter.total_tx_count == 1
    assert minter.total_minted_cr == 0.84


def test_seen_popw_batch_is_settled_without_duplicate_mint(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    batch = minter._make_popw_batch("wallet1", 1.07, [{"t": "heartbeat", "ts": 3.0}])
    minter._upsert_outbox_batch(batch)
    monkeypatch.setattr(minter, "_batch_already_seen", lambda batch: True)

    assert minter._retry_persisted_batches("wallet1") == 1

    data = json.loads(minter._outbox_path.read_text())
    assert data["batches"] == []
    assert minter.total_tx_count == 0


def test_rust_client_nonce_uses_chain_nonce_not_mempool(monkeypatch):
    def fake_rpc(method, params=None, **kw):
        if method == "get_nonce":
            return {"nonce": 281}
        if method == "get_mempool_txs":
            return {
                "pending_transactions": [
                    {"from_address": "wallet1", "nonce": 281, "tx_hash": "abc"}
                ]
            }
        raise AssertionError(method)

    monkeypatch.setattr("repryntt.economy.rust_chain_client.rpc_call", fake_rpc)

    assert get_next_nonce("wallet1") == 281
    assert pending_nonce_tx("wallet1", 281)["tx_hash"] == "abc"


def test_popw_nonce_uses_chain_nonce_and_detects_pending(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)

    def fake_rpc(method, params=None, **kw):
        if method == "get_nonce":
            return {"nonce": 281}
        if method == "get_mempool_txs":
            return {
                "pending_transactions": [
                    {"from_address": "wallet1", "nonce": 281, "tx_hash": "abc"}
                ]
            }
        raise AssertionError(method)

    assert minter._get_next_rust_nonce(fake_rpc, "wallet1") == 281
    assert minter._pending_nonce_tx(fake_rpc, "wallet1", 281)["tx_hash"] == "abc"


def test_popw_outbox_pauses_behind_pending_nonce_without_burning_attempts(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    first = minter._make_popw_batch("wallet1", 0.22, [{"t": "heartbeat", "ts": 4.0}])
    second = minter._make_popw_batch("wallet1", 0.33, [{"t": "heartbeat", "ts": 5.0}])
    first["attempts"] = 4
    second["attempts"] = 2
    minter._upsert_outbox_batch(first)
    minter._upsert_outbox_batch(second)
    monkeypatch.setattr(minter, "_batch_already_seen", lambda batch: False)
    monkeypatch.setattr(
        minter,
        "_wallet_nonce_block",
        lambda wallet: {
            "wallet": wallet,
            "nonce": 283,
            "tx_hash": "5da9d02bdef749b5",
            "popw_batch_id": "",
            "tx_type": "workload_completion",
        },
    )
    monkeypatch.setattr(
        minter,
        "_mint_via_rust_rpc",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mint should be paused")),
    )

    assert minter._retry_persisted_batches("wallet1", limit=5) == 0

    batches = json.loads(minter._outbox_path.read_text())["batches"]
    by_id = {b["popw_batch_id"]: b for b in batches}
    assert by_id[first["popw_batch_id"]]["attempts"] == 4
    assert "nonce 283 already pending" in by_id[first["popw_batch_id"]]["last_error"]
    assert by_id[second["popw_batch_id"]]["attempts"] == 2
    assert minter._last_attempt_paused is True


def test_popw_outbox_settles_batch_already_submitted_to_mempool(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    batch = minter._make_popw_batch("wallet1", 0.44, [{"t": "heartbeat", "ts": 6.0}])
    minter._upsert_outbox_batch(batch)
    monkeypatch.setattr(minter, "_batch_already_seen", lambda batch: False)
    monkeypatch.setattr(
        minter,
        "_wallet_nonce_block",
        lambda wallet: {
            "wallet": wallet,
            "nonce": 283,
            "tx_hash": "5da9d02bdef749b5",
            "popw_batch_id": batch["popw_batch_id"],
            "tx_type": "workload_completion",
        },
    )
    monkeypatch.setattr(
        minter,
        "_mint_via_rust_rpc",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate mint")),
    )

    assert minter._retry_persisted_batches("wallet1", limit=5) == 1

    data = json.loads(minter._outbox_path.read_text())
    assert data["batches"] == []
    assert minter.total_tx_count == 0


def test_popw_outbox_status_reports_portable_queue_summary(tmp_path, monkeypatch):
    minter = _fresh_minter(tmp_path, monkeypatch)
    batch = minter._make_popw_batch("wallet1", 0.51, [{"t": "heartbeat", "ts": 7.0}])
    batch["last_error"] = "queued for retry"
    minter._upsert_outbox_batch(batch)

    status = minter.get_outbox_status(limit=10)

    assert status["path"] == "~/.repryntt/economy/popw_pending.json"
    assert status["count"] == 1
    assert status["total_cr"] == 0.51
    assert status["batches"][0]["popw_batch_id"] == batch["popw_batch_id"]
    assert status["batches"][0]["last_error"] == "queued for retry"
