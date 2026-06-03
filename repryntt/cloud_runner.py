"""
repryntt.cloud_runner — local-OSS runner that connects to the Repryntt cloud.

Run this on a paid user's machine to register their local Repryntt as the
execution backend for the paid website. It dials OUT to api.repryntt.com over
a WebSocket (no inbound ports), authenticates with the user's API key, keeps
the connection alive with heartbeats, and EXECUTES dispatched jobs on the real
local OSS — using the keys/config already on this machine. Results stream back
to the cloud, which updates the job so the website dashboard shows them.

Usage:
    REPRYNTT_API_KEY=rkey_... python -m repryntt.cloud_runner

Env:
    REPRYNTT_API_KEY     required — the user's rkey_ key from the dashboard
    REPRYNTT_RUNNER_WS   optional — override the control-channel URL
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import websockets

logger = logging.getLogger("repryntt.cloud_runner")

DEFAULT_WS_URL = "wss://api.repryntt.com/v1/runner/connect"
RUNNER_VERSION = "0.2.0"
HEARTBEAT_INTERVAL = 20  # seconds (cloud TTL is 60s)

# Default tunnel prefix for the repryntt.com dashboard's Nexus iframe.
# The dashboard mounts the runner's localhost:8089 at this path, so absolute
# URLs in returned HTML (`href="/agents"`) need to be rewritten to live under
# this prefix or they 404 against the marketing site.
DEFAULT_TUNNEL_PREFIX = "/api/proxy/v1/runner/nexus"

# Match href/src/action attributes whose value is an absolute path starting
# with a single "/" — not "//" (protocol-relative), and not already prefixed.
_HTML_ABS_PATH = re.compile(
    r'''\b(href|src|action|formaction)   # the attribute name
        \s*=\s*
        (["\'])                          # opening quote
        (/(?!/)[^"\'<>\s]*)              # path starting with one "/", not "//"
        (["\'])                          # closing quote''',
    re.IGNORECASE | re.VERBOSE,
)


def _rewrite_html_for_tunnel(html: str, prefix: str) -> str:
    """Rewrite absolute /paths in HTML to live under the tunnel prefix.

    `prefix` is the cloud-side mount point, e.g. ``/api/proxy/v1/runner/nexus``.
    Also injects ``<base href="{prefix}/">`` so relative URLs and JS-built
    fetches resolve under the tunnel. Idempotent — running it twice is a no-op.

    Any malformed HTML or regex failure falls back to the original string so
    the user always gets *something* rather than a broken page.
    """
    if not prefix or not prefix.startswith("/"):
        return html
    prefix = prefix.rstrip("/")

    def _sub(m):
        attr, q1, path, q2 = m.group(1), m.group(2), m.group(3), m.group(4)
        # idempotent — don't double-prefix
        if path == prefix or path.startswith(prefix + "/"):
            return m.group(0)
        return f"{attr}={q1}{prefix}{path}{q2}"

    try:
        rewritten = _HTML_ABS_PATH.sub(_sub, html)
        # Inject <base href> right after the first <head ...> tag if no <base> already.
        if "<base " not in rewritten.lower()[:8000]:
            rewritten = re.sub(
                r"(<head\b[^>]*>)",
                rf'\1<base href="{prefix}/">',
                rewritten,
                count=1,
                flags=re.IGNORECASE,
            )
        return rewritten
    except Exception:
        return html


def _maybe_rewrite_html_response(resp: dict, in_headers: dict) -> None:
    """If `resp` is HTML, rewrite its body to use the tunnel prefix in-place."""
    headers = resp.get("headers") or {}
    ct = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            ct = (v or "").lower()
            break
    if "text/html" not in ct:
        return
    # Resolve the tunnel prefix: header from the cloud > env var > default
    prefix = ""
    for k, v in (in_headers or {}).items():
        if k.lower() == "x-forwarded-prefix":
            prefix = v or ""
            break
    if not prefix:
        prefix = os.environ.get("REPRYNTT_TUNNEL_PREFIX", DEFAULT_TUNNEL_PREFIX)
    if not prefix:
        return
    try:
        import base64 as _b64
        body = _b64.b64decode(resp.get("body") or "")
        html = body.decode("utf-8")
        new_html = _rewrite_html_for_tunnel(html, prefix)
        if new_html is not html:
            resp["body"] = _b64.b64encode(new_html.encode("utf-8")).decode()
            # Content-Length is no longer accurate — drop it; the cloud/browser
            # will use chunked or compute from the body bytes.
            for k in list(headers.keys()):
                if k.lower() == "content-length":
                    del headers[k]
    except Exception:
        # Non-UTF-8 body, decode error, etc — leave the response untouched.
        pass


def _provider_for_model(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3"):
        return "openai"
    if m.startswith("grok"):
        return "xai"
    if m.startswith("gemini"):
        return "google_gemini"
    return ""


def _anthropic_complete(endpoint: str, api_key: str, model: str, system: str,
                        task: str, max_tokens: int, temperature: float):
    """Native Anthropic /v1/messages call with auto-continuation (Anthropic is
    not OpenAI-compatible, so call_llm can't be used for it)."""
    import requests
    ep = endpoint.rstrip("/")
    if not ep.endswith("/messages"):
        ep = ep + "/v1/messages" if not ep.endswith("/v1") else ep + "/messages"
    acc, total_out, usage = "", 0, {"input_tokens": 0, "output_tokens": 0}
    ceiling, budget = 16000, max(16, int(max_tokens))
    for _ in range(8):
        if budget - total_out <= 0:
            break
        msgs = [{"role": "user", "content": task}]
        if acc:
            # End on a USER turn — some models reject assistant prefill, and
            # prefill also forbids trailing whitespace. "Continue" works on all.
            msgs.append({"role": "assistant", "content": acc.rstrip()})
            msgs.append({"role": "user", "content":
                         "Continue exactly where you left off. Do not repeat any text already written."})
        r = requests.post(
            ep,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": min(budget - total_out, ceiling),
                  "temperature": temperature, "system": system, "messages": msgs},
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        chunk = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
        if not chunk:
            break
        acc += chunk
        u = data.get("usage", {})
        usage["input_tokens"] += u.get("input_tokens", 0)
        total_out += u.get("output_tokens", 0)
        usage["output_tokens"] = total_out
        if data.get("stop_reason") != "max_tokens":
            break
    return acc, usage


class RunnerClient:
    def __init__(self, api_key: str, ws_url: str | None = None):
        self.api_key = api_key
        self.ws_url = ws_url or os.environ.get("REPRYNTT_RUNNER_WS", DEFAULT_WS_URL)

    def _capabilities(self) -> list:
        """Features this install can actually execute locally. The cloud only
        routes these to the runner; anything missing falls back to cloud
        execution. (The public OSS ships agents; the full tree adds the rest.)"""
        import importlib.util as _u
        feats = ["agent"]
        if _u.find_spec("repryntt.codeforge.governance"):
            feats.append("forge")
        if _u.find_spec("repryntt.tools.video_production"):
            feats.append("video")
        return feats

    # ── job execution (runs on the real local OSS) ──────────────────────

    def _execute(self, msg: dict) -> dict:
        feature = msg.get("feature")
        if feature == "agent":
            return self._execute_agent(msg)
        if feature == "forge":
            return self._execute_forge(msg)
        if feature == "video":
            return self._execute_video(msg)
        raise RuntimeError(f"runner does not support feature '{feature}' yet")

    def _upload_artifact(self, local_path: str, build_id: str) -> str | None:
        """Send a produced file to the cloud, which stores it (Spaces) and
        returns a download URL for the website. The file ALSO stays local."""
        import os
        import requests
        api_base = os.environ.get("REPRYNTT_API_BASE", "https://api.repryntt.com")
        fname = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            r = requests.post(
                f"{api_base}/v1/runner/artifact",
                params={"build_id": build_id, "filename": fname},
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=f, timeout=600,
            )
        r.raise_for_status()
        return r.json().get("artifact_url")

    def _execute_forge(self, msg: dict) -> dict:
        from pathlib import Path
        import shutil
        import tempfile
        from repryntt.codeforge.governance import (
            propose_project, approve_proposal, start_approved_project,
        )
        from repryntt.codeforge.forge import get_forge

        prop = propose_project(
            description=msg.get("description", ""), proposer="runner",
            provider=msg.get("provider", ""), model=msg.get("model", ""),
        )
        pid = prop.get("id") if isinstance(prop, dict) else None
        if not pid:
            raise RuntimeError(f"proposal creation failed: {prop}")
        approved = approve_proposal(pid)
        if isinstance(approved, dict) and approved.get("error"):
            raise RuntimeError(f"approve failed: {approved['error']}")
        sr = start_approved_project(pid)
        project_id = sr.get("project_id") if isinstance(sr, dict) else None
        if not project_id:
            raise RuntimeError(f"start failed: {sr}")

        # start_project runs the build in a background thread (returns at
        # status=queued). Poll until terminal before packaging.
        import time as _t
        forge = get_forge()
        deadline = _t.time() + 3300
        detail = {}
        while _t.time() < deadline:
            detail = forge.get_project_detail(project_id) or {}
            if detail.get("status") in ("completed", "failed"):
                break
            _t.sleep(5)
        if detail.get("status") != "completed":
            raise RuntimeError(f"forge ended {detail.get('status')}: {detail.get('error_log', [])[-2:]}")

        artifact_url = None
        ws_path = Path(detail.get("workspace_path") or "")
        if ws_path.exists():
            zip_base = Path(tempfile.gettempdir()) / f"{project_id}"
            zip_path = Path(shutil.make_archive(str(zip_base), "zip", str(ws_path)))
            artifact_url = self._upload_artifact(str(zip_path), msg["build_id"])
            try:
                zip_path.unlink()
            except OSError:
                pass
        return {"output": {
            "project_id": project_id,
            "modules_count": len(detail.get("modules", []) or []),
            "api_calls": detail.get("api_calls", 0),
            "status": detail.get("status"),
        }, "artifact_url": artifact_url}

    def _execute_video(self, msg: dict) -> dict:
        import json as _json
        from pathlib import Path
        from repryntt.tools import video_production as vp

        fn_name = msg.get("fn", "")
        fn = getattr(vp, fn_name, None)
        if fn is None:
            raise RuntimeError(f"unknown video function: {fn_name}")
        raw = fn(*(msg.get("args") or []), **(msg.get("kwargs") or {}))

        parsed = raw
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
            except Exception:
                parsed = {}

        artifact_url = None
        if isinstance(parsed, dict):
            try:
                from repryntt.paths import get_data_dir
                data_dir = get_data_dir()
            except Exception:
                data_dir = Path(".")
            for key in ("final_video", "output_path", "final_path", "render_path", "file_path", "path"):
                v = parsed.get(key)
                if v:
                    p = Path(v)
                    p = p if p.is_absolute() else data_dir / p
                    if p.exists():
                        artifact_url = self._upload_artifact(str(p), msg["build_id"])
                        break
        return {"output": {
            "fn_name": fn_name,
            "result": raw if isinstance(raw, (dict, list, str, int, float, bool, type(None))) else str(raw),
        }, "artifact_url": artifact_url}

    def _execute_agent(self, msg: dict) -> dict:
        if msg.get("mode") == "deep":
            return self._execute_agent_deep(msg)
        from repryntt.llm import load_ai_config, resolve_provider, call_llm
        from repryntt.agents.marketplace_prompts import (
            ROLE_INSTRUCTIONS, DEFAULT_ROLE_INSTRUCTION,
        )
        kind = msg.get("kind", "")
        task = msg.get("task", "")
        model = msg.get("model") or ""
        max_tokens = int(msg.get("max_tokens", 32000))
        temperature = float(msg.get("temperature", 0.7))

        system = ROLE_INSTRUCTIONS.get(kind) or DEFAULT_ROLE_INSTRUCTION
        config = load_ai_config()
        provider = _provider_for_model(model) if model else ""
        pinfo = resolve_provider(config, provider, model_override=model)
        ep = (pinfo.get("endpoint") or "")

        started = time.time()
        if pinfo.get("provider") == "anthropic" or ep.rstrip("/").endswith("/messages"):
            text, usage = _anthropic_complete(
                ep, pinfo.get("api_key", ""), pinfo.get("model", ""),
                system, task, max_tokens, temperature,
            )
        else:
            text = call_llm(
                [{"role": "system", "content": system},
                 {"role": "user", "content": task}],
                pinfo, max_tokens=min(max_tokens, 16384), temperature=temperature,
            ) or ""
            usage = {}
        latency_ms = int((time.time() - started) * 1000)
        return {"output": {
            "output": text,
            "model_used": pinfo.get("model", ""),
            "provider": pinfo.get("provider", ""),
            "tokens": usage,
            "latency_ms": latency_ms,
        }}

    def _execute_agent_deep(self, msg: dict) -> dict:
        """Run the task through the FULL local brain: queue it, let the
        heartbeat reasoning loop work it (plan→act→evaluate→reflect, tools,
        memory), then return the brain's summary + any artifact it produced.

        Requires the brain daemon to be running (`repryntt start --no-blockchain`)
        — the runner only submits + watches; the daemon does the reasoning.
        """
        import json as _j
        import os
        import time as _t
        from repryntt.agents.task_queue import TaskQueue
        from repryntt.agents.persistent_agents import _jarvis_workspace

        ws_dir = _jarvis_workspace()
        kind = msg.get("kind", "")
        title = (kind.split("::")[-1] if "::" in kind else kind) or "Task"
        added = TaskQueue(ws_dir).add_task(
            title=title[:80], description=msg.get("task", ""),
            priority="operator", source="operator", bypass_intake=True,
        )
        tid = added.get("id") if isinstance(added, dict) else None
        if not tid or added.get("status") == "rejected":
            raise RuntimeError(f"brain rejected the task: {added.get('reasons') if isinstance(added, dict) else added}")

        queue_path = os.path.join(ws_dir, "task_queue.json")

        def _find():
            try:
                with open(queue_path) as f:
                    for t in (_j.load(f).get("tasks") or []):
                        if t.get("id") == tid:
                            return t
            except Exception:
                pass
            return None

        started = _t.time()
        ever_in_progress = False
        TIMEOUT = 1800  # 30 min hard cap
        while _t.time() - started < TIMEOUT:
            _t.sleep(5)
            t = _find()
            if not t:
                continue
            st = t.get("status")
            if st == "in_progress":
                ever_in_progress = True
            elif st == "completed":
                summary = t.get("summary") or "Completed."
                artifact_url = self._upload_task_artifact(t, ws_dir, msg["build_id"])
                return {"output": {
                    "output": summary, "mode": "deep",
                    "artifact_location": t.get("expected_location", ""),
                    "status": "completed",
                }, "artifact_url": artifact_url}
            elif st in ("failed", "skipped"):
                raise RuntimeError(f"brain task {st}: {t.get('summary') or ''}")
            # If it never even starts, the brain daemon probably isn't running.
            if not ever_in_progress and (_t.time() - started) > 75:
                raise RuntimeError(
                    "Task wasn't picked up — is your Repryntt brain running? "
                    "Start it with: repryntt start --no-blockchain"
                )
        raise RuntimeError("Deep task timed out after 30 minutes.")

    def _upload_task_artifact(self, task: dict, ws_dir: str, build_id: str):
        """If the completed task wrote an artifact to its expected_location,
        upload it (zip dirs) so the website can download it."""
        import os
        import shutil
        import tempfile
        loc = task.get("expected_location") or ""
        if not loc:
            return None
        path = loc if os.path.isabs(loc) else os.path.join(ws_dir, loc)
        if not os.path.exists(path):
            return None
        try:
            if os.path.isdir(path):
                base = os.path.join(tempfile.gettempdir(), f"{build_id}_artifact")
                zip_path = shutil.make_archive(base, "zip", path)
                url = self._upload_artifact(zip_path, build_id)
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                return url
            return self._upload_artifact(path, build_id)
        except Exception:
            return None

    async def _run_job(self, ws, msg: dict) -> None:
        build_id = msg.get("build_id")
        feature = msg.get("feature")
        logger.info("executing job build=%s %s/%s", build_id, feature, msg.get("kind"))
        await ws.send(json.dumps({"type": "job_status", "build_id": build_id, "status": "running"}))
        try:
            result = await asyncio.to_thread(self._execute, msg)
            await ws.send(json.dumps({
                "type": "job_result", "build_id": build_id, "feature": feature,
                "output": result.get("output"), "artifact_url": result.get("artifact_url"),
            }))
            logger.info("job done build=%s", build_id)
        except Exception as e:
            logger.exception("job failed build=%s", build_id)
            await ws.send(json.dumps({
                "type": "job_error", "build_id": build_id,
                "error": f"{type(e).__name__}: {e}",
            }))

    # ── control channel ─────────────────────────────────────────────────

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send(json.dumps({"type": "heartbeat"}))

    async def _handle(self, ws, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype in ("heartbeat_ack", "connected"):
            return
        if mtype == "job":
            # Run jobs concurrently so one long build doesn't block heartbeats.
            asyncio.create_task(self._run_job(ws, msg))
        elif mtype == "http_request":
            # Cloud is tunneling an HTTP request through to the local Nexus
            # dashboard (default :8089). Forward + reply with http_response.
            asyncio.create_task(self._handle_http(ws, msg))

    async def _handle_http(self, ws, msg: dict) -> None:
        import base64 as _b64
        req_id = msg.get("request_id", "")
        method = msg.get("method", "GET")
        path = msg.get("path", "/")
        query = msg.get("query", "")
        in_headers = msg.get("headers") or {}
        body_b64 = msg.get("body", "")
        body = _b64.b64decode(body_b64) if body_b64 else None

        nexus_base = os.environ.get("REPRYNTT_NEXUS_URL", "http://localhost:8089")
        url = f"{nexus_base.rstrip('/')}{path}"
        if query:
            url += "?" + query

        def _do() -> dict:
            import urllib.error
            import urllib.request
            req = urllib.request.Request(url, method=method, data=body)
            for k, v in in_headers.items():
                if k.lower() not in ("host", "content-length"):
                    try:
                        req.add_header(k, v)
                    except Exception:
                        pass
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return {
                        "status": getattr(r, "status", r.getcode()),
                        "headers": {k: v for k, v in r.headers.items()},
                        "body": _b64.b64encode(r.read()).decode(),
                    }
            except urllib.error.HTTPError as e:
                try:
                    data = e.read() if hasattr(e, "read") else b""
                except Exception:
                    data = b""
                headers = {k: v for k, v in (e.headers or {}).items()} if e.headers else {}
                return {"status": e.code, "headers": headers, "body": _b64.b64encode(data).decode()}
            except Exception as e:
                msg_body = f"runner→Nexus error: {type(e).__name__}: {e}".encode()
                return {"status": 502, "headers": {"Content-Type": "text/plain"},
                        "body": _b64.b64encode(msg_body).decode()}

        try:
            resp = await asyncio.to_thread(_do)
        except Exception as e:
            resp = {"status": 502, "headers": {"Content-Type": "text/plain"},
                    "body": _b64.b64encode(f"runner error: {e}".encode()).decode()}

        # Rewrite absolute /paths in HTML responses to live under the tunnel
        # prefix (so links like <a href="/agents"> don't 404 against
        # repryntt.com — they should hit /api/proxy/v1/runner/nexus/agents).
        _maybe_rewrite_html_response(resp, in_headers)

        await ws.send(json.dumps({"type": "http_response", "request_id": req_id, **resp}))

    async def _session(self) -> None:
        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({
                "type": "auth", "api_key": self.api_key, "version": RUNNER_VERSION,
                "features": self._capabilities(),
            }))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if reply.get("type") != "connected":
                raise RuntimeError(f"authentication rejected: {reply}")
            logger.info("runner ONLINE (user=%s, tier=%s)", reply.get("user_id"), reply.get("tier"))
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    await self._handle(ws, msg)
            finally:
                hb.cancel()

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except Exception as e:
                logger.warning("disconnected: %s — reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    api_key = os.environ.get("REPRYNTT_API_KEY", "")
    if not api_key.startswith("rkey_"):
        raise SystemExit(
            "Set REPRYNTT_API_KEY=rkey_... (from your Repryntt dashboard) "
            "to connect this runner."
        )
    asyncio.run(RunnerClient(api_key).run_forever())


if __name__ == "__main__":
    main()
