"""Codex support: the OpenAI-Responses side of the gateway.

Sits beside :mod:`claudecode` -- this one owns the ``model_catalog_json`` Codex
reads to decide whether the wire carries a reasoning block at all, where
:mod:`claudecode` owns ``~/.claude/settings.json`` and the
``ANTHROPIC_BASE_URL`` env. They share exactly one thing with the rest of the
package, ``routing.effort``, and nothing else, so the two harnesses cannot break
each other.
"""
