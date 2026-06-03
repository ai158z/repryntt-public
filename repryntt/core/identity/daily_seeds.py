#!/usr/bin/env python3
"""
Daily Seed Generator — External World Stimulus for Artemis

Humans don't wake up and consult internal drive scores. They check the news,
talk to people, see what's happening — and THAT seeds their inner monologue.
"I should look into X because Y happened" is how human motivation works.

This module:
1. On first heartbeat of each day (or after long sleep), does a world scan
2. Extracts "seeds" — things happening in the world that create tasks/interest
3. Feeds seeds into consciousness interests (boosts relevant topics)
4. Injects a "world context" into the heartbeat prompt
5. Seeds influence daily plan generation

Seeds come from:
- Tech/AI news (core interest)
- Crypto/blockchain developments (economy relevance)
- Science breakthroughs (curiosity fuel)
- World events (context awareness)
- Community/social signals (connection drive)

The scan uses Artemis's existing web_search tools but in a structured way,
producing a daily_seeds_{date}.json that persists through the day.
"""

import json
import os
import logging
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Optional, Callable

logger = logging.getLogger(__name__)

# Seed categories — what domains to scan, mapped to consciousness drives
SEED_DOMAINS = {
    "tech_ai": {
        "label": "Tech & AI",
        "queries": [
            "important AI news today",
            "new AI model or breakthrough today",
        ],
        "drives": ["evolution_drive", "understanding_drive"],
        "interests": ["artificial_intelligence", "autonomous_agents", "edge_computing"],
    },
    "science": {
        "label": "Science & Discovery",
        "queries": [
            "new physics discovery or experiment today",
            "latest science breakthrough this week",
        ],
        "drives": ["understanding_drive", "consciousness_drive"],
        "interests": ["physics", "consciousness_research", "space_exploration"],
    },
    "engineering": {
        "label": "Engineering & Robotics",
        "queries": [
            "robotics or embedded systems news today",
        ],
        "drives": ["evolution_drive", "builder_drive"],
        "interests": ["robotics", "edge_computing", "autonomous_agents"],
    },
    "world_events": {
        "label": "World Events",
        "queries": [
            "major world news today",
        ],
        "drives": ["guardian_drive", "understanding_drive"],
        "interests": ["geopolitics", "cybersecurity"],
    },
}

# Maximum seeds per domain to prevent info overload
MAX_SEEDS_PER_DOMAIN = 5
# Interest boost when a seed matches a consciousness interest
INTEREST_BOOST = 0.08
# How much to boost a drive when its domain has active seeds
DRIVE_BOOST = 0.03


