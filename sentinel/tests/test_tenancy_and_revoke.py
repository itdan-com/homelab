"""7.2.1 (ADR-005): principals with TOFU sub-pinning, profile grants
(a SET of tools for a WINDOW, snapshotted at mint), per-grant and
per-flow revoke, and the deny-closed placeholder for flow-less grants.

Service-level where the property lives in the service, plus an admin
API pass for the new /v1/grants surface. Same harness conventions as
test_broker_flow: throwaway DB via SENTINEL_DB before any app import,
migrated with the real Alembic stack; unique ids per test so the file
coexists with the other suites in one pytest process.

    python -m pytest tests/        # or:  python -m pytest tests/test_tenancy_and_revoke.py
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SENTINEL_CONSOLE_HOSTS", "127.0.0.1,localhost,testserver")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select  # noqa: E402

_HERE = os.path.dirname(__file__)


def _migrate() -> None:
    cfg = Config(os.path.join(_HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_HERE, "..", "migrations"))
    command.upgrade(cfg, "head")


_migrate()

from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, AuditEventType, Flow  # noqa: E402
from app.service import (  # noqa: E402
    check_capability,
    get_or_create_principal,
    mint_profile_grant,
    revoke_flow,
    revoke_grant,
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _flow(s, fid: str) -> None:
    s.add(Flow(id=fid, agent="tenancy-test"))
    s.commit()


def test_principal_tofu_pin_then_mismatch_refused():
    email = f"{_uid('alice')}@example.com"
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email)
        assert p.idp_sub is None

        p = get_or_create_principal(s, email=email, idp_sub="sub-1")
        assert p.idp_sub == "sub-1"  # pinned on first sight of a sub

        # Same sub: fine. Different sub for the same email: refused.
        get_or_create_principal(s, email=email, idp_sub="sub-1")
        with pytest.raises(ValueError, match="idp-sub-mismatch"):
            get_or_create_principal(s, email=email, idp_sub="sub-2")

        rows = s.scalars(select(AuditEvent).where(
            AuditEvent.principal == email,
            AuditEvent.event_type == AuditEventType.AUTH_FAILURE,
        )).all()
        assert any((r.details or {}).get("anomaly") == "idp-sub-mismatch"
                   for r in rows), "the anomaly must leave an audit row"


def test_principal_email_normalized_and_creation_audited():
    email = f"{_uid('Case')}@Example.COM"
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email)
        assert p.email == email.lower()
        rows = s.scalars(select(AuditEvent).where(
            AuditEvent.principal == email.lower(),
            AuditEvent.event_type == AuditEventType.CREDENTIAL_ADDED,
        )).all()
        assert any((r.details or {}).get("kind") == "principal" for r in rows)


def test_profile_grant_covers_its_set_and_only_its_set():
    with SessionLocal() as s:
        fid = _uid("flow")
        _flow(s, fid)
        _, tok = mint_profile_grant(
            s, profile="github:write",
            tools=["github.create_pr", "github.merge_pr"],
            window_minutes=30, granted_by="tester", granted_via="confirm",
            flow_id=fid,
        )
        for covered in ("github.create_pr", "github.merge_pr"):
            ok, reason, grant = check_capability(s, token=tok, tool=covered,
                                                 flow_id=fid)
            assert (ok, reason) == (True, "ok"), covered
        # Outside the snapshot: denied, same reason as any scope miss.
        ok, reason, _ = check_capability(s, token=tok,
                                         tool="github.delete_repo", flow_id=fid)
        assert (ok, reason) == (False, "scope-mismatch")
        # Right tool, wrong flow: still flow-locked.
        ok, reason, _ = check_capability(s, token=tok, tool="github.create_pr",
                                         flow_id=_uid("other"))
        assert (ok, reason) == (False, "scope-mismatch")


def test_flowless_profile_grant_denies_closed_until_the_person_door_exists():
    """A principal-bound grant with no flow binding must be USELESS on
    today's flow-header path — its door is 7.3. Deny closed, not 'not
    yet checked'."""
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=f"{_uid('bob')}@example.com")
        _, tok = mint_profile_grant(
            s, profile="github:read", tools=["github.get_file_contents"],
            window_minutes=30, granted_by="tester", granted_via="confirm",
            principal=p,
        )
        ok, reason, _ = check_capability(s, token=tok, tool="github.get_file_contents",
                                         flow_id=_uid("any"))
        assert (ok, reason) == (False, "scope-mismatch")


