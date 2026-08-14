"""MCP as a governed Nova capability source.

The value of MCP is its ecosystem, not its execution model. Nova already has
tool selection, a permission broker, a context firewall, artifacts and an audit
trail that are stronger than handing a model a list of remote functions — so
MCP feeds those systems rather than going around them.

    MCP server
      -> session (stdio JSON-RPC)
      -> discovery
      -> normalized Nova capability descriptors   (namespaced, sanitised)
      -> capability registry                      (schema hash, embeddings cache)
      -> ToolSelector                             (the existing one)
      -> permissions                              (the existing broker)
      -> context firewall / trust                 (UNTRUSTED_EXTERNAL)
      -> execution via ToolRouter                 (timeout, retry, audit)
      -> artifacts / provenance

Nothing here may become a second execution path. If a future change makes an
MCP call that skips ToolRouter, permissions or trust classification, that is a
bug regardless of how convenient it is.
"""
