"""
grokipedia.py — Grokipedia knowledge search & domain analysis tools.

Uses the grokipedia.com typeahead API to discover articles, then fetches
the server-rendered HTML for full content extraction. No browser/Selenium needed.
"""

import json
import math
import os
import re
import time
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("repryntt.search.grokipedia")

_TYPEAHEAD_URL = "https://grokipedia.com/api/typeahead"
_PAGE_BASE = "https://grokipedia.com/page/"
_UA = "Mozilla/5.0 (Linux; aarch64) repryntt/1.0"
_FETCH_TIMEOUT = 12


# ─── Search History Persistence ───────────────────────────────────

def _searches_file(brain_path) -> str:
    return os.path.join(str(brain_path), "recent_grokipedia_searches.json")


def load_recent_grokipedia_searches(brain_path) -> Dict[str, float]:
    """Load recent grokipedia searches from persistent storage."""
    try:
        fp = _searches_file(brain_path)
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            current = time.time()
            return {q: t for q, t in data.items() if current - t < 86400}
    except Exception as e:
        logger.warning(f"Failed to load recent grokipedia searches: {e}")
    return {}


def save_recent_grokipedia_searches(brain_path, searches: Dict[str, float]) -> None:
    """Save recent grokipedia searches to persistent storage."""
    try:
        fp = _searches_file(brain_path)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(searches, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save recent grokipedia searches: {e}")


def clear_grokipedia_search_history(brain_path) -> None:
    """Clear the recent grokipedia search history."""
    fp = _searches_file(brain_path)
    if os.path.exists(fp):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({}, f)
    logger.info("Cleared grokipedia search history")


# ─── Query Similarity ────────────────────────────────────────────

def _queries_are_similar(q1: str, q2: str) -> bool:
    """Simple word-overlap similarity check between two queries."""
    words1 = set(q1.lower().split())
    words2 = set(q2.lower().split())
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2)
    return overlap / max(len(words1), len(words2)) > 0.6


# ─── Content Helpers ──────────────────────────────────────────────

def categorize_grokipedia_content(title: str, content: str) -> str:
    """Categorize grokipedia content into appropriate knowledge domain."""
    title_lower = title.lower()
    content_lower = content.lower()

    domain_keywords = {
        "technology": ["artificial intelligence", "machine learning", "neural network",
                       "algorithm", "computer", "software", "technology", "ai", "ml", "data science"],
        "science": ["physics", "chemistry", "biology", "mathematics", "quantum",
                    "theory", "scientific", "research", "experiment"],
        "medicine": ["medical", "health", "disease", "treatment", "physiology",
                     "clinical", "patient", "therapy"],
        "philosophy": ["philosophy", "psychology", "consciousness", "mind",
                       "cognition", "ethics", "logic", "reasoning"],
        "history": ["history", "civilization", "society", "culture", "politics",
                    "economics", "social"],
        "art": ["art", "literature", "music", "painting", "sculpture",
                "poetry", "novel", "drama", "theater"],
    }

    for domain, keywords in domain_keywords.items():
        if any(k in title_lower or k in content_lower for k in keywords):
            return domain
    return "science"


def extract_grokipedia_tags(title: str, content: str) -> List[str]:
    """Extract relevant tags from grokipedia content."""
    tags: List[str] = []
    text = (title + " " + content[:1000]).lower()

    tag_mappings = {
        "artificial intelligence": ["AI", "artificial intelligence"],
        "machine learning": ["machine learning", "ML"],
        "neural network": ["neural networks", "deep learning"],
        "physics": ["physics", "physical sciences"],
        "mathematics": ["mathematics", "math"],
        "biology": ["biology", "life sciences"],
        "chemistry": ["chemistry", "chemical sciences"],
        "computer science": ["computer science", "computing"],
        "philosophy": ["philosophy", "philosophical"],
        "psychology": ["psychology", "psychological"],
        "history": ["history", "historical"],
        "medicine": ["medicine", "medical"],
        "economics": ["economics", "economic"],
        "literature": ["literature", "literary"],
    }

    for keyword, tag_list in tag_mappings.items():
        if keyword in text:
            tags.extend(tag_list)
    return list(set(tags))[:5]


def extract_key_facts(content: str) -> List[str]:
    """Extract key factual statements from grokipedia content."""
    facts: List[str] = []
    for line in content.split("\n")[:20]:
        line = line.strip()
        if 20 < len(line) < 200 and not line.startswith("#"):
            if not any(s in line.lower() for s in
                       ["see also", "references", "external links", "further reading"]):
                facts.append(line[:150])
    return facts[:5]