def test_use_audit_carries_principal_and_profile():
    with SessionLocal() as s:
        email = f"{_uid('carol')}@example.com"
        p = get_or_create_principal(s, email=email)
        fid = _uid("flow")
        _flow(s, fid)
        _, tok = mint_profile_grant(
            s, profile="echo:say", tools=["echo.say"], window_minutes=5,
            granted_by="tester", granted_via="approve", principal=p, flow_id=fid,
        )
        ok, _, _ = check_capability(s, token=tok, tool="echo.say", flow_id=fid)
        assert ok
        use = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.USE,
            AuditEvent.flow_id == fid,
        )).all()
        assert use and use[-1].principal == email
        assert (use[-1].details or {}).get("profile") == "echo:say"


def test_revoke_grant_kills_midwindow_and_double_revoke_errors():
    with SessionLocal() as s:
        fid = _uid("flow")
        _flow(s, fid)
        grant, tok = mint_profile_grant(
            s, profile="github:write", tools=["github.create_pr"],
            window_minutes=60, granted_by="tester", granted_via="confirm",
            flow_id=fid,
        )
        ok, _, _ = check_capability(s, token=tok, tool="github.create_pr",
                                    flow_id=fid)
        assert ok

        revoke_grant(s, grant, by="operator-test", reason="mid-window stop")
        ok, reason, _ = check_capability(s, token=tok, tool="github.create_pr",
                                         flow_id=fid)
        assert (ok, reason) == (False, "revoked")

        with pytest.raises(ValueError, match="already revoked"):
            revoke_grant(s, grant, by="operator-test")

        rows = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.REVOCATION,
            AuditEvent.flow_id == fid,
        )).all()
        assert any((r.details or {}).get("cause") == "manual" for r in rows)


def test_revoke_flow_sweeps_all_live_grants_and_zero_is_success():
    with SessionLocal() as s:
        fid = _uid("flow")
        _flow(s, fid)
        toks = []
        for i in range(2):
            _, tok = mint_profile_grant(
                s, profile=f"set-{i}", tools=[f"svc.tool{i}"], window_minutes=30,
                granted_by="tester", granted_via="confirm", flow_id=fid,
            )
            toks.append(tok)

        assert revoke_flow(s, fid, by="operator-test") == 2
        for i, tok in enumerate(toks):
            ok, reason, _ = check_capability(s, token=tok, tool=f"svc.tool{i}",
                                             flow_id=fid)
            assert (ok, reason) == (False, "revoked")
        # Second sweep finds nothing — and that is a success, not an error.
        assert revoke_flow(s, fid, by="operator-test") == 0


def test_admin_grant_surface_and_api_revoke():
    """The new console-facing surface: list grants, revoke one over the
    API (guards + operator resolution included), 409 on the repeat."""
    from starlette.testclient import TestClient
    from app.main import app as admin_app
    from authkit import sign_in

    admin = TestClient(admin_app, base_url="https://testserver")
    CONSOLE = {"x-sentinel-console": "1"}
    sign_in(admin, username=_uid("op"), label="tenancy-key")

    with SessionLocal() as s:
        fid = _uid("flow")
        _flow(s, fid)
        grant, _ = mint_profile_grant(
            s, profile="github:write", tools=["github.create_pr"],
            window_minutes=30, granted_by="tester", granted_via="confirm",
            flow_id=fid,
        )
        gid = grant.id

    listing = admin.get("/v1/grants", params={"live": True, "flow_id": fid},
                        headers=CONSOLE)
    assert listing.status_code == 200
    rows = listing.json()
    assert [r["grant_id"] for r in rows] == [gid]
    assert rows[0]["tools"] == ["github.create_pr"]
    assert rows[0]["granted_via"] == "confirm"
    assert rows[0]["live"] is True

    r = admin.post(f"/v1/grants/{gid}/revoke", json={"reason": "api test"},
                   headers=CONSOLE)
    assert r.status_code == 200 and r.json()["grant_id"] == gid

    again = admin.post(f"/v1/grants/{gid}/revoke", json={}, headers=CONSOLE)
    assert again.status_code == 409

    missing = admin.post("/v1/grants/nope/revoke", json={}, headers=CONSOLE)
    assert missing.status_code == 404


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
