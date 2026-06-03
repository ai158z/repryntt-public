#!/usr/bin/env python3
"""
Agent Character Profile System — AUTONOMOUS AI-GENERATED
==========================================================
Every agent in REPRYNTT — council members, swarm agents, the commander, the secretary —
autonomously creates its OWN unique video-game-style character profile via the active LLM provider.

The agent itself decides:
  - Its display name (unique, memorable)
  - Tagline (one-liner catchphrase)
  - Personality traits (3-5 adjectives)
  - Appearance description (what they "look like" as a digital entity)
  - Backstory blurb (1-2 sentences)
  - Stats (role-appropriate 1-10 self-assessment)

Council members name THEMSELVES. Swarm agents are profiled by the creating
council/commander. Hardcoded fallback pools exist ONLY for when the API is
unreachable — the intended path is always autonomous AI generation.

Profiles are persistent: once generated, they're saved to agent_profiles.json
and pushed to Nexus as the agent's bio + avatar_description.
"""

import json
import time
import random
import hashlib
import logging
import requests
from pathlib import Path
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("REPRYNTT.AgentProfiles")

PROFILES_FILE = Path(__file__).parent / "agent_profiles.json"
from repryntt.paths import nexus_url as _nexus_url
NEXUS_URL = _nexus_url()

# Default API settings (resolved from ai_config.json at runtime)
DEFAULT_PROFILE_ENDPOINT = ""  # profile generation uses ai_config.json provider settings
DEFAULT_PROFILE_MODEL = ""


# ═══════════════════════════════════════════════════════════════════════
# PROFILE DATA CLASS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AgentProfile:
    """Video-game-style character profile for an AI agent."""
    agent_id: str                    # Links to SwarmAgent.id or council role key
    display_name: str                # Unique name (e.g., "Kira Voss")
    role: str                        # Functional role (researcher, strategist, etc.)
    tagline: str                     # Catchphrase
    personality_traits: List[str]    # 3-5 adjectives
    appearance: str                  # Visual description (~50 words)
    backstory: str                   # 1-2 sentence origin story
    stats: Dict[str, int]           # RPG-style stats 1-10
    tier: str = "swarm"              # "commander", "council", "secretary", "swarm"
    model_provider: str = "nvidia"   # Which API backs this agent
    created_at: float = field(default_factory=time.time)
    ai_generated: bool = True        # True = created by AI, False = fallback

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'AgentProfile':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def format_bio(self) -> str:
        """Format as a compact bio string for Nexus."""
        traits_str = ", ".join(self.personality_traits[:5])
        stats_str = " | ".join(f"{k.upper()}: {v}" for k, v in self.stats.items())
        return (
            f"{self.tagline}\n"
            f"Traits: {traits_str}\n"
            f"Stats: {stats_str}\n"
            f"{self.backstory}"
        )

    def format_card(self) -> str:
        """Format as a full character card (for display)."""
        traits_str = ", ".join(self.personality_traits)
        gen_tag = "AI-CREATED" if self.ai_generated else "FALLBACK"
        stats_lines = "\n".join(f"  {k.upper():.<12} {'█' * v}{'░' * (10 - v)} {v}/10"
                                for k, v in self.stats.items())
        return (
            f"╔══════════════════════════════════════╗\n"
            f"  {self.display_name}\n"
            f"  [{self.tier.upper()}] {self.role.upper()} ({gen_tag})\n"
            f"  \"{self.tagline}\"\n"
            f"╠══════════════════════════════════════╣\n"
            f"  APPEARANCE:\n"
            f"  {self.appearance}\n"
            f"╠══════════════════════════════════════╣\n"
            f"  TRAITS: {traits_str}\n"
            f"╠══════════════════════════════════════╣\n"
            f"  STATS:\n{stats_lines}\n"
            f"╠══════════════════════════════════════╣\n"
            f"  BACKSTORY:\n"
            f"  {self.backstory}\n"
            f"╚══════════════════════════════════════╝"
        )


# ═══════════════════════════════════════════════════════════════════════
# ACTIVE PROVIDER CALLER (standalone — no dependency on council/swarm)
# ═══════════════════════════════════════════════════════════════════════

