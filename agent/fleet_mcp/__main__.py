"""Run the wavebreak-fleet MCP server: `PYTHONPATH=. python -m agent.fleet_mcp`."""

from __future__ import annotations

import logging
import sys

from .config import Settings
from .server import build_fleet, create_server


def main() -> None:
    """Build settings from the environment and serve MCP over streamable HTTP."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings.from_env()
    log = logging.getLogger("wavebreak-fleet")
    log.info(
        "starting on %s:%s (lab_mock=%s, ledger=%s)",
        settings.mcp_host,
        settings.mcp_port,
        settings.lab_mock,
        settings.ledger_path,
    )
    create_server(build_fleet(settings), settings).run(transport="streamable-http")


if __name__ == "__main__":
    main()
