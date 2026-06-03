"""
Structured JSON logging for SAIGE with correlation IDs.

Sets up JSON-formatted log output with per-request correlation IDs
that trace a request across Flask → daemon → agent calls.

Usage:
    from structured_logging import setup_logging, get_logger, get_correlation_id
    
    setup_logging(app)  # call once during Flask init
    log = get_logger('my_module')
    log.info("processing request", extra={'user': 'admin', 'action': 'invoke'})
    
    # In middleware, correlation ID is auto-injected into every log line.
    # Access it manually:
    cid = get_correlation_id()
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Optional

from flask import Flask, g, request

try:
    from pythonjsonlogger.json import JsonFormatter
except ImportError:
    # Fallback for older versions of python-json-logger
    try:
        from pythonjsonlogger import jsonlogger
        JsonFormatter = jsonlogger.JsonFormatter
    except ImportError:
        JsonFormatter = None  # type: ignore


# ─── Correlation ID ────────────────────────────────────────────────────────

_CORRELATION_HEADER = 'X-Correlation-ID'


def get_correlation_id() -> str:
    """Get the current request's correlation ID, or generate one if outside request context."""
    try:
        return g.correlation_id
    except (AttributeError, RuntimeError):
        return str(uuid.uuid4())[:12]


class CorrelationFilter(logging.Filter):
    """Inject correlation_id into every log record."""
    def filter(self, record):
        record.correlation_id = get_correlation_id()  # type: ignore
        return True


# ─── Formatter ──────────────────────────────────────────────────────────────

_JSON_FORMAT = "%(asctime)s %(name)s %(levelname)s %(correlation_id)s %(message)s"


def _make_json_formatter() -> logging.Formatter:
    """Create a JSON log formatter, or fall back to a structured text formatter."""
    if JsonFormatter is not None:
        return JsonFormatter(
            fmt=_JSON_FORMAT,
            rename_fields={
                "asctime": "timestamp",
                "name": "logger",
                "levelname": "level",
                "correlation_id": "cid",
            },
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    # Fallback: structured text format (if python-json-logger missing)
    return logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s cid=%(correlation_id)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ─── Setup ──────────────────────────────────────────────────────────────────

_SETUP_DONE = False


def setup_logging(app: Optional[Flask] = None, level: str = '') -> None:
    """Configure structured JSON logging for the entire application.
    
    Args:
        app: Flask application — if provided, registers before/after request
             hooks for correlation ID tracking and request logging.
        level: Log level string (DEBUG, INFO, WARNING, etc.).
               Defaults to SAIGE_LOG_LEVEL env var or INFO.
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    _SETUP_DONE = True

    log_level = (level or os.environ.get('SAIGE_LOG_LEVEL', 'INFO')).upper()

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers to avoid duplicate output
    root.handlers.clear()

    # JSON handler → stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(getattr(logging, log_level, logging.INFO))
    handler.setFormatter(_make_json_formatter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)

    # Optional file handler for production
    log_file = os.environ.get('SAIGE_LOG_FILE', '').strip()
    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=5)
        fh.setLevel(getattr(logging, log_level, logging.INFO))
        fh.setFormatter(_make_json_formatter())
        fh.addFilter(CorrelationFilter())
        root.addHandler(fh)

    # Quiet noisy libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    if app:
        _register_flask_hooks(app)


def _register_flask_hooks(app: Flask) -> None:
    """Add before/after request hooks for correlation ID and request logging."""

    @app.before_request
    def _set_correlation_id():
        # Accept from upstream proxy or generate new
        cid = request.headers.get(_CORRELATION_HEADER, '').strip()
        if not cid:
            cid = uuid.uuid4().hex[:12]
        g.correlation_id = cid
        g.request_start_time = __import__('time').time()

    @app.after_request
    def _log_request(response):
        # Inject correlation ID into response headers for client tracing
        cid = getattr(g, 'correlation_id', 'unknown')
        response.headers[_CORRELATION_HEADER] = cid

        # Calculate duration
        start = getattr(g, 'request_start_time', None)
        duration_ms = round(((__import__('time').time() - start) * 1000), 1) if start else 0

        # Log the request (skip noisy static/health)
        path = request.path
        if path.startswith('/static/') or path == '/health':
            return response

        log = logging.getLogger('saige.http')
        log.info(
            "request",
            extra={
                'method': request.method,
                'path': path,
                'status': response.status_code,
                'duration_ms': duration_ms,
                'remote_addr': request.remote_addr,
                'content_length': response.content_length,
            }
        )
        return response


def get_logger(name: str) -> logging.Logger:
    """Get a named logger with correlation ID filter pre-attached.
    
    Use this instead of logging.getLogger() to ensure correlation IDs
    are always present in log output.
    """
    log = logging.getLogger(name)
    # Correlation filter is on the root handler, so it applies automatically
    return log
