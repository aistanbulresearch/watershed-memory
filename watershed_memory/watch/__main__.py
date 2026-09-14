"""Explicit bounded command line for the acquisition-only watch runner."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from .runner import WatchRunner
from .store import MonitorConfig, WatchStore
from .usgs import DEFAULT_STATION, GALLINAS_SERIES, USGSClient


def _parser():
    parser = argparse.ArgumentParser(
        description=__doc__
        + " A run deadline stops admitting new polls; an active bounded HTTP fetch may finish."
    )
    parser.add_argument("command", choices=("init", "poll", "run", "status", "events"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--start")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument(
        "--overlap-seconds",
        type=int,
        default=7200,
        help="Gallinas late-publication lookback (default: two hours)",
    )
    parser.add_argument("--max-polls", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--limit", type=int, default=100)
    return parser


def _start(value: str) -> datetime:
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("start must be RFC3339 with an explicit timezone")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return {key: _json_value(item) for key, item in value.__dict__.items()}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _main(argv=None):
    args = _parser().parse_args(argv)
    if not args.db.exists() and args.command != "init":
        raise SystemExit("the database must already exist for this command")
    if args.command == "run" and (args.max_polls is None or args.max_seconds is None):
        raise SystemExit("run requires --max-polls and --max-seconds")
    if args.command == "run":
        if type(args.max_polls) is not int or not 1 <= args.max_polls <= 100:
            raise SystemExit("max-polls must be an integer from 1 to 100")
        if type(args.max_seconds) is not int or not 1 <= args.max_seconds <= 21600:
            raise SystemExit("max-seconds must be an integer from 1 to 21600")
    if args.command == "events" and (type(args.limit) is not int or not 1 <= args.limit <= 100):
        raise SystemExit("limit must be between 1 and 100")
    start = None
    if args.command == "init":
        if not args.case_id or not args.start:
            raise SystemExit("init requires --case-id and --start")
        try:
            start = _start(args.start)
            if type(args.poll_seconds) is not int or not 1 <= args.poll_seconds <= 86400:
                raise ValueError("poll-seconds is outside the bounded range")
            if type(args.interval_seconds) is not int or not 1 <= args.interval_seconds <= 86400:
                raise ValueError("interval-seconds is outside the bounded range")
            if type(args.overlap_seconds) is not int or not 1 <= args.overlap_seconds < 86400:
                raise ValueError("overlap-seconds is outside the bounded range")
        except ValueError as error:
            raise SystemExit(str(error)) from error
    config = None
    if args.command == "init":
        try:
            config = MonitorConfig(
                monitor_id=args.monitor,
                station_id=DEFAULT_STATION,
                case_id=args.case_id,
                start_at=start,
                series=GALLINAS_SERIES,
                poll_seconds=args.poll_seconds,
                interval_seconds=args.interval_seconds,
                overlap_seconds=args.overlap_seconds,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    store = WatchStore(args.db)
    if args.command == "init":
        store.register(config, now=start)
        print(
            json.dumps(
                {"status": "initialized", "monitor_id": args.monitor, "case_id": args.case_id}
            )
        )
        return
    if args.command == "status":
        print(json.dumps(_json_value(store.status(args.monitor)), sort_keys=True))
        return
    if args.command == "events":
        print(
            json.dumps(
                _json_value(store.pending_events(args.monitor, limit=args.limit)), sort_keys=True
            )
        )
        return
    config = store.get_config(args.monitor)
    if config.station_id != DEFAULT_STATION or config.series != GALLINAS_SERIES:
        raise SystemExit("the runner only permits the configured Gallinas USGS source")
    runner = WatchRunner(store, USGSClient(station_id=config.station_id, specs=config.series))
    if args.command == "poll":
        print(json.dumps(_json_value(runner.tick(args.monitor)), sort_keys=True))
        return
    print(
        json.dumps(
            _json_value(
                runner.run(args.monitor, max_polls=args.max_polls, max_seconds=args.max_seconds)
            ),
            sort_keys=True,
        )
    )


def main(argv=None):
    try:
        return _main(argv) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
