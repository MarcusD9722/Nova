"""Tool selection — deciding which tools the model should even see.

Execution stays in core/tool_router.py. Nothing here runs a tool, checks a
permission, or interprets a result; the selector's only job is to shorten the
list before the agent loop reasons over it.
"""
