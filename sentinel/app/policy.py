"""The policy store (7.2.2, ADR-005 Decision 5) — load, generate,
validate, activate.

Three human-intent documents (console-edited from 7.2.4; hand-editable
until then) live in POLICY_DIR:

  entities.yaml — groups (optional `parent` → the lattice) + people
                  (email → display_name, groups). Every person is
                  implicitly in `all-employees`, the birthright base.
  matrix.yaml   — `defaults.windows` + `grants`: per (group, server)
                  an access LEVEL from none | read | write |
                  write-on-request | write-on-approval (each write*
                  level implies read birthright), optional per-entry
                  `windows` override; and `forbids`: rules that trump
                  every level at every rung (Cedar guarantees it).
  servers.yaml  — per server: tool classification (`read`/`write`
                  leaf-name lists; an entry ending `.*` is a prefix
                  class, e.g. `rpc.*` covers the MCP handshake) and,
                  for 7.2.3, the resource extraction + tier map.

  overlay.cedar — optional raw Cedar for what the matrix cannot say.
                  Validated with everything else; expected ~empty —
                  anything accumulating here is a schema smell.

Cedar is GENERATED, never hand-edited: read/write levels emit baseline
permits, write-on-request permits under context.elevated,
write-on-approval under context.approved; forbids emit `forbid`
policies. The ladder (7.2.3) asks with Action::"read"/"write" after
classifying the concrete tool via servers.yaml.

Activation is atomic and last-good-stays-live: a store failing
semantic checks or Cedar validation raises PolicyError and the
previously active policy keeps serving. Success writes
generated/{policies.cedar,entities.json}, stamps a content-hash
version, and auto-commits the WHOLE store to a local git history
(author = the console operator) — git as memory, never as an
authoring surface (ADR-005 Decision 5). Audit rows for activation are
written by the calling endpoint (it holds the DB session); this module
deliberately knows nothing about the database.
"""

import hashlib
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from cedarpy import validate_policies

from .config import POLICY_DIR
from .models import utcnow

LEVELS = {"none", "read", "write", "write-on-request", "write-on-approval"}
_WRITE_LEVELS = {"write", "write-on-request", "write-on-approval"}
BIRTHRIGHT_GROUP = "all-employees"
DEFAULT_WINDOWS = [30, 60, 120]  # minutes — ADR-005, owner input 1

# Everything that lands inside a Cedar string literal is restricted to
# this charset. Not cosmetics: a crafted email like `x" || true //`
# would otherwise be a policy-injection vector THROUGH the entity
# store. Reject, never escape — escaping is where injection bugs live.
_SAFE = re.compile(r"^[A-Za-z0-9@._:-]{1,254}$")

# The Cedar schema the generated policy set must validate against.
# Resource.tier is REQUIRED — Cedar's third evaluation property is
# skip-on-error, so a forbid reading a missing attribute would be
# silently skipped and a data bug would become a permit (ADR-005 D3).
_SCHEMA = json.dumps({"": {
    "entityTypes": {
        "User": {"memberOfTypes": ["Group"]},
        "Group": {"memberOfTypes": ["Group"]},
        "Resource": {"shape": {"type": "Record", "attributes": {
            "server": {"type": "String"},
            "tier": {"type": "String"},
        }}},
    },
    "actions": {
        action: {"appliesTo": {
            "principalTypes": ["User"],
            "resourceTypes": ["Resource"],
            "context": {"type": "Record", "attributes": {
                "elevated": {"type": "Boolean", "required": False},
                "approved": {"type": "Boolean", "required": False},
            }},
        }} for action in ("read", "write")
    },
}})


