"""7.8.1 (ADR-008 D1/D3): the issuer-qualified pin, the migration
window, and the offboarding switch.

What is proven:
1. The pin is (issuer, sub): the same subject asserted by a DIFFERENT
   issuer refuses — email is never the join key across issuers.
2. A pre-7.8.1 row (sub pinned, issuer column empty) backfills its
   issuer on the next matching sign-in, audited, without ceremony.
3. The migration window is the ONE sanctioned re-pin path: open it for
   an issuer and a mismatched sign-in from exactly that issuer re-pins
   (audited with old/new); any other issuer still refuses; an expired
   window refuses; closing is audited.
4. `disabled_at` has a writer now, and it works end to end: the
   service flips it with an audit row, and a disabled person's next
   sign-in refuses.
5. The admin surface (/v1/principals, /disabled, /v1/idp-migration)
   works behind the real console guards.

Run: python -m pytest tests/test_idp_federation.py -q
"""
import os
import sys
import tempfile
import uuid
from datetime import timedelta
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
from app.models import (  # noqa: E402
    AuditEvent,
    AuditEventType,
    IdpMigration,
    Principal,
    utcnow,
)
from app.service import (  # noqa: E402
    close_idp_migration,
    get_or_create_principal,
    idp_migration_active,
    open_idp_migration,
    set_principal_disabled,
)

ISS_A = "https://authentik.lab.local/application/o/mcp/"
ISS_B = "https://acme.okta.com"


def _email() -> str:
    return f"person-{uuid.uuid4().hex[:8]}@example.com"


def _sub(tag: str = "sub") -> str:
    """Unique per call: the composite (iss, sub) unique is REAL in the
    suite-shared DB, so constant sub strings collide across tests —
    exactly the anomaly the constraint exists to catch."""
    return f"{tag}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _no_window():
    """Each test starts and ends without a migration window — a leaked
    window would let a later test's mismatch silently re-pin."""
    with SessionLocal() as s:
        m = s.get(IdpMigration, 1)
        if m:
            s.delete(m)
            s.commit()
    yield
    with SessionLocal() as s:
        m = s.get(IdpMigration, 1)
        if m:
            s.delete(m)
            s.commit()


def _last_audit(s, email, kind_key="kind"):
    row = s.scalars(select(AuditEvent).where(AuditEvent.principal == email)
                    .order_by(AuditEvent.id.desc())).first()
    return row


def test_pin_is_issuer_qualified():
    email = _email()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub="sub-1", idp_iss=ISS_A)
        # same sub, different issuer: refused — a sub is only meaningful
        # relative to who asserted it
        with pytest.raises(ValueError, match="idp-sub-mismatch"):
            get_or_create_principal(s, email=email, idp_sub="sub-1",
                                    idp_iss=ISS_B)
    with SessionLocal() as s:
        row = _last_audit(s, email)
        assert row.event_type == AuditEventType.AUTH_FAILURE
        assert row.details["pinned_iss"] == ISS_A
        assert row.details["token_iss"] == ISS_B


def test_legacy_row_backfills_issuer():
    email = _email()
    with SessionLocal() as s:
        # a pre-b6e4d1a8c3f2 row: sub pinned, issuer column empty
        p = get_or_create_principal(s, email=email, idp_sub="sub-legacy")
        assert p.idp_iss is None
        p = get_or_create_principal(s, email=email, idp_sub="sub-legacy",
                                    idp_iss=ISS_A)
        assert p.idp_iss == ISS_A
        row = _last_audit(s, email)
        assert row.details.get("kind") == "principal-iss-backfill"


