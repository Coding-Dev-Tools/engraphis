"""Generate the three example cards exclusively from validated offline fixture values."""
from __future__ import annotations

import argparse
from pathlib import Path
from string import Template

from scripts.render_benchmark_report import _integer, _number, _validate_rate, load_report_snapshot


def render(source: Path, output: Path) -> None:
    selected, raw = load_report_snapshot(source)
    runs = {row["id"]: row["result"] for row in raw["runs"]}
    chunking, grounded = runs["offline-chunking"], runs["offline-grounded"]
    values = {name: _integer(grounded[name]) for name in
              ("grounded", "answerable", "abstained", "off_topic")}
    if (values["grounded"] > values["answerable"]
            or values["abstained"] > values["off_topic"]):
        raise ValueError("grounding outcomes exceed their denominators")
    values.update({
        "whole_tokens": f"{_number(chunking['whole']['mean_context_tokens']):.1f}",
        "chunk_tokens": f"{_number(chunking['chunked']['mean_context_tokens']):.1f}",
        "k": _integer(chunking["k"]),
        "recall": f"{_validate_rate(chunking['chunked']['recall_at_k'], 'recall'):.3f}",
        "artifact_sha256": selected["source"]["artifact_sha256"],
    })
    template = Template(Path(__file__).with_name("templates").joinpath("benchmark_examples.svg").read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(template.substitute(values))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    render(args.report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
