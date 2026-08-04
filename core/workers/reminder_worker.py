from __future__ import annotations

"""User-facing scheduling: reminders and a spoken morning briefing.

Distinct from internal worker pacing (NOVA_SELF_IMPROVE_INTERVAL_S etc.) — this
is real "remind me at 5pm" / "check on me every morning" capability. Polls due
reminders on a short interval, publishes them on the event bus for the
frontend to surface as a chat message + speech, and reschedules recurring ones.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from core.dates import parse_reminder_time
from core.event_bus import BUS, clip
from core.logging_setup import get_logger
from core.tool_router import ToolCall, ToolRouter
from core.workers.lifecycle import stop_worker
from memory.unifier import MemoryUnifier

logger = get_logger(__name__)

_BRIEFING_TITLE = "__nova_morning_briefing__"

# A reminder due more than this long ago was almost certainly missed because
# Nova was OFFLINE (normal firing happens within one ~30s poll). Missed items
# are handled specially instead of firing stale as if they're happening now.
_MISSED_GRACE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReminderWorker:
    def __init__(
        self,
        *,
        memory: MemoryUnifier,
        router: ToolRouter,
        poll_interval_s: float | None = None,
        briefing_time: str | None = None,
    ) -> None:
        self._memory = memory
        self._router = router
        self._interval = float(poll_interval_s if poll_interval_s is not None else os.getenv("NOVA_REMINDER_POLL_S", "30") or "30")
        # Empty string disables the automatic morning briefing entirely.
        self._briefing_time = (briefing_time if briefing_time is not None else os.getenv("NOVA_BRIEFING_TIME", "08:00")).strip()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_birthday_scan_date: str | None = None
        self._last_habit_scan_date: str | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("reminder_worker_started", interval_s=self._interval, briefing_time=self._briefing_time or "disabled")

    async def stop(self) -> None:
        self._stop.set()
        await stop_worker(self._task, name="reminders")

    async def _run(self) -> None:
        await self._memory.initialize()
        # Operator visibility: current local time + how long Nova was offline,
        # so reminder timing across boots is auditable.
        try:
            now_local = datetime.now().astimezone()
            offline_note = "unknown"
            last = await self._memory.get_latest_fact(entity="session", attribute="last_active")
            if last and last.value:
                try:
                    prev = datetime.fromisoformat(last.value)
                    if prev.tzinfo is None:
                        prev = prev.replace(tzinfo=timezone.utc)
                    mins = max(0, int((_now() - prev).total_seconds() // 60))
                    offline_note = f"{mins} min" if mins < 120 else f"{mins // 60}h {mins % 60}m"
                except Exception:
                    pass
            logger.info("reminder_worker_boot_clock", local_time=now_local.strftime("%Y-%m-%d %H:%M %Z"), offline_for=offline_note)
        except Exception:
            pass
        try:
            await self._ensure_morning_briefing()
        except Exception as e:  # noqa: BLE001
            logger.debug("morning_briefing_setup_failed", error=str(e))
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("reminder_tick_failed", error=str(e)[:200])
            try:
                await self._check_birthdays()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.debug("birthday_check_failed", error=str(e)[:200])
            try:
                await self._check_habits()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.debug("habit_check_failed", error=str(e)[:200])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    @staticmethod
    def _hour_window_label(hour: int) -> str:
        end = (hour + 2) % 24
        fmt = lambda h: f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
        return f"{fmt(hour)}-{fmt(end)}"

    async def _check_habits(self) -> None:
        """Once per day: look for a tool called around the same time on
        several distinct recent days, and — if not already suggested —
        surface it as a one-time question, never an auto-created reminder
        (HP1). Marcus has to say yes before anything gets automated."""
        today_str = _now().strftime("%Y-%m-%d")
        if self._last_habit_scan_date == today_str:
            return
        self._last_habit_scan_date = today_str

        for tool_name in await self._memory.distinct_logged_tools(window_days=14):
            if not await self._memory.should_suggest_habit(tool_name):
                continue
            habit = await self._memory.detect_habit(tool_name)
            if habit is None:
                continue
            window = self._hour_window_label(int(habit["hour"]))
            title = f"__nova_habit_{tool_name}__"
            details = (
                f"I've noticed you tend to ask about this ({tool_name}) most days around {window} — "
                "want me to just bring it up automatically instead of you having to ask?"
            )
            await self._memory.create_reminder(
                title=title, details=details, due_at_iso=_now().isoformat(), recurrence="none"
            )
            await self._memory.mark_habit_suggested(tool_name)
            BUS.publish("habit.detected", {"tool": tool_name, "window": window, "distinct_days": habit["distinct_days"]})
            return  # one suggestion per scan, don't pile on

    async def _check_birthdays(self) -> None:
        """Once per day: auto-create a heads-up reminder for any birthday/
        anniversary landing within the next 3 days (MR1). Idempotent — a
        title keyed by name+date means a re-scan (or a restart) never creates
        a duplicate for the same occurrence."""
        today_str = _now().strftime("%Y-%m-%d")
        if self._last_birthday_scan_date == today_str:
            return
        self._last_birthday_scan_date = today_str

        upcoming = await self._memory.list_people_with_upcoming_dates(within_days=3)
        if not upcoming:
            return
        existing_titles = {r.get("title") for r in await self._memory.list_reminders(status=None, limit=300)}
        for entry in upcoming:
            occurrence = str(entry["occurrence"])  # YYYY-MM-DD
            title = f"__nova_{entry['label']}_{entry['name']}_{occurrence}__"
            if title in existing_titles:
                continue
            try:
                due = datetime.fromisoformat(occurrence).replace(hour=9, minute=0, second=0, microsecond=0)
            except Exception:
                continue
            label = "birthday" if "birthday" in entry["label"] or entry["label"] == "bday" else entry["label"]
            details = f"{entry['name']}'s {label} is today." if entry["days_until"] == 0 else (
                f"{entry['name']}'s {label} is in {entry['days_until']} day(s) ({occurrence})."
            )
            await self._memory.create_reminder(title=title, details=details, due_at_iso=due.isoformat(), recurrence="none")

    async def _ensure_morning_briefing(self) -> None:
        """Idempotently create the one recurring briefing reminder, if enabled."""
        if not self._briefing_time:
            return
        existing = await self._memory.list_reminders(status="pending", limit=200)
        if any(r.get("title") == _BRIEFING_TITLE for r in existing):
            return
        due = parse_reminder_time(f"every day at {self._briefing_time}")
        if due is None:
            logger.warning("invalid_briefing_time", value=self._briefing_time)
            return
        due_at, recurrence = due
        await self._memory.create_reminder(
            title=_BRIEFING_TITLE, details="Morning briefing", due_at_iso=due_at.isoformat(), recurrence=recurrence,
        )
        logger.info("morning_briefing_scheduled", first_due=due_at.isoformat())

    async def _tick(self) -> None:
        due = await self._memory.due_reminders(limit=50)
        if not due:
            return
        now = _now()
        missed_oneshots: list[str] = []

        for r in due:
            reminder_id = str(r["reminder_id"])
            title = str(r.get("title") or "")
            is_briefing = title == _BRIEFING_TITLE
            recurrence = str(r.get("recurrence") or "none")

            try:
                due_at = datetime.fromisoformat(str(r["due_at"]))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except Exception:
                due_at = now
            missed = (now - due_at) > _MISSED_GRACE

            # ── Missed while offline: don't fire stale as if it's happening now ──
            if missed:
                if recurrence != "none":
                    # Recurring (briefing, daily/weekly): roll forward to the next
                    # future occurrence instead of firing the missed one (fixes the
                    # 12:53 AM "good morning" after an overnight-offline gap).
                    try:
                        nxt = self._next_future_occurrence(due_at, recurrence, now)
                        await self._memory.reschedule_reminder(reminder_id=reminder_id, next_due_at_iso=nxt.isoformat())
                    except Exception as e:  # noqa: BLE001
                        logger.warning("reminder_reschedule_failed", reminder_id=reminder_id, error=str(e)[:160])
                        await self._memory.complete_reminder(reminder_id=reminder_id)
                    continue
                # One-shot reminder/timer missed while offline: collect for a
                # single consolidated catch-up (below); mark it done. Internal
                # bookkeeping reminders (__nova_*) aren't surfaced to Marcus.
                if not title.startswith("__nova_"):
                    missed_oneshots.append(str(r.get("details") or title))
                await self._memory.complete_reminder(reminder_id=reminder_id)
                continue

            # ── On time (within grace): fire normally ──
            try:
                message = await self._compose_briefing() if is_briefing else (str(r.get("details") or title))
            except Exception as e:  # noqa: BLE001
                logger.debug("briefing_compose_failed", error=str(e))
                message = "Good morning!" if is_briefing else (str(r.get("details") or title))

            BUS.publish(
                "reminder.due",
                {"reminder_id": reminder_id, "title": clip(title, 120) if not is_briefing else "Morning briefing",
                 "message": clip(message, 900), "briefing": is_briefing},
            )

            if recurrence == "none":
                await self._memory.complete_reminder(reminder_id=reminder_id)
                continue
            try:
                next_due = self._next_future_occurrence(due_at, recurrence, now)
                await self._memory.reschedule_reminder(reminder_id=reminder_id, next_due_at_iso=next_due.isoformat())
            except Exception as e:  # noqa: BLE001
                logger.warning("reminder_reschedule_failed", reminder_id=reminder_id, error=str(e)[:160])
                await self._memory.complete_reminder(reminder_id=reminder_id)  # don't loop forever on a bad row

        # One consolidated heads-up for everything missed while offline — not a
        # flood of individual stale alerts.
        if missed_oneshots:
            shown = "; ".join(missed_oneshots[:8])
            extra = f" (+{len(missed_oneshots) - 8} more)" if len(missed_oneshots) > 8 else ""
            BUS.publish(
                "reminder.due",
                {"reminder_id": "missed-batch", "title": "Heads up",
                 "message": clip(f"While you were away, these reminders came due: {shown}{extra}.", 900),
                 "briefing": False, "missed": True},
            )

    @staticmethod
    def _advance(due_at_iso: str, recurrence: str) -> datetime:
        try:
            due = datetime.fromisoformat(due_at_iso)
        except Exception:
            due = _now()
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if recurrence == "daily":
            return due + timedelta(days=1)
        if recurrence == "weekday":
            nxt = due + timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            return nxt
        if recurrence == "weekly":
            return due + timedelta(days=7)
        return due + timedelta(days=1)

    @classmethod
    def _next_future_occurrence(cls, due_at: datetime, recurrence: str, now: datetime) -> datetime:
        """Advance a recurring due-time repeatedly until it lands in the future,
        so a reminder missed across a multi-day offline gap resumes at its next
        real occurrence rather than firing every skipped one."""
        nxt = due_at
        for _ in range(500):  # bounded; 500 daily steps ≈ 1.3 years of catch-up
            nxt = cls._advance(nxt.isoformat(), recurrence)
            if nxt > now:
                return nxt
        return nxt

    @staticmethod
    def _short_place_label(address: str) -> str:
        """Pull a friendly city-ish label out of a formatted address, e.g.
        '9139 Coronal Rings, San Antonio, TX 78254, USA' -> 'San Antonio'."""
        parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
        if len(parts) >= 2:
            return parts[1]
        return parts[0] if parts else str(address)

    async def _weather_line(self) -> str:
        """Weather for Marcus's stored location. A bare city name goes straight
        to the weather API; a full street address is geocoded to coordinates
        first (the city-name lookup 404s on a street address). Returns '' —
        never a fabricated line — when it can't be resolved."""
        loc = await self._memory.get_latest_fact(entity="user", attribute="location")
        place = (loc.value.strip() if loc and loc.value else "")
        if not place:
            return ""

        weather_args: dict = {"units": "imperial"}
        label = place
        if any(ch.isdigit() for ch in place):
            geo = await self._router.execute(
                ToolCall(name="maps.geocode", args={"address": place}), timeout_s=10.0, retries=0
            )
            coords = (geo.result or {}).get("location") if (geo.ok and isinstance(geo.result, dict)) else None
            if not coords or coords.get("lat") is None or coords.get("lng") is None:
                return ""  # can't geocode a street address -> skip honestly, don't 404
            weather_args["lat"] = coords["lat"]
            weather_args["lon"] = coords["lng"]
            label = self._short_place_label((geo.result or {}).get("formatted_address") or place)
        else:
            weather_args["city"] = place

        res = await self._router.execute(
            ToolCall(name="weather.current", args=weather_args), timeout_s=10.0, retries=0
        )
        if res.ok and isinstance(res.result, dict):
            desc = str(res.result.get("description") or "").strip()
            temp = res.result.get("temp")
            if desc and temp is not None:
                return f"It's {desc} and {temp}°F in {label} right now."
        return ""

    async def _compose_briefing(self) -> str:
        """Best-effort morning briefing: weather + active goals + upcoming
        reminders. Sections are omitted honestly (not guessed) when the
        underlying data isn't available — e.g. no known location -> no
        weather line, rather than a fabricated one."""
        parts: list[str] = []

        try:
            weather_line = await self._weather_line()
            if weather_line:
                parts.append(weather_line)
        except Exception:
            pass

        try:
            res = await self._router.execute(ToolCall(name="calendar.today", args={}), timeout_s=10.0, retries=0)
            if res.ok and isinstance(res.result, dict):
                events = res.result.get("events") or []
                if events:
                    titles = ", ".join(str(e.get("summary") or "") for e in events[:3])
                    more = f" (+{len(events) - 3} more)" if len(events) > 3 else ""
                    parts.append(f"On your calendar today: {titles}{more}.")
        except Exception:
            pass

        try:
            # Count only — never content in a proactive/unprompted surface;
            # full summaries stay on-demand (email.recent) when Marcus asks.
            res = await self._router.execute(
                ToolCall(name="email.recent", args={"unread_only": True, "limit": 25}), timeout_s=10.0, retries=0,
            )
            if res.ok and isinstance(res.result, dict):
                count = int(res.result.get("count") or 0)
                if count:
                    parts.append(f"{count} unread email(s) waiting.")
        except Exception:
            pass

        try:
            goals = await self._memory.list_goals(limit=10)
            active = [g for g in goals if str(g.get("status")) == "active"]
            if active:
                titles = ", ".join(str(g.get("title") or "") for g in active[:3])
                parts.append(f"You've got {len(active)} active goal(s) I'm working on: {titles}.")
        except Exception:
            pass

        try:
            rems = await self._memory.list_reminders(status="pending", limit=20)
            other = [r for r in rems if str(r.get("title")) != _BRIEFING_TITLE]
            if other:
                parts.append(f"{len(other)} reminder(s) coming up.")
        except Exception:
            pass

        if not parts:
            return "Good morning! Nothing urgent on the radar right now — hope you have a good day."
        return "Good morning! " + " ".join(parts)
