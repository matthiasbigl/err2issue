"""Entry point: `err2issue` or `python -m err2issue`."""

from __future__ import annotations

import logging
import sys

from .config import get_settings


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    log = logging.getLogger("err2issue")

    problems = settings.validation_errors()
    for problem in problems:
        log.error("configuration: %s", problem)
    if problems and settings.sink != "dry-run":
        log.error(
            "refusing to start with an unusable configuration; "
            "set E2I_SINK=dry-run to run without GitHub credentials"
        )
        return 2

    import uvicorn

    uvicorn.run(
        "err2issue.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
