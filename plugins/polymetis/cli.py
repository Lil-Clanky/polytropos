"""``hermes polymetis``: the report on what Polymetis decided and what the models used."""

import argparse
import sys

from . import hooks, log, report


def setup_parser(parser: argparse.ArgumentParser) -> None:
    # The dest must differ from Hermes's own `command`, which names `polymetis` itself.
    sub = parser.add_subparsers(dest="polymetis_command", required=True, metavar="<command>")
    p = sub.add_parser("report", help="show what Polymetis decided and which skills the models used")
    p.add_argument("--days", type=int, default=30, help="look at the last N days (default 30)")


def handle(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    if args.polymetis_command == "report":
        records = log.read(hooks.log_path())
        out.write(report.render(records, days=args.days, catalogs=hooks.catalogs()) + "\n")
        out.write(f"Log: {hooks.log_path()}\n")
    return 0
