"""Environment-driven settings.

Kept deliberately tiny: Sentinel is a trust anchor, and every knob is
attack surface. Add settings only when a checklist item needs them.
"""

import os

# SQLite database path. Dev default is repo-local (gitignored);
# production (5.5.7) sets SENTINEL_DB=/var/lib/sentinel/sentinel.db
# via the systemd unit.
DB_PATH = os.environ.get("SENTINEL_DB", "./sentinel-dev.db")
DB_URL = f"sqlite+pysqlite:///{DB_PATH}"
