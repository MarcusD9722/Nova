"""Voice-side concerns: how Nova's words become speech, and how a turn ends.

Deliberately free of backend and model imports so every rule in here is a pure
function that can be tested without a GPU, a model, or an event loop.
"""
