"""
CodeForge Benchmark — Tests whether a model/node is good enough to contribute code.

Before ANY node can participate in swarm code generation, it must pass a coding
benchmark. This prevents weak models from wasting time generating bad code.

Benchmark tasks are simple but targeted:
1. Implement a function from a docstring (can it follow specs?)
2. Fix a buggy function (can it reason about errors?)
3. Write a function with edge cases (does it handle boundaries?)
4. Complete a partial implementation (can it understand context?)

Each task is validated by actually running the code against expected outputs.
Score = tasks_passed / tasks_attempted * 100. Minimum 60 to contribute.
"""

import json
import time
import logging
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from dataclasses import asdict

from .models import BenchmarkResult

logger = logging.getLogger("codeforge.benchmark")

# ── Benchmark Tasks ──
# Each task has: prompt (what the LLM sees), test_code (validation), expected result

BENCHMARK_TASKS: List[Dict] = [
    {
        "id": "fn_from_spec",
        "name": "Implement from specification",
        "prompt": (
            "Write a Python function called `merge_sorted(a, b)` that takes two "
            "sorted lists of integers and returns a single sorted list containing "
            "all elements from both lists. Do not use the built-in sorted() function. "
            "Time complexity should be O(n+m). Reply with ONLY the function code, "
            "no explanation."
        ),
        "test_code": """
assert merge_sorted([], []) == []
assert merge_sorted([1, 3, 5], [2, 4, 6]) == [1, 2, 3, 4, 5, 6]
assert merge_sorted([1], [2, 3, 4]) == [1, 2, 3, 4]
assert merge_sorted([1, 1, 1], [1, 1]) == [1, 1, 1, 1, 1]
assert merge_sorted([-5, 0, 3], [-3, 1, 4]) == [-5, -3, 0, 1, 3, 4]
assert merge_sorted([100], []) == [100]
print("PASS")
""",
    },
    {
        "id": "fix_bug",
        "name": "Fix a buggy function",
        "prompt": (
            "This Python function has a bug. Fix it so all test cases pass. "
            "Reply with ONLY the corrected function code.\n\n"
            "```python\n"
            "def flatten(nested):\n"
            "    result = []\n"
            "    for item in nested:\n"
            "        if isinstance(item, list):\n"
            "            result.append(flatten(item))\n"
            "        else:\n"
            "            result.append(item)\n"
            "    return result\n"
            "```\n\n"
            "Expected: flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5]"
        ),
        "test_code": """
assert flatten([]) == []
assert flatten([1, 2, 3]) == [1, 2, 3]
assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5]
assert flatten([[[[1]]]]) == [1]
assert flatten([1, [2], [3, [4, [5]]]]) == [1, 2, 3, 4, 5]
print("PASS")
""",
    },
    {
        "id": "edge_cases",
        "name": "Handle edge cases correctly",
        "prompt": (
            "Write a Python function `safe_divide(a, b, default=0)` that:\n"
            "- Returns a / b as a float\n"
            "- Returns `default` if b is 0\n"
            "- Returns `default` if either argument is not a number (int or float)\n"
            "- Handles negative numbers correctly\n"
            "- Handles very large numbers without crashing\n"
            "Reply with ONLY the function code."
        ),
        "test_code": """
assert safe_divide(10, 2) == 5.0
assert safe_divide(10, 0) == 0
assert safe_divide(10, 0, -1) == -1
assert safe_divide("a", 2) == 0
assert safe_divide(10, "b") == 0
assert safe_divide(None, 2) == 0
assert safe_divide(-10, 2) == -5.0
assert safe_divide(10**100, 10**50) == 10**50
assert safe_divide(1, 3) - 0.333333 < 0.001
print("PASS")
""",
    },
    {
        "id": "complete_impl",
        "name": "Complete partial implementation",
        "prompt": (
            "Complete this Python class. The `add`, `remove`, and `__contains__` "
            "methods need implementations. The class is a simple set using a list "
            "internally (no using Python's built-in set). Reply with the COMPLETE "
            "class code.\n\n"
            "```python\n"
            "class SimpleSet:\n"
            "    def __init__(self):\n"
            "        self._items = []\n"
            "    \n"
            "    def add(self, item):\n"
            "        \"\"\"Add item if not already present.\"\"\"\n"
            "        pass\n"
            "    \n"
            "    def remove(self, item):\n"
            "        \"\"\"Remove item. Raise KeyError if not present.\"\"\"\n"
            "        pass\n"
            "    \n"
            "    def __contains__(self, item):\n"
            "        \"\"\"Check if item is in the set.\"\"\"\n"
            "        pass\n"
            "    \n"
            "    def __len__(self):\n"
            "        return len(self._items)\n"
            "```"
        ),
        "test_code": """
s = SimpleSet()
assert len(s) == 0
s.add(1)
assert 1 in s
assert len(s) == 1
s.add(1)  # duplicate
assert len(s) == 1
s.add(2)
assert 2 in s
assert len(s) == 2
s.remove(1)
assert 1 not in s
assert len(s) == 1
try:
    s.remove(99)
    assert False, "Should have raised KeyError"
except KeyError:
    pass
print("PASS")
""",
    },
    {
        "id": "data_transform",
        "name": "Data transformation",
        "prompt": (
            "Write a Python function `group_by(items, key_fn)` that takes a list of "
            "items and a function that extracts a key from each item, and returns "
            "a dictionary mapping keys to lists of items with that key. "
            "Preserve insertion order within each group. "
            "Reply with ONLY the function code."
        ),
        "test_code": """
result = group_by([1, 2, 3, 4, 5, 6], lambda x: x % 2)
assert result == {1: [1, 3, 5], 0: [2, 4, 6]}

result = group_by(["apple", "banana", "avocado", "blueberry"], lambda x: x[0])
assert result == {"a": ["apple", "avocado"], "b": ["banana", "blueberry"]}

result = group_by([], lambda x: x)
assert result == {}

result = group_by([{"name": "alice", "age": 30}, {"name": "bob", "age": 30}, {"name": "charlie", "age": 25}], lambda x: x["age"])
assert len(result[30]) == 2
assert len(result[25]) == 1
print("PASS")
""",
    },
]


