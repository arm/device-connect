"""Entry point: python -m device_connect_server.portal"""

import logging

from aiohttp import web

from .app import create_app
from .config import PORTAL_HOST, PORTAL_PORT


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )

    app = create_app()
    logging.getLogger(__name__).info(
        "Starting Device Connect Portal on %s:%s", PORTAL_HOST, PORTAL_PORT,
    )
    web.run_app(app, host=PORTAL_HOST, port=PORTAL_PORT)


if __name__ == "__main__":
    main()
