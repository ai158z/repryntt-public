"""
Companion configuration — name, voice, personality sliders.

Stored at ~/.repryntt/brain/companion_config.json.
All fields have safe defaults so the system works even if the file is missing.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".repryntt" / "brain" / "companion_config.json"

AVAILABLE_VOICES = [
    "artemis",   # default — warm, clear
    "nova",      # calm, measured
    "echo",      # bright, energetic
    "sage",      # deep, thoughtful
    "lyra",      # gentle, soft
]


@dataclass
class CompanionConfig:
    name: str = "Artemis"
    voice: str = "artemis"
    # Personality sliders — 0.0 to 1.0
    warmth: float = 0.7          # 0 = professional, 1 = warm
    curiosity: float = 0.8       # 0 = reserved, 1 = curious/adventurous
    verbosity: float = 0.5       # 0 = terse, 1 = chatty
    proactivity: float = 0.6     # 0 = waits to be asked, 1 = reaches out often
    # Feature flags
    daily_rituals_enabled: bool = True   # morning greeting + evening check-in
    proactive_outreach_enabled: bool = True  # companion initiates conversations
    # Push relay
    push_device_token: Optional[str] = None
    push_relay_url: Optional[str] = None
    # Metadata
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CompanionConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def personality_summary(self) -> str:
        """One-line description of personality for injection into prompts."""
        warmth_word = "warm" if self.warmth >= 0.6 else ("neutral" if self.warmth >= 0.4 else "professional")
        curiosity_word = "curious" if self.curiosity >= 0.6 else ("calm" if self.curiosity >= 0.4 else "reserved")
        verbosity_word = "chatty" if self.verbosity >= 0.6 else ("balanced" if self.verbosity >= 0.4 else "concise")
        return f"{warmth_word}, {curiosity_word}, {verbosity_word}"


def load_companion_config(path: Path = DEFAULT_CONFIG_PATH) -> CompanionConfig:
    """Load companion config from disk, returning defaults if file is missing or corrupt."""
    if not path.exists():
        return CompanionConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return CompanionConfig.from_dict(data)
    except Exception as e:
        logger.warning(f"Failed to load companion config from {path}: {e} — using defaults")
        return CompanionConfig()


def save_companion_config(config: CompanionConfig, path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Save companion config to disk. Returns True on success."""
    from datetime import datetime, timezone
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = config.to_dict()
        if data.get("created_at") is None:
            data["created_at"] = datetime.now(timezone.utc).isoformat()
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save companion config to {path}: {e}")
        return False
