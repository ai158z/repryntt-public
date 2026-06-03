#!/usr/bin/env python3
"""
knowledge_router.py — Domain-Aware Knowledge Acquisition System
═══════════════════════════════════════════════════════════════════
Replaces all search-engine scraping (Google/DuckDuckGo/Selenium)
with legitimate, commercially-licensable knowledge sources:

  • Wikipedia REST API (free, unlimited)
  • arXiv API (free, unlimited)
  • PubMed/NCBI E-Utilities (free, 10/sec)
  • NASA Open APIs (free, 1000/hr)
  • Open-Meteo Weather API (free, unlimited)
  • CrossRef (free, academic metadata)
  • OpenStreetMap Nominatim (free, 1/sec)
  • FRED (Federal Reserve Economic Data, free)
  • Direct URL fetching via requests+BeautifulSoup (legal)

Architecture:
  KnowledgeRouter.search(query, domain_hint)
    → determines best sources for the query
    → queries them in parallel/fallback order
    → returns unified results dict compatible with the old search API

No Selenium. No Chrome. No search engine scraping.
All sources have official APIs or explicitly allow programmatic access.
═══════════════════════════════════════════════════════════════════
"""

import re
import time
import json
import logging
import requests
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus, urljoin, urlparse
from datetime import datetime

from repryntt.search.grokipedia import _typeahead_search, _fetch_article

logger = logging.getLogger("saige.knowledge_router")

# ── Rate limit tracker ──
_rate_limits: Dict[str, float] = {}
_rate_lock = __import__("threading").Lock()

# Per-source cool-down (epoch time when the source becomes usable again).
# Set when a source returns 429 — prevents the next heartbeat from hammering
# the same endpoint that just rate-limited us. Cleared automatically on
# successful subsequent calls.
_source_cooldowns: Dict[str, float] = {}

# Maximum Retry-After we'll actually wait for synchronously. Anything longer
# than this skips the source for this call rather than blocking the heartbeat.
_MAX_INLINE_RETRY_AFTER_SEC = 8.0

# Default cool-down when 429 has no Retry-After header.
_DEFAULT_COOLDOWN_SEC = 60.0


def _rate_limit(source: str, min_interval: float = 1.0):
    """Simple per-source rate limiting."""
    with _rate_lock:
        last = _rate_limits.get(source, 0)
        now = time.time()
        wait = min_interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        _rate_limits[source] = time.time()


def _is_cooled_down(source: str) -> Tuple[bool, float]:
    """Return (cooled_down, seconds_remaining). cooled_down=True means skip."""
    with _rate_lock:
        until = _source_cooldowns.get(source, 0.0)
    remaining = until - time.time()
    return (remaining > 0, max(0.0, remaining))


def _mark_cooldown(source: str, retry_after_sec: float):
    """Mark a source as cooling down. Subsequent calls skip until expiry."""
    with _rate_lock:
        _source_cooldowns[source] = max(
            _source_cooldowns.get(source, 0.0),
            time.time() + max(1.0, float(retry_after_sec)),
        )
    logger.warning(
        f"⏸️ {source} cooling down for {retry_after_sec:.0f}s "
        f"(further calls this window will be skipped)"
    )


def _parse_retry_after(resp) -> Optional[float]:
    """Pull the Retry-After header from a response. Accepts seconds-as-int
    or HTTP-date. Returns None if absent or unparseable."""
    try:
        h = resp.headers.get("Retry-After") if resp is not None else None
        if not h:
            return None
        h = h.strip()
        try:
            return float(h)
        except ValueError:
            pass
        # HTTP-date — let email.utils handle it
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            when = parsedate_to_datetime(h)
            now = datetime.now(timezone.utc)
            return max(0.0, (when - now).total_seconds())
        except Exception:
            return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
# INDIVIDUAL KNOWLEDGE SOURCES
# ════════════════════════════════════════════════════════════════════

class WikipediaSource:
    """Wikipedia REST API — General knowledge, definitions, overviews."""
    NAME = "wikipedia"
    BASE = "https://en.wikipedia.org/api/rest_v1"
    SEARCH_URL = "https://en.wikipedia.org/w/api.php"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search Wikipedia and return article summaries."""
        _rate_limit("wikipedia", 0.2)
        try:
            # Step 1: Search for articles
            params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": max_results,
                "format": "json",
                "utf8": 1,
            }
            headers = {"User-Agent": "Repryntt/0.1 (Autonomous AI Research)"}
            resp = requests.get(WikipediaSource.SEARCH_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            search_results = data.get("query", {}).get("search", [])

            results = []
            for sr in search_results:
                title = sr.get("title", "")
                snippet = re.sub(r"<[^>]+>", "", sr.get("snippet", ""))  # strip HTML
                page_id = sr.get("pageid", 0)

                # Step 2: Get article summary via REST
                try:
                    _rate_limit("wikipedia", 0.15)
                    summary_resp = requests.get(
                        f"{WikipediaSource.BASE}/page/summary/{quote_plus(title)}",
                        headers={"User-Agent": "Repryntt/0.1 (Autonomous AI Research)"},
                        timeout=10,
                    )
                    if summary_resp.status_code == 200:
                        sdata = summary_resp.json()
                        extract = sdata.get("extract", snippet)
                        url = sdata.get("content_urls", {}).get("desktop", {}).get("page", "")
                    else:
                        extract = snippet
                        url = f"https://en.wikipedia.org/wiki/{quote_plus(title)}"
                except Exception:
                    extract = snippet
                    url = f"https://en.wikipedia.org/wiki/{quote_plus(title)}"

                results.append({
                    "title": title,
                    "url": url,
                    "snippet": extract[:500],
                    "content": extract,
                    "source": "wikipedia",
                    "page_id": page_id,
                })

            return results

        except Exception as e:
            logger.warning(f"Wikipedia search failed: {e}")
            return []

    @staticmethod
    def get_full_article(title: str) -> Optional[Dict[str, Any]]:
        """Get full article text from Wikipedia."""
        _rate_limit("wikipedia", 0.2)
        try:
            params = {
                "action": "query",
                "titles": title,
                "prop": "extracts",
                "explaintext": True,
                "format": "json",
            }
            headers = {"User-Agent": "Repryntt/0.1 (Autonomous AI Research)"}
            resp = requests.get(WikipediaSource.SEARCH_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page_id, page in pages.items():
                if page_id == "-1":
                    continue
                return {
                    "title": page.get("title", title),
                    "content": page.get("extract", ""),
                    "url": f"https://en.wikipedia.org/wiki/{quote_plus(page.get('title', title))}",
                    "source": "wikipedia",
                }
            return None
        except Exception as e:
            logger.warning(f"Wikipedia full article failed: {e}")
            return None


class GrokipediaSource:
    """Grokipedia — General knowledge via grokipedia.com typeahead + article fetch."""
    NAME = "grokipedia"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search Grokipedia and return article content."""
        _rate_limit("grokipedia", 1.0)
        try:
            hits = _typeahead_search(query, limit=max_results + 2)
            results = []
            for item in hits[:max_results]:
                slug = item.get("slug", "")
                if not slug:
                    continue
                try:
                    _rate_limit("grokipedia", 0.5)
                    article = _fetch_article(slug, max_chars=5000)
                    content = article.get("content", item.get("snippet", ""))
                    results.append({
                        "title": article.get("title", item.get("title", slug)),
                        "url": article.get("url", f"https://grokipedia.com/page/{slug}"),
                        "snippet": (article.get("description") or content)[:300],
                        "content": content,
                        "source": "grokipedia",
                    })
                except Exception as e:
                    logger.warning(f"Grokipedia article fetch failed ({slug}): {e}")
            return results
        except Exception as e:
            logger.warning(f"Grokipedia search failed: {e}")
            return []


