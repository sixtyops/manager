import json
from pathlib import Path

from fastapi.routing import APIRoute

from updater.app import app


def route_auth_map():
    result = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        auth = set()

        def visit(dependency):
            call = dependency.call
            name = getattr(call, "__name__", "")
            if name in {"require_auth", "require_inventory_read"}:
                auth.add(name)
            elif name == "_check":
                roles = next(
                    (cell.cell_contents for cell in (call.__closure__ or ())
                     if isinstance(cell.cell_contents, tuple)),
                    (),
                )
                auth.add("require_role:" + ",".join(roles))
            for child in dependency.dependencies:
                visit(child)

        visit(route.dependant)
        key = ",".join(sorted(route.methods or [])) + " " + route.path
        result[key] = sorted(auth)
    return dict(sorted(result.items()))


def test_route_auth_matches_pre_gating_snapshot():
    baseline_path = Path(__file__).parent / "data" / "route_auth_baseline.json"
    baseline = json.loads(baseline_path.read_text())
    assert route_auth_map() == baseline
