"""
url_guard.py — SSRF Protection for URL-fetching tools
══════════════════════════════════════════════════════════
Validates URLs before requests.get() to prevent Server-Side Request Forgery.
Blocks:
  - Private/internal IPs (127.x, 10.x, 172.16-31.x, 192.168.x, fd00::, etc.)
  - Localhost aliases
  - Non-HTTP schemes (file://, ftp://, gopher://, etc.)
  - Unresolvable hostnames

Adapted from NemoClaw's validate_endpoint_url() pattern (Apache-2.0).
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("100.64.0.0/10"),   # Carrier-grade NAT
    ipaddress.ip_network("198.18.0.0/15"),    # Benchmarking
)

_ALLOWED_SCHEMES = ("https", "http")

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",       # Cloud metadata endpoints
    "metadata.internal",
    "instance-data",
}


def _is_private_ip(addr_str: str) -> bool:
    """Check if an IP address belongs to a private/internal network."""
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


def validate_url(url: str) -> str:
    """
    Validate a URL before fetching. Returns the URL unchanged if safe.
    Raises ValueError if the URL targets a private/internal resource.
    """
    if not url or not isinstance(url, str):
        raise ValueError("Empty or invalid URL")

    parsed = urlparse(url)

    # Scheme check
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Blocked URL scheme '{parsed.scheme}://'. "
            f"Only {', '.join(s + '://' for s in _ALLOWED_SCHEMES)} are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"No hostname in URL: {url}")

    # Blocked hostname check
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname: {hostname}")

    # Check if hostname is a raw IP literal
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private_ip(str(addr)):
            raise ValueError(
                f"Blocked: URL resolves to private/internal address {addr}"
            )
        return url  # Public IP literal — OK
    except ValueError:
        pass  # Not an IP literal, proceed to DNS resolution

    # DNS resolution — check ALL resolved addresses
    try:
        addr_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname '{hostname}': {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = str(sockaddr[0])
        if _is_private_ip(ip_str):
            raise ValueError(
                f"Blocked: '{hostname}' resolves to private/internal address {ip_str}"
            )

    return url


def safe_url_or_none(url: str) -> str | None:
    """
    Convenience wrapper: returns validated URL or None (with logging).
    Use this where you want to silently skip bad URLs rather than raise.
    """
    try:
        return validate_url(url)
    except ValueError as e:
        logger.warning(f"🛡️ SSRF blocked: {e}")
        return None