class ArxivSource:
    """arXiv API — Research papers in physics, math, CS, biology, etc."""
    NAME = "arxiv"
    BASE = "http://export.arxiv.org/api/query"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search arXiv for academic papers.

        Respects per-source cooldowns: if arXiv 429'd us earlier in the
        window, we skip the call entirely rather than burning the heartbeat
        budget on retries. Honors the Retry-After header when present and
        inline-waits at most _MAX_INLINE_RETRY_AFTER_SEC; anything longer
        flips to cooldown-skip.
        """
        cooled, remaining = _is_cooled_down("arxiv")
        if cooled:
            logger.info(
                f"⏸️ arXiv search skipped — cooling down for {remaining:.0f}s more"
            )
            return []
        _rate_limit("arxiv", 3.0)  # arXiv asks for 3s between requests
        try:
            params = {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max_results,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
            resp = requests.get(ArxivSource.BASE, params=params, timeout=20)
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp) or _DEFAULT_COOLDOWN_SEC
                if retry_after <= _MAX_INLINE_RETRY_AFTER_SEC:
                    logger.info(f"arXiv 429 — waiting {retry_after:.1f}s then retrying once")
                    time.sleep(retry_after)
                    resp = requests.get(ArxivSource.BASE, params=params, timeout=20)
                    if resp.status_code == 429:
                        retry_after = _parse_retry_after(resp) or _DEFAULT_COOLDOWN_SEC
                        _mark_cooldown("arxiv", retry_after)
                        return []
                else:
                    _mark_cooldown("arxiv", retry_after)
                    return []
            resp.raise_for_status()

            # Parse Atom XML
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(resp.text)
            results = []

            for entry in root.findall("atom:entry", ns):
                title_el = entry.find("atom:title", ns)
                summary_el = entry.find("atom:summary", ns)
                id_el = entry.find("atom:id", ns)
                published_el = entry.find("atom:published", ns)

                title = title_el.text.strip().replace("\n", " ") if title_el is not None else "?"
                summary = summary_el.text.strip().replace("\n", " ") if summary_el is not None else ""
                url = id_el.text.strip() if id_el is not None else ""
                published = published_el.text.strip()[:10] if published_el is not None else ""

                authors = []
                for author_el in entry.findall("atom:author", ns):
                    name_el = author_el.find("atom:name", ns)
                    if name_el is not None:
                        authors.append(name_el.text.strip())

                # Get categories
                categories = []
                for cat_el in entry.findall("{http://arxiv.org/schemas/atom}primary_category"):
                    categories.append(cat_el.get("term", ""))

                results.append({
                    "title": title,
                    "url": url,
                    "snippet": summary[:300],
                    "content": summary,
                    "source": "arxiv",
                    "authors": authors[:5],
                    "published": published,
                    "categories": categories,
                })

            return results

        except Exception as e:
            logger.warning(f"arXiv search failed: {e}")
            return []


class PubMedSource:
    """PubMed/NCBI E-Utilities — Biomedical and life science literature."""
    NAME = "pubmed"
    ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search PubMed for biomedical literature."""
        _rate_limit("pubmed", 0.35)  # NCBI allows ~3/sec without API key
        try:
            # Step 1: Search for PMIDs
            params = {
                "db": "pubmed",
                "term": query,
                "retmax": max_results,
                "retmode": "json",
                "sort": "relevance",
            }
            resp = requests.get(PubMedSource.ESEARCH, params=params, timeout=15)
            resp.raise_for_status()
            pmids = resp.json().get("esearchresult", {}).get("idlist", [])

            if not pmids:
                return []

            # Step 2: Fetch summaries
            _rate_limit("pubmed", 0.35)
            summary_params = {
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "json",
            }
            summary_resp = requests.get(PubMedSource.ESUMMARY, params=summary_params, timeout=15)
            summary_resp.raise_for_status()
            summary_data = summary_resp.json().get("result", {})

            results = []
            for pmid in pmids:
                article = summary_data.get(pmid, {})
                if not article or isinstance(article, list):
                    continue

                title = article.get("title", "")
                # Get authors
                authors = []
                for auth in article.get("authors", [])[:5]:
                    authors.append(auth.get("name", ""))

                source_journal = article.get("source", "")
                pub_date = article.get("pubdate", "")

                results.append({
                    "title": title,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "snippet": f"{title} — {source_journal} ({pub_date})",
                    "content": f"Title: {title}\nAuthors: {', '.join(authors)}\nJournal: {source_journal}\nDate: {pub_date}\nPMID: {pmid}",
                    "source": "pubmed",
                    "authors": authors,
                    "journal": source_journal,
                    "published": pub_date,
                    "pmid": pmid,
                })

            return results

        except Exception as e:
            logger.warning(f"PubMed search failed: {e}")
            return []