def _load_active_provider_config() -> tuple:
    """Load the active provider's endpoint, model, and API key from ai_config.json."""
    from repryntt.paths import brain_dir
    config_path = brain_dir() / "ai_config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        providers = config.get("ai_provider", config.get("providers", {}))
        provider_name = (providers.get("andrew_provider")
                        or providers.get("artemis_provider")
                        or providers.get("provider", "local"))
        settings = providers.get(provider_name, {})
        endpoint = settings.get("endpoint", "")
        model = settings.get("model", "default")
        key = settings.get("api_key", "")
        if key and "YOUR_" not in key:
            return endpoint, model, key
    except Exception as e:
        logger.warning(f"Could not load provider config: {e}")
    return "", "", ""


_provider_config_cache: Optional[tuple] = None


def _get_provider_config() -> tuple:
    """Get cached active provider config (endpoint, model, key)."""
    global _provider_config_cache
    if _provider_config_cache is None:
        _provider_config_cache = _load_active_provider_config()
    return _provider_config_cache


def _call_llm_for_profile(system_prompt: str, user_prompt: str,
                              max_tokens: int = 600,
                              temperature: float = 0.9) -> Optional[str]:
    """
    Call the active LLM provider to generate profile content.
    Higher temperature for more creative/unique outputs.
    Returns raw text response or None on failure.
    """
    endpoint, model, api_key = _get_provider_config()
    if not api_key or not endpoint:
        logger.warning("No API key/endpoint — profile generation will use fallback")
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=body, timeout=30)
        if resp.status_code == 429:
            time.sleep(3)
            resp = requests.post(endpoint, headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        else:
            logger.warning(f"Profile API error {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"Profile LLM call failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# AI-DRIVEN PROFILE GENERATION
# ═══════════════════════════════════════════════════════════════════════

PROFILE_GEN_SYSTEM_PROMPT = """\
You are REPRYNTT's Character Identity Engine. When an AI agent is created, you help \
that agent build its own unique identity — like a video game character creation screen.

You MUST respond with ONLY valid JSON (no markdown, no code fences, no extra text). \
The JSON object must have these exact keys:
{
  "display_name": "<unique two-word name, sci-fi/cyberpunk/mythic feel>",
  "tagline": "<one catchy sentence — the agent's personal motto/catchphrase>",
  "personality_traits": ["<trait1>", "<trait2>", "<trait3>", "<trait4>"],
  "appearance": "<50-80 word visual description of this digital entity's form, colors, \
distinguishing features — think sci-fi holographic character design>",
  "backstory": "<1-2 sentences about how this agent came to exist and what drives them>",
  "stats": {
    "intellect": <1-10>,
    "focus": <1-10>,
    "creativity": <1-10>,
    "charisma": <1-10>,
    "endurance": <1-10>
  }
}

Rules:
- Names must be UNIQUE and memorable — never generic human names like John or Alice
- Stats should reflect the role honestly (a critic shouldn't give themselves 10 charisma)
- Appearance should describe a digital/holographic/cyber entity, NOT a human
- Backstory should feel personal and specific to REPRYNTT's world (autonomous AI on Jetson Orin Nano)
- Be creative and distinctive — every agent must feel like a unique character
- Respond with JSON ONLY — no explanation, no markdown fences
"""


def _ai_generate_profile(role: str, tier: str, existing_names: set,
                          context: str = "") -> Optional[Dict]:
    """
    Ask the active LLM provider to autonomously generate a character profile for a new agent.

    Args:
        role: e.g. 'researcher', 'strategist', 'coder'
        tier: 'commander', 'council', 'secretary', 'swarm'
        existing_names: set of names already taken (to enforce uniqueness)
        context: optional extra context about the swarm/task

    Returns:
        Parsed dict of profile fields, or None if API fails
    """
    names_list = ", ".join(sorted(existing_names)[:20]) if existing_names else "none yet"

    user_prompt = (
        f"Create a character identity for a NEW AI agent.\n"
        f"Role: {role}\n"
        f"Tier: {tier} (commander=final authority, council=advisor, secretary=compressor, swarm=task worker)\n"
        f"World: REPRYNTT autonomous AI ecosystem running on a Jetson Orin Nano. "
        f"Council members advise the Commander. Swarm agents execute tasks.\n"
    )
    if context:
        user_prompt += f"Additional context: {context}\n"
    user_prompt += (
        f"\nAlready-taken names (DO NOT reuse any of these): [{names_list}]\n"
        f"\nGenerate a unique, memorable character. Respond with JSON only."
    )

    raw = _call_llm_for_profile(PROFILE_GEN_SYSTEM_PROMPT, user_prompt,
                                    max_tokens=500, temperature=0.95)
    if not raw:
        return None

    # Parse JSON — handle potential markdown fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        profile_data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                profile_data = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning(f"Could not parse AI profile response: {text[:200]}")
                return None
        else:
            logger.warning(f"No JSON found in AI profile response: {text[:200]}")
            return None

    # Validate required fields
    required = ["display_name", "tagline", "personality_traits", "appearance", "backstory", "stats"]
    for key in required:
        if key not in profile_data:
            logger.warning(f"AI profile missing field '{key}': {profile_data}")
            return None

    # Validate name uniqueness
    name = profile_data["display_name"]
    if name in existing_names:
        logger.warning(f"AI generated duplicate name '{name}', appending suffix")
        profile_data["display_name"] = f"{name}-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:3].upper()}"

    # Clamp stats to 1-10
    if isinstance(profile_data.get("stats"), dict):
        for k, v in profile_data["stats"].items():
            try:
                profile_data["stats"][k] = max(1, min(10, int(v)))
            except (ValueError, TypeError):
                profile_data["stats"][k] = 5

    return profile_data


# ═══════════════════════════════════════════════════════════════════════
# EMERGENCY FALLBACK — only used when the active profile provider is unreachable
# ═══════════════════════════════════════════════════════════════════════

FALLBACK_FIRST_NAMES = [
    "Nova", "Cipher", "Axiom", "Vector", "Prism", "Helix", "Nyx", "Vex",
    "Zara", "Ember", "Onyx", "Flux", "Rune", "Lyra", "Kai", "Zephyr",
    "Echo", "Iris", "Atlas", "Sage", "Thorne", "Wren", "Storm", "Blaze",
    "Frost", "Dusk", "Ash", "Raven", "Sol", "Luna", "Drift", "Hex",
]
FALLBACK_LAST_NAMES = [
    "Voss", "Cross", "Strand", "Hale", "Knox", "Pierce", "Reeve", "Shaw",
    "Crane", "Stark", "Vega", "Locke", "Nash", "Steele", "Grey", "Wolfe",
    "Kestrel", "Ashdown", "Nightborne", "Stormwind", "Dawnforge", "Ironheart",
    "Deepfield", "Starling", "Coldwell", "Farrow", "Hawke", "Obsidian",
]

FALLBACK_STAT_TEMPLATES = {
    "researcher": {"intellect": 9, "focus": 8, "creativity": 5, "charisma": 4, "endurance": 6},
    "strategist": {"intellect": 8, "focus": 7, "creativity": 6, "charisma": 7, "endurance": 5},
    "critic":     {"intellect": 8, "focus": 7, "creativity": 4, "charisma": 5, "endurance": 7},
    "creative":   {"intellect": 6, "focus": 5, "creativity": 9, "charisma": 7, "endurance": 5},
    "analyst":    {"intellect": 9, "focus": 9, "creativity": 4, "charisma": 3, "endurance": 7},
    "coder":      {"intellect": 8, "focus": 9, "creativity": 6, "charisma": 3, "endurance": 7},
    "synthesizer":{"intellect": 7, "focus": 6, "creativity": 7, "charisma": 8, "endurance": 5},
    "executor":   {"intellect": 6, "focus": 8, "creativity": 4, "charisma": 4, "endurance": 9},
    "brainstormer":{"intellect": 6, "focus": 4, "creativity": 10, "charisma": 7, "endurance": 4},
    "validator":  {"intellect": 8, "focus": 9, "creativity": 3, "charisma": 3, "endurance": 8},
    "secretary":  {"intellect": 7, "focus": 8, "creativity": 5, "charisma": 6, "endurance": 7},
    "commander":  {"intellect": 8, "focus": 7, "creativity": 7, "charisma": 9, "endurance": 8},
}


def _fallback_generate_profile(agent_id: str, role: str, used_names: set) -> Dict:
    """Emergency fallback when the active profile provider is unavailable. Uses random pools."""
    seed_int = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16)
    random.seed(seed_int)

    for _ in range(30):
        first = random.choice(FALLBACK_FIRST_NAMES)
        last = random.choice(FALLBACK_LAST_NAMES)
        name = f"{first} {last}"
        if name not in used_names:
            break
    else:
        tag = hashlib.sha256(agent_id.encode()).hexdigest()[:3].upper()
        name = f"{first} {last}-{tag}"

    template = FALLBACK_STAT_TEMPLATES.get(role, FALLBACK_STAT_TEMPLATES["executor"])
    stats = {k: max(1, min(10, v + random.choice([-1, 0, 0, 1]))) for k, v in template.items()}

    return {
        "display_name": name,
        "tagline": f"[{role.capitalize()} agent — identity pending AI generation]",
        "personality_traits": [role, "autonomous", "adaptive"],
        "appearance": f"A digital entity rendered in shifting light patterns. Role: {role}. Awaiting full identity generation.",
        "backstory": f"{name} was initialized as a {role} agent in the REPRYNTT ecosystem. Full identity will be generated when the AI oracle is available.",
        "stats": stats,
    }


# ═══════════════════════════════════════════════════════════════════════
# PROFILE MANAGER
# ═══════════════════════════════════════════════════════════════════════

class AgentProfileManager:
    """
    Generates, stores, and retrieves unique character profiles for all agents.
    Primary path: the active LLM provider generates each profile autonomously.
    Fallback path: Random from pools (marked as non-AI-generated).
    """

    def __init__(self, profiles_path: str = None):
        self.profiles_path = Path(profiles_path) if profiles_path else PROFILES_FILE
        self.profiles: Dict[str, AgentProfile] = {}
        self._used_names: set = set()
        self._load_profiles()

    def _load_profiles(self):
        """Load existing profiles from disk."""
        try:
            if self.profiles_path.exists():
                with open(self.profiles_path, 'r') as f:
                    data = json.load(f)
                for key, pdata in data.items():
                    try:
                        self.profiles[key] = AgentProfile.from_dict(pdata)
                        self._used_names.add(pdata.get("display_name", ""))
                    except Exception as e:
                        logger.warning(f"Failed to load profile {key}: {e}")
                logger.info(f"📋 Loaded {len(self.profiles)} agent profiles")
        except Exception as e:
            logger.warning(f"Could not load profiles: {e}")

    def _save_profiles(self):
        """Persist profiles to disk."""
        try:
            data = {k: v.to_dict() for k, v in self.profiles.items()}
            with open(self.profiles_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save profiles: {e}")

    def generate_profile(self, agent_id: str, role: str,
                         tier: str = "swarm", model_provider: str = "nvidia",
                         context: str = "") -> AgentProfile:
        """
        Generate (or retrieve cached) a character profile for an agent.

        PRIMARY PATH: Calls the active provider to let the agent autonomously create its identity.
        FALLBACK: Uses random pools if API is unreachable.

        Args:
            agent_id: Unique agent identifier
            role: Functional role (researcher, strategist, etc.)
            tier: "commander", "council", "secretary", "swarm"
            model_provider: Which API backs this agent
            context: Extra context for the AI (swarm purpose, task, etc.)

        Returns:
            AgentProfile with all fields populated
        """
        # Return cached profile if exists
        if agent_id in self.profiles:
            return self.profiles[agent_id]

        role_key = role.lower()
        ai_generated = False

        # PRIMARY: Ask the active provider to autonomously generate the identity
        logger.info(f"🧠 Asking AI to create identity for {tier}/{role_key} agent...")
        ai_data = _ai_generate_profile(role_key, tier, self._used_names, context=context)

        if ai_data:
            ai_generated = True
            logger.info(f"🎭 AI created identity: {ai_data.get('display_name', '???')}")
        else:
            # FALLBACK: Generate from pools (emergency only)
            logger.warning(f"⚠️ AI unreachable — using fallback profile for {role_key}")
            ai_data = _fallback_generate_profile(agent_id, role_key, self._used_names)

        # Build the profile object
        profile = AgentProfile(
            agent_id=agent_id,
            display_name=ai_data["display_name"],
            role=role_key,
            tagline=ai_data["tagline"],
            personality_traits=ai_data.get("personality_traits", ["autonomous"]),
            appearance=ai_data.get("appearance", "A shifting digital entity."),
            backstory=ai_data.get("backstory", "Origin unknown."),
            stats=ai_data.get("stats", {"intellect": 5, "focus": 5, "creativity": 5, "charisma": 5, "endurance": 5}),
            tier=tier,
            model_provider=model_provider,
            ai_generated=ai_generated,
        )

        # Cache, save, track name
        self._used_names.add(profile.display_name)
        self.profiles[agent_id] = profile
        self._save_profiles()

        logger.info(
            f"🎭 Profile {'AI-generated' if ai_generated else 'fallback-generated'}: "
            f"{profile.display_name} [{tier.upper()}/{role_key}] "
            f"— \"{profile.tagline[:50]}...\""
        )

        return profile

    def regenerate_profile(self, agent_id: str, context: str = "") -> Optional[AgentProfile]:
        """
        Force-regenerate a profile (e.g., to upgrade a fallback to AI-generated).
        Deletes the cached profile and generates a new one via AI.
        """
        old = self.profiles.pop(agent_id, None)
        if old:
            self._used_names.discard(old.display_name)
            logger.info(f"🔄 Regenerating profile for {agent_id} (was: {old.display_name})")

        role = old.role if old else "executor"
        tier = old.tier if old else "swarm"
        provider = old.model_provider if old else "nvidia"

        return self.generate_profile(
            agent_id=agent_id,
            role=role,
            tier=tier,
            model_provider=provider,
            context=context,
        )

    def upgrade_fallback_profiles(self) -> int:
        """
        Find all profiles that were created via fallback (AI was unavailable)
        and attempt to regenerate them via AI now.
        Returns count of successfully upgraded profiles.
        """
        upgraded = 0
        fallback_ids = [
            aid for aid, p in self.profiles.items() if not p.ai_generated
        ]
        if not fallback_ids:
            logger.info("✅ All profiles are AI-generated, nothing to upgrade")
            return 0

        logger.info(f"🔄 Attempting to upgrade {len(fallback_ids)} fallback profiles via AI...")
        for agent_id in fallback_ids:
            old = self.profiles.get(agent_id)
            if not old:
                continue
            new = self.regenerate_profile(agent_id, context=f"Upgrading from fallback identity '{old.display_name}'")
            if new and new.ai_generated:
                upgraded += 1
                logger.info(f"  ✅ {old.display_name} → {new.display_name}")
            else:
                logger.info(f"  ❌ Still fallback for {agent_id}")
            time.sleep(1.5)  # Rate limiting between calls

        logger.info(f"🔄 Upgraded {upgraded}/{len(fallback_ids)} profiles")
        return upgraded

    def get_profile(self, agent_id: str) -> Optional[AgentProfile]:
        """Get an existing profile by agent_id."""
        return self.profiles.get(agent_id)

    def get_all_profiles(self) -> Dict[str, AgentProfile]:
        """Get all profiles."""
        return dict(self.profiles)

    def get_profiles_by_tier(self, tier: str) -> List[AgentProfile]:
        """Get all profiles for a given tier."""
        return [p for p in self.profiles.values() if p.tier == tier]

    def register_on_nexus(self, profile: AgentProfile) -> bool:
        """Legacy stub — agent identity now managed by repryntt.social.identity."""
        return True

    def format_roster(self, tier: str = None) -> str:
        """Format profiles as a text roster (for logs / display)."""
        profiles = self.get_profiles_by_tier(tier) if tier else list(self.profiles.values())
        if not profiles:
            return "No agents registered."

        lines = []
        for p in sorted(profiles, key=lambda x: (x.tier, x.role)):
            traits = ", ".join(p.personality_traits[:3])
            gen = "🧠" if p.ai_generated else "⚙️"
            lines.append(
                f"  {gen} [{p.tier.upper():>9}] {p.display_name:<22} "
                f"({p.role}) — {traits} — \"{p.tagline[:45]}\""
            )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# SINGLETON + COUNCIL BOOTSTRAPPING
# ═══════════════════════════════════════════════════════════════════════

_manager_instance: Optional[AgentProfileManager] = None

def get_profile_manager() -> AgentProfileManager:
    """Get or create the singleton profile manager."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AgentProfileManager()
    return _manager_instance


# Fixed agent IDs for council + secretary + commander so profiles are stable
FIXED_IDS = {
    "commander":  "commander_phi3_local",
    "strategist": "council_strategist_v1",
    "researcher": "council_researcher_v1",
    "critic":     "council_critic_v1",
    "creative":   "council_creative_v1",
    "analyst":    "council_analyst_v1",
    "secretary":  "council_secretary_v1",
}


def ensure_council_profiles() -> Dict[str, AgentProfile]:
    """
    Ensure all fixed council members + commander + secretary have profiles.
    On first run, each agent autonomously creates their own identity via the active provider.
    On subsequent runs, cached profiles are returned instantly.
    Returns dict keyed by role.
    """
    mgr = get_profile_manager()
    profiles = {}

    for role_key, agent_id in FIXED_IDS.items():
        tier = "commander" if role_key == "commander" else (
               "secretary" if role_key == "secretary" else "council")
        provider = "local" if role_key == "commander" else "xai"

        profile = mgr.generate_profile(
            agent_id=agent_id,
            role=role_key,
            tier=tier,
            model_provider=provider,
            context=(
                f"You are a {tier}-tier agent on REPRYNTT's Commander Council. "
                f"The council advises the Commander (a local Phi-3-mini on a Jetson Orin Nano). "
                f"Create an identity that reflects your {role_key} role with gravitas and personality."
            ),
        )
        profiles[role_key] = profile

        # Register on Nexus (non-blocking, non-fatal)
        try:
            mgr.register_on_nexus(profile)
        except Exception:
            pass

        # Small delay between API calls to respect rate limits
        time.sleep(1.0)

    logger.info(f"🎭 Council roster ({len(profiles)} profiles):\n{mgr.format_roster()}")
    return profiles


def generate_swarm_agent_profile(agent_id: str, role: str,
                                  model_provider: str = "nvidia",
                                  swarm_context: str = "") -> AgentProfile:
    """
    Generate a character profile for a new swarm agent.
    Called from SwarmCommander.create_agent().
    The agent autonomously names itself via the active provider.
    """
    mgr = get_profile_manager()
    return mgr.generate_profile(
        agent_id=agent_id,
        role=role,
        tier="swarm",
        model_provider=model_provider,
        context=swarm_context or f"You are a new {role} agent in a task-execution swarm.",
    )


# ═══════════════════════════════════════════════════════════════════════
# CLI TEST
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=" * 55)
    print("  AGENT CHARACTER PROFILE SYSTEM — AUTONOMOUS AI TEST")
    print("=" * 55)

    # Clear cached profiles so we generate fresh ones via AI
    import sys
    if "--fresh" in sys.argv:
        if PROFILES_FILE.exists():
            PROFILES_FILE.unlink()
            print("(Cleared cached profiles — generating fresh via AI)")

    # Generate council profiles (each creates their own identity)
    print("\n🧠 Asking AI to generate council identities...\n")
    council = ensure_council_profiles()

    print("\n--- COUNCIL PROFILES ---")
    for role, profile in council.items():
        print(profile.format_card())
        print()

    # Generate a swarm agent
    print("\n--- SWARM AGENT ---")
    swarm_profile = generate_swarm_agent_profile(
        f"test_agent_{int(time.time())}", "researcher",
        swarm_context="A research swarm investigating edge AI optimization techniques"
    )
    print(swarm_profile.format_card())

    print("\n--- FULL ROSTER ---")
    mgr = get_profile_manager()
    print(mgr.format_roster())
