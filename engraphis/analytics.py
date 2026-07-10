"""Workspace analytics — the Pro dashboard's data layer.

House style (AGENTS.md §3): the math is a pure, tested function over plain rows
(:func:`analytics_from_rows`); SQL lives only in the thin :func:`compute_analytics`
wrapper. stdlib + core only — no new dependency, no LLM, no network.

Gating: ``require_feature("analytics")`` is called inside :func:`compute_analytics`.
Every caller — the Inspector, the v1 dashboard, the v2 dashboard — passes through
this one gate, so a bypass-er has to find and modify the compiled licensing module
rather than just deleting a decorator.
"""
from __future__ import annotations

import html as _html
import math
import time
from typing import Any, Iterable, Optional

from engraphis.core.scoring import retention

#: Retention level treated as "effectively forgotten" — matches the consolidation
#: sweep's ``archive_below`` default so the forecast predicts what the next sweep does.
FORGET_THRESHOLD = 0.05

_WEEK = 7 * 86400.0
_GROWTH_WEEKS = 12
_HIST_BUCKETS = 5


def _days_until_forgotten(stability: float, last_access: Optional[float],
                          now: float) -> float:
    """Days until Ebbinghaus retention exp(-Δt/S) crosses :data:`FORGET_THRESHOLD`.

    Solving exp(-dt/S) = T gives dt = S·ln(1/T); subtract days already elapsed.
    Returns +inf for pinned-style stability outliers only via natural math (S huge).
    """
    s = max(stability or 1.0, 1e-3)
    horizon_days = s * math.log(1.0 / FORGET_THRESHOLD)
    elapsed_days = max((now - (last_access if last_access is not None else now)) / 86400.0, 0.0)
    return horizon_days - elapsed_days


