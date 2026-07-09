"""LLM-powered memory intelligence — auto-categorization, conflict detection, and splitting.

When a memory is ingested, the LLM can:
1. Classify it as semantic / episodic / procedural / working
2. Detect if it contains conflicting information that should be split
3. Return split suggestions with separate content blocks

This engine is optional — if no LLM key is configured, it silently skips and
defaults to the caller-provided memory_type.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from engraphis.llm.client import LLMClient

logger = logging.getLogger("engraphis.intelligence")

_CLASSIFY_PROMPT = """You are a memory classification engine. Analyze the given text and classify it into exactly one of these cognitive memory types:

- **semantic**: Facts, knowledge, preferences, stable information ("Python is dynamically typed", "User prefers dark mode")
- **episodic**: Events, experiences, meetings, time-stamped occurrences ("Met with Alice on June 28", "Deployed v2.1 yesterday")
- **procedural**: How-to, workflows, step-by-step instructions ("To deploy: build, test, then push")
- **working**: Current task context, temporary state, in-progress work ("Currently fixing the login bug")

Also detect if the text contains CONFLICTING or UNRELATED information that would be better stored as separate memories. For example, if it mixes a fact with an event, or contains two unrelated topics.

Respond as JSON only:
{
  "memory_type": "semantic|episodic|procedural|working",
  "confidence": 0.0-1.0,
  "should_split": true/false,
  "reason": "one sentence explanation",
  "splits": [
    {"title": "suggested title", "content": "extracted content", "memory_type": "type"}
  ]
}

If should_split is false, leave splits as empty array []."""

_CONFLICT_CHECK_PROMPT = """You are a memory conflict detector. Compare the new memory against existing memories in the same namespace. Detect:
1. **Contradictions**: The new memory directly contradicts an existing one (e.g., "prefers dark mode" vs "prefers light mode")
2. **Updates**: The new memory supersedes or updates an existing one (e.g., new phone number replaces old)
3. **Complements**: The new memory adds complementary info to an existing one

Respond as JSON only:
{
  "has_conflict": true/false,
  "conflict_type": "contradiction|update|complement|none",
  "conflicting_memory_id": "document_id or null",
  "resolution": "replace|merge|keep_both|update_in_place",
  "explanation": "one sentence"
}"""


def auto_categorize(content: str, title: str = "",
                    suggested_type: str = "semantic") -> dict[str, Any]:
    """Use the LLM to categorize a memory and detect if it should be split.

    Returns:
        {
            "memory_type": str,
            "confidence": float,
            "should_split": bool,
            "reason": str,
            "splits": list of {title, content, memory_type} if should_split
        }
    """
    try:
        with LLMClient() as llm:
            text = f"Title: {title}\n\nContent:\n{content[:2000]}"
            raw = llm.chat(
                [{"role": "user", "content": text}],
                system=_CLASSIFY_PROMPT,
                temperature=0.1,
                max_tokens=512,
            )
            result = _parse_json(raw)
            # Validate and fallback
            if "memory_type" not in result:
                result["memory_type"] = suggested_type
            if result["memory_type"] not in ("semantic", "episodic", "procedural", "working"):
                result["memory_type"] = suggested_type
            if "confidence" not in result:
                result["confidence"] = 0.5
            if "should_split" not in result:
                result["should_split"] = False
            if "splits" not in result:
                result["splits"] = []
            if "reason" not in result:
                result["reason"] = ""
            return result
    except Exception as e:
        logger.debug("Auto-categorize skipped: %s", e)
        return {
            "memory_type": suggested_type,
            "confidence": 0.0,
            "should_split": False,
            "reason": f"LLM unavailable: {e}",
            "splits": [],
        }


def check_conflicts(content: str, namespace: str,
                    existing_memories: list[dict[str, Any]]) -> dict[str, Any]:
    """Check if a new memory conflicts with existing ones.

    Args:
        content: New memory content
        namespace: Namespace
        existing_memories: List of existing memories to compare against
            (each should have document_id, title, content)

    Returns:
        {
            "has_conflict": bool,
            "conflict_type": str,
            "conflicting_memory_id": str or None,
            "resolution": str,
            "explanation": str
        }
    """
    if not existing_memories:
        return {"has_conflict": False, "conflict_type": "none",
                "conflicting_memory_id": None, "resolution": "keep_both",
                "explanation": "No existing memories to compare"}

    try:
        with LLMClient() as llm:
            existing_text = "\n\n".join([
                f"[{m.get('document_id', '?')}] {m.get('title', '')}: {m.get('content', '')[:300]}"
                for m in existing_memories[:10]
            ])
            prompt = f"New memory:\n{content[:1000]}\n\nExisting memories in namespace '{namespace}':\n{existing_text}"
            raw = llm.chat(
                [{"role": "user", "content": prompt}],
                system=_CONFLICT_CHECK_PROMPT,
                temperature=0.1,
                max_tokens=256,
            )
            result = _parse_json(raw)
            if "has_conflict" not in result:
                result["has_conflict"] = False
            if "conflict_type" not in result:
                result["conflict_type"] = "none"
            if "resolution" not in result:
                result["resolution"] = "keep_both"
            if "explanation" not in result:
                result["explanation"] = ""
            return result
    except Exception as e:
        logger.debug("Conflict check skipped: %s", e)
        return {"has_conflict": False, "conflict_type": "none",
                "conflicting_memory_id": None, "resolution": "keep_both",
                "explanation": f"LLM unavailable: {e}"}


def _parse_json(raw: str) -> dict[str, Any]:
    """Best-effort JSON parse, tolerating markdown fences and extra text."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    # Try to extract JSON from the response
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        text = json_match.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": raw}
