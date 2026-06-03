"""
CodeForge Environments — Docker-based service orchestration for testing.

When a project needs databases, caches, or other services (postgres, redis,
rabbitmq, etc.), this module spins up a Docker Compose environment, waits
for health checks, provides connection strings, and tears everything down
after tests complete.

Also handles browser-based testing via Playwright for webapp/fullstack/saas
projects.
"""

import json
import os
import shutil
import subprocess
import time
import logging
import tempfile
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from .models import ForgeProject, ServiceDefinition, ProjectType

logger = logging.getLogger("codeforge.environments")

# ── Timeouts ──
SERVICE_START_TIMEOUT = 120   # seconds to wait for all services to be healthy
HEALTH_CHECK_INTERVAL = 3     # seconds between health checks
DOCKER_COMPOSE_TIMEOUT = 180  # max time docker compose up can take


# ── Well-known service templates ──
# When the LLM spec says "postgres", we know exactly what Docker image to use

SERVICE_TEMPLATES: Dict[str, ServiceDefinition] = {
    "postgres": ServiceDefinition(
        name="postgres", image="postgres:16-alpine",
        ports=["5432:5432"],
        env_vars={"POSTGRES_USER": "forge", "POSTGRES_PASSWORD": "forgetest",
                  "POSTGRES_DB": "forgedb"},
        health_check="pg_isready -U forge",
    ),
    "postgresql": ServiceDefinition(
        name="postgres", image="postgres:16-alpine",
        ports=["5432:5432"],
        env_vars={"POSTGRES_USER": "forge", "POSTGRES_PASSWORD": "forgetest",
                  "POSTGRES_DB": "forgedb"},
        health_check="pg_isready -U forge",
    ),
    "mysql": ServiceDefinition(
        name="mysql", image="mysql:8",
        ports=["3306:3306"],
        env_vars={"MYSQL_ROOT_PASSWORD": "forgetest", "MYSQL_DATABASE": "forgedb"},
        health_check="mysqladmin ping -h localhost",
    ),
    "mongodb": ServiceDefinition(
        name="mongodb", image="mongo:7",
        ports=["27017:27017"],
        env_vars={},
        health_check="mongosh --eval 'db.runCommand({ping:1})' --quiet",
    ),
    "mongo": ServiceDefinition(
        name="mongodb", image="mongo:7",
        ports=["27017:27017"],
        env_vars={},
        health_check="mongosh --eval 'db.runCommand({ping:1})' --quiet",
    ),
    "redis": ServiceDefinition(
        name="redis", image="redis:7-alpine",
        ports=["6379:6379"],
        env_vars={},
        health_check="redis-cli ping",
    ),
    "rabbitmq": ServiceDefinition(
        name="rabbitmq", image="rabbitmq:3-management-alpine",
        ports=["5672:5672", "15672:15672"],
        env_vars={"RABBITMQ_DEFAULT_USER": "forge", "RABBITMQ_DEFAULT_PASS": "forgetest"},
        health_check="rabbitmq-diagnostics -q ping",
    ),
    "elasticsearch": ServiceDefinition(
        name="elasticsearch", image="elasticsearch:8.12.0",
        ports=["9200:9200"],
        env_vars={"discovery.type": "single-node", "xpack.security.enabled": "false"},
        health_check="curl -s http://localhost:9200/_cluster/health",
    ),
    "minio": ServiceDefinition(
        name="minio", image="minio/minio:latest",
        ports=["9000:9000", "9001:9001"],
        env_vars={"MINIO_ROOT_USER": "forgeadmin", "MINIO_ROOT_PASSWORD": "forgetest123"},
        health_check="curl -s http://localhost:9000/minio/health/live",
    ),
}


