"""Staged multi-agent orchestrator experiment harness.

See ``eval/README.md``. Stage 0 = ground-truth re-pin + torch-free metrics;
Stage 1 = does a structured plan help the local 9b executor; Stage 2 (in the
sibling ``orchestrator/`` package) = the LangGraph sketch, gated on Stage 1.

Nothing here is imported by ``locagent_mcp.py`` or the runtime graph build; the
only shared dependency is ``import locagent_mcp`` for ground-truth resolution.
"""
