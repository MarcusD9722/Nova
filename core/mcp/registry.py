from __future__ import annotations

"""The capability registry: many servers, one namespace, bounded prompt cost.

Three problems this solves, in order of how badly they bite:

1. **Identity.** A server may call its tool `search`. So may three others. A
   bare remote name is not an identity, so every capability gets
   `mcp:<server-id>:<tool-name>` — stable, unique, permission-checkable, and
   traceable back to which server said what.

2. **Prompt cost.** Nova must scale toward hundreds or thousands of tools
   without showing the model all of them. Selection runs on cheap one-line
   metadata; the full JSON Schema for a tool is hydrated only after it has been
   shortlisted. A thousand registered capabilities cost a thousand short strings
   at selection time, not a thousand schemas.

3. **Churn.** Servers reconnect with different tools, rename things, and change
   schemas between calls. Every descriptor carries a schema hash, and anything
   derived from it (embeddings, hydrated schemas) is keyed by that hash, so a
   change invalidates exactly what it should and nothing else.

Trust is not negotiable here: everything from a server is UNTRUSTED_EXTERNAL,
sanitised at ingest, and permission-tiered pessimistically.
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.logging_setup import get_logger
from core.mcp.sanitize import (
    looks_like_injection,
    sanitize_identifier,
    sanitize_schema,
    sanitize_text,
)
from memory.artifacts import TRUST_UNTRUSTED

logger = get_logger(__name__)

#: Nova capability ids for MCP tools always start here, so a permission rule or
#: an audit line can be matched on prefix without parsing.
MCP_PREFIX = "mcp"


def capability_id(server_id: str, tool_name: str) -> str:
    return f"{MCP_PREFIX}:{sanitize_identifier(server_id)}:{sanitize_identifier(tool_name)}"


@dataclass
class Capability:
    """One MCP tool, normalised into something Nova can govern."""

    cap_id: str                       # mcp:<server>:<tool>
    server_id: str
    remote_name: str                  # exactly what the server called it
    description: str                  # sanitised, one line, length-capped
    schema: dict = field(default_factory=dict)     # sanitised JSON Schema
    schema_hash: str = ""
    trust: str = TRUST_UNTRUSTED
    permission_capability: str = ""   # what the broker is asked about
    read_only: bool = False
    destructive: bool = False
    injection_flagged: bool = False   # metadata contained instruction-shaped text
    discovered_at: float = field(default_factory=time.time)

    def selector_line(self) -> str:
        """The cheap metadata selection sees. Deliberately NOT the schema."""
        origin = f"[{self.server_id}]"
        return f"{origin} {self.description}".strip()

    def to_status(self) -> dict[str, Any]:
        return {
            "id": self.cap_id, "server": self.server_id, "remote_name": self.remote_name,
            "schema_hash": self.schema_hash[:12], "read_only": self.read_only,
            "destructive": self.destructive, "trust": self.trust,
            "permission": self.permission_capability,
            "injection_flagged": self.injection_flagged,
        }


def _hash_schema(name: str, description: str, schema: dict) -> str:
    payload = repr((name, description, sorted(schema.items()) if schema else []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _permission_for(cap_id: str, annotations: dict) -> tuple[str, bool, bool]:
    """Decide what permission a remote tool needs.

    MCP annotations are HINTS FROM THE SERVER, i.e. from the thing being
    governed. They are used to make a tool *more* restricted, never less: a
    server claiming `readOnlyHint` does not get to lower its own tier. Unknown
    capabilities already default to ADMIN in core/permissions.py, which is the
    behaviour Nova wants — an unrecognised remote action requires confirmation.
    """
    read_only = bool(annotations.get("readOnlyHint"))
    destructive = bool(annotations.get("destructiveHint"))
    # The permission capability IS the namespaced id, so a future policy can
    # allowlist `mcp:github:search_issues` specifically without touching others.
    return cap_id, read_only, destructive


class CapabilityRegistry:
    """All MCP capabilities Nova currently knows about."""

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}
        self._by_server: dict[str, set[str]] = {}
        self.discoveries = 0
        self.invalidations = 0

    # -- ingest ---------------------------------------------------------------

    def replace_server(self, server_id: str, raw_tools: Iterable[dict]) -> dict[str, int]:
        """Install a server's tool list, replacing whatever it had before.

        Replace rather than merge: a server that no longer advertises a tool has
        removed it, and leaving a stale capability selectable would produce a
        call that fails at execution time for no visible reason.
        """
        server_id = sanitize_identifier(server_id)
        previous = set(self._by_server.get(server_id, set()))
        seen: set[str] = set()
        added = changed = 0

        for raw in raw_tools:
            if not isinstance(raw, dict):
                continue
            remote_name = str(raw.get("name") or "").strip()
            if not remote_name:
                continue

            cap_id = capability_id(server_id, remote_name)
            desc_raw = raw.get("description") or ""
            description = sanitize_text(desc_raw)
            schema = sanitize_schema(raw.get("inputSchema") or raw.get("input_schema") or {})
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            perm, read_only, destructive = _permission_for(cap_id, annotations)
            schema_hash = _hash_schema(remote_name, description, schema)

            flagged = looks_like_injection(desc_raw) or looks_like_injection(
                str(raw.get("inputSchema") or ""))
            if flagged:
                # Not blocked — the sanitiser already neutralised it, and
                # dropping a tool because its description tripped a regex would
                # be its own denial-of-service. Recorded so it is visible.
                logger.warning("mcp_injection_in_metadata", server=server_id,
                               tool=remote_name)

            existing = self._caps.get(cap_id)
            cap = Capability(
                cap_id=cap_id, server_id=server_id, remote_name=remote_name,
                description=description or f"MCP tool {remote_name}",
                schema=schema, schema_hash=schema_hash, permission_capability=perm,
                read_only=read_only, destructive=destructive, injection_flagged=flagged,
            )
            if existing is None:
                added += 1
            elif existing.schema_hash != schema_hash:
                changed += 1
                self.invalidations += 1
            self._caps[cap_id] = cap
            seen.add(cap_id)

        removed = previous - seen
        for cap_id in removed:
            self._caps.pop(cap_id, None)
        self._by_server[server_id] = seen
        self.discoveries += 1
        return {"added": added, "changed": changed, "removed": len(removed),
                "total": len(seen)}

    def drop_server(self, server_id: str) -> int:
        server_id = sanitize_identifier(server_id)
        ids = self._by_server.pop(server_id, set())
        for cap_id in ids:
            self._caps.pop(cap_id, None)
        return len(ids)

    # -- read -----------------------------------------------------------------

    def get(self, cap_id: str) -> Capability | None:
        return self._caps.get(cap_id)

    def all(self) -> list[Capability]:
        return list(self._caps.values())

    def for_server(self, server_id: str) -> list[Capability]:
        return [self._caps[i] for i in self._by_server.get(sanitize_identifier(server_id), set())
                if i in self._caps]

    def selector_descriptions(self) -> dict[str, str]:
        """Cheap metadata for ToolSelector — one line each, no schemas.

        This is the whole scaling story: selection sees N short strings, and the
        expensive JSON Schema is fetched only for whatever survives.
        """
        return {c.cap_id: c.selector_line() for c in self._caps.values()}

    def hydrate(self, cap_ids: Iterable[str]) -> dict[str, dict]:
        """Full schemas, for shortlisted capabilities only."""
        out: dict[str, dict] = {}
        for cap_id in cap_ids:
            cap = self._caps.get(cap_id)
            if cap is not None:
                out[cap_id] = {
                    "name": cap.cap_id,
                    "description": cap.description,
                    "input_schema": cap.schema,
                    "server": cap.server_id,
                    "trust": cap.trust,
                }
        return out

    def search(self, query: str, *, limit: int = 8) -> list[Capability]:
        """The capability.search escape hatch.

        Deliberately lexical and dependency-free: this runs when the semantic
        shortlist already failed, so falling back to the same ranking that just
        missed would be pointless. Cheap word overlap over names and
        descriptions finds the thing the model can name but the selector did not
        surface.
        """
        terms = {t for t in _tokens(query) if len(t) > 2}
        if not terms:
            return []
        scored: list[tuple[float, Capability]] = []
        for cap in self._caps.values():
            hay = _tokens(f"{cap.remote_name} {cap.server_id} {cap.description}")
            if not hay:
                continue
            overlap = len(terms & hay)
            if overlap:
                scored.append((overlap / len(terms), cap))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [c for _s, c in scored[:limit]]

    def stats(self) -> dict[str, Any]:
        return {
            "capabilities": len(self._caps),
            "servers": len([s for s, ids in self._by_server.items() if ids]),
            "discoveries": self.discoveries,
            "schema_invalidations": self.invalidations,
            "injection_flagged": sum(1 for c in self._caps.values() if c.injection_flagged),
        }


def _tokens(text: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))
