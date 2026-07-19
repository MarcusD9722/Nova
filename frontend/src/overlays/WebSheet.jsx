import React, { useState, useRef } from "react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

export default function WebSheet() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [pageContent, setPageContent] = useState(null);
  const [fetchingUrl, setFetchingUrl] = useState("");
  const inputRef = useRef(null);

  async function handleSearch(e) {
    e?.preventDefault();
    const q = query.trim();
    if (!q) return;
    setLoading(true);
    setError("");
    setResults(null);
    setPageContent(null);
    try {
      const res = await fetch(apiUrl(`/api/web/search?q=${encodeURIComponent(q)}&max_results=8`));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setResults(data);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  }

  async function handleFetch(url) {
    setFetchingUrl(url);
    setPageContent(null);
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/web/fetch?url=${encodeURIComponent(url)}&max_chars=6000`));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setPageContent(data);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setFetchingUrl("");
    }
  }

  return (
    <div className="space-y-3 text-nova-gold h-full flex flex-col">
      <form onSubmit={handleSearch} className="flex gap-2">
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
          placeholder="Search the web…"
        />
        <button
          type="submit"
          disabled={loading}
          className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
        >
          {loading ? "…" : "Search"}
        </button>
      </form>

      {error && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}

      {pageContent && (
        <div className="flex flex-col gap-2 flex-1 min-h-0">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPageContent(null)}
              className="text-xs text-nova-gold/60 hover:text-nova-gold underline"
            >
              ← Back to results
            </button>
            <a
              href={pageContent.url}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-nova-gold/50 hover:text-nova-gold/80 truncate flex-1"
            >
              {pageContent.url}
            </a>
          </div>
          <div className="rounded-xl border border-nova-gold/10 bg-black/20 p-3 text-xs text-nova-gold/85 whitespace-pre-wrap overflow-y-auto flex-1 leading-relaxed">
            {pageContent.content}
          </div>
        </div>
      )}

      {results && !pageContent && (
        <div className="space-y-2 overflow-y-auto flex-1">
          {results.results.length === 0 && (
            <div className="text-xs text-nova-gold/60 text-center py-4">No results found.</div>
          )}
          {results.results.map((r, i) => (
            <div
              key={i}
              className="rounded-xl border border-nova-gold/10 bg-black/20 p-3 space-y-1"
            >
              <a
                href={r.url}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-nova-gold font-medium hover:underline block truncate"
              >
                {r.title || r.url}
              </a>
              <div className="text-xs text-nova-gold/55 truncate">{r.url}</div>
              {r.snippet && (
                <div className="text-xs text-nova-gold/75">{r.snippet}</div>
              )}
              <button
                type="button"
                disabled={fetchingUrl === r.url}
                onClick={() => handleFetch(r.url)}
                className="text-xs text-nova-gold/50 hover:text-nova-gold/80 underline disabled:opacity-50"
              >
                {fetchingUrl === r.url ? "Reading…" : "Read page"}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

