"""Check read access to the native runners' Bazel content-addressable store."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from tools.bazel.ci.preserve_setup_bazel_cache import PRIVATE_REMOTE_CACHE


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
FIND_MISSING = "/build.bazel.remote.execution.v2.ContentAddressableStorage/FindMissingBlobs"
# REAPI v2 FindMissingBlobsRequest.blob_digests (field 2) contains one
# 66-byte Digest message. Its hash (field 1) is the 64 ASCII hex digits.
# size_bytes=0 and the default instance are omitted by protobuf.
EMPTY_BLOB_REQUEST = b"\x12\x42\x0a\x40" + EMPTY_SHA256.encode("ascii")


def probe(grpc, output):
    endpoint = PRIVATE_REMOTE_CACHE
    scheme, _, target = endpoint.partition("://")
    if scheme != "grpc" or not target:
        raise ValueError("The native cache probe requires a grpc:// endpoint")

    try:
        with grpc.insecure_channel(target) as channel:
            lookup = channel.unary_unary(FIND_MISSING)
            response = lookup(EMPTY_BLOB_REQUEST, timeout=15, wait_for_ready=True)
        # The default FindMissingBlobsResponse has no missing_blob_digests.
        status = "OK" if response == b"" else "UNEXPECTED_FIND_MISSING_BLOBS_RESPONSE"
    except grpc.RpcError as error:
        status = error.code().name

    receipt = {"endpoint": endpoint, "digest": EMPTY_SHA256, "status": status}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"Bazel CAS FindMissingBlobs at {endpoint}: {status}")
    return status == "OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import grpc  # Installed by the native Make workflow.

    return 0 if probe(grpc, args.output) else 1


if __name__ == "__main__":
    sys.exit(main())
