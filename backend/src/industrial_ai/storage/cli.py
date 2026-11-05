"""Database tool.

    industrial-ai-db migrate     # apply pending migrations to OPENGAUSS_DSN
"""

import argparse
import asyncio
import sys

from industrial_ai.core.config import Settings
from industrial_ai.core.logging import configure_logging
from industrial_ai.storage.opengauss import DatabaseCompatibilityError, migrate


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="industrial-ai-db")
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level, settings.app_env, stream=sys.stderr)
    if settings.opengauss_dsn is None:
        sys.exit("OPENGAUSS_DSN is not set")
    try:
        applied = asyncio.run(migrate(settings.opengauss_dsn.get_secret_value()))
    except DatabaseCompatibilityError as exc:
        sys.exit(f"error: {exc}")
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))


if __name__ == "__main__":
    main()