def extract_related_topics(content: str) -> List[str]:
    """Extract related topics mentioned in the content."""
    topics: List[str] = []
    words = content.lower().split()
    for i in range(len(words) - 1):
        if words[i][0:1].isupper() and words[i + 1][0:1].isupper():
            phrase = f"{words[i]} {words[i + 1]}"
            if len(phrase) > 5 and phrase not in topics:
                topics.append(phrase)
    return topics[:5]


def format_grokipedia_insights(articles: List[Dict], original_query: str) -> str:
    """Format grokipedia search results into AI-consumable insights."""
    if not articles:
        return f"No accessible articles found for query: '{original_query}'."

    insights = [f"Grokipedia Knowledge for '{original_query}':\n"]
    for i, article in enumerate(articles, 1):
        title = article.get("title", "Unknown")
        content = article.get("content", "")
        insights.append(f"Article {i}: {title}")
        insights.append(f"   Key Content: {content[:300]}...")
        facts = extract_key_facts(content)
        if facts:
            insights.append("   Key Facts:")
            for fact in facts[:3]:
                insights.append(f"   - {fact}")
        insights.append("")

    insights.append("This knowledge has been automatically stored in your brain for future reference.")
    return "\n".join(insights)


# ─── HTTP Helpers ─────────────────────────────────────────────────

def _fetch_json(url: str) -> Any:
    """Fetch JSON from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_html(url: str) -> str:
    """Fetch raw HTML from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_article_text(html: str, max_chars: int = 6000) -> str:
    """Extract clean article text from grokipedia server-rendered HTML."""
    # Find the first <h2> (start of content sections) to footer/contribute
    start = html.find("<h2")
    if start == -1:
        start = 0
    end = html.find("<footer")
    if end == -1:
        end = html.find('id="contribute-article-form"')
    if end == -1:
        end = len(html)

    chunk = html[start:end]
    # Strip <style> and <script> blocks
    chunk = re.sub(r"<style[^>]*>.*?</style>", "", chunk, flags=re.DOTALL)
    chunk = re.sub(r"<script[^>]*>.*?</script>", "", chunk, flags=re.DOTALL)
    # Convert headers to markdown
    chunk = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", chunk, flags=re.DOTALL)
    chunk = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", chunk, flags=re.DOTALL)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", chunk)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Keep lines with substance
    lines = text.split("\n")
    good = [l.strip() for l in lines if l.strip() and (l.strip().startswith("#") or len(l.strip()) > 25)]
    result = "\n".join(good)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n...[truncated]"
    return result


def _typeahead_search(query: str, limit: int = 5) -> List[Dict]:
    """Search grokipedia via the typeahead API. Returns list of {slug, title, snippet}."""
    params = urllib.parse.urlencode({"query": query, "limit": str(limit)})
    url = f"{_TYPEAHEAD_URL}?{params}"
    data = _fetch_json(url)
    results = []
    for item in data.get("results", []):
        results.append({
            "slug": item.get("slug", ""),
            "title": item.get("title", "").strip("_"),
            "snippet": item.get("snippet", ""),
        })
    return results


def _fetch_article(slug: str, max_chars: int = 6000) -> Dict[str, Any]:
    """Fetch and extract a full grokipedia article by slug."""
    url = f"{_PAGE_BASE}{slug}"
    html = _fetch_html(url)

    # Extract meta description for summary
    meta_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
    description = meta_match.group(1) if meta_match else ""

    # Extract section headers
    headers = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html, re.DOTALL)
    sections = [re.sub(r"<[^>]+>", "", h).strip() for h in headers]

    content = _extract_article_text(html, max_chars=max_chars)

    return {
        "title": slug.replace("_", " "),
        "url": url,
        "description": description,
        "sections": sections[:20],
        "content": content,
    }


# ─── Main Search ─────────────────────────────────────────────────