def analytics_from_rows(mem_rows: Iterable[dict], audit_counts: dict,
                        entity_rows: Iterable[dict], *, now: Optional[float] = None) -> dict:
    """Pure aggregation. ``mem_rows`` need: mtype, stability, last_access, ingested_at,
    importance, pinned, valid_to, expired_at. ``audit_counts`` maps action→count.
    ``entity_rows`` need: name, etype, n (mention/edge count)."""
    now = time.time() if now is None else now
    rows = list(mem_rows)
    live = [r for r in rows
            if r.get("expired_at") is None
            and (r.get("valid_to") is None or now < r["valid_to"])]

    # ── growth: weekly ingest counts, oldest→newest, exactly _GROWTH_WEEKS buckets ──
    growth = [0] * _GROWTH_WEEKS
    for r in rows:
        ts = r.get("ingested_at")
        if ts is None:
            continue
        idx = _GROWTH_WEEKS - 1 - int((now - ts) // _WEEK)
        if 0 <= idx < _GROWTH_WEEKS:
            growth[idx] += 1

    # ── retention histogram over live memories ──
    hist = [0] * _HIST_BUCKETS
    ret_sum = 0.0
    for r in live:
        ret = retention(r.get("stability") or 1.0, r.get("last_access"), now)
        ret_sum += ret
        hist[min(int(ret * _HIST_BUCKETS), _HIST_BUCKETS - 1)] += 1

    # ── decay forecast (pinned memories are exempt from decay archival) ──
    at_risk_7 = at_risk_30 = 0
    for r in live:
        if r.get("pinned"):
            continue
        days = _days_until_forgotten(r.get("stability") or 1.0, r.get("last_access"), now)
        if days <= 7:
            at_risk_7 += 1
        if days <= 30:
            at_risk_30 += 1

    by_type: dict = {}
    for r in live:
        by_type[r.get("mtype") or "?"] = by_type.get(r.get("mtype") or "?", 0) + 1

    total = len(rows)
    n_live = len(live)
    return {
        "generated_at": now,
        "totals": {
            "live": n_live,
            "all_rows": total,
            "superseded": total - n_live,
            "pinned": sum(1 for r in live if r.get("pinned")),
            "avg_retention": round(ret_sum / n_live, 4) if n_live else 0.0,
        },
        "growth_weekly": growth,
        "retention_histogram": {
            "buckets": ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"],
            "counts": hist,
        },
        "decay_forecast": {
            "threshold": FORGET_THRESHOLD,
            "at_risk_7d": at_risk_7,
            "at_risk_30d": at_risk_30,
        },
        "by_type": by_type,
        "resolver_mix": {k: int(v) for k, v in sorted(audit_counts.items())},
        "top_entities": [
            {"name": e["name"], "etype": e.get("etype") or "", "n": int(e.get("n") or 0)}
            for e in entity_rows
        ],
    }


_REPORT_CSS = """
body{font:14px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#1f2328;
  background:#fff;margin:0;padding:32px;max-width:880px}
h1{font-size:22px;margin:0 0 2px}
h2{font-size:15px;margin:26px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
.sub{color:#59636e;font-size:13px;margin:0 0 18px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #e4e8ec;vertical-align:top}
th{color:#59636e;font-weight:600;background:#f6f8fa}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{background:#dbe6f4;height:12px;border-radius:3px;display:inline-block;
  vertical-align:middle;min-width:2px}
.footer{color:#59636e;font-size:12px;margin-top:28px;border-top:1px solid #d0d7de;
  padding-top:8px}
""".strip()


def _esc(value: Any) -> str:
    return _html.escape(str(value), quote=True)


def _table(headers: list, rows: list) -> str:
    """rows are lists of (text, is_numeric) cells, already escaped by the caller."""
    head = "".join("<th>%s</th>" % h for h in headers)
    body = "".join(
        "<tr>%s</tr>" % "".join(
            '<td class="num">%s</td>' % c[0] if c[1] else "<td>%s</td>" % c[0]
            for c in row)
        for row in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)


def _bar_cell(n: int, peak: int) -> str:
    width = int(round(n / peak * 160)) if peak else 0
    return '<span class="bar" style="width:%dpx"></span> %d' % (width, n)


def render_analytics_html(data: dict, *, workspace: str, version: str = "") -> str:
    """A self-contained HTML report over the :func:`analytics_from_rows` payload.

    Everything inline (CSS included), zero external requests — the file can be
    archived, emailed, or opened offline years later and still render. All dynamic
    text is escaped; nothing from the store reaches the page unescaped."""
    t = data.get("totals", {})
    f = data.get("decay_forecast", {})
    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                              time.gmtime(data.get("generated_at", time.time())))

    totals_rows = [
        [("Live memories", False), (_esc(t.get("live", 0)), True)],
        [("All rows (incl. superseded history)", False), (_esc(t.get("all_rows", 0)), True)],
        [("Superseded (kept in history)", False), (_esc(t.get("superseded", 0)), True)],
        [("Pinned (decay-exempt)", False), (_esc(t.get("pinned", 0)), True)],
        [("Average retention", False),
         ("%.0f%%" % (float(t.get("avg_retention", 0.0)) * 100), True)],
        [("Fading within 7 days", False), (_esc(f.get("at_risk_7d", 0)), True)],
        [("Fading within 30 days", False), (_esc(f.get("at_risk_30d", 0)), True)],
    ]

    weeks = list(data.get("growth_weekly", []))
    peak = max(weeks) if weeks else 0
    growth_rows = [
        [("now" if back == 0 else "%d week%s ago" % (back, "" if back == 1 else "s"), False),
         (_bar_cell(n, peak), True)]
        for back, n in ((len(weeks) - 1 - i, n) for i, n in enumerate(weeks))]

    hist = data.get("retention_histogram", {})
    counts = list(hist.get("counts", []))
    hpeak = max(counts) if counts else 0
    hist_rows = [[(_esc(b), False), (_bar_cell(n, hpeak), True)]
                 for b, n in zip(hist.get("buckets", []), counts)]

    type_rows = [[(_esc(k), False), (_esc(v), True)]
                 for k, v in sorted(data.get("by_type", {}).items())]
    mix_rows = [[(_esc(k), False), (_esc(v), True)]
                for k, v in sorted(data.get("resolver_mix", {}).items())]
    entity_rows = [
        [(_esc(e.get("name", "")), False), (_esc(e.get("etype", "")), False),
         (_esc(e.get("n", 0)), True)]
        for e in data.get("top_entities", [])]

    def section(title: str, body: str, empty_note: str = "nothing recorded yet") -> str:
        return "<h2>%s</h2>%s" % (
            _esc(title), body or "<p class=\"sub\">%s</p>" % _esc(empty_note))

    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Engraphis analytics — %s</title>" % _esc(workspace),
        "<style>%s</style></head><body>" % _REPORT_CSS,
        "<h1>Engraphis analytics report</h1>",
        "<p class=\"sub\">workspace <strong>%s</strong> · generated %s%s</p>" % (
            _esc(workspace), _esc(generated),
            " · engraphis v%s" % _esc(version) if version else ""),
        section("Totals & decay forecast", _table(["Metric", "Value"], totals_rows)),
        section("Memories written per week", _table(["Week", "Written"], growth_rows)),
        section("Retention distribution (live memories)",
                _table(["Retention", "Memories"], hist_rows)),
        section("Live memories by type", _table(["Type", "Count"], type_rows)
                if type_rows else ""),
        section("Write-path resolver activity", _table(["Action", "Count"], mix_rows)
                if mix_rows else "", "no resolver events yet"),
        section("Most connected entities",
                _table(["Entity", "Type", "Connections"], entity_rows)
                if entity_rows else "", "no entities yet — they appear as the graph grows"),
        "<p class=\"footer\">Self-contained report — no external assets, no tracking. "
        "Engraphis Pro analytics%s.</p>" % (" · v%s" % _esc(version) if version else ""),
        "</body></html>",
    ]
    return "".join(parts)


