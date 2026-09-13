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
from .field_codec import decode
from .field_presence import require_legacy_context
from .field_records import require_location_versions
from .field_schema import ensure_schema as check_field_schema
from .field_store import FieldStore
from .field_types import FieldPrincipal
from .locations import LocationEntry, LocationRegistry


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--db", type=Path, required=True, help="Existing current-case database.")
    command.add_argument(
        "--case", required=True, help="Case identity already registered in the database."
    )
    command.add_argument("--port", type=int, default=8771)
    command.add_argument(
        "--replay-db",
        type=Path,
        help="Separate historical replay database; defaults to DB.replay.sqlite.",
    )
    command.add_argument(
        "--field-location",
        type=Path,
        action="append",
        default=[],
        help="Trusted canonical field-site JSON; repeat for saved site versions.",
    )
    command.add_argument(
        "--field-principal-id",
        help="Configured local operator with coordinator, field operator and verifier roles.",
    )
    return command


def _field_binding(args):
    if bool(args.field_location) != bool(args.field_principal_id):
        raise ValueError("Provide both trusted field locations and a local operator identity.")
    if not args.field_location:
        return None, None
    if len(args.field_location) > 1000:
        raise ValueError("Too many trusted field location versions.")
    try:
        entries = []
        for path in args.field_location:
            with path.open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError("location file exceeds its size bound")
            entries.append(decode(raw.decode("utf-8").strip(), LocationEntry))
        registry = LocationRegistry(tuple(entries))
        principal = FieldPrincipal(
            args.field_principal_id, ("COORDINATOR", "FIELD_OPERATOR", "VERIFIER"), (args.case,)
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(
            "Check the trusted field-site files and local operator identity."
        ) from error
    return registry, principal


def _replay_preflight(path):
    """Reject an unrelated or broken existing replay target without writing it."""
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError("Select a separate replay database file.")
    columns = {
        "sessions": (("id", "TEXT", 0, 1), ("revision", "INTEGER", 1, 0), ("state", "TEXT", 1, 0)),
        "requests": (
            ("session_id", "TEXT", 1, 1),
            ("request_id", "TEXT", 1, 2),
            ("digest", "TEXT", 1, 0),
            ("response", "TEXT", 1, 0),
        ),
        "claims": (
            ("session_id", "TEXT", 0, 1),
            ("request_id", "TEXT", 1, 0),
            ("digest", "TEXT", 1, 0),
            ("owner", "TEXT", 1, 0),
            ("expires", "REAL", 1, 0),
        ),
    }
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            objects = set(
                db.execute(
                    "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                    "AND type IN ('table','view','trigger')"
                )
            )
            if objects != {("table", name) for name in columns}:
                raise ValueError("Select a supported historical replay database.")
            if db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise ValueError("Historical replay database integrity check failed.")
            for table, expected in columns.items():
                actual = tuple(
                    (r[1], r[2], r[3], r[5]) for r in db.execute(f"PRAGMA table_info({table})")
                )
                foreign = list(db.execute(f"PRAGMA foreign_key_list({table})"))
                expected_foreign = (
                    []
                    if table == "sessions"
                    else [(0, 0, "sessions", "session_id", "id", "NO ACTION", "NO ACTION", "NONE")]
                )
                if actual != expected or foreign != expected_foreign:
                    raise ValueError("Select a supported historical replay database.")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Historical replay references are inconsistent.")
    except sqlite3.Error as error:
        raise ValueError("Select a readable historical replay database.") from error


def configured_app(args):
    path = args.db.resolve()
    replay = (args.replay_db or path.with_suffix(".replay.sqlite")).resolve()
    if not path.is_file():
        raise ValueError("Select an existing current-case database.")
    if path == replay or (replay.exists() and path.samefile(replay)):
        raise ValueError("Historical replay needs a separate database.")
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        raise ValueError("Choose a port between 1 and 65535.")
    locations, principal = _field_binding(args)
    _replay_preflight(replay)
    # Prove an existing case before CaseStore is allowed to initialize anything.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name GLOB 'current_*' LIMIT 1"
            ).fetchone():
                raise ValueError("Select a database with an existing current case.")
            ensure_schema(db)
            config = _case_config(case_row(db, args.case))
            if locations is None:
                require_legacy_context(db, args.case)
            else:
                if any(
                    site.kind != "FIELD_SITE"
                    or site.case_id != args.case
                    or site.simulated != config.simulated
                    for site in locations._entries
                ):
                    raise ValueError(
                        "Trusted field sites must match the selected case and simulation mode."
                    )
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name GLOB 'field_*' LIMIT 1"
                ).fetchone():
                    check_field_schema(db)
                    require_location_versions(db, args.case, locations._entries)
    except (sqlite3.Error, KeyError) as error:
        raise ValueError(
            "Select a supported database containing the requested current case."
        ) from error
    # Open the separate replay before any permitted primary-schema initialization.
    try:
        replay_service = Service(replay)
    except (OSError, sqlite3.Error) as error:
        raise ValueError("The separate historical replay database could not be opened.") from error
    cases = CaseStore(path)
    try:
        cases.context(args.case)
    except KeyError as error:
        raise ValueError("The selected case is not registered in this database.") from error
    fields = FieldStore(cases, locations) if locations is not None else None
    return create_app(
        replay_service,
        current_desk=CurrentDesk(
            cases,
            args.case,
            fields=fields,
            principal=principal,
        ),
    )


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
