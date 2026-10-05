#!/usr/bin/env python3
"""Run the internal kudos portal.

    python app.py

Then open http://127.0.0.1:8000
"""

import logging
import os
from pathlib import Path

from kudos import KudosService
from server import serve

ROOT = Path(__file__).resolve().parent


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    db_path = os.environ.get("KUDOS_DB", str(ROOT / "kudos.db"))
    port = int(os.environ.get("KUDOS_PORT", "8000"))
    service = KudosService(db_path)
    server = serve(service, port=port)
    host, bound_port = server.server_address
    logging.getLogger("kudos").info("Kudos portal at http://%s:%s", host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
