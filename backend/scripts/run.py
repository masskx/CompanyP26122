#!/usr/bin/env python
"""Start the API (Linux, macOS, Windows). Same as `uvicorn app.main:app` plus the event-loop
setting openGauss (psycopg) needs on Windows.

  python scripts/run.py                 # http://127.0.0.1:8000/docs
  python scripts/run.py --reload
  python scripts/run.py --host 0.0.0.0 --port 8000
"""

import argparse
import sys

import _common  # noqa: F401
import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    a = p.parse_args()
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload,
                loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto")
