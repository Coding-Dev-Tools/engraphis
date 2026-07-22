# Licensing and commercial service boundary

Engraphis uses an open-core/service model with two boundaries that must not be confused:
the license on published source code and authorization to use the official hosted service.

## Public Apache layer

Everything released in this public repository is licensed under Apache-2.0, including the
local memory engine, stores, retrieval pipeline, MCP and CLI clients, dashboard code,
customer-side cloud-session and relay protocols, and other code present here. Subject to the
license terms, recipients may use, modify, redistribute, and fork those releases. The
hosted vendor, relay, compute, and worker implementations are not distributed here.

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

Cloud Sync storage and authorization, Analytics computation, Automation scheduling, Auto
Dreaming, Auto Consolidation, managed workers, and Team identity/seat administration are part of
that private service boundary. The public package keeps only customer protocols, consent/status
surfaces, and the free manual consolidation action.

Code already published here remains Apache-2.0 even if an analogous capability is later
implemented in a private service. New private work should have a clear provenance and
copyright boundary so its distribution terms are unambiguous.

## Public runtime boundary

The public package accepts only `ENGRAPHIS_SERVICE_MODE=customer`. It contains the local
memory engine and customer clients, but no license issuer, entitlement registry, billing
fulfillment, transactional-email worker, hosted relay server, managed-compute server, or
vendor administration API. Attempts to select the former `vendor`, `relay`, or `combined`
roles are rejected at startup.

The official hosted control, relay, compute, and worker services are built and operated from
a private repository. Public clients communicate with those services through authenticated,
versioned protocols. This boundary protects new commercial implementation work; it does not
change the Apache license or prevent a fork from modifying code already released here.

## Trial, grace, and recovery

The server-issued Pro or Team trial lasts **exactly 3 active days**. Grace is a separately
named operational state and never turns that into a four-day trial.

`workspace_write_grace` can preserve an already provisioned installation after authoritative
entitlement expiry or denial for **up to 24 hours**. The private control plane anchors and caps
that window; restarting a public client or moving its clock cannot reset it.

During this grace state, the already provisioned installation may continue ordinary local-core
workspace writes. Paid or cost-bearing features and hosted agent writes still require live
authorization and may stop immediately. Grace cannot:

- extend the signed trial or paid entitlement expiry;
- enable a new installation or activation;
- add hosted users, seats, invitations, devices, or credentials;
- permit administrative growth; or
- reset any expiry or grace clock.

After grace, the installation enters `recovery_read_only`. Local reads, data export, and
relicensing remain available while normal mutations are blocked. Existing customer data is
therefore recoverable without granting continuing paid or Team capability.

## Forks, service access, and trademarks

A fork may lawfully exercise the rights Apache-2.0 grants over the published code, but that
does not confer an official Engraphis subscription, hosted capacity, production credentials,
support, or permission to present the fork as the official Engraphis service. See [LICENSE](../LICENSE),
[NOTICE](../NOTICE), and [SECURITY.md](../SECURITY.md) for the controlling repository terms and
deployment guidance.
