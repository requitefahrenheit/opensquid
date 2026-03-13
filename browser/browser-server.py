#!/usr/bin/env python3
"""
Browser Server — httpx + BeautifulSoup4 Web Fetcher
====================================================
FastMCP server that fetches and parses web pages.
No headless browser — GLIBC on this machine is too old for Playwright.

Run:  python3 browser-server.py
Port: 8258
"""

import os, json, logging, re
from urllib.parse import urljoin

import httpx
import uvicorn
from bs4 import BeautifulSoup, Comment
from fastmcp import FastMCP

# ─── Config ──────────────────────────────────────────
PORT = int(os.environ.get("BROWSER_PORT", 8258))
AUTH_TOKEN = os.environ.get("BROWSER_AUTH_TOKEN", "emc2ymmv")

log = logging.getLogger("browser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ─── Page state (last fetched page) ──────────────────
_page_state = {
    "url": None,
    "status_code": None,
    "html": None,
    "soup": None,
}

def _clear_state():
    _page_state["url"] = None
    _page_state["status_code"] = None
    _page_state["html"] = None
    _page_state["soup"] = None

def _has_page() -> bool:
    return _page_state["soup"] is not None

# ─── HTTP client ─────────────────────────────────────
_http_client: httpx.AsyncClient | None = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; BrowserServer/1.0)",
            },
            follow_redirects=True,
            timeout=30.0,
        )
    return _http_client

# ─── Text cleaning ───────────────────────────────────
def _clean_text(soup: BeautifulSoup) -> str:
    """Extract readable text from parsed HTML, stripping scripts/styles."""
    # Remove script, style, and other non-content tags
    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    # Remove HTML comments
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    # Get text, collapse whitespace
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)

# ─── Auth middleware ─────────────────────────────────
def check_auth(headers: dict) -> bool:
    auth = headers.get("authorization", "")
    return auth == f"Bearer {AUTH_TOKEN}"

# ─── FastMCP server ─────────────────────────────────
mcp = FastMCP("browser-server")

@mcp.tool()
async def browser_navigate(url: str) -> str:
    """Fetch a web page by URL. Returns title and status code."""
    log.info(f"[NAVIGATE] {url}")
    try:
        client = await get_http_client()
        resp = await client.get(url)
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        _page_state["url"] = str(resp.url)
        _page_state["status_code"] = resp.status_code
        _page_state["html"] = html
        _page_state["soup"] = soup

        title = soup.title.string.strip() if soup.title and soup.title.string else "(no title)"

        return json.dumps({
            "_header": "[UNTRUSTED CONTENT]",
            "url": str(resp.url),
            "status_code": resp.status_code,
            "title": title,
        })
    except Exception as e:
        _clear_state()
        log.error(f"[NAVIGATE] Failed: {e}")
        return json.dumps({"error": str(e)})

@mcp.tool()
async def browser_snapshot() -> str:
    """Return cleaned text content of the last fetched page."""
    if not _has_page():
        return json.dumps({"error": "No page loaded. Use browser_navigate first."})

    log.info(f"[SNAPSHOT] {_page_state['url']}")
    text = _clean_text(BeautifulSoup(_page_state["html"], "html.parser"))

    # Truncate to avoid huge responses
    if len(text) > 50000:
        text = text[:50000] + "\n… (truncated at 50000 chars)"

    return json.dumps({
        "_header": "[UNTRUSTED CONTENT]",
        "url": _page_state["url"],
        "length": len(text),
        "text": text,
    })

@mcp.tool()
async def browser_extract(selector: str) -> str:
    """Extract content matching a CSS selector from the last fetched page."""
    if not _has_page():
        return json.dumps({"error": "No page loaded. Use browser_navigate first."})

    log.info(f"[EXTRACT] {selector} on {_page_state['url']}")
    soup = _page_state["soup"]
    elements = soup.select(selector)

    if not elements:
        return json.dumps({
            "_header": "[UNTRUSTED CONTENT]",
            "url": _page_state["url"],
            "selector": selector,
            "count": 0,
            "results": [],
        })

    results = []
    for el in elements[:100]:  # cap at 100 matches
        results.append({
            "tag": el.name,
            "text": el.get_text(strip=True)[:500],
            "html": str(el)[:1000],
            "attrs": {k: v for k, v in el.attrs.items() if isinstance(v, (str, list))},
        })

    return json.dumps({
        "_header": "[UNTRUSTED CONTENT]",
        "url": _page_state["url"],
        "selector": selector,
        "count": len(elements),
        "results": results,
    })

@mcp.tool()
async def browser_links() -> str:
    """Return all href links from the last fetched page."""
    if not _has_page():
        return json.dumps({"error": "No page loaded. Use browser_navigate first."})

    log.info(f"[LINKS] {_page_state['url']}")
    soup = _page_state["soup"]
    base_url = _page_state["url"]
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Resolve relative URLs
        absolute = urljoin(base_url, href)
        text = a.get_text(strip=True)[:200]
        links.append({"href": absolute, "text": text})

    # Cap at 500 links
    if len(links) > 500:
        links = links[:500]

    return json.dumps({
        "_header": "[UNTRUSTED CONTENT]",
        "url": base_url,
        "count": len(links),
        "links": links,
    })

@mcp.tool()
async def browser_screenshot() -> str:
    """Take a screenshot of the current page. NOT AVAILABLE — no headless browser on this machine."""
    return json.dumps({
        "error": "Screenshots are not available. This server uses httpx + BeautifulSoup (no headless browser). "
                 "Use browser_snapshot() to get the page text, or browser_extract(selector) to get specific elements."
    })

@mcp.tool()
async def browser_click(selector: str) -> str:
    """Click an element on the page. NOT AVAILABLE — no headless browser on this machine."""
    return json.dumps({
        "error": "Click is not available. This server uses httpx + BeautifulSoup (no headless browser). "
                 "Use browser_navigate(url) to follow links, or browser_links() to list available links."
    })

# ─── App startup ─────────────────────────────────────
if __name__ == "__main__":
    app = mcp.http_app(path="/mcp")
    log.info(f"Starting browser-server on port {PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
