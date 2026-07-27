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

# How long a pending request waits for a human before lapsing.
REQUEST_TTL_MINUTES = int(os.environ.get("SENTINEL_REQUEST_TTL_MINUTES", "10"))

# Default grant lifetime when the granter doesn't choose one (the GUI's
# buttons send 5 or 60 explicitly).
DEFAULT_GRANT_TTL_MINUTES = int(os.environ.get("SENTINEL_GRANT_TTL_MINUTES", "5"))

# Token prefix: makes credentials grep-able in logs/incident response
# (the industry "sk_"-style convention) without weakening entropy.
TOKEN_PREFIX = "snt_"