def test_migration_window_is_the_one_sanctioned_repin():
    email = _email()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub="old-sub",
                                idp_iss=ISS_A)
        # without a window: refused
        with pytest.raises(ValueError):
            get_or_create_principal(s, email=email, idp_sub="new-sub",
                                    idp_iss=ISS_B)
        open_idp_migration(s, new_issuer=ISS_B, actor="op-test")
        # wrong issuer during the window: still refused
        with pytest.raises(ValueError):
            get_or_create_principal(s, email=email, idp_sub="evil-sub",
                                    idp_iss="https://evil.example")
        # the declared issuer: re-pins, audited with old and new
        p = get_or_create_principal(s, email=email, idp_sub="new-sub",
                                    idp_iss=ISS_B,
                                    idp_stable_id="oid:abc@tenant")
        assert (p.idp_iss, p.idp_sub) == (ISS_B, "new-sub")
        assert p.idp_stable_id == "oid:abc@tenant"
        row = _last_audit(s, email)
        assert row.details["kind"] == "principal-sub-repin"
        assert row.details["old_iss"] == ISS_A
        assert row.details["old_sub"] == "old-sub"
        assert row.details["migration_opened_by"] == "op-test"


def test_expired_window_refuses():
    email = _email()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub=_sub(), idp_iss=ISS_A)
        m = open_idp_migration(s, new_issuer=ISS_B, actor="op-test")
        m.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
        assert idp_migration_active(s) is None
        with pytest.raises(ValueError):
            get_or_create_principal(s, email=email, idp_sub=_sub(),
                                    idp_iss=ISS_B)


def test_close_is_audited():
    with SessionLocal() as s:
        open_idp_migration(s, new_issuer=ISS_B, actor="op-a")
        assert close_idp_migration(s, actor="op-b") is True
        assert close_idp_migration(s, actor="op-b") is False
        row = s.scalars(select(AuditEvent)
                        .where(AuditEvent.event_type == AuditEventType.POLICY_CHANGE)
                        .order_by(AuditEvent.id.desc())).first()
        assert row.details["action"] == "idp-migration-closed"


def test_disabled_at_finally_has_a_writer():
    email = _email()
    with SessionLocal() as s:
        p = get_or_create_principal(s, email=email, idp_sub="s", idp_iss=ISS_A)
        pid = p.id
        set_principal_disabled(s, pid, disabled=True, actor="op-test")
        assert s.get(Principal, pid).disabled_at is not None
        row = _last_audit(s, email)
        assert row.details["action"] == "principal-disabled"
        with pytest.raises(ValueError, match="principal-disabled"):
            get_or_create_principal(s, email=email, idp_sub="s", idp_iss=ISS_A)
        set_principal_disabled(s, pid, disabled=False, actor="op-test")
        assert s.get(Principal, pid).disabled_at is None
        # and they can sign in again
        get_or_create_principal(s, email=email, idp_sub="s", idp_iss=ISS_A)


def test_admin_surface_behind_real_guards():
    from starlette.testclient import TestClient
    from app.main import app as admin_app
    from authkit import sign_in

    admin = TestClient(admin_app, base_url="https://testserver")
    CONSOLE = {"x-sentinel-console": "1"}
    sign_in(admin, username=f"op-{uuid.uuid4().hex[:6]}", label="fed-key")

    email = _email()
    with SessionLocal() as s:
        pid = get_or_create_principal(s, email=email, idp_sub="s1",
                                      idp_iss=ISS_A).id

    rows = admin.get("/v1/principals", headers=CONSOLE)
    assert rows.status_code == 200
    mine = [r for r in rows.json() if r["email"] == email]
    assert mine and mine[0]["disabled"] is False and mine[0]["idp_iss"] == ISS_A

    r = admin.post(f"/v1/principals/{pid}/disabled",
                   json={"disabled": True}, headers=CONSOLE)
    assert r.status_code == 200 and r.json()["disabled"] is True

    r = admin.post("/v1/principals/nope/disabled",
                   json={"disabled": True}, headers=CONSOLE)
    assert r.status_code == 404

    assert admin.get("/v1/idp-migration", headers=CONSOLE).json() == {"active": False}
    r = admin.post("/v1/idp-migration",
                   json={"new_issuer": ISS_B, "ttl_hours": 2}, headers=CONSOLE)
    assert r.status_code == 200 and r.json()["new_issuer"] == ISS_B
    assert admin.get("/v1/idp-migration", headers=CONSOLE).json()["active"] is True
    r = admin.request("DELETE", "/v1/idp-migration", headers=CONSOLE)
    assert r.status_code == 200 and r.json()["was_open"] is True


