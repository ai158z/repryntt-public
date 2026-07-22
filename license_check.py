#!/usr/bin/env python3
"""
repryntt commercial license verifier — fully OFFLINE.

Usage:
    python3 license_check.py <license-key>
    python3 license_check.py            # reads REPRYNTT_COMMERCIAL_LICENSE
                                        # or ~/.repryntt/license.key

A commercial license key is an Ed25519-signed record of your license:
who it's for, when it was issued, when it expires. The signature is checked
against the public key below — no network, works air-gapped. The key is your
proof-of-license artifact; the license itself is the agreement in
COMMERCIAL-LICENSE.md plus your purchase receipt.

Verification online (equivalent): https://api.repryntt.com/v1/license/verify
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# repryntt's Ed25519 license-signing public key (raw, base64).
REPRYNTT_LICENSE_PUBKEY_B64 = "MrNdTM0gcI3KiN4Z2A8rQ6wZGnhNBxvXpaExnZAJRY4="


def _b64pad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(key: str) -> dict:
    """Return the license payload if the signature is valid, else raise."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    body_b64, sig_b64 = key.strip().split(".", 1)
    body, sig = _b64pad(body_b64), _b64pad(sig_b64)
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(REPRYNTT_LICENSE_PUBKEY_B64))
    pub.verify(sig, body)                     # raises on any tampering
    return json.loads(body)


def main() -> int:
    key = (sys.argv[1] if len(sys.argv) > 1 else
           os.environ.get("REPRYNTT_COMMERCIAL_LICENSE", "").strip())
    if not key:
        p = Path.home() / ".repryntt" / "license.key"
        key = p.read_text().strip() if p.is_file() else ""
    if not key:
        print("no license key given (arg, $REPRYNTT_COMMERCIAL_LICENSE, "
              "or ~/.repryntt/license.key) — running under AGPL-3.0 terms.")
        return 1
    try:
        payload = verify(key)
    except Exception:
        print("❌ INVALID — signature does not verify (tampered or malformed).")
        return 2
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expired = str(payload.get("expires", "")) < today
    print(f"{'⚠️ EXPIRED' if expired else '✅ VALID'} commercial license")
    print(f"   id:       {payload.get('id')}")
    print(f"   licensee: {payload.get('licensee')}")
    print(f"   issued:   {payload.get('issued')}   expires: {payload.get('expires')}")
    print(f"   terms:    {payload.get('terms')}")
    if expired:
        print("   note: lapsed subscription → AGPL-3.0 terms govern (see "
              "COMMERCIAL-LICENSE.md §3). Renew at repryntt.com/commercial")
    return 0 if not expired else 3


if __name__ == "__main__":
    raise SystemExit(main())
