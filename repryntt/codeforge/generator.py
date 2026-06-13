"""
CodeForge Generator — LLM-powered code generation for individual modules.

Supports both API providers (NVIDIA, Anthropic, OpenAI-compatible) and local
llama.cpp models. Each module gets its own generation call with full project
context so the LLM understands how the module fits into the whole.

Production hardening (enterprise-grade output):
- Repetition penalty to prevent degenerate loops
- Post-processing pipeline: fence stripping, dedup, syntax validation
- Quality gate: modules must pass syntax check before acceptance
- Retry with error feedback when syntax check fails
- Truncation detection with automatic retry at higher token limit
"""

import ast
import json
import re
import time
import logging
import requests
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path

from .models import ForgeProject, ForgeModule, ModuleStatus

logger = logging.getLogger("codeforge.generator")

# ── Default timeout/retry config ──
API_TIMEOUT = 300    # seconds (free-tier APIs can be slow for large code gen)
MAX_RETRIES = 2
RETRY_DELAY = 15     # seconds between retries

# ── Generation quality controls ──
REPETITION_PENALTY = 1.2      # Penalize repeated tokens (1.0 = off, 1.2 = moderate)
FREQUENCY_PENALTY = 0.3       # Penalize tokens by frequency in output so far
MAX_DUPLICATE_LINES = 3       # Max identical consecutive lines before dedup kicks in
SYNTAX_RETRY_LIMIT = 2        # Re-generate if syntax check fails (separate from API retries)
CODE_MAX_TOKENS = 8192         # Token budget for code generation (was 6000)
TEST_MAX_TOKENS = 6000         # Token budget for test generation (was 4000)


def _load_ai_config() -> Dict[str, Any]:
    """Load AI config from ~/.repryntt/brain/ai_config.json."""
    config_path = Path.home() / ".repryntt" / "brain" / "ai_config.json"
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text())
        # Handle nested ai_provider structure
        if "ai_provider" in raw and isinstance(raw["ai_provider"], dict):
            return raw["ai_provider"]
        return raw
    except Exception as e:
        logger.warning(f"Failed to load ai_config: {e}")
        return {}


# Frontier model markers — when the loaded `model` matches one of these,
# it's strong enough to code on its own and we should NOT silently swap to
# a separate `coding_model`. The operator deliberately loaded a frontier
# model; honor it for coding too. Weak/free primaries (mistral-small,
# gemini-flash, etc.) still fall through to a code-specialist coding_model.
_FRONTIER_MODEL_MARKERS = (
    "grok", "claude", "opus", "sonnet",
    "gpt-4", "gpt-5", "o1", "o3", "o4",
    "gemini-2.5-pro", "gemini-3", "deepseek",
)


def _is_frontier_model(name: str) -> bool:
    """True if the model name looks like a frontier-class model that can
    handle coding directly (so we don't override it with coding_model)."""
    n = (name or "").lower()
    return any(m in n for m in _FRONTIER_MODEL_MARKERS)


def _resolve_provider(config: dict, provider: str = "",
                       model_override: str = "") -> Dict[str, str]:
    """Resolve endpoint, api_key, model from config for a given provider name.

    CodeForge always prefers the ``coding_model`` key (if present in the
    provider section) over the default ``model``, so the heartbeat / general
    agent can keep using a conversational model while forge uses a
    code-specialised one.

    Falls back to nvidia if the requested provider has no working endpoint
    (e.g. ``local`` when llama.cpp isn't running, or ``auto`` which doesn't
    exist in config).

    `model_override`: non-empty string replaces the resolved model name. This
    is how operators bring their own model per-project (Claude / GPT / local
    LLMs / Qwen / whatever). The provider's endpoint and api_key are still
    used — only the `model` field on the request body changes.
    """
    # Cloud/BYOK override: the worker injects the customer's OpenAI-compatible
    # coding provider via env. Takes precedence over any on-disk ai_config so
    # builds run on the customer's key, never the operator's.
    import os as _os
    _env_key = _os.environ.get("REPRYNTT_CODING_API_KEY", "")
    _env_ep = _os.environ.get("REPRYNTT_CODING_ENDPOINT", "")
    if _env_key and _env_ep:
        return {
            "provider": _os.environ.get("REPRYNTT_CODING_PROVIDER", "byok"),
            "endpoint": _env_ep,
            "api_key": _env_key,
            "model": model_override or _os.environ.get("REPRYNTT_CODING_MODEL", "gpt-4o"),
        }

    if not provider:
        provider = config.get("andrew_provider",
                   config.get("artemis_provider",
                   config.get("provider", "nvidia")))

    def _try_section(prov: str) -> Optional[Dict[str, str]]:
        section = config.get(prov, {})
        if isinstance(section, dict) and section.get("endpoint"):
            loaded = section.get("model", "")
            coder = section.get("coding_model")
            # Operator can force the coding_model even with a frontier
            # primary via "force_coding_model": true (rare — e.g. a
            # dedicated code-tuned variant they trust more).
            force_coder = bool(section.get("force_coding_model", False))
            if coder and (force_coder or not _is_frontier_model(loaded)):
                # Weak/free primary (or forced) → use the code specialist.
                model = coder
                logger.info(f"CodeForge using coding model: {model}")
            else:
                # Frontier primary loaded → it does the coding itself.
                model = loaded or coder or ""
                if loaded and _is_frontier_model(loaded):
                    logger.info(
                        f"CodeForge using loaded frontier model for coding: {model} "
                        f"(provider={prov})"
                    )
            return {
                "provider": prov,
                "endpoint": section["endpoint"],
                "api_key": section.get("api_key", ""),
                "model": model,
            }
        return None

    # Try requested provider first
    result = _try_section(provider)
    if result:
        # For 'local' provider, verify the server is actually reachable
        if provider == "local":
            import urllib.request
            try:
                req = urllib.request.Request(
                    result["endpoint"].rsplit("/chat/completions", 1)[0] + "/v1/models",
                    method="GET",
                )
                urllib.request.urlopen(req, timeout=2)
            except Exception:
                logger.warning(
                    f"Local LLM server not reachable at {result['endpoint']}, "
                    f"falling back to nvidia"
                )
                result = None

    # Fallback to nvidia if requested provider didn't work
    if not result and provider != "nvidia":
        logger.warning(f"Provider '{provider}' has no working endpoint, falling back to nvidia")
        result = _try_section("nvidia")

    if result:
        if model_override:
            result["model"] = model_override
            logger.info(f"CodeForge: per-project model override → {model_override}")
        return result

    # Last resort: flat config (endpoint/api_key at top level)
    model = model_override or config.get("coding_model") or config.get("model", "")
    return {
        "provider": provider,
        "endpoint": config.get("endpoint", ""),
        "api_key": config.get("api_key", ""),
        "model": model,
    }


