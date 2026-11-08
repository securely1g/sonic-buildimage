"""Keep the existing Bookworm FIB request in its own APT namespace."""

load("@rules_distroless//apt:extensions.bzl", _apt = "apt")

apt = _apt
