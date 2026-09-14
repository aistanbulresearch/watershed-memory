"""Build an inspectable Linux ARM64 CodeZip from explicit public source and locked deps."""

import argparse
import hashlib
import json
import stat
import struct
import zipfile
from pathlib import Path

SOURCE_FILES = (
    "watershed_memory/__init__.py", "watershed_memory/catalog.py",
    "watershed_memory/planning.py", "watershed_memory/strands_agent.py",
    "watershed_memory/agentcore_protocol.py", "watershed_memory/data/observations.json",
    "watershed_memory/data/NOTICE.md", "runtime/entrypoint.py", "runtime/launch.py",
    "LICENSE", "THIRD_PARTY_NOTICES.md",
    "third_party/aws-otel-python-instrumentation/LICENSE",
    "third_party/aws-otel-python-instrumentation/NOTICE",
    "third_party/aws-otel-python-instrumentation/THIRD-PARTY-LICENSES",
)


def validate_native(name: str, content: bytes) -> None:
    """Reject host-native artifacts even when installed by a cross-platform tool."""
    if name.endswith((".exe", ".pyd", ".dll", ".node")) or content[:2] == b"MZ":
        raise ValueError(f"Windows binary in Runtime artifact: {name}")
    if content[:4] == b"\x7fELF":
        if len(content) < 20 or content[4:6] != b"\x02\x01":
            raise ValueError(f"Unsupported ELF format: {name}")
        if struct.unpack("<H", content[18:20])[0] != 183:
            raise ValueError(f"Non-ARM64 native dependency: {name}")
    elif name.endswith(".so"):
        raise ValueError(f"Native library is not ELF: {name}")


_PUBLIC_EXTENSIONS = {".py", ".json", ".md", ".html", ".css", ".js", ".mjs"}
_FIXED_PUBLIC_FILES = {
    "LICENSE", "THIRD_PARTY_NOTICES.md",
    "third_party/aws-otel-python-instrumentation/LICENSE",
    "third_party/aws-otel-python-instrumentation/NOTICE",
    "third_party/aws-otel-python-instrumentation/THIRD-PARTY-LICENSES",
    "runtime/entrypoint.py", "runtime/launch.py",
    "runtime/current_entrypoint.py", "runtime/current_launch.py",
}


def _link_or_junction(path: Path) -> bool:
    """Detect both symlinks and Windows junctions without following them."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _has_link_parent(path: Path, boundary: Path) -> bool:
    current = path
    while True:
        if _link_or_junction(current):
            return True
        if current == boundary:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _validate_source_name(name: object) -> str:
    if type(name) is not str or not name or not name.isascii():
        raise ValueError("Selected source names must be nonempty ASCII strings.")
    if "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ValueError(f"Non-canonical source path: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise ValueError(f"Non-canonical source path: {name!r}")
    if name.startswith("watershed_memory/"):
        if Path(name).suffix not in _PUBLIC_EXTENSIONS:
            raise ValueError(f"Unrecognized public source type: {name}")
    elif name not in _FIXED_PUBLIC_FILES:
        raise ValueError(f"Unrecognized public source path: {name}")
    if any(part.casefold() in {"agents.md", "project_state.md"} for part in parts):
        raise ValueError(f"Private file in artifact: {name}")
    return name


def build_package(root: Path, dependencies: Path, destination: Path, *,
                  source_files: tuple[str, ...] = SOURCE_FILES) -> dict:
    root, dependencies, destination = root.resolve(), dependencies.resolve(), destination.resolve()
    if type(source_files) is not tuple or not source_files:
        raise ValueError("source_files must be a nonempty tuple.")
    selected = [_validate_source_name(name) for name in source_files]
    lowered = [name.casefold() for name in selected]
    if len(set(lowered)) != len(lowered):
        raise ValueError("Selected source paths contain duplicate aliases.")
    manifest_path = destination.with_suffix(".manifest.json")
    if destination.exists() or manifest_path.exists():
        raise ValueError("Use a fresh artifact path; preserve previously inspected builds.")
    entries: dict[str, Path] = {}
    entry_aliases: set[str] = set()
    for path in sorted(dependencies.rglob("*")):
        if _link_or_junction(path):
            raise ValueError("Dependency cache contains link or junction indirection.")
        if not path.is_file():
            continue
        relative = path.relative_to(dependencies)
        if "__pycache__" in relative.parts or relative.parts[0] in ("bin", "Scripts"):
            continue
        if path.suffix == ".pyc":
            continue
        name = relative.as_posix()
        if _has_link_parent(path, dependencies) or not path.resolve().is_relative_to(dependencies):
            raise ValueError("Dependency link leaves the inspected artifact directory.")
        if not name or any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError(f"Non-canonical dependency path: {name}")
        alias = name.casefold()
        if (alias == "watershed_memory.py" or alias.startswith("watershed_memory/")
                or alias == "runtime.py" or alias.startswith("runtime/")):
            raise ValueError(f"Dependency shadows application namespace: {name}")
        if alias in entry_aliases:
            raise ValueError(f"Duplicate dependency archive path: {name}")
        entry_aliases.add(alias)
        entries[name] = path
    for name in selected:
        path = root / name
        if (not path.is_file() or _has_link_parent(path, root)
                or not path.resolve().is_relative_to(root)):
            raise ValueError(f"Missing regular public source file: {name}")
        alias = name.casefold()
        if alias in entry_aliases:
            raise ValueError(f"Dependency shadows application source: {name}")
        entry_aliases.add(alias)
        entries[name] = path
    if not any(name.startswith("aws_opentelemetry_distro-") for name in entries):
        raise ValueError("The artifact requires the locked AWS OpenTelemetry distribution.")
    manifest = []
    total = 0
    for name, path in sorted(entries.items()):
        if (name.startswith((".local/", "temp/", "Daily/"))
                or any(part.casefold() in {"agents.md", "project_state.md"}
                       for part in name.split("/"))):
            raise ValueError(f"Private file in artifact: {name}")
        content = path.read_bytes()
        validate_native(name, content)
        total += len(content)
        manifest.append({"path": name, "bytes": len(content),
                         "sha256": hashlib.sha256(content).hexdigest()})
    if total > 750 * 1024 * 1024:
        raise ValueError("Runtime artifact exceeds the uncompressed size limit.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for entry in manifest:
            name = entry["path"]
            content = entries[name].read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("Source changed during artifact construction; rebuild after writers stop.")
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    if destination.stat().st_size > 250 * 1024 * 1024:
        raise ValueError("Runtime artifact exceeds the compressed size limit.")
    result = {"schema_version": 1, "platform": "linux-aarch64", "python": "3.12",
              "artifact_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
              "compressed_bytes": destination.stat().st_size, "uncompressed_bytes": total,
              "entries": manifest}
    manifest_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_package(Path(__file__).resolve().parents[1], args.dependencies, args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "entries"}, indent=2))


if __name__ == "__main__":
    main()
