"""
REPRYNTT PumpFun Token Launcher — Launch Tokens on pump.fun
===========================================================

Uses PumpPortal's official API (pump.fun's first-party endpoint) for
creating and launching new tokens on the Pump.fun bonding curve.

Two execution modes:
  1. Lightning (server-signed): Send metadata + keypair → PumpPortal signs and
     submits. Simpler but requires trusting PumpPortal with the mint keypair.
  2. Local (client-signed): Get unsigned tx → we sign locally → submit ourselves.
     Full control over private keys (recommended).

DRY_RUN = True  → Prepares everything but does NOT submit. Logs what would happen.
DRY_RUN = False → Signs and submits real token creation transaction.

Cost: ~0.02 SOL for creation + whatever you set as the dev buy.
      PumpFun takes a standard trading fee on the initial buy.

Safety Notes:
  - This uses pump.fun's OFFICIAL API at pumpportal.fun
  - No third-party SDK needed — just HTTP requests to their endpoint
  - We sign locally (mode 2) so private keys never leave this machine
  - DRY_RUN is True by default — won't create tokens until you flip it
"""

import json
import logging
import base58
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

logger = logging.getLogger("repryntt.pumpfun_launcher")

# ─── Configuration ────────────────────────────────────────────────────────────

DRY_RUN = True  # SAFETY: Must be False to actually launch tokens

PUMPFUN_IPFS_URL = "https://pump.fun/api/ipfs"
PUMPPORTAL_TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"

LAUNCH_LOG_DIR = Path.home() / ".repryntt" / "wallet" / "launch_logs"
LAUNCH_LOG_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TokenLaunchConfig:
    """Configuration for launching a new PumpFun token."""
    name: str
    symbol: str
    description: str
    image_path: str                  # Path to token image (PNG/JPG)
    initial_buy_sol: float = 0.1     # Dev buy amount in SOL
    slippage: int = 10               # Slippage tolerance %
    priority_fee: float = 0.0005     # Priority fee in SOL
    twitter: str = ""
    telegram: str = ""
    website: str = ""


@dataclass
class LaunchResult:
    """Result of a token launch attempt."""
    success: bool = False
    dry_run: bool = True
    token_address: Optional[str] = None
    mint_pubkey: Optional[str] = None
    tx_signature: Optional[str] = None
    metadata_uri: Optional[str] = None
    error: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "dry_run": self.dry_run,
            "token_address": self.token_address,
            "mint_pubkey": self.mint_pubkey,
            "tx_signature": self.tx_signature,
            "metadata_uri": self.metadata_uri,
            "error": self.error,
            "config": self.config,
            "timestamp": self.timestamp,
        }


async def upload_metadata(
    session: aiohttp.ClientSession,
    config: TokenLaunchConfig
) -> Optional[str]:
    """Upload token metadata + image to pump.fun's IPFS.

    Returns the metadata URI or None on failure.
    """
    form_data = aiohttp.FormData()
    form_data.add_field("name", config.name)
    form_data.add_field("symbol", config.symbol)
    form_data.add_field("description", config.description)
    if config.twitter:
        form_data.add_field("twitter", config.twitter)
    if config.telegram:
        form_data.add_field("telegram", config.telegram)
    if config.website:
        form_data.add_field("website", config.website)
    form_data.add_field("showName", "true")

    image_path = Path(config.image_path)
    if not image_path.exists():
        logger.error(f"Image not found: {image_path}")
        return None

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    content_type = "image/png"
    if image_path.suffix.lower() in (".jpg", ".jpeg"):
        content_type = "image/jpeg"
    elif image_path.suffix.lower() == ".gif":
        content_type = "image/gif"
    elif image_path.suffix.lower() == ".webp":
        content_type = "image/webp"

    form_data.add_field(
        "file", image_bytes,
        filename=image_path.name,
        content_type=content_type
    )

    try:
        async with session.post(PUMPFUN_IPFS_URL, data=form_data) as resp:
            if resp.status == 200:
                data = await resp.json()
                uri = data.get("metadataUri")
                logger.info(f"Metadata uploaded to IPFS: {uri}")
                return uri
            else:
                body = await resp.text()
                logger.error(f"IPFS upload failed HTTP {resp.status}: {body}")
    except Exception as e:
        logger.error(f"IPFS upload error: {e}")

    return None