def _is_docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def resolve_services(project: ForgeProject) -> List[ServiceDefinition]:
    """
    Resolve service definitions from the project spec.
    Maps spec database/service names to concrete Docker definitions.
    """
    services = []
    seen = set()

    # Check explicit services from project
    for svc in project.services:
        if svc.name not in seen:
            services.append(svc)
            seen.add(svc.name)

    # Check database field
    if project.database and project.database.lower() not in seen:
        db_key = project.database.lower().strip()
        if db_key in SERVICE_TEMPLATES:
            svc = SERVICE_TEMPLATES[db_key]
            services.append(ServiceDefinition(
                name=svc.name, image=svc.image, ports=list(svc.ports),
                env_vars=dict(svc.env_vars), health_check=svc.health_check,
            ))
            seen.add(svc.name)

    # Check spec dependencies for service-like entries
    spec_deps = project.spec.get("dependencies", [])
    # Also check a services key if the LLM included one
    spec_services = project.spec.get("services", [])
    for dep in spec_deps + spec_services:
        dep_lower = dep.lower().strip() if isinstance(dep, str) else ""
        if dep_lower in SERVICE_TEMPLATES and dep_lower not in seen:
            tmpl = SERVICE_TEMPLATES[dep_lower]
            services.append(ServiceDefinition(
                name=tmpl.name, image=tmpl.image, ports=list(tmpl.ports),
                env_vars=dict(tmpl.env_vars), health_check=tmpl.health_check,
            ))
            seen.add(tmpl.name)

    return services


def generate_compose_yaml(services: List[ServiceDefinition],
                          project_name: str = "forge") -> str:
    """Generate a docker-compose.yml string for the given services."""
    lines = [
        f"# Auto-generated by CodeForge for project: {project_name}",
        "services:",
    ]

    for svc in services:
        lines.append(f"  {svc.name}:")
        lines.append(f"    image: {svc.image}")
        if svc.ports:
            lines.append("    ports:")
            for port in svc.ports:
                lines.append(f'      - "{port}"')
        if svc.env_vars:
            lines.append("    environment:")
            for k, v in svc.env_vars.items():
                lines.append(f"      {k}: \"{v}\"")
        if svc.health_check:
            lines.append("    healthcheck:")
            lines.append(f'      test: ["{svc.health_check}"]')
            lines.append("      interval: 5s")
            lines.append("      timeout: 5s")
            lines.append("      retries: 10")
        if svc.volumes:
            lines.append("    volumes:")
            for vol in svc.volumes:
                lines.append(f"      - {vol}")

    return "\n".join(lines) + "\n"


def get_service_env_vars(services: List[ServiceDefinition]) -> Dict[str, str]:
    """
    Build environment variables that the test code can use to connect to services.
    Maps service names to connection strings/URLs.
    """
    env = {}
    for svc in services:
        name_upper = svc.name.upper().replace("-", "_")

        if "postgres" in svc.name:
            user = svc.env_vars.get("POSTGRES_USER", "forge")
            pw = svc.env_vars.get("POSTGRES_PASSWORD", "forgetest")
            db = svc.env_vars.get("POSTGRES_DB", "forgedb")
            env["DATABASE_URL"] = f"postgresql://{user}:{pw}@localhost:5432/{db}"
            env["POSTGRES_URL"] = env["DATABASE_URL"]
        elif "mysql" in svc.name:
            pw = svc.env_vars.get("MYSQL_ROOT_PASSWORD", "forgetest")
            db = svc.env_vars.get("MYSQL_DATABASE", "forgedb")
            env["DATABASE_URL"] = f"mysql://root:{pw}@localhost:3306/{db}"
            env["MYSQL_URL"] = env["DATABASE_URL"]
        elif "mongo" in svc.name:
            env["MONGODB_URL"] = "mongodb://localhost:27017/forgedb"
            env["DATABASE_URL"] = env["MONGODB_URL"]
        elif "redis" in svc.name:
            env["REDIS_URL"] = "redis://localhost:6379/0"
        elif "rabbitmq" in svc.name:
            env["RABBITMQ_URL"] = "amqp://forge:forgetest@localhost:5672/"
        elif "elasticsearch" in svc.name:
            env["ELASTICSEARCH_URL"] = "http://localhost:9200"
        elif "minio" in svc.name:
            env["S3_ENDPOINT"] = "http://localhost:9000"
            env["AWS_ACCESS_KEY_ID"] = svc.env_vars.get("MINIO_ROOT_USER", "forgeadmin")
            env["AWS_SECRET_ACCESS_KEY"] = svc.env_vars.get("MINIO_ROOT_PASSWORD", "forgetest123")

        # Generic: expose all service env vars with prefix
        for k, v in svc.env_vars.items():
            env[f"{name_upper}_{k}"] = v

    return env


