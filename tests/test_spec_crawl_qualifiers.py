"""Numbers qualify explicit phrases, not unrelated wording nearby."""
from __future__ import annotations

import pytest

from engraphis.core.spec_crawl import analyze_memory_nodes, crawl_spec


PRECISE = [
    ("Check every 5 minutes.", "every"),
    ("Check every 0.5 seconds.", "every"),
    ("Check every 500ms.", "every"),
    ("Process every 5 requests.", "every"),
    ("Check every 1,000 requests.", "every"),
    ("Run all 5 tests.", "all"),
    ("Run all 1,000 tests.", "all"),
    ("Run all the 5 tests.", "all"),
    ("Run all of the 5 tests.", "all"),
    ("Choose any 2 samples.", "any"),
    ("Choose any of the 2 samples.", "any"),
    ("Finish quickly within 5 seconds.", "quickly"),
    ("Respond fast under 100ms.", "fast"),
    ("Start soon in 2 minutes.", "soon"),
    ("Finish asap within 10 sec.", "asap"),
    ("Finish quickly at most 5 seconds.", "quickly"),
    ("Finish quickly no more than 5 seconds.", "quickly"),
]

UNBOUNDED = [
    ("Deploy to port 443 and run all tests.", "all"),
    ("Run 443 all tests.", "all"),
    ("Run release443 all tests.", "all"),
    ("Run all release443 tests.", "all"),
    ("Run all version 2 tests.", "all"),
    ("Use port 443 for every test.", "every"),
    ("In 2026 run all tests.", "all"),
    ("Run all tests in 2026.", "all"),
    ("Run all tests for 2026-10-16.", "all"),
    ("Build v2 checks all tests.", "all"),
    ("Use 443, run all tests.", "all"),
    ("Run all tests, 443 failed.", "all"),
    ("Run all tests and keep 5 logs.", "all"),
    ("Keep 3 logs and run all tests.", "all"),
    ("Port 443. Run all tests.", "all"),
    ("Port 443; run all tests.", "all"),
    ("Port 443\nRun all tests.", "all"),
    ("Run `443` all tests.", "all"),
    ("Run all `443` tests.", "all"),
    ("Run all ``literal ` 443`` tests.", "all"),
    ("```text\n443\n```\nRun all tests.", "all"),
    ("# Release 443\nRun all tests.", "all"),
    ("Clean 3 files.", "clean"),
    ("Write proper code using v2.", "proper"),
    ("Respond quickly on port 443.", "quickly"),
    ("Respond fast after 5 retries.", "fast"),
    ("Start soon in 2026.", "soon"),
    ("Finish quickly within 5 ports.", "quickly"),
    ("Check every 5.", "every"),
    ("Check every v2 minutes.", "every"),
    ("Check every 5 and report.", "every"),
    ("Run all 5.5 tests.", "all"),
    ("Run all -5 tests.", "all"),
    ("Run all 5files.", "all"),
    ("Check every, 5 minutes later.", "every"),
]


@pytest.mark.parametrize("mode", ["spec", "memory"])
@pytest.mark.parametrize("text,word", PRECISE)
def test_explicit_count_cadence_and_duration_phrases_are_precise(mode, text, word):
    report = (crawl_spec("# Rules\n" + text) if mode == "spec"
              else analyze_memory_nodes([{"id": "rule", "content": text}]))
    assert not any(flag["token"].casefold() == word for flag in report["flags"])


@pytest.mark.parametrize("mode", ["spec", "memory"])
@pytest.mark.parametrize("text,word", UNBOUNDED)
def test_unrelated_numbers_cannot_hide_an_unbounded_qualifier(mode, text, word):
    report = (crawl_spec("# Rules\n" + text) if mode == "spec"
              else analyze_memory_nodes([{"id": "rule", "title": "Release 443", "content": text}]))
    assert any(flag["token"].casefold() == word for flag in report["flags"])


def test_restored_flags_preserve_exact_source_offsets_and_determinism():
    text = "# Release 443\nDeploy to port 443 and run all tests. Check every 5 minutes."
    first = crawl_spec(text)
    flag = next(flag for flag in first["flags"] if flag["token"] == "all")
    assert text[flag["start"]:flag["end"]] == "all"
    assert first == crawl_spec(text)


def test_a_precise_count_does_not_clear_a_different_vague_term():
    report = crawl_spec("# Rules\nRun all 5 tests quickly.")
    assert not any(flag["token"] == "all" for flag in report["flags"])
    assert any(flag["token"] == "quickly" for flag in report["flags"])
