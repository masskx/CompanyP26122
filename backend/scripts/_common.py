"""Shared helpers for the scripts in this folder (not a public module)."""

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

try:
    import industrial_ai  # noqa: F401
except ImportError:  # running from a checkout without `pip install -e .`
    sys.path.insert(0, str(BACKEND_DIR / "src"))
    try:
        import industrial_ai  # noqa: F401
    except ImportError:
        sys.exit('industrial_ai is not importable. Run:  pip install -e ".[dev,mqtt]"  '
                 "from the backend/ directory first.")

from industrial_ai.core.config import Settings  # noqa: E402
from industrial_ai.core.logging import configure_logging  # noqa: E402


def add_env_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", default=".env",
                        help="settings file (default: .env in the current directory)")


def load_settings(args: argparse.Namespace, **overrides) -> Settings:
    env_file = Path(args.env_file)
    if not env_file.is_file() and args.env_file != ".env":
        sys.exit(f"env file not found: {env_file}")
    try:
        settings = Settings(_env_file=env_file if env_file.is_file() else None,
                            **{k: v for k, v in overrides.items() if v is not None})
    except Exception as exc:  # pydantic ValidationError: show the readable part
        sys.exit(f"invalid configuration:\n{exc}")
    configure_logging(settings.log_level, settings.app_env, stream=sys.stderr)
    return settings


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def skip(msg: str) -> None:
    print(f"  [SKIP] {msg}")


def header(title: str) -> None:
    print(f"\n== {title}")
