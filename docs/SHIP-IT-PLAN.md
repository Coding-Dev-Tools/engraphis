# Engraphis — Ship-It Plan (one page, on purpose)

You don't have a marketing problem or a mindset problem. **Engraphis is built.** It's just
never been *shipped*: it's not on PyPI, your best code isn't merged to `main`, and no human
has been told it exists. Your landing copy and go-to-market doc are already written and sharp.
This plan just executes the last, smallest, scariest step.

**Positioning (straight from your own GTM — don't reopen it):**
> Local-first memory for coding agents. MCP server, runs on your machine, no cloud, no per-token cost.

**The one rule for the next month:** no new features, no new audits, no new handoff docs, no
touching the other three products or Hermes. Only the checkboxes below. Everything else is parked.

---

## ~7 hrs/week × 4 weeks

### Week 1 — Publish the code (≈3–4 hrs)
- [ ] Commit the working tree; merge `improve/engraphis-20260702` → `main`; push.
- [ ] Confirm the GitHub repo is **public** and the README renders correctly.
- [ ] Drop one screenshot or GIF of the Memory Inspector near the top of the README.

### Week 2 — Let a stranger install it in 2 minutes (≈3 hrs)
- [ ] Publish to PyPI so `pip install engraphis` works. **The name is free.** Add a release workflow (you already have `ci.yml` to copy from).
- [ ] Record a 60–90s demo: add engraphis to Claude Code → it recalls a fact across a restart → open the Inspector. **This one asset is 80% of your marketing.**

### Week 3 — Tell the warm audience (≈2 hrs)
- [ ] Post the launch write-up (already drafted → `LAUNCH-POST.md`) to **Show HN**, **r/LocalLLaMA / r/ClaudeAI**, and as an **X thread**.
- [ ] Spend that day replying to comments. That is the entire "promotion."

### Week 4 — Turn attention into a money signal (≈2 hrs)
- [ ] Announce **GitHub Sponsors** (already configured in `FUNDING.yml`).
- [ ] Add a **"Pro / Team tier — join the waitlist"** email-capture link to the README + landing page. Waitlist size tells you whether the paid tier is worth building.

---

## The money question, straight

An Apache-2.0 core won't produce revenue on day one — and that's fine. What you're building
*this* month is **users + GitHub stars + a waitlist** in a hot category (mem0/Zep/Letta are
funded; Zep abandoned self-hosting — that gap is yours). Money comes next, from the open-core
**Pro/Team tier your GTM already defines**, or sponsorship, once there's adoption.
Distribution first; monetize the demand. Trying to charge before anyone uses it is the reason
it feels stuck.

## The mental-state question, straight

The motivation you're waiting for arrives *after* the first person says "oh, nice" — not
before. And promoting feels awful right now partly because promoting something nobody can
install feels like lying. Once `pip install engraphis` works and the repo is public,
"promotion" stops being a performance and becomes "showing people a thing that works." That's
a completely different, much lighter feeling — and it's two weeks away, not a personality change away.
