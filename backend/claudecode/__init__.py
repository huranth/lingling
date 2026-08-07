"""Claude Code support: the Anthropic Messages wire format.

Kept in its own package rather than beside ``routing/responses_bridge.py`` so the
Codex work and this cannot break each other. The two harnesses share exactly one
thing -- ``routing.effort``, which translates a thinking-depth label into the
value a chosen OpenCode model actually publishes -- and nothing else.
"""
