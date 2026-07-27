"""SQLAlchemy plumbing: one engine, one session factory, one Base.

SQLite is the right store here (phase doc 5.5.2): single-writer host
service, small data, and the audit log gains durability from living
on the WSL2 host filesystem — outside the cluster's blast radius.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_URL

# check_same_thread=False: FastAPI may service requests on different
# threads; SQLAlchemy's connection pool serializes actual use.
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base — Alembic autogenerate diffs against its metadata."""
