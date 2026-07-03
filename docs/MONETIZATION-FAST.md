# Engraphis — Making money faster (freemium, done right)

Your instinct is correct: **free trial → subscribe** is the right model. One correction saves you
from killing your own product — **do not put the usage limit on the local core.** Here's why, then
exactly where the paywall goes and how to get cash fastest.

## Why not limit the local engine
1. **It's unenforceable.** The core is Apache-2.0 and runs on the *user's* machine. Anyone can read
   the code and delete the limit, or fork it. A cap there is theater.
2. **There's no cost to recoup.** mem0 and Zep meter you because *they* pay for the servers your
   memories live on. Engraphis's free core runs on the user's own hardware — you pay nothing per
   memory, so there's nothing to bill back. A limit there is pure friction that kills adoption and
   throws away your one winning line — *"no per-token cost, no metering"* — the exact thing that
   beats mem0 and fills the gap Zep left when it dropped self-hosting.

The limit belongs where you **actually bear cost and can enforce it**: hosted sync, team memory,
and closed-source Pro add-ons.

## The model — same "try free → subscribe," placed correctly

| Tier | Price (anchored to competitors) | What's in it | Where the limit lives |
|---|---|---|---|
| **Free** (local, OSS, unlimited) | $0 | Engine + MCP server + CLI + single-user local Inspector. Yours forever, on your machine. | None — this is the trial that hooks them |
| **Pro** (individual) | ~$15–19/mo or **$99/yr founding** | Hosted memory **sync across your devices**, cloud backup, Inspector Pro dashboard, priority support | Free = 1 device / small sync quota → Pro = unlimited |
| **Team** | ~$25–40/seat/mo | **Shared team memory**, RBAC/SSO, audit exports, encryption-at-rest | Per seat |
| **Cloud / Enterprise** (later) | custom | Managed hosting, SLA, on-prem help | — |

The "try it, then subscribe" path stays intact: they run the free local tool, love it, and the
moment they want their memory **on their laptop *and* desktop, backed up, or shared with a
teammate** — that's the wall, and it's one worth paying to cross. Price it flat and undercut mem0
($19/$79/$249); "no per-memory metering" *is* your ad.

> Prices anchored to your own GTM table (mem0 $19/$79/$249, Letta $20, Zep $125+). Re-check before
> publishing — they move.

## What to build first for money
**Multi-device / team memory sync.** It's the natural upgrade (persistent memory that people
immediately want *everywhere*), it's server-side so the limit is real, and your GTM already named
it as the Pro wedge. Your Memory Inspector already exists — hosting it as the Pro dashboard is a
fast paid surface. Roughly 2–4 focused weeks *after* launch. Don't start it until the free tool is
live and getting users.

## Fastest actual cash — in order (the "faster" you asked for)
Subscriptions need volume to matter (2% of 50 users = 1 customer), so the fast money at launch
isn't the subscription — it's capturing *committed demand*:

1. **Founding-member prepay (launch day).** Stand up a Stripe/Gumroad link:
   *"Engraphis Pro — Founding: $99/yr, locked forever, hosted sync the day it ships."* You collect
   real cash and validate willingness-to-pay **before** building the cloud. Single fastest honest
   dollar — and the prepay count tells you whether Pro is even worth building.
2. **GitHub Sponsors** — already configured in `FUNDING.yml`. Turn on tiers; announce it in the
   launch post. Small but immediate.
3. **Paid setup/tuning for teams** — *"I'll deploy + tune Engraphis for your team, $X."* Fastest
   dollars-per-hour for a technical founder; doesn't scale, but it funds runway and puts you in the
   room with your first paying teams.

## Sequencing
Launch free → capture founding prepay + waitlist on day one → build team/multi-device sync as the
first paid feature → convert the waitlist. **Distribution first; monetize the demand.** Trying to
charge before anyone uses it is exactly why it feels stuck.

---
*Not legal or financial advice. You can keep the core Apache-2.0 and ship Pro features as a
separate, commercially-licensed package (`engraphis-pro`) — Apache is permissive, so new code can
carry your terms while the open core stays open. If you ever accept outside code contributions,
use a contributor agreement so you retain the right to do this — looks solo today, so low risk.
Validate the "Engraphis" trademark and your license terms with a professional before charging.*
