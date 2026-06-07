# Zenoh DC-Portal enablement — problem observed & deploy note

> For the deployment agent. This branch (`harden/zenoh-portal-security`) fixes
> the server-side bugs that prevented DC Portal from working on the **Zenoh**
> backend. Redeploy the portal + registry images from this branch and run the
> verification at the bottom.

## What was observed (live test, tenant `test-zenoh`, portal on Zenoh/mTLS)

Two simulated devices (a sensor + a controller) were provisioned and launched
against the portal at `http://137.184.86.16:8080`, Zenoh router `tls/…:7447`.
The end-to-end flow (register → `devices list` → invoke → stream events) failed:

| Symptom | Where |
|---|---|
| `HTTP 502 invoke_failed: cannot import name 'ZenohAdapter' from 'device_connect_edge.messaging'` | every `dc-portalctl devices invoke` over Zenoh |
| `Registration failed: Request to device-connect.<tenant>.registry timed out after 15.0s (no responders)` (retried forever) | every device at startup; devices never appear in `devices list` |
| Two authenticated tenant clients on the router could not reach each other (raw `put`/`subscribe` + mTLS: cross-session FAIL, same-session loopback PASS) | device↔device and portal↔device routing |

## Root causes & fixes (this branch)

1. **Portal invoke import bug** — `portal/services/zenoh_rpc.py` imported
   `ZenohAdapter` from the `device_connect_edge.messaging` package root, which
   only exports the `create_client` factory. → switched to
   `create_client("zenoh")`; also cached the invoke adapter (was a full mTLS
   handshake per call) and wired its cleanup into app shutdown.
   *(commit "repair portal invoke + ACL self-check")*

2. **ACL self-check read the wrong key** — `zenoh_backend.run_verification()`
   read `cfg["plugins"]["access_control"]`, but Zenoh 1.x puts access control
   at the **top level** (`access_control`). The portal always reported "ACL
   plugin not enabled", masking the real state during debugging. → read the
   top-level key. *(same commit)*

