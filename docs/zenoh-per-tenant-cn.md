# Zenoh per-tenant-CN authorization model

This document explains how the multi-tenant Zenoh deployment authorizes
devices, why it is designed the way it is, and the trade-off it makes.

## TL;DR

- Every device in a tenant gets its **own key pair** but a TLS client
  certificate whose **Common Name (CN) is the tenant** (the device id lives
  in the cert **OU** and the application-layer `device_id`, not the CN).
- The Zenoh router ACL therefore has **one static rule per tenant**
  (`cert_common_names: [tenant]` → `device-connect/{tenant}/**`).
- **Provisioning and revoking a device never change the ACL**, so they need
  **no router restart**. Only **tenant creation/deletion** touches the ACL.
- The cost: **per-device revocation is *soft*** (the shared CN cannot be
  denied individually at the broker). Use **short-lived certs** and/or
  **hard tenant revocation** for cert-level cutoff.

## Background: why a reload was needed

Zenoh enforces tenant isolation with its mTLS **access-control (ACL)**
plugin, matching each connection's client-cert CN against allow rules. The
ACL is **loaded once at router startup and cannot be hot-reloaded** — there
is no admin-space update, no SIGHUP, no config-file watch for it. This is
confirmed in the current Zenoh release (1.8 "Kiyohime") and the Access
Control RFC; the maintainers themselves note static per-node ACLs "do not
scale well for larger and dynamic environments"
([zenoh#1432](https://github.com/eclipse-zenoh/zenoh/issues/1432)).

In the original design the ACL listed **every device's CN** under its
tenant. So **every** device provision/revoke edited the ACL and required a
full **router restart**, which drops *all* tenants' device sessions for
~10–25 s (they auto-reconnect). At any real provisioning rate this is a
recurring, fleet-wide blip — a cross-tenant coupling of a single-tenant
operation.

Since the ACL cannot be reloaded in place, the only way to make device
operations cheap is to **stop changing the ACL on the device hot path**.

## The model

A Zenoh ACL subject can match on the cert **CN**, and — crucially —
**multiple distinct certificates may share one CN**. So:

| Identity | Carried in | Used by |
|---|---|---|
| **Tenant** | cert **CN** | the **router ACL** (tenant isolation) |
| **Device** | cert **OU** + creds `device_id` | audit / the **registry** (app layer) |

Each device still gets a unique key pair (compromise of one device doesn't
expose another's key), but because they all present `CN=tenant`, **one
static ACL subject authorizes the whole tenant**:

```json5
{ "id": "tenant-acme", "cert_common_names": ["acme"] }      // subject
{ "id": "tenant-acme", "key_exprs": ["device-connect/acme/**"], "permission": "allow" }  // rule
```

Adding the 1st or the 1000th device of `acme` produces a new cert with
`CN=acme` that already matches this rule. **No ACL edit, no restart.**

This was validated empirically (two different certs sharing a CN are both
authorized by the one rule; a different-CN cert is denied).

### The reload gate

Because device add/remove must produce **no** restart, `reload_broker()` is
**gated on an actual config change**: it hashes `zenoh-config.json5` and
only signals the reloader sidecar when the hash differs from the
last-reloaded baseline (recorded at bootstrap and on each reload). Device
provisioning/revocation leave the config byte-identical → the restart is
skipped. Tenant create/delete change the config → one debounced restart.

## What happens on each operation

| Operation | ACL change? | Router restart? |
|---|---|---|
| **Create tenant** (signup) | yes — add `CN=tenant` subject/rule/policy | **yes** (once, debounced) |
| **Provision device** | no | **no** |
| **Revoke device** (soft) | no | **no** |
| **Hard-revoke / delete tenant** | yes — drop the tenant subject/rule/policy | **yes** (once, debounced) |

So the frequent operations (device provision/revoke) are reload-free; only
the rare structural operations (tenant lifecycle) restart the router, and
even those are coalesced by the debouncing reloader sidecar.

## The trade-off: revocation is soft per device

Because all of a tenant's devices share `CN=tenant`, the broker **cannot
deny one device** without denying the whole tenant. So:

- **`revoke <device>` is soft.** It deletes the credential and the device's
  cert/key on the portal (so the creds can't be re-downloaded and the device
  disappears from the portal/registry), **but a cert already deployed stays
  cryptographically valid until it expires.** The revoke API returns a
  `backend_warning` saying exactly this.
- **Hard cutoff options:**
  - **Short-lived device certs + renewal** (recommended). Bound the
    soft-revocation window to the cert lifetime; "revoking" = stop renewing.
    (`CERT_DAYS` in `zenoh_pki.py`; renewal flow is a follow-up.)
  - **Hard-revoke the whole tenant** — `zenoh_acl.remove_tenant_rule(tenant)`
    drops the tenant CN so *none* of its certs are authorized after the
    (one) reload. This cuts off every device in the tenant.

This is an explicit, documented trade-off: we exchange immediate per-device
ACL-based revocation (which forced a restart on every device op) for
reload-free provisioning. It mirrors how app-layer authorization (e.g.
verifiable mandates) is expected to carry fine-grained, per-agent
revocation independently of the broker.

## Migration (breaking change for existing deployments)

The cert identity model changes, so this is **not backward compatible** with
an existing per-device-CN deployment:

- New device certs carry `CN=tenant`; the ACL is rewritten to per-tenant
  subjects. **Existing device certs (CN=device) will no longer match** a
  regenerated per-tenant rule.
- To migrate: re-bootstrap the PKI/ACL and **re-issue device credentials**
  (each tenant's devices get fresh `CN=tenant` certs). Tenants created under
  the old code keep working only until their ACL rule is regenerated.

For a fresh deployment there is nothing to do — bootstrap produces the
per-tenant-CN model directly.

## Related

- `services/zenoh_acl.py` — `add_tenant_rule`, `remove_tenant_rule`, and the
  now-no-op `add_devices_to_tenant` / `remove_devices_from_tenant`.
- `services/zenoh_pki.py` — `generate_client_cert(name, common_name=tenant)`
  (CN=tenant, OU=device).
- `services/zenoh_admin.py` — the hash-gated reload.
- Upstream: [Zenoh Access Control](https://zenoh.io/docs/manual/access-control/),
  [zenoh#1432 trust-based authorization](https://github.com/eclipse-zenoh/zenoh/issues/1432).
