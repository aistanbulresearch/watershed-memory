import json
from pathlib import Path

from deployment import build_current_runtime as cli


def test_inventory_is_fixed_and_contains_current_pair():
    names = cli.source_inventory()
    assert len(names) == 125
    assert names == tuple(sorted(names))
    assert "runtime/current_entrypoint.py" in names
    assert "runtime/current_launch.py" in names
    assert "runtime/entrypoint.py" not in names
    assert "runtime/launch.py" not in names
    assert all(not name.startswith((".local/", "temp/")) for name in names)


def test_main_forwards_fixed_inventory_without_rebuilding(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_build(root, dependencies, destination, *, source_files):
        calls.update(root=root, dependencies=dependencies, destination=destination, source_files=source_files)
        return {"artifact_sha256": "x", "entries": []}

    monkeypatch.setattr(cli, "build_package", fake_build)
    cli.main(["--dependencies", str(tmp_path / "deps"), "--output", str(tmp_path / "artifact.zip")])
    assert calls["root"] == Path(cli.__file__).resolve().parents[1]
    assert calls["source_files"] == cli.source_inventory()
    assert json.loads(capsys.readouterr().out)["artifact_sha256"] == "x"
