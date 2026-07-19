from __future__ import annotations

"""Self-evaluation metrics (Phase 2.5 of docs/ROADMAP.md).

A lightweight in-memory collector fed by the self-improve worker's existing
bus subscription. It counts only signals that actually exist on the bus —
reply latency, empty replies, tool failure rate, vision errors — rather than
inventing metrics with no source. Rolls over at the UTC day boundary; the
worker snapshots it into a daily `self_eval` fact.
"""

from datetime import datetime, timezone


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class MetricsCollector:
    """Fed one bus event at a time via observe(); snapshot() reads the day."""

    def __init__(self) -> None:
        self._day = ""
        self._pending_start: datetime | None = None
        self._reset(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def _reset(self, day: str) -> None:
        self._day = day
        self._turns = 0
        self._empty_replies = 0
        self._latencies: list[float] = []
        self._tool_calls = 0
        self._tool_failures = 0
        self._vision_errors = 0
        self._tools_used_total = 0
        # _pending_start intentionally survives a rollover (a turn straddling
        # midnight still gets a latency), everything else resets.

    def _rollover_if_needed(self, ts: str) -> None:
        dt = _parse_ts(ts)
        day = (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        if day != self._day:
            self._reset(day)

    def observe(self, etype: str, ts: str, data: dict) -> None:
        self._rollover_if_needed(ts)
        data = data or {}

        if etype == "chat.thinking_start":
            self._pending_start = _parse_ts(ts)

        elif etype == "chat.assistant_done":
            self._turns += 1
            if int(data.get("chars") or 0) == 0:
                self._empty_replies += 1
            self._tools_used_total += int(data.get("tools_used") or 0)
            done = _parse_ts(ts)
            if self._pending_start and done:
                latency = (done - self._pending_start).total_seconds()
                if 0 <= latency < 600:  # ignore clock skew / absurd values
                    self._latencies.append(latency)
            self._pending_start = None

        elif etype == "tool.result":
            self._tool_calls += 1
            if data.get("ok") is False:
                self._tool_failures += 1

        elif etype == "tool.error":
            # A real failure — but "not configured" is a state, not a defect
            # (Phase 0.7 routes those to tool.not_configured, so they don't
            # arrive here). unknown_tool is a genuine miss; count it.
            self._tool_calls += 1
            self._tool_failures += 1

        elif etype == "vision.error":
            self._vision_errors += 1

    @staticmethod
    def _pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
        return round(s[idx], 2)

    def snapshot(self) -> dict:
        lat = self._latencies
        rate = (self._tool_failures / self._tool_calls) if self._tool_calls else 0.0
        return {
            "day": self._day,
            "turns": self._turns,
            "empty_replies": self._empty_replies,
            "reply_latency_s": {
                "count": len(lat),
                "avg": round(sum(lat) / len(lat), 2) if lat else 0.0,
                "p50": self._pct(lat, 50),
                "p95": self._pct(lat, 95),
                "max": round(max(lat), 2) if lat else 0.0,
            },
            "tool_calls": self._tool_calls,
            "tool_failures": self._tool_failures,
            "tool_failure_rate": round(rate, 3),
            "avg_tools_per_turn": round(self._tools_used_total / self._turns, 2) if self._turns else 0.0,
            "vision_errors": self._vision_errors,
        }