class NASASource:
    """NASA Open APIs — Space, astronomy, planetary science."""
    NAME = "nasa"
    # NASA Image and Video Library (no key needed)
    SEARCH_URL = "https://images-api.nasa.gov/search"
    # NASA TechPort (no key needed)
    TECHPORT_URL = "https://techport.nasa.gov/api/projects"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search NASA image/video library + tech projects."""
        _rate_limit("nasa", 1.0)
        results = []
        try:
            params = {
                "q": query,
                "media_type": "image",  # Also returns descriptions
            }
            resp = requests.get(NASASource.SEARCH_URL, params=params, timeout=15)
            resp.raise_for_status()
            items = resp.json().get("collection", {}).get("items", [])

            for item in items[:max_results]:
                data = item.get("data", [{}])[0]
                title = data.get("title", "")
                description = data.get("description", "")
                nasa_id = data.get("nasa_id", "")
                date_created = data.get("date_created", "")[:10]
                center = data.get("center", "")

                results.append({
                    "title": title,
                    "url": f"https://images.nasa.gov/details/{nasa_id}" if nasa_id else "",
                    "snippet": description[:300],
                    "content": description,
                    "source": "nasa",
                    "date": date_created,
                    "center": center,
                })

        except Exception as e:
            logger.warning(f"NASA search failed: {e}")

        return results


class OpenMeteoSource:
    """Open-Meteo — Free weather and climate data API."""
    NAME = "open_meteo"
    GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
    WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
    HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"

    @staticmethod
    def search(query: str, max_results: int = 3) -> List[Dict[str, Any]]:
        """Get weather data for a location."""
        _rate_limit("open_meteo", 0.5)
        try:
            # Extract location from query
            location = re.sub(
                r"(?:current\s+|today['\u2019]?s?\s+)?(weather|forecast|temperature|rain|wind|humidity|climate|conditions?)(?:\s+(?:in|for|at|near|of|around))?\s*",
                "", query, flags=re.IGNORECASE,
            ).strip()
            # Clean up residual prepositions
            location = re.sub(r"^(?:in|for|at|near|of|around)\s+", "", location, flags=re.IGNORECASE).strip()
            if not location:
                location = query

            # Geocode the location
            geo_resp = requests.get(
                OpenMeteoSource.GEOCODE_URL,
                params={"name": location, "count": 1, "language": "en"},
                timeout=10,
            )
            geo_resp.raise_for_status()
            geo_results = geo_resp.json().get("results", [])
            
            # If full location string fails, try just the city name (first word/words before state)
            if not geo_results:
                # Try splitting "City State" → just "City"
                parts = re.split(r",\s*|\s+(?:FL|CA|NY|TX|OH|PA|IL|GA|NC|MI|NJ|VA|WA|AZ|MA|TN|IN|MO|MD|WI|MN|CO|AL|SC|LA|KY|OR|OK|CT|UT|IA|NV|AR|MS|KS|NM|NE|ID|WV|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WY|DC|Florida|California|Texas|New York|Ohio|Pennsylvania|Illinois|Georgia|North Carolina|Michigan|New Jersey|Virginia|Washington|Arizona|Massachusetts|Tennessee|Indiana|Missouri|Maryland|Wisconsin|Minnesota|Colorado|Alabama|South Carolina|Louisiana|Kentucky|Oregon|Oklahoma|Connecticut|Utah|Iowa|Nevada|Arkansas|Mississippi|Kansas)\b", location, flags=re.IGNORECASE)
                city_name = parts[0].strip() if parts else location
                if city_name and city_name != location:
                    geo_resp2 = requests.get(
                        OpenMeteoSource.GEOCODE_URL,
                        params={"name": city_name, "count": 1, "language": "en"},
                        timeout=10,
                    )
                    geo_results = geo_resp2.json().get("results", [])
            
            if not geo_results:
                return [{"title": "Location not found", "content": f"Could not geocode '{location}'", "source": "open_meteo"}]

            lat = geo_results[0]["latitude"]
            lon = geo_results[0]["longitude"]
            name = geo_results[0].get("name", location)
            country = geo_results[0].get("country", "")
            admin = geo_results[0].get("admin1", "")
            location_str = f"{name}, {admin}, {country}" if admin else f"{name}, {country}"

            # Fetch current weather + 7-day forecast
            _rate_limit("open_meteo", 0.3)
            weather_resp = requests.get(
                OpenMeteoSource.WEATHER_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                    "timezone": "auto",
                    "forecast_days": 7,
                },
                timeout=10,
            )
            weather_resp.raise_for_status()
            weather = weather_resp.json()

            current = weather.get("current", {})
            daily = weather.get("daily", {})

            # Weather code descriptions
            WMO_CODES = {
                0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
                45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
                55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
                71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 80: "Slight showers",
                81: "Moderate showers", 82: "Violent showers", 95: "Thunderstorm",
                96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
            }

            current_desc = WMO_CODES.get(current.get("weather_code", -1), "Unknown")
            content_parts = [
                f"Weather for {location_str}",
                f"\nCurrent Conditions:",
                f"  Temperature: {current.get('temperature_2m', '?')}°F (feels like {current.get('apparent_temperature', '?')}°F)",
                f"  Conditions: {current_desc}",
                f"  Humidity: {current.get('relative_humidity_2m', '?')}%",
                f"  Wind: {current.get('wind_speed_10m', '?')} mph",
                f"  Precipitation: {current.get('precipitation', 0)} in",
                f"\n7-Day Forecast:",
            ]

            dates = daily.get("time", [])
            maxtemps = daily.get("temperature_2m_max", [])
            mintemps = daily.get("temperature_2m_min", [])
            precip = daily.get("precipitation_sum", [])
            codes = daily.get("weather_code", [])

            for i in range(min(7, len(dates))):
                desc = WMO_CODES.get(codes[i] if i < len(codes) else -1, "?")
                content_parts.append(
                    f"  {dates[i]}: {desc}, High {maxtemps[i] if i < len(maxtemps) else '?'}°F / "
                    f"Low {mintemps[i] if i < len(mintemps) else '?'}°F, "
                    f"Precip: {precip[i] if i < len(precip) else 0} in"
                )

            content = "\n".join(content_parts)
            return [{
                "title": f"Weather — {location_str}",
                "url": f"https://open-meteo.com/",
                "snippet": f"Current: {current.get('temperature_2m', '?')}°F, {current_desc}",
                "content": content,
                "source": "open_meteo",
                "location": location_str,
            }]

        except Exception as e:
            logger.warning(f"Open-Meteo search failed: {e}")
            return []


class CrossRefSource:
    """CrossRef — Academic paper metadata from DOIs and journal articles."""
    NAME = "crossref"
    BASE = "https://api.crossref.org/works"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search CrossRef for academic publications."""
        _rate_limit("crossref", 1.0)
        try:
            params = {
                "query": query,
                "rows": max_results,
                "select": "DOI,title,author,published-print,container-title,abstract,URL",
                "sort": "relevance",
            }
            headers = {"User-Agent": "Repryntt/0.1 (Autonomous AI Research)"}
            resp = requests.get(CrossRefSource.BASE, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])

            results = []
            for item in items:
                title_list = item.get("title", [""])
                title = title_list[0] if title_list else "Untitled"
                doi = item.get("DOI", "")
                url = item.get("URL", f"https://doi.org/{doi}" if doi else "")
                journal_list = item.get("container-title", [""])
                journal = journal_list[0] if journal_list else ""
                abstract = item.get("abstract", "")
                # Clean HTML from abstract
                abstract = re.sub(r"<[^>]+>", "", abstract)

                authors = []
                for auth in item.get("author", [])[:5]:
                    given = auth.get("given", "")
                    family = auth.get("family", "")
                    authors.append(f"{given} {family}".strip())

                pub_date_parts = item.get("published-print", {}).get("date-parts", [[]])
                pub_date = "-".join(str(p) for p in pub_date_parts[0]) if pub_date_parts[0] else ""

                results.append({
                    "title": title,
                    "url": url,
                    "snippet": abstract[:300] if abstract else f"{title} — {journal} ({pub_date})",
                    "content": f"Title: {title}\nAuthors: {', '.join(authors)}\nJournal: {journal}\nDOI: {doi}\nDate: {pub_date}\n\nAbstract: {abstract}" if abstract else f"Title: {title}\nAuthors: {', '.join(authors)}\nJournal: {journal}\nDOI: {doi}\nDate: {pub_date}",
                    "source": "crossref",
                    "doi": doi,
                    "journal": journal,
                    "authors": authors,
                    "published": pub_date,
                })

            return results

        except Exception as e:
            logger.warning(f"CrossRef search failed: {e}")
            return []


