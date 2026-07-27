"""Make `app` importable when tests run from anywhere (adds the
sentinel/ project root to sys.path)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
