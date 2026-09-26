"""ota-agent CLI.

  python3 -m ota_agent ddi                  field mode: poll hawkBit DDI forever
  python3 -m ota_agent serve                lab mode: local install API (token)
  python3 -m ota_agent install BUNDLE.tar   one-shot local install
  python3 -m ota_agent status               print status JSON

Settings come from the environment (the systemd unit loads /etc/wavebreak/identity.env and
/etc/wavebreak/ota-agent.env): DEVICE_ID, HAWKBIT_DDI_URL, HAWKBIT_TENANT, DDI_AUTH_MODE,
HAWKBIT_GATEWAY_TOKEN / HAWKBIT_TARGET_TOKEN, LAB_DEVICE_TOKEN, LOCAL_API_PORT, HEALTH_WINDOW_S.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .install import Installer


def build_installer(env: dict) -> Installer:
    return Installer(health_window_s=float(env.get("HEALTH_WINDOW_S", "15")))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ota-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddi", help="poll hawkBit DDI forever")
    s = sub.add_parser("serve", help="local install API (lab devices)")
    s.add_argument("--port", type=int, default=None)
    i = sub.add_parser("install", help="install a bundle tar")
    i.add_argument("bundle", type=Path)
    i.add_argument("--sha256", default=None)
    sub.add_parser("status", help="print status JSON")
    args = p.parse_args(argv)

    env = dict(os.environ)
    installer = build_installer(env)

    if args.cmd == "ddi":
        from .ddi import DdiAgent, config_from_env

        DdiAgent(config_from_env(env), installer).run_forever()
        return 0
    if args.cmd == "serve":
        from .local import serve

        port = args.port or int(env.get("LOCAL_API_PORT", "8081"))
        serve(installer, env.get("LAB_DEVICE_TOKEN", ""), port=port)
        return 0
    if args.cmd == "install":
        result = installer.install(args.bundle, expected_sha256=args.sha256)
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.ok else 1
    if args.cmd == "status":
        from .local import status

        print(json.dumps(status(installer), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
