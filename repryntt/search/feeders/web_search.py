#!/usr/bin/env python3
"""
Web Search Feeder - SAIGE AI Web Search and Content Extraction
Provides web search capabilities for AI model to gather information from the internet
Designed to run on host computer while edge device handles AI processing
"""

import json
import os
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import hashlib
import re
from urllib.parse import urlparse, urljoin, quote_plus

# Web scraping and content extraction
from bs4 import BeautifulSoup
try:
    import newspaper
    from newspaper import Article
    NEWSPAPER_AVAILABLE = True
except ImportError:
    NEWSPAPER_AVAILABLE = False
    print("Warning: newspaper3k not available, using basic extraction")

logger = logging.getLogger(__name__)

@dataclass
class SearchResult:
    """Represents a single search result"""
    title: str
    url: str
    snippet: str
    source: str
    search_rank: int
    timestamp: float

@dataclass
class ExtractedContent:
    """Represents extracted content from a web page"""
    url: str
    title: str
    content: str
    summary: str
    keywords: List[str]
    word_count: int
    extraction_success: bool
    extraction_method: str
    timestamp: float

class WebSearchFeeder:
    """
    Web search and content extraction system for SAIGE AI
    Runs on host computer to provide web search capabilities to edge device
    """

    def __init__(self, config_path: str = "config/web_search_feeder.json"):
        self.config = self._load_config(config_path)

        # Search engine configurations - DISABLED per API reduction
        # Keeping only approved APIs: arxiv, pubmed, github, mit, openlibrary
        self.search_engines = {
            # All external search APIs removed - replaced with approved services
        }

        # Rate limiting
        self.last_search_time = 0
        self.min_search_interval = 1.0  # seconds between searches

        # Content extraction settings
        self.max_content_length = 10000
        self.min_content_length = 200

        # Search history for deduplication
        self.search_history = set()

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "search_engines": ["duckduckgo_api", "bing"],
            "max_results_per_engine": 5,
            "max_total_results": 10,
            "content_extraction_timeout": 10,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "output_file": "data/web_search_results.json",
            "cache_dir": "data/web_search_cache",
            "enable_cache": True
        }

        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config

    def search_web(self, query: str, max_results: int = None) -> List[SearchResult]:
        """
        Perform web search across multiple sources
        DISABLED: All external APIs removed per API reduction policy
        Keeping only: arxiv, pubmed, github, mit, openlibrary
        """
        logger.info(f"Web search DISABLED for query '{query}' - external APIs removed")
        return []  # Return empty results

    def _search_engine(self, engine_name: str, query: str) -> List[SearchResult]:
        """Search a specific search engine"""
        engine = self.search_engines[engine_name]
        results = []

        try:
            headers = {
                'User-Agent': self.config["user_agent"],
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            }

            params = engine['params'].copy()
            params['q'] = query

            response = requests.get(
                engine['url'],
                params=params,
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                results = engine['parser'](response.text, engine_name)
            else:
                logger.warning(f"{engine_name} returned status {response.status_code}")
                logger.debug(f"Response content: {response.text[:500]}")

        except Exception as e:
            logger.error(f"Error searching {engine_name}: {e}")

        return results

    def _parse_duckduckgo_results(self, html: str, source: str) -> List[SearchResult]:
        """Parse DuckDuckGo search results"""
        results = []
        soup = BeautifulSoup(html, 'html.parser')

        # DuckDuckGo result structure
        result_divs = soup.find_all('div', class_='result')

        for i, div in enumerate(result_divs[:self.config["max_results_per_engine"]]):
            try:
                title_elem = div.find('a', class_='result__a')
                snippet_elem = div.find('a', class_='result__snippet')

                if title_elem and title_elem.get('href'):
                    title = title_elem.get_text().strip()
                    url = title_elem['href']

                    # Extract snippet
                    snippet = ""
                    if snippet_elem:
                        snippet = snippet_elem.get_text().strip()

                    # Clean URL (DuckDuckGo uses redirects)
                    if url.startswith('//duckduckgo.com/l/?uddg='):
                        url = url.split('uddg=')[1].split('&')[0]
                        url = requests.utils.unquote(url)

                    result = SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                        source=source,
                        search_rank=i + 1,
                        timestamp=time.time()
                    )
                    results.append(result)

            except Exception as e:
                logger.debug(f"Error parsing DuckDuckGo result: {e}")
                continue

        return results

    def _parse_bing_results(self, html: str, source: str) -> List[SearchResult]:
        """Parse Bing search results"""
        results = []
        soup = BeautifulSoup(html, 'html.parser')

        # Bing result structure
        result_divs = soup.find_all('li', class_='b_algo')

        for i, li in enumerate(result_divs[:self.config["max_results_per_engine"]]):
            try:
                title_elem = li.find('h2').find('a') if li.find('h2') else None
                snippet_elem = li.find('div', class_='b_caption').find('p') if li.find('div', class_='b_caption') else None

                if title_elem and title_elem.get('href'):
                    title = title_elem.get_text().strip()
                    url = title_elem['href']

                    snippet = ""
                    if snippet_elem:
                        snippet = snippet_elem.get_text().strip()

                    result = SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                        source=source,
                        search_rank=i + 1,
                        timestamp=time.time()
                    )
                    results.append(result)

            except Exception as e:
                logger.debug(f"Error parsing Bing result: {e}")
                continue

        return results

    def _search_wikipedia_direct(self, query: str) -> List[SearchResult]:
        """Search Wikipedia directly using their opensearch API"""
        results = []

        try:
            search_url = "https://en.wikipedia.org/w/api.php"
            params = {
                'action': 'opensearch',
                'search': query,
                'limit': self.config["max_results_per_engine"],
                'namespace': 0,
                'format': 'json'
            }

            headers = {'User-Agent': self.config["user_agent"]}
            response = requests.get(search_url, params=params, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                titles = data[1] if len(data) > 1 else []
                descriptions = data[2] if len(data) > 2 else []
                urls = data[3] if len(data) > 3 else []

                for i, (title, desc, url) in enumerate(zip(titles, descriptions, urls)):
                    result = SearchResult(
                        title=title,
                        url=url,
                        snippet=desc[:200] + "..." if len(desc) > 200 else desc,
                        source='wikipedia',
                        search_rank=i + 1,
                        timestamp=time.time()
                    )
                    results.append(result)

        except Exception as e:
            logger.error(f"Error searching Wikipedia: {e}")

        return results

    def _parse_wikipedia_results(self, json_response: str, source: str) -> List[SearchResult]:
        """Parse Wikipedia API results by searching for related pages"""
        results = []

        try:
            # For Wikipedia, we'll search using the opensearch API
            # Since the summary API requires exact titles, let's use search
            search_url = "https://en.wikipedia.org/w/api.php"
            params = {
                'action': 'opensearch',
                'search': '',  # This will be set in the search method
                'limit': self.config["max_results_per_engine"],
                'namespace': 0,
                'format': 'json'
            }

            # We need to modify the search method to handle this differently
            # For now, return empty results as Wikipedia search needs different handling
            return results

        except Exception as e:
            logger.debug(f"Error parsing Wikipedia result: {e}")

        return results

    def extract_content(self, url: str) -> Optional[ExtractedContent]:
        """
        Extract clean content from a web page
        Returns ExtractedContent object or None if extraction fails
        """
        try:
            # Check cache first
            if self.config["enable_cache"]:
                cached_content = self._get_cached_content(url)
                if cached_content:
                    return cached_content

            logger.info(f"Extracting content from: {url}")

            if NEWSPAPER_AVAILABLE:
                # Use newspaper3k for article extraction
                article = Article(url)
                article.download()
                article.parse()

                # Basic validation
                if not article.text or len(article.text.strip()) < self.min_content_length:
                    logger.warning(f"Content too short from {url}")
                    return None

                # Clean and truncate content
                content = article.text.strip()
                title = article.title or "No Title"
                extraction_method = 'newspaper3k'
            else:
                # Fallback to basic BeautifulSoup extraction
                content, title = self._extract_with_bs4(url)
                extraction_method = 'beautifulsoup4'

                if not content or len(content.strip()) < self.min_content_length:
                    logger.warning(f"Content too short from {url}")
                    return None

            # Clean and truncate content
            content = content.strip()
            if len(content) > self.max_content_length:
                content = content[:self.max_content_length] + "..."

            # Generate summary
            summary = self._generate_summary(content)

            # Extract keywords
            keywords = self._extract_keywords(content)

            extracted = ExtractedContent(
                url=url,
                title=title,
                content=content,
                summary=summary,
                keywords=keywords,
                word_count=len(content.split()),
                extraction_success=True,
                extraction_method=extraction_method,
                timestamp=time.time()
            )

            # Cache the result
            if self.config["enable_cache"]:
                self._cache_content(extracted)

            return extracted

        except Exception as e:
            logger.error(f"Error extracting content from {url}: {e}")

            # Return basic info even on failure
            return ExtractedContent(
                url=url,
                title="Extraction Failed",
                content="",
                summary="",
                keywords=[],
                word_count=0,
                extraction_success=False,
                extraction_method='failed',
                timestamp=time.time()
            )

    def _generate_summary(self, content: str, max_length: int = 200) -> str:
        """Generate a simple summary from content"""
        try:
            # Take first few sentences
            sentences = content.split('.')[:3]
            summary = '.'.join(sentences).strip()

            if len(summary) > max_length:
                summary = summary[:max_length] + "..."

            return summary

        except Exception as e:
            logger.debug(f"Error generating summary: {e}")
            return content[:max_length] + "..." if len(content) > max_length else content

    def _extract_keywords(self, content: str, max_keywords: int = 10) -> List[str]:
        """Extract basic keywords from content"""
        try:
            # Simple keyword extraction based on frequency
            words = re.findall(r'\b\w{4,}\b', content.lower())  # Words longer than 3 chars

            # Remove common stop words
            stop_words = {'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'an', 'a'}
            filtered_words = [word for word in words if word not in stop_words]

            # Get most frequent words
            from collections import Counter
            word_counts = Counter(filtered_words)
            keywords = [word for word, count in word_counts.most_common(max_keywords)]

            return keywords

        except Exception as e:
            logger.debug(f"Error extracting keywords: {e}")
            return []

    def _extract_with_bs4(self, url: str) -> Tuple[str, str]:
        """Extract content using BeautifulSoup as fallback"""
        try:
            # SSRF protection
            from repryntt.search.url_guard import validate_url
            url = validate_url(url)

            headers = {
                'User-Agent': self.config["user_agent"],
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }

            response = requests.get(url, headers=headers, timeout=self.config["content_extraction_timeout"])

            if response.status_code != 200:
                return "", "Failed to fetch"

            soup = BeautifulSoup(response.content, 'html.parser')

            # Remove script, style, nav, header, footer elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                element.decompose()

            # Try to get title
            title = ""
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.get_text().strip()

            # Try to find main content
            content_selectors = [
                'main',
                '[role="main"]',
                '.content',
                '.post-content',
                '.entry-content',
                'article',
                '.article-content',
                '#content',
                '.main-content'
            ]

            content_text = ""
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    content_text = content_elem.get_text(separator=' ', strip=True)
                    if len(content_text) > self.min_content_length:
                        break

            # If no specific content found, get all paragraphs
            if not content_text or len(content_text) < self.min_content_length:
                paragraphs = soup.find_all('p')
                content_text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

            # Clean up whitespace
            content_text = re.sub(r'\s+', ' ', content_text).strip()

            return content_text, title or "No Title"

        except Exception as e:
            logger.debug(f"Error in BS4 extraction: {e}")
            return "", "Extraction Error"

    def _get_cached_content(self, url: str) -> Optional[ExtractedContent]:
        """Get cached content if available and not expired"""
        try:
            cache_dir = Path(self.config["cache_dir"])
            cache_dir.mkdir(exist_ok=True)

            url_hash = hashlib.md5(url.encode()).hexdigest()
            cache_file = cache_dir / f"{url_hash}.json"

            if cache_file.exists():
                # Check if cache is less than 24 hours old
                if time.time() - cache_file.stat().st_mtime < 86400:  # 24 hours
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                        return ExtractedContent(**data)

            return None

        except Exception as e:
            logger.debug(f"Error reading cache: {e}")
            return None

    def _cache_content(self, content: ExtractedContent):
        """Cache extracted content"""
        try:
            cache_dir = Path(self.config["cache_dir"])
            cache_dir.mkdir(exist_ok=True)

            url_hash = hashlib.md5(content.url.encode()).hexdigest()
            cache_file = cache_dir / f"{url_hash}.json"

            with open(cache_file, 'w') as f:
                json.dump(asdict(content), f, indent=2)

        except Exception as e:
            logger.debug(f"Error caching content: {e}")

    def search_and_extract(self, query: str, max_results: int = 5) -> List[ExtractedContent]:
        """
        Complete workflow: search web and extract content from results
        DISABLED: All external APIs removed per API reduction policy
        """
        logger.info(f"Search and extract DISABLED for '{query}' - external APIs removed")
        return []  # Return empty results

    def save_results(self, query: str, search_results: List[SearchResult],
                    extracted_contents: List[ExtractedContent]):
        """Save search and extraction results to file"""
        try:
            result_data = {
                "timestamp": time.time(),
                "query": query,
                "search_results": [asdict(result) for result in search_results],
                "extracted_contents": [asdict(content) for content in extracted_contents],
                "metadata": {
                    "total_search_results": len(search_results),
                    "successful_extractions": len([c for c in extracted_contents if c.extraction_success]),
                    "total_words_extracted": sum(c.word_count for c in extracted_contents)
                }
            }

            output_file = self.config["output_file"]
            os.makedirs(os.path.dirname(output_file), exist_ok=True)

            with open(output_file, 'w') as f:
                json.dump(result_data, f, indent=2)

            logger.info(f"Saved results to {output_file}")

        except Exception as e:
            logger.error(f"Error saving results: {e}")

def main():
    """Main entry point for testing"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    feeder = WebSearchFeeder()

    if len(os.sys.argv) > 1:
        query = ' '.join(os.sys.argv[1:])
    else:
        query = "artificial intelligence latest developments"

    print(f"Searching for: {query}")

    # Perform search and extraction
    extracted_contents = feeder.search_and_extract(query, max_results=3)

    print(f"\nFound {len(extracted_contents)} results:")
    for i, content in enumerate(extracted_contents, 1):
        print(f"\n{i}. {content.title}")
        print(f"   URL: {content.url}")
        print(f"   Words: {content.word_count}")
        print(f"   Summary: {content.summary[:100]}...")
        print(f"   Keywords: {', '.join(content.keywords[:5])}")

    # Save results
    search_results = feeder.search_web(query, max_results=3)
    feeder.save_results(query, search_results, extracted_contents)

if __name__ == "__main__":
    main()