def _extract_code(response: str) -> str:
    """Extract code from an LLM response (strip markdown fences)."""
    import re
    # Try python code block
    match = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Try generic code block
    match = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Return as-is
    return response.strip()


def _run_test(code: str, test_code: str, timeout: int = 30) -> Tuple[bool, str]:
    """
    Run generated code + test assertions in an isolated subprocess.
    Returns (passed, output).
    """
    full_code = code + "\n\n" + test_code

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(full_code)
        f.flush()
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True, text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0 and "PASS" in result.stdout
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    except Exception as e:
        return False, str(e)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def run_benchmark(call_llm_fn, node_id: str = "",
                  model_name: str = "", provider: str = "") -> BenchmarkResult:
    """
    Run the full benchmark suite against an LLM.

    Args:
        call_llm_fn: Callable(prompt: str) -> Optional[str]
            A function that sends a prompt to the LLM and returns the response.
        node_id: Identifier for the node being benchmarked.
        model_name: Name of the model being tested.
        provider: Provider name (nvidia, local, etc.)

    Returns:
        BenchmarkResult with score and details.
    """
    result = BenchmarkResult(
        node_id=node_id,
        model_name=model_name,
        provider=provider,
    )

    times = []
    for task in BENCHMARK_TASKS:
        result.tasks_attempted += 1
        start = time.time()

        try:
            response = call_llm_fn(task["prompt"])
            elapsed = time.time() - start
            times.append(elapsed)

            if not response:
                logger.warning(f"Benchmark {task['id']}: No response from LLM")
                continue

            code = _extract_code(response)
            passed, output = _run_test(code, task["test_code"])

            if passed:
                result.tasks_passed += 1
                logger.info(f"  ✅ Benchmark {task['id']}: PASS ({elapsed:.1f}s)")
            else:
                logger.info(f"  ❌ Benchmark {task['id']}: FAIL — {output[:200]}")

        except Exception as e:
            logger.error(f"  ⚠️ Benchmark {task['id']}: Error — {e}")

    # Calculate scores
    if result.tasks_attempted > 0:
        result.score = round(
            (result.tasks_passed / result.tasks_attempted) * 100, 1
        )
    result.avg_response_time = (
        round(sum(times) / len(times), 2) if times else 0.0
    )
    result.language_scores["python"] = result.score
    result.tested_at = time.time()
    result.expires_at = result.tested_at + 86400  # 24h validity

    logger.info(
        f"🏁 Benchmark complete: {model_name} on {node_id} — "
        f"Score: {result.score}/100 ({result.tasks_passed}/{result.tasks_attempted}) "
        f"{'✅ PASSED' if result.passed else '❌ FAILED'}"
    )
    return result


# ── Benchmark cache ──
_benchmark_cache: Dict[str, BenchmarkResult] = {}
_cache_file = Path.home() / ".repryntt" / "workspace" / "projects" / "codeforge" / "benchmarks.json"


def get_cached_benchmark(node_id: str) -> Optional[BenchmarkResult]:
    """Get a cached benchmark result if still valid."""
    _load_cache()
    result = _benchmark_cache.get(node_id)
    if result and result.is_valid:
        return result
    return None


def save_benchmark(result: BenchmarkResult):
    """Save a benchmark result to cache."""
    _benchmark_cache[result.node_id] = result
    _save_cache()


def _load_cache():
    if _cache_file.exists() and not _benchmark_cache:
        try:
            data = json.loads(_cache_file.read_text())
            for node_id, d in data.items():
                _benchmark_cache[node_id] = BenchmarkResult.from_dict(d)
        except Exception:
            pass


def _save_cache():
    try:
        _cache_file.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in _benchmark_cache.items()}
        _cache_file.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Failed to save benchmark cache: {e}")