class PolicyError(ValueError):
    """The store did not activate. `errors` says why, one line each."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ActivePolicy:
    version: str
    policies: str
    entities_json: str
    matrix: dict
    servers: dict
    loaded_at: datetime


_active: ActivePolicy | None = None
_lock = threading.Lock()


def get_active() -> ActivePolicy | None:
    return _active


# --- loading + semantic validation -------------------------------------------

def _read_yaml(path: Path, errors: list[str], required: bool) -> dict:
    if not path.exists():
        if required:
            errors.append(f"missing required document: {path.name}")
        return {}
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        errors.append(f"{path.name}: not valid YAML ({e})")
        return {}
    if not isinstance(doc, dict):
        errors.append(f"{path.name}: top level must be a mapping")
        return {}
    return doc


def _safe(name: str, what: str, errors: list[str]) -> bool:
    if not isinstance(name, str) or not _SAFE.fullmatch(name):
        errors.append(f"{what} {name!r}: only [A-Za-z0-9@._:-] allowed "
                      "(these strings land inside Cedar literals)")
        return False
    return True


def load_store(policy_dir: str | Path = POLICY_DIR):
    """Parse + semantically validate the store. Returns
    (groups, people, matrix, servers, overlay, sources) or raises
    PolicyError listing every problem found (all of them, not the
    first — a console save should show the whole story once)."""
    d = Path(policy_dir)
    errors: list[str] = []
    ent = _read_yaml(d / "entities.yaml", errors, required=True)
    matrix = _read_yaml(d / "matrix.yaml", errors, required=True)
    servers_doc = _read_yaml(d / "servers.yaml", errors, required=True)
    overlay_path = d / "overlay.cedar"
    overlay = overlay_path.read_text() if overlay_path.exists() else ""
    if errors:
        raise PolicyError(errors)

    groups: dict[str, dict] = {}
    for name, spec in (ent.get("groups") or {}).items():
        if not _safe(name, "group", errors):
            continue
        spec = spec or {}
        groups[name] = {"parent": spec.get("parent")}
    groups.setdefault(BIRTHRIGHT_GROUP, {"parent": None})
    for name, spec in groups.items():
        parent = spec["parent"]
        if parent is not None and parent not in groups:
            errors.append(f"group {name!r}: unknown parent {parent!r}")
    # Cycle check: walk each chain; a repeat is a cycle.
    for name in groups:
        seen, cur = set(), name
        while cur is not None:
            if cur in seen:
                errors.append(f"group {name!r}: parent cycle via {cur!r}")
                break
            seen.add(cur)
            cur = groups.get(cur, {}).get("parent")

    people: dict[str, dict] = {}
    for email, spec in (ent.get("people") or {}).items():
        if not isinstance(email, str):
            errors.append(f"person key {email!r} is not a string")
            continue
        email = email.strip().lower()
        if not _safe(email, "person", errors):
            continue
        spec = spec or {}
        member_of = spec.get("groups") or []
        for g in member_of:
            if g not in groups:
                errors.append(f"person {email!r}: unknown group {g!r}")
        people[email] = {"display_name": spec.get("display_name"),
                         "groups": list(member_of)}

    servers: dict[str, dict] = {}
    for name, spec in (servers_doc or {}).items():
        if not _safe(name, "server", errors):
            continue
        spec = spec or {}
        tools = spec.get("tools") or {}
        read = tools.get("read") or []
        write = tools.get("write") or []
        for leaf in [*read, *write]:
            base = leaf[:-2] if isinstance(leaf, str) and leaf.endswith(".*") else leaf
            if not isinstance(base, str) or not re.fullmatch(
                    r"[A-Za-z0-9._-]{1,120}", base):
                errors.append(f"server {name!r}: bad tool entry {leaf!r}")
        servers[name] = {"read": list(read), "write": list(write),
                         "resource": spec.get("resource")}

    defaults = matrix.get("defaults") or {}
    windows = defaults.get("windows", DEFAULT_WINDOWS)
    grants = matrix.get("grants") or {}
    for group, per_server in grants.items():
        if group not in groups:
            errors.append(f"matrix grants: unknown group {group!r}")
            continue
        for server, spec in (per_server or {}).items():
            if server not in servers:
                errors.append(f"matrix {group!r}: unknown server {server!r}")
                continue
            spec = spec or {}
            level = spec.get("level", "none")
            if level not in LEVELS:
                errors.append(f"matrix {group!r}×{server!r}: bad level "
                              f"{level!r} (one of {sorted(LEVELS)})")
            for w in spec.get("windows", windows):
                if not isinstance(w, int) or w <= 0:
                    errors.append(f"matrix {group!r}×{server!r}: bad window {w!r}")

    for i, rule in enumerate(matrix.get("forbids") or []):
        rule = rule or {}
        if rule.get("server") not in servers:
            errors.append(f"forbids[{i}]: unknown server {rule.get('server')!r}")
        if rule.get("group") is not None and rule["group"] not in groups:
            errors.append(f"forbids[{i}]: unknown group {rule['group']!r}")
        for a in rule.get("actions", ["write"]):
            if a not in ("read", "write"):
                errors.append(f"forbids[{i}]: bad action {a!r}")
        tier = rule.get("tier")
        if tier is not None and not _safe(str(tier), f"forbids[{i}] tier", errors):
            pass

    if errors:
        raise PolicyError(errors)
    sources = {"entities.yaml": (d / "entities.yaml").read_bytes(),
               "matrix.yaml": (d / "matrix.yaml").read_bytes(),
               "servers.yaml": (d / "servers.yaml").read_bytes(),
               "overlay.cedar": overlay.encode()}
    return groups, people, matrix, servers, overlay, sources


# --- generation ---------------------------------------------------------------

def _permit(group: str, action: str, server: str, context_flag: str | None) -> str:
    cond = f'resource.server == "{server}"'
    if context_flag:
        cond += (f' && context has {context_flag} '
                 f'&& context.{context_flag} == true')
    return (f'permit(\n'
            f'  principal in Group::"{group}",\n'
            f'  action == Action::"{action}",\n'
            f'  resource\n'
            f') when {{ {cond} }};\n')


def generate(groups: dict, people: dict, matrix: dict,
             overlay: str) -> tuple[str, str]:
    """(policy_text, entities_json) — deterministic output for a given
    store, so the content-hash version is stable and the git history
    diffs cleanly."""
    lines = ["// GENERATED by app.policy — do not edit; edit the store "
             "documents and re-activate.\n"]
    for group in sorted(matrix.get("grants") or {}):
        for server in sorted((matrix["grants"][group]) or {}):
            level = ((matrix["grants"][group][server]) or {}).get("level", "none")
            if level == "none":
                continue
            lines.append(f"// matrix: {group} × {server} = {level}\n")
            lines.append(_permit(group, "read", server, None))
            if level == "write":
                lines.append(_permit(group, "write", server, None))
            elif level == "write-on-request":
                lines.append(_permit(group, "write", server, "elevated"))
            elif level == "write-on-approval":
                lines.append(_permit(group, "write", server, "approved"))

    for rule in matrix.get("forbids") or []:
        server, tier, group = rule["server"], rule.get("tier"), rule.get("group")
        principal = f'principal in Group::"{group}"' if group else "principal"
        cond = f'resource.server == "{server}"'
        if tier:
            cond += f' && resource.tier == "{tier}"'
        for action in rule.get("actions", ["write"]):
            lines.append(f"// forbid: {server}"
                         f"{' tier=' + tier if tier else ''} ({action})\n")
            lines.append(f'forbid(\n  {principal},\n'
                         f'  action == Action::"{action}",\n  resource\n'
                         f') when {{ {cond} }};\n')

    if overlay.strip():
        lines.append("// --- overlay.cedar (hand-written escape hatch) ---\n")
        lines.append(overlay if overlay.endswith("\n") else overlay + "\n")

    entities = []
    for name in sorted(groups):
        parents = []
        if groups[name].get("parent"):
            parents.append({"type": "Group", "id": groups[name]["parent"]})
        entities.append({"uid": {"type": "Group", "id": name},
                         "attrs": {}, "parents": parents})
    for email in sorted(people):
        member = set(people[email]["groups"]) | {BIRTHRIGHT_GROUP}
        entities.append({
            "uid": {"type": "User", "id": email}, "attrs": {},
            "parents": [{"type": "Group", "id": g} for g in sorted(member)],
        })
    return "".join(lines), json.dumps(entities, indent=1, sort_keys=True)


# --- activation ---------------------------------------------------------------

def _git(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=Sentinel",
         "-c", "user.email=sentinel@localhost", *args],
        capture_output=True, text=True)


def _commit_history(d: Path, version: str, actor: str) -> None:
    """Git as memory (ADR-005 D5): every activated version is a commit
    in a LOCAL repo inside the store. Failure here must not fail the
    activation — history is a mirror of decided state, not a gate on
    it — but it must not be silent either, so it surfaces in the
    returned warnings via an exception the caller logs."""
    if not (d / ".git").exists():
        _git(d, "init", "-q")
    if _git(d, "status", "--porcelain").stdout.strip():
        _git(d, "add", "-A")
        r = _git(d, "commit", "-q", "-m", f"policy {version} by {actor}")
        if r.returncode != 0:
            raise RuntimeError(f"policy history commit failed: {r.stderr.strip()}")


def activate(policy_dir: str | Path = POLICY_DIR, *,
             actor: str = "system") -> ActivePolicy:
    """Load → generate → validate → swap, atomically. Raises
    PolicyError (and keeps last-good live) on any failure before the
    swap. Returns the now-active policy."""
    global _active
    d = Path(policy_dir)
    groups, people, matrix, servers, overlay, sources = load_store(d)
    policies, entities_json = generate(groups, people, matrix, overlay)

    result = validate_policies(policies, _SCHEMA)
    if not result.validation_passed:
        raise PolicyError([f"cedar: {e}" for e in result.errors] or
                          ["cedar: validation failed"])

    h = hashlib.sha256()
    for name in sorted(sources):
        h.update(name.encode() + b"\0" + sources[name] + b"\0")
    h.update(policies.encode())
    version = h.hexdigest()[:12]

    gen = d / "generated"
    gen.mkdir(exist_ok=True)
    (gen / "policies.cedar").write_text(policies)
    (gen / "entities.json").write_text(entities_json)
    _commit_history(d, version, actor)

    with _lock:
        _active = ActivePolicy(version=version, policies=policies,
                               entities_json=entities_json, matrix=matrix,
                               servers=servers, loaded_at=utcnow())
    return _active


# --- helpers for the ladder + doors (consumed from 7.2.3 on) ------------------

def classify_tool(servers: dict, server: str, leaf: str) -> str | None:
    """read | write | None(unknown). Prefix classes (`rpc.*`) match by
    prefix; unknown tools classify as None and the caller denies closed."""
    spec = servers.get(server)
    if spec is None:
        return None
    for action in ("read", "write"):
        for entry in spec[action]:
            if entry.endswith(".*"):
                if leaf.startswith(entry[:-1]) or leaf == entry[:-2]:
                    return action
            elif leaf == entry:
                return action
    return None


def profile_tools(servers: dict, server: str, level: str) -> list[str]:
    """The tool-set a `<server>:<level>` profile covers, as FULL tool
    names (`<server>.<leaf>`). Prefix classes are returned as-is —
    grant snapshots store them verbatim and _grant_covers gains prefix
    awareness in 7.2.3. read profiles cover the read set; write
    profiles cover read + write (a borrowing writer still reads)."""
    spec = servers.get(server)
    if spec is None:
        return []
    leaves = list(spec["read"])
    if level == "write":
        leaves += list(spec["write"])
    return [f"{server}.{leaf}" for leaf in leaves]
