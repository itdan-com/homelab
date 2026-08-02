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
import tempfile
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
    groups: dict
    people: dict
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
        resource = spec.get("resource")
        if resource is not None:
            src = (resource or {}).get("from", "")
            if not re.fullmatch(r"params\.arguments\.[A-Za-z0-9_.-]{1,64}", str(src)):
                errors.append(f"server {name!r}: resource.from must be "
                              f"'params.arguments.<key>', got {src!r}")
            for tier, members in ((resource or {}).get("tiers") or {}).items():
                if not _safe(str(tier), f"server {name!r} tier", errors):
                    continue
                if not isinstance(members, list):
                    errors.append(f"server {name!r} tier {tier!r}: not a list")
        servers[name] = {"read": list(read), "write": list(write),
                         "resource": resource}

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
                               servers=servers, groups=groups, people=people,
                               loaded_at=utcnow())
    return _active


# --- console store management (7.2.4) -----------------------------------------

_DOCS = {"entities": "entities.yaml", "matrix": "matrix.yaml",
         "servers": "servers.yaml", "overlay": "overlay.cedar"}


def store_documents(policy_dir: str | Path = POLICY_DIR) -> dict[str, str]:
    """Raw document texts for the editors — from DISK, which is always
    last-good-or-better (see save_and_activate). Missing files read as
    empty so a fresh install's console shows editable blanks, not a 500."""
    d = Path(policy_dir)
    return {key: (d / name).read_text() if (d / name).exists() else ""
            for key, name in _DOCS.items()}


def save_and_activate(policy_dir: str | Path, docs: dict[str, str], *,
                      actor: str) -> ActivePolicy:
    """The console's save. The candidate is validated in a THROWAWAY
    directory first — semantic checks, generation, Cedar validation —
    and only a candidate that fully passes is written to the real
    store and activated. A rejected save therefore never touches disk:
    the store on disk stays last-good, so a broker restart mid-mistake
    re-activates the good version instead of failing closed on a
    half-saved one."""
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        for key, name in _DOCS.items():
            (t / name).write_text(docs.get(key, ""))
        groups, people, matrix, _servers, overlay, _ = load_store(t)
        policies, _entities = generate(groups, people, matrix, overlay)
        result = validate_policies(policies, _SCHEMA)
        if not result.validation_passed:
            raise PolicyError([f"cedar: {e}" for e in result.errors] or
                              ["cedar: validation failed"])
    d = Path(policy_dir)
    d.mkdir(parents=True, exist_ok=True)
    for key, name in _DOCS.items():
        (d / name).write_text(docs.get(key, ""))
    return activate(d, actor=actor)


def structured_to_documents(groups: dict, people: dict, matrix: dict,
                            servers: dict) -> dict[str, str]:
    """The GUI's save path (7.2.6): structured JSON → the store's YAML
    documents, which then ride the exact same validate→activate gate
    as a raw save. `sort_keys` keeps the emission deterministic, so an
    unchanged intent hashes to an unchanged version. Comments do not
    survive a GUI save — a GUI-managed file is machine-formatted, and
    the annotated human story lives in policy-example/."""
    ent = {
        "groups": {
            name: ({"parent": spec["parent"]}
                   if (spec or {}).get("parent") else {})
            for name, spec in (groups or {}).items()
        },
        "people": {
            email: {
                **({"display_name": p["display_name"]}
                   if (p or {}).get("display_name") else {}),
                "groups": list((p or {}).get("groups") or []),
            }
            for email, p in (people or {}).items()
        },
    }
    srv = {}
    for name, spec in (servers or {}).items():
        spec = spec or {}
        entry = {"tools": {"read": list(spec.get("read") or []),
                           "write": list(spec.get("write") or [])}}
        if spec.get("resource"):
            entry["resource"] = spec["resource"]
        srv[name] = entry
    mat = {"defaults": (matrix or {}).get("defaults") or
           {"windows": list(DEFAULT_WINDOWS)},
           "grants": (matrix or {}).get("grants") or {}}
    if (matrix or {}).get("forbids"):
        mat["forbids"] = matrix["forbids"]

    def dump(obj: dict) -> str:
        return yaml.safe_dump(obj, sort_keys=True, default_flow_style=False)

    return {"entities": dump(ent), "matrix": dump(mat), "servers": dump(srv)}