def compute_analytics(store: Any, workspace_id: str, *, now: Optional[float] = None) -> dict:
    """SQL wrapper: fetch rows for one workspace, delegate to the pure function.

    Gate lives here so every caller (Inspector, v1 dashboard, v2 dashboard)
    passes through a single, auditable check inside the computation module
    rather than at every HTTP endpoint."""
    from engraphis.licensing import require_feature
    require_feature("analytics")

    conn = store.conn
    mem_rows = [dict(r) for r in conn.execute(
        "SELECT mtype, stability, last_access, ingested_at, importance, pinned, "
        "valid_to, expired_at FROM memories WHERE workspace_id=?", (workspace_id,))]
    audit_counts = {r["action"]: r["n"] for r in conn.execute(
        "SELECT a.action, COUNT(*) AS n FROM audit a JOIN memories m ON m.id = a.target "
        "WHERE m.workspace_id=? GROUP BY a.action", (workspace_id,))}
    entity_rows = [dict(r) for r in conn.execute(
        "SELECT e.name, e.etype, COUNT(ed.id) AS n FROM entities e "
        "LEFT JOIN edges ed ON (ed.src = e.id OR ed.dst = e.id) "
        "WHERE e.workspace_id=? GROUP BY e.id, e.name, e.etype "
        "ORDER BY n DESC, e.created_at DESC LIMIT 8", (workspace_id,))]
    return analytics_from_rows(mem_rows, audit_counts, entity_rows, now=now)


# ── cross-workspace portfolio view (Pro) ──────────────────────────────────────

#: Cap on merged top_entities in the portfolio payload — same width as the
#: per-workspace query's LIMIT so the rollup never widens what one page shows.
_PORTFOLIO_TOP_ENTITIES = 8