def resolve_critic_provider(config: Dict[str, Any],
                            default_info: Dict[str, str],
                            purpose: str = "architecture_judge"
                            ) -> Dict[str, str]:
    """Resolve a provider_info for gate-level calls (judge / critic).

    Reads `config["critic_provider"]`; falls back to `default_info` if the
    operator hasn't configured one. This keeps the free-tier/Python-only
    install path unchanged — the architecture judge and critic gates only
    hit a different model if the operator opts in.

    Block shape:
        "critic_provider": {
            "provider": "anthropic",            # or xai/nvidia/...
            "model": "claude-opus-4-7",         # optional
            "purposes": ["architecture_judge"]  # optional allowlist
        }
    """
    cp = config.get("critic_provider") if isinstance(config, dict) else None
    if not isinstance(cp, dict):
        return default_info
    allowed = cp.get("purposes")
    if isinstance(allowed, list) and purpose not in allowed:
        return default_info
    provider = cp.get("provider")
    if not provider:
        return default_info
    info = _resolve_provider(config, provider=provider,
                             model_override=cp.get("model", ""))
    if info and info.get("endpoint"):
        logger.info(
            f"CodeForge critic-routing: purpose={purpose!r} → "
            f"provider={info.get('provider')!r}, model={info.get('model')!r}"
        )
        return info
    return default_info


# ── Provider rate gate ─────────────────────────────────────────────────
# CodeForge originally went around the daemon's rate limiter because the
# generator runs in its own thread pool. That meant heavy code-generation
# bursts (one call per module on a 12-module project) hammered the same
# NIM endpoint Andrew was using, both stampeding the 40 RPM ceiling.
# This local gate gives codeforge its own per-provider request spacing
# matching the daemon's defaults; the dict + lock are module-level so all
# threads share state.

import threading as _rl_threading
_RL_LOCK = _rl_threading.Lock()
_RL_LAST: Dict[str, float] = {}

# Provider → minimum seconds between requests
_RL_INTERVAL: Dict[str, float] = {
    "anthropic": 12.0,   # 5 RPM tier 1
    "nvidia":    6.0,    # well under 40 RPM free tier
    "openai":    1.0,
    "xai":       1.0,
    "openrouter":1.0,
    "local":     0.1,
    "custom":    0.1,
}


def _gate_provider(provider: str) -> None:
    """Sleep until the per-provider minimum interval is satisfied."""
    if not provider:
        return
    interval = _RL_INTERVAL.get(provider, 1.0)
    with _RL_LOCK:
        last = _RL_LAST.get(provider, 0.0)
        now = time.time()
        wait = interval - (now - last)
        if wait > 0:
            # Release the lock while sleeping so other threads can queue
            pass
        _RL_LAST[provider] = max(now + max(0.0, wait), now)
    if wait > 0:
        time.sleep(wait)


