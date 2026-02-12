from __future__ import annotations

import re
from pathlib import Path

from core.safety import ensure_safe_subdir


class ProjectManager:
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

    def scaffold_project(self, name: str) -> Path:
        safe_name = self._sanitize(name)
        proj = ensure_safe_subdir(self._repo_root, self._projects_dir, self._projects_dir / safe_name)
        proj.mkdir(parents=True, exist_ok=True)

        (proj / "README.md").write_text(f"# {safe_name}\n\nScaffolded by Nova.\n", encoding="utf-8")
        (proj / "src").mkdir(exist_ok=True)
        (proj / "src" / "main.py").write_text("def main():\n    print('Hello from Nova scaffold')\n\n\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
        return proj
