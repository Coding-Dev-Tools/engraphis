"""Engraphis evaluation harness.

A small, dependency-light runner so retrieval quality is measured from day one and
can gate CI. Phase 0 ships the harness + metrics + a tiny multi-session fixture;
later phases plug in LoCoMo, LongMemEval, and the new Engraphis-CodeMem suite, and
swap the deterministic embedder for a real model behind the same interface.
"""
