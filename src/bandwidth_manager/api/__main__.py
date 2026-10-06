"""Run the control-plane API. Linux counters and tc are used as configured."""

import logging
import sys

import uvicorn

from bandwidth_manager.api.app import create_app
from bandwidth_manager.api.runtime import build_runtime
from bandwidth_manager.api.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    uvicorn.run(create_app(build_runtime(settings)), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
