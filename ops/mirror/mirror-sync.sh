#!/usr/bin/env bash
# =============================================================================
# ADR-009 D3: the bare local mirror — insurance, not a service.
#
# One fetch from GitHub into a bare repo on this host, every few
# minutes, so that during a GitHub outage: the rebuild has a source
# (bootstrap can clone from here), ArgoCD has a documented repoint
# target, and a future forge (ADR-009 D6, Phase 9) has a seed. Plus
# two caches that make the REBUILD GitHub-independent, because git
# data was never the whole dependency:
#   charts/  — the catalog's helm dependency tarballs (4 of 8 chart
#              repos are GitHub Pages and die with GitHub)
#   tools/   — the sops + helm-secrets artifacts the ArgoCD
#              repo-server's initContainer needs (pinned versions,
#              fetched once, checksum recorded on first fetch)
#
# VERIFIED, NOT TRUSTED: every run clones the mirror back to a temp
# dir and compares HEAD against the mirror's own ref — a cron mirror
# that rots silently is worse than no mirror, because it gets trusted
# (the review's phrase, kept). Every run rewrites last-sync.txt; a
# stale timestamp IS the failure signal.
#
# Runs as an ordinary user unit (see deploy/), tokenless — the repo is
# public. Host-agnostic by construction: everything derives from $HOME
# at runtime (ADR-002/ADR-004 — a cloud VM runs this identical file).
# =============================================================================
set -euo pipefail

REPO_URL="${MIRROR_REPO_URL:-https://github.com/itdan-com/homelab.git}"
MIRROR_ROOT="${MIRROR_ROOT:-$HOME/.local/state/homelab-mirror}"
BUILDER_CHECKOUT="${MIRROR_BUILDER_CHECKOUT:-$HOME/homelab}"
BARE="$MIRROR_ROOT/repo.git"
# Tool pins — kept in lockstep with catalog/argocd/values.yaml's
# initContainer URLs; a version bump there means a bump here.
SOPS_VER="v3.13.1"
HS_VER="v4.6.5"

mkdir -p "$MIRROR_ROOT/charts" "$MIRROR_ROOT/tools"

# --- 1. the mirror itself -----------------------------------------------
if [[ ! -d "$BARE" ]]; then
  git clone --quiet --mirror "$REPO_URL" "$BARE"
  echo "mirror: initial clone complete"
fi
git --git-dir="$BARE" remote update --prune >/dev/null

# --- 2. clone-back verification (the part that makes it trustworthy) ----
VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT
git clone --quiet --depth 1 "file://$BARE" "$VERIFY_DIR/check"
CLONED_HEAD="$(git -C "$VERIFY_DIR/check" rev-parse HEAD)"
MIRROR_MAIN="$(git --git-dir="$BARE" rev-parse refs/heads/main)"
if [[ "$CLONED_HEAD" != "$MIRROR_MAIN" ]]; then
  echo "mirror: VERIFY FAILED — clone-back HEAD $CLONED_HEAD != mirror main $MIRROR_MAIN" >&2
  exit 1
fi

# --- 3. chart-tarball cache ---------------------------------------------
# Cached FROM the builder checkout's gitignored charts/ dirs (whatever
# the last successful `helm dependency build` produced) — preserves the
# deliberate don't-commit-tarballs decision while giving bootstrap a
# GitHub-Pages-independent fallback.
if [[ -d "$BUILDER_CHECKOUT/catalog" ]]; then
  find "$BUILDER_CHECKOUT/catalog" -path '*/charts/*.tgz' -exec cp -u {} "$MIRROR_ROOT/charts/" \; 2>/dev/null || true
fi

# --- 4. the repo-server's toolchain, fetched once ------------------------
# These are exactly the two artifacts catalog/argocd's initContainer
# wgets from github.com on EVERY pod start (the ADR-009 finding). A
# copy here means a rebuild-during-outage can be hand-fed; replacing
# the initContainer's wget entirely is the deferred image-vendoring
# item (needs a registry story).
SOPS_BIN="$MIRROR_ROOT/tools/sops-$SOPS_VER.linux.amd64"
HS_TGZ="$MIRROR_ROOT/tools/helm-secrets-$HS_VER.tar.gz"
if [[ ! -s "$SOPS_BIN" ]]; then
  curl -fsSL -m 60 -o "$SOPS_BIN" \
    "https://github.com/getsops/sops/releases/download/$SOPS_VER/sops-$SOPS_VER.linux.amd64" \
    && sha256sum "$SOPS_BIN" > "$SOPS_BIN.sha256" \
    && echo "mirror: cached sops $SOPS_VER" || echo "mirror: sops fetch failed (retry next run)" >&2
fi
if [[ ! -s "$HS_TGZ" ]]; then
  curl -fsSL -m 60 -o "$HS_TGZ" \
    "https://github.com/jkroepke/helm-secrets/releases/download/$HS_VER/helm-secrets.tar.gz" \
    && sha256sum "$HS_TGZ" > "$HS_TGZ.sha256" \
    && echo "mirror: cached helm-secrets $HS_VER" || echo "mirror: helm-secrets fetch failed (retry next run)" >&2
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$MIRROR_ROOT/last-sync.txt"
echo "mirror: synced+verified at $MIRROR_MAIN ($(ls "$MIRROR_ROOT/charts" | wc -l) chart tgz cached)"
