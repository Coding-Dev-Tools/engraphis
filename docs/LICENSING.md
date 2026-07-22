# Licensing and commercial service boundary

Engraphis uses an open-core/service model with two boundaries that must not be confused:
the license on published source code and authorization to use the official hosted service.

## Public Apache layer

Everything released in this public repository is licensed under Apache-2.0, including the
local memory engine, stores, retrieval pipeline, MCP and CLI clients, dashboard code,
protocols, and the customer and vendor support code currently present here. Subject to the
license terms, recipients may use, modify, redistribute, and fork those releases.

Apache-2.0 is a perpetual, irrevocable grant for code already distributed under it. A later
release, repository reorganization, or commercial strategy cannot retroactively withdraw
those rights from copies people already received. Redistributors must satisfy the license's
notice and attribution requirements. The code license does not grant rights to the Engraphis
name, marks, hosted accounts, production data, credentials, or support service.

This means a runtime mode or local license check is a deployment safeguard, not DRM: anyone
who controls an Apache-licensed fork can change it. Capabilities that must remain proprietary
must be developed and delivered separately rather than published in this repository.

## Private hosted control-plane value

Access to the official managed services is separate from the Apache code grant. The private
commercial boundary includes the production signing keys, account and entitlement records,
device and seat allocations, billing and email credentials, managed infrastructure, backups,
monitoring, operations, support, and any future commercial modules delivered separately from
the public repository. None of those secrets or production records belongs in a public image,
customer deployment, source archive, test fixture, or documentation example.

Code already published here remains Apache-2.0 even if an analogous capability is later
implemented in a private service. New private work should have a clear provenance and
copyright boundary so its distribution terms are unambiguous.

## Service modes

`ENGRAPHIS_SERVICE_MODE` separates deployment trust domains:

| Mode | Intended use | Boundary |
|---|---|---|
| `customer` | Default for normal installs and hosted dashboards | Dashboard, memory, and customer sync surfaces; vendor issuance, billing, email, and administration routes are absent. |
| `relay` | Explicit selection for the Engraphis-managed sync data plane | Bundle transport, liveness/readiness, and the hard-sunset legacy proxy only; dashboard, memory, customer auth, billing, and license issuance routes are absent. |
| `vendor` | Explicit selection on the isolated official control plane | License issuance and leases, billing fulfillment, transactional email, and vendor operations. Its secrets and state stay on the operator-controlled host. |
| `combined` | Explicit local development and test compatibility only | Mounts both roles and must never be used for a production customer or vendor service. |

The default is deliberately `customer`, so an omitted environment variable does not merge
the trust domains. Selecting a mode controls which routes a deployment exposes; it does not
change the Apache license or prevent a fork from modifying source code.

## Trial, grace, and recovery

The server-issued Pro or Team trial lasts **exactly 3 active days**. Grace is a separately
named operational state and never turns that into a four-day trial.

`workspace_write_grace` can preserve an already activated or provisioned installation after
entitlement or lease loss for **up to 24 hours**. It is restart-safe and bounded by a monotonic
clock high-water mark. The window is anchored to the signed expiry when that time has already
passed, or to the first authoritative denial otherwise. A still-valid cached lease remains
active before either condition and its unused lifetime is not subtracted from the grace window;
restarting or moving the system clock cannot reset the window.

During this grace state, authenticated existing users may continue ordinary local-core
workspace writes. Paid or cost-bearing features and MCP/agent writes still require a live
lease and may stop immediately. Grace cannot:

- extend the signed trial or paid entitlement expiry;
- enable a new installation or activation;
- create the initial administrator or add users, seats, invitations, or tokens;
- permit administrative growth; or
- reset any expiry or grace clock.

After grace, the installation enters `recovery_read_only`. The existing login wall remains;
login and password recovery, authenticated reads, data export, and relicensing remain
available, while normal mutations and Team administration are blocked. Existing customer data
is therefore recoverable without granting continuing paid capability.

## Forks, service access, and trademarks

A fork may lawfully exercise the rights Apache-2.0 grants over the published code, but that
does not confer an official Engraphis subscription, hosted capacity, production credentials,
support, or permission to present the fork as the official Engraphis service. See [LICENSE](../LICENSE),
[NOTICE](../NOTICE), and [SECURITY.md](../SECURITY.md) for the controlling repository terms and
deployment guidance.
