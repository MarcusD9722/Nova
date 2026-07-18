from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = (os.getenv("NOVA_HOST", "127.0.0.1").strip() or "127.0.0.1")
    port = int(os.getenv("NOVA_PORT", "8008").strip() or "8008")
    log_level = (os.getenv("NOVA_LOG_LEVEL", "INFO").strip() or "INFO").lower()

    # Import app lazily so env vars are set before config loads.
    from backend.app import app  # noqa: WPS433

    uvicorn.run(app, host=host, port=port, log_level=log_level, access_log=False)


if __name__ == "__main__":
    main()