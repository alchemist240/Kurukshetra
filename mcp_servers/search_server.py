import asyncio
import os
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

# Load .env so TAVILY_API_KEY is available even when run standalone
load_dotenv()

mcp = MCPServer("kurukshetra-search")

SNIPPET_MAX_CHARS = 600
EXCLUDED_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "tracxn.com",
]


def _search_duckduckgo_sync(query: str, max_results: int) -> list[dict]:
    """Runs keyless DuckDuckGo search via ddgs and filters out low-signal profile domains."""
    from ddgs import DDGS

    with DDGS() as ddgs:
        # Fetch a few extra results so filtering out blocked domains still leaves max_results
        raw = list(ddgs.text(query, max_results=max_results + 4))

    filtered = []
    for item in raw:
        href = str(item.get("href", "")).lower()
        if any(domain in href for domain in EXCLUDED_DOMAINS):
            continue
        filtered.append(item)
        if len(filtered) >= max_results:
            break
    return filtered


async def _search_tavily(query: str, max_results: int, api_key: str) -> str:
    """Runs clean AI-agent web search via Tavily (1 credit per basic search)."""
    from tavily import AsyncTavilyClient

    client = AsyncTavilyClient(api_key=api_key)
    resp = await client.search(
        query=query,
        max_results=max_results,
        search_depth="basic",
        include_answer=True,
        exclude_domains=EXCLUDED_DOMAINS,
    )

    lines = [f"[Web Search via Tavily | Query: {query}]"]
    if resp.get("answer"):
        lines.append(f"Quick Summary: {resp['answer']}")

    for i, item in enumerate(resp.get("results", [])[:max_results], start=1):
        title = item.get("title", "Untitled").strip()
        url = item.get("url", "").strip()
        snippet = item.get("content", "").replace("\n", " ").strip()[:SNIPPET_MAX_CHARS]
        lines.append(f"{i}. {title} ({url})\n   {snippet}")

    return "\n".join(lines)


@mcp.tool()
async def web_search(query: str, max_results: int = 4) -> str:
    """
    Search the live web for up-to-date facts, current prices, recent product launches,
    trades, benchmarks, or news. Search ONE focused subject per query and include the
    relevant year/region (e.g., 'Arshdeep Singh IPL 2026 bowling stats').
    """
    clean_query = query.strip()
    if not clean_query:
        return "Error: search query cannot be empty."

    limit = max(1, min(int(max_results), 5))
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    fallback_note = ""

    # 1. Primary: Tavily (if API key is configured)
    if tavily_key:
        try:
            return await _search_tavily(clean_query, limit, tavily_key)
        except Exception as e:
            fallback_note = f" (Tavily fallback triggered: {type(e).__name__})"

    # 2. Automatic Fallback: Free keyless DuckDuckGo (ddgs)
    try:
        results = await asyncio.to_thread(_search_duckduckgo_sync, clean_query, limit)
        if not results:
            return f"[Web Search via DuckDuckGo{fallback_note}] No results found for: {clean_query}"

        lines = [f"[Web Search via DuckDuckGo{fallback_note} | Query: {clean_query}]"]
        for i, item in enumerate(results[:limit], start=1):
            title = str(item.get("title", "Untitled")).strip()
            url = str(item.get("href", "")).strip()
            snippet = str(item.get("body", "")).replace("\n", " ").strip()[:SNIPPET_MAX_CHARS]
            lines.append(f"{i}. {title} ({url})\n   {snippet}")

        return "\n".join(lines)
    except Exception as e:
        return f"Search failed on both providers: {type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run()