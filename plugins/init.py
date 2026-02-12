from __future__ import annotations

"""Plugin import hub.

Importing this module registers tools via decorators at import-time.
Keep this file lightweight: only import plugin modules.
"""

# Importing these modules registers tools via decorators at import-time.
from . import discord  # noqa: F401
from . import google_maps  # noqa: F401
from . import weather  # noqa: F401
