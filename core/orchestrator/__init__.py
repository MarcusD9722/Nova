"""Orchestration layer (Phase 2 of docs/ROADMAP.md).

Nova coordinates specialized agent *roles* over a shared model, rather than
doing everything in one inline loop. On today's single-GPU / single-9B setup
every role resolves to the same model — but the interfaces are written so a
role can later point at a second model instance (RTX 3080 timeshare or a
Coder-14B swap) with only a config change, never a rewrite.
"""
