"""Fixed OpenTelemetry launcher for the current runtime entrypoint."""

import sys
from pathlib import Path


def main() -> None:
    from opentelemetry.instrumentation import auto_instrumentation

    target = Path(__file__).with_name("current_entrypoint.py")
    sys.argv[:] = ["opentelemetry-instrument", sys.executable, str(target)]
    auto_instrumentation.run()


if __name__ == "__main__":
    main()
