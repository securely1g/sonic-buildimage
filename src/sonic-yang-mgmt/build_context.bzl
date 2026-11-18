"""Labels shared by the image workspace and the standalone YANG module."""

def build_context():
    """Preserve image-root labels while exposing the same owner targets alone."""
    in_image = native.package_name() == "src/sonic-yang-mgmt"
    models = "//src/sonic-yang-models:" if in_image else "@sonic_yang_models//:"
    return struct(
        models_payload = models + "yang_models",
        models_runtime = models + "sonic-yang-models.runtime",
        requirements_lock = "//src/sonic-config-engine:requirements_lock.txt" if in_image else "@sonic_config_engine_lock//file:requirements_lock.txt",
        source_test_visibility = ["//tools/bazel/tests:__pkg__"] if in_image else ["//:__pkg__"],
        wheel_strip_prefixes = ["src/sonic-yang-mgmt/"] if in_image else [],
    )