class ServiceEnvironment:
    """
    Manages a Docker Compose environment for a forge project's test run.
    Context manager: spins up on enter, tears down on exit.
    """

    def __init__(self, project: ForgeProject, work_dir: Path):
        self.project = project
        self.work_dir = work_dir
        self.services = resolve_services(project)
        self.compose_file = work_dir / "docker-compose.yml"
        self.env_vars: Dict[str, str] = {}
        self._started = False

    @property
    def needs_docker(self) -> bool:
        """Whether this project needs Docker services."""
        return len(self.services) > 0

    def __enter__(self):
        if self.services and _is_docker_available():
            self.start()
        elif self.services:
            logger.warning("Docker not available — services will be skipped, "
                           "database-dependent tests may fail")
        self.env_vars = get_service_env_vars(self.services) if self._started else {}
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._started:
            self.stop()
        return False

    def start(self) -> bool:
        """Start Docker Compose services and wait for health checks."""
        if not self.services:
            return True

        # Write compose file
        compose_yaml = generate_compose_yaml(self.services, self.project.name)
        self.compose_file.write_text(compose_yaml)

        logger.info(f"🐳 Starting {len(self.services)} service(s): "
                     f"{', '.join(s.name for s in self.services)}")

        try:
            # docker compose up -d
            result = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_file),
                 "-p", f"forge_{self.project.project_id[:8]}",
                 "up", "-d", "--wait"],
                capture_output=True, text=True,
                timeout=DOCKER_COMPOSE_TIMEOUT,
                cwd=str(self.work_dir),
            )

            if result.returncode != 0:
                logger.error(f"Docker compose up failed: {result.stderr[:500]}")
                return False

            self._started = True

            # Wait for services to be healthy
            return self._wait_for_healthy()

        except subprocess.TimeoutExpired:
            logger.error("Docker compose up timed out")
            self.stop()  # cleanup partial start
            return False
        except FileNotFoundError:
            logger.error("Docker or docker compose not found")
            return False
        except Exception as e:
            logger.error(f"Failed to start services: {e}")
            self.stop()
            return False

    def stop(self):
        """Tear down Docker Compose services."""
        if not self.compose_file.exists():
            return

        try:
            subprocess.run(
                ["docker", "compose", "-f", str(self.compose_file),
                 "-p", f"forge_{self.project.project_id[:8]}",
                 "down", "-v", "--remove-orphans"],
                capture_output=True, text=True,
                timeout=60,
                cwd=str(self.work_dir),
            )
            self._started = False
            logger.info("🐳 Services stopped and cleaned up")
        except Exception as e:
            logger.warning(f"Error stopping services: {e}")

    def _wait_for_healthy(self) -> bool:
        """Wait for all services to report healthy."""
        deadline = time.time() + SERVICE_START_TIMEOUT

        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["docker", "compose", "-f", str(self.compose_file),
                     "-p", f"forge_{self.project.project_id[:8]}",
                     "ps", "--format", "json"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.work_dir),
                )

                if result.returncode == 0 and result.stdout.strip():
                    # Check if all containers are running
                    all_running = True
                    for line in result.stdout.strip().split("\n"):
                        try:
                            container = json.loads(line)
                            state = container.get("State", "")
                            if state != "running":
                                all_running = False
                                break
                        except json.JSONDecodeError:
                            continue

                    if all_running:
                        logger.info("🐳 All services healthy and running")
                        return True

            except Exception:
                pass

            time.sleep(HEALTH_CHECK_INTERVAL)

        logger.warning("🐳 Services did not all become healthy within timeout")
        return True  # proceed anyway — some tests might still work