def grokipedia_search(brain_path, query: str = "", max_results: int = 3,
                      store_results: bool = True,
                      memory_store_fn=None, **kwargs) -> Dict[str, Any]:
    """Search Grokipedia and automatically store results in brain knowledge base.

    Uses the typeahead API to find articles, then fetches full content
    from server-rendered HTML pages. No browser required.

    Parameters:
        query: Search query
        max_results: Max articles to fetch (default 3)
        store_results: Whether to store in brain knowledge base
        memory_store_fn: Optional callable(article_data) to store in memory system
    """
    if not query:
        return {"error": "query is required"}

    # ── Redundancy check ──
    searches = load_recent_grokipedia_searches(brain_path)
    query_lower = query.lower().strip()

    for recent_q, ts in searches.items():
        if recent_q.lower().strip() == query_lower:
            hours_ago = (time.time() - ts) / 3600
            return {
                "query": query,
                "warning": "exact_duplicate",
                "message": f"This exact topic was searched {hours_ago:.1f} hours ago",
                "suggested_action": "use brain_network_search for existing knowledge",
            }
        if _queries_are_similar(query, recent_q) and time.time() - ts < 3600:
            return {
                "query": query,
                "warning": "redundant_search",
                "message": f"Similar search performed recently: '{recent_q}'",
                "suggested_action": "use brain_network_search instead",
            }

    # Track this search
    searches[query] = time.time()
    now = time.time()
    searches = {q: t for q, t in list(searches.items())[-200:] if now - t < 86400}
    save_recent_grokipedia_searches(brain_path, searches)

    try:
        # Step 1: Typeahead search to find articles
        logger.info(f"Searching Grokipedia for: '{query}'")
        search_results = _typeahead_search(query, limit=max_results + 2)

        if not search_results:
            return {"error": "No search results found", "query": query}

        # Step 2: Fetch full articles
        articles_data: List[Dict] = []
        for item in search_results[:max_results]:
            slug = item.get("slug", "")
            if not slug:
                continue
            try:
                logger.info(f"Fetching article: {item['title']} ({slug})")
                article = _fetch_article(slug)
                if article.get("content"):
                    articles_data.append(article)
                    if store_results and memory_store_fn:
                        try:
                            memory_store_fn(article)
                        except Exception as e:
                            logger.warning(f"Failed to store article: {e}")
            except Exception as e:
                logger.warning(f"Failed to fetch article {slug}: {e}")

        return {
            "query": query,
            "total_search_results": len(search_results),
            "successful_articles": len(articles_data),
            "articles_found": len(articles_data),
            "stored_in_brain": store_results and len(articles_data) > 0,
            "insights": format_grokipedia_insights(articles_data, query),
        }

    except Exception as e:
        logger.error(f"Grokipedia search failed: {e}")
        return {"error": f"Search failed: {str(e)}", "query": query}


# ─── Domain Distribution Analysis ────────────────────────────────

def calculate_balance_score(domain_breakdown: Dict[str, int]) -> float:
    """Calculate knowledge distribution balance (0.0 = imbalanced, 1.0 = perfect)."""
    if not domain_breakdown:
        return 0.0
    total = sum(domain_breakdown.values())
    if total == 0:
        return 0.0
    num_domains = len(domain_breakdown)
    entropy = 0.0
    for count in domain_breakdown.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    max_entropy = math.log2(num_domains)
    return round(entropy / max_entropy, 3) if max_entropy > 0 else 0.0


def get_knowledge_domain_distribution(brain_path) -> Dict[str, Any]:
    """Analyze the AI's current knowledge distribution across domains."""
    try:
        brain_file = Path(brain_path).parent / "node2040_brain.json"
        if not brain_file.exists():
            return {
                "total_domains": 0, "domains": {},
                "over_represented": [], "under_represented": [], "missing": [],
                "recommendations": ["Unable to analyze - brain file not found"],
            }

        with open(brain_file, "r") as f:
            brain_data = json.load(f)

        domain_breakdown = (
            brain_data.get("metadata", {})
            .get("brain_network_stats", {})
            .get("domain_breakdown", {})
        )
        if not domain_breakdown:
            return {
                "total_domains": 0, "domains": {},
                "over_represented": [], "under_represented": [], "missing": [],
                "recommendations": ["No domain data available"],
            }

        total = sum(domain_breakdown.values())
        sorted_domains = sorted(domain_breakdown.items(), key=lambda x: x[1], reverse=True)
        threshold_over = total * 0.10 if total > 0 else 0

        over_rep, under_rep, missing = [], [], []
        for domain, count in sorted_domains:
            pct = (count / total * 100) if total > 0 else 0
            if count == 0:
                missing.append(domain)
            elif count > threshold_over:
                over_rep.append({"domain": domain, "count": count, "percentage": round(pct, 2)})
            else:
                under_rep.append({"domain": domain, "count": count, "percentage": round(pct, 2)})

        recs: List[str] = []
        if missing:
            recs.append(f"Unexplored domains: {', '.join(missing[:5])}")
        if under_rep:
            top_under = sorted(under_rep, key=lambda x: x["count"])[:3]
            recs.append(f"Low knowledge in: {', '.join(d['domain'] for d in top_under)}")
        if len(over_rep) >= 2:
            recs.append(f"Heavy focus on: {', '.join(d['domain'] for d in over_rep[:2])}")

        return {
            "total_domains": len(domain_breakdown),
            "total_knowledge_entries": total,
            "domains": domain_breakdown,
            "over_represented": over_rep,
            "under_represented": under_rep,
            "missing": missing,
            "recommendations": recs,
            "balance_score": calculate_balance_score(domain_breakdown),
        }

    except Exception as e:
        logger.error(f"Error analyzing knowledge domain distribution: {e}")
        return {
            "total_domains": 0, "domains": {},
            "over_represented": [], "under_represented": [], "missing": [],
            "recommendations": [f"Analysis error: {str(e)}"],
        }


