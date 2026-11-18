"""Command line entry point for benchreconcile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import DEFAULT_BOOTSTRAP, DEFAULT_SEED, audit, format_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchreconcile",
        description=(
            "Recompute a benchmark repo's medians from its raw samples and "
            "compare them with the committed aggregates and README claims."
        ),
    )
    parser.add_argument("repo", help="path to the repository under audit")
    parser.add_argument(
        "--json",
        dest="json_out",
        metavar="PATH",
        help="write the full machine-readable report to PATH ('-' for stdout)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=DEFAULT_BOOTSTRAP,
        help=f"bootstrap rounds for the 95%% CI (default {DEFAULT_BOOTSTRAP})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"bootstrap seed (default {DEFAULT_SEED})",
    )
    args = parser.parse_args(argv)

    report = audit(args.repo, bootstrap=args.bootstrap, seed=args.seed)
    print(format_report(report))

    if args.json_out:
        payload = report.to_dict()
        if args.json_out == "-":
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            Path(args.json_out).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    failed = not report.ok or (args.strict and report.warnings)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())