#!/usr/bin/env python
"""Start the FastAPI backend (works on Linux, macOS and Windows).

  python scripts/run_api.py                 # http://127.0.0.1:8000
  python scripts/run_api.py --reload        # auto-reload on code changes
  python scripts/run_api.py --host 0.0.0.0 --port 8000

Equivalent to `uvicorn --factory industrial_ai.api.main:app_factory`, plus the event-loop
setting psycopg (openGauss) needs on Windows. Settings are read from .env in the current
directory, so run it from backend/.
"""

import argparse
import sys

import _common  # noqa: F401  (import path fallback)
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run(
        "industrial_ai.api.main:app_factory", factory=True, host=args.host, port=args.port,
        reload=args.reload, workers=None if args.reload else args.workers,
        # psycopg async cannot use the Windows Proactor loop.
        loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        log_level="info",
    )


if __name__ == "__main__":
    main()
