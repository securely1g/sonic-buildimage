"""Export upstream resolution state as the reviewed image input lock."""
import json
from pathlib import Path
root = Path(__file__).resolve().parent
matches = []
def visit(value):
    if isinstance(value, dict):
        if "lock_content" in value:
            lock = json.loads(value["lock_content"])
            if "mux_debug_debian" in lock.get("dependency_sets", {}):
                matches.append(lock)
        for child in value.values():
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)
visit(json.loads((root / "MODULE.bazel.lock").read_text()))
# Bazel can retain the same extension result for multiple evaluation keys.
for item in matches:
    item.pop("facts", None)
unique = {json.dumps(item, sort_keys=True): item for item in matches}
assert len(unique) == 1, "Resolve the dedicated refresh module first"
lock = next(iter(unique.values()))
assert set(lock["dependency_sets"]) == {"mux_runtime_debian", "mux_debug_debian", "mux_test_debian"}
assert set(lock["dependency_sets"]["mux_debug_debian"]["sets"]) == {"amd64"}
lock.pop("facts", None)
lock["_generated"] = {"architecture": "amd64", "generator": "rules_distroless apt.install; export from refresh/MODULE.bazel.lock", "scope": "Native AMD64 docker-mux runtime imports; no package producers"}
(root.parent / "apt.lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
