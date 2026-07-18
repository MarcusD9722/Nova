from __future__ import annotations

import json
from typing import Any


def extract_first_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object from an LLM response.

    LLMs frequently wrap JSON in prose, code fences, or emit multiple objects.
    This function finds the first balanced `{...}` chunk that parses as JSON.
    """
    if not raw:
        return None
    s = raw.strip()

    # Fast path: exact JSON
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = s[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break

        start = s.find("{", start + 1)

    return None
