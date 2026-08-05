"""Capabilities: the self-contained things Nova can DO (U10).

Each module here owns one capability end to end — its language patterns, its
state, its tool calls and its reply wording — so a change to how Nova handles
maps touches maps, and nothing else. They are extracted from
`core/runtime.py` one at a time, each move landing behind the integration
tests in `tests/test_it_*.py` (see docs/NEXT_SESSION.md).

`RuntimeManager` stays the coordinator: it owns the turn, decides the ORDER
capabilities get a look at a message, and falls through to the LLM when none
of them claims it.
"""
