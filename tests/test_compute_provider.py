import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from repryntt.economy.compute_provider import ComputeProviderDaemon, ProviderConfig
from repryntt.paths import set_data_dir


def _signer():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    def sign(payload: bytes) -> tuple[str, str]:
        return private_key.sign(payload).hex(), public_key.hex()

    return sign


def test_provider_announcement_is_signed_and_verifiable(tmp_path, monkeypatch):
    set_data_dir(tmp_path)
    monkeypatch.setenv("REPRYNTT_ADDRESS", "a" * 40)

    provider = ComputeProviderDaemon(config_path=tmp_path / "provider.json", signer=_signer())
    provider.set_enabled(True)

    announcement = provider.build_announcement()

    assert announcement["enabled"] is True
    assert announcement["wallet_address"] == "a" * 40
    assert announcement["effective_tflops"] >= 0
    assert ComputeProviderDaemon.verify_announcement(announcement)


def test_provider_health_job_completes_with_receipt(tmp_path, monkeypatch):
    set_data_dir(tmp_path)
    monkeypatch.setenv("REPRYNTT_ADDRESS", "b" * 40)

    provider = ComputeProviderDaemon(config_path=tmp_path / "provider.json", signer=_signer())
    provider.config = ProviderConfig(
        enabled=True,
        provider_id="provider-test",
        wallet_address="b" * 40,
        supported_task_types=["health_check"],
    )
    provider.save_config()

    job = provider.submit_local_job(
        buyer_address="buyer",
        task_type="health_check",
        payload={},
    )
    completed = provider.run_once()

    assert completed is not None
    assert completed.job_id == job.job_id
    assert completed.state == "completed"
    assert completed.receipt_hash

    receipt_path = tmp_path / "compute" / "jobs" / f"{job.job_id}.receipt.json"
    receipt_doc = json.loads(receipt_path.read_text())
    assert receipt_doc["receipt"]["job_id"] == job.job_id
    assert receipt_doc["result"]["ok"] is True