def test_same_issuer_window_is_refused():
    """Review-proven attack: a window naming the CURRENT issuer would
    convert the re-issued-mailbox TOFU defense into a 24h silent
    re-bind. Refused at the service (and 409 at the endpoint)."""
    from app.config import OIDC_ISSUER
    with SessionLocal() as s:
        with pytest.raises(ValueError, match="same-issuer-window"):
            open_idp_migration(s, new_issuer=OIDC_ISSUER, actor="op")
        # trailing-slash games do not dodge the refusal
        with pytest.raises(ValueError, match="same-issuer-window"):
            open_idp_migration(s, new_issuer=OIDC_ISSUER.rstrip("/"),
                               actor="op")
        assert idp_migration_active(s) is None


def test_window_issuer_comparison_normalizes_trailing_slash():
    """An operator typing the issuer with (or without) a trailing slash
    must not open a window that silently never matches (review-caught:
    Authentik issuers end in '/', Okta's do not)."""
    email = _email()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub=_sub(), idp_iss=ISS_A)
        open_idp_migration(s, new_issuer=ISS_B + "/", actor="op")
        new_sub = _sub("new")
        p = get_or_create_principal(s, email=email, idp_sub=new_sub,
                                    idp_iss=ISS_B)  # token iss: no slash
        assert p.idp_sub == new_sub


def test_iss_sub_collision_refuses_with_audit_not_500():
    """Review-proven blocker: two principals contending for one
    (iss, sub) raised an uncaught IntegrityError that ALSO rolled back
    its own audit row. Now: a clean refusal, and the anomaly has a
    paper trail."""
    victim, attacker = _email(), _email()
    shared = _sub("shared")
    with SessionLocal() as s:
        get_or_create_principal(s, email=victim, idp_sub=shared,
                                idp_iss=ISS_A)
        # create-path collision: new email, already-pinned (iss, sub)
        with pytest.raises(ValueError, match="idp-sub-collision"):
            get_or_create_principal(s, email=attacker, idp_sub=shared,
                                    idp_iss=ISS_A)
    with SessionLocal() as s:
        row = _last_audit(s, attacker)
        assert row is not None, "the anomaly must leave a paper trail"
        assert row.event_type == AuditEventType.AUTH_FAILURE
        assert row.details["anomaly"] == "idp-sub-collision"


def test_repin_collision_during_window_refuses_cleanly():
    """The migration-window variant of the same blocker: re-pinning
    onto an (iss, sub) another row already claimed at the new issuer."""
    early, late = _email(), _email()
    ns = _sub("ns")
    with SessionLocal() as s:
        get_or_create_principal(s, email=early, idp_sub=ns, idp_iss=ISS_B)
        get_or_create_principal(s, email=late, idp_sub=_sub(), idp_iss=ISS_A)
        open_idp_migration(s, new_issuer=ISS_B, actor="op")
        with pytest.raises(ValueError, match="idp-sub-collision"):
            get_or_create_principal(s, email=late, idp_sub=ns,
                                    idp_iss=ISS_B)


def test_mismatch_during_open_window_names_the_near_miss():
    """Mid-migration, a refusal must show the operator the open window
    it did NOT match (review-caught: the silent version left a cutover
    ceremony undiagnosable)."""
    email = _email()
    with SessionLocal() as s:
        get_or_create_principal(s, email=email, idp_sub=_sub(), idp_iss=ISS_A)
        open_idp_migration(s, new_issuer=ISS_B, actor="op")
        with pytest.raises(ValueError, match="idp-sub-mismatch"):
            get_or_create_principal(s, email=email, idp_sub=_sub("x"),
                                    idp_iss="https://third.example")
    with SessionLocal() as s:
        row = _last_audit(s, email)
        assert row.details["open_window_issuer"] == ISS_B
