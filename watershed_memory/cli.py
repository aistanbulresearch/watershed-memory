"""Local operator workspace entry point."""

import argparse
from pathlib import Path

import uvicorn

from .api import create_app
from .service import Service


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Watershed Memory operator workspace.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=Path(".local/runtime/cases.sqlite"))
    args = parser.parse_args()
    uvicorn.run(create_app(Service(args.database)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
