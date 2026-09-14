"""The separately packaged deployment entrypoint imports and runs its fixed app."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from watershed_memory.current import runtime_config


@pytest.mark.parametrize("as_main", [False, True])
def test_current_entrypoint_uses_absolute_package_import_and_exposes_app(monkeypatch, as_main):
    events = []
    app = SimpleNamespace(run=lambda: events.append("run"))
    def factory():
        events.append("create")
        return app
    monkeypatch.setattr(runtime_config, "create_production_app", factory)
    path = Path(__file__).resolve().parents[1] / "runtime" / "current_entrypoint.py"
    scope = runpy.run_path(str(path), run_name="__main__" if as_main else "deployment_entrypoint")
    assert scope["app"] is app
    assert events == (["create", "run"] if as_main else ["create"])
