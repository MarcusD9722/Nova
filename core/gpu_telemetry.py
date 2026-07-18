from __future__ import annotations

"""Real GPU telemetry via nvidia-smi.

Used by the /status endpoint so the UI shows the actual GPU name, VRAM usage,
and temperature instead of placeholder values. Fails soft: if nvidia-smi is
missing or errors, returns available=False and the UI shows an honest
"unavailable" state.
"""

import asyncio
import shutil
from dataclasses import asdict, dataclass
from time import monotonic


@dataclass
class GpuTelemetry:
    available: bool
    name: str | None = None
    vram_total_mb: int | None = None
    vram_used_mb: int | None = None
    temperature_c: int | None = None
    utilization_pct: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_CACHE: tuple[float, GpuTelemetry] | None = None
_CACHE_TTL_S = 2.0
_QUERY = "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu"


def _parse_int(raw: str) -> int | None:
    try:
        return int(float(raw.strip()))
    except Exception:
        return None


def _read_nvidia_smi() -> GpuTelemetry:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return GpuTelemetry(available=False, error="nvidia-smi not found on PATH")

    import subprocess

    try:
        out = subprocess.run(
            [exe, _QUERY, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        return GpuTelemetry(available=False, error=f"nvidia-smi failed: {exc}")

    if out.returncode != 0 or not out.stdout.strip():
        return GpuTelemetry(available=False, error=(out.stderr or "nvidia-smi returned no data").strip()[:200])

    # First GPU line only (Nova pins main_gpu=0 by default).
    line = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return GpuTelemetry(available=False, error="unexpected nvidia-smi output")

    return GpuTelemetry(
        available=True,
        name=parts[0] or None,
        vram_total_mb=_parse_int(parts[1]),
        vram_used_mb=_parse_int(parts[2]),
        temperature_c=_parse_int(parts[3]),
        utilization_pct=_parse_int(parts[4]),
    )


async def get_gpu_telemetry() -> GpuTelemetry:
    """Cached async read (2s TTL) so /status polling stays cheap."""
    global _CACHE
    now = monotonic()
    if _CACHE is not None and (now - _CACHE[0]) < _CACHE_TTL_S:
        return _CACHE[1]

    telemetry = await asyncio.to_thread(_read_nvidia_smi)
    _CACHE = (monotonic(), telemetry)
    return telemetry
