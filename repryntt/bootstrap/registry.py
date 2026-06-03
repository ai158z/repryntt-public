#!/usr/bin/env python3
"""repryntt bootstrap registry.

Lightweight peer-discovery phonebook for the blockchain P2P network.
It does not run consensus, hold wallets, store chain state, or accept
transactions.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOG = logging.getLogger("repryntt.bootstrap.registry")

EXPECTED_GENESIS_HASH = (
    "84adf3566b7ede5500dbc0cd11f5096a2e12230b23b6833a0118330c04b5270f"
    "17dab17e1e6fb8b41f725ac0ba895af23e9658af63d79a1cc76c2413bf13c1ef"
)
DEFAULT_BIND = "0.0.0.0:6600"
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_PEERS = 10_000
DEFAULT_MIN_ANNOUNCE_INTERVAL = 10
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{6,128}$")


def _state_path() -> Path:
    return Path(
        os.environ.get(
            "REPRYNTT_BOOTSTRAP_STATE",
            str(Path.home() / ".repryntt" / "bootstrap" / "peers.json"),
        )
    )


def _expected_genesis() -> str:
    return os.environ.get("REPRYNTT_EXPECTED_GENESIS_HASH", EXPECTED_GENESIS_HASH).strip()


def _allow_private() -> bool:
    return os.environ.get("REPRYNTT_BOOTSTRAP_ALLOW_PRIVATE", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _now() -> float:
    return time.time()


def _parse_bind(raw: str) -> tuple[str, int]:
    if ":" not in raw:
        return raw, 6600
    host, port_s = raw.rsplit(":", 1)
    return host or "0.0.0.0", int(port_s)


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or handler.client_address[0]


def _is_public_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def _host_from_address(address: str) -> str:
    parsed = urlparse(address)
    if parsed.scheme:
        return parsed.hostname or ""
    return address.split("/", 1)[0].rsplit(":", 1)[0]


def _normalize_address(raw: str | None, remote_ip: str, p2p_port: int) -> str:
    if raw:
        address = raw.strip()
        if "://" not in address:
            address = f"tcp://{address}"
    else:
        address = f"tcp://{remote_ip}:{p2p_port}"

    parsed = urlparse(address)
    scheme = parsed.scheme or "tcp"
    if scheme not in {"tcp", "ws", "wss", "http", "https"}:
        raise ValueError("unsupported address scheme")

    host = parsed.hostname
    port = parsed.port or p2p_port
    if not host:
        raise ValueError("missing address host")
    if port <= 0 or port > 65535:
        raise ValueError("invalid p2p port")
    if not _allow_private() and not _is_public_ip(host):
        raise ValueError("private peer addresses are not accepted by this registry")

    return f"tcp://{host}:{port}"


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.peers: dict[str, dict[str, Any]] = {}
        self.last_announce_by_ip: dict[str, float] = {}
        self.ttl_seconds = int(os.environ.get("REPRYNTT_BOOTSTRAP_TTL_SECONDS", DEFAULT_TTL_SECONDS))
        self.max_peers = int(os.environ.get("REPRYNTT_BOOTSTRAP_MAX_PEERS", DEFAULT_MAX_PEERS))
        self.min_announce_interval = int(
            os.environ.get(
                "REPRYNTT_BOOTSTRAP_MIN_ANNOUNCE_INTERVAL",
                DEFAULT_MIN_ANNOUNCE_INTERVAL,
            )
        )
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                self.peers = {
                    str(k): v
                    for k, v in data.get("peers", {}).items()
                    if isinstance(v, dict)
                }
        except Exception as exc:
            LOG.warning("Could not load bootstrap registry state: %s", exc)
            self.peers = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"peers": self.peers, "updated_at": _now()}
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name,
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, sort_keys=True)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def prune_locked(self) -> None:
        cutoff = _now() - self.ttl_seconds
        self.peers = {
            node_id: info
            for node_id, info in self.peers.items()
            if float(info.get("last_seen", 0)) >= cutoff
        }

    def announce(self, data: dict[str, Any], remote_ip: str) -> dict[str, Any]:
        node_id = str(data.get("node_id", "")).strip()
        if not NODE_ID_RE.match(node_id):
            raise ValueError("invalid node_id")

        genesis_hash = str(data.get("genesis_hash", "")).strip()
        if genesis_hash != _expected_genesis():
            raise ValueError("genesis_hash mismatch")

        p2p_port = int(data.get("p2p_port", 5001))
        address = _normalize_address(data.get("address"), remote_ip, p2p_port)
        host = _host_from_address(address)
        if not _allow_private() and not _is_public_ip(remote_ip) and not _is_public_ip(host):
            raise ValueError("private source addresses are not accepted by this registry")

        with self.lock:
            now = _now()
            last = self.last_announce_by_ip.get(remote_ip, 0)
            if now - last < self.min_announce_interval:
                raise RuntimeError("announce rate limited")
            self.last_announce_by_ip[remote_ip] = now

            self.prune_locked()
            if len(self.peers) >= self.max_peers and node_id not in self.peers:
                raise RuntimeError("registry full")

            self.peers[node_id] = {
                "node_id": node_id,
                "address": address,
                "chain_height": int(data.get("chain_height", 0)),
                "genesis_hash": genesis_hash,
                "version": str(data.get("version", "")),
                "last_seen": now,
                "source_ip": remote_ip,
            }
            self.save()
            return {"status": "ok", "peers": len(self.peers)}

    def list_peers(self) -> list[dict[str, Any]]:
        with self.lock:
            self.prune_locked()
            self.save()
            return sorted(
                self.peers.values(),
                key=lambda item: float(item.get("last_seen", 0)),
                reverse=True,
            )


class Handler(BaseHTTPRequestHandler):
    registry: Registry

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            raise ValueError("invalid request body length")
        body = self.rfile.read(length)
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        return data

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "repryntt-bootstrap",
                    "peers": len(self.registry.list_peers()),
                },
            )
            return
        if self.path == "/rendezvous/peers":
            self._json(HTTPStatus.OK, {"peers": self.registry.list_peers()})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/rendezvous/announce":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.registry.announce(self._read_json(), _client_ip(self))
            self._json(HTTPStatus.OK, result)
        except RuntimeError as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("REPRYNTT_BOOTSTRAP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bind = os.environ.get("REPRYNTT_BOOTSTRAP_BIND", DEFAULT_BIND)
    host, port = _parse_bind(bind)
    Handler.registry = Registry(_state_path())
    server = ThreadingHTTPServer((host, port), Handler)
    LOG.info("repryntt bootstrap registry listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