_SUBJECT = re.compile(r"^policy ([0-9a-f]{12}) by (.+)$")


def history(policy_dir: str | Path = POLICY_DIR) -> list[dict]:
    """Activated versions, newest first, from the store's own git —
    the memory the console renders and revert_to() restores from."""
    d = Path(policy_dir)
    if not (d / ".git").exists():
        return []
    r = _git(d, "log", "--format=%H%x1f%s%x1f%aI")
    rows = []
    for line in r.stdout.splitlines():
        sha, _, rest = line.partition("\x1f")
        subject, _, ts = rest.partition("\x1f")
        m = _SUBJECT.match(subject)
        if m:
            rows.append({"sha": sha, "version": m.group(1),
                         "actor": m.group(2), "ts": ts})
    return rows


def revert_to(policy_dir: str | Path, version: str, *,
              actor: str) -> ActivePolicy:
    """Restore version N — forward, never rewriting: the old sources
    are checked out into the working tree and re-activated, which
    (content-hash versioning) yields the SAME version id as a NEW
    commit on top of history. The audit row and the git log both say
    a restore happened; nothing is erased."""
    d = Path(policy_dir)
    target = next((row for row in history(d) if row["version"] == version), None)
    if target is None:
        raise PolicyError([f"unknown policy version {version!r}"])
    r = _git(d, "checkout", target["sha"], "--", ".")
    if r.returncode != 0:
        raise PolicyError([f"restore failed: {r.stderr.strip()}"])
    return activate(d, actor=actor)


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


def transitive_groups(groups: dict, direct: set[str]) -> set[str]:
    """A person's full group set: direct memberships expanded UP the
    parent chain (hr-head ⇒ hr). Cedar does this itself during
    evaluation via entity parents; this helper exists for the parts
    that are NOT Cedar — the elevation hint (which matrix cell's
    windows apply) and the doors' profile lookups."""
    out: set[str] = set()
    for g in direct:
        cur = g
        while cur is not None and cur not in out:
            out.add(cur)
            cur = (groups.get(cur) or {}).get("parent")
    return out


# The resource id lands inside a Cedar literal exactly like emails do —
# same guard, same stance: reject, never escape.
_SAFE_RESOURCE = re.compile(r"^[A-Za-z0-9@._:/-]{1,200}$")


def derive_resource(servers: dict, server: str, leaf: str,
                    arguments: dict | None) -> tuple[str, str] | None:
    """(resource_id, tier), or None ⇒ the caller denies closed.

    Handshake scopes (`rpc.*`) carry no arguments and address the
    server itself. A server without a resource map yields
    `<server>/*` at tier `unclassified` — and POLICY decides what
    unclassified may do (for a chat toy, everything; for a database,
    nothing). A server WITH a map must extract cleanly or the call
    dies: a missing key, a non-string, or an unsafe value is
    `unmapped-resource`, never a permissive default. Tier membership
    is exact match, or prefix when the entry ends with `*`; an
    unmatched value is tier `unclassified` — the ADR's rule that the
    tier attribute is TOTAL (Cedar skip-on-error) while staying
    deny-biased through policy."""
    spec = servers.get(server) or {}
    rmap = spec.get("resource")
    if leaf.startswith("rpc.") or rmap is None:
        return f"{server}/*", "unclassified"
    key_path = rmap["from"].split(".")[2:]  # validated shape at load
    value = arguments or {}
    for key in key_path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if not isinstance(value, str) or not _SAFE_RESOURCE.fullmatch(value):
        return None
    tier = "unclassified"
    for name, members in (rmap.get("tiers") or {}).items():
        for entry in members:
            e = str(entry)
            if (e.endswith("*") and value.startswith(e[:-1])) or value == e:
                tier = name
                break
        if tier != "unclassified":
            break
    return f"{server}/{value}", tier


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
