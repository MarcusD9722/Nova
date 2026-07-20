"""Phase 7 / #10 + #18: codebase understanding + SW-eng reports.

Deterministic indexer over a synthetic project: structure, symbols, impact
analysis, health score, tech-debt, architecture, and a defensive security scan.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.code_intel import (
    architecture_summary,
    health_score,
    impact_of,
    index_project,
    security_scan,
    tech_debt,
)

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def build_project(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        '"""App entry point."""\n'
        "from helper import compute\n\n"
        "def run():\n"
        '    """Run the app."""\n'
        "    return compute() + 1\n\n"
        "def helper_call():\n"          # public, undocumented
        "    return compute()\n",
        encoding="utf-8",
    )
    (root / "helper.py").write_text(
        '"""Helper utilities."""\n\n'
        "class Helper:\n"
        '    """A helper."""\n'
        "    def method_a(self):\n"
        "        return 1\n\n"
        "def compute():\n"
        '    """Compute a value."""\n'
        "    return 41\n",
        encoding="utf-8",
    )
    (root / "risky.py").write_text(
        "password = \"supersecret123\"\n"
        "def danger(user_input):\n"
        "    return eval(user_input)\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def oops(:\n    pass\n", encoding="utf-8")  # syntax error
    (root / "big.py").write_text("\n".join("x = 1" for _ in range(650)), encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
    (root / "web.js").write_text("import x from 'y';\nfunction widget() { return 1; }\n", encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td) / "proj"
        build_project(root)
        idx = index_project(root)

        # ── structure ──
        st = idx["stats"]
        check(st["files"] == 7, f"all source files indexed (got {st['files']})")
        check("python" in st["languages"] and "javascript" in st["languages"], "multiple languages detected")
        check(st["test_files"] == 1, f"test file detected (got {st['test_files']})")
        check(st["syntax_errors"] == 1, f"syntax error flagged (got {st['syntax_errors']})")
        check("Helper" in idx["symbols"] and "compute" in idx["symbols"], "classes + functions indexed as symbols")
        check("widget" in idx["symbols"], "generic (JS) function indexed too")

        # ── impact analysis ──
        imp = impact_of(idx, "compute")
        check("helper.py" in imp["defined_in"], "impact: symbol's defining file found")
        ref_files = {r["file"] for r in imp["referenced_in"]}
        check("app.py" in ref_files, f"impact: app.py flagged as a user of compute (got {ref_files})")
        check(imp["impact"] in ("low", "high"), "impact level classified")
        check(impact_of(idx, "Helper")["impact"] == "isolated", "an unused symbol is isolated (safe to change)")

        # ── health ──
        h = health_score(idx)
        check(0 <= h["score"] <= 100 and h["grade"] in "ABCDF", f"health score + grade (got {h['score']}/{h['grade']})")
        check(h["factors"]["public_doc_coverage"] < 1.0, "undocumented public symbol lowers doc coverage")
        check(h["factors"]["has_tests"] is True, "test presence recorded")
        check(h["factors"]["syntax_errors"] == 1, "syntax error reflected in health factors")

        # ── tech debt ──
        debt = tech_debt(idx)
        kinds = {i["kind"] for i in debt["items"]}
        check("very_long_file" in kinds, "650-line file flagged as very long")
        check("syntax_error" in kinds, "syntax error flagged as debt")
        check(debt["by_severity"]["high"] >= 2, "high-severity debt counted")

        # ── architecture ──
        arch = architecture_summary(idx)
        check(any(Path(f["path"]).name == "app.py" for f in arch["largest_files"]) or arch["entry_points"],
              "architecture summary produced")
        check("app.py" in arch["entry_points"], "entry point detected")

        # ── security scan (defensive, on the project's own code) ──
        scan = security_scan(root)
        notes = " ".join(f["note"] for f in scan["findings"])
        check(any(f["file"] == "risky.py" and "eval" in f["note"] for f in scan["findings"]), "eval() flagged")
        check("credential" in notes, "hardcoded credential flagged")
        check(scan["by_severity"]["high"] >= 2, "high-severity security findings counted")
        check("not confirmed" in scan["disclaimer"], "scan is honest: heuristic, not proof")

        # ── missing project is honest ──
        check(index_project(root / "nope")["exists"] is False, "missing project reported honestly")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
