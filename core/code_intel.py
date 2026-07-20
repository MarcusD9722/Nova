from __future__ import annotations

"""Continuous codebase understanding (Goal #10, Phase 7).

A deterministic indexer over a project directory: it parses Python with `ast`
(classes, functions, imports, docstrings, TODOs) and falls back to lightweight
regex for other languages, then aggregates a structural picture — symbols,
imports, TODOs, per-file stats. On top of the index (see also the SW-eng reports
in the same module) it answers "what references this symbol" for impact analysis
before an edit.

Everything here is pure and deterministic (no LLM, no network), so it is fully
testable and safe to run over any registered project.
"""

import ast
import re
from pathlib import Path
from typing import Any

# Directories that are never source worth indexing.
_SKIP_DIRS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv", "env", "dist", "build",
    ".next", ".nuxt", "target", "out", ".idea", ".vscode", "coverage", "site-packages",
    ".mypy_cache", ".pytest_cache", "$recycle.bin", "system volume information",
})

_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby", ".cs": "csharp",
}

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)", re.IGNORECASE)
# Generic (non-python) declarations — deliberately loose; best-effort.
_JS_FUNC_RE = re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(|([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{)")
_JS_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")
_JS_IMPORT_RE = re.compile(r"""(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\))""")


def language_of(path: Path) -> str | None:
    return _LANG_BY_EXT.get(path.suffix.lower())


def _todos(source: str) -> list[dict[str, Any]]:
    out = []
    for i, line in enumerate(source.splitlines(), 1):
        m = _TODO_RE.search(line)
        if m:
            out.append({"line": i, "tag": m.group(1).upper(), "text": m.group(2).strip()[:160]})
    return out


def index_python(source: str) -> dict[str, Any]:
    """AST-based structure for one Python file. Never raises on bad syntax —
    returns a syntax_error flag instead (that's itself a useful signal)."""
    info: dict[str, Any] = {
        "classes": [], "functions": [], "imports": [], "todos": _todos(source),
        "loc": len(source.splitlines()), "has_module_docstring": False, "syntax_error": False,
    }
    try:
        tree = ast.parse(source)
    except SyntaxError:
        info["syntax_error"] = True
        return info

    info["has_module_docstring"] = ast.get_docstring(tree) is not None
    for node in tree.body:  # top-level only for classes/functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["functions"].append({
                "name": node.name, "line": node.lineno,
                "args": [a.arg for a in node.args.args],
                "documented": ast.get_docstring(node) is not None,
                "public": not node.name.startswith("_"),
            })
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            info["classes"].append({
                "name": node.name, "line": node.lineno, "methods": methods,
                "documented": ast.get_docstring(node) is not None,
                "public": not node.name.startswith("_"),
            })
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                info["imports"].append(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            info["imports"].append(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    info["imports"] = sorted(set(info["imports"]))
    info["used_names"] = used
    return info


def index_generic(source: str, lang: str) -> dict[str, Any]:
    funcs = []
    for m in _JS_FUNC_RE.finditer(source):
        name = m.group(1) or m.group(2) or m.group(3)
        if name and name not in ("if", "for", "while", "switch", "catch", "function", "return"):
            funcs.append({"name": name, "line": source[:m.start()].count("\n") + 1, "public": not name.startswith("_")})
    classes = [{"name": m.group(1), "line": source[:m.start()].count("\n") + 1, "public": True}
               for m in _JS_CLASS_RE.finditer(source)]
    imports = sorted({(m.group(1) or m.group(2)) for m in _JS_IMPORT_RE.finditer(source) if (m.group(1) or m.group(2))})
    used = set(re.findall(r"[A-Za-z_$][\w$]*", source))
    return {"classes": classes, "functions": funcs, "imports": imports, "todos": _todos(source),
            "loc": len(source.splitlines()), "has_module_docstring": False, "syntax_error": False,
            "used_names": used}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def iter_source_files(root: Path, *, max_files: int = 600):
    count = 0
    for p in sorted(root.rglob("*")):
        if count >= max_files:
            return
        if not p.is_file():
            continue
        if any(part.lower() in _SKIP_DIRS for part in p.parts):
            continue
        if language_of(p) is None:
            continue
        count += 1
        yield p


def index_project(root: str | Path, *, max_files: int = 600) -> dict[str, Any]:
    """Structural index of a project directory."""
    root = Path(root)
    files: list[dict[str, Any]] = []
    symbols: dict[str, list[str]] = {}
    import_counts: dict[str, int] = {}
    all_todos: list[dict[str, Any]] = []
    langs: dict[str, int] = {}
    total_loc = 0

    if not root.exists() or not root.is_dir():
        return {"root": str(root), "exists": False, "files": [], "stats": {}, "symbols": {}, "todos": []}

    for path in iter_source_files(root, max_files=max_files):
        lang = language_of(path)
        src = _read(path)
        info = index_python(src) if lang == "python" else index_generic(src, lang or "")
        rel = str(path.relative_to(root)).replace("\\", "/")
        langs[lang] = langs.get(lang, 0) + 1
        total_loc += info["loc"]
        for sym in [c["name"] for c in info["classes"]] + [f["name"] for f in info["functions"]]:
            symbols.setdefault(sym, [])
            if rel not in symbols[sym]:
                symbols[sym].append(rel)
        for imp in info["imports"]:
            import_counts[imp] = import_counts.get(imp, 0) + 1
        for t in info["todos"]:
            all_todos.append({"file": rel, **t})
        files.append({
            "path": rel, "lang": lang, "loc": info["loc"],
            "classes": info["classes"], "functions": info["functions"],
            "imports": info["imports"], "todo_count": len(info["todos"]),
            "has_module_docstring": info["has_module_docstring"], "syntax_error": info["syntax_error"],
            "_used": info.get("used_names") or set(),
        })

    # Resolve cross-file references: keep only the project-defined symbols each
    # file actually uses (bounded + directly useful for impact analysis).
    all_symbols = set(symbols)
    for f in files:
        own = {c["name"] for c in f["classes"]} | {fn["name"] for fn in f["functions"]}
        f["refs"] = sorted((f.pop("_used") & all_symbols) - own)

    n_class = sum(len(f["classes"]) for f in files)
    n_func = sum(len(f["functions"]) for f in files)
    test_files = [f["path"] for f in files if _is_test(f["path"])]
    return {
        "root": str(root), "exists": True,
        "files": files,
        "symbols": symbols,
        "todos": all_todos,
        "top_imports": sorted(import_counts.items(), key=lambda x: -x[1])[:20],
        "stats": {
            "files": len(files), "loc": total_loc, "classes": n_class, "functions": n_func,
            "todos": len(all_todos), "languages": langs, "test_files": len(test_files),
            "syntax_errors": sum(1 for f in files if f["syntax_error"]),
        },
    }


def _is_test(rel_path: str) -> bool:
    p = rel_path.lower()
    return "test" in Path(p).name or "/tests/" in ("/" + p) or p.startswith("tests/")


def impact_of(index: dict[str, Any], name: str) -> dict[str, Any]:
    """Impact analysis: which files define `name`, and which reference it (import
    the defining module or mention the symbol). A pre-edit 'what might break'."""
    name = (name or "").strip()
    if not name:
        return {"symbol": name, "defined_in": [], "referenced_in": []}
    defined_in = index.get("symbols", {}).get(name, [])
    def_modules = {Path(p).stem for p in defined_in}
    direct: list[dict[str, Any]] = []       # actually use the symbol — the real blast radius
    module_importers: list[str] = []        # import the module but not this symbol — softer coupling
    for f in index.get("files", []):
        if f["path"] in defined_in:
            continue
        if name in f.get("refs", []):
            direct.append({"file": f["path"], "reasons": ["uses the symbol"]})
        elif def_modules & set(f["imports"]):
            module_importers.append(f["path"])
    return {
        "symbol": name, "defined_in": defined_in,
        "referenced_in": direct,
        "also_import_module": module_importers,
        "impact": "none" if not defined_in else "isolated" if not direct
                  else "low" if len(direct) <= 3 else "high",
    }


# ── SW-engineering reports (Goal #18) — deterministic, over the index ─────────

def _public_doc_coverage(index: dict[str, Any]) -> tuple[int, int]:
    documented = total = 0
    for f in index.get("files", []):
        if f["lang"] != "python":
            continue
        for sym in f["classes"] + f["functions"]:
            if sym.get("public"):
                total += 1
                if sym.get("documented"):
                    documented += 1
    return documented, total


def health_score(index: dict[str, Any]) -> dict[str, Any]:
    """A 0-100 project-health score from real index signals, with the factors
    that drove it. Deterministic — the same tree always scores the same."""
    files = index.get("files", [])
    stats = index.get("stats", {})
    if not files:
        return {"score": 0, "grade": "n/a", "factors": {}, "note": "empty or unreadable project"}

    n = len(files)
    documented, total_pub = _public_doc_coverage(index)
    doc_cov = (documented / total_pub) if total_pub else 1.0
    py = [f for f in files if f["lang"] == "python"]
    mod_doc_cov = (sum(1 for f in py if f["has_module_docstring"]) / len(py)) if py else 1.0
    long_files = sum(1 for f in files if f["loc"] > 400)
    long_ratio = long_files / n
    todo_density = stats.get("todos", 0) / n
    has_tests = stats.get("test_files", 0) > 0
    syntax_errors = stats.get("syntax_errors", 0)

    # Start at 100, subtract for each weakness (clamped contributions).
    score = 100.0
    score -= (1 - doc_cov) * 25          # up to -25 for undocumented public API
    score -= (1 - mod_doc_cov) * 10      # up to -10 for missing module docs
    score -= min(1.0, long_ratio * 2) * 15   # up to -15 for many long files
    score -= min(1.0, todo_density / 2) * 10  # up to -10 for TODO density
    score -= 0 if has_tests else 15      # -15 for no tests at all
    score -= min(syntax_errors, 5) * 3   # -3 per syntax error (cap -15)
    score = max(0, round(score))
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    return {
        "score": score, "grade": grade,
        "factors": {
            "public_doc_coverage": round(doc_cov, 2),
            "module_doc_coverage": round(mod_doc_cov, 2),
            "long_files": long_files,
            "todo_density_per_file": round(todo_density, 2),
            "has_tests": has_tests,
            "syntax_errors": syntax_errors,
        },
    }


def tech_debt(index: dict[str, Any]) -> dict[str, Any]:
    """Ranked technical-debt items from index signals."""
    items: list[dict[str, Any]] = []
    for f in index.get("files", []):
        if f["syntax_error"]:
            items.append({"severity": "high", "kind": "syntax_error", "file": f["path"],
                          "detail": "file fails to parse"})
        if f["loc"] > 600:
            items.append({"severity": "high", "kind": "very_long_file", "file": f["path"],
                          "detail": f"{f['loc']} lines — consider splitting"})
        elif f["loc"] > 400:
            items.append({"severity": "medium", "kind": "long_file", "file": f["path"],
                          "detail": f"{f['loc']} lines"})
        if f["todo_count"] >= 3:
            items.append({"severity": "low", "kind": "many_todos", "file": f["path"],
                          "detail": f"{f['todo_count']} TODO/FIXME markers"})
        undoc = [s["name"] for s in f["classes"] + f["functions"] if s.get("public") and not s.get("documented")]
        if f["lang"] == "python" and len(undoc) >= 4:
            items.append({"severity": "low", "kind": "undocumented_api", "file": f["path"],
                          "detail": f"{len(undoc)} undocumented public symbols"})
    if index.get("stats", {}).get("test_files", 0) == 0 and index.get("files"):
        items.append({"severity": "medium", "kind": "no_tests", "file": "(project)",
                      "detail": "no test files found"})
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: order.get(x["severity"], 3))
    return {"items": items, "count": len(items),
            "by_severity": {s: sum(1 for i in items if i["severity"] == s) for s in ("high", "medium", "low")}}


def architecture_summary(index: dict[str, Any]) -> dict[str, Any]:
    """A structural outline: languages, top directories, largest files, key
    dependencies, and likely entry points."""
    files = index.get("files", [])
    dirs: dict[str, int] = {}
    for f in files:
        top = f["path"].split("/", 1)[0] if "/" in f["path"] else "(root)"
        dirs[top] = dirs.get(top, 0) + 1
    largest = sorted(files, key=lambda f: -f["loc"])[:8]
    entry_names = ("main", "app", "index", "__main__", "server", "cli")
    entry_points = [f["path"] for f in files if Path(f["path"]).stem in entry_names]
    return {
        "stats": index.get("stats", {}),
        "top_level_dirs": sorted(dirs.items(), key=lambda x: -x[1]),
        "largest_files": [{"path": f["path"], "loc": f["loc"]} for f in largest],
        "key_dependencies": index.get("top_imports", []),
        "entry_points": entry_points,
    }


# Defensive security scan (the user's OWN registered code): flags patterns worth
# a human review. It reports possibilities, never claims proof of a vulnerability.
_SECURITY_PATTERNS = [
    (re.compile(r"\beval\s*\("), "high", "eval() on dynamic input can execute arbitrary code"),
    (re.compile(r"\bexec\s*\("), "high", "exec() can execute arbitrary code"),
    (re.compile(r"shell\s*=\s*True"), "high", "subprocess with shell=True risks command injection"),
    (re.compile(r"\bos\.system\s*\("), "high", "os.system() risks command injection"),
    (re.compile(r"pickle\.loads?\s*\("), "medium", "pickle on untrusted data can execute code"),
    (re.compile(r"yaml\.load\s*\((?!.*Loader)"), "medium", "yaml.load without SafeLoader can construct objects"),
    (re.compile(r"verify\s*=\s*False"), "medium", "TLS verification disabled"),
    (re.compile(r"""(?i)(password|secret|api_key|apikey|token)\s*=\s*['"][^'"]{6,}['"]"""), "high",
     "possible hardcoded credential"),
    (re.compile(r"(?i)hashlib\.(md5|sha1)\s*\("), "low", "weak hash (md5/sha1) — not for passwords"),
]


def security_scan(root: str | Path, *, max_files: int = 600) -> dict[str, Any]:
    root = Path(root)
    findings: list[dict[str, Any]] = []
    if not root.exists():
        return {"findings": [], "count": 0, "note": "path not found"}
    for path in iter_source_files(root, max_files=max_files):
        src = _read(path)
        rel = str(path.relative_to(root)).replace("\\", "/")
        for i, line in enumerate(src.splitlines(), 1):
            for rx, sev, desc in _SECURITY_PATTERNS:
                if rx.search(line):
                    findings.append({"file": rel, "line": i, "severity": sev, "note": desc,
                                     "snippet": line.strip()[:120]})
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: order.get(x["severity"], 3))
    return {
        "findings": findings, "count": len(findings),
        "by_severity": {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")},
        "disclaimer": "Heuristic pattern matches for human review — not confirmed vulnerabilities.",
    }
