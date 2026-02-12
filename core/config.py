from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class NovaConfig:
    version: str
    host: str
    port: int
    log_level: str
    context_tokens: int

    repo_root: Path
    model_dir: Path
    voice_dir: Path
    projects_dir: Path
    memory_dir: Path

    model_path: Path | None


def _first_existing_gguf(model_dir: Path) -> Path | None:
    if not model_dir.exists():
        return None
    ggufs = sorted([p for p in model_dir.glob("*.gguf") if p.is_file()])
    return ggufs[0] if ggufs else None


def load_config(repo_root: Path | None = None) -> NovaConfig:
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()

    load_dotenv(dotenv_path=root / ".env", override=False)

    version = "0.1.0"
    host = os.getenv("NOVA_HOST", "127.0.0.1")
    port = int(os.getenv("NOVA_PORT", "8008"))
    log_level = os.getenv("NOVA_LOG_LEVEL", "INFO").upper()
    context_tokens = int(os.getenv("NOVA_CONTEXT_TOKENS", "8192"))

    model_dir = (root / "model").resolve()
    voice_dir = (root / "voice").resolve()
    projects_dir = (root / "projects").resolve()

    memory_dir_env = os.getenv("NOVA_MEMORY_DIR", "memory_data")
    memory_dir = (root / memory_dir_env).resolve()

    model_path_env = os.getenv("NOVA_MODEL_PATH", "").strip()
    model_path: Path | None
    if model_path_env:
        model_path = Path(model_path_env).expanduser().resolve()
    else:
        model_path = _first_existing_gguf(model_dir)

    return NovaConfig(
        version=version,
        host=host,
        port=port,
        log_level=log_level,
        context_tokens=context_tokens,
        repo_root=root,
        model_dir=model_dir,
        voice_dir=voice_dir,
        projects_dir=projects_dir,
        memory_dir=memory_dir,
        model_path=model_path,
    )