class OpenStreetMapSource:
    """OpenStreetMap Nominatim — Geocoding and geographic data."""
    NAME = "openstreetmap"
    BASE = "https://nominatim.openstreetmap.org"

    @staticmethod
    def search(query: str, max_results: int = 3) -> List[Dict[str, Any]]:
        """Search for geographic locations."""
        _rate_limit("nominatim", 1.1)  # Nominatim: max 1 req/sec
        try:
            params = {
                "q": query,
                "format": "json",
                "limit": max_results,
                "addressdetails": 1,
                "extratags": 1,
            }
            headers = {"User-Agent": "Repryntt/0.1"}
            resp = requests.get(
                f"{OpenStreetMapSource.BASE}/search",
                params=params, headers=headers, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data:
                display = item.get("display_name", "")
                lat = item.get("lat", "")
                lon = item.get("lon", "")
                osm_type = item.get("type", "")
                address = item.get("address", {})

                content = f"Location: {display}\nCoordinates: {lat}, {lon}\nType: {osm_type}"
                if address:
                    addr_parts = []
                    for key in ["road", "city", "town", "village", "state", "country", "postcode"]:
                        if key in address:
                            addr_parts.append(f"{key}: {address[key]}")
                    if addr_parts:
                        content += "\nAddress: " + ", ".join(addr_parts)

                results.append({
                    "title": display,
                    "url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}",
                    "snippet": display,
                    "content": content,
                    "source": "openstreetmap",
                    "lat": lat,
                    "lon": lon,
                })

            return results

        except Exception as e:
            logger.warning(f"Nominatim search failed: {e}")
            return []


class FREDSource:
    """FRED (Federal Reserve Economic Data) — Economic indicators."""
    NAME = "fred"
    BASE = "https://api.stlouisfed.org/fred"

    @staticmethod
    def search(query: str, max_results: int = 5, api_key: str = None) -> List[Dict[str, Any]]:
        """Search FRED for economic data series."""
        if not api_key:
            # FRED requires an API key, but we can still search series
            # without one via the website. For now, return guidance.
            return [{
                "title": "FRED Economic Data",
                "url": f"https://fred.stlouisfed.org/searchresults/?st={quote_plus(query)}",
                "snippet": f"Search FRED for '{query}' — Free API key at https://fred.stlouisfed.org/docs/api/api_key.html",
                "content": f"FRED (Federal Reserve Economic Data) has comprehensive US economic data. Visit the URL for '{query}' data. To enable direct API access, set a FRED API key.",
                "source": "fred",
            }]

        _rate_limit("fred", 1.0)
        try:
            params = {
                "api_key": api_key,
                "search_text": query,
                "limit": max_results,
                "file_type": "json",
            }
            resp = requests.get(f"{FREDSource.BASE}/series/search", params=params, timeout=15)
            resp.raise_for_status()
            series_list = resp.json().get("seriess", [])

            results = []
            for s in series_list:
                sid = s.get("id", "")
                title = s.get("title", "")
                notes = s.get("notes", "")
                freq = s.get("frequency", "")
                units = s.get("units", "")
                last_updated = s.get("last_updated", "")

                results.append({
                    "title": title,
                    "url": f"https://fred.stlouisfed.org/series/{sid}",
                    "snippet": f"{title} ({freq}, {units})",
                    "content": f"Series: {sid}\nTitle: {title}\nFrequency: {freq}\nUnits: {units}\nLast Updated: {last_updated}\n\n{notes}",
                    "source": "fred",
                    "series_id": sid,
                })

            return results

        except Exception as e:
            logger.warning(f"FRED search failed: {e}")
            return []


class DirectURLFetcher:
    """
    Fetch and extract content from any URL using requests + BeautifulSoup.
    No Selenium. No Chrome. Legal — you're just visiting a URL like a browser.
    Human-like headers & timing to avoid bot detection.
    """
    NAME = "direct_fetch"

    # Rotate realistic browser User-Agents to avoid fingerprinting
    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    @staticmethod
    def _human_headers(url: str) -> Dict[str, str]:
        """Build realistic browser headers to avoid bot detection."""
        import random
        domain = urlparse(url).netloc
        return {
            "User-Agent": random.choice(DirectURLFetcher._USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Referer": f"https://www.google.com/search?q={domain}",
            "Cache-Control": "max-age=0",
        }

    @staticmethod
    def fetch(url: str, max_content_length: int = 15000) -> Optional[Dict[str, Any]]:
        """Fetch a URL and extract readable content with human-like behavior."""
        import random

        # SSRF protection: block private/internal URLs before fetching
        try:
            from repryntt.search.url_guard import validate_url
            url = validate_url(url)
        except ValueError as e:
            logger.warning(f"🛡️ SSRF blocked in DirectURLFetcher: {e}")
            return {"url": url, "error": f"Blocked: {e}", "success": False, "source": "direct_fetch"}

        # Human-like delay: 1.5-3.5s between fetches (real users don't click instantly)
        _rate_limit("direct_fetch", 1.5 + random.random() * 2.0)

        max_retries = 2
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                headers = DirectURLFetcher._human_headers(url)
                resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)

                # Retry on 403/429/500/502/503 with exponential backoff
                if resp.status_code in (403, 429, 500, 502, 503) and attempt < max_retries:
                    wait = (2 ** attempt) + random.random() * 2
                    logger.info(f"⏳ Fetch {url} got {resp.status_code}, retrying in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                break  # success
            except requests.exceptions.HTTPError as e:
                last_error = e
                if attempt < max_retries:
                    wait = (2 ** attempt) + random.random() * 2
                    logger.info(f"⏳ Fetch {url} HTTP error, retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise
            except requests.exceptions.Timeout:
                last_error = "Timeout"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                logger.warning(f"Timeout fetching {url} after {max_retries+1} attempts")
                return {"url": url, "error": "Timeout after retries", "success": False, "source": "direct_fetch"}
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                logger.warning(f"Failed to fetch {url}: {e}")
                return {"url": url, "error": str(e), "success": False, "source": "direct_fetch"}

        try:

            content_type = resp.headers.get("content-type", "")

            # Handle JSON responses directly
            if "json" in content_type:
                try:
                    data = resp.json()
                    text = json.dumps(data, indent=2)[:max_content_length]
                    return {
                        "title": urlparse(url).netloc,
                        "url": url,
                        "content": text,
                        "content_length": len(text),
                        "source": "direct_fetch",
                        "success": True,
                    }
                except Exception:
                    pass

            # Handle plain text
            if "text/plain" in content_type:
                text = resp.text[:max_content_length]
                return {
                    "title": urlparse(url).netloc,
                    "url": url,
                    "content": text,
                    "content_length": len(text),
                    "source": "direct_fetch",
                    "success": True,
                }

            # Parse HTML
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                return {
                    "title": urlparse(url).netloc,
                    "url": url,
                    "content": resp.text[:max_content_length],
                    "content_length": len(resp.text),
                    "source": "direct_fetch",
                    "success": True,
                }

            soup = BeautifulSoup(resp.text, "lxml")

            # Get title
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else urlparse(url).netloc

            # Remove non-content elements
            for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                                       "aside", "form", "iframe", "noscript"]):
                tag.decompose()

            # Try to find main content
            content = ""
            for selector in ["article", "main", '[role="main"]',
                             ".content", "#content", ".post-content",
                             ".entry-content", ".article-body"]:
                el = soup.select_one(selector)
                if el:
                    content = el.get_text(separator="\n", strip=True)
                    break

            if not content or len(content) < 100:
                # Fallback: get body text
                body = soup.find("body")
                if body:
                    content = body.get_text(separator="\n", strip=True)
                else:
                    content = soup.get_text(separator="\n", strip=True)

            # Clean up: remove excessive blank lines
            content = re.sub(r"\n{3,}", "\n\n", content)
            content = content[:max_content_length]

            # Extract headings for structure
            headings = []
            for h in soup.find_all(["h1", "h2", "h3"])[:10]:
                headings.append(h.get_text(strip=True))

            return {
                "title": title,
                "url": url,
                "content": content,
                "content_length": len(content),
                "headings": headings,
                "source": "direct_fetch",
                "success": True,
            }

        except Exception as e:
            logger.warning(f"Failed to parse content from {url}: {e}")
            return {"url": url, "error": str(e), "success": False, "source": "direct_fetch"}


class MaterialsProjectSource:
    """Materials Project API — Materials science data (crystal structures, properties)."""
    NAME = "materials_project"
    # The MP API requires a key but the search endpoint gives useful metadata
    BASE = "https://api.materialsproject.org"

    @staticmethod
    def search(query: str, max_results: int = 5, api_key: str = None) -> List[Dict[str, Any]]:
        """Search Materials Project — requires API key for full access."""
        if not api_key:
            # Without API key, return guidance + link
            return [{
                "title": f"Materials Project — {query}",
                "url": f"https://next-gen.materialsproject.org/materials?formula={quote_plus(query)}",
                "snippet": f"Search Materials Project for '{query}'. Free API key at https://materialsproject.org/api",
                "content": f"Materials Project has comprehensive data on materials properties, crystal structures, band gaps, and more. Visit the URL for '{query}' data.",
                "source": "materials_project",
            }]
        return []


class PubChemSource:
    """PubChem PUG REST — Chemical compound data."""
    NAME = "pubchem"
    BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    @staticmethod
    def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search PubChem for chemical compounds."""
        _rate_limit("pubchem", 0.5)
        try:
            # Search by name
            search_url = f"{PubChemSource.BASE}/compound/name/{quote_plus(query)}/JSON"
            resp = requests.get(search_url, timeout=15)

            if resp.status_code != 200:
                # Try autocomplete search
                auto_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/autocomplete/compound/{quote_plus(query)}/json?limit={max_results}"
                auto_resp = requests.get(auto_url, timeout=10)
                if auto_resp.status_code == 200:
                    suggestions = auto_resp.json().get("dictionary_terms", {}).get("compound", [])
                    return [{
                        "title": f"PubChem: {s}",
                        "url": f"https://pubchem.ncbi.nlm.nih.gov/#query={quote_plus(s)}",
                        "snippet": f"Chemical compound match: {s}",
                        "content": f"Found compound suggestion: {s}. Search PubChem for full data.",
                        "source": "pubchem",
                    } for s in suggestions[:max_results]]
                return []

            data = resp.json()
            compounds = data.get("PC_Compounds", [])
            results = []

            for comp in compounds[:max_results]:
                cid = comp.get("id", {}).get("id", {}).get("cid", "")
                props = comp.get("props", [])

                # Extract useful properties
                prop_data = {}
                for p in props:
                    label = p.get("urn", {}).get("label", "")
                    name = p.get("urn", {}).get("name", "")
                    val = p.get("value", {})
                    value = val.get("sval", val.get("fval", val.get("ival", "")))
                    if label and value:
                        prop_data[f"{label} ({name})" if name else label] = value

                content_parts = [f"PubChem CID: {cid}"]
                for k, v in list(prop_data.items())[:15]:
                    content_parts.append(f"  {k}: {v}")

                results.append({
                    "title": f"PubChem CID {cid}",
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    "snippet": f"CID {cid} — {', '.join(list(prop_data.values())[:3])}",
                    "content": "\n".join(content_parts),
                    "source": "pubchem",
                    "cid": cid,
                })

            return results

        except Exception as e:
            logger.warning(f"PubChem search failed: {e}")
            return []


# ════════════════════════════════════════════════════════════════════
# DOMAIN DETECTION & ROUTING
# ════════════════════════════════════════════════════════════════════

# Map departments to knowledge sources
DEPARTMENT_SOURCES = {
    "energy_physics": ["arxiv", "crossref", "grokipedia"],
    "materials_science": ["arxiv", "crossref", "pubchem", "grokipedia"],
    "computational_science": ["arxiv", "crossref", "grokipedia"],
    "aerospace": ["nasa", "arxiv", "grokipedia"],
    "biotech_medical": ["pubmed", "crossref", "grokipedia"],
    "robotics_automation": ["arxiv", "crossref", "grokipedia"],
    "mathematics_theory": ["arxiv", "crossref", "grokipedia"],
    "communications": ["arxiv", "crossref", "grokipedia"],
    "climate_planetary": ["arxiv", "nasa", "grokipedia"],
    "mining_resources": ["arxiv", "crossref", "grokipedia"],
    "economics_governance": ["crossref", "fred", "grokipedia"],
    "electrical_engineering": ["arxiv", "crossref", "grokipedia"],
}

# Query keyword → source hints
KEYWORD_SOURCES = {
    # Weather patterns
    r"weather|forecast|temperature|rain|wind|humidity|heat wave|cold front|storm": ["open_meteo", "grokipedia"],
    # Geographic/location queries
    r"where is|location of|coordinates|map|latitude|longitude|directions|route": ["openstreetmap", "grokipedia"],
    # Medical/biology
    r"disease|drug|treatment|clinical|medical|health|gene|protein|cell|virus|bacter|pharma|symptom|diagnosis": ["pubmed", "crossref", "grokipedia"],
    # Chemistry
    r"compound|molecule|chemical|element|reaction|synthesis|polymer|catalyst|ion|atom|bond": ["pubchem", "crossref", "arxiv", "grokipedia"],
    # Space/astronomy
    r"planet|star|galaxy|astronaut|spacecraft|orbit|nasa|satellite|telescope|cosmos|nebula|asteroid|comet|rocket|launch": ["nasa", "arxiv", "grokipedia"],
    # Materials
    r"alloy|ceramic|graphene|composite|superconductor|crystal|nanomater|metal.*property|tensile|hardness": ["arxiv", "crossref", "grokipedia"],
    # Physics/math
    r"quantum|relativity|particle|boson|fermion|topology|manifold|theorem|equation|entropy|plasma|fusion|fission": ["arxiv", "crossref", "grokipedia"],
    # Economics
    r"gdp|inflation|unemployment|interest rate|stock|market|econom|trade|federal reserve|monetary|fiscal": ["fred", "crossref", "grokipedia"],
    # News/current events
    r"news|latest|recent|today|2026|current|update|breaking": ["grokipedia", "crossref"],
    # General
    r"what is|who is|when did|how does|define|explain|history of|overview|introduction to": ["grokipedia", "crossref"],
}

# Source registry
SOURCE_REGISTRY = {
    "grokipedia": GrokipediaSource,
    "wikipedia": WikipediaSource,
    "arxiv": ArxivSource,
    "pubmed": PubMedSource,
    "nasa": NASASource,
    "open_meteo": OpenMeteoSource,
    "crossref": CrossRefSource,
    "openstreetmap": OpenStreetMapSource,
    "fred": FREDSource,
    "pubchem": PubChemSource,
    "materials_project": MaterialsProjectSource,
}


def detect_query_sources(query: str, department: str = "") -> List[str]:
    """
    Determine which knowledge sources are most relevant for a query.
    Uses keyword matching + department mapping.
    Returns ordered list of source names (best first).
    """
    scores: Dict[str, float] = {}

    # Department-based scoring
    if department and department in DEPARTMENT_SOURCES:
        for i, src in enumerate(DEPARTMENT_SOURCES[department]):
            scores[src] = scores.get(src, 0) + (3.0 - i * 0.5)

    # Keyword-based scoring
    query_lower = query.lower()
    for pattern, sources in KEYWORD_SOURCES.items():
        if re.search(pattern, query_lower):
            for i, src in enumerate(sources):
                # First source in list gets highest score (5.0), decreasing
                scores[src] = scores.get(src, 0) + (5.0 - i * 1.0)

    # Grokipedia always gets a baseline score (general fallback)
    scores["grokipedia"] = scores.get("grokipedia", 0) + 1.0

    # Sort by score descending
    ranked = sorted(scores.keys(), key=lambda s: scores[s], reverse=True)

    # Limit to top 3 sources (avoid hammering too many APIs)
    return ranked[:3]


# ════════════════════════════════════════════════════════════════════
# MAIN KNOWLEDGE ROUTER
# ════════════════════════════════════════════════════════════════════

class KnowledgeRouter:
    """
    Unified knowledge acquisition interface.
    Drop-in replacement for the old google_web_search / DuckDuckGo system.

    Usage:
        router = KnowledgeRouter()
        result = router.search("graphene composite tensile strength", department="materials_science")
        result = router.fetch_url("https://arxiv.org/abs/2401.12345")
    """

    # Recent-query cache: prevents Andrew (or any caller) from firing the
    # same or near-identical search multiple times in a heartbeat. Without
    # this we observed 4 nearly-identical queries in ~5 minutes (each
    # fanning out to 3 sources), which is what was burning the NIM rate
    # budget and triggering the arXiv 429 cascade.
    _QUERY_CACHE_TTL_SEC = 300.0        # cache results for 5 min
    _QUERY_SIMILARITY_THRESHOLD = 0.82  # Jaccard threshold for "same query"
    _QUERY_CACHE_MAXLEN = 32

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="knowledge")
        # entries: list of (timestamp, normalized_query, cached_result)
        self._recent_queries: List[Tuple[float, str, Dict[str, Any]]] = []
        self._recent_lock = __import__("threading").Lock()

    @staticmethod
    def _normalize_query(q: str) -> str:
        """Lowercase + collapse whitespace + drop punctuation. Used for the
        similarity check below."""
        import re
        q = (q or "").lower()
        q = re.sub(r"[^\w\s]", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q

    @staticmethod
    def _query_similarity(a: str, b: str) -> float:
        """Jaccard on token sets. Good enough for "near-duplicate" detection
        without pulling in embeddings. Drops words ≤ 3 chars (stopword-ish)."""
        ta = {w for w in a.split() if len(w) > 3}
        tb = {w for w in b.split() if len(w) > 3}
        if not ta or not tb:
            return 0.0
        inter = ta & tb
        union = ta | tb
        return len(inter) / len(union) if union else 0.0

    def _check_query_cache(self, query: str) -> Optional[Dict[str, Any]]:
        """Return a cached result for a near-identical recent query, or None."""
        norm = self._normalize_query(query)
        now = time.time()
        with self._recent_lock:
            # Drop expired entries
            self._recent_queries = [
                e for e in self._recent_queries
                if now - e[0] < self._QUERY_CACHE_TTL_SEC
            ][-self._QUERY_CACHE_MAXLEN:]
            for ts, prev_norm, cached in reversed(self._recent_queries):
                sim = self._query_similarity(norm, prev_norm)
                if sim >= self._QUERY_SIMILARITY_THRESHOLD:
                    age = now - ts
                    logger.info(
                        f"♻️ Dedup hit: '{query[:60]}' ≈ '{prev_norm[:60]}' "
                        f"(sim={sim:.2f}, age={age:.0f}s) — returning cached result"
                    )
                    # Return a shallow copy with a flag so callers can see it
                    return {**cached, "deduplicated": True, "dedup_age_s": round(age, 1)}
        return None

    def _put_query_cache(self, query: str, result: Dict[str, Any]) -> None:
        if not result or not result.get("success"):
            return
        norm = self._normalize_query(query)
        with self._recent_lock:
            self._recent_queries.append((time.time(), norm, result))
            self._recent_queries = self._recent_queries[-self._QUERY_CACHE_MAXLEN:]

    def search(self, query: str, max_results: int = 10,
               department: str = "", sources_override: List[str] = None) -> Dict[str, Any]:
        """
        Search for knowledge across multiple legitimate sources.

        Args:
            query: The search query
            max_results: Maximum total results to return
            department: Department hint for routing (e.g. "materials_science")
            sources_override: Force specific sources (e.g. ["arxiv", "pubmed"])

        Returns:
            Dict compatible with the old google_web_search return format:
            {
                "success": bool,
                "query": str,
                "results": [...],
                "sources_queried": [...],
                "insights": str,
            }

        Near-duplicate queries fired within the past 5 minutes return the
        cached result with ``deduplicated=True`` instead of re-fanning out
        to all sources. This is the single biggest knock-on fix for the
        query-fan-out cascade that was burning the NIM rate budget.
        """
        if not query or not query.strip():
            return {"success": False, "error": "Empty query", "query": query}

        # ── Recent-query dedup ─────────────────────────────────────────
        cached = self._check_query_cache(query)
        if cached is not None:
            return cached

        # Determine which sources to query
        if sources_override:
            source_names = sources_override
        else:
            source_names = detect_query_sources(query, department)

        logger.info(f"🔍 Knowledge search: '{query}' → sources: {source_names}")

        all_results = []
        sources_queried = []
        errors = []

        # Query sources (sequentially to respect rate limits, but fast enough)
        for src_name in source_names:
            source_cls = SOURCE_REGISTRY.get(src_name)
            if not source_cls:
                continue

            try:
                per_source_max = max(3, max_results // len(source_names))
                results = source_cls.search(query, max_results=per_source_max)
                if results:
                    all_results.extend(results)
                    sources_queried.append(src_name)
                    logger.info(f"  ✅ {src_name}: {len(results)} results")
            except Exception as e:
                errors.append(f"{src_name}: {e}")
                logger.warning(f"  ❌ {src_name}: {e}")

        # Trim to max_results
        all_results = all_results[:max_results]

        if not all_results:
            # If all failed, try Grokipedia as ultimate fallback
            if "grokipedia" not in sources_queried:
                try:
                    results = GrokipediaSource.search(query, max_results=3)
                    if results:
                        all_results.extend(results)
                        sources_queried.append("grokipedia")
                except Exception:
                    pass

        # Build insights string (backward compatible with old search format)
        insights = self._format_insights(query, all_results, sources_queried)

        result = {
            "success": len(all_results) > 0,
            "query": query,
            "results": all_results,
            "result_count": len(all_results),
            "sources_queried": sources_queried,
            "errors": errors if errors else None,
            "insights": insights,
        }
        # Cache for near-duplicate dedup on subsequent calls
        self._put_query_cache(query, result)
        return result

    def search_results_only(self, query: str, max_results: int = 10,
                            department: str = "") -> Dict[str, Any]:
        """
        Lighter search: just titles, URLs, and snippets.
        No full content fetching. Faster.
        """
        result = self.search(query, max_results=max_results, department=department)
        if result.get("success"):
            # Trim content to just snippets
            for r in result.get("results", []):
                if "content" in r:
                    r["content"] = r.get("snippet", r["content"][:300])
            result["next_step"] = "Use fetch_url(url) to get full content from any result"
        return result

    # Minimum bytes of cleaned content for a fetch to count as useful.
    # Below this, the page is almost always a redirect stub, paywall splash,
    # JS-only shell, or DOI bounce — Andrew should not waste an LLM call
    # reasoning about it. Tuned from observed failures (DOI redirects came
    # back at 301/319 chars).
    _MIN_USEFUL_CONTENT_BYTES = 500

    # Substrings that mark a page as a "dead end" even at higher byte counts:
    # paywall splash, JS shell, captcha wall. Case-insensitive substring match
    # against the first 1200 chars of the content.
    _DEAD_END_SIGNATURES = (
        "please sign in", "subscribe to continue", "your access is limited",
        "purchase to read", "this article is for subscribers",
        "enable javascript", "javascript is required", "please enable cookies",
        "verify you are human", "checking your browser", "captcha",
        "access denied", "are you a robot",
        # DOI / publisher redirect stubs
        "redirecting to", "this page is redirecting",
        "you are being redirected",
    )

    def fetch_url(self, url: str) -> Dict[str, Any]:
        """
        Fetch full content from a specific URL.
        Uses requests + BeautifulSoup — no Selenium, no Chrome.
        Legal: just visiting a URL like a browser.

        Empty-scrape short-circuit: pages that come back with less than
        ``_MIN_USEFUL_CONTENT_BYTES`` of cleaned content, or whose content
        matches a known dead-end signature (paywall splash, JS shell,
        captcha, DOI redirect stub) are returned with ``success=False``
        and ``dead_end=True`` so callers don't recurse on them.
        """
        # SSRF protection at the router level (defense in depth)
        try:
            from repryntt.search.url_guard import validate_url
            url = validate_url(url)
        except ValueError as e:
            logger.warning(f"🛡️ SSRF blocked in fetch_url: {e}")
            return {"success": False, "url": url, "error": f"Blocked: {e}"}

        logger.info(f"📄 Fetching URL: {url}")
        result = DirectURLFetcher.fetch(url)
        if result and result.get("success"):
            content = result.get("content", "") or ""
            content_len = result.get("content_length", len(content))
            title = result.get("title", "") or ""

            # ── Dead-end detection ────────────────────────────────────
            preview = content[:1200].lower() if content else ""
            matched_signature: Optional[str] = None
            for sig in self._DEAD_END_SIGNATURES:
                if sig in preview:
                    matched_signature = sig
                    break

            if content_len < self._MIN_USEFUL_CONTENT_BYTES:
                logger.info(
                    f"📄 Dead-end fetch: {url} returned only {content_len} bytes "
                    f"(threshold {self._MIN_USEFUL_CONTENT_BYTES}); marking dead-end"
                )
                return {
                    "success": False,
                    "dead_end": True,
                    "url": url,
                    "title": title,
                    "content_length": content_len,
                    "error": (
                        f"Page returned only {content_len} bytes of usable content "
                        f"(threshold {self._MIN_USEFUL_CONTENT_BYTES}). This is almost "
                        f"always a paywall splash, JS-only shell, or redirect stub. "
                        f"Do NOT retry this URL — pick a different result from your search."
                    ),
                }
            if matched_signature:
                logger.info(
                    f"📄 Dead-end fetch: {url} matched signature {matched_signature!r}; "
                    f"marking dead-end"
                )
                return {
                    "success": False,
                    "dead_end": True,
                    "url": url,
                    "title": title,
                    "content_length": content_len,
                    "matched_signature": matched_signature,
                    "error": (
                        f"Page is a dead-end (matched: {matched_signature!r}). "
                        f"Likely paywall, JS shell, captcha, or DOI redirect. "
                        f"Do NOT retry this URL — pick a different result."
                    ),
                }

            return {
                "success": True,
                "url": url,
                "title": title,
                "content": content,
                "content_length": content_len,
                "headings": result.get("headings", []),
                "source": "direct_fetch",
                "insights": f"📖 Fetched {content_len} characters from {url}\n\nTitle: {title}\n\nPreview:\n{content[:500]}...",
            }
        return {
            "success": False,
            "url": url,
            "error": result.get("error", "Unknown error") if result else "Fetch returned nothing",
        }

    def _format_insights(self, query: str, results: List[Dict], sources: List[str]) -> str:
        """Format results into a readable insights string for AI consumption."""
        if not results:
            return f"No results found for '{query}'. Try rephrasing or using fetch_url() on a known URL."

        lines = [
            f"🔍 Knowledge Search: '{query}'",
            f"📚 Sources: {', '.join(sources)} | {len(results)} results\n",
        ]

        for i, r in enumerate(results[:8], 1):
            title = r.get("title", "Untitled")
            source = r.get("source", "unknown")
            url = r.get("url", "")
            content = r.get("content", r.get("snippet", ""))

            lines.append(f"{'─' * 50}")
            lines.append(f"[{i}] {title}")
            lines.append(f"    Source: {source} | URL: {url}")

            # Show content preview
            if content:
                preview = content[:400].replace("\n", "\n    ")
                lines.append(f"    {preview}")
                if len(content) > 400:
                    lines.append(f"    ... ({len(content)} chars total)")

            # Show extra metadata
            if r.get("authors"):
                lines.append(f"    Authors: {', '.join(r['authors'][:3])}")
            if r.get("published"):
                lines.append(f"    Published: {r['published']}")
            if r.get("journal"):
                lines.append(f"    Journal: {r['journal']}")

            lines.append("")

        lines.append(f"{'─' * 50}")
        lines.append(f"💡 To get full content from any result, use: fetch_url(\"<url>\")")

        return "\n".join(lines)


# ── Singleton ──
_router_instance: Optional[KnowledgeRouter] = None

def get_knowledge_router() -> KnowledgeRouter:
    """Get or create the global KnowledgeRouter singleton."""
    global _router_instance
    if _router_instance is None:
        _router_instance = KnowledgeRouter()
    return _router_instance


# ════════════════════════════════════════════════════════════════════
# CLI TESTING
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    router = KnowledgeRouter()

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = "graphene composite tensile strength"

    print(f"\n{'=' * 60}")
    print(f"  Knowledge Router Test: '{query}'")
    print(f"{'=' * 60}\n")

    # Detect sources
    sources = detect_query_sources(query)
    print(f"Detected sources: {sources}\n")

    # Search
    result = router.search(query, department="materials_science")
    print(result.get("insights", "No insights"))

    print(f"\n\nRaw results: {len(result.get('results', []))} items")
    print(f"Sources queried: {result.get('sources_queried', [])}")
    if result.get("errors"):
        print(f"Errors: {result['errors']}")
