"""Apply or clear lab HTB rules. Bandwidth arguments are bits per second."""

import argparse
import logging
import sys
from pathlib import Path

from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.manager import manager_from_config

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce lab bandwidth limits with tc HTB.")
    parser.add_argument("--config", type=Path, default=Path("config/qos.example.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="action", required=True)

    limit = sub.add_parser("apply-limit", help="set one client's ceiling in bits per second")
    limit.add_argument("client")
    limit.add_argument("bandwidth_bps", type=int)

    remove = sub.add_parser("remove-limit", help="remove one client's ceiling")
    remove.add_argument("client")

    allocation = sub.add_parser("apply-allocation", help="client=bits pairs, for example client-b=20000000")
    allocation.add_argument("pairs", nargs="+")

    sub.add_parser("clear", help="delete HTB root qdiscs on the configured lab interfaces")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if not args.config.is_file():
        logger.error("config not found: %s", args.config)
        return 2
    config = load_qos_config(args.config)
    manager = manager_from_config(config, dry_run=True if args.dry_run else None)

    if args.action == "apply-limit":
        result = manager.apply_limit(args.client, args.bandwidth_bps)
    elif args.action == "remove-limit":
        result = manager.remove_limit(args.client)
    elif args.action == "apply-allocation":
        policy: dict[str, int] = {}
        for pair in args.pairs:
            client_id, separator, raw_rate = pair.partition("=")
            if separator != "=" or not raw_rate.isdecimal():
                logger.error("expected client=bits, got %s", pair)
                return 2
            policy[client_id] = int(raw_rate)
        result = manager.apply_allocation(policy)
    else:
        result = manager.clear_all_rules()

    for command in result.commands:
        print(" ".join(command))
    if result.errors:
        for error in result.errors:
            logger.error("%s", error)
    if result.dry_run:
        print("dry-run: not enforced")
        return 0
    if not result.enforced:
        print("not enforced")
        return 1
    print("enforced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
