#!/usr/bin/env python3
"""
SAIGE Commerce Integration — Multi-platform e-commerce for AI entrepreneurship.

Supports four platforms (each activates when API keys are configured):

    1. **Shopify** — Full storefront (digital + physical products)
       Config: ``~/.repryntt/commerce/shopify.json``
       ``{"store_url": "your-store.myshopify.com", "access_token": "shpat_xxx"}``

    2. **eBay** — Marketplace (physical + digital)
       Config: ``~/.repryntt/commerce/ebay.json``
       ``{"client_id": "xxx", "client_secret": "xxx", "refresh_token": "xxx",
         "environment": "sandbox|production"}``

    4. **Etsy** — Handmade / unique items marketplace
       Config: ``~/.repryntt/commerce/etsy.json``
       ``{"api_key": "xxx", "shop_id": "xxx", "access_token": "xxx"}``

    5. **LemonSqueezy** — Digital products
       Config: ``~/.repryntt/commerce/lemonsqueezy.json``
       ``{"api_key": "xxx", "store_id": "xxx"}``

Architecture:
    Each platform has a class with standard methods (list_products, create_product,
    etc.). The unified COMMERCE_TOOLS dict exposes platform-agnostic functions
    that auto-detect which platforms are available and route accordingly.

Two operation modes:
    - DIGITAL (autonomous): Andrew creates the product file, writes the listing,
      sets the price, and publishes — no human needed.
    - PHYSICAL (collaborative): Andrew creates the listing, human handles
      fulfillment. Orders requiring shipment are flagged for operator attention.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger("brain.commerce")

# ──────────────────────────────────────────────────
# Config paths
# ──────────────────────────────────────────────────

from repryntt.paths import get_data_dir as _get_data_dir, operator_dir as _operator_dir

COMMERCE_DIR = str(_get_data_dir() / "commerce")
PRODUCTS_DIR = str(_operator_dir() / "commerce")


def _load_config(platform: str) -> Optional[Dict]:
    """Load platform config from ~/.repryntt/commerce/<platform>.json."""
    path = os.path.join(COMMERCE_DIR, f"{platform}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            cfg = json.load(f)
        return cfg if cfg else None
    except Exception as e:
        logger.warning(f"Commerce config error for {platform}: {e}")
        return None


def _get_available_platforms() -> Dict[str, Dict]:
    """Return dict of platform_name -> config for all configured platforms."""
    platforms = {}
    for name in ("shopify", "ebay", "etsy", "lemonsqueezy"):
        cfg = _load_config(name)
        if cfg:
            platforms[name] = cfg
    return platforms


def _http_request(method: str, url: str, headers: Dict = None,
                  json_data: Dict = None, timeout: int = 30) -> Dict:
    """Simple HTTP request wrapper using urllib (no external deps)."""
    import urllib.request
    import urllib.error

    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    data = None
    if json_data is not None:
        data = json.dumps(json_data).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return {"ok": True, "status": resp.status, "data": json.loads(body) if body else {}}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": e.code, "error": body[:500]}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)[:500]}


# ══════════════════════════════════════════════════
# SHOPIFY ADAPTER
# ══════════════════════════════════════════════════

class ShopifyAdapter:
    """Shopify Admin API — full store management."""

    API_VERSION = "2024-10"

    def __init__(self, cfg: Dict):
        self.store_url = cfg["store_url"].rstrip("/")
        self.token = cfg["access_token"]
        self.base = f"https://{self.store_url}/admin/api/{self.API_VERSION}"

    def _headers(self) -> Dict:
        return {
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
        }

    def list_products(self, limit: int = 10) -> Dict:
        url = f"{self.base}/products.json?limit={limit}"
        return _http_request("GET", url, headers=self._headers())

    def create_product(self, title: str, description: str, price: str,
                       product_type: str = "digital", images: List[str] = None,
                       tags: str = "", **kwargs) -> Dict:
        payload = {
            "product": {
                "title": title,
                "body_html": description,
                "product_type": product_type,
                "tags": tags,
                "variants": [{"price": price, "requires_shipping": product_type != "digital"}],
            }
        }
        if images:
            payload["product"]["images"] = [{"src": url} for url in images[:5]]
        url = f"{self.base}/products.json"
        return _http_request("POST", url, headers=self._headers(), json_data=payload)

    def update_product(self, product_id: str, updates: Dict) -> Dict:
        url = f"{self.base}/products/{product_id}.json"
        return _http_request("PUT", url, headers=self._headers(),
                             json_data={"product": updates})

    def get_orders(self, status: str = "any", limit: int = 10) -> Dict:
        url = f"{self.base}/orders.json?status={status}&limit={limit}"
        return _http_request("GET", url, headers=self._headers())

    def get_product(self, product_id: str) -> Dict:
        url = f"{self.base}/products/{product_id}.json"
        return _http_request("GET", url, headers=self._headers())


# ══════════════════════════════════════════════════
# EBAY ADAPTER
# ══════════════════════════════════════════════════

class EbayAdapter:
    """eBay REST API — marketplace listings."""

    SANDBOX_BASE = "https://api.sandbox.ebay.com"
    PROD_BASE = "https://api.ebay.com"

    def __init__(self, cfg: Dict):
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.refresh_token = cfg["refresh_token"]
        self.environment = cfg.get("environment", "sandbox")
        self.base = self.PROD_BASE if self.environment == "production" else self.SANDBOX_BASE
        self._access_token = None
        self._token_expires = 0

    def _get_access_token(self) -> str:
        """Exchange refresh token for access token (OAuth2)."""
        if self._access_token and time.time() < self._token_expires:
            return self._access_token

        import base64
        import urllib.request
        import urllib.parse

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        url = f"{self.base}/identity/v1/oauth2/token"
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }).encode()

        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        })

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            self._access_token = result["access_token"]
            self._token_expires = time.time() + result.get("expires_in", 7200) - 60
            return self._access_token

    def _headers(self) -> Dict:
        token = self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def list_products(self, limit: int = 10) -> Dict:
        url = f"{self.base}/sell/inventory/v1/inventory_item?limit={limit}"
        return _http_request("GET", url, headers=self._headers())

    def create_product(self, title: str, description: str, price: str,
                       product_type: str = "physical", **kwargs) -> Dict:
        # eBay uses a two-step process: create inventory item, then create offer
        sku = kwargs.get("sku", f"SAIGE-{int(time.time())}")
        category_id = kwargs.get("category_id", "175673")  # Default: digital goods

        # Step 1: Create inventory item
        inv_url = f"{self.base}/sell/inventory/v1/inventory_item/{sku}"
        inv_payload = {
            "product": {
                "title": title,
                "description": description,
            },
            "condition": "NEW",
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": int(kwargs.get("quantity", 999 if product_type == "digital" else 1))
                }
            },
        }
        result = _http_request("PUT", inv_url, headers=self._headers(), json_data=inv_payload)
        if not result.get("ok") and result.get("status") not in (200, 201, 204):
            return result

        # Step 2: Create offer
        offer_url = f"{self.base}/sell/inventory/v1/offer"
        offer_payload = {
            "sku": sku,
            "marketplaceId": "EBAY_US",
            "format": "FIXED_PRICE",
            "pricingSummary": {
                "price": {"value": price, "currency": "USD"}
            },
            "categoryId": category_id,
            "listingDescription": description,
        }
        return _http_request("POST", offer_url, headers=self._headers(), json_data=offer_payload)

    def get_orders(self, limit: int = 10) -> Dict:
        url = f"{self.base}/sell/fulfillment/v1/order?limit={limit}"
        return _http_request("GET", url, headers=self._headers())


# ══════════════════════════════════════════════════
# ETSY ADAPTER
# ══════════════════════════════════════════════════

class EtsyAdapter:
    """Etsy Open API v3 — handmade/unique marketplace."""

    BASE = "https://api.etsy.com/v3/application"

    def __init__(self, cfg: Dict):
        self.api_key = cfg["api_key"]
        self.shop_id = cfg["shop_id"]
        self.access_token = cfg.get("access_token", "")

    def _headers(self) -> Dict:
        h = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def list_products(self, limit: int = 10) -> Dict:
        url = f"{self.BASE}/shops/{self.shop_id}/listings/active?limit={limit}"
        return _http_request("GET", url, headers=self._headers())

    def create_product(self, title: str, description: str, price: str,
                       product_type: str = "physical", **kwargs) -> Dict:
        url = f"{self.BASE}/shops/{self.shop_id}/listings"
        try:
            price_float = float(price)
        except (ValueError, TypeError):
            price_float = 0.0

        payload = {
            "title": title,
            "description": description,
            "price": price_float,
            "quantity": int(kwargs.get("quantity", 999 if product_type == "digital" else 1)),
            "who_made": "i_did",
            "when_made": "made_to_order",
            "taxonomy_id": int(kwargs.get("taxonomy_id", 69)),  # Default: art & collectibles
            "type": "download" if product_type == "digital" else "physical",
            "is_digital": product_type == "digital",
        }
        return _http_request("POST", url, headers=self._headers(), json_data=payload)

    def get_orders(self, limit: int = 10) -> Dict:
        url = f"{self.BASE}/shops/{self.shop_id}/receipts?limit={limit}"
        return _http_request("GET", url, headers=self._headers())


# ══════════════════════════════════════════════════
# LEMONSQUEEZY ADAPTER
# ══════════════════════════════════════════════════

class LemonSqueezyAdapter:
    """LemonSqueezy API — modern digital product platform."""

    BASE = "https://api.lemonsqueezy.com/v1"

    def __init__(self, cfg: Dict):
        self.api_key = cfg["api_key"]
        self.store_id = cfg.get("store_id", "")

    def _headers(self) -> Dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
        }

    def list_products(self, limit: int = 10) -> Dict:
        url = f"{self.BASE}/products"
        if self.store_id:
            url += f"?filter[store_id]={self.store_id}"
        return _http_request("GET", url, headers=self._headers())

    def create_product(self, title: str, description: str, price: str,
                       product_type: str = "digital", **kwargs) -> Dict:
        try:
            price_cents = int(float(price) * 100)
        except (ValueError, TypeError):
            price_cents = 0

        payload = {
            "data": {
                "type": "products",
                "attributes": {
                    "name": title,
                    "description": description,
                    "price": price_cents,
                    "status": "draft",
                },
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": self.store_id}
                    }
                },
            }
        }
        url = f"{self.BASE}/products"
        return _http_request("POST", url, headers=self._headers(), json_data=payload)

    def get_orders(self, limit: int = 10) -> Dict:
        url = f"{self.BASE}/orders"
        if self.store_id:
            url += f"?filter[store_id]={self.store_id}"
        return _http_request("GET", url, headers=self._headers())


# ══════════════════════════════════════════════════
# ADAPTER REGISTRY
# ══════════════════════════════════════════════════

ADAPTER_CLASSES = {
    "shopify": ShopifyAdapter,
    "ebay": EbayAdapter,
    "etsy": EtsyAdapter,
    "lemonsqueezy": LemonSqueezyAdapter,
}


def _get_adapter(platform: str):
    """Get an initialized adapter for the given platform, or None."""
    cfg = _load_config(platform)
    if not cfg:
        return None
    cls = ADAPTER_CLASSES.get(platform)
    if not cls:
        return None
    try:
        return cls(cfg)
    except Exception as e:
        logger.warning(f"Failed to init {platform} adapter: {e}")
        return None


# ══════════════════════════════════════════════════
# UNIFIED TOOL FUNCTIONS (called by agents)
# ══════════════════════════════════════════════════

def commerce_status() -> str:
    """Check which commerce platforms are configured and available."""
    platforms = _get_available_platforms()
    if not platforms:
        return json.dumps({
            "configured_platforms": [],
            "message": (
                "No commerce platforms configured yet. "
                "To set up a platform, create a config file in ~/.repryntt/commerce/. "
                "Supported: shopify.json, ebay.json, etsy.json, lemonsqueezy.json. "
                "See TRADING.md 'Commerce & Entrepreneurship' section for setup details."
            ),
        })

    status = []
    for name, cfg in platforms.items():
        adapter = _get_adapter(name)
        info = {
            "platform": name,
            "configured": True,
            "ready": adapter is not None,
        }
        # Add platform-specific details
        if name == "shopify":
            info["store_url"] = cfg.get("store_url", "?")
        elif name == "ebay":
            info["environment"] = cfg.get("environment", "sandbox")
        elif name == "etsy":
            info["shop_id"] = cfg.get("shop_id", "?")
        elif name == "lemonsqueezy":
            info["store_id"] = cfg.get("store_id", "?")
        status.append(info)

    return json.dumps({
        "configured_platforms": [s["platform"] for s in status],
        "platforms": status,
        "digital_products_dir": PRODUCTS_DIR,
    })


def commerce_list_products(platform: str = "", limit: int = 10) -> str:
    """List products on a specific platform, or all configured platforms."""
    results = {}

    if platform:
        adapter = _get_adapter(platform)
        if not adapter:
            return json.dumps({"error": f"Platform '{platform}' is not configured. Run commerce_status to see available platforms."})
        result = adapter.list_products(limit=limit)
        results[platform] = result
    else:
        # Query all configured platforms
        platforms = _get_available_platforms()
        if not platforms:
            return json.dumps({"error": "No commerce platforms configured."})
        for name in platforms:
            adapter = _get_adapter(name)
            if adapter:
                results[name] = adapter.list_products(limit=limit)

    return json.dumps(results, default=str)


def commerce_create_product(platform: str, title: str, description: str,
                            price: str, product_type: str = "digital",
                            **kwargs) -> str:
    """Create a product listing on the specified platform.

    Args:
        platform: shopify, ebay, etsy, or lemonsqueezy
        title: Product name
        description: Product description (HTML for Shopify, plain text for others)
        price: Price in USD (e.g. "9.99")
        product_type: "digital" or "physical"
        **kwargs: Platform-specific options (images, tags, category_id, etc.)
    """
    adapter = _get_adapter(platform)
    if not adapter:
        return json.dumps({
            "error": f"Platform '{platform}' is not configured.",
            "hint": f"Create ~/.repryntt/commerce/{platform}.json with your API credentials.",
        })

    result = adapter.create_product(
        title=title,
        description=description,
        price=price,
        product_type=product_type,
        **kwargs,
    )

    # Log the creation for tracking
    _log_commerce_action("create_product", platform, {
        "title": title, "price": price, "type": product_type,
        "result_ok": result.get("ok", False),
    })

    return json.dumps(result, default=str)


def commerce_check_orders(platform: str = "", limit: int = 10) -> str:
    """Check recent orders on a platform, or all configured platforms."""
    results = {}

    if platform:
        adapter = _get_adapter(platform)
        if not adapter:
            return json.dumps({"error": f"Platform '{platform}' is not configured."})
        if not hasattr(adapter, "get_orders"):
            return json.dumps({"error": f"Platform '{platform}' does not support order checking."})
        results[platform] = adapter.get_orders(limit=limit)
    else:
        platforms = _get_available_platforms()
        if not platforms:
            return json.dumps({"error": "No commerce platforms configured."})
        for name in platforms:
            adapter = _get_adapter(name)
            if adapter and hasattr(adapter, "get_orders"):
                results[name] = adapter.get_orders(limit=limit)

    # Flag physical orders needing human fulfillment
    return json.dumps(results, default=str)


def commerce_save_digital_product(filename: str, content: str,
                                  product_type: str = "text") -> str:
    """Save a digital product file that can be listed for sale.

    This creates the actual deliverable file in the commerce products directory.
    After saving, use commerce_create_product to list it on a platform.

    Args:
        filename: Name for the file (e.g. "trading_signals_weekly.pdf")
        content: The product content (text, markdown, CSV, etc.)
        product_type: "text", "markdown", "csv", "json", or "code"
    """
    os.makedirs(PRODUCTS_DIR, exist_ok=True)

    # Sanitize filename
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        return json.dumps({"error": "Invalid filename"})

    filepath = os.path.join(PRODUCTS_DIR, safe_name)

    try:
        with open(filepath, 'w') as f:
            f.write(content)

        _log_commerce_action("save_product", "local", {
            "filename": safe_name,
            "size": len(content),
            "type": product_type,
        })

        return json.dumps({
            "success": True,
            "file": filepath,
            "size": len(content),
            "message": (
                f"Product file saved: {safe_name} ({len(content)} chars). "
                f"Next: use commerce_create_product to list it on a platform."
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def commerce_list_saved_products() -> str:
    """List digital product files saved locally, ready to be listed for sale."""
    if not os.path.exists(PRODUCTS_DIR):
        return json.dumps({"products": [], "count": 0, "directory": PRODUCTS_DIR})

    products = []
    for f in sorted(os.listdir(PRODUCTS_DIR)):
        if f.startswith("."):
            continue
        fpath = os.path.join(PRODUCTS_DIR, f)
        if os.path.isfile(fpath):
            products.append({
                "filename": f,
                "size": os.path.getsize(fpath),
                "modified": os.path.getmtime(fpath),
            })

    return json.dumps({
        "products": products,
        "count": len(products),
        "directory": PRODUCTS_DIR,
    })


# ──────────────────────────────────────────────────
# Commerce activity log
# ──────────────────────────────────────────────────

def _log_commerce_action(action: str, platform: str, details: Dict):
    """Append to commerce activity log for tracking."""
    log_path = os.path.join(PRODUCTS_DIR, ".commerce_log.json")
    os.makedirs(PRODUCTS_DIR, exist_ok=True)

    entry = {
        "ts": time.time(),
        "action": action,
        "platform": platform,
        "details": details,
    }

    try:
        log = []
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                log = json.load(f)
        log.append(entry)
        # Keep last 500 entries
        log = log[-500:]
        with open(log_path, 'w') as f:
            json.dump(log, f, indent=1)
    except Exception as e:
        logger.debug(f"Commerce log write failed: {e}")


# ══════════════════════════════════════════════════
# TOOL REGISTRY (imported by brain system)
# ══════════════════════════════════════════════════

COMMERCE_TOOLS = {
    "commerce_status": commerce_status,
    "commerce_list_products": commerce_list_products,
    "commerce_create_product": commerce_create_product,
    "commerce_check_orders": commerce_check_orders,
    "commerce_save_digital_product": commerce_save_digital_product,
    "commerce_list_saved_products": commerce_list_saved_products,
}
