from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from core.project_names import (
    canonical_project_slug, safe_live_component, safe_trash_entry,
)
from core.safety import ensure_safe_subdir


class ProjectManager:
    """
    Nova project workspace manager.

    This keeps Nova's original scaffold behavior but adds ADA-style workspace features:
    - per-project folders (artifacts/cad/browser/notes/src)
    - per-project chat_history.jsonl
    - bounded project context snapshot for goal continuity
    """

    def __init__(self, repo_root: Path, projects_dir: Path):
        self._repo_root = repo_root
        self._projects_dir = projects_dir

    def _sanitize(self, name: str) -> str:
        """Resolve a name to a LIVE project directory component.

        This used to be a second, independent definition of the project namespace
        — case-preserving, allowing dots and underscores, no length cap and no
        Win32 reserved-device handling. So `project.scaffold` and
        `project.start_build` could create two different directories for one human
        name, and only one of the two was protected from `CON`/`NUL`/`COM1`.

        An EXISTING directory is resolved by its real name first, so legacy
        projects created under the old rules keep working and are never renamed.
        Anything new gets the canonical contract.
        """
        raw = (name or "").strip()

        # Probe for an EXISTING legacy directory first. This must not raise for a
        # degenerate name: `safe_live_component("🎈")` has nothing to work with,
        # and letting that propagate made ProjectManager reject names that
        # ProjectBuilder happily resolved to `untitled` — the same input producing
        # a project on one surface and an error on the other.
        try:
            legacy = safe_live_component(raw)
            if (self._projects_dir / legacy).is_dir():
                return legacy
        except (ValueError, OSError):
            pass

        # Anything else is a NEW project name, and gets the canonical contract —
        # including the `untitled` fallback, so every creation surface agrees.
        return canonical_project_slug(raw)

    def project_path(self, name: str) -> Path:
        safe_name = self._sanitize(name)
        return ensure_safe_subdir(self._repo_root, self._projects_dir, self._projects_dir / safe_name)

    # ── Deletion: recoverable by default ────────────────────────────────────
    # Nova can remove a project, but never irreversibly on the first step.
    # delete_project() MOVES the folder into projects/.trash/, so a mistaken
    # delete (wrong name, misheard request) is always undoable with restore.
    # Only purge_trash() erases bytes, and that sits behind the CRITICAL
    # permission tier — denied in the default 'guarded' mode.

    TRASH_DIRNAME = ".trash"

    def _trash_dir(self) -> Path:
        return self._projects_dir / self.TRASH_DIRNAME

    @staticmethod
    def _measure(path: Path) -> tuple[int, int]:
        """(file count, total bytes) — so a delete can report what it moved."""
        files = 0
        size = 0
        for p in path.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        return files, size

    def list_projects(self) -> list[str]:
        """Project names, excluding the trash folder and other dot-dirs."""
        if not self._projects_dir.exists():
            return []
        return sorted(
            p.name for p in self._projects_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def delete_project(self, name: str) -> dict[str, Any]:
        """Move a project into .trash/ (recoverable). Never deletes bytes."""
        proj = self.project_path(name)          # sandboxed by ensure_safe_subdir
        if proj.resolve() == self._projects_dir.resolve():
            raise ValueError("refusing to delete the projects directory itself")
        if not proj.exists() or not proj.is_dir():
            raise FileNotFoundError(f"no such project: {name}")

        files, size = self._measure(proj)
        trash = self._trash_dir()
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = trash / f"{proj.name}--{stamp}"
        shutil.move(str(proj), str(target))
        return {
            "project": proj.name, "moved_to_trash": target.name,
            "files": files, "bytes": size, "recoverable": True,
        }

    def list_trash(self) -> list[dict[str, Any]]:
        trash = self._trash_dir()
        if not trash.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(trash.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            files, size = self._measure(p)
            out.append({"entry": p.name, "original": p.name.rsplit("--", 1)[0],
                        "files": files, "bytes": size})
        return out

    def restore_project(self, entry: str) -> dict[str, Any]:
        """Move a trashed project back. Refuses to clobber a live project."""
        # A trash id carries a timestamp (`slug--20260818-062122`), so it has its
        # own rules — forcing the live-project contract onto it would corrupt
        # existing entries.
        safe = safe_trash_entry(entry)
        src = ensure_safe_subdir(self._repo_root, self._trash_dir(), self._trash_dir() / safe)
        if not src.exists() or not src.is_dir():
            raise FileNotFoundError(f"nothing in trash named: {entry}")
        # Restore to the EXACT identity the project had, not a canonicalised one.
        # `project_path()` would re-canonicalise here — and since the live legacy
        # directory no longer exists (it is in the trash), the legacy probe misses
        # and `My_Old.Project` came back as `my-old-project`. That silently renamed
        # a project during restore, contradicting the legacy contract outright.
        original = src.name.rsplit("--", 1)[0]
        component = safe_live_component(original)
        dest = ensure_safe_subdir(self._repo_root, self._projects_dir,
                                  self._projects_dir / component)
        if dest.exists():
            raise FileExistsError(f"'{original}' already exists — rename or delete it first")
        shutil.move(str(src), str(dest))
        return {"restored": dest.name, "from_trash": src.name}

    def purge_trash(self, entry: str | None = None) -> dict[str, Any]:
        """PERMANENTLY erase one trashed project, or all of them. This is the
        only method here that destroys data; it is gated at CRITICAL."""
        trash = self._trash_dir()
        if not trash.exists():
            return {"purged": [], "note": "trash is already empty"}
        if entry:
            # A trash id carries a timestamp (`slug--20260818-062122`), so it
            # has its own rules — forcing the live-project contract onto it
            # would corrupt existing entries.
            safe = safe_trash_entry(entry)
            target = ensure_safe_subdir(self._repo_root, trash, trash / safe)
            if not target.exists():
                raise FileNotFoundError(f"nothing in trash named: {entry}")
            targets = [target]
        else:
            targets = [p for p in trash.iterdir() if p.is_dir()]
        purged = []
        for t in targets:
            files, size = self._measure(t)
            shutil.rmtree(t, ignore_errors=False)
            purged.append({"entry": t.name, "files": files, "bytes": size})
        return {"purged": purged, "permanent": True}

    def ensure_workspace(self, name: str) -> Path:
        proj = self.project_path(name)
        proj.mkdir(parents=True, exist_ok=True)
        for d in ("artifacts", "cad", "browser", "notes", "src"):
            (proj / d).mkdir(exist_ok=True)
        # Ensure chat log exists
        chat = proj / "chat_history.jsonl"
        if not chat.exists():
            chat.write_text("", encoding="utf-8")
        return proj

    def scaffold_project(self, name: str) -> Path:
        # Preserve original behavior; now scaffold into the workspace.
        proj = self.ensure_workspace(name)
        safe_name = self._sanitize(name)
        (proj / "README.md").write_text(f"# {safe_name}\n\nScaffolded by Nova.\n", encoding="utf-8")
        (proj / "src").mkdir(exist_ok=True)
        (proj / "src" / "main.py").write_text(
            "def main():\n    print('Hello from Nova scaffold')\n\n\nif __name__ == '__main__':\n    main()\n",
            encoding="utf-8",
        )
        return proj

    def append_chat_log(self, project_name: str, *, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        proj = self.ensure_workspace(project_name)
        entry = {
            "ts": time.time(),
            "role": role,
            "content": content,
            "meta": meta or {},
        }
        with (proj / "chat_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def save_artifact(
        self,
        project_name: str,
        *,
        kind: str,
        data: bytes | None = None,
        source_path: Path | None = None,
        filename_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        proj = self.ensure_workspace(project_name)
        ts = int(time.time())
        safe_kind = re.sub(r"[^a-zA-Z0-9._-]+", "-", (kind or "artifact")).strip("-.") or "artifact"
        hint = (filename_hint or "").strip()
        hint = re.sub(r"[^a-zA-Z0-9._-]+", "-", hint).strip("-.")
        name = f"{safe_kind}_{ts}" + (f"_{hint}" if hint else "")
        out_dir = proj / "artifacts"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / name

        if source_path is not None:
            # preserve extension if present
            out_path = out_dir / (name + source_path.suffix)
            out_path.write_bytes(source_path.read_bytes())
        elif data is not None:
            out_path.write_bytes(data)
        else:
            out_path.write_text("", encoding="utf-8")

        # write metadata sidecar
        meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
        meta = {"kind": kind, "created_at": ts, **(metadata or {})}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_path

    def _iter_project_files(self, proj: Path, *, include_globs: Iterable[str]) -> list[Path]:
        files: list[Path] = []
        for g in include_globs:
            files.extend([p for p in proj.rglob(g) if p.is_file()])
        # de-dupe and keep inside project
        out = []
        seen = set()
        for p in files:
            try:
                rp = p.resolve()
            except Exception:
                continue
            if str(rp) in seen:
                continue
            seen.add(str(rp))
            out.append(p)
        return out

    def get_project_context(
        self,
        project_name: str,
        *,
        max_chars: int = 6000,
        tail_chat_lines: int = 40,
        include_globs: list[str] | None = None,
    ) -> str:
        """
        Returns a bounded snapshot of the project state:
        - directory highlights
        - recent chat tail
        - small excerpts from selected text files
        """
        proj = self.ensure_workspace(project_name)
        include_globs = include_globs or ["README.md", "notes/*.md", "notes/*.txt", "src/*.py"]

        parts: list[str] = []
        parts.append(f"PROJECT: {project_name}")
        parts.append("FOLDERS: " + ", ".join([p.name for p in proj.iterdir() if p.is_dir()]))

        # Recent chat tail
        chat_path = proj / "chat_history.jsonl"
        if chat_path.exists():
            try:
                lines = chat_path.read_text(encoding="utf-8").splitlines()
                tail = lines[-tail_chat_lines:] if tail_chat_lines > 0 else []
                parts.append("RECENT_CHAT:")
                for ln in tail:
                    try:
                        obj = json.loads(ln)
                        role = obj.get("role", "?")
                        content = str(obj.get("content", ""))[:500]
                        parts.append(f"{role}: {content}")
                    except Exception:
                        continue
            except Exception:
                pass

        # File excerpts
        parts.append("FILES:")
        files = self._iter_project_files(proj, include_globs=include_globs)
        for fp in files[:25]:
            rel = fp.relative_to(proj)
            parts.append(f"- {rel.as_posix()}")
        parts.append("FILE_EXCERPTS:")
        remaining = max_chars
        for fp in files[:12]:
            if remaining <= 0:
                break
            # only text-like
            if fp.suffix.lower() not in (".md", ".txt", ".py", ".json", ".yaml", ".yml"):
                continue
            try:
                txt = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            chunk = txt[: min(len(txt), 1200)]
            block = f"## {fp.relative_to(proj).as_posix()}\n{chunk}\n"
            if len(block) > remaining:
                block = block[:remaining]
            parts.append(block)
            remaining -= len(block)

        out = "\n".join(parts)
        return out[:max_chars]
