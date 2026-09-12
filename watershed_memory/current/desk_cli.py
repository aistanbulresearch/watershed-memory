"""Serve one existing current case on loopback; acquisition runs separately."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

import uvicorn

from ..api import create_app
from ..service import Service
from .case_records import case_row
from .case_schema import ensure_schema
from .case_store import CaseStore
from .context import _case_config
from .desk import CurrentDesk


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--db", type=Path, required=True, help="Existing current-case database.")
    command.add_argument("--case", required=True, help="Case identity already registered in the database.")
    command.add_argument("--port", type=int, default=8771)
    command.add_argument("--replay-db", type=Path, help="Separate historical replay database; defaults to DB.replay.sqlite.")
    return command


def configured_app(args):
    path = args.db.resolve()
    replay = (args.replay_db or path.with_suffix(".replay.sqlite")).resolve()
    if not path.is_file():
        raise ValueError("Select an existing current-case database.")
    if path == replay or (replay.exists() and path.samefile(replay)):
        raise ValueError("Historical replay needs a separate database.")
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        raise ValueError("Choose a port between 1 and 65535.")
    # Prove an existing case before CaseStore is allowed to initialize anything.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'current_*' LIMIT 1").fetchone():
                raise ValueError("Select a database with an existing current case.")
            ensure_schema(db)
            _case_config(case_row(db, args.case))
    except (sqlite3.Error, KeyError) as error:
        raise ValueError("Select a supported database containing the requested current case.") from error
    cases = CaseStore(path)
    try:
        cases.context(args.case)
    except KeyError as error:
        raise ValueError("The selected case is not registered in this database.") from error
    return create_app(Service(replay), current_desk=CurrentDesk(cases, args.case))


def main():
    command = parser()
    args = command.parse_args()
    try:
        app = configured_app(args)
    except ValueError as error:
        command.error(str(error))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
