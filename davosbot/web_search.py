"""Web-search tool helpers."""

import requests
from urllib.parse import urlsplit

from .config import TAVILY_API_KEY


def _web_search(query: str, api_key: str | None = None, requests_module=requests) -> str:
    key = TAVILY_API_KEY if api_key is None else api_key
    if not key:
        return "No Tavily API key configured."
    resp = requests_module.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": 5},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return "No results found."
    rendered = []
    for result in results:
        if not isinstance(result, dict):
            continue
        lines = [str(result.get("title", "")), str(result.get("content", ""))]
        url = str(result.get("url") or "").strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme in {"https", "http"} and parsed.hostname and parsed.username is None and parsed.password is None and not any(char.isspace() for char in url):
                lines.append(url)
        except ValueError:
            pass
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered) or "No results found."