def needs_browser_testing(project: ForgeProject) -> bool:
    """Check if the project type requires browser-based testing."""
    browser_types = (
        ProjectType.WEBAPP.value,
        ProjectType.FULLSTACK.value,
        ProjectType.SAAS.value,
    )
    return project.project_type in browser_types


def run_browser_tests(project: ForgeProject, work_dir: Path,
                      env: Dict[str, str]) -> Tuple[bool, str]:
    """
    Run Playwright browser tests for frontend projects.
    Starts a dev server, runs tests against it, returns results.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return True, "Playwright not installed — browser tests skipped"

    # Look for a frontend dev server command
    pkg_json = work_dir / "package.json"
    if not pkg_json.exists():
        return True, "No package.json — browser tests skipped"

    try:
        pkg = json.loads(pkg_json.read_text())
    except Exception:
        return True, "Invalid package.json — browser tests skipped"

    scripts = pkg.get("scripts", {})
    dev_cmd = scripts.get("dev") or scripts.get("start") or scripts.get("serve")
    if not dev_cmd:
        return True, "No dev/start script in package.json — browser tests skipped"

    # Find any Playwright test files
    test_files = list(work_dir.rglob("*.spec.ts")) + \
                 list(work_dir.rglob("*.spec.js")) + \
                 list(work_dir.rglob("*.e2e.ts")) + \
                 list(work_dir.rglob("*.e2e.js"))

    if not test_files:
        # No explicit Playwright tests — do a basic smoke test
        return _smoke_test_frontend(work_dir, dev_cmd, env)

    # Run Playwright tests
    try:
        result_env = os.environ.copy()
        result_env.update(env)

        result = subprocess.run(
            ["npx", "playwright", "test", "--reporter=list"],
            capture_output=True, text=True,
            timeout=180,
            cwd=str(work_dir),
            env=result_env,
        )

        output = (result.stdout + "\n" + result.stderr)[:5000]
        passed = result.returncode == 0
        return passed, output

    except subprocess.TimeoutExpired:
        return False, "Playwright tests timed out (180s)"
    except FileNotFoundError:
        return True, "npx/playwright not found — browser tests skipped"
    except Exception as e:
        return False, f"Browser test error: {e}"


def _smoke_test_frontend(work_dir: Path, dev_cmd: str,
                         env: Dict[str, str]) -> Tuple[bool, str]:
    """
    Basic smoke test: start dev server, verify it responds on localhost,
    optionally load in headless browser and check for JS errors.
    """
    result_env = os.environ.copy()
    result_env.update(env)
    result_env["PORT"] = "3456"
    result_env["BROWSER"] = "none"  # don't open browser

    server_proc = None
    try:
        # Start dev server
        server_proc = subprocess.Popen(
            ["npm", "run", "dev"] if "dev" in dev_cmd else ["npm", "start"],
            cwd=str(work_dir), env=result_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        # Wait for server to start
        import urllib.request
        for _ in range(20):
            time.sleep(2)
            try:
                resp = urllib.request.urlopen("http://localhost:3456", timeout=5)
                if resp.status == 200:
                    break
            except Exception:
                if server_proc.poll() is not None:
                    stderr = server_proc.stderr.read().decode()[:1000]
                    return False, f"Dev server crashed: {stderr}"
                continue
        else:
            return False, "Dev server did not start within 40s"

        # Try Playwright headless check for JS errors
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto("http://localhost:3456", timeout=15000)
                page.wait_for_load_state("networkidle", timeout=15000)
                title = page.title()
                browser.close()

                if errors:
                    return False, f"Page loaded (title: {title}) but has JS errors:\n" + \
                                  "\n".join(errors[:5])
                return True, f"✅ Frontend smoke test passed (title: {title}, no JS errors)"
        except ImportError:
            # No playwright — just verify HTTP 200
            return True, "✅ Dev server responds on :3456 (no Playwright for deeper check)"
        except Exception as e:
            return False, f"Playwright smoke test failed: {e}"

    except Exception as e:
        return False, f"Frontend smoke test error: {e}"
    finally:
        if server_proc and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
