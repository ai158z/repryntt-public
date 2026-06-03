"""
CodeForge Validator — Syntax checking, security scanning, quality scoring.

Runs entirely locally — no API calls needed. Operates on generated source code
to catch issues before packaging.
"""

import ast
import re
import logging
from typing import List, Tuple
from .models import ForgeModule, QualityReport

logger = logging.getLogger("codeforge.validator")

# ── Security Patterns (things that should never appear in generated code) ──

SECURITY_PATTERNS: List[Tuple[str, str]] = [
    (r'\beval\s*\(', "eval() — arbitrary code execution risk"),
    (r'\bexec\s*\(', "exec() — arbitrary code execution risk"),
    (r'\b__import__\s*\(', "__import__() — dynamic import injection"),
    (r'\bos\.system\s*\(', "os.system() — use subprocess instead"),
    (r'\bpickle\.loads?\s*\(', "pickle.load() — deserialization vulnerability"),
    (r'\byaml\.load\s*\([^)]*\)\s*$', "yaml.load() without SafeLoader"),
    (r'subprocess\..*shell\s*=\s*True', "subprocess with shell=True — command injection risk"),
    (r'password\s*=\s*["\'][^"\']+["\']', "hardcoded password"),
    (r'api_key\s*=\s*["\'][A-Za-z0-9]{16,}["\']', "hardcoded API key"),
    (r'secret\s*=\s*["\'][^"\']+["\']', "hardcoded secret"),
    (r'\bSECRET_KEY\s*=\s*["\'][^"\']+["\']', "hardcoded SECRET_KEY"),
    (r'SELECT\s+.*\+\s*["\']?\s*\+', "SQL string concatenation — use parameterized queries"),
    (r'innerHTML\s*=', "innerHTML assignment — XSS risk"),
    (r'dangerouslySetInnerHTML', "React dangerouslySetInnerHTML — XSS risk"),
    (r'\bchmod\s+777\b', "chmod 777 — overly permissive"),
    (r'verify\s*=\s*False', "SSL verification disabled"),
]

# ── Complexity heuristics ──

def _count_complexity(code: str) -> float:
    """Rough cyclomatic complexity estimate from indentation depth and branching."""
    score = 0.0
    lines = code.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Count branching keywords
        for keyword in ("if ", "elif ", "for ", "while ", "except ", "case "):
            if stripped.startswith(keyword):
                score += 1
        # Nested depth penalty
        indent = len(line) - len(line.lstrip())
        if indent > 16:
            score += 0.5  # deeply nested
    # Normalize: 0 = trivial, 100 = absurdly complex
    normalized = min(100.0, (score / max(len(lines), 1)) * 200)
    return round(normalized, 1)


def check_python_syntax(code: str) -> Tuple[bool, str]:
    """Validate Python syntax via AST parse. Returns (valid, error_msg)."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"Line {e.lineno}: {e.msg}"


def check_javascript_syntax(code: str) -> Tuple[bool, str]:
    """Basic JS syntax validation via bracket/brace matching."""
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    in_string = None
    escaped = False

    for i, ch in enumerate(code):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in ('"', "'", "`"):
            if in_string == ch:
                in_string = None
            elif in_string is None:
                in_string = ch
            continue
        if in_string:
            continue
        if ch in ("(", "[", "{"):
            stack.append((ch, i))
        elif ch in pairs:
            if not stack or stack[-1][0] != pairs[ch]:
                return False, f"Char {i}: unmatched '{ch}'"
            stack.pop()

    if stack:
        return False, f"Char {stack[-1][1]}: unclosed '{stack[-1][0]}'"
    return True, ""


def check_syntax(code: str, language: str = "python") -> Tuple[bool, str]:
    """Route to language-specific syntax checker."""
    if language == "python":
        return check_python_syntax(code)
    elif language in ("javascript", "typescript", "js", "ts"):
        return check_javascript_syntax(code)
    elif language in ("go", "rust", "java"):
        # For compiled languages, we do bracket matching as a basic check
        return check_javascript_syntax(code)  # bracket matching is universal
    elif language in ("dockerfile", "yaml", "env", "makefile", "json",
                      "toml", "gitignore", "markdown", "nginx"):
        # Config files — just check non-empty
        if not code.strip():
            return False, "Empty file"
        # JSON gets a parse check
        if language == "json":
            import json
            try:
                json.loads(code)
            except Exception as e:
                return False, f"Invalid JSON: {e}"
        return True, ""
    # For other languages, at least check non-empty
    if not code.strip():
        return False, "Empty code"
    return True, ""


def scan_security(code: str) -> List[str]:
    """Scan code for security anti-patterns. Returns list of issues found."""
    issues = []
    for pattern, desc in SECURITY_PATTERNS:
        matches = re.findall(pattern, code, re.MULTILINE | re.IGNORECASE)
        if matches:
            issues.append(desc)
    return issues


def validate_module(module: ForgeModule) -> Tuple[bool, List[str]]:
    """
    Full validation of a single module: syntax + security.
    Returns (passed, list_of_issues).
    """
    issues = []

    if not module.implementation.strip():
        return False, ["Empty implementation"]

    # Syntax check
    valid, err = check_syntax(module.implementation, module.language)
    if not valid:
        issues.append(f"Syntax error: {err}")

    # Security scan
    sec_issues = scan_security(module.implementation)
    issues.extend(sec_issues)

    return len(issues) == 0, issues


def build_quality_report(modules: List[ForgeModule]) -> QualityReport:
    """Build a comprehensive quality report for all project modules."""
    report = QualityReport()
    report.total_files = len(modules)

    all_syntax_clean = True
    total_lines = 0

    for module in modules:
        # Count lines
        lines = len(module.implementation.split("\n")) if module.implementation else 0
        total_lines += lines
        if module.test_code:
            total_lines += len(module.test_code.split("\n"))

        # Syntax
        valid, err = check_syntax(module.implementation, module.language)
        if not valid:
            all_syntax_clean = False
            report.warnings.append(f"{module.filename}: {err}")

        # Security
        sec_issues = scan_security(module.implementation)
        report.security_issues.extend(
            f"{module.filename}: {issue}" for issue in sec_issues
        )

        # Tests
        if module.status in ("passed",):
            report.tests_passed += 1
        elif module.status in ("failed", "fix_retry"):
            report.tests_failed += 1
        if module.test_code:
            report.tests_total += 1

    report.total_lines = total_lines
    report.syntax_clean = all_syntax_clean
    report.all_tests_pass = report.tests_failed == 0 and report.tests_passed > 0

    # Complexity — average across modules
    complexities = [_count_complexity(m.implementation) for m in modules
                    if m.implementation]
    report.complexity_score = (
        round(sum(complexities) / len(complexities), 1) if complexities else 0.0
    )

    # Overall score: weighted composite
    score = 0.0
    if report.syntax_clean:
        score += 30
    if report.all_tests_pass:
        score += 35
    if not report.security_issues:
        score += 20
    # Simplicity bonus (lower complexity = higher score)
    simplicity = max(0, 15 - report.complexity_score * 0.15)
    score += simplicity

    report.overall_score = round(min(100.0, score), 1)
    return report
