# Mullvad Workstation — a VPN-isolated Ubuntu desktop (side project)

**What this is:** a toggle-on/off Ubuntu desktop, used in a browser tab, whose
every byte of internet traffic exits through Mullvad VPN — with a kill-switch
so that if the VPN drops, **nothing leaks**. Runs as its own `docker compose`
stack on the WSL2 Docker daemon, **completely separate from the k3d homelab
platform** (different trust domain, not a catalog service). On/off with
`docker compose up -d` / `docker compose stop`.

**Status:** ✅ LIVE (2026-06-03). gluetun (Mullvad, exit verified = Portugal) +
Ubuntu XFCE desktop at `https://localhost:3001`. Desktop egress confirmed = the
Mullvad IP. Remaining: install Mullvad Browser (optional — any browser here is
already VPN'd), run the kill-switch drop-test (on request), connect the SMB share.

---

## Owner's requirements (captured 2026-06-03)
- Super secure: traffic on Mullvad and **nothing else**; no leaks.
- Ubuntu **desktop GUI**, plenty of resources, shut on/off at will.
- Easy file egress to a **local SMB/FTP file server** on the LAN.
- Accessed from the **Windows box via RDP** → `localhost:<port>` is fine for now
  (Mac-direct access deferred; same WSL2-ports-not-on-LAN caveat as OpenWebUI).

---

## Architecture (recommended)
```
gluetun  (Mullvad WireGuard tunnel + kill-switch firewall)
   ^  shares network namespace
ubuntu-xfce desktop  --  Mullvad Browser + file manager
   - web GUI via KasmVNC at  http://localhost:<port>
   - every packet exits via gluetun -> Mullvad; VPN down => blocked
   - LAN subnet whitelisted so it can reach the SMB/FTP server
```

### The one open decision — how to run the VPN
- **(A, recommended) gluetun gateway, no Mullvad app.** A dedicated VPN
  container holds the Mullvad WireGuard tunnel and a firewall kill-switch; the
  desktop has *no network of its own* and rides gluetun's. Most bulletproof
  "nothing leaks," simplest, always-on. Browse with Mullvad Browser; the
  Mullvad app is not needed (gluetun *is* the Mullvad connection). Trade-off:
  no click-to-connect GUI; exit location is set in config.
  - Needs: a Mullvad **WireGuard** config (private key + assigned address).
- **(B) Mullvad app inside the desktop.** Install the official Mullvad Linux
  app in the desktop; connect + enable **lockdown mode** (its kill-switch) from
  its GUI. Familiar app, on-the-fly location switching. Trade-off: the Mullvad
  daemon in a container is fiddlier (no systemd) and the kill-switch is
  app-dependent — a slightly larger leak surface than (A).
  - Needs: a Mullvad **account number** (the app logs in and fetches config).

---

## Checklist

### Phase A — decide & gather (owner)
- [x] **VPN approach chosen: (A) gluetun gateway** (network-level kill-switch; no Mullvad app).
- [ ] Provide Mullvad creds for it (WireGuard config for A; account number for B).
- [ ] LAN subnet (e.g. `192.168.1.0/24`) + file-server IP + protocol (SMB/FTP) + creds.
- [ ] Resource budget (RAM/CPU — shares the WSL2 ~31 GB / 16-thread pool with the homelab).
- [ ] Mullvad exit location preference (optional).

### Phase B — build
- [x] `extras/mullvad-workstation/compose.yaml` — gluetun + Ubuntu XFCE desktop (KasmVNC), parameterized via `.env`.
- [ ] Encrypt the Mullvad secret (SOPS, reuse the repo age key) or a gitignored env file.
- [ ] Set a KasmVNC web password; resource limits; named volume for profile + downloads.
- [ ] Verify WSL2 exposes `/dev/net/tun`; grant `NET_ADMIN` to the VPN container.

### Phase C — harden ("nothing else")
- [ ] Kill-switch on; confirm non-VPN egress is dropped.
- [ ] Whitelist *only* the LAN/file-server for outbound; everything else VPN-only.
- [x] DNS goes through the tunnel encrypted (gluetun DoT, Cloudflare upstream) — no ISP/plaintext leak. For **Mullvad** DNS specifically (what Mullvad Browser's checker wants), set Mullvad DoH *in the browser* — gluetun has no Mullvad resolver. See gotcha.
- [x] Mullvad Browser **15.0.14** installed (portable tarball in `/config`, persists; desktop launcher created); binary verified (`MullvadBrowser 140.11.0esr`).

### Phase D — verify leak-tight
- [x] Desktop egress verified = Mullvad exit (`185.92.210.215`, Portugal) via a netns-shared curl. (In-browser `mullvad.net/check` confirms the same.)
- [x] Kill-switch test PASSED (2026-06-03): stopped the VPN gateway -> desktop had 0 global IPs / no internet (no leak); restored cleanly.
- [ ] Confirm the LAN file server IS reachable and a file moves out.

### Phase E — file egress + on/off
- [ ] Connect to the SMB share from the desktop file manager (`smb://server/share`) or pre-mount it.
- [ ] `docker compose up -d` / `stop` toggles the whole stack.

---

## Access flow (what using it looks like)
1. RDP into the Windows box.
2. Open `http://localhost:<port>` in the Windows browser.
3. Enter the KasmVNC web password -> Ubuntu XFCE desktop (no separate Ubuntu login).
4. Browse via Mullvad Browser; file manager -> `smb://<server>/<share>` -> move files out.

---

## Gotchas
- **Same WSL2 host as the homelab** — shares the RAM/CPU budget; size accordingly.
- **Mac-direct GUI access** would hit the WSL2-ports-not-on-LAN wall (same as
  OpenWebUI); for now use the Windows box. Deferred.
- **WireGuard in WSL2** — works; the WSL2 kernel ships WireGuard, so gluetun used
  the kernelspace implementation. Needs `NET_ADMIN` + `/dev/net/tun` (both present).
- **IPv6 tunnel address breaks gluetun on WSL2.** Mullvad's WireGuard config hands
  out BOTH a v4 and a v6 `Address`. WSL2's Docker has no IPv6, so gluetun crash-loops
  with `interface address is IPv6 but IPv6 is not supported`. Fix: keep ONLY the IPv4
  CIDR in `WIREGUARD_ADDRESSES` (drop the `,fc00:.../128`). Bonus: removes the v6 leak path.
- **Not a platform/catalog service** — deliberately a personal compose stack
  outside k3d; it does NOT get the Sentinel/Slack/GitOps treatment.
- **Mullvad DNS belongs in the browser, not gluetun.** gluetun resolves via its
  own encrypted DoT (Cloudflare upstream) tunneled through Mullvad — encrypted,
  not an ISP leak — but Mullvad Browser's leak-check flags non-Mullvad DNS.
  gluetun has NO Mullvad DoT/DoH provider, and forcing Mullvad's in-tunnel DNS
  (`10.64.0.1`, `DNS_UPSTREAM_RESOLVER_TYPE=plain`) fights gluetun's firewall —
  it blocks the address and goes unhealthy. Clean fix: set Mullvad DoH in
  Mullvad Browser itself → Settings → Privacy & Security → DNS over HTTPS →
  Max Protection → Custom → `https://base.dns.mullvad.net/dns-query`. That gives
  the browser (what the checker tests) Mullvad DNS without touching gluetun.
