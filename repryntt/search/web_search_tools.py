"""
web_search_tools.py — Web search tools extracted from BrainSystem monolith.

Routes through repryntt's KnowledgeRouter and KnowledgeAPIFeeder.
Complex tools (grokipedia, semantic_memory, brain_network_search) remain
monolith-delegated — they depend on FAISS indexes and Selenium.
"""

import json
import logging

logger = logging.getLogger("repryntt.search.web_search_tools")


# ─── real_web_search ──────────────────────────────────────────────

def real_web_search(query: str, num_results: int = 10, region: str = "us-en", **kw) -> dict:
    """Search the web and return real URLs with titles and snippets. ALWAYS use URLs from these results — never guess or fabricate URLs.

    Parameters:
        query: Search query string. Be specific (e.g. 'pyserial UART example Jetson Orin Nano' not just 'UART')
        num_results: How many results to return (default 10)
        region: Region for results (default 'us-en')
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        results_list = []
        ddgs = DDGS()
        for r in ddgs.text(query, max_results=int(num_results), region=region):
            results_list.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
                "source": "duckduckgo",
            })

        if not results_list:
            return {"success": False, "error": "No results found", "query": query}

        summary = [f"Web search results for: {query}\n"]
        for i, r in enumerate(results_list, 1):
            summary.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}\n")

        return {
            "success": True, "query": query,
            "results": results_list,
            "result_count": len(results_list),
            "sources_queried": ["duckduckgo"],
            "insights": "\n".join(summary),
            "NEXT_STEP": "Call scrape_web_page(url) on the most relevant result URL to get full page content.",
        }
    except ImportError:
        return {"success": False, "error": "ddgs not installed. Run: pip install ddgs", "query": query}
    except Exception as e:
        logger.error(f"Real web search failed: {e}")
        return {"success": False, "error": f"Web search failed: {e}", "query": query}


# ─── google_web_search (Knowledge Router) ─────────────────────────

def google_web_search(query: str = "", num_results: int = 10,
                      scrape_content: bool = True, scrape_top_n: int = 3,
                      store_results: bool = True, department: str = "", **kw) -> dict:
    """KNOWLEDGE SEARCH TOOL (Domain-Aware).

    Searches legitimate knowledge sources (Wikipedia, arXiv, PubMed, NASA, etc.)
    based on query content and department context.

    Parameters:
        query: The search query
        num_results: Maximum number of results (default 10)
        department: Department hint for routing (e.g. 'materials_science')
    """
    try:
        from repryntt.search.knowledge_router import get_knowledge_router

        router = get_knowledge_router()
        result = router.search(query, max_results=int(num_results), department=department)
        return result
    except Exception as e:
        logger.error(f"Knowledge search failed: {e}")
        return {"success": False, "error": f"Search failed: {e}", "query": query}


# ─── web_search_results_only ─────────────────────────────────────

def web_search_results_only(query: str = "", num_results: int = 10,
                            department: str = "", **kw) -> dict:
    """Quick Knowledge Search (Summaries Only).

    Get results with titles, URLs, and snippets from legitimate APIs.
    Use this first, then scrape_web_page(url) for full content.

    Parameters:
        query: The search query
        num_results: Number of results (default 10)
        department: Department hint for source routing
    """
    try:
        from repryntt.search.knowledge_router import get_knowledge_router

        router = get_knowledge_router()
        return router.search_results_only(query, max_results=int(num_results), department=department)
    except Exception as e:
        logger.error(f"Quick search failed: {e}")
        return {"success": False, "error": f"Search failed: {e}", "query": query}


# ─── scrape_web_page ─────────────────────────────────────────────

def scrape_web_page(url: str = "", store_in_brain: bool = True, **kw) -> dict:
    """Fetch and extract text content from a URL. Only use URLs returned by web_search — never fabricate URLs. If a URL returns 404, try a different URL from search results instead.

    Parameters:
        url: A real URL from web_search results. Must start with https:// or http://. Do NOT guess URLs
        store_in_brain: (ignored in native mode — kept for compat)
    """
    try:
        from repryntt.search.knowledge_router import get_knowledge_router

        router = get_knowledge_router()
        return router.fetch_url(url)
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return {"success": False, "error": f"Fetch failed: {e}", "url": url}


# ─── call_knowledge_api_feeder ────────────────────────────────────

def call_knowledge_api_feeder(query: str = "", apis: str = "", **kw) -> dict:
    """Call the knowledge API feeder to fetch external information.

    Parameters:
        query: What to search for
        apis: Comma-separated list of APIs to query (optional)
    """
    try:
        from repryntt.search.feeders.knowledge_api import KnowledgeAPIFeeder

        feeder = KnowledgeAPIFeeder()
        api_list = [a.strip() for a in apis.split(",")] if apis else None
        results = feeder.run_knowledge_search(query, apis=api_list, extract_content=True)
        return {
            "success": True,
            "results": [r if isinstance(r, dict) else {"content": str(r)} for r in results],
            "stored_entries": len(results),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── extract_content_from_url ────────────────────────────────────

def extract_content_from_url(url: str = "", **kw) -> dict:
    """Extract content from a URL using the knowledge API feeder.

    Parameters:
        url: URL to extract content from
    """
    try:
        from repryntt.search.feeders.knowledge_api import KnowledgeAPIFeeder

        feeder = KnowledgeAPIFeeder()
        extracted = feeder.extract_content_from_url(url)
        if extracted and getattr(extracted, "success", False):
            return {
                "success": True,
                "content": extracted.cleaned_text,
                "word_count": extracted.word_count,
                "summary": getattr(extracted, "summary", ""),
                "key_phrases": getattr(extracted, "key_phrases", []),
            }
        return {
            "success": False,
            "error": getattr(extracted, "error_message", "Extraction failed") if extracted else "Extraction failed",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
