#!/usr/bin/env python3
"""Dependency license audit — guards the AGPL-3.0 + commercial story.

Scans every package installed in the current environment and fails
(exit 1) if any carries a license that is either incompatible with
distributing repryntt under AGPL-3.0 or unsafe for commercial use:

  BLOCKED:
    - GPL-2.0-only        (incompatible with AGPL-3.0; "or later" is fine)
    - SSPL                (MongoDB-style, not open source)
    - BUSL / Business Source License (HashiCorp-style)
    - Commons Clause
    - Elastic License
    - CC-BY-NC / any "non-commercial" grant

  FINE (flows into AGPL-3.0 one-way):
    MIT, BSD, Apache-2.0, ISC, PSF, Zlib, MPL-2.0, Unlicense, CC0,
    LGPL (any version), GPL-2.0-or-later, GPL-3.0, AGPL-3.0.

Run locally:   python scripts/license_audit.py
CI runs this on every push. To audit the full optional-dependency set:
    pip install -e ".[all]" && python scripts/license_audit.py

Related guard: repryntt/hardware/depth_perception.py refuses to load
ML models that aren't on its commercial-safe allowlist (Depth Anything
V2 Base/Large/Giant are CC-BY-NC-4.0 — only Small is Apache-2.0).
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

# Packages whose PyPI metadata is wrong/missing but whose license has
# been manually verified. Map: normalized name -> reason string.
ALLOWLIST: dict[str, str] = {
    # "some-pkg": "metadata says X but repo LICENSE is Apache-2.0 (checked 2026-07-01)",
}

BLOCKED_PATTERNS = [
    (re.compile(r"\bSSPL\b|Server Side Public License", re.I), "SSPL"),
    (re.compile(r"\bBUSL\b|Business Source License", re.I), "BUSL"),
    (re.compile(r"Commons Clause", re.I), "Commons Clause"),
    (re.compile(r"Elastic License", re.I), "Elastic License"),
    (re.compile(r"CC[- ]?BY[- ]?NC|Non[- ]?Commercial", re.I), "Non-commercial (CC-BY-NC-style)"),
]

# GPL-2 detection: block only the "v2 only" form. LGPL/AGPL/GPLv3 and
# "v2 or later" are all AGPL-3.0-compatible. Dual licenses that offer a
# permissive alternative ("GPL-2.0 OR MIT") are fine too.
GPL2 = re.compile(r"GPL[ -]?v?2(?:\.0)?(?!\d)", re.I)
GPL2_ESCAPE = re.compile(r"or[- ]later|GPL[ -]?v?2(?:\.0)?\+|Lesser|LGPL|Affero|AGPL|GPL[ -]?v?3", re.I)
PERMISSIVE_ALTERNATIVE = re.compile(r"\bMIT\b|\bBSD\b|Apache|\bISC\b", re.I)


def license_blob(dist: metadata.Distribution) -> str:
    """All license-relevant metadata for a distribution, joined."""
    md = dist.metadata
    parts = [
        md.get("License-Expression") or "",
        (md.get("License") or "")[:400],  # some embed full license text
    ]
    parts += [c for c in md.get_all("Classifier") or [] if c.startswith("License")]
    return " ".join(parts)


def check(name: str, blob: str) -> str | None:
    """Return a violation description, or None if the package is fine."""
    for pattern, label in BLOCKED_PATTERNS:
        if pattern.search(blob):
            return label
    if GPL2.search(blob) and not GPL2_ESCAPE.search(blob) \
            and not PERMISSIVE_ALTERNATIVE.search(blob):
        return "GPL-2.0-only (incompatible with AGPL-3.0)"
    return None


def main() -> int:
    violations: list[tuple[str, str, str]] = []
    unknown: list[str] = []
    scanned = 0

    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        scanned += 1
        if name.lower() in ALLOWLIST:
            continue
        blob = license_blob(dist)
        if not blob.strip():
            unknown.append(name)
            continue
        problem = check(name, blob)
        if problem:
            violations.append((name, problem, blob[:120]))

    print(f"License audit: {scanned} packages scanned")
    if unknown:
        print(f"  ℹ️ {len(unknown)} with no license metadata (not failing): "
              + ", ".join(sorted(unknown)[:10]))

    if violations:
        print(f"\n❌ {len(violations)} BLOCKED license(s) found:\n")
        for name, problem, blob in sorted(violations):
            print(f"  {name}: {problem}")
            print(f"      metadata: {blob}")
        print(
            "\nFix: remove the dependency, replace it, or — if the "
            "metadata is wrong and the real license is verified safe — "
            "add it to ALLOWLIST in scripts/license_audit.py with a reason."
        )
        return 1

    print("✅ No AGPL-incompatible or non-commercial licenses found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
