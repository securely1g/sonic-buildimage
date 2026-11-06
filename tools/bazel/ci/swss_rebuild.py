#!/usr/bin/env python3
"""Keep the existing SWSS probe entry point while editing the selected owner."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.swss_image_rebuild import main


if __name__ == "__main__":
    main()
