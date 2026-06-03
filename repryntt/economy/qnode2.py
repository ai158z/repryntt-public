"""
Proof of Power Blockchain Node (Production Grade)

Transform from wasteful Proof of Work to productive Proof of Power:
- Transactions: Formal transaction system with audit trail
- Real AI Computation: Replace hash puzzles with actual AI workload processing
- Dynamic Rewards: Pay proportional to verified computational contribution
- Verification: Deterministic, checkpoint, and consensus-based validation
- Security: Stake requirements, reputation system, penalty mechanisms

---

This file is part of the Repryntt blockchain system.
Copyright (C) 2026 Repryntt Foundation

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import hashlib
import json
import math
import time
import socket
import threading
import pickle # DEPRECATED — kept only for legacy fallback detection
import argparse
import struct
import os
import re
import tempfile
import http.server
import socketserver
from datetime import datetime
from typing import Optional, Dict, Any, List

# Cross-platform file locking
try:
    import fcntl
    def _try_lock(lock_f):
        fcntl.flock(lock_f._fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except ImportError:
    try:
        import msvcrt
        def _try_lock(lock_f):
            msvcrt.locking(lock_f._fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        def _try_lock(lock_f):
            pass  # No file locking available on this platform

# SECURITY: Safe serialization replaces pickle for all network communication
from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

# Import our new systems
from repryntt.economy.transaction import Transaction, TransactionPool, create_reward_transaction, create_fee_transaction, create_penalty_transaction
from repryntt.economy.proof_of_power import ProofOfPower
from repryntt.economy.smartcontracts import WorkloadContract
from repryntt.economy.dao import PlanetaryDAO
from repryntt.economy.code_integrity import verify_code_integrity, verify_blockchain_checkpoints
from repryntt.economy.logging_config import blockchain_logger
