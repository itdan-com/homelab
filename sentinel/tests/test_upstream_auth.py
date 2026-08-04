"""7.4: upstream credentials — the workload holds none, and nothing
depends on a human remembering to rotate.

    python -m pytest tests/test_upstream_auth.py -q
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SENTINEL_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app import upstream_auth  # noqa: E402
from app.upstream_auth import UpstreamAuthError, token_for  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    upstream_auth.forget()
    yield
    upstream_auth.forget()


@pytest.fixture
def key_file(tmp_path):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "app.pem"
    p.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return str(p)


def _conf(tmp_path, doc) -> str:
    p = tmp_path / "upstream-tokens.json"
    p.write_text(json.dumps(doc))
    return str(p)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


@pytest.fixture
def github(monkeypatch):
    """Stub GitHub's App endpoints; record what was sent so the
    assertion itself can be inspected."""
    state = {"calls": [], "expires_in": 3600, "token": "ghs_minted_1", "n": 0}

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, headers=None):
            state["calls"].append(("GET", url, headers))
            return _Resp(200, [{"id": 4242}])

        def post(self, url, headers=None):
            state["calls"].append(("POST", url, headers))
            state["n"] += 1
            from datetime import datetime, timedelta, timezone
            exp = datetime.now(timezone.utc) + timedelta(
                seconds=state["expires_in"])
            return _Resp(201, {"token": f"{state['token']}_{state['n']}",
                               "expires_at": exp.isoformat().replace(
                                   "+00:00", "Z")})

    monkeypatch.setattr(upstream_auth.httpx, "Client", lambda **k: C())
    return state


# --- the simple cases ---------------------------------------------------------

def test_no_entry_is_not_an_error(tmp_path):
    """A server the deployment has not given a credential is decidable
    but not callable — an honest state, not a failure."""
    assert token_for("github", _conf(tmp_path, {})) is None


def test_a_bare_string_is_a_static_token(tmp_path):
    assert token_for("slack", _conf(tmp_path, {"slack": "xoxb-abc"})) == "xoxb-abc"


def test_a_half_configured_app_refuses_rather_than_guessing(tmp_path):
    with pytest.raises(UpstreamAuthError, match="app_id"):
        token_for("github", _conf(tmp_path, {"github": {"app_id": "1"}}))


# --- the App path -------------------------------------------------------------

def test_app_config_mints_a_short_lived_token(tmp_path, key_file, github):
    path = _conf(tmp_path, {"github": {
        "app_id": "12345", "private_key_file": key_file,
        "installation_id": "67890"}})
    tok = token_for("github", path)
    assert tok.startswith("ghs_minted_1")
    # It went to the installation's endpoint with a signed App assertion
    method, url, headers = github["calls"][-1]
    assert method == "POST" and url.endswith("/installations/67890/access_tokens")
    assert headers["Authorization"].startswith("Bearer ey")  # a JWT


def test_the_token_is_cached_not_reminted_every_call(tmp_path, key_file, github):
    """A mint per call would burn GitHub's rate limit and add a network
    round trip to every tool invocation."""
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": key_file, "installation_id": "2"}})
    first = token_for("github", path)
    assert token_for("github", path) == first
    assert github["n"] == 1


def test_it_remints_before_expiry_not_after(tmp_path, key_file, github):
    """Refreshing at expiry means the last calls in the window fail.
    A token inside the refresh margin is treated as already gone."""
    github["expires_in"] = 120          # inside REFRESH_MARGIN_SECONDS
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": key_file, "installation_id": "2"}})
    first = token_for("github", path)
    second = token_for("github", path)
    assert second != first and github["n"] == 2


def test_github_stated_expiry_wins_over_our_assumption(tmp_path, key_file, github):
    """We do not assume the documented hour: assuming a vendor's TTL is
    how a credential expires in production and not in testing."""
    github["expires_in"] = 7200
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": key_file, "installation_id": "2"}})
    token_for("github", path)
    _tok, exp = upstream_auth._cache["github"]
    assert exp > time.time() + 7000


def test_an_ambiguous_installation_refuses_instead_of_picking(
        tmp_path, key_file, monkeypatch):
    """Two installations and no `installation_id` is the operator's
    decision, not ours — guessing would silently act on the wrong org."""
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            return _Resp(200, [{"id": 1}, {"id": 2}])
        def post(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("minted despite ambiguity")

    monkeypatch.setattr(upstream_auth.httpx, "Client", lambda **k: C())
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": key_file}})
    with pytest.raises(UpstreamAuthError, match="installations"):
        token_for("github", path)


def test_a_missing_private_key_is_a_clear_refusal(tmp_path, github):
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": "/nope/absent.pem",
        "installation_id": "2"}})
    with pytest.raises(UpstreamAuthError, match="private key"):
        token_for("github", path)


def test_rotation_takes_effect_without_a_restart(tmp_path, key_file, github):
    """Editing the credential file and forgetting the cache is the
    whole rotation procedure — no unit restart, no redeploy."""
    path = _conf(tmp_path, {"github": {
        "app_id": "1", "private_key_file": key_file, "installation_id": "2"}})
    token_for("github", path)
    Path(path).write_text(json.dumps({"github": "ghp_static_now"}))
    upstream_auth.forget("github")
    assert token_for("github", path) == "ghp_static_now"


def test_a_default_destructive_hint_does_not_hide_read_tools(monkeypatch):
    """MCP's spec default for destructiveHint is TRUE, so a server that
    annotates it selectively leaves it set on tools it never considered.
    Slack's server reports destructive on 21 of 22 tools, read-only ones
    included — reading that first would classify the whole catalog as
    dangerous, leave every tool unclassified, and make the server
    silently uncallable."""
    class R:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"result": {"tools": [
                {"name": "channels_list",
                 "annotations": {"readOnlyHint": True, "destructiveHint": True}},
                {"name": "conversations_add_message",
                 "annotations": {"readOnlyHint": False, "destructiveHint": True}},
                {"name": "usergroups_me", "annotations": {}},
            ]}}

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()

    monkeypatch.setattr(upstream_auth.httpx, "Client", lambda **k: C())
    out = upstream_auth.discover_tools("slack", "https://x/mcp", "t")
    assert "channels_list" in out["read"]          # read wins over the default
    assert "conversations_add_message" in out["destructive"]
    assert "usergroups_me" in out["write"]         # unannotated = nobody vouched
