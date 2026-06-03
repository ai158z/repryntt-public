import asyncio
import aiofiles
import json
import logging
import os
from datetime import datetime, timezone
import time
import requests

# Setup logging
logger = logging.getLogger("TrendAgent")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)

# Constants
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_MODULE_DIR, "data")
TOKEN_PROFILES_DIR = os.path.join(_DATA_DIR, "token_profiles")
PREDICTIONS_DIR = os.path.join(_DATA_DIR, "predictions")
TOKEN_PERFORMANCE_FILE = os.path.join(_DATA_DIR, "token_performance.json")

# ── Load .env if present (trading_bot/.env) ──
_dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_dotenv_path):
    with open(_dotenv_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

def _load_ai_config():
    """Load AI provider config from ai_config.json."""
    try:
        config_paths = [
            os.path.join(os.path.expanduser("~"), ".repryntt", "brain", "ai_config.json"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "ai_config.json"),
        ]
        for p in config_paths:
            if os.path.exists(p):
                with open(p) as f:
                    cfg = json.load(f)
                return cfg.get("ai_provider", {})
    except Exception as e:
        logger.warning(f"Failed to load ai_config.json: {e}")
    return {}

_AI_CONFIG = _load_ai_config()
_PROVIDER_NAME = _AI_CONFIG.get("provider", "nvidia")
_PROVIDER = _AI_CONFIG.get(_PROVIDER_NAME, {})

# Trend parameters
MIN_DATA_POINTS = 15
MONITOR_INTERVAL = 5
EXPORT_COOLDOWN = 30
NEW_TOKEN_EXPIRY = 15  # Minutes
UPTREND_THRESHOLD = 5.0  # % increase from previous ATH
UPTREND_WINDOW = 400  # Seconds to check increase

os.makedirs(PREDICTIONS_DIR, exist_ok=True)

async def load_token_performance():
    """Load token performance data from token_performance.json."""
    try:
        if os.path.exists(TOKEN_PERFORMANCE_FILE):
            async with aiofiles.open(TOKEN_PERFORMANCE_FILE, "r", encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        return {}
    except Exception as e:
        logger.error(f"Error loading token performance data: {e}")
        return {}

async def is_token_eligible(token_address):
    """Check if token is eligible based on performance (losses < 2 and no 3 consecutive wins)."""
    performance_data = await load_token_performance()
    token_data = performance_data.get(token_address, {"wins": 0, "losses": 0, "history": []})
    losses = token_data.get("losses", 0)
    history = token_data.get("history", [])
    
    if losses >= 2:
        logger.info(f"Token {token_address} ineligible: {losses} losses (>= 2).")
        return False
    
    # Check for 3 consecutive wins
    consecutive_wins = 0
    for event in reversed(history):
        if event["type"] == "win":
            consecutive_wins += 1
        else:
            break
    if consecutive_wins >= 3:
        logger.info(f"Token {token_address} ineligible: {consecutive_wins} consecutive wins (>= 3).")
        return False
    
    logger.debug(f"Token {token_address} eligible: {losses} losses, {consecutive_wins} consecutive wins.")
    return True

async def analyze_with_llm(prompt):
    """Call the active AI provider (OpenAI-compatible endpoint) for trend analysis."""
    endpoint = _PROVIDER.get("endpoint", "")
    api_key = _PROVIDER.get("api_key", "")
    model = _PROVIDER.get("model", "default")
    if not endpoint or not api_key or "YOUR_" in api_key:
        logger.warning("No valid AI provider configured for trend analysis")
        return None
    try:
        def _call():
            resp = requests.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a crypto trend analyst. Reply with exactly one word: uptrend, downtrend, or crab."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 50,
                    "temperature": 0.5,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.error(f"LLM API error: {e}")
        return None

async def predict_trend(token_address, token_name, price_history, ath_price, ath_timestamp, is_new=False):
    """Predict trend based on lifetime ATH breakout with uptrend price increase."""
    if not price_history or len(price_history) < MIN_DATA_POINTS:
        logger.info(f"Not enough data points ({len(price_history) if price_history else 0}) for {token_address}")
        return "crab"
    
    # Calculate time since ATH
    try:
        ath_ts = float(ath_timestamp) if ath_timestamp else 0
    except (ValueError, TypeError):
        ath_ts = 0
    time_since_ath = time.time() - ath_ts if ath_ts > 0 else 0
    
    # Calculate price increase since ATH
    try:
        current_price = float(price_history[-1][1]) if isinstance(price_history[-1], (list, tuple)) else float(price_history[-1])
        ath_price_f = float(ath_price) if ath_price else 0
        price_increase = ((current_price - ath_price_f) / ath_price_f) * 100 if ath_price_f > 0 else 0
    except (ValueError, TypeError, IndexError):
        logger.warning(f"Bad price data for {token_address}")
        return "crab"
    
    # Check if it's a new token within NEW_TOKEN_EXPIRY minutes
    if is_new and time_since_ath > (NEW_TOKEN_EXPIRY * 60):
        logger.info(f"New token {token_address} expired after {NEW_TOKEN_EXPIRY} minutes.")
        return "crab"
    
    # Check for recent uptrend
    if price_increase >= UPTREND_THRESHOLD and time_since_ath <= UPTREND_WINDOW:
        logger.info(f"ATH breakout with uptrend price increase for {token_address}.")
        trend_prediction = "uptrend"
    else:
        trend_prediction = "downtrend"
    
    # Construct prompt for Gemini
    try:
        ath_time_str = datetime.fromtimestamp(ath_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if ath_ts > 0 else "unknown"
    except (ValueError, OSError):
        ath_time_str = "unknown"
    prompt = f"Given the token {token_name} ({token_address}) has a lifetime ATH of {ath_price} at {ath_time_str}, and the current price is {current_price}. Predict the short-term trend (uptrend, downtrend, or crab)."
    
    llm_prediction = await analyze_with_llm(prompt)
    if llm_prediction:
        logger.info(f"LLM prediction for {token_address}: {llm_prediction}")
    else:
        logger.warning(f"Failed to get LLM prediction for {token_address}, defaulting to {trend_prediction}.")
        return trend_prediction

    if llm_prediction == "uptrend" and trend_prediction == "uptrend":
        final_prediction = "uptrend"
    elif llm_prediction == "downtrend" and trend_prediction == "downtrend":
        final_prediction = "downtrend"
    else:
        final_prediction = "crab"
    
    export_filename = os.path.join(PREDICTIONS_DIR, f"{token_address}.json")
    export_data = {"token_address": token_address, "token_name": token_name, "trend": final_prediction, "llm_prediction": llm_prediction, "trend_prediction": trend_prediction, "ath_price": ath_price, "ath_timestamp": ath_timestamp, "current_price": current_price, "time_since_ath": time_since_ath}
    async with aiofiles.open(export_filename, "w", encoding='utf-8') as f:
        await f.write(json.dumps(export_data, indent=4))
    logger.info(f"Exported prediction for {token_address} to {export_filename}")
    return final_prediction

async def main():
    start_time = time.time()
    logger.info("Starting Trend Agent...")

    # Load token performance data
    token_performance = await load_token_performance()
    
    # Load token profiles (simulated from dexscreener_discovery.py)
    token_profiles = []
    for filename in os.listdir(TOKEN_PROFILES_DIR):
        if filename.endswith(".json"):
            file_path = os.path.join(TOKEN_PROFILES_DIR, filename)
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    token_data = json.loads(content)
                    token_profiles.append(token_data)
            except Exception as e:
                logger.error(f"Error reading token profile {filename}: {e}")
                continue
    logger.info(f"Loaded {len(token_profiles)} token profiles.")
    
    # Process each token profile
    tasks = []
    for token_profile in token_profiles:
        token_address = token_profile.get("address") or token_profile.get("token_address")
        token_name = token_profile.get("token_name") or token_profile.get("base_token_name", "Unknown")
        price_history = token_profile.get("price_history", [])
        ath_price = token_profile.get("ath_price", 0)
        ath_timestamp = token_profile.get("ath_timestamp", "")
        is_new = token_profile.get("is_new", False)

        if not token_address:
            logger.warning(f"Skipping profile with no address: {list(token_profile.keys())[:5]}")
            continue
        
        # Check token eligibility
        if not await is_token_eligible(token_address):
            continue
        
        # Run prediction
        task = asyncio.create_task(predict_trend(token_address, token_name, price_history, ath_price, ath_timestamp, is_new))
        tasks.append(task)
    
    # Wait for all tasks to complete
    if tasks:
        await asyncio.gather(*tasks)
    
    end_time = time.time()
    elapsed_time = end_time - start_time
    logger.info(f"Trend Agent completed in {elapsed_time:.2f} seconds.")

if __name__ == "__main__":
    asyncio.run(main())