3. **Sessions did not survive a router restart** — adding a tenant changes the
   ACL and reloads the router; the registry service (and any long-lived
   subscriber) then lost its queryables and never re-declared them, so
   `…/registry` had no responder thereafter. The `ZenohAdapter` accepted a
   `reconnect_cb` but never called it and had no redeclare path. → router-bound
   clients now use `connect.exit_on_failure=false` + a retry policy, and a
   session-health watchdog re-opens the session and replays all
   subscribers/queryables on a hard close (no duplicate declarations; caller
   handles stay valid). *(commit "survive router restarts via persistent
   reconnect + redeclare")*

4. **A slow registry black-holed invoke** — `DeviceRuntime.run()` awaited
   registration (which retries forever) *before* subscribing to the device's
   `.cmd` subject, so a device could be "up" yet not listening. → subscribe to
   commands first, register in the background with a bounded wait that
   preserves the healthy-path ordering. *(commit "serve commands before
   registration")*

### Already fixed on `pr-52` — verify it is included in the deployed images

The **edge mTLS/creds handling** was the underlying cause of symptom 3's
"clients can't route" observation: released 0.2.4 ignored the inline
`ca_pem/cert_pem/key_pem` in `*.creds.json` and connected **without** the
client certificate, so the router's per-tenant-CN ACL could not match the
connection and denied all traffic (deny-by-default). `pr-52` reads the inline
PEMs and uses the correct Zenoh 1.x TLS field names
(`connect_certificate_base64`, `root_ca_certificate_base64`, `enable_mtls`).
**The registry and all device images must be built from `pr-52` (or newer, which
this branch is) so they actually authenticate.** Confirm the live device certs
carry `CN=<tenant>` (`openssl x509 -subject` → `CN=test-zenoh`); they already do.

## Redeploy

1. Build/redeploy the **portal** and **device-registry** images from this branch.
2. No ACL/config schema change is required; existing per-tenant-CN certs and the
   top-level `access_control` config are unchanged.
3. Devices (edge SDK) should be on `pr-52`+ as well.

## Verification (end-to-end)

```bash
# 1. invoke no longer 502s
dc-portalctl devices invoke test-zenoh-controller-001 get_state \
    --params '{}' --reason "post-deploy check"      # expect a JSON result, not HTTP 502

# 2. devices register and are visible
dc-portalctl devices list                            # expect both devices, fresh Last Seen

# 3. ACL self-check reports enabled
dc-portalctl ... fleet verify / portal verification   # "Zenoh ACL Plugin: pass"

# 4. router-restart resilience: provision a device in a NEW tenant (forces a
#    router reload), then confirm the registry still answers and existing
#    devices stay invocable.
```

A reference two-device rig (sensor + controller exercising `@rpc`, `@periodic`,
`@emit`, and D2D `invoke_remote`) is available on request to reproduce the full
flow.

---

## Post-redeploy finding: the router server cert must be regenerated

After redeploying with the fixes above, devices now correctly present their
mTLS client certificate (the `pr-52` inline-PEM fix) -- and that surfaced a
**stale server certificate** on the router:

```
Failed to connect to Zenoh: Unable to connect to any of [tls/<public-ip>:7447]!
```

Root cause (confirmed live): the router's server cert was generated **before**
the IP-SAN fix (`harden(zenoh): IP SAN for IP hosts`, commit 3cca1c1) and cert
generation is existence-gated, so the redeploy did not regenerate it. It carries
the public IP as a **DNS** SAN (`DNS:<ip>`) with only `IP:127.0.0.1` as an IP
SAN. rustls/Zenoh, when connecting to an **IP literal**, matches **IP SANs
only**, so server-name verification fails and the client cannot connect.
(Proof: the exact same creds connect with `verify_name_on_connect=false`.)

The cert-generation **code is already correct** (`zenoh_pki.generate_server_cert`
emits `IP:<host>` for IP-literal hosts) -- only the on-disk cert is stale. The
CA and all device credentials are unchanged, so only the router's own cert needs
refreshing.

### Fix: regenerate the server cert (now safe to do via setup re-run)

`bootstrap()` is now idempotent: it **keeps an existing CA** (re-running setup no
longer rotates the CA, which previously would have invalidated every device
credential) and **always refreshes the router server cert**. So the supported
fix is simply to re-run setup, then restart the router:

```
Admin -> Setup   (or:  POST /api/admin/setup  with the public IP as host)
# then restart the router container so it reloads the refreshed cert:
docker restart <zenoh-router-container>     # e.g. dc-zenoh
```

Equivalent direct call inside the portal container (same CA, server cert only):

```bash
python -c "import asyncio; from device_connect_server.portal.services import zenoh_pki; \
  asyncio.run(zenoh_pki.generate_server_cert('<public-ip>'))"
docker restart <zenoh-router-container>
```

Verify the SAN is now correct:

```bash
openssl s_client -connect <public-ip>:7447 </dev/null 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName
# expect:  IP Address:<public-ip>   (not DNS:<public-ip>)
```

After this, devices connect with full TLS verification (no
`verify_name_on_connect` override needed) and the e2e flow works.

---

## Round-3 retest (2026-06-07) — note to the deployment agent

Re-tested live after the latest fixes (agent-tools self-config `b9c5672`,
public-host advertising `7e0897a`, idempotent bootstrap, server-cert IP SAN).

### Verified working — no action needed
- Devices launch with the canonical `MESSAGING_CREDENTIALS_FILE` (no
  `NATS_CREDENTIALS_FILE`, no deprecation warning), connect over mTLS, and
  register (fleet: 2 registered / 2 online).
- D2D `invoke_remote` + events + high-temp alert over the router.
- Portal-mediated `dc-portalctl devices invoke` returns real results.
- agent-tools self-configures creds + per-backend mTLS from
  `MESSAGING_CREDENTIALS_FILE` (no explicit `tls_config` needed); `invoke()`
  returns a real result.

### ACTION NEEDED (deployment-side): set `ZENOH_PUBLIC_HOST`
Re-downloaded device creds still advertise the **internal** broker host
(`zenoh+tls://zenoh:7447`), so external devices still need a `ZENOH_CONNECT`
override to reach the box. The `7e0897a` precedence is
`ZENOH_PUBLIC_HOST > bootstrap host > ZENOH_HOST`, and it is currently falling
through to `ZENOH_HOST=zenoh`.

- Set **`ZENOH_PUBLIC_HOST=137.184.86.16`** (the externally-reachable host) in
  the portal's environment.
- Re-issue / re-download device credentials so the `zenoh.urls` in each
  `*.creds.json` advertises the public host. (Cert material is unchanged; this
  only rewrites the advertised URL.)
- Verify: a freshly downloaded cred shows
  `"urls": ["zenoh+tls://137.184.86.16:7447"]`, and a device launched with only
  `MESSAGING_CREDENTIALS_FILE` (no `ZENOH_CONNECT`) connects and registers.

### NOT deployment issues — code fixes pending on this branch (FYI, do not chase as ops)
1. **agent-tools tenant resolution.** `connect(zone="default")` has a truthy
   default, so `zone = zone or os.environ.get("TENANT", ...)` never falls
   through to `TENANT`, and the creds-file `tenant` is not consulted — external
   agents hit `device-connect.default.*` unless `zone=` is passed explicitly.
   Fix is client-side (default `zone=None`; resolve explicit -> creds tenant ->
   `TENANT` env -> `default`). No deploy action.
2. **Bus discovery returns an empty roster.** agent-tools `discover()` over the
   broker returns 0 devices (even addressed by exact id) for the correct
   tenant, while `invoke()` works and the portal `devices list` shows them.
   This points at the registry service's bus discovery/`listDevices` handler
   (the portal HTTP roster is fine). Under investigation; likely a
   registry-side code fix, not a deploy/config change — flagging so it is not
   mistaken for a broker outage.

### Update (deployment agent) — actioned

- **`ZENOH_PUBLIC_HOST`: DONE.** Set `ZENOH_PUBLIC_HOST=137.184.86.16` in the
  portal env (persisted in the gitignored `infra/.env`) and redeployed.
  Verified: a freshly provisioned device's cred now advertises
  `"urls": ["zenoh+tls://137.184.86.16:7447"]`, and a device launched with only
  `MESSAGING_CREDENTIALS_FILE` (no `ZENOH_CONNECT`) connects + registers.
  *Existing* creds issued before this still carry the internal host — re-issue
  (re-provision) or do a clean re-bootstrap to refresh them.
- **Code fix #1 (agent-tools tenant resolution): FIXED.** `DeviceConnection`
  now defaults `zone=None` and resolves explicit -> credential `tenant` ->
  `TENANT` env -> `"default"`, reading the creds file once for tenant + TLS +
  URL. Verified live: an agent with no `zone=` resolved to `ptcn1` and invoked
  a device in that tenant. (+ unit tests.)
- **Code fix #2 (bus discovery roster): still open** — registry-side, not a
  deploy/config issue. Not yet addressed here.

---

## Round-4 retest (2026-06-07) — tenant fix verified; discovery roster still empty

Retested after `aab181d` (agent-tools tenant/zone from credential).

### Verified fixed
- **agent-tools tenant resolution** now works: with no `TENANT` env and no
  `zone=` arg, `connect()` derives the tenant from the credential
  (`resolved zone: test-zenoh`) and `invoke()` returns a real result. The
  round-3 item #1 is closed.

### Still open — #2 bus discovery returns an empty roster (registry-side)
`discover()` over the broker returns **0** for the correct tenant, while at the
**same moment** the portal HTTP `devices list` shows the devices and `invoke()`
to them succeeds. Now narrowed — ruled OUT:
- auth/connect (invoke works), tenant (resolves correctly), ACL visibility
  (default `visible_to=["*"]` matches any requester), and **labels** (added a
  matching `category` label to a device; `discover('device(category:...)')`,
  `device(*)`, and `device(<exact-id>)` all still returned 0).

So the registry's bus **`discovery/listDevices`** handler returns an empty list
for a tenant whose devices ARE present in the registry that the portal HTTP
roster reads (etcd-backed). This points at a divergence between the
registry-service that answers `device-connect.<tenant>.discovery` over the bus
and the store/path the portal `devices list` reads — e.g. a different/empty
etcd view, a tenant key-prefix mismatch in `registry.list_devices(tenant)`, or
the discovery responder not being the component registration wrote to.

Suggested next step (server-side, needs etcd access): on the box, compare
`etcdctl get --prefix /device-connect/<tenant>/devices/` against what the
registry-service's `discovery/listDevices` returns for the same tenant, and
confirm the discovery responder and the registration writer share one etcd +
the same `/device-connect/{tenant}/devices/{id}` key scheme.

### Deployment item still pending (from round 3)
`ZENOH_PUBLIC_HOST` is still unset — freshly downloaded creds advertise
`zenoh+tls://zenoh:7447` (internal). Set `ZENOH_PUBLIC_HOST=<public-ip>` and
re-issue creds for zero-override external connect.

## Round-5 resolution (2026-06-07) — both items closed

Ran the round-4 repro on the box with the suggested etcd diff. Result: **no
registry-side divergence — #2 was a client-side tenant bug, already fixed by
`aab181d`.**

### #2 bus discovery — RESOLVED (by the tenant-resolution fix)

Reproduced live (tenant `tz-retest`, two registered devices):

- etcd `/device-connect/tz-retest/devices/` holds **both** devices.
- raw `discovery/listDevices` to `device-connect.tz-retest.discovery` returns
  **both** devices.
- high-level `discover('device(*)')` → **2**, `discover('device(<id>)')` → 1,
  `invoke('device(<id>).function(get_state)')` → success.

The registry handler and key scheme are correct: `register()` and
`list_devices()` share `/device-connect/{tenant}/devices/{id}` on the same
etcd, and `_extract_tenant` handles the Zenoh slash form. The empty roster was
the **client** querying `device-connect.`**`default`**`.discovery`: agent-tools
defaulted `zone="default"`, so `RegistryClient(tenant=self.zone)` asked the
wrong tenant. `aab181d` resolves the zone from the credential's `tenant`
(explicit → cred tenant → `TENANT` env → `"default"`), so discovery now hits
the right tenant.

Why it *looked* tenant-independent before: `invoke()` addresses the device's
`.cmd` subject directly and the portal `devices list` reads the **provisioned**
store — so both worked while the registry roster (keyed by tenant) came back
empty. The divergence was portal-roster-source vs registry-tenant, not a
registry bug.

### Deployment item — RESOLVED

`ZENOH_PUBLIC_HOST=137.184.86.16` is set on the live portal (persisted in the
gitignored `infra/.env`); a freshly provisioned device's cred advertises
`zenoh+tls://137.184.86.16:7447` and connects from the credential alone.

### Doc — AGENTS.md §2 fixed

§2 now uses the current selector API (`discover()` / `discover_labels()` /
`invoke()`); the deprecated `describe_fleet` / `list_devices` /
`get_device_functions` / `invoke_device` meta-tools are flagged as deprecated.
