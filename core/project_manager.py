from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

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
        name = name.strip()
        name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name)
        name = name.strip("-.")
        if not name:
            raise ValueError("Project name is empty after sanitization")
        return name

    def project_path(self, name: str) -> Path:
        safe_name = self._sanitize(name)
        return ensure_safe_subdir(self._repo_root, self._projects_dir, self._projects_dir / safe_name)

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