async def launch_token(
    config: TokenLaunchConfig,
    wallet_keypair_path: Optional[str] = None
) -> LaunchResult:
    """Launch a new token on pump.fun using local signing.

    Args:
        config: Token configuration (name, symbol, image, etc.)
        wallet_keypair_path: Path to Solana keypair JSON. Defaults to Artemis wallet.

    Returns:
        LaunchResult with success status, token address, and transaction signature.
    """
    result = LaunchResult(
        dry_run=DRY_RUN,
        timestamp=datetime.now(timezone.utc).isoformat(),
        config={
            "name": config.name,
            "symbol": config.symbol,
            "initial_buy_sol": config.initial_buy_sol,
        }
    )

    # Load wallet
    if wallet_keypair_path is None:
        wallet_keypair_path = str(
            Path.home() / ".repryntt" / "wallet" / "artemis_mainnet.json"
        )

    try:
        with open(wallet_keypair_path) as f:
            secret_bytes = json.load(f)
        signer = Keypair.from_bytes(bytes(secret_bytes))
    except Exception as e:
        result.error = f"Failed to load wallet: {e}"
        _log_launch(result)
        return result

    # Generate mint keypair for the new token
    mint_keypair = Keypair()
    result.mint_pubkey = str(mint_keypair.pubkey())
    result.token_address = str(mint_keypair.pubkey())

    logger.info(
        f"{'[DRY_RUN] ' if DRY_RUN else ''}"
        f"Launching token: {config.name} ({config.symbol}) "
        f"mint={result.mint_pubkey[:12]}... "
        f"dev_buy={config.initial_buy_sol} SOL"
    )

    async with aiohttp.ClientSession() as session:
        # 1. Upload metadata to IPFS
        metadata_uri = await upload_metadata(session, config)
        if not metadata_uri:
            result.error = "Failed to upload metadata to IPFS"
            _log_launch(result)
            return result
        result.metadata_uri = metadata_uri

        token_metadata = {
            "name": config.name,
            "symbol": config.symbol,
            "uri": metadata_uri,
        }

        # 2. DRY_RUN: stop here
        if DRY_RUN:
            result.success = True
            result.error = None
            logger.info(
                f"[DRY_RUN] Token launch prepared — NOT submitted\n"
                f"  Name: {config.name}\n"
                f"  Symbol: {config.symbol}\n"
                f"  Mint: {result.mint_pubkey}\n"
                f"  Metadata: {metadata_uri}\n"
                f"  Dev buy: {config.initial_buy_sol} SOL"
            )
            _log_launch(result)
            return result

        # 3. LIVE: Build transaction via PumpPortal local signing
        payload = {
            "publicKey": str(signer.pubkey()),
            "action": "create",
            "tokenMetadata": token_metadata,
            "mint": str(mint_keypair.pubkey()),
            "denominatedInSol": "true",
            "amount": config.initial_buy_sol,
            "slippage": config.slippage,
            "priorityFee": config.priority_fee,
            "pool": "pump",
        }

        try:
            async with session.post(
                PUMPPORTAL_TRADE_LOCAL_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    result.error = f"PumpPortal API error HTTP {resp.status}: {body}"
                    _log_launch(result)
                    return result

                tx_bytes = await resp.read()

            # 4. Sign the transaction locally
            unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(
                unsigned_tx.message, [mint_keypair, signer]
            )

            # 5. Submit to Solana
            signed_bytes = bytes(signed_tx)
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    base58.b58encode(signed_bytes).decode(),
                    {
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed"
                    }
                ]
            }

            from repryntt.trading.solana_executor import RPC_ENDPOINT
            async with session.post(RPC_ENDPOINT, json=rpc_payload) as resp:
                data = await resp.json()
                if "result" in data:
                    result.success = True
                    result.tx_signature = data["result"]
                    logger.info(
                        f"Token launched! {config.name} ({config.symbol})\n"
                        f"  Address: {result.token_address}\n"
                        f"  TX: https://solscan.io/tx/{result.tx_signature}\n"
                        f"  PumpFun: https://pump.fun/{result.token_address}"
                    )
                else:
                    error = data.get("error", {})
                    result.error = f"RPC error: {error.get('message', str(error))}"
                    logger.error(f"Token launch submission failed: {result.error}")

        except Exception as e:
            result.error = f"Launch failed: {e}"
            logger.error(f"Token launch exception: {e}", exc_info=True)

    _log_launch(result)
    return result


def _log_launch(result: LaunchResult):
    """Save launch result to daily log."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = LAUNCH_LOG_DIR / f"launches_{today}.json"

        launches = []
        if log_file.exists():
            with open(log_file) as f:
                launches = json.load(f)

        launches.append(result.to_dict())

        with open(log_file, "w") as f:
            json.dump(launches, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to log launch: {e}")
