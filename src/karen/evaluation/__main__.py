"""python -m karen.evaluation prepare/run; commands never read personal memory."""

import argparse
import asyncio

from .datasets import prepare, prepare_next
from .runner import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    build.add_argument("--data-dir", required=True)
    build.add_argument("--chinese", default="tests/fixtures/evaluation_zh.json")
    build.add_argument("--output", required=True)
    expand = commands.add_parser("prepare-next")
    expand.add_argument("--data-dir", required=True)
    expand.add_argument("--previous", action="append", required=True)
    expand.add_argument("--output", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--manifest", required=True)
    execute.add_argument("--output", required=True)
    execute.add_argument("--split", choices=["all", "development", "heldout"], default="all")
    execute.add_argument("--suite", action="append", default=[])
    execute.add_argument("--case", action="append", default=[])
    execute.add_argument("--concurrency", type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = prepare(args.data_dir, args.chinese, args.output)
        print(f"Frozen {len(manifest['cases'])} cases")
    elif args.command == "prepare-next":
        manifest = prepare_next(args.data_dir, args.previous, args.output)
        print(f"Frozen {len(manifest['cases'])} unseen cases")
    else:
        results = asyncio.run(
            run(
                args.manifest,
                args.output,
                split=args.split,
                suites=args.suite,
                case_ids=args.case,
                concurrency=args.concurrency,
            )
        )
        raise SystemExit(0 if all(r["status"] == "passed" for r in results) else 1)


if __name__ == "__main__":
    main()
