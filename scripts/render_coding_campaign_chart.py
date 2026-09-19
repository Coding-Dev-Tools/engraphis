"""Render observed coding outcomes from a checksummed public campaign artifact."""
from __future__ import annotations

import argparse
from collections import Counter
import itertools
import json
from pathlib import Path

from eval.benchmark import canonical_json, sha256_file, sha256_text, validate_report


def chart_data(report: dict, manifest: dict, eligibility: dict = None) -> dict:
    """Recompute every count, keeping unscored and missing attempts visible."""
    metrics = report["metrics"]
    unsigned = {key: value for key, value in manifest.items() if key != "binding_sha256"}
    if (manifest.get("binding_sha256") != sha256_text(canonical_json(unsigned))
            or metrics.get("campaign_sha256") != manifest["binding_sha256"]):
        raise ValueError("campaign binding mismatch")
    stage = manifest["stages"][metrics["stage"]]
    keys = ("scenario_id", "arm", "token_budget", "repetition")
    expected = set(itertools.product(stage["scenario_ids"], stage["arms"],
                                     stage["token_budgets"], range(stage["repetitions"])))
    excluded = set()
    if eligibility is not None:
        unsigned_mask = {key: value for key, value in eligibility.items() if key != "binding_sha256"}
        parent = metrics.get("parent_campaign_sha256", manifest["binding_sha256"])
        if (eligibility.get("schema") != "engraphis-campaign-eligibility/v1"
                or eligibility.get("origin") != "implementation_team"
                or eligibility.get("binding_sha256") != sha256_text(canonical_json(unsigned_mask))
                or eligibility.get("parent_campaign_sha256") != parent
                or eligibility.get("stage") != metrics["stage"]
                or eligibility.get("retrospective") is not True):
            raise ValueError("eligibility binding mismatch")
        for cell in eligibility["exclusions"]:
            identity = tuple(cell[key] for key in keys)
            if (identity not in expected or identity in excluded
                    or cell.get("reason") != "invalid_fixture_exact_wording"):
                raise ValueError("invalid or duplicate exclusion")
            excluded.add(identity)
        scenarios = {identity[0] for identity in excluded}
        if excluded != {identity for identity in expected if identity[0] in scenarios}:
            raise ValueError("exclusions must cover whole scenarios across all arms and budgets")
    rows = {}
    statuses = Counter()
    critical = 0
    for row in report["records"]:
        identity = tuple(row[key] for key in keys)
        if identity not in expected or identity in rows:
            raise ValueError("unexpected or duplicate attempt")
        status, success = row["status"], row["task_success"]
        if status not in {"complete", "unsupported", "error"}:
            raise ValueError("unknown attempt status")
        if (status == "complete" and type(success) is not bool
                or status != "complete" and success is not None):
            raise ValueError("unscored attempt cannot become a task failure")
        count = row["critical_violation_count"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid critical count")
        critical += count
        statuses[status] += 1
        rows[identity] = row
    missing = len(expected) - len(rows)
    if (metrics["expected_attempts"] != len(expected)
            or metrics["missing_attempts"] != missing
            or Counter(metrics["statuses"]) != statuses
            or metrics["critical_violations"] != critical):
        raise ValueError("summary counts differ from observed records")
    panels = []
    for budget in stage["token_budgets"]:
        arms = []
        for arm in stage["arms"]:
            selected = [key for key in sorted(expected) if key[1] == arm and key[2] == budget]
            counts = Counter()
            for key in selected:
                row = rows.get(key)
                outcome = "invalid_fixture" if key in excluded else "missing" if row is None else row["status"]
                if outcome == "complete":
                    outcome = "success" if row["task_success"] else "task_failure"
                counts[outcome] += 1
            arms.append({"arm": arm, "expected": len(selected), "counts": dict(counts)})
        panels.append({"budget": budget, "arms": arms})
    if set(metrics["arms"]) != set(stage["arms"]):
        raise ValueError("arm aggregate inventory differs from the manifest")
    for arm, aggregate in metrics["arms"].items():
        selected = [row for row in rows.values() if row["arm"] == arm]
        if (aggregate["attempts"] != len(selected)
                or aggregate["complete"] != sum(row["status"] == "complete" for row in selected)
                or aggregate["successes"] != sum(row["task_success"] is True for row in selected)):
            raise ValueError("arm aggregate differs from observed records")
    return {"panels": panels, "expected": len(expected), "missing": missing,
            "valid_missing": len(expected - excluded - set(rows)), "excluded": len(excluded),
            "critical": critical, "statuses": dict(statuses),
            "scenarios": len(stage["scenario_ids"]), "repetitions": stage["repetitions"],
            "max_corrections": (stage["max_reader_turns"] - 1
                                if type(stage.get("max_reader_turns")) is int
                                and stage["max_reader_turns"] > 0 else None),
            "families": len({row["family_id"] for row in rows.values() if row.get("family_id")})}


def render(report_path: Path, manifest_path: Path, output: Path, eligibility_path: Path = None) -> None:
    for path in (report_path, manifest_path, *([eligibility_path] if eligibility_path else [])):
        if path.with_suffix(path.suffix + ".sha256").read_text().split()[0] != sha256_file(path):
            raise ValueError("artifact checksum mismatch")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    errors = validate_report(report)
    if errors:
        raise ValueError("invalid public artifact: " + "; ".join(errors))
    eligibility = json.loads(eligibility_path.read_text(encoding="utf-8")) if eligibility_path else None
    data = chart_data(report, json.loads(manifest_path.read_text(encoding="utf-8")), eligibility)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    colors = {"success": "#00b889", "task_failure": "#d98480", "error": "#d5a64c",
              "unsupported": "#73849c", "invalid_fixture": "#9f87ad", "missing": "#333b45"}
    labels = {"success": "Task passed", "task_failure": "Task failed", "error": "Infrastructure error",
              "unsupported": "Unsupported", "invalid_fixture": "Invalid fixture (excluded)", "missing": "Missing"}
    background, foreground, muted = "#0e1114", "#e9f1f5", "#9caebe"
    with plt.rc_context({"font.family": "DejaVu Sans", "figure.facecolor": background,
                         "axes.facecolor": background, "text.color": foreground,
                         "xtick.color": muted, "ytick.color": foreground, "svg.fonttype": "none"}):
        fig, axes = plt.subplots(1, len(data["panels"]), figsize=(12, 6.4), squeeze=False)
        for axis, panel in zip(axes[0], data["panels"]):
            for position, arm in enumerate(panel["arms"]):
                left = 0
                for outcome, color in colors.items():
                    value = arm["counts"].get(outcome, 0)
                    axis.barh(position, value, left=left, color=color, height=.52)
                    left += value
                eligible = arm["expected"] - arm["counts"].get("invalid_fixture", 0)
                axis.text(arm["expected"] + .15, position,
                          f"{arm['counts'].get('success', 0)}/{eligible}", va="center", fontsize=10)
            total = panel["arms"][0]["expected"]
            axis.set_xlim(0, total * 1.24)
            axis.set_xticks([0, total / 2, total], ["0", f"{total / 2:g}", str(total)])
            axis.set_yticks(range(len(panel["arms"])), [arm["arm"] for arm in panel["arms"]])
            axis.invert_yaxis()
            axis.set_title(f"{panel['budget']:,} evidence tokens", loc="left", color=foreground, pad=15)
            axis.grid(axis="x", color="#28313a", linewidth=.6)
            axis.set_axisbelow(True)
            for spine in axis.spines.values():
                spine.set_visible(False)
        fig.suptitle("Coding pilot: observed repository-task outcomes", x=.025, ha="left", fontsize=18)
        family_word = "family" if data["families"] == 1 else "families"
        fig.text(.025, .89, f"implementation_team | {data['scenarios']} planned scenarios | "
                 f"{data['families']} synthetic repository {family_word} | "
                 f"{data['repetitions']} repetition(s) | labels: passed / eligible",
                 fontsize=10, color=muted)
        fig.legend([Patch(color=color) for color in colors.values()], list(labels.values()),
                   loc="lower left", bbox_to_anchor=(.02, .22), ncol=3, frameon=False, fontsize=9)
        correction_cap = (f"Correction cap: {data['max_corrections']} per task."
                          if data['max_corrections'] is not None else "Correction cap not declared.")
        fig.text(.025, .12, f"{data['expected']} planned attempts; {data['excluded']} fixture exclusions; "
                 f"{data['valid_missing']} eligible missing; "
                 f"{data['statuses'].get('error', 0)} infrastructure errors; {data['critical']} critical violations.\n"
                 f"Whole-scenario exclusions are retrospective; raw outcomes are retained. {correction_cap}\n"
                 "No inferential family interval shown; non-inferiority is indeterminate. No independent acceptance.",
                 color=muted, fontsize=9)
        fig.text(.025, .04, f"Source SHA-256: {sha256_file(report_path)}", color=muted, fontsize=8)
        if eligibility_path:
            fig.text(.025, .015, f"Eligibility SHA-256: {sha256_file(eligibility_path)}", color=muted, fontsize=8)
        fig.subplots_adjust(left=.095, right=.98, top=.78, bottom=.36, wspace=.63)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, metadata={"Title": "Engraphis coding pilot observed outcomes"})
        plt.close(fig)
        if output.suffix.lower() == ".svg":
            output.write_text("\n".join(line.rstrip() for line in output.read_text(encoding="utf-8").splitlines())
                              + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--eligibility", type=Path)
    args = parser.parse_args()
    render(args.report, args.manifest, args.output, args.eligibility)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
