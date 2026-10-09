"""python -m karen.evaluation prepare/run; commands never read personal memory."""

import argparse
import asyncio

from .datasets import prepare, prepare_next
from .review import calibrate, freeze_v2, regrade
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
    upgrade = commands.add_parser("prepare-v2")
    upgrade.add_argument("--manifest", required=True)
    upgrade.add_argument("--output", required=True)
    calibration = commands.add_parser("calibrate")
    calibration.add_argument("--cases", default="tests/fixtures/evaluation_judge_calibration_v2.json")
    calibration.add_argument("--output", required=True)
    calibration.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    review = commands.add_parser("regrade")
    review.add_argument("--source", required=True)
    review.add_argument("--output", required=True)
    review.add_argument("--suite", action="append", default=[])
    review.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = prepare(args.data_dir, args.chinese, args.output)
        print(f"Frozen {len(manifest['cases'])} cases")
    elif args.command == "prepare-next":
        manifest = prepare_next(args.data_dir, args.previous, args.output)
        print(f"Frozen {len(manifest['cases'])} unseen cases")
    elif args.command == "prepare-v2":
        freeze_v2(args.manifest, args.output)
    elif args.command == "calibrate":
        result = asyncio.run(calibrate(args.cases, args.output, concurrency=args.concurrency))
        raise SystemExit(0 if result["calibrated_on_curated_set"] else 1)
    elif args.command == "regrade":
        asyncio.run(regrade(args.source, args.output, suites=args.suite, concurrency=args.concurrency))
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