# ─── Knowledge Context Integration ───────────────────────────────

def pull_knowledge_topics(brain_path, semantic_cache: dict,
                          query: str = "", max_topics: int = 5) -> List[Dict]:
    """Pull relevant knowledge topics from semantic memory for AI context."""
    try:
        results = []
        query_lower = query.lower()
        for _key, mem in semantic_cache.items():
            if (query_lower in mem.topic.lower()
                    or query_lower in mem.content.lower()
                    or any(query_lower in r.lower() for r in mem.related_topics)):
                results.append({
                    "topic": mem.topic,
                    "content": mem.content[:200] + "..." if len(mem.content) > 200 else mem.content,
                    "key_facts": mem.key_facts,
                    "confidence": mem.confidence,
                    "domain": mem.domain,
                })
        results.sort(key=lambda x: x["confidence"], reverse=True)
        return results[:max_topics]
    except Exception as e:
        logger.error(f"Error pulling knowledge topics: {e}")
        return []


def integrate_knowledge_context(brain_path, node2040_brain: dict,
                                topics: List[Dict]) -> str:
    """Integrate knowledge topics into AI's active context and persist."""
    if not topics:
        return "No knowledge topics to integrate"
    try:
        parts = []
        for t in topics:
            parts.append(
                f"Topic: {t['topic']}\nKey Facts: {', '.join(t['key_facts'])}\nDomain: {t['domain']}\n"
            )
        integrated = "\n---\n".join(parts)

        preload = node2040_brain.get("preload", {})
        recent = preload.get("recent_context", [])
        recent.append({
            "timestamp": time.time(),
            "type": "knowledge_integration",
            "content": integrated,
            "topics_count": len(topics),
        })
        max_items = node2040_brain.get("metadata", {}).get("max_recent_items", 50)
        preload["recent_context"] = recent[-max_items:]
        node2040_brain["preload"] = preload

        # Persist
        n2040_path = Path(brain_path).parent / "node2040_brain.json"
        with open(n2040_path, "w") as f:
            json.dump(node2040_brain, f, indent=2, default=str)

        return f"Integrated {len(topics)} knowledge topics into active context"
    except Exception as e:
        logger.error(f"Error integrating knowledge context: {e}")
        return f"Error: {str(e)}"


# ─── Topic Analysis ──────────────────────────────────────────────

def analyze_topic_complexity(brain_path, topic: str = "",
                             search_fn=None, **kwargs) -> Dict[str, Any]:
    """Analyze topic complexity and determine knowledge requirements.

    search_fn: Optional callable(query, limit) returning list of memory objects
               with .confidence attribute.
    """
    if not topic:
        return {"error": "topic is required"}

    existing = []
    if search_fn:
        try:
            existing = search_fn(topic, limit=3)
        except Exception:
            existing = []

    depth = len(existing)
    avg_conf = sum(getattr(m, "confidence", 0.5) for m in existing) / max(depth, 1)
    needs_research = depth < 2 or avg_conf < 0.7

    return {
        "topic": topic,
        "existing_knowledge_count": depth,
        "average_confidence": avg_conf,
        "needs_research": needs_research,
        "recommended_tools": ["search_knowledge", "fetch_web_info"] if needs_research else ["search_knowledge"],
    }


def find_related_topics(brain_path, topic: str = "",
                        search_semantic_fn=None,
                        search_episodic_fn=None, **kwargs) -> List[str]:
    """Find topics related to the given topic."""
    if not topic:
        return []
    related: set = set()

    if search_semantic_fn:
        try:
            for mem in search_semantic_fn(topic, limit=10):
                related.update(getattr(mem, "related_topics", []))
        except Exception:
            pass

    if search_episodic_fn:
        try:
            for mem in search_episodic_fn(topic, limit=5):
                words = getattr(mem, "content", "").lower().split()
                for w in words:
                    if len(w) > 4 and w not in {"about", "would", "there", "their", "which", "could"}:
                        related.add(w)
        except Exception:
            pass

    return list(related)[:10]
