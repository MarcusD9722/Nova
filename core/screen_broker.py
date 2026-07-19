from __future__ import annotations

"""In-process request/response broker for agent-initiated screen looks (PI1).

Screen capture can only happen in the Electron frontend (desktopCapturer),
never the Python backend — so when the agent decides to look at the screen
mid-conversation, the backend publishes a request over the event bus and
waits here for the frontend to resolve it, after the user explicitly
approves via a confirm click. Ephemeral, in-memory only: nothing here needs
to survive a restart.
"""

import asyncio
from uuid import uuid4


class ScreenCaptureBroker:
    def __init__(self) -> None:
        self._pending: dict[str, "asyncio.Future[dict]"] = {}

    def new_request(self) -> tuple[str, "asyncio.Future[dict]"]:
        request_id = str(uuid4())
        fut: "asyncio.Future[dict]" = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        return request_id, fut

    def resolve(self, request_id: str, result: dict) -> bool:
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True

    def cancel(self, request_id: str) -> None:
        self._pending.pop(request_id, None)
