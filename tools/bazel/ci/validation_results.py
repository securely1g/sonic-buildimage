"""Check the result of every VS validation against the selection that scheduled it."""

import json
import os


def verify(jobs):
    producer = jobs["vs_container"]
    expected = {"vs_container": True, "checks": True, "installer_validation": True}
    for name in expected:
        if jobs[name]["result"] != "success":
            raise ValueError(f"{name} did not succeed: {jobs[name]['result']}")

    outputs = producer["outputs"]
    groups = json.loads(outputs["plan"])["groups"]
    source_groups = json.loads(jobs["checks"]["outputs"]["plan"])["groups"]
    for name in ("archive", "package_amd64", "kernel_packages_amd64", "frr_python_amd64", "yang_models"):
        expected[name] = groups[name]
    expected["kernel_arm64"] = source_groups["kernel_arm64"]
    expected["native_sysmgr_amd64"] = bool(outputs["sysmgr_artifact_id"]) and groups["native_sysmgr_amd64"]
    expected["validate_component"] = bool(json.loads(outputs["component_matrix"])["include"])
    expected["validate_container"] = bool(json.loads(outputs["containers"])["include"])

    if set(expected) != set(jobs):
        raise ValueError(f"Unaccounted validation dependencies: {sorted(set(jobs) ^ set(expected))}")
    for name, selected in expected.items():
        if type(selected) is not bool:
            raise ValueError(f"Invalid validation selection for {name}")
        required = "success" if selected else "skipped"
        result = jobs[name]["result"]
        if result != required:
            raise ValueError(f"{name}: expected {required}, got {result}")
        print(f"{name}: {result}")


if __name__ == "__main__":
    verify(json.loads(os.environ["VALIDATION_JOBS"]))
