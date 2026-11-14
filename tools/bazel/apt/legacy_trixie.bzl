"""Keep image payload locks separate from the shared build-tool lock."""

load("@rules_distroless//apt:extensions.bzl", _apt = "apt")

apt = _apt
