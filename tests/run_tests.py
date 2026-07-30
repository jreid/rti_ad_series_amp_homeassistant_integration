#!/usr/bin/env python3
"""Run the whole suite without any third-party dependencies.

    python3 tests/run_tests.py

The tests are also plain pytest-compatible functions, so `pytest tests/` works
once pytest is available -- no pytest-asyncio needed, since each async case is
driven by asyncio.run inside a sync wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import test_config_flow  # noqa: E402
import test_coordinator  # noqa: E402
import test_media_player  # noqa: E402
import test_number  # noqa: E402
import test_protocol  # noqa: E402

if __name__ == "__main__":
    sys.exit(
        harness.main(
            test_protocol,
            test_coordinator,
            test_config_flow,
            test_media_player,
            test_number,
        )
    )