def _call_llm(messages: List[Dict], provider_info: Dict[str, str],
              max_tokens: int = 4000, temperature: float = 0.3) -> Optional[str]:
    """
    Call an LLM API (OpenAI-compatible or Anthropic) and return the text response.
    Works with NVIDIA, local llama.cpp, or any OpenAI-compatible endpoint.

    Honors a per-provider request-spacing gate (see ``_gate_provider``) so
    codeforge's worker threads don't bypass the rate limit and stampede the
    daemon's allowance.
    """
    endpoint = provider_info.get("endpoint", "")
    api_key = provider_info.get("api_key", "")
    model = provider_info.get("model", "")
    provider = provider_info.get("provider", "")

    if not endpoint:
        logger.error("No endpoint configured for code generation")
        return None

    # Per-provider rate gate — enforces minimum spacing between requests
    # (the bypass that was burning the NIM rate budget).
    _gate_provider(provider)

    # Ensure endpoint ends with /chat/completions for OpenAI-compatible
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    # Adaptive-thinking flagships (Opus 4.7+, Fable 5+, Mythos 5+) deprecated
    # the `temperature` param — extended thinking is always on. Sending it
    # returns 400 from the Anthropic OpenAI-compat endpoint. Skip for those
    # models; include for everything else.
    _m = (model or "").lower()
    _temp_deprecated = (
        "opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
        or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
        or "claude-5-" in _m
    )
    if not _temp_deprecated:
        body["temperature"] = temperature
    # frequency_penalty: supported by OpenAI, NVIDIA, etc. but NOT xAI/Grok,
    # and also rejected by the Anthropic compat shim on those flagships.
    if provider not in ("xai",) and not _temp_deprecated:
        body["frequency_penalty"] = FREQUENCY_PENALTY
    # Some providers support repetition_penalty (NVIDIA NIM, vLLM, llama.cpp)
    # but not all OpenAI-compatible endpoints do. Include it — harmless if ignored.
    if provider in ("nvidia", "local", "custom"):
        body["repetition_penalty"] = REPETITION_PENALTY

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                endpoint, json=body, headers=headers,
                timeout=API_TIMEOUT
            )
            if resp.status_code == 429:
                wait = RETRY_DELAY * (attempt + 1)
                logger.warning(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            # OpenAI format
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")

            return None

        except requests.exceptions.Timeout:
            logger.warning(f"API timeout (attempt {attempt + 1}/{MAX_RETRIES + 1})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue
        except Exception as e:
            logger.error(f"API call failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue

    return None


def _extract_code_block(text: str, language: str = "python") -> str:
    """Extract code from markdown code blocks in LLM response."""
    # Try language-specific block first
    pattern = rf"```{re.escape(language)}\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try generic code block
    pattern = r"```\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try any code block
    pattern = r"```\w*\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # No code block found — return the whole response (might be raw code)
    return text.strip()


# ── Post-processing Pipeline ──
# These functions clean up common LLM generation artifacts before the code
# is accepted. Inspired by enterprise code-gen systems (Blitzy, Devin, etc.)
# that validate every generated artifact before committing it.

def _strip_markdown_fences(code: str) -> str:
    """Remove any remaining markdown code fences from generated code."""
    # Strip leading ```language and trailing ```
    code = re.sub(r'^```\w*\s*\n?', '', code)
    code = re.sub(r'\n?```\s*$', '', code)
    return code.strip()


def _deduplicate_lines(code: str, max_consecutive: int = MAX_DUPLICATE_LINES) -> str:
    """Remove runs of identical consecutive lines beyond the threshold.

    Catches the classic LLM hallucination loop where the model emits the same
    import or line of code hundreds of times.
    """
    lines = code.split('\n')
    cleaned = []
    prev_line = None
    repeat_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped == prev_line and stripped:  # Empty lines don't count
            repeat_count += 1
            if repeat_count < max_consecutive:
                cleaned.append(line)
            # else: skip the duplicate
        else:
            repeat_count = 0
            cleaned.append(line)
            prev_line = stripped

    original_len = len(lines)
    cleaned_len = len(cleaned)
    if original_len - cleaned_len > 5:
        logger.warning(f"Dedup removed {original_len - cleaned_len} duplicate lines")
    return '\n'.join(cleaned)


def _detect_truncation(code: str, language: str = "python") -> bool:
    """Detect if LLM output was truncated mid-expression.

    Common signs: unclosed brackets, truncated string, ends with operator,
    or code ends mid-function.
    """
    if not code.strip():
        return True

    last_line = code.rstrip().split('\n')[-1].strip()

    # Ends mid-expression
    if last_line.endswith((',', '(', '[', '{', '\\', '+', '-', '*', '=', ':')):
        return True

    # Unclosed brackets (simple count — not perfect but catches most cases)
    if language == "python":
        opens = code.count('(') - code.count(')')
        if opens > 2:
            return True
        opens = code.count('[') - code.count(']')
        if opens > 2:
            return True

    # Ends with incomplete string
    if last_line.count('"') % 2 != 0 or last_line.count("'") % 2 != 0:
        return True

    return False


def _check_python_syntax(code: str) -> Tuple[bool, str]:
    """Validate Python syntax via AST parse."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"Line {e.lineno}: {e.msg}"


def _postprocess_code(code: str, language: str = "python") -> Tuple[str, bool, str]:
    """Full post-processing pipeline for generated code.

    Returns (cleaned_code, is_valid, error_message).
    """
    if not code or not code.strip():
        return "", False, "Empty code"

    # Step 1: Strip any remaining markdown fences
    code = _strip_markdown_fences(code)

    # Step 2: Deduplicate hallucination loops
    code = _deduplicate_lines(code)

    # Step 3: Remove obvious garbage lines (model noise)
    lines = code.split('\n')
    cleaned_lines = []
    for line in lines:
        # Skip lines that are clearly model noise
        if re.match(r'^\s*from\s+fast0\.\d+\s+import', line):
            continue  # Garbage like "from fast0.15 import main"
        cleaned_lines.append(line)
    code = '\n'.join(cleaned_lines)

    # Step 4: Syntax validation (Python only — other languages get bracket check)
    is_valid = True
    error_msg = ""
    if language == "python":
        is_valid, error_msg = _check_python_syntax(code)
    elif language in ("javascript", "typescript", "js", "ts"):
        # Basic bracket balance check
        for open_ch, close_ch in [('(', ')'), ('[', ']'), ('{', '}')]:
            if abs(code.count(open_ch) - code.count(close_ch)) > 2:
                is_valid = False
                error_msg = f"Unbalanced {open_ch}{close_ch}"
                break

    # Step 5: Truncation detection
    if is_valid and _detect_truncation(code, language):
        logger.warning("Code appears truncated")
        # Don't fail — truncated code might still parse. Flag it.

    return code, is_valid, error_msg


def generate_spec(description: str, provider_info: Dict[str, str]) -> Optional[Dict]:
    """
    Stage 1: Parse a natural language request into a structured project spec.
    Returns a JSON dict with project type, languages, frameworks, services, etc.
    """
    messages = [
        {"role": "system", "content": (
            "You are a senior software architect. Parse the user's project request "
            "into a precise technical specification. You must identify the PROJECT TYPE "
            "and generate a complete production-ready spec.\n\n"
            "Reply with ONLY a JSON object (no markdown, no explanation) with these fields:\n"
            '  "name": "project-name-kebab-case",\n'
            '  "project_type": "library|cli|api|webapp|fullstack|saas|automation",\n'
            '  "language": "python|javascript|typescript|rust|go|java",\n'
            '  "framework": "primary framework (flask/fastapi/express/react/vue/svelte/nextjs/etc)",\n'
            '  "frontend_framework": "react|vue|svelte|nextjs|none" (for fullstack/saas),\n'
            '  "backend_framework": "fastapi|express|flask|django|none" (for fullstack/saas),\n'
            '  "database": "postgres|mysql|mongodb|sqlite|none",\n'
            '  "services": ["postgres", "redis", ...] (external services needed),\n'
            '  "modules": ["list of source files to create"],\n'
            '  "dependencies": ["external packages needed"],\n'
            '  "description": "one-paragraph technical summary",\n'
            '  "constraints": ["any constraints or requirements"],\n'
            '  "test_framework": "pytest|unittest|jest|vitest|mocha|cargo test|go test",\n'
            '  "features": ["list of key features/endpoints/pages"],\n'
            '  "auth_required": true/false (for saas/webapp),\n'
            '  "needs_docker": true/false\n\n'
            "PROJECT TYPE GUIDE:\n"
            "- library: standalone package (pip/npm installable)\n"
            "- cli: command-line tool with argument parsing\n"
            "- api: REST/GraphQL backend with endpoints\n"
            "- webapp: frontend web application (SPA or SSR)\n"
            "- fullstack: frontend + backend + optional database\n"
            "- saas: fullstack + auth + payments + multi-tenancy\n"
            "- automation: scripts, bots, data pipelines\n\n"
            "For fullstack/saas projects, include BOTH frontend and backend modules."
        )},
        {"role": "user", "content": description},
    ]

    response = _call_llm(messages, provider_info, max_tokens=2000, temperature=0.2)
    if not response:
        return None

    # Extract JSON from response
    try:
        # Try direct parse
        return json.loads(response)
    except json.JSONDecodeError:
        # Try extracting from code block
        import re
        match = re.search(r"```(?:json)?\s*\n(.*?)```", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # Try finding JSON object in the response
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    logger.error(f"Failed to parse spec from LLM response: {response[:200]}")
    return None


def generate_architecture(project: ForgeProject,
                          provider_info: Dict[str, str]) -> Optional[Dict]:
    """
    Stage 2: Given a spec, produce the file/module architecture — interfaces,
    dependency graph, and module descriptions. Now handles full-stack projects
    with frontend/backend/config/Docker organization.
    """
    spec_text = json.dumps(project.spec, indent=2)
    project_type = project.project_type or project.spec.get("project_type", "library")

    # Build type-specific guidance
    type_guidance = ""
    if project_type in ("fullstack", "saas"):
        type_guidance = (
            "\n\nFULL-STACK ARCHITECTURE RULES:\n"
            "- Separate frontend/ and backend/ directories\n"
            "- Backend modules: server entry, routes/controllers, models, middleware, config\n"
            "- Frontend modules: App component, pages/views, API client, styles\n"
            "- Include a Dockerfile for the backend\n"
            "- Include a docker-compose.yml if services (db/cache) are needed\n"
            "- Include .env.example with all environment variables\n"
            "- Include a Makefile or scripts/ for common operations\n"
        )
        if project_type == "saas":
            type_guidance += (
                "- Include auth middleware (JWT or session-based)\n"
                "- Include user/tenant models\n"
                "- Include a billing/payments module\n"
            )
    elif project_type == "api":
        type_guidance = (
            "\n\nAPI ARCHITECTURE RULES:\n"
            "- Include server entry point, route handlers, models, middleware\n"
            "- Include a Dockerfile\n"
            "- Include .env.example with API configuration\n"
            "- Include health check endpoint\n"
        )
    elif project_type == "webapp":
        type_guidance = (
            "\n\nWEB APP ARCHITECTURE RULES:\n"
            "- Standard SPA structure: App, pages/routes, components, styles\n"
            "- Include a proper build configuration\n"
            "- Include index.html entry point\n"
        )

    messages = [
        {"role": "system", "content": (
            "You are a senior software architect designing a production-grade project structure. "
            "Given a project specification, produce a detailed, deployable architecture. "
            "Reply with ONLY a JSON object (no markdown) with:\n"
            '  "file_tree": ["src/main.py", "src/utils.py", "Dockerfile", ...],\n'
            '  "modules": [\n'
            '    {"filename": "src/main.py", "description": "what it does", '
            '"language": "python", "interfaces": "class/function signatures", '
            '"dependencies": ["src/utils.py"]},\n'
            '    ...\n'
            '  ],\n'
            '  "entry_point": "src/main.py",\n'
            '  "external_deps": ["flask>=3.0", "requests"],\n'
            '  "services": [{"name": "postgres", "image": "postgres:16-alpine", '
            '"ports": ["5432:5432"], "env_vars": {"POSTGRES_DB": "app"}}],\n'
            '  "env_vars": {"DATABASE_URL": "postgresql://...", "SECRET_KEY": "changeme"},\n'
            '  "notes": "architectural decisions"\n\n'
            "RULES:\n"
            "1. Each module should have a clear single responsibility\n"
            "2. Include configuration files (Dockerfile, .env.example, requirements.txt/package.json)\n"
            "3. For projects needing databases, include migration/schema modules\n"
            "4. Include a proper entry point that can be run directly\n"
            "5. Config files (Dockerfile, docker-compose.yml, .env.example) are modules too — "
            "set their language to 'dockerfile', 'yaml', or 'env' respectively"
            + type_guidance
        )},
        {"role": "user", "content": (
            f"Project: {project.name}\n"
            f"Type: {project_type}\n"
            f"Language: {project.language}\n"
            f"Framework: {project.framework}\n"
            f"Frontend: {project.frontend_framework or 'n/a'}\n"
            f"Backend: {project.backend_framework or 'n/a'}\n"
            f"Database: {project.database or 'none'}\n"
            f"Spec:\n{spec_text}"
        )},
    ]

    response = _call_llm(messages, provider_info, max_tokens=3000, temperature=0.2)
    if not response:
        return None

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    logger.error(f"Failed to parse architecture: {response[:200]}")
    return None


def generate_module_code(module: ForgeModule, project: ForgeProject,
                         provider_info: Dict[str, str]) -> Optional[str]:
    """
    Stage 3: Generate implementation code for a single module.
    Gets full project context so the LLM knows how this module fits.
    Handles source code, Dockerfiles, configs, and infrastructure files.
    """
    # Build context of other modules' interfaces
    other_modules = []
    for m in project.modules:
        if m.module_id != module.module_id and m.interfaces:
            other_modules.append(f"# {m.filename}\n{m.interfaces}")
    context = "\n\n".join(other_modules) if other_modules else "No other modules yet."

    # Detect if this is a config/infrastructure file
    config_extensions = {
        "dockerfile": "dockerfile", "docker-compose.yml": "yaml",
        "docker-compose.yaml": "yaml", ".env.example": "env",
        "makefile": "makefile", ".gitignore": "gitignore",
        "nginx.conf": "nginx", "tsconfig.json": "json",
        "vite.config.ts": "typescript", "webpack.config.js": "javascript",
    }
    fname_lower = module.filename.lower().split("/")[-1]
    is_config = fname_lower in config_extensions or module.language in (
        "dockerfile", "yaml", "env", "makefile", "json", "toml"
    )

    project_type = project.project_type or project.spec.get("project_type", "library")

    # Build environment context for modules that need connection strings
    env_context = ""
    if project.database or project.services:
        env_context = (
            f"\n\nProject uses these services:\n"
            f"- Database: {project.database or 'none'}\n"
            f"- Services: {', '.join(s.name if hasattr(s, 'name') else str(s) for s in project.services) or 'none'}\n"
            f"Use environment variables for all connection strings (DATABASE_URL, REDIS_URL, etc.)\n"
            f"NEVER hardcode credentials — read from os.environ or process.env\n"
        )

    if is_config:
        system_prompt = (
            f"You are a DevOps engineer writing production configuration files. "
            f"Generate the complete contents of {module.filename} for this project.\n\n"
            f"Project: {project.name} ({project_type})\n"
            f"Language: {project.language}\n"
            f"Framework: {project.framework}\n"
            f"Database: {project.database or 'none'}\n\n"
            "Rules:\n"
            "1. Write COMPLETE, production-ready configuration\n"
            "2. Use multi-stage builds for Dockerfiles\n"
            "3. Include health checks where appropriate\n"
            "4. Use environment variables for all secrets\n"
            "5. Follow security best practices (non-root user, minimal base image)\n\n"
            "OUTPUT FORMAT: Reply with ONLY the raw file contents.\n"
            "DO NOT wrap in markdown code fences (no ```). Just the raw code."
        )
    else:
        system_prompt = (
            f"You are an expert {module.language} developer writing production-grade "
            f"software for enterprise deployment. Write COMPLETE, working code for "
            f"the specified module.\n\n"
            "CRITICAL RULES:\n"
            "1. Write COMPLETE code — no stubs, no TODOs, no placeholders, no '...'\n"
            "2. Every function and class must be fully implemented\n"
            "3. Include proper error handling and input validation\n"
            "4. Use type hints (Python) or TypeScript types where applicable\n"
            "5. Follow the language's best practices and conventions\n"
            "6. Do NOT use eval(), exec(), pickle, or os.system()\n"
            "7. Use parameterized queries for any database operations\n"
            "8. Do NOT hardcode secrets, passwords, or API keys — use env vars\n"
            "9. All database connections should use connection pooling\n"
            "10. Use proper logging (not print statements)\n"
            "11. Do NOT repeat yourself — write each import and function ONCE\n"
            "12. Do NOT generate placeholder or dummy implementations\n\n"
            f"OUTPUT FORMAT: Reply with ONLY the raw {module.language} code.\n"
            "DO NOT wrap in markdown code fences (no ```). Just the raw code.\n"
            "DO NOT include explanations before or after the code."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Project: {project.name} ({project.language}"
            f"{', ' + project.framework if project.framework else ''}"
            f", type: {project_type})\n\n"
            f"Module: {module.filename}\n"
            f"Description: {module.description}\n"
            f"Interfaces to implement:\n{module.interfaces}\n\n"
            f"Dependencies on other modules:\n{context}\n\n"
            f"External packages available: "
            f"{', '.join(project.spec.get('dependencies', []))}"
            f"{env_context}\n\n"
            f"Write the complete implementation for {module.filename}."
        )},
    ]

    # ── Generate with syntax-check-and-retry loop ──
    for syntax_attempt in range(SYNTAX_RETRY_LIMIT + 1):
        token_budget = CODE_MAX_TOKENS
        # If retrying due to truncation, increase budget
        if syntax_attempt > 0:
            token_budget = min(CODE_MAX_TOKENS + 2048, 16384)

        response = _call_llm(messages, provider_info,
                             max_tokens=token_budget, temperature=0.3)
        if not response:
            return None

        raw_code = _extract_code_block(response, module.language)
        code, is_valid, error_msg = _postprocess_code(raw_code, module.language)

        if is_valid:
            return code

        # Syntax failed — retry with error feedback
        if syntax_attempt < SYNTAX_RETRY_LIMIT:
            logger.warning(f"Syntax error in {module.filename} (attempt "
                           f"{syntax_attempt + 1}): {error_msg}. Retrying...")
            # Add the error as feedback for the retry
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": (
                f"The code has a syntax error: {error_msg}\n"
                "Fix the error and output the complete corrected code.\n"
                "DO NOT wrap in markdown fences. Just raw code."
            )})
        else:
            logger.error(f"Syntax check failed after {SYNTAX_RETRY_LIMIT + 1} "
                         f"attempts for {module.filename}: {error_msg}")
            # Return the cleaned (but invalid) code — validator will flag it
            return code

    return None


def generate_test_code(module: ForgeModule, project: ForgeProject,
                       provider_info: Dict[str, str]) -> Optional[str]:
    """
    Generate test code for a module. Skips config/infrastructure files.
    """
    # Don't generate tests for config/infrastructure files
    skip_languages = ("dockerfile", "yaml", "env", "makefile", "json", "toml",
                      "gitignore", "markdown", "nginx")
    if module.language in skip_languages:
        return None
    skip_files = ("dockerfile", "docker-compose", ".env", "makefile",
                  ".gitignore", "readme", "license", "tsconfig", "vite.config",
                  "webpack.config", "nginx.conf")
    fname_lower = module.filename.lower().split("/")[-1]
    if any(fname_lower.startswith(s) for s in skip_files):
        return None

    test_framework = project.spec.get("test_framework", "pytest")

    # Map language to appropriate test framework if not specified
    lang_test_defaults = {
        "python": "pytest",
        "javascript": "jest",
        "typescript": "jest",
        "js": "jest",
        "ts": "jest",
        "rust": "cargo test",
        "go": "go test",
        "java": "junit",
    }
    if test_framework == "pytest" and module.language not in ("python",):
        test_framework = lang_test_defaults.get(module.language, test_framework)

    messages = [
        {"role": "system", "content": (
            f"You are a QA engineer writing {test_framework} tests for production "
            f"software. Write focused, practical unit tests for the given "
            f"{module.language} module.\n\n"
            "CRITICAL RULES:\n"
            "1. Test all public functions/methods — one test per behavior\n"
            "2. Include edge cases and error conditions\n"
            "3. Use descriptive test names\n"
            "4. Mock external dependencies (network, filesystem, databases)\n"
            "5. Each test should be independent and deterministic\n"
            "6. Keep tests concise — no bloated test suites (max 15-20 tests)\n"
            "7. Do NOT test private internals — only public API\n"
            "8. Do NOT duplicate test logic — if two tests check the same thing, merge them\n"
            "9. Do NOT write performance, memory, or concurrency tests unless asked\n"
            "10. Use simple, concrete test values — not generated data\n\n"
            f"OUTPUT FORMAT: Reply with ONLY the raw {module.language} test code.\n"
            "DO NOT wrap in markdown code fences (no ```). Just the raw code.\n"
            "DO NOT include explanations before or after the code."
        )},
        {"role": "user", "content": (
            f"Module: {module.filename}\n"
            f"Implementation:\n{module.implementation}\n\n"
            f"Write focused {test_framework} tests. Keep it under 20 tests."
        )},
    ]

    response = _call_llm(messages, provider_info,
                         max_tokens=TEST_MAX_TOKENS, temperature=0.3)
    if not response:
        return None

    raw_code = _extract_code_block(response, module.language)
    code, is_valid, error_msg = _postprocess_code(raw_code, module.language)

    if not is_valid:
        logger.warning(f"Test code for {module.filename} has syntax issues: {error_msg}")
        # Retry once with feedback
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": (
            f"The test code has a syntax error: {error_msg}\n"
            "Fix the error and output the complete corrected test code.\n"
            "DO NOT wrap in markdown fences. Just raw code."
        )})
        response = _call_llm(messages, provider_info,
                             max_tokens=TEST_MAX_TOKENS, temperature=0.2)
        if response:
            raw_retry = _extract_code_block(response, module.language)
            retry_code, retry_valid, _ = _postprocess_code(raw_retry, module.language)
            if retry_valid:
                return retry_code

    return code


def fix_module_code(module: ForgeModule, project: ForgeProject,
                    error_output: str,
                    provider_info: Dict[str, str]) -> Optional[str]:
    """
    Fix-iterate: given test failures or validation errors, ask the LLM to
    fix the implementation. Returns corrected code.

    If test code itself has syntax errors, fix the test too and return the
    implementation (test fix is applied via module.test_code update).
    """
    # Check if the error is actually in the test code, not the implementation
    test_has_syntax_error = False
    if module.test_code:
        test_valid, test_err = _check_python_syntax(module.test_code)
        if not test_valid:
            test_has_syntax_error = True
            logger.info(f"Test code for {module.filename} has syntax error: {test_err}")

    messages = [
        {"role": "system", "content": (
            f"You are a senior {module.language} developer debugging code. "
            "The tests or validation for this module failed. Fix the implementation "
            "so all tests pass.\n\n"
            "CRITICAL RULES:\n"
            "1. Analyze the error output carefully — identify the ROOT CAUSE\n"
            "2. Fix ONLY what's broken — don't rewrite everything\n"
            "3. Make sure the fix doesn't break the module's interfaces\n"
            "4. Do NOT use eval(), exec(), or other unsafe patterns\n"
            "5. Write each import and function ONCE — no duplicates\n\n"
            f"OUTPUT FORMAT: Reply with ONLY the complete fixed {module.language} code.\n"
            "DO NOT wrap in markdown code fences (no ```). Just the raw code."
        )},
        {"role": "user", "content": (
            f"Module: {module.filename}\n\n"
            f"Current implementation:\n{module.implementation}\n\n"
            + (f"Test code:\n{module.test_code}\n\n" if not test_has_syntax_error else
               f"NOTE: The test code itself has a syntax error ({test_err}). "
               f"Focus on fixing the implementation only.\n\n")
            + f"Error output:\n{error_output[:3000]}\n\n"
            f"Fix the implementation."
        )},
    ]

    response = _call_llm(messages, provider_info,
                         max_tokens=CODE_MAX_TOKENS, temperature=0.2)
    if not response:
        return None

    raw_code = _extract_code_block(response, module.language)
    code, is_valid, error_msg = _postprocess_code(raw_code, module.language)

    if not is_valid:
        logger.warning(f"Fix attempt for {module.filename} still has syntax error: {error_msg}")

    return code


def generate_readme(project: ForgeProject,
                    provider_info: Dict[str, str]) -> Optional[str]:
    """Generate a README.md for the completed project."""
    modules_desc = "\n".join(
        f"- {m.filename}: {m.description}" for m in project.modules
    )
    project_type = project.project_type or project.spec.get("project_type", "library")

    setup_hint = ""
    if project.database or project.services:
        svc_names = ", ".join(s.name if hasattr(s, "name") else str(s) for s in project.services)
        setup_hint = (
            f"\nThis project uses Docker services: {svc_names or project.database}\n"
            "Include Docker Compose setup instructions in the README.\n"
        )

    messages = [
        {"role": "system", "content": (
            "Write a professional README.md for this software project. Include: "
            "project name, description, features, prerequisites, installation/setup "
            "instructions, environment variables, usage examples, API documentation "
            "(if applicable), project structure, testing instructions, deployment "
            "instructions (if Docker/services), and license (MIT). "
            "Be thorough but concise. Use proper markdown formatting."
        )},
        {"role": "user", "content": (
            f"Project: {project.name}\n"
            f"Type: {project_type}\n"
            f"Description: {project.description}\n"
            f"Language: {project.language}\n"
            f"Framework: {project.framework}\n"
            f"Frontend: {project.frontend_framework or 'n/a'}\n"
            f"Backend: {project.backend_framework or 'n/a'}\n"
            f"Database: {project.database or 'none'}\n"
            f"Dependencies: {', '.join(project.spec.get('dependencies', []))}\n"
            f"Modules:\n{modules_desc}\n"
            f"Quality Score: {project.quality.overall_score if project.quality else 'N/A'}/100"
            f"{setup_hint}"
        )},
    ]

    response = _call_llm(messages, provider_info, max_tokens=3000, temperature=0.4)
    return response


def generate_ci_config(project: ForgeProject,
                       provider_info: Dict[str, str]) -> Optional[str]:
    """Generate a GitHub Actions CI/CD workflow for the project."""
    project_type = project.project_type or project.spec.get("project_type", "library")

    services_yaml = ""
    if project.services:
        svc_names = [s.name if hasattr(s, "name") else str(s) for s in project.services]
        services_yaml = f"Services needed: {', '.join(svc_names)}"

    messages = [
        {"role": "system", "content": (
            "You are a DevOps engineer. Generate a GitHub Actions CI/CD workflow file "
            "(YAML) for this project. Include:\n"
            "1. Lint/format check\n"
            "2. Run all tests\n"
            "3. Build step (if applicable)\n"
            "4. Docker build (if Dockerfile exists)\n"
            "5. Use service containers for databases if needed\n"
            "6. Cache dependencies for faster builds\n"
            "Reply with ONLY the YAML content in a code block."
        )},
        {"role": "user", "content": (
            f"Project: {project.name} ({project_type})\n"
            f"Language: {project.language}\n"
            f"Framework: {project.framework}\n"
            f"Test framework: {project.spec.get('test_framework', 'pytest')}\n"
            f"Dependencies: {', '.join(project.spec.get('dependencies', []))}\n"
            f"{services_yaml}\n"
            f"Generate .github/workflows/ci.yml"
        )},
    ]

    response = _call_llm(messages, provider_info, max_tokens=2000, temperature=0.3)
    if not response:
        return None

    return _extract_code_block(response, "yaml")


# ─── Architecture sanity check ─────────────────────────────────────────

def judge_architecture(project: ForgeProject,
                       provider_info: Dict[str, str],
                       config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One cheap LLM-judge call after `_stage_architect` to catch obviously
    bad plans before we spend the entire generate stage on them.

    Asks: given the spec, does the proposed module list cover it? Are critical
    pieces missing? Are dependencies sensible?

    Returns:
        {ok: bool, concerns: List[str], reasoning: str}

    On any error returns `{ok: True, concerns: [], reasoning: "<error>"}` so
    the build doesn't get blocked by judge-itself failures.
    """
    spec = project.spec or {}
    modules = project.architecture.get("modules", []) if project.architecture else []
    if not modules:
        return {"ok": False, "concerns": ["architecture has no modules"],
                "reasoning": "empty module list"}

    module_summary = "\n".join(
        f"- {m.get('filename', '?')}: {m.get('description', '(no description)')[:120]} "
        f"(deps: {', '.join(m.get('dependencies', []) or []) or 'none'})"
        for m in modules[:30]
    )

    project_type = project.project_type or spec.get("project_type", "library")
    services = ", ".join(
        s.get("name", "?") if isinstance(s, dict) else str(s)
        for s in project.architecture.get("services", []) or []
    )

    messages = [
        {"role": "system", "content": (
            "You are a senior code architect reviewing a proposed module structure "
            "before code generation begins. Be skeptical and concrete. Your job is "
            "to catch obvious gaps — missing critical modules, nonsensical "
            "dependencies, mismatch between spec and architecture — BEFORE the "
            "team wastes hours generating from a broken plan.\n\n"
            "Output STRICT JSON with this exact shape:\n"
            "{\n"
            '  "ok": true | false,\n'
            '  "concerns": ["specific concern 1", "specific concern 2", ...],\n'
            '  "reasoning": "one-paragraph summary of your assessment"\n'
            "}\n\n"
            "Set ok=true ONLY if the architecture is reasonable. Set ok=false if "
            "any critical piece is missing, any dependency is wrong, or the "
            "module set obviously cannot deliver the spec. Maximum 5 concerns. "
            "Output ONLY the JSON object — no prose, no markdown fences."
        )},
        {"role": "user", "content": (
            f"# Project spec\n"
            f"Name: {project.name}\n"
            f"Type: {project_type}\n"
            f"Language: {project.language}\n"
            f"Framework: {project.framework or 'none'}\n"
            f"Database: {project.database or 'none'}\n"
            f"Services: {services or 'none'}\n"
            f"Description:\n{project.description}\n\n"
            f"Key requirements from spec:\n"
            f"{spec.get('requirements') or spec.get('summary') or '(none extracted)'}\n\n"
            f"# Proposed architecture ({len(modules)} modules)\n"
            f"{module_summary}\n\n"
            "Review this architecture against the spec. JSON only."
        )},
    ]

    # Critic-provider routing: if the operator configured a critic_provider
    # block in ai_config, route this judge call through it (frontier model).
    # Else fall back to the project's primary provider_info (cheap producer).
    routed_info = provider_info
    if config is not None:
        try:
            routed_info = resolve_critic_provider(config, provider_info,
                                                  purpose="architecture_judge")
        except Exception:
            routed_info = provider_info

    try:
        response = _call_llm(messages, routed_info, max_tokens=800, temperature=0.2)
    except Exception as e:
        return {"ok": True, "concerns": [],
                "reasoning": f"judge call raised: {e} — proceeding without sanity check"}
    if not response:
        return {"ok": True, "concerns": [],
                "reasoning": "judge returned empty — proceeding"}

    # Parse JSON robustly: strip markdown fences if present
    import re as _re
    text = response.strip()
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if m:
        text = m.group(0)
    try:
        result = json.loads(text)
    except Exception as e:
        logger.warning(f"judge_architecture: could not parse JSON: {e}")
        return {"ok": True, "concerns": [],
                "reasoning": f"judge JSON parse failed — proceeding ({response[:200]})"}

    if not isinstance(result, dict):
        return {"ok": True, "concerns": [], "reasoning": "judge returned non-object"}

    return {
        "ok": bool(result.get("ok", True)),
        "concerns": [str(c) for c in (result.get("concerns") or [])][:5],
        "reasoning": str(result.get("reasoning", ""))[:1000],
    }
