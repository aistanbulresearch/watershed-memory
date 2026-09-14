"""Build the fixed public current-v3 Runtime artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .build_runtime import build_package


def source_inventory() -> tuple[str, ...]:
    path = Path(__file__).with_name("current_sources.json")
    values = json.loads(path.read_text(encoding="utf-8"))
    if type(values) is not list or not values or any(type(item) is not str for item in values):
        raise ValueError("current source inventory must be a nonempty string list")
    if values != sorted(set(values)):
        raise ValueError("current source inventory must be sorted and unique")
    return tuple(values)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_package(
        Path(__file__).resolve().parents[1], args.dependencies, args.output,
        source_files=source_inventory(),
    )
    print(json.dumps({key: value for key, value in result.items() if key != "entries"}, indent=2))


if __name__ == "__main__":
    main()