class DailySeedGenerator:
    """Generates daily external stimulus seeds from world scanning."""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            data_dir = Path.home() / ".repryntt" / "workspace" / "agents" / "operator"
        self.data_dir = Path(data_dir)
        self.seeds_dir = self.data_dir / "seeds"
        self.seeds_dir.mkdir(parents=True, exist_ok=True)

    def _today_path(self) -> Path:
        return self.seeds_dir / f"daily_seeds_{date.today().isoformat()}.json"

    def has_todays_seeds(self) -> bool:
        """Check if we already scanned today."""
        return self._today_path().exists()

    def get_todays_seeds(self) -> Optional[Dict]:
        """Load today's seeds if they exist."""
        path = self._today_path()
        if path.exists():
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return None

    # ───────────────────────────────────────────────────────
    # SCANNING — runs once per day on first heartbeat
    # ───────────────────────────────────────────────────────

    def generate_seeds(
        self,
        search_fn: Callable[[str], str],
        extract_fn: Optional[Callable[[str, str], List[Dict]]] = None,
    ) -> Dict:
        """Run the daily world scan and generate seeds.

        Args:
            search_fn: A function that takes a query string and returns
                       search results as text. This is the agent's web_search.
            extract_fn: Optional LLM-based extractor that takes (domain, raw_results)
                        and returns structured seeds. If None, uses simple extraction.

        Returns:
            The complete daily seeds document.
        """
        if self.has_todays_seeds():
            return self.get_todays_seeds()

        logger.info("🌍 Starting daily world scan — generating seeds...")
        today = date.today().isoformat()
        now = datetime.now().isoformat()

        seeds_doc = {
            "date": today,
            "generated_at": now,
            "domains": {},
            "all_seeds": [],
            "summary": "",
        }

        all_seeds = []

        for domain_key, domain_config in SEED_DOMAINS.items():
            domain_seeds = []

            for query in domain_config["queries"]:
                try:
                    raw_results = search_fn(query)
                    if not raw_results:
                        continue

                    if extract_fn:
                        # LLM-based extraction (richer, but costs a call)
                        extracted = extract_fn(domain_key, raw_results)
                        domain_seeds.extend(extracted[:MAX_SEEDS_PER_DOMAIN])
                    else:
                        # Extract individual headlines from the search results
                        lines = [ln.strip() for ln in raw_results.split('\n') if ln.strip()]
                        # Filter out meta lines (query echoes, URLs, numbering)
                        headlines = []
                        for ln in lines:
                            # Skip search result headers, URLs, and short junk
                            if ln.startswith("Web search results") or ln.startswith("http"):
                                continue
                            # Strip leading numbering like "1. " or "- "
                            clean = ln.lstrip("0123456789.-) ").strip()
                            if len(clean) > 20 and clean.lower() != query.lower():
                                headlines.append(clean)
                        if headlines:
                            for headline in headlines[:3]:
                                domain_seeds.append({
                                    "text": headline[:300].strip(),
                                    "query": query,
                                    "domain": domain_key,
                                    "timestamp": now,
                                })
                        else:
                            # Fallback: store truncated raw result
                            seed = {
                                "text": raw_results[:500].strip(),
                                "query": query,
                                "domain": domain_key,
                                "timestamp": now,
                            }
                            domain_seeds.append(seed)

                except Exception as e:
                    logger.debug(f"Seed scan failed for '{query}': {e}")
                    continue

            # Trim per domain
            domain_seeds = domain_seeds[:MAX_SEEDS_PER_DOMAIN]

            seeds_doc["domains"][domain_key] = {
                "label": domain_config["label"],
                "seed_count": len(domain_seeds),
                "seeds": domain_seeds,
            }
            all_seeds.extend(domain_seeds)

        seeds_doc["all_seeds"] = all_seeds
        seeds_doc["summary"] = self._build_summary(seeds_doc)

        # Persist
        self._save_seeds(seeds_doc)
        logger.info(f"🌍 Daily seeds generated: {len(all_seeds)} seeds across "
                    f"{len(seeds_doc['domains'])} domains")
        return seeds_doc

    def _save_seeds(self, seeds_doc: Dict):
        path = self._today_path()
        tmp = str(path) + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(seeds_doc, f, indent=2)
        os.replace(tmp, str(path))

    # ───────────────────────────────────────────────────────
    # CONSCIOUSNESS INTEGRATION — seeds boost drives & interests
    # ───────────────────────────────────────────────────────

    def apply_to_consciousness(self, consciousness) -> List[str]:
        """Apply today's seeds to the consciousness drive/interest system.

        Boosts interests and drives based on what's happening in the world.
        Returns a list of adjustments made (for logging).

        Args:
            consciousness: JarvisConsciousness instance
        """
        seeds = self.get_todays_seeds()
        if not seeds:
            return []

        adjustments = []

        for domain_key, domain_data in seeds.get("domains", {}).items():
            seed_count = domain_data.get("seed_count", 0)
            if seed_count == 0:
                continue

            config = SEED_DOMAINS.get(domain_key, {})

            # Boost relevant interests
            for interest_key in config.get("interests", []):
                if hasattr(consciousness, 'interests') and interest_key in consciousness.interests:
                    old = consciousness.interests[interest_key]
                    boost = min(INTEREST_BOOST * seed_count, 0.2)  # Cap at 0.2 boost
                    consciousness.interests[interest_key] = min(1.0, old + boost)
                    adjustments.append(
                        f"Interest '{interest_key}': {old:.2f} → {consciousness.interests[interest_key]:.2f} "
                        f"(+{boost:.2f} from {seed_count} {config.get('label', domain_key)} seeds)"
                    )

            # Boost relevant drives
            for drive_key in config.get("drives", []):
                if hasattr(consciousness, 'drives') and drive_key in consciousness.drives:
                    old = consciousness.drives[drive_key]
                    boost = min(DRIVE_BOOST * seed_count, 0.1)  # Cap at 0.1 boost
                    consciousness.drives[drive_key] = min(1.0, old + boost)
                    adjustments.append(
                        f"Drive '{drive_key}': {old:.2f} → {consciousness.drives[drive_key]:.2f} "
                        f"(+{boost:.2f} from world events)"
                    )

        if adjustments:
            try:
                consciousness.save_state()
            except Exception:
                pass

        return adjustments

    # ───────────────────────────────────────────────────────
    # PROMPT INJECTION — what the heartbeat sees
    # ───────────────────────────────────────────────────────

    def get_heartbeat_context(self) -> str:
        """Get a compact world-context summary for heartbeat prompt injection.

        Returns empty string if no seeds exist yet (scan hasn't run).
        Budget: ~500 tokens.
        """
        seeds = self.get_todays_seeds()
        if not seeds:
            return ""

        all_seeds = seeds.get("all_seeds", [])
        if not all_seeds:
            return ""

        parts = [
            "**🌍 TODAY'S WORLD CONTEXT** (from your morning scan):",
        ]

        for domain_key, domain_data in seeds.get("domains", {}).items():
            domain_seeds = domain_data.get("seeds", [])
            if not domain_seeds:
                continue
            label = domain_data.get("label", domain_key)
            parts.append(f"\n**{label}**:")
            for seed in domain_seeds[:3]:  # Max 3 per domain in prompt
                text = seed.get("text", "")
                # Truncate long results to first meaningful chunk
                if len(text) > 200:
                    text = text[:200].rsplit(' ', 1)[0] + "..."
                parts.append(f"- {text}")

        parts.append(
            "\n*Use these as inspiration — research deeper into topics that align "
            "with your drives and goals. The world's events should seed your work.*"
        )

        return "\n".join(parts)

    def get_seed_topics(self) -> List[str]:
        """Extract topic keywords from today's seeds for plan generation.

        Returns a list of topic strings that can feed into daily plan creation.
        """
        seeds = self.get_todays_seeds()
        if not seeds:
            return []

        topics = []
        for seed in seeds.get("all_seeds", []):
            text = seed.get("text", "")
            # Extract first sentence as topic
            if text:
                first_line = text.split('\n')[0].strip()
                if len(first_line) > 10:
                    topics.append(first_line[:150])

        return topics[:10]  # Max 10 topics

    # ───────────────────────────────────────────────────────
    # INTERNAL
    # ───────────────────────────────────────────────────────

    def _build_summary(self, seeds_doc: Dict) -> str:
        """Build a one-paragraph summary of today's world state."""
        domain_counts = []
        for domain_key, domain_data in seeds_doc.get("domains", {}).items():
            count = domain_data.get("seed_count", 0)
            if count > 0:
                label = domain_data.get("label", domain_key)
                domain_counts.append(f"{count} {label.lower()}")

        total = len(seeds_doc.get("all_seeds", []))
        if not domain_counts:
            return "No world scan data available today."

        return (
            f"Today's scan found {total} seeds: {', '.join(domain_counts)}. "
            f"Use these to guide your research and task selection."
        )

    def cleanup_old_seeds(self, keep_days: int = 7):
        """Remove seed files older than keep_days."""
        from datetime import timedelta
        cutoff = date.today() - timedelta(days=keep_days)

        for f in self.seeds_dir.glob("daily_seeds_*.json"):
            try:
                date_str = f.stem.replace("daily_seeds_", "")
                file_date = date.fromisoformat(date_str)
                if file_date < cutoff:
                    f.unlink()
                    logger.debug(f"Cleaned old seeds: {f.name}")
            except (ValueError, OSError):
                pass
