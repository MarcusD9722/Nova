from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanStep:
    action: str
    args: dict


class Planner:
    def plan(self, user_message: str) -> list[PlanStep]:
        msg = user_message.strip()

        # Minimal real planner: detects project scaffolding intent.
        m = re.search(r"\bcreate\s+project\s+([a-zA-Z0-9._-]{1,64})\b", msg, flags=re.IGNORECASE)
        if m:
            return [PlanStep(action="scaffold_project", args={"name": m.group(1)})]

        return []
