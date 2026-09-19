"""Render a checksummed diagnostic analysis with Matplotlib (optional plotting env)."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def render(analysis: Path, output: Path) -> None:
    expected = analysis.with_suffix(analysis.suffix + ".sha256").read_text(encoding="utf-8").split()[0]
    if hashlib.sha256(analysis.read_bytes()).hexdigest() != expected:
        raise ValueError("analysis checksum mismatch")
    data = json.loads(analysis.read_text(encoding="utf-8"))
    if data.get("schema") != "engraphis-external-analysis/v1" or not data.get("reports"):
        raise ValueError("expected an external diagnostic analysis")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    reports = data["reports"]
    background, text, muted = "#0e1114", "#e9f1f5", "#9caebe"
    with plt.rc_context({"font.family": "DejaVu Sans", "figure.facecolor": background,
                         "axes.facecolor": background, "text.color": text,
                         "axes.labelcolor": text, "xtick.color": muted, "ytick.color": text,
                         "svg.fonttype": "none"}):
        fig, axes = plt.subplots(len(reports), 1, figsize=(11, 2.7 * len(reports) + 1.7), squeeze=False)
        for axis, report in zip(axes[:, 0], reports):
            if report["status"] != "COMPLETE":
                raise ValueError("a completed diagnostic is required for this score chart")
            for position, key, color, label in ((1, "recall", "#71849b", "Retrieved evidence"),
                                                 (0, "packed_recall", "#00b889", "Packed evidence")):
                interval = report[key]
                point, low, high = (interval[name] for name in ("point", "low", "high"))
                if any(type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1 for v in (point, low, high)):
                    raise ValueError("chart requires finite measured scores and intervals")
                axis.barh(position, point * 100, height=.42, color=color)
                axis.errorbar(point * 100, position, xerr=[[max(0, point - low) * 100], [max(0, high - point) * 100]],
                              fmt="none", color=text, capsize=5, linewidth=1.3)
                axis.text(102, position, f"{point:.2%}  [{low:.2%}, {high:.2%}]", va="center", fontsize=10)
            config = report["configuration"]
            axis.set_title(f"{report['dataset']}  |  Recall@{config['k']}  |  {config['token_budget']:,} evidence tokens",
                           loc="left", fontsize=13, pad=14, fontweight="bold")
            axis.set_yticks([0, 1], ["Packed evidence", "Retrieved evidence"])
            axis.set_xlim(0, 140)
            axis.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
            axis.set_ylim(-.55, 1.55)
            axis.grid(axis="x", color="#28313a", linewidth=.6)
            axis.set_axisbelow(True)
            for spine in axis.spines.values():
                spine.set_visible(False)
            note = (f"{report['retrieval_scored_questions']:,}/{report['questions']:,} scored; "
                    f"{report['retrieval_exclusions']} explicit exclusions; {report['recall']['source_cases']} source cases. "
                    f"Context mean {report['mean_context_tokens']:.1f}; max {report['max_context_tokens']:,} tokens.")
            axis.text(0, -.3, note, transform=axis.transAxes, color=muted, fontsize=9)
        fig.suptitle("External retrieval diagnostics", x=.02, y=.99, ha="left", fontsize=21, fontweight="bold")
        fig.text(.02, .025, "95% source-case bootstrap intervals; one execution. No generated-answer or official QA score.\n"
                 f"Source analysis SHA-256: {expected}", color=muted, fontsize=8)
        fig.subplots_adjust(left=.18, right=.98, top=.78 if len(reports) == 1 else .87,
                            bottom=.27 if len(reports) == 1 else .18, hspace=.9)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, facecolor=background, metadata={"Title": "Engraphis external retrieval diagnostics"})
        plt.close(fig)
        if output.suffix.lower() == ".svg":
            # Matplotlib adds trailing spaces inside multiline SVG paths.
            output.write_text("\n".join(line.rstrip() for line in
                              output.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.analysis, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
