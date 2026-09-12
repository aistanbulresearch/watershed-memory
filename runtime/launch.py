"""Portable equivalent of the OpenTelemetry console launcher for CodeZip."""

import sys
from pathlib import Path


def main() -> None:
    # Cross-platform uv installs can generate Windows .exe launchers. Execute the
    # installed, pinned Python entry function instead of packaging those binaries.
    from opentelemetry.instrumentation.auto_instrumentation import run

    sys.argv = ["opentelemetry-instrument", sys.executable,
                str(Path(__file__).with_name("entrypoint.py"))]
    run()


if __name__ == "__main__":
    main()
