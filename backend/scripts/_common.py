"""Shared bits for the scripts (run them from the backend/ directory)."""

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))  # works without `pip install -e .` too

from app.config import Settings  # noqa: E402


def parser(doc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=doc,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env", help="settings file (default .env)")
    return p


def settings(args: argparse.Namespace, **overrides) -> Settings:
    env = Path(args.env_file)
    try:
        return Settings(_env_file=env if env.is_file() else None,
                        **{k: v for k, v in overrides.items() if v is not None})
    except Exception as exc:
        sys.exit(f"invalid configuration:\n{exc}")


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def skip(msg: str) -> None:
    print(f"  [SKIP] {msg}")
