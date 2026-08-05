"""Full-audit coverage for core/gpu_telemetry.py and core/logging_setup.py.

gpu_telemetry feeds /status. Its whole contract is failing SOFT: when
nvidia-smi is missing or broken the UI must show an honest "unavailable"
rather than plausible-looking zeros.

logging_setup gets a test because a log line was able to kill a background
worker on this machine (see docs/AUDIT_2026-08-03.md finding 11).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

import core.gpu_telemetry as gt
from core.gpu_telemetry import GpuTelemetry, _parse_int, _read_nvidia_smi, get_gpu_telemetry
from core.logging_setup import _force_utf8_console, get_logger, setup_logging

check = Checks()


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


async def test_parse_and_shape() -> None:
    check.section("GpuTelemetry shape")
    t = GpuTelemetry(available=False, error="nope")
    d = t.to_dict()
    check(set(d) == {"available", "name", "vram_total_mb", "vram_used_mb",
                     "temperature_c", "utilization_pct", "error"},
          f"to_dict exposes the full field set ({sorted(d)})")
    check(d["available"] is False and d["error"] == "nope", "an unavailable GPU carries its reason")

    check(_parse_int("42") == 42, "_parse_int parses an int")
    check(_parse_int(" 7.9 ") == 7, "_parse_int tolerates floats and whitespace")
    check(_parse_int("[N/A]") is None, "_parse_int returns None for nvidia-smi's [N/A]")
    check(_parse_int("") is None, "_parse_int returns None for empty")


async def test_read_paths(monkey) -> None:
    check.section("_read_nvidia_smi — honest failure modes")

    gt.shutil.which = lambda _n: None
    t = _read_nvidia_smi()
    check(t.available is False and "not found" in (t.error or ""),
          "a missing nvidia-smi is reported, not faked as zeros")
    check(t.vram_total_mb is None, "no invented numbers when unavailable")

    gt.shutil.which = lambda _n: "nvidia-smi"

    import subprocess as _sp
    real_run = _sp.run

    _sp.run = lambda *a, **kw: _Proc(stdout="NVIDIA GeForce RTX 5080, 16376, 2048, 45, 13\n")
    t = _read_nvidia_smi()
    check(t.available is True, "a good reading is available")
    check(t.name == "NVIDIA GeForce RTX 5080", f"the GPU name is parsed ({t.name})")
    check(t.vram_total_mb == 16376 and t.vram_used_mb == 2048, "VRAM figures are parsed")
    check(t.temperature_c == 45 and t.utilization_pct == 13, "temperature and utilization are parsed")

    _sp.run = lambda *a, **kw: _Proc(stdout="", stderr="driver error", returncode=9)
    t = _read_nvidia_smi()
    check(t.available is False and "driver error" in (t.error or ""),
          "a non-zero exit surfaces the real stderr")

    _sp.run = lambda *a, **kw: _Proc(stdout="only, three, fields\n")
    t = _read_nvidia_smi()
    check(t.available is False and "unexpected" in (t.error or ""),
          "short/garbled output is refused rather than half-parsed")

    _sp.run = lambda *a, **kw: _Proc(stdout="GPU A, [N/A], [N/A], [N/A], [N/A]\n")
    t = _read_nvidia_smi()
    check(t.available is True and t.vram_total_mb is None,
          "unreadable individual fields become None while the GPU stays available")

    def _boom(*a, **kw):
        raise OSError("access denied")

    _sp.run = _boom
    t = _read_nvidia_smi()
    check(t.available is False and "access denied" in (t.error or ""),
          "an exception from the subprocess is reported honestly")

    _sp.run = lambda *a, **kw: _Proc(stdout="GPU A, 1, 2, 3, 4\nGPU B, 9, 9, 9, 9\n")
    t = _read_nvidia_smi()
    check(t.name == "GPU A", "only the first GPU line is used (Nova pins main_gpu=0)")

    _sp.run = real_run


async def test_cache() -> None:
    check.section("get_gpu_telemetry caching")
    gt._CACHE = None
    calls = {"n": 0}

    def _counted():
        calls["n"] += 1
        return GpuTelemetry(available=True, name=f"call{calls['n']}")

    real = gt._read_nvidia_smi
    gt._read_nvidia_smi = _counted
    try:
        a = await get_gpu_telemetry()
        b = await get_gpu_telemetry()
        check(calls["n"] == 1, f"a second read within the TTL is served from cache ({calls['n']} call(s))")
        check(a.name == b.name, "the cached value is returned unchanged")

        results = await asyncio.gather(*(get_gpu_telemetry() for _ in range(10)))
        check(all(r.name == a.name for r in results), "10 concurrent reads agree")

        gt._CACHE = (0.0, a)   # force expiry
        c = await get_gpu_telemetry()
        check(calls["n"] == 2 and c.name == "call2", "an expired cache triggers a real re-read")
    finally:
        gt._read_nvidia_smi = real
        gt._CACHE = None


async def test_logging_setup() -> None:
    check.section("logging_setup")
    _force_utf8_console()
    check(sys.stdout.encoding.lower().replace("-", "") == "utf8",
          f"stdout is forced to UTF-8 so a log line cannot kill a worker ({sys.stdout.encoding})")
    check(sys.stderr.encoding.lower().replace("-", "") == "utf8", "stderr too")

    log = get_logger("audit-test")
    # Box-drawing characters are exactly what structlog's traceback renderer
    # emits; these used to raise UnicodeEncodeError on a cp1252 console.
    log.info("unicode_probe", art="┌─┐ │ └─┘ → ✓ •", emoji="🔥")
    check(True, "logging box-drawing characters and emoji does not raise")

    setup_logging("INFO")
    check(True, "setup_logging runs without raising")
    get_logger("audit-test").warning("after_setup", art="│─→")
    check(True, "logging still works after setup_logging")


async def main() -> None:
    await test_parse_and_shape()
    await test_read_paths(None)
    await test_cache()
    await test_logging_setup()
    check.finish()


asyncio.run(main())
