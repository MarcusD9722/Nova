from __future__ import annotations

import re

import httpx

from plugins.registry import tool


_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _strip_html(html: str) -> str:
    """Very lightweight HTML → plain text (no external deps)."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s{3,}", "\n\n", text)
    return text.strip()


@tool(
    name="web.search",
    description=(
        "Search the web using DuckDuckGo. Returns up to 8 result snippets with titles and URLs. "
        "args: {query, max_results?}. Use for questions needing up-to-date information."
    ),
)
async def web_search(args: dict) -> dict:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        raise ValueError("web.search requires 'query'")

    max_results = min(int(args.get("max_results") or 8), 10)
    timeout = httpx.Timeout(15.0)

    async with httpx.AsyncClient(timeout=timeout, headers=_DDG_HEADERS, follow_redirects=True) as client:
        # DuckDuckGo lite HTML — no JS, no API key, returns simple result HTML
        r = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "us-en"},
        )
        r.raise_for_status()
        html = r.text

    results = []
    # Parse result blocks: <a class="result__a" href="...">title</a> + snippet
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html,
        re.S,
    )
    for url, title_html, snippet_html in blocks:
        if len(results) >= max_results:
            break
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()
        # Skip DDG advertisement redirects entirely — they 400 when fetched
        # and point at ad-tracking endpoints, not the actual site.
        if "y.js" in url or "ad_domain=" in url or "ad_provider=" in url:
            continue
        # DDG wraps organic URLs in a redirect — extract the actual URL
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            import urllib.parse
            url = urllib.parse.unquote(m.group(1))
        results.append({"title": title, "url": url, "snippet": snippet})

    return {"query": query, "results": results, "count": len(results)}


@tool(
    name="web.fetch",
    description=(
        "Fetch the plain-text content of a web page by URL. "
        "args: {url, max_chars?}. Use to read a page found via web.search."
    ),
)
async def web_fetch(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("web.fetch requires 'url'")
    if not url.startswith(("http://", "https://")):
        raise ValueError("web.fetch: URL must start with http:// or https://")

    max_chars = min(int(args.get("max_chars") or 8000), 16000)
    timeout = httpx.Timeout(20.0)

    async with httpx.AsyncClient(
        timeout=timeout, headers=_DDG_HEADERS, follow_redirects=True
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return {"url": url, "content": f"[Non-text content: {content_type}]", "chars": 0}
        html = r.text

    text = _strip_html(html)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated]"

    return {"url": url, "content": text, "chars": len(text)}
