"""SQLAlchemy plumbing: one engine, one session factory, one Base.

SQLite is the right store here (phase doc 5.5.2): single-writer host
service, small data, and the audit log gains durability from living
on the WSL2 host filesystem — outside the cluster's blast radius.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_URL

# check_same_thread=False: FastAPI may service requests on different
# threads; SQLAlchemy's connection pool serializes actual use.
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    # foreign_keys: SQLite ships enforcement OFF per connection. Without
    # this, capability_grants.flow_id would accept any string — a silent
    # hole under the security model.
    dbapi_conn.execute("PRAGMA foreign_keys=ON")
    # journal_mode=WAL + busy_timeout: TWO processes share this file (the
    # broker and the admin console), and the broker commits an audit row
    # on every single MCP call. In the default rollback-journal mode a
    # console read blocks that write; with no busy timeout the write
    # fails outright. The failure mode is vicious: the ext_authz call
    # times out, Envoy denies (failOpen is false), and the audit row
    # explaining why is the very write that got blocked. WAL lets
    # readers and one writer proceed together; the timeout absorbs the
    # rest.
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base — Alembic autogenerate diffs against its metadata."""