def portfolio_rollup(per_workspace: dict) -> dict:
    """Pure cross-workspace aggregation over :func:`analytics_from_rows` payloads.

    ``per_workspace`` maps workspace *name* → analytics payload. All payloads must
    share the same ``now`` (see :func:`compute_portfolio`) so weekly growth buckets
    align; sums are then exact. ``avg_retention`` is re-weighted by live counts
    rather than naively averaged. Per-workspace summary rows are sorted by live
    count (desc), then name, so the busiest workspace leads the view.
    """
    growth = [0] * _GROWTH_WEEKS
    hist = [0] * _HIST_BUCKETS
    live = all_rows = superseded = pinned = at_risk_7 = at_risk_30 = 0
    ret_weighted = 0.0
    by_type: dict = {}
    resolver_mix: dict = {}
    entities: list = []
    ws_rows: list = []
    generated = 0.0

    for name in sorted(per_workspace):
        d = per_workspace[name]
        t = d.get("totals") or {}
        f = d.get("decay_forecast") or {}
        n_live = int(t.get("live", 0))
        live += n_live
        all_rows += int(t.get("all_rows", 0))
        superseded += int(t.get("superseded", 0))
        pinned += int(t.get("pinned", 0))
        ret_weighted += float(t.get("avg_retention", 0.0)) * n_live
        at_risk_7 += int(f.get("at_risk_7d", 0))
        at_risk_30 += int(f.get("at_risk_30d", 0))
        for i, n in enumerate(list(d.get("growth_weekly") or [])[-_GROWTH_WEEKS:]):
            growth[i] += int(n)
        counts = list((d.get("retention_histogram") or {}).get("counts") or [])
        for i, n in enumerate(counts[:_HIST_BUCKETS]):
            hist[i] += int(n)
        for k, v in (d.get("by_type") or {}).items():
            by_type[k] = by_type.get(k, 0) + int(v)
        for k, v in (d.get("resolver_mix") or {}).items():
            resolver_mix[k] = resolver_mix.get(k, 0) + int(v)
        for e in d.get("top_entities") or []:
            entities.append({"name": e.get("name", ""), "etype": e.get("etype", ""),
                             "n": int(e.get("n") or 0), "workspace": name})
        generated = max(generated, float(d.get("generated_at") or 0.0))
        ws_rows.append({
            "workspace": name, "live": n_live,
            "all_rows": int(t.get("all_rows", 0)),
            "superseded": int(t.get("superseded", 0)),
            "pinned": int(t.get("pinned", 0)),
            "avg_retention": float(t.get("avg_retention", 0.0)),
            "at_risk_7d": int(f.get("at_risk_7d", 0)),
            "at_risk_30d": int(f.get("at_risk_30d", 0)),
        })

    ws_rows.sort(key=lambda w: (-w["live"], w["workspace"]))
    entities.sort(key=lambda e: (-e["n"], e["name"]))
    return {
        "generated_at": generated or time.time(),
        "workspaces": ws_rows,
        "totals": {
            "workspaces": len(ws_rows),
            "live": live,
            "all_rows": all_rows,
            "superseded": superseded,
            "pinned": pinned,
            "avg_retention": round(ret_weighted / live, 4) if live else 0.0,
        },
        "growth_weekly": growth,
        "retention_histogram": {
            "buckets": ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"],
            "counts": hist,
        },
        "decay_forecast": {
            "threshold": FORGET_THRESHOLD,
            "at_risk_7d": at_risk_7,
            "at_risk_30d": at_risk_30,
        },
        "by_type": by_type,
        "resolver_mix": {k: int(v) for k, v in sorted(resolver_mix.items())},
        "top_entities": entities[:_PORTFOLIO_TOP_ENTITIES],
    }


def compute_portfolio(store: Any, workspaces: Iterable, *,
                      now: Optional[float] = None) -> dict:
    """SQL wrapper for the cross-workspace portfolio view.

    ``workspaces`` is an iterable of ``(workspace_id, name)`` pairs — the
    *caller's permitted set* (e.g. what ``MemoryService.list_workspaces`` returns
    under its team-auth boundary). This function never enumerates workspaces
    itself, so it cannot widen access beyond what the caller may see.

    Gate: ``require_feature("analytics")`` here, plus once per workspace inside
    :func:`compute_analytics` — same single "analytics" feature as the
    single-workspace dashboard, checked in the computation module per house rule.
    A shared ``now`` keeps every workspace's weekly buckets aligned for the sum.
    """
    from engraphis.licensing import require_feature
    require_feature("analytics")

    now = time.time() if now is None else now
    return portfolio_rollup({
        name: compute_analytics(store, wid, now=now) for wid, name in workspaces})
