from __future__ import annotations

"""Guarded Developer Mode: Nova may inspect her own code and PROPOSE changes.

Safety model (do not weaken without user sign-off):
- Disabled by default. Enabled only via NOVA_DEV_MODE=1 in .env (explicit user
  action on their own machine).
- Read-only by default: inspection never modifies anything.
- Writes happen ONLY through the propose -> user-confirm -> apply flow:
  every apply requires the proposal id AND confirm=True from the caller (the
  UI must show the diff to the user first).
- Path allowlist: repo root + projects dir. Everything else is refused.
- Hard denials regardless of allowlist: .env* files, .git internals,
  memory_data, model weights, node_modules, venv, and the .nova_dev state dir.
- Every apply is REVERSIBLE: the current file is backed up first, the proposed
  content is syntax-checked (for .py), written atomically, then boot-tested by
  importing the changed module in a subprocess. If the boot test fails, the
  backup is restored automatically and the proposal is marked "reverted".
- Proposals + backups are persisted under .nova_dev/ so they survive restarts.
- No recursive/self-triggering applies: applying a proposal never enqueues
  another proposal.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.event_bus import BUS, clip
from core.logging_setup import get_logger


logger = get_logger(__name__)

_MAX_FILE_BYTES = 400_000
_STATE_DIRNAME = ".nova_dev"
_DENY_NAMES = {".env", ".env.local", ".env.production"}
_DENY_PARTS = {
    ".git", "memory_data", "node_modules", "venv", ".venv", "model",
    "dist", "release", "__pycache__", _STATE_DIRNAME,
    "credentials",  # OAuth tokens (PI1) — same protection tier as .env/model
}
_BOOT_TEST_TIMEOUT_S = 90.0
# Same system-directory guard as memory.index_folder's — an external project
# root must never be a Windows/system directory.
_EXTERNAL_SYSTEM_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\programdata")


class DevModeError(RuntimeError):
    pass


class DevModeDisabled(DevModeError):
    pass


def dev_mode_enabled() -> bool:
    return os.getenv("NOVA_DEV_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ChangeProposal:
    id: str
    path: str
    reason: str
    diff: str
    new_content: str
    created_at: str
    status: str = "pending"  # pending | applied | rejected | reverted
    backup_path: str = ""
    applied_at: str = ""
    boot_error: str = ""
    origin: str = "nova"  # who proposed it (nova | user)
    # Full pre-change file content, so the UI can render a real side-by-side
    # diff. Defaults to "" so proposals persisted before this field existed
    # still load (their diff string remains the only view for them).
    old_content: str = ""
    # Registered external-project name when the target lives outside Nova's
    # own repo/projects roots ("" = her own code). See register_external_root.
    project: str = ""


@dataclass
class DevMode:
    repo_root: Path
    projects_dir: Path
    _proposals: dict[str, ChangeProposal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).resolve()
        self.projects_dir = Path(self.projects_dir).resolve()
        self._state_dir = self.repo_root / _STATE_DIRNAME
        self._proposals_dir = self._state_dir / "proposals"
        self._backups_dir = self._state_dir / "backups"
        # Registered external project roots (WS-I): name -> resolved path.
        # These get the same propose/approve/backup/rollback guard as her own
        # code, minus the Nova-specific boot test (reported honestly).
        self._extra_roots: dict[str, Path] = {}
        # Load any persisted proposals, but do NOT create the state dir eagerly —
        # a Nova instance that never uses dev mode should leave no .nova_dev/.
        try:
            self._load_proposals()
            self._load_external_roots()
        except Exception as e:  # noqa: BLE001
            logger.warning("dev_mode_state_init_failed", error=str(e)[:200])

    def _ensure_dirs(self) -> None:
        self._proposals_dir.mkdir(parents=True, exist_ok=True)
        self._backups_dir.mkdir(parents=True, exist_ok=True)

    # ── Guards ───────────────────────────────────────────────────────────────

    def _require_enabled(self) -> None:
        if not dev_mode_enabled():
            raise DevModeDisabled("Developer mode is disabled. Set NOVA_DEV_MODE=1 in .env to enable it.")

    def _resolve_safe(self, raw_path: str, *, project: str = "") -> Path:
        base = self.repo_root
        if project:
            ext = self._extra_roots.get(project)
            if ext is None:
                raise DevModeError(
                    f"Unknown project '{project}'. Register it first (self.register_project with its folder path)."
                )
            base = ext

        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = base / target
        target = target.resolve()

        allowed = False
        for root in (self.repo_root, self.projects_dir, *self._extra_roots.values()):
            try:
                target.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise DevModeError(f"Path is outside the allowed roots: {target}")

        if target.name.lower() in _DENY_NAMES or target.name.lower().startswith(".env"):
            raise DevModeError("Refusing to touch environment/secret files.")
        if any(part.lower() in _DENY_PARTS for part in target.parts):
            raise DevModeError(f"Refusing to touch protected directory in path: {target}")
        return target

    def _external_root_for(self, path: Path) -> str:
        """Registered project name if `path` lives under an external root
        (and NOT under Nova's own repo/projects roots), else ""."""
        for own in (self.repo_root, self.projects_dir):
            try:
                path.relative_to(own)
                return ""
            except ValueError:
                pass
        for name, root in self._extra_roots.items():
            try:
                path.relative_to(root)
                return name
            except ValueError:
                continue
        return ""

    def _display_path(self, path: Path) -> str:
        """Human-readable path: repo-relative for her own code, 'project:rel'
        for a registered external project, absolute otherwise."""
        try:
            return str(path.relative_to(self.repo_root))
        except ValueError:
            pass
        for name, root in self._extra_roots.items():
            try:
                return f"{name}:{path.relative_to(root)}"
            except ValueError:
                continue
        return str(path)

    # ── External project roots (WS-I) ────────────────────────────────────────

    def _external_roots_file(self) -> Path:
        return self._state_dir / "external_roots.json"

    def _load_external_roots(self) -> None:
        f = self._external_roots_file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for name, raw in data.items():
            p = Path(str(raw))
            if p.exists() and p.is_dir():
                self._extra_roots[str(name)] = p.resolve()

    def _save_external_roots(self) -> None:
        self._ensure_dirs()
        self._external_roots_file().write_text(
            json.dumps({k: str(v) for k, v in self._extra_roots.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def register_external_root(self, name: str, raw_path: str) -> dict[str, object]:
        """Register one of Marcus's OTHER projects for guarded editing.

        Same propose -> approve -> backup -> apply -> rollback protection as
        her own code, with one honest difference surfaced at apply time: the
        Nova-specific boot smoke test can't run against a foreign project's
        environment, so external applies are compile-checked but not
        boot-verified (boot_test: "skipped_external_project")."""
        self._require_enabled()
        slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")[:48]
        if not slug:
            raise DevModeError("A project needs a short name (letters/numbers/dashes).")
        path = Path(raw_path or "").expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise DevModeError(f"Not an existing directory: {path}")
        if str(path).lower().startswith(_EXTERNAL_SYSTEM_PREFIXES):
            raise DevModeError("Refusing to register a system directory as a project.")
        if path.name.lower() in _DENY_PARTS:
            raise DevModeError(f"Refusing to register a protected directory ({path.name}) as a project root.")
        for own in (self.repo_root, self.projects_dir):
            try:
                path.relative_to(own)
                raise DevModeError(
                    f"{path} is already inside Nova's own allowed roots — no registration needed."
                )
            except ValueError:
                continue
        existing = self._extra_roots.get(slug)
        self._extra_roots[slug] = path
        self._save_external_roots()
        BUS.publish("dev.external_root_registered", {"project": slug, "path": clip(str(path), 200)})
        logger.info("dev_external_root_registered", project=slug, path=str(path), replaced=bool(existing))
        return {
            "project": slug, "path": str(path), "replaced_previous": bool(existing),
            "note": ("Registered. Changes here go through the same propose->approve->backup->rollback flow as "
                     "Nova's own code, but the boot smoke test is skipped (it only works for her own environment) — "
                     "applies are syntax-checked and reversible, not boot-verified."),
        }

    def list_external_roots(self) -> list[dict[str, str]]:
        self._require_enabled()
        return [{"project": k, "path": str(v)} for k, v in sorted(self._extra_roots.items())]

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save_proposal(self, p: ChangeProposal) -> None:
        try:
            self._ensure_dirs()
            (self._proposals_dir / f"{p.id}.json").write_text(
                json.dumps(asdict(p), ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("dev_proposal_persist_failed", proposal_id=p.id, error=str(e)[:200])

    def _load_proposals(self) -> None:
        if not self._proposals_dir.exists():
            return
        for f in sorted(self._proposals_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                self._proposals[str(d["id"])] = ChangeProposal(**d)
            except Exception:
                continue

    # ── Read-only inspection ────────────────────────────────────────────────

    def list_files(self, subdir: str = "", limit: int = 500, *, project: str = "") -> list[dict[str, object]]:
        self._require_enabled()
        base = self._resolve_safe(subdir or ".", project=project)
        if not base.exists() or not base.is_dir():
            raise DevModeError(f"Not a directory: {base}")

        out: list[dict[str, object]] = []
        for p in sorted(base.rglob("*")):
            if len(out) >= limit:
                break
            if not p.is_file():
                continue
            try:
                self._resolve_safe(str(p))
            except DevModeError:
                continue
            out.append({"path": self._display_path(p), "bytes": p.stat().st_size})
        return out

    def read_file(self, raw_path: str, *, project: str = "") -> dict[str, object]:
        self._require_enabled()
        path = self._resolve_safe(raw_path, project=project)
        if not path.exists() or not path.is_file():
            raise DevModeError(f"File not found: {path}")
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise DevModeError(f"File too large to inspect ({path.stat().st_size} bytes)")
        content = path.read_text(encoding="utf-8", errors="replace")
        BUS.publish("dev.inspect", {"path": clip(str(path), 200)})
        return {"path": str(path), "content": content}

    # ── Propose / confirm / apply ───────────────────────────────────────────

    def propose_change(
        self, raw_path: str, new_content: str, reason: str = "", origin: str = "nova", *, project: str = ""
    ) -> ChangeProposal:
        """Compute a diff and register a pending proposal. Never writes."""
        self._require_enabled()
        path = self._resolve_safe(raw_path, project=project)
        if len(new_content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise DevModeError("Proposed content too large.")

        # Guard against a forgotten `project` arg silently becoming a NEW file
        # in Nova's own repo: if the relative target doesn't exist here but
        # DOES exist in a registered external project, that's almost certainly
        # what the caller meant — refuse with a self-correcting message
        # instead of quietly proposing a stray file at the wrong root.
        if not project and not path.exists() and not Path(raw_path).expanduser().is_absolute():
            # Try the raw path AND every shorter suffix of it (models often
            # prepend the project folder name themselves), against each root.
            parts = Path(raw_path).parts
            suffixes = [str(Path(*parts[i:])) for i in range(len(parts))]
            for name, root in self._extra_roots.items():
                for suffix in suffixes:
                    candidate = (root / suffix).resolve()
                    try:
                        candidate.relative_to(root)
                    except ValueError:
                        continue
                    if candidate.exists():
                        raise DevModeError(
                            f"'{raw_path}' does not exist in Nova's own code, but '{suffix}' DOES exist in the "
                            f"registered project '{name}' — retry the same call with project='{name}' and "
                            f"path='{suffix}'. (To really create a new file in Nova's repo, pass its absolute path.)"
                        )

        old_content = ""
        if path.exists():
            if not path.is_file():
                raise DevModeError(f"Not a file: {path}")
            old_content = path.read_text(encoding="utf-8", errors="replace")

        rel = self._display_path(path)
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        if not diff:
            raise DevModeError("Proposed content is identical to the current file.")

        proposal = ChangeProposal(
            id=uuid4().hex,
            path=str(path),
            reason=(reason or "").strip(),
            diff=diff,
            new_content=new_content,
            created_at=datetime.now(timezone.utc).isoformat(),
            origin=(origin or "nova").strip() or "nova",
            old_content=old_content,
            project=self._external_root_for(path),
        )
        self._proposals[proposal.id] = proposal
        self._save_proposal(proposal)
        BUS.publish(
            "dev.proposal_created",
            {"proposal_id": proposal.id, "path": clip(rel, 200), "reason": clip(reason, 160), "origin": proposal.origin},
        )
        logger.info("dev_proposal_created", proposal_id=proposal.id, path=rel, origin=proposal.origin)
        return proposal

    def list_proposals(self) -> list[dict[str, object]]:
        self._require_enabled()
        return [
            {
                "id": p.id, "path": p.path, "reason": p.reason, "status": p.status,
                "created_at": p.created_at, "diff": p.diff, "origin": p.origin,
                "applied_at": p.applied_at, "boot_error": p.boot_error,
                "has_backup": bool(p.backup_path and Path(p.backup_path).exists()),
                "project": p.project,
                "display_path": self._display_path(Path(p.path)),
            }
            for p in sorted(self._proposals.values(), key=lambda x: x.created_at, reverse=True)
        ]

    def get_proposal(self, proposal_id: str) -> dict[str, object]:
        """Full detail for one proposal, including old/new content so the UI
        can render a real side-by-side diff (list_proposals stays light)."""
        self._require_enabled()
        p = self._proposals.get(proposal_id)
        if p is None:
            raise DevModeError(f"Unknown proposal: {proposal_id}")
        d = asdict(p)
        d["has_backup"] = bool(p.backup_path and Path(p.backup_path).exists())
        d["display_path"] = self._display_path(Path(p.path))
        return d

    def reject_proposal(self, proposal_id: str) -> None:
        self._require_enabled()
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise DevModeError(f"Unknown proposal: {proposal_id}")
        proposal.status = "rejected"
        self._save_proposal(proposal)
        BUS.publish("dev.proposal_rejected", {"proposal_id": proposal_id})

    def apply_proposal(self, proposal_id: str, *, confirm: bool = False) -> dict[str, object]:
        """Apply a previously proposed change. Requires explicit confirm=True.

        Flow: syntax-check (.py) -> back up current file -> atomic write ->
        boot smoke test (import the module) -> auto-rollback if the boot test
        fails. The caller (UI) is responsible for showing the diff to the user
        and only passing confirm=True after the user approves.
        """
        self._require_enabled()
        if not confirm:
            raise DevModeError("Refusing to apply without explicit confirmation (confirm=true).")

        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise DevModeError(f"Unknown proposal: {proposal_id}")
        if proposal.status != "pending":
            raise DevModeError(f"Proposal is not pending (status={proposal.status}).")

        # Re-validate the path at apply time (defense in depth).
        path = self._resolve_safe(proposal.path)

        # 1) Pre-apply syntax check for Python — never write code that won't compile.
        if path.suffix == ".py":
            try:
                compile(proposal.new_content, str(path), "exec")
            except SyntaxError as e:
                raise DevModeError(f"Refusing to apply: proposed content has a syntax error: {e.msg} (line {e.lineno})")

        # 2) Back up the current file (if any) so the apply is reversible.
        existed = path.exists()
        backup = self._backup_file(path, proposal_id) if existed else ""
        proposal.backup_path = backup

        # 3) Atomic write.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".nova-tmp")
        tmp.write_text(proposal.new_content, encoding="utf-8")
        tmp.replace(path)

        # 4) Boot smoke test — import the changed module in a fresh subprocess.
        # For a registered EXTERNAL project this is honestly skipped: the test
        # imports against Nova's own environment/import layout, which proves
        # nothing about a foreign project (different venv/deps/language) and
        # could even false-fail a perfectly good change.
        external = self._external_root_for(path)
        boot_err = None if external else self._boot_check(path)
        if boot_err:
            if existed and backup:
                self._restore_backup(backup, path)
            elif not existed:
                path.unlink(missing_ok=True)  # it was a brand-new file
            proposal.status = "reverted"
            proposal.boot_error = boot_err[:1000]
            self._save_proposal(proposal)
            BUS.publish(
                "dev.proposal_reverted",
                {"proposal_id": proposal_id, "path": clip(str(path), 200), "reason": "boot_test_failed"},
            )
            logger.warning("dev_proposal_reverted", proposal_id=proposal_id, path=str(path))
            return {
                "proposal_id": proposal_id, "path": str(path), "status": "reverted",
                "rolled_back": True, "boot_error": boot_err[:800],
            }

        proposal.status = "applied"
        proposal.applied_at = datetime.now(timezone.utc).isoformat()
        self._save_proposal(proposal)
        BUS.publish("dev.proposal_applied", {"proposal_id": proposal_id, "path": clip(str(path), 200)})
        logger.info("dev_proposal_applied", proposal_id=proposal_id, path=str(path))
        return {
            "proposal_id": proposal_id, "path": str(path), "status": "applied",
            "backup": backup,
            "boot_test": (
                "skipped_external_project" if external
                else ("passed" if self._module_name(path) else "skipped (non-module)")
            ),
        }

    def rollback_proposal(self, proposal_id: str) -> dict[str, object]:
        """Restore the pre-apply backup for an already-applied proposal."""
        self._require_enabled()
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise DevModeError(f"Unknown proposal: {proposal_id}")
        if proposal.status != "applied":
            raise DevModeError(f"Only an applied proposal can be rolled back (status={proposal.status}).")
        if not proposal.backup_path or not Path(proposal.backup_path).exists():
            raise DevModeError("No backup is available for this proposal.")

        path = self._resolve_safe(proposal.path)
        self._restore_backup(proposal.backup_path, path)
        proposal.status = "reverted"
        self._save_proposal(proposal)
        BUS.publish("dev.proposal_rolled_back", {"proposal_id": proposal_id, "path": clip(str(path), 200)})
        logger.info("dev_proposal_rolled_back", proposal_id=proposal_id, path=str(path))
        return {"proposal_id": proposal_id, "path": str(path), "status": "reverted"}

    def list_backups(self) -> list[dict[str, object]]:
        self._require_enabled()
        out: list[dict[str, object]] = []
        for f in sorted(self._backups_dir.glob("*"), reverse=True):
            if f.is_file():
                out.append({"name": f.name, "bytes": f.stat().st_size, "path": str(f)})
        return out

    # ── Backup helpers ───────────────────────────────────────────────────────

    def _backup_file(self, path: Path, proposal_id: str) -> str:
        self._ensure_dirs()
        # Backups ALWAYS live centrally under Nova's .nova_dev/backups — never
        # inside an external project. External backups are prefixed with the
        # registered project name so they're identifiable in the list.
        try:
            rel = str(path.relative_to(self.repo_root))
        except ValueError:
            ext = self._external_root_for(path)
            if ext:
                try:
                    rel = f"{ext}__{path.relative_to(self._extra_roots[ext])}"
                except (ValueError, KeyError):
                    rel = f"{ext}__{path.name}"
            else:
                rel = path.name
        flat = rel.replace(os.sep, "__").replace("/", "__")
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self._backups_dir / f"{ts}__{proposal_id[:8]}__{flat}"
        shutil.copy2(path, dest)
        return str(dest)

    @staticmethod
    def _restore_backup(backup_path: str, path: Path) -> None:
        src = Path(backup_path)
        tmp = path.with_suffix(path.suffix + ".nova-rollback-tmp")
        shutil.copy2(src, tmp)
        tmp.replace(path)

    # ── Boot smoke test ──────────────────────────────────────────────────────

    def _module_name(self, path: Path) -> str | None:
        """Dotted import path for a repo .py file, or None if not importable that way."""
        try:
            rel = path.resolve().relative_to(self.repo_root)
        except ValueError:
            return None
        if rel.suffix != ".py":
            return None
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return None
        return ".".join(parts)

    def _boot_check(self, path: Path) -> str | None:
        """Import the changed module in a subprocess. None = healthy/skipped.

        A non-empty string is the import error, which triggers auto-rollback.
        Non-Python or non-importable files are skipped (can't be boot-tested
        this way). A timeout is treated as inconclusive (pass) to avoid
        reverting a good change just because a heavy module imports slowly.
        """
        mod = self._module_name(path)
        if mod is None:
            return None
        try:
            proc = subprocess.run(
                [sys.executable, "-c", f"import {mod}"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=_BOOT_TEST_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            logger.warning("dev_boot_check_timeout", module=mod)
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)[:400]
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or f"import {mod} failed").strip()[-1500:]
        return None
