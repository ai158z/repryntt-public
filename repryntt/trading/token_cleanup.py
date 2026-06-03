import asyncio
import aiofiles
import os
import shutil
import json
import logging
from datetime import datetime

# Setup logging
logger = logging.getLogger("ProfileCleaner")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)

# Constants
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_MODULE_DIR, "data")
TOKEN_PROFILES_DIR = os.path.join(_DATA_DIR, "token_profiles")
ARCHIVE_DIR = os.path.join(_DATA_DIR, "token_profiles_archive")
ACTIVE_TOKENS_FILE = os.path.join(_DATA_DIR, "active_tokens.json")
CHECK_INTERVAL = 30  # 30 seconds
MAX_TOP_20_HOLDERS_PERCENTAGE = 90.0  # Maximum allowed percentage for top 20 holders

os.makedirs(ARCHIVE_DIR, exist_ok=True)

async def load_active_tokens():
    """Load active token addresses from active_tokens.json."""
    try:
        async with aiofiles.open(ACTIVE_TOKENS_FILE, "r") as f:
            content = await f.read()
            tokens = json.loads(content)
            return {token["address"] for token in tokens}
    except Exception as e:
        logger.error(f"Error loading active tokens: {e}")
        return set()

async def check_token_profile(filepath):
    """Check if a token profile should be archived based on top 20 holders percentage."""
    try:
        async with aiofiles.open(filepath, "r") as f:
            content = await f.read()
            profile_data = json.loads(content)
            top_20_holders_pct = profile_data.get("top_20_holders_percentage", 0.0)
            
            if top_20_holders_pct >= MAX_TOP_20_HOLDERS_PERCENTAGE:
                logger.warning(f"Token {profile_data.get('address')} has high holder concentration: {top_20_holders_pct}%")
                return True
    except Exception as e:
        logger.error(f"Error checking token profile {filepath}: {e}")
    return False

async def clean_token_profiles():
    """Move non-active token profiles to archive and remove tokens with high holder concentration."""
    while True:
        try:
            # Load active tokens
            active_tokens = await load_active_tokens()
            logger.info(f"Loaded {len(active_tokens)} active tokens from {ACTIVE_TOKENS_FILE}")

            # Scan TOKEN_PROFILES_DIR
            files = [f for f in os.listdir(TOKEN_PROFILES_DIR) if f.endswith('.json')]
            moved_count = 0
            concentration_removed = 0

            for filename in files:
                token_address = filename.replace('.json', '')
                src_path = os.path.join(TOKEN_PROFILES_DIR, filename)
                dest_path = os.path.join(ARCHIVE_DIR, filename)

                # Check if token is inactive or has high holder concentration
                should_archive = token_address not in active_tokens
                if not should_archive:
                    should_archive = await check_token_profile(src_path)
                    if should_archive:
                        concentration_removed += 1
                
                if should_archive:
                    shutil.move(src_path, dest_path)
                    moved_count += 1
                    logger.info(f"Moved profile {filename} to {ARCHIVE_DIR}")

            logger.info(f"Cleanup complete: Moved {moved_count} profiles ({concentration_removed} due to high holder concentration)")
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Error cleaning token profiles: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

async def main():
    logger.info("Starting token profile cleanup script...")
    await clean_token_profiles()

if __name__ == "__main__":
    asyncio.run(main())