"""
SAIGE Robot Economy — Logging Configuration
============================================
Centralized logging for all robot economy components.
Each component gets its own named logger with consistent formatting.
"""

import logging
import os
import sys
from datetime import datetime

# Fix Windows cp1252 console encoding before any log output
from repryntt.platform_utils import fix_windows_encoding
fix_windows_encoding()

# ═══════════════════════════════════════════════════════
#  LOG DIRECTORY
# ═══════════════════════════════════════════════════════

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════
#  FORMATTER
# ═══════════════════════════════════════════════════════

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

# ═══════════════════════════════════════════════════════
#  HANDLER FACTORY
# ═══════════════════════════════════════════════════════

def _make_logger(name: str, filename: str, level=logging.INFO) -> logging.Logger:
    """Create a named logger with console + file handlers."""
    logger = logging.getLogger(name)
    
    # Prevent duplicate handlers on re-import
    if logger.handlers:
        return logger
    
    logger.setLevel(level)
    logger.propagate = False
    
    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    # File handler (rotating per component)
    try:
        filepath = os.path.join(LOG_DIR, filename)
        file_handler = logging.FileHandler(filepath, mode="a", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Could not create log file {filename}: {e}")
    
    return logger

# ═══════════════════════════════════════════════════════
#  COMPONENT LOGGERS
# ═══════════════════════════════════════════════════════

blockchain_logger = _make_logger("blockchain", "blockchain.log")
miner_logger = _make_logger("miner", "miner.log")
contract_logger = _make_logger("contract", "contract.log")
dao_logger = _make_logger("dao", "dao.log")
economy_logger = _make_logger("economy", "economy.log")
p2p_logger = _make_logger("p2p_economy", "p2p_economy.log")
integrity_logger = _make_logger("integrity", "integrity.log")
