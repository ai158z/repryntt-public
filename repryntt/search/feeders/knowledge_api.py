#!/usr/bin/env python3
"""
Knowledge API Feeder - SAIGE Knowledge Integration Pipeline
Aggregates information from multiple free commercial APIs to build comprehensive knowledge base
Supports all major knowledge domains: academic, news, reference, data, and media
"""

import json
import os
import time
import logging
import requests
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Any, Union
from dataclasses import dataclass, asdict
from collections import defaultdict
import threading
import queue
import re
from urllib.parse import urljoin, urlparse, quote
import xml.etree.ElementTree as ET

# Content extraction libraries
try:
    import fitz  # PyMuPDF for PDF processing
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    logging.warning("PyMuPDF not available - PDF extraction disabled")

try:
    from bs4 import BeautifulSoup
    import newspaper3k
    from newspaper import Article
    WEB_SCRAPING_AVAILABLE = True
except ImportError:
    WEB_SCRAPING_AVAILABLE = False
    logging.warning("Web scraping libraries not available - web content extraction disabled")

try:
    import nltk
    from nltk.tokenize import sent_tokenize, word_tokenize
    from nltk.corpus import stopwords
    TEXT_PROCESSING_AVAILABLE = True
except ImportError:
    TEXT_PROCESSING_AVAILABLE = False
    logging.warning("NLTK not available - advanced text processing disabled")

logger = logging.getLogger(__name__)

@dataclass
class APIResult:
    """Standardized result format for all APIs"""
    api_name: str
    query: str
    success: bool
    data: Dict[str, Any]
    metadata: Dict[str, Any]
    timestamp: float
    error_message: Optional[str] = None
    content_hash: Optional[str] = None

@dataclass
class ExtractedContent:
    """Extracted content from URLs, PDFs, or enhanced API data"""
    url: str
    content_type: str  # 'pdf', 'html', 'text', 'api_enhanced'
    raw_content: str
    cleaned_text: str
    word_count: int
    summary: str
    key_phrases: List[str]
    metadata: Dict[str, Any]
    extraction_timestamp: float
    success: bool
    error_message: Optional[str] = None

@dataclass
class KnowledgeEntry:
    """Unified knowledge entry from any API with extracted content"""
    id: str
    title: str
    content: str
    extracted_content: Optional[ExtractedContent]
    summary: str
    source: str
    category: str
    url: str
    timestamp: float
    relevance_score: float
    credibility_score: float
    tags: List[str]
    entities: List[Dict[str, str]]
    metadata: Dict[str, Any]

class RateLimiter:
    """Simple rate limiter for API calls"""

    def __init__(self, calls_per_minute: int = 60):
        self.calls_per_minute = calls_per_minute
        self.call_times = []
        self.lock = threading.Lock()

    def wait_if_needed(self):
        """Wait if we're exceeding rate limit"""
        with self.lock:
            current_time = time.time()
            # Remove calls older than 1 minute
            self.call_times = [t for t in self.call_times if current_time - t < 60]

            if len(self.call_times) >= self.calls_per_minute:
                # Calculate wait time
                oldest_call = min(self.call_times)
                wait_time = 60 - (current_time - oldest_call)
                if wait_time > 0:
                    time.sleep(wait_time)

            self.call_times.append(current_time)

class KnowledgeAPIFeeder:
    """
    Aggregates knowledge from multiple free commercial APIs
    """

    def __init__(self, config_path: str = "config/knowledge_api_feeder.json"):
        self.config = self._load_config(config_path)

        # Rate limiters for each API
        self.rate_limiters = {
            'wikipedia': RateLimiter(100),  # Very generous
            'openlibrary': RateLimiter(100),
            'dbpedia': RateLimiter(50),
            'pubmed': RateLimiter(10),  # NCBI limits
            'arxiv': RateLimiter(100),
            'semantic_scholar': RateLimiter(100),
            'newsapi': RateLimiter(50),  # Free tier limit
            'currents': RateLimiter(600),  # 600/day = ~25/min
            'dictionary': RateLimiter(100),
            'restcountries': RateLimiter(100),
            'opentdb': RateLimiter(100),
            'github': RateLimiter(10),  # GitHub limits
            'stackexchange': RateLimiter(100),
            'worldbank': RateLimiter(100),
            'nasa': RateLimiter(100),
            'omdb': RateLimiter(1000)  # 1000/day = ~42/min
        }

        # Session for connection reuse
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SAIGE-KnowledgeFeeder/1.0 (https://github.com/saige-project)'
        })

        # Knowledge base tracking
        self.processed_queries = set()  # Query hashes already processed
        self.knowledge_entries = []  # Recent knowledge entries
        self.api_stats = defaultdict(lambda: {'calls': 0, 'successes': 0, 'errors': 0})

        # Knowledge categories
        self.categories = {
            'academic': ['research', 'paper', 'study', 'academic', 'scholar', 'university'],
            'reference': ['definition', 'encyclopedia', 'dictionary', 'book', 'author'],
            'news': ['news', 'current', 'breaking', 'headline', 'article'],
            'data': ['statistics', 'data', 'country', 'development', 'economic'],
            'media': ['movie', 'tv', 'film', 'entertainment', 'actor'],
            'technical': ['code', 'programming', 'api', 'software', 'github'],
            'science': ['space', 'nasa', 'astronomy', 'earth', 'climate']
        }

        # Content extraction settings
        self.content_cache = {}  # URL -> ExtractedContent cache
        self.extraction_stats = defaultdict(lambda: {'attempts': 0, 'successes': 0, 'failures': 0})

        # Initialize NLTK if available
        if TEXT_PROCESSING_AVAILABLE:
            try:
                nltk.data.find('tokenizers/punkt')
            except LookupError:
                try:
                    nltk.download('punkt', quiet=True)
                except:
                    pass
            try:
                nltk.data.find('corpora/stopwords')
            except LookupError:
                try:
                    nltk.download('stopwords', quiet=True)
                except:
                    pass

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "apis": {
                "wikipedia": {
                    "enabled": False,  # DISABLED: Not in approved APIs
                    "base_url": "https://en.wikipedia.org/api/rest_v1/",
                    "endpoints": {
                        "summary": "page/summary/{title}",
                        "search": "page/search",
                        "random": "page/random/summary"
                    }
                },
                "openlibrary": {
                    "enabled": True,
                    "base_url": "https://openlibrary.org/",
                    "endpoints": {
                        "search": "search.json",
                        "works": "works/{olid}.json",
                        "authors": "authors/{olid}.json"
                    }
                },
                "dbpedia": {
                    "enabled": True,
                    "sparql_endpoint": "https://dbpedia.org/sparql",
                    "default_graph": "http://dbpedia.org"
                },
                "pubmed": {
                    "enabled": True,
                    "base_url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/",
                    "endpoints": {
                        "search": "esearch.fcgi",
                        "summary": "esummary.fcgi",
                        "fetch": "efetch.fcgi"
                    },
                    "api_key": ""  # Optional: increases rate limits
                },
                "arxiv": {
                    "enabled": True,
                    "base_url": "http://export.arxiv.org/api/query",
                    "max_results": 10
                },
                "semantic_scholar": {
                    "enabled": False,  # DISABLED: Not in approved APIs
                    "base_url": "https://api.semanticscholar.org/",
                    "version": "v1"
                },
                "github": {
                    "enabled": True,  # APPROVED: GitHub API
                    "base_url": "https://api.github.com/",
                    "endpoints": {
                        "search_repos": "search/repositories",
                        "search_code": "search/code"
                    }
                },
                "mit_ocw": {
                    "enabled": True,  # APPROVED: MIT OpenCourseWare
                    "base_url": "https://ocw.mit.edu/",
                    "endpoints": {
                        "search": "search/",
                        "courses": "courses/"
                    }
                },
                "newsapi": {
                    "enabled": False,  # Requires API key
                    "base_url": "https://newsapi.org/v2/",
                    "api_key": "",
                    "endpoints": {
                        "top_headlines": "top-headlines",
                        "everything": "everything"
                    }
                },
                "currents": {
                    "enabled": True,
                    "base_url": "https://api.currentsapi.services/v1/",
                    "endpoints": {
                        "latest": "latest-news",
                        "search": "search"
                    },
                    "api_key": ""  # Optional for higher limits
                },
                "dictionary": {
                    "enabled": True,
                    "base_url": "https://api.dictionaryapi.dev/api/v2/entries/en/"
                },
                "restcountries": {
                    "enabled": True,
                    "base_url": "https://restcountries.com/v3.1/"
                },
                "opentdb": {
                    "enabled": True,
                    "base_url": "https://opentdb.com/api.php"
                },
                "github": {
                    "enabled": True,
                    "base_url": "https://api.github.com/search/",
                    "token": ""  # Optional: increases rate limits
                },
                "stackexchange": {
                    "enabled": True,
                    "base_url": "https://api.stackexchange.com/2.3/"
                },
                "worldbank": {
                    "enabled": True,
                    "base_url": "https://api.worldbank.org/v2/"
                },
                "nasa": {
                    "enabled": True,
                    "base_url": "https://api.nasa.gov/",
                    "api_key": "DEMO_KEY"  # Free demo key
                },
                "omdb": {
                    "enabled": True,
                    "base_url": "http://www.omdbapi.com/",
                    "api_key": ""  # Free tier available
                }
            },
            "processing": {
                "max_results_per_api": 10,
                "min_relevance_threshold": 0.1,
                "cache_results": True,
                "cache_ttl_hours": 24,
                "parallel_requests": True,
                "timeout_seconds": 30
            },
            "output": {
                "knowledge_data": "data/knowledge_entries.json",
                "api_stats": "data/api_stats.json",
                "search_results": "data/search_results.json"
            }
        }

        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config

    # Content Extraction Methods

    def extract_content_from_url(self, url: str, content_type_hint: str = None) -> ExtractedContent:
        """Extract content from URL (PDF, web page, or direct text)"""
        if url in self.content_cache:
            return self.content_cache[url]

        self.extraction_stats['total']['attempts'] += 1

        try:
            # Determine content type
            parsed_url = urlparse(url)
            if content_type_hint:
                content_type = content_type_hint
            elif parsed_url.path.lower().endswith('.pdf'):
                content_type = 'pdf'
            else:
                content_type = 'html'

            # Extract based on type
            if content_type == 'pdf' and PDF_AVAILABLE:
                extracted = self._extract_pdf_content(url)
            elif content_type == 'html' and WEB_SCRAPING_AVAILABLE:
                extracted = self._extract_web_content(url)
            else:
                # Fallback to basic HTTP request
                extracted = self._extract_basic_content(url)

            if extracted and extracted.success:
                self.extraction_stats['total']['successes'] += 1
                self.content_cache[url] = extracted
            else:
                self.extraction_stats['total']['failures'] += 1

            return extracted

        except Exception as e:
            self.extraction_stats['total']['failures'] += 1
            return ExtractedContent(
                url=url,
                content_type='error',
                raw_content='',
                cleaned_text='',
                word_count=0,
                summary='',
                key_phrases=[],
                metadata={},
                extraction_timestamp=time.time(),
                success=False,
                error_message=str(e)
            )

    def _extract_pdf_content(self, url: str) -> ExtractedContent:
        """Extract text content from PDF URL"""
        try:
            # Download PDF
            response = self.session.get(url, timeout=30, stream=True)
            response.raise_for_status()

            # Save temporarily and extract text
            pdf_data = response.content

            # Extract text using PyMuPDF
            doc = fitz.open(stream=pdf_data, filetype="pdf")
            text = ""
            metadata = {
                'page_count': len(doc),
                'title': doc.metadata.get('title', ''),
                'author': doc.metadata.get('author', ''),
                'subject': doc.metadata.get('subject', ''),
                'creator': doc.metadata.get('creator', '')
            }

            # Extract text from all pages
            for page in doc:
                text += page.get_text() + "\n"

            doc.close()

            # Clean and process text
            cleaned_text = self._clean_extracted_text(text)
            word_count = len(cleaned_text.split())
            summary = self._generate_content_summary(cleaned_text)
            key_phrases = self._extract_key_phrases(cleaned_text)

            return ExtractedContent(
                url=url,
                content_type='pdf',
                raw_content=text,
                cleaned_text=cleaned_text,
                word_count=word_count,
                summary=summary,
                key_phrases=key_phrases,
                metadata=metadata,
                extraction_timestamp=time.time(),
                success=True
            )

        except Exception as e:
            return ExtractedContent(
                url=url,
                content_type='pdf',
                raw_content='',
                cleaned_text='',
                word_count=0,
                summary='',
                key_phrases=[],
                metadata={},
                extraction_timestamp=time.time(),
                success=False,
                error_message=str(e)
            )

    def _extract_web_content(self, url: str) -> ExtractedContent:
        """Extract content from web page using newspaper3k and BeautifulSoup"""
        try:
            # Try newspaper3k first (better for articles)
            article = Article(url)
            article.download()
            article.parse()

            if article.text and len(article.text) > 100:
                text = article.text
                metadata = {
                    'title': article.title,
                    'authors': article.authors,
                    'publish_date': str(article.publish_date) if article.publish_date else '',
                    'top_image': article.top_image,
                    'movies': article.movies,
                    'keywords': article.keywords,
                    'summary': article.summary
                }
            else:
                # Fallback to BeautifulSoup
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')

                # Remove unwanted elements
                for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'advertisement']):
                    element.decompose()

                # Try to find main content
                content_selectors = ['article', '.article-content', '.content', '.story', '.post-content', 'main']
                text = ""

                for selector in content_selectors:
                    content_elem = soup.select_one(selector)
                    if content_elem:
                        text = content_elem.get_text(separator=' ', strip=True)
                        break

                if not text:
                    # Last resort: get all paragraph text
                    paragraphs = soup.find_all('p')
                    text = ' '.join([p.get_text(strip=True) for p in paragraphs])

                metadata = {
                    'title': soup.find('title').get_text() if soup.find('title') else '',
                    'description': soup.find('meta', attrs={'name': 'description'}).get('content') if soup.find('meta', attrs={'name': 'description'}) else ''
                }

            # Clean and process text
            cleaned_text = self._clean_extracted_text(text)
            word_count = len(cleaned_text.split())
            summary = self._generate_content_summary(cleaned_text)
            key_phrases = self._extract_key_phrases(cleaned_text)

            return ExtractedContent(
                url=url,
                content_type='html',
                raw_content=text,
                cleaned_text=cleaned_text,
                word_count=word_count,
                summary=summary,
                key_phrases=key_phrases,
                metadata=metadata,
                extraction_timestamp=time.time(),
                success=True
            )

        except Exception as e:
            return ExtractedContent(
                url=url,
                content_type='html',
                raw_content='',
                cleaned_text='',
                word_count=0,
                summary='',
                key_phrases=[],
                metadata={},
                extraction_timestamp=time.time(),
                success=False,
                error_message=str(e)
            )

    def _extract_basic_content(self, url: str) -> ExtractedContent:
        """Basic content extraction via HTTP request"""
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            content_type = response.headers.get('content-type', '').lower()

            if 'pdf' in content_type and PDF_AVAILABLE:
                return self._extract_pdf_content(url)
            elif 'text' in content_type:
                text = response.text
                cleaned_text = self._clean_extracted_text(text)
                word_count = len(cleaned_text.split())
                summary = self._generate_content_summary(cleaned_text)
                key_phrases = self._extract_key_phrases(cleaned_text)

                return ExtractedContent(
                    url=url,
                    content_type='text',
                    raw_content=text,
                    cleaned_text=cleaned_text,
                    word_count=word_count,
                    summary=summary,
                    key_phrases=key_phrases,
                    metadata={'content_type': content_type},
                    extraction_timestamp=time.time(),
                    success=True
                )
            else:
                return ExtractedContent(
                    url=url,
                    content_type='binary',
                    raw_content='',
                    cleaned_text='',
                    word_count=0,
                    summary='Binary content not extractable',
                    key_phrases=[],
                    metadata={'content_type': content_type},
                    extraction_timestamp=time.time(),
                    success=False,
                    error_message='Unsupported content type'
                )

        except Exception as e:
            return ExtractedContent(
                url=url,
                content_type='error',
                raw_content='',
                cleaned_text='',
                word_count=0,
                summary='',
                key_phrases=[],
                metadata={},
                extraction_timestamp=time.time(),
                success=False,
                error_message=str(e)
            )

    def _clean_extracted_text(self, text: str) -> str:
        """Clean and normalize extracted text"""
        if not text:
            return ""

        # Remove extra whitespace
        text = re.sub(r'\s+', ' ', text)

        # Remove non-printable characters
        text = re.sub(r'[^\x20-\x7E\n]', '', text)

        # Fix common encoding issues
        text = text.replace('\x00', '').replace('\ufeff', '')

        # Remove excessive newlines
        text = re.sub(r'\n\s*\n', '\n\n', text)

        return text.strip()

    def _generate_content_summary(self, text: str, max_sentences: int = 3) -> str:
        """Generate summary from extracted text"""
        if not text or len(text) < 100:
            return text

        if not TEXT_PROCESSING_AVAILABLE:
            # Simple extractive summary
            words = text.split()
            if len(words) <= 50:
                return text
            return ' '.join(words[:50]) + '...'

        try:
            # Use NLTK for sentence tokenization
            sentences = sent_tokenize(text)

            if len(sentences) <= max_sentences:
                return text

            # Simple position-based summarization
            selected_sentences = []

            # First sentence often contains main topic
            if sentences:
                selected_sentences.append(sentences[0])

            # Middle sentences for content
            if len(sentences) > 2:
                mid_start = max(1, len(sentences) // 3)
                mid_end = min(len(sentences) - 1, len(sentences) * 2 // 3)
                selected_sentences.extend(sentences[mid_start:mid_end][:max_sentences-1])

            # Last sentence sometimes has conclusion
            if len(sentences) > 1 and len(selected_sentences) < max_sentences:
                selected_sentences.append(sentences[-1])

            return ' '.join(selected_sentences[:max_sentences])

        except Exception:
            # Fallback
            return text[:500] + '...' if len(text) > 500 else text

    def _extract_key_phrases(self, text: str, max_phrases: int = 10) -> List[str]:
        """Extract key phrases from text"""
        if not text or not TEXT_PROCESSING_AVAILABLE:
            return []

        try:
            # Simple noun phrase extraction
            words = word_tokenize(text.lower())
            if TEXT_PROCESSING_AVAILABLE:
                stop_words = set(stopwords.words('english'))
                words = [word for word in words if word.isalnum() and word not in stop_words and len(word) > 3]

            # Simple frequency-based extraction
            from collections import Counter
            word_freq = Counter(words)

            # Get most common words as key phrases
            key_phrases = [word for word, freq in word_freq.most_common(max_phrases) if freq > 1]

            return key_phrases[:max_phrases]

        except Exception:
            return []

    def enhance_api_result_with_content(self, api_result: APIResult) -> APIResult:
        """Enhance API result with extracted content from URLs"""
        if not api_result.success or not api_result.data:
            return api_result

        enhanced_data = api_result.data.copy()

        # Extract content from URLs in the results
        if 'results' in enhanced_data:
            for result in enhanced_data['results']:
                url = result.get('url') or result.get('pdf_url') or result.get('abstract_url')
                if url:
                    extracted = self.extract_content_from_url(url)
                    if extracted and extracted.success:
                        result['extracted_content'] = asdict(extracted)

        return APIResult(
            api_name=api_result.api_name,
            query=api_result.query,
            success=api_result.success,
            data=enhanced_data,
            metadata=api_result.metadata,
            timestamp=api_result.timestamp,
            error_message=api_result.error_message,
            content_hash=api_result.content_hash
        )

    def _make_request(self, api_name: str, url: str, params: Dict = None,
                     headers: Dict = None, method: str = 'GET') -> Optional[requests.Response]:
        """Make HTTP request with rate limiting and error handling"""
        try:
            # Apply rate limiting
            if api_name in self.rate_limiters:
                self.rate_limiters[api_name].wait_if_needed()

            # Update stats
            self.api_stats[api_name]['calls'] += 1

            # Prepare request
            request_headers = dict(self.session.headers)
            if headers:
                request_headers.update(headers)

            # Make request
            if method.upper() == 'GET':
                response = self.session.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=self.config["processing"]["timeout_seconds"]
                )
            elif method.upper() == 'POST':
                response = self.session.post(
                    url,
                    data=params,
                    headers=request_headers,
                    timeout=self.config["processing"]["timeout_seconds"]
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Check response
            response.raise_for_status()

            # Update success stats
            self.api_stats[api_name]['successes'] += 1

            return response

        except requests.RequestException as e:
            self.api_stats[api_name]['errors'] += 1
            logger.error(f"Request error for {api_name}: {e}")
            return None
        except Exception as e:
            self.api_stats[api_name]['errors'] += 1
            logger.error(f"Unexpected error for {api_name}: {e}")
            return None

    def _standardize_result(self, api_name: str, query: str, data: Dict,
                           success: bool = True, error_msg: str = None) -> APIResult:
        """Create standardized API result"""
        content_hash = None
        if success and data:
            content_str = json.dumps(data, sort_keys=True)
            content_hash = hashlib.md5(content_str.encode()).hexdigest()

        return APIResult(
            api_name=api_name,
            query=query,
            success=success,
            data=data,
            metadata={
                'api_version': self.config["apis"][api_name].get('version', '1.0'),
                'request_timestamp': time.time()
            },
            timestamp=time.time(),
            error_message=error_msg,
            content_hash=content_hash
        )

    # API Implementations

    def search_wikipedia(self, query: str, limit: int = 5) -> APIResult:
        """Search Wikipedia for articles"""
        if not self.config["apis"]["wikipedia"]["enabled"]:
            return self._standardize_result("wikipedia", query, {}, False, "API disabled")

        base_url = self.config["apis"]["wikipedia"]["base_url"]
        search_url = f"{base_url}page/search"

        response = self._make_request("wikipedia", search_url, params={
            'q': query,
            'limit': min(limit, 20)
        })

        if not response:
            return self._standardize_result("wikipedia", query, {}, False, "Request failed")

        try:
            search_data = response.json()
            results = []

            for page in search_data.get('pages', [])[:limit]:
                # Get full summary for each result
                summary_url = f"{base_url}page/summary/{quote(page['title'])}"
                summary_response = self._make_request("wikipedia", summary_url)

                if summary_response:
                    summary_data = summary_response.json()
                    results.append({
                        'title': summary_data.get('title', ''),
                        'url': summary_data.get('content_urls', {}).get('desktop', {}).get('page', ''),
                        'summary': summary_data.get('extract', ''),
                        'categories': summary_data.get('categories', []),
                        'thumbnail': summary_data.get('thumbnail', {}).get('source', '')
                    })

            return self._standardize_result("wikipedia", query, {
                'query': query,
                'total_results': len(results),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("wikipedia", query, {}, False, str(e))

    def search_openlibrary(self, query: str, limit: int = 5) -> APIResult:
        """Search Open Library for books"""
        if not self.config["apis"]["openlibrary"]["enabled"]:
            return self._standardize_result("openlibrary", query, {}, False, "API disabled")

        base_url = self.config["apis"]["openlibrary"]["base_url"]
        search_url = f"{base_url}search.json"

        response = self._make_request("openlibrary", search_url, params={
            'q': query,
            'limit': limit,
            'fields': 'key,title,author_name,first_publish_year,cover_i,subject'
        })

        if not response:
            return self._standardize_result("openlibrary", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for doc in data.get('docs', [])[:limit]:
                results.append({
                    'title': doc.get('title', ''),
                    'authors': doc.get('author_name', []),
                    'key': doc.get('key', ''),
                    'first_publish_year': doc.get('first_publish_year'),
                    'subjects': doc.get('subject', [])[:5],  # Limit subjects
                    'cover_url': f"https://covers.openlibrary.org/b/id/{doc.get('cover_i', '')}-M.jpg" if doc.get('cover_i') else None
                })

            return self._standardize_result("openlibrary", query, {
                'query': query,
                'total_results': data.get('num_found', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("openlibrary", query, {}, False, str(e))

    def search_dbpedia(self, query: str, limit: int = 5) -> APIResult:
        """Query DBpedia SPARQL endpoint"""
        if not self.config["apis"]["dbpedia"]["enabled"]:
            return self._standardize_result("dbpedia", query, {}, False, "API disabled")

        sparql_endpoint = self.config["apis"]["dbpedia"]["sparql_endpoint"]

        # Create SPARQL query for general search
        sparql_query = f"""
        PREFIX dbo: <http://dbpedia.org/ontology/>
        PREFIX dbp: <http://dbpedia.org/property/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

        SELECT DISTINCT ?resource ?label ?abstract ?type WHERE {{
            ?resource rdfs:label ?label ;
                     dbo:abstract ?abstract ;
                     rdf:type ?type .
            ?label bif:contains "{query}" .
            FILTER(LANG(?label) = "en")
            FILTER(LANG(?abstract) = "en")
            FILTER(?type IN (dbo:Person, dbo:Place, dbo:Organisation, dbo:Work))
        }}
        LIMIT {limit}
        """

        response = self._make_request("dbpedia", sparql_endpoint,
                                    params={'query': sparql_query, 'format': 'json'})

        if not response:
            return self._standardize_result("dbpedia", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for binding in data.get('results', {}).get('bindings', []):
                results.append({
                    'resource': binding.get('resource', {}).get('value', ''),
                    'label': binding.get('label', {}).get('value', ''),
                    'abstract': binding.get('abstract', {}).get('value', ''),
                    'type': binding.get('type', {}).get('value', '').split('#')[-1]
                })

            return self._standardize_result("dbpedia", query, {
                'query': query,
                'results': results
            })

        except Exception as e:
            return self._standardize_result("dbpedia", query, {}, False, str(e))

    def search_pubmed(self, query: str, limit: int = 5) -> APIResult:
        """Search PubMed for medical literature"""
        if not self.config["apis"]["pubmed"]["enabled"]:
            return self._standardize_result("pubmed", query, {}, False, "API disabled")

        base_url = self.config["apis"]["pubmed"]["base_url"]
        api_key = self.config["apis"]["pubmed"]["api_key"]

        # First, search for article IDs
        search_url = f"{base_url}esearch.fcgi"
        search_params = {
            'db': 'pubmed',
            'term': query,
            'retmax': limit,
            'retmode': 'json',
            'sort': 'relevance'
        }
        if api_key:
            search_params['api_key'] = api_key

        search_response = self._make_request("pubmed", search_url, search_params)

        if not search_response:
            return self._standardize_result("pubmed", query, {}, False, "Search request failed")

        try:
            search_data = search_response.json()
            pmids = search_data.get('esearchresult', {}).get('idlist', [])

            if not pmids:
                return self._standardize_result("pubmed", query, {'results': []})

            # Get summaries for the found articles
            summary_url = f"{base_url}esummary.fcgi"
            summary_params = {
                'db': 'pubmed',
                'id': ','.join(pmids),
                'retmode': 'json'
            }
            if api_key:
                summary_params['api_key'] = api_key

            summary_response = self._make_request("pubmed", summary_url, summary_params)

            if not summary_response:
                return self._standardize_result("pubmed", query, {}, False, "Summary request failed")

            summary_data = summary_response.json()
            results = []

            for pmid in pmids:
                article = summary_data.get('result', {}).get(pmid, {})
                results.append({
                    'pmid': pmid,
                    'title': article.get('title', ''),
                    'authors': [author.get('name', '') for author in article.get('authors', [])],
                    'journal': article.get('source', ''),
                    'pubdate': article.get('pubdate', ''),
                    'abstract': '',  # Would need efetch for full abstract
                    'doi': article.get('elocationid', '')
                })

            return self._standardize_result("pubmed", query, {
                'query': query,
                'total_results': search_data.get('esearchresult', {}).get('count', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("pubmed", query, {}, False, str(e))

    def get_pubmed_full_abstract(self, pmid: str) -> str:
        """Get full abstract text for a PubMed ID"""
        if not self.config["apis"]["pubmed"]["enabled"]:
            return ""

        base_url = self.config["apis"]["pubmed"]["base_url"]
        api_key = self.config["apis"]["pubmed"]["api_key"]

        # Get full abstract using efetch
        fetch_url = f"{base_url}efetch.fcgi"
        params = {
            'db': 'pubmed',
            'id': pmid,
            'retmode': 'xml'
        }
        if api_key:
            params['api_key'] = api_key

        response = self._make_request("pubmed", fetch_url, params=params)

        if not response:
            return ""

        try:
            # Parse XML to extract abstract
            root = ET.fromstring(response.content)
            ns = {'': 'http://www.ncbi.nlm.nih.gov/pubmed'}

            # Find abstract text
            abstract_texts = []
            for abstract in root.findall('.//AbstractText'):
                if abstract.text:
                    abstract_texts.append(abstract.text)

            return ' '.join(abstract_texts)

        except Exception as e:
            logger.debug(f"Failed to parse PubMed abstract for {pmid}: {e}")
            return ""

    def search_arxiv(self, query: str, limit: int = 5, include_abstracts: bool = True) -> APIResult:
        """Search arXiv for research papers with optional full abstract extraction"""
        if not self.config["apis"]["arxiv"]["enabled"]:
            return self._standardize_result("arxiv", query, {}, False, "API disabled")

        base_url = self.config["apis"]["arxiv"]["base_url"]

        response = self._make_request("arxiv", base_url, params={
            'search_query': f'all:{query}',
            'start': 0,
            'max_results': min(limit, 10),
            'sortBy': 'relevance',
            'sortOrder': 'descending'
        })

        if not response:
            return self._standardize_result("arxiv", query, {}, False, "Request failed")

        try:
            # Parse XML response
            root = ET.fromstring(response.content)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}

            results = []
            for entry in root.findall('atom:entry', ns)[:limit]:
                # Extract title
                title_elem = entry.find('atom:title', ns)
                title = title_elem.text.strip() if title_elem is not None else ''

                # Extract authors
                authors = []
                for author in entry.findall('atom:author', ns):
                    name_elem = author.find('atom:name', ns)
                    if name_elem is not None:
                        authors.append(name_elem.text.strip())

                # Extract summary (abstract)
                summary_elem = entry.find('atom:summary', ns)
                summary = summary_elem.text.strip() if summary_elem is not None else ''

                # Extract ID and PDF link
                id_elem = entry.find('atom:id', ns)
                paper_id = id_elem.text.split('/')[-1] if id_elem is not None else ''

                # Extract published date
                published_elem = entry.find('atom:published', ns)
                published = published_elem.text if published_elem is not None else ''

                # Extract categories
                categories = []
                for category in entry.findall('atom:category', ns):
                    if category.get('term'):
                        categories.append(category.get('term'))

                paper_data = {
                    'title': title,
                    'authors': authors,
                    'summary': summary,
                    'paper_id': paper_id,
                    'pdf_url': f'https://arxiv.org/pdf/{paper_id}.pdf',
                    'abstract_url': f'https://arxiv.org/abs/{paper_id}',
                    'html_url': f'https://arxiv.org/html/{paper_id}',
                    'published': published,
                    'categories': categories,
                    'word_count': len(summary.split()) if summary else 0
                }

                # Optionally extract full PDF content (expensive, so optional)
                if include_abstracts and summary and len(summary) > 100:
                    paper_data['full_abstract_available'] = True
                else:
                    paper_data['full_abstract_available'] = False

                results.append(paper_data)

            return self._standardize_result("arxiv", query, {
                'query': query,
                'total_results': len(results),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("arxiv", query, {}, False, str(e))

    def search_semantic_scholar(self, query: str, limit: int = 5) -> APIResult:
        """Search Semantic Scholar for academic papers"""
        if not self.config["apis"]["semantic_scholar"]["enabled"]:
            return self._standardize_result("semantic_scholar", query, {}, False, "API disabled")

        base_url = self.config["apis"]["semantic_scholar"]["base_url"]
        version = self.config["apis"]["semantic_scholar"]["version"]
        search_url = f"{base_url}{version}/paper/search"

        response = self._make_request("semantic_scholar", search_url, params={
            'query': query,
            'limit': min(limit, 100),
            'fields': 'title,abstract,authors,year,venue,citationCount,influentialCitationCount'
        })

        if not response:
            return self._standardize_result("semantic_scholar", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for paper in data.get('data', [])[:limit]:
                results.append({
                    'title': paper.get('title', ''),
                    'abstract': paper.get('abstract', ''),
                    'authors': [author.get('name', '') for author in paper.get('authors', [])],
                    'year': paper.get('year'),
                    'venue': paper.get('venue', ''),
                    'citation_count': paper.get('citationCount', 0),
                    'influential_citations': paper.get('influentialCitationCount', 0),
                    'paper_id': paper.get('paperId', '')
                })

            return self._standardize_result("semantic_scholar", query, {
                'query': query,
                'total_results': data.get('total', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("semantic_scholar", query, {}, False, str(e))

    def search_newsapi(self, query: str, limit: int = 5) -> APIResult:
        """Search NewsAPI for news articles"""
        if not self.config["apis"]["newsapi"]["enabled"]:
            return self._standardize_result("newsapi", query, {}, False, "API disabled")

        api_key = self.config["apis"]["newsapi"]["api_key"]
        if not api_key:
            return self._standardize_result("newsapi", query, {}, False, "API key required")

        base_url = self.config["apis"]["newsapi"]["base_url"]
        search_url = f"{base_url}everything"

        response = self._make_request("newsapi", search_url, params={
            'q': query,
            'apiKey': api_key,
            'pageSize': min(limit, 100),
            'sortBy': 'relevancy',
            'language': 'en'
        })

        if not response:
            return self._standardize_result("newsapi", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for article in data.get('articles', [])[:limit]:
                results.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('source', {}).get('name', ''),
                    'published_at': article.get('publishedAt', ''),
                    'author': article.get('author', ''),
                    'url_to_image': article.get('urlToImage', '')
                })

            return self._standardize_result("newsapi", query, {
                'query': query,
                'total_results': data.get('totalResults', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("newsapi", query, {}, False, str(e))

    def search_currents(self, query: str = "", limit: int = 5) -> APIResult:
        """Search Currents API for latest news"""
        if not self.config["apis"]["currents"]["enabled"]:
            return self._standardize_result("currents", query, {}, False, "API disabled")

        base_url = self.config["apis"]["currents"]["base_url"]
        api_key = self.config["apis"]["currents"]["api_key"]

        if query:
            search_url = f"{base_url}search"
            params = {'keywords': query, 'apiKey': api_key} if api_key else {'keywords': query}
        else:
            search_url = f"{base_url}latest-news"
            params = {'apiKey': api_key} if api_key else {}

        params['page_size'] = min(limit, 200)

        response = self._make_request("currents", search_url, params=params)

        if not response:
            return self._standardize_result("currents", query or "latest", {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for article in data.get('news', [])[:limit]:
                results.append({
                    'title': article.get('title', ''),
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'source': article.get('author', ''),
                    'published': article.get('published', ''),
                    'category': article.get('category', []),
                    'image': article.get('image', 'none')
                })

            return self._standardize_result("currents", query or "latest", {
                'query': query or "latest",
                'total_results': len(results),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("currents", query or "latest", {}, False, str(e))

    def search_dictionary(self, word: str) -> APIResult:
        """Get word definition from Free Dictionary API"""
        if not self.config["apis"]["dictionary"]["enabled"]:
            return self._standardize_result("dictionary", word, {}, False, "API disabled")

        base_url = self.config["apis"]["dictionary"]["base_url"]
        url = f"{base_url}{quote(word)}"

        response = self._make_request("dictionary", url)

        if not response:
            return self._standardize_result("dictionary", word, {}, False, "Request failed")

        try:
            data = response.json()
            if not data:
                return self._standardize_result("dictionary", word, {}, False, "Word not found")

            # Take first definition
            word_data = data[0]
            definitions = []

            for meaning in word_data.get('meanings', []):
                part_of_speech = meaning.get('partOfSpeech', '')
                for definition in meaning.get('definitions', []):
                    definitions.append({
                        'part_of_speech': part_of_speech,
                        'definition': definition.get('definition', ''),
                        'example': definition.get('example', ''),
                        'synonyms': definition.get('synonyms', []),
                        'antonyms': definition.get('antonyms', [])
                    })

            result = {
                'word': word_data.get('word', ''),
                'phonetic': word_data.get('phonetic', ''),
                'phonetics': word_data.get('phonetics', []),
                'meanings': definitions,
                'license': word_data.get('license', {}),
                'sourceUrls': word_data.get('sourceUrls', [])
            }

            return self._standardize_result("dictionary", word, result)

        except Exception as e:
            return self._standardize_result("dictionary", word, {}, False, str(e))

    def search_restcountries(self, query: str) -> APIResult:
        """Search REST Countries API"""
        if not self.config["apis"]["restcountries"]["enabled"]:
            return self._standardize_result("restcountries", query, {}, False, "API disabled")

        base_url = self.config["apis"]["restcountries"]["base_url"]

        # Try different search types
        search_types = [
            f"name/{quote(query)}",  # By name
            f"capital/{quote(query)}",  # By capital
            f"alpha/{quote(query)}" if len(query) == 2 else None,  # By code
        ]

        for search_type in search_types:
            if not search_type:
                continue

            url = f"{base_url}{search_type}"
            response = self._make_request("restcountries", url)

            if response and response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, list) and data:
                        country = data[0]  # Take first result

                        result = {
                            'name': country.get('name', {}).get('common', ''),
                            'official_name': country.get('name', {}).get('official', ''),
                            'capital': country.get('capital', [None])[0],
                            'region': country.get('region', ''),
                            'subregion': country.get('subregion', ''),
                            'population': country.get('population', 0),
                            'area': country.get('area', 0),
                            'languages': list(country.get('languages', {}).values()),
                            'currencies': list(country.get('currencies', {}).keys()),
                            'flag_url': country.get('flags', {}).get('png', ''),
                            'coat_of_arms': country.get('coatOfArms', {}).get('png', ''),
                            'maps': country.get('maps', {})
                        }

                        return self._standardize_result("restcountries", query, result)

                except Exception as e:
                    continue

        return self._standardize_result("restcountries", query, {}, False, "Country not found")

    def search_opentdb(self, query: str = "", limit: int = 5) -> APIResult:
        """Get trivia questions from Open Trivia Database"""
        if not self.config["apis"]["opentdb"]["enabled"]:
            return self._standardize_result("opentdb", query or "random", {}, False, "API disabled")

        base_url = self.config["apis"]["opentdb"]["base_url"]

        params = {
            'amount': min(limit, 50),
            'type': 'multiple'  # Multiple choice questions
        }

        if query:
            # Try to find category by searching
            # This is a simplified approach - in practice you'd want to cache categories
            category_map = {
                'science': 17, 'history': 23, 'geography': 22, 'sports': 21,
                'entertainment': 11, 'art': 25, 'animals': 27, 'politics': 24
            }

            query_lower = query.lower()
            for key, cat_id in category_map.items():
                if key in query_lower:
                    params['category'] = cat_id
                    break

        response = self._make_request("opentdb", base_url, params=params)

        if not response:
            return self._standardize_result("opentdb", query or "random", {}, False, "Request failed")

        try:
            data = response.json()

            if data.get('response_code') != 0:  # 0 = success
                return self._standardize_result("opentdb", query or "random", {},
                                              False, f"API error: {data.get('response_code')}")

            results = []
            for question in data.get('results', []):
                results.append({
                    'category': question.get('category', ''),
                    'type': question.get('type', ''),
                    'difficulty': question.get('difficulty', ''),
                    'question': question.get('question', ''),
                    'correct_answer': question.get('correct_answer', ''),
                    'incorrect_answers': question.get('incorrect_answers', [])
                })

            return self._standardize_result("opentdb", query or "random", {
                'query': query or "random",
                'results': results
            })

        except Exception as e:
            return self._standardize_result("opentdb", query or "random", {}, False, str(e))

    def search_github(self, query: str, limit: int = 5) -> APIResult:
        """Search GitHub repositories and code"""
        if not self.config["apis"]["github"]["enabled"]:
            return self._standardize_result("github", query, {}, False, "API disabled")

        base_url = self.config["apis"]["github"]["base_url"]
        token = self.config["apis"]["github"]["token"]

        headers = {}
        if token:
            headers['Authorization'] = f'token {token}'

        # Search repositories
        repo_url = f"{base_url}repositories"
        response = self._make_request("github", repo_url, params={
            'q': query,
            'sort': 'stars',
            'order': 'desc',
            'per_page': min(limit, 100)
        }, headers=headers)

        if not response:
            return self._standardize_result("github", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for repo in data.get('items', [])[:limit]:
                results.append({
                    'name': repo.get('name', ''),
                    'full_name': repo.get('full_name', ''),
                    'description': repo.get('description', ''),
                    'url': repo.get('html_url', ''),
                    'language': repo.get('language', ''),
                    'stars': repo.get('stargazers_count', 0),
                    'forks': repo.get('forks_count', 0),
                    'owner': repo.get('owner', {}).get('login', ''),
                    'created_at': repo.get('created_at', ''),
                    'updated_at': repo.get('updated_at', '')
                })

            return self._standardize_result("github", query, {
                'query': query,
                'total_count': data.get('total_count', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("github", query, {}, False, str(e))

    def search_stackexchange(self, query: str, limit: int = 5) -> APIResult:
        """Search Stack Exchange for programming Q&A"""
        if not self.config["apis"]["stackexchange"]["enabled"]:
            return self._standardize_result("stackexchange", query, {}, False, "API disabled")

        base_url = self.config["apis"]["stackexchange"]["base_url"]
        search_url = f"{base_url}search/advanced"

        response = self._make_request("stackexchange", search_url, params={
            'q': query,
            'site': 'stackoverflow',
            'pagesize': min(limit, 100),
            'sort': 'relevance',
            'order': 'desc',
            'filter': 'default'  # Include question body
        })

        if not response:
            return self._standardize_result("stackexchange", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for question in data.get('items', [])[:limit]:
                results.append({
                    'title': question.get('title', ''),
                    'link': question.get('link', ''),
                    'score': question.get('score', 0),
                    'answer_count': question.get('answer_count', 0),
                    'is_answered': question.get('is_answered', False),
                    'tags': question.get('tags', []),
                    'owner': question.get('owner', {}).get('display_name', ''),
                    'creation_date': question.get('creation_date', 0),
                    'last_activity_date': question.get('last_activity_date', 0)
                })

            return self._standardize_result("stackexchange", query, {
                'query': query,
                'has_more': data.get('has_more', False),
                'quota_remaining': data.get('quota_remaining', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("stackexchange", query, {}, False, str(e))

    def search_worldbank(self, query: str, limit: int = 5) -> APIResult:
        """Search World Bank data"""
        if not self.config["apis"]["worldbank"]["enabled"]:
            return self._standardize_result("worldbank", query, {}, False, "API disabled")

        base_url = self.config["apis"]["worldbank"]["base_url"]

        # First search for indicators
        search_url = f"{base_url}indicators"
        response = self._make_request("worldbank", search_url, params={
            'format': 'json',
            'per_page': 100  # Get more to filter
        })

        if not response:
            return self._standardize_result("worldbank", query, {}, False, "Request failed")

        try:
            data = response.json()
            if len(data) < 2:
                return self._standardize_result("worldbank", query, {}, False, "Invalid response")

            indicators = data[1]  # Second element contains the data

            # Filter indicators by query
            matching_indicators = []
            query_lower = query.lower()

            for indicator in indicators:
                name = indicator.get('name', '').lower()
                source_note = indicator.get('sourceNote', '').lower()

                if query_lower in name or query_lower in source_note:
                    matching_indicators.append({
                        'id': indicator.get('id', ''),
                        'name': indicator.get('name', ''),
                        'source_note': indicator.get('sourceNote', ''),
                        'source': indicator.get('source', {}).get('value', ''),
                        'topics': [topic.get('value', '') for topic in indicator.get('topics', [])]
                    })

            return self._standardize_result("worldbank", query, {
                'query': query,
                'total_results': len(matching_indicators),
                'results': matching_indicators[:limit]
            })

        except Exception as e:
            return self._standardize_result("worldbank", query, {}, False, str(e))

    def search_nasa(self, query: str = "apod", limit: int = 5) -> APIResult:
        """Search NASA APIs for space data"""
        if not self.config["apis"]["nasa"]["enabled"]:
            return self._standardize_result("nasa", query, {}, False, "API disabled")

        base_url = self.config["apis"]["nasa"]["base_url"]
        api_key = self.config["apis"]["nasa"]["api_key"]

        # Default to Astronomy Picture of the Day if no specific query
        if query.lower() in ['apod', 'picture', 'image', 'astronomy']:
            apod_url = f"{base_url}planetary/apod"
            response = self._make_request("nasa", apod_url, params={
                'api_key': api_key,
                'count': min(limit, 10) if limit > 1 else None
            })

            if response:
                try:
                    data = response.json()
                    if isinstance(data, list):
                        results = data
                    else:
                        results = [data]

                    return self._standardize_result("nasa", query, {
                        'query': query,
                        'results': results
                    })
                except Exception as e:
                    pass

        # Try NASA Image and Video Library
        search_url = f"{base_url}search"
        response = self._make_request("nasa", search_url, params={
            'q': query,
            'media_type': 'image,video',
            'page_size': min(limit, 100)
        })

        if not response:
            return self._standardize_result("nasa", query, {}, False, "Request failed")

        try:
            data = response.json()
            results = []

            for item in data.get('collection', {}).get('items', [])[:limit]:
                result = {
                    'title': item.get('data', [{}])[0].get('title', ''),
                    'description': item.get('data', [{}])[0].get('description', ''),
                    'media_type': item.get('data', [{}])[0].get('media_type', ''),
                    'date_created': item.get('data', [{}])[0].get('date_created', ''),
                    'keywords': item.get('data', [{}])[0].get('keywords', [])
                }

                # Add media links
                if item.get('links'):
                    result['preview_url'] = item['links'][0].get('href', '')

                results.append(result)

            return self._standardize_result("nasa", query, {
                'query': query,
                'total_hits': data.get('collection', {}).get('metadata', {}).get('total_hits', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("nasa", query, {}, False, str(e))

    def search_omdb(self, query: str, limit: int = 5) -> APIResult:
        """Search OMDB for movie/TV information"""
        if not self.config["apis"]["omdb"]["enabled"]:
            return self._standardize_result("omdb", query, {}, False, "API disabled")

        base_url = self.config["apis"]["omdb"]["base_url"]
        api_key = self.config["apis"]["omdb"]["api_key"]

        if not api_key:
            return self._standardize_result("omdb", query, {}, False, "API key required")

        # First search for movies
        search_url = base_url
        response = self._make_request("omdb", search_url, params={
            's': query,
            'apikey': api_key,
            'type': 'movie',  # Can be movie, series, episode
            'page': 1
        })

        if not response:
            return self._standardize_result("omdb", query, {}, False, "Request failed")

        try:
            data = response.json()

            if data.get('Response') == 'False':
                return self._standardize_result("omdb", query, {}, False, data.get('Error', 'Unknown error'))

            results = []
            for movie in data.get('Search', [])[:limit]:
                # Get detailed info for each movie
                detail_response = self._make_request("omdb", search_url, params={
                    'i': movie.get('imdbID'),
                    'apikey': api_key,
                    'plot': 'short'
                })

                if detail_response:
                    detail_data = detail_response.json()
                    if detail_data.get('Response') == 'True':
                        results.append({
                            'title': detail_data.get('Title', ''),
                            'year': detail_data.get('Year', ''),
                            'rated': detail_data.get('Rated', ''),
                            'released': detail_data.get('Released', ''),
                            'runtime': detail_data.get('Runtime', ''),
                            'genre': detail_data.get('Genre', ''),
                            'director': detail_data.get('Director', ''),
                            'writer': detail_data.get('Writer', ''),
                            'actors': detail_data.get('Actors', ''),
                            'plot': detail_data.get('Plot', ''),
                            'language': detail_data.get('Language', ''),
                            'country': detail_data.get('Country', ''),
                            'awards': detail_data.get('Awards', ''),
                            'poster': detail_data.get('Poster', ''),
                            'imdb_rating': detail_data.get('imdbRating', ''),
                            'imdb_votes': detail_data.get('imdbVotes', ''),
                            'imdb_id': detail_data.get('imdbID', ''),
                            'type': detail_data.get('Type', '')
                        })

            return self._standardize_result("omdb", query, {
                'query': query,
                'total_results': data.get('totalResults', 0),
                'results': results
            })

        except Exception as e:
            return self._standardize_result("omdb", query, {}, False, str(e))

    def multi_search(self, query: str, apis: List[str] = None) -> Dict[str, APIResult]:
        """Search multiple APIs simultaneously"""
        if apis is None:
            # Default to all enabled APIs
            apis = [api for api in self.config["apis"].keys()
                   if self.config["apis"][api]["enabled"]]

        results = {}

        # Define API search methods
        api_methods = {
            'wikipedia': lambda: self.search_wikipedia(query),
            'openlibrary': lambda: self.search_openlibrary(query),
            'dbpedia': lambda: self.search_dbpedia(query),
            'pubmed': lambda: self.search_pubmed(query),
            'arxiv': lambda: self.search_arxiv(query),
            'semantic_scholar': lambda: self.search_semantic_scholar(query),
            'newsapi': lambda: self.search_newsapi(query),
            'currents': lambda: self.search_currents(query),
            'dictionary': lambda: self.search_dictionary(query),
            'restcountries': lambda: self.search_restcountries(query),
            'opentdb': lambda: self.search_opentdb(query),
            'github': lambda: self.search_github(query),
            'stackexchange': lambda: self.search_stackexchange(query),
            'worldbank': lambda: self.search_worldbank(query),
            'nasa': lambda: self.search_nasa(query),
            'omdb': lambda: self.search_omdb(query)
        }

        if self.config["processing"]["parallel_requests"]:
            # Parallel execution
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=min(len(apis), 5)) as executor:
                future_to_api = {executor.submit(api_methods[api]): api for api in apis if api in api_methods}
                for future in as_completed(future_to_api):
                    api = future_to_api[future]
                    try:
                        results[api] = future.result()
                    except Exception as e:
                        logger.error(f"Error in {api} search: {e}")
                        results[api] = self._standardize_result(api, query, {}, False, str(e))
        else:
            # Sequential execution
            for api in apis:
                if api in api_methods:
                    try:
                        results[api] = api_methods[api]()
                    except Exception as e:
                        logger.error(f"Error in {api} search: {e}")
                        results[api] = self._standardize_result(api, query, {}, False, str(e))

        return results

    def process_knowledge_results(self, search_results: Dict[str, APIResult],
                                 extract_content: bool = True) -> List[KnowledgeEntry]:
        """Process API results into unified knowledge entries with optional content extraction"""
        knowledge_entries = []

        for api_name, result in search_results.items():
            if not result.success or not result.data:
                continue

            # Extract knowledge from different API formats
            entries = self._extract_knowledge_from_api(api_name, result)

            for entry in entries:
                # Skip if we've seen this content before
                content_hash = hashlib.md5(entry['content'].encode()).hexdigest()
                if content_hash in self.processed_queries:
                    continue

                self.processed_queries.add(content_hash)

                # Extract additional content from URLs if enabled
                extracted_content = None
                if extract_content:
                    url = entry.get('url') or entry.get('pdf_url') or entry.get('abstract_url')
                    if url:
                        try:
                            extracted_content = self.extract_content_from_url(url)
                            if not extracted_content.success:
                                extracted_content = None
                        except Exception as e:
                            logger.debug(f"Content extraction failed for {url}: {e}")
                            extracted_content = None

                # Create knowledge entry
                knowledge_entry = KnowledgeEntry(
                    id=f"{api_name}_{content_hash[:8]}",
                    title=entry['title'],
                    content=entry['content'],
                    extracted_content=extracted_content,
                    summary=entry.get('summary', entry['content'][:500]),
                    source=api_name,
                    category=self._categorize_knowledge(entry, api_name),
                    url=entry.get('url', ''),
                    timestamp=time.time(),
                    relevance_score=self._calculate_relevance(entry, result.query),
                    credibility_score=self._assess_credibility(api_name, entry),
                    tags=entry.get('tags', []),
                    entities=entry.get('entities', []),
                    metadata={
                        'api_result': asdict(result),
                        'processing_timestamp': time.time(),
                        'content_hash': content_hash,
                        'has_extracted_content': extracted_content is not None
                    }
                )

                knowledge_entries.append(knowledge_entry)

        # Sort by relevance and credibility
        knowledge_entries.sort(key=lambda x: (x.relevance_score * x.credibility_score), reverse=True)

        return knowledge_entries

    def _extract_knowledge_from_api(self, api_name: str, result: APIResult) -> List[Dict]:
        """Extract knowledge entries from API-specific result format"""
        entries = []

        if api_name == 'wikipedia':
            for item in result.data.get('results', []):
                entries.append({
                    'title': item.get('title', ''),
                    'content': item.get('summary', ''),
                    'summary': item.get('summary', ''),
                    'url': item.get('url', ''),
                    'tags': item.get('categories', []),
                    'entities': []  # Could be enhanced with NER
                })

        elif api_name == 'openlibrary':
            for item in result.data.get('results', []):
                content = f"Title: {item.get('title', '')}\n"
                content += f"Authors: {', '.join(item.get('authors', []))}\n"
                if item.get('first_publish_year'):
                    content += f"First Published: {item.get('first_publish_year')}\n"
                content += f"Subjects: {', '.join(item.get('subjects', [])[:5])}"

                entries.append({
                    'title': item.get('title', ''),
                    'content': content,
                    'url': f"https://openlibrary.org{item.get('key', '')}",
                    'tags': item.get('subjects', [])[:10],
                    'entities': [{'text': author, 'type': 'PERSON'} for author in item.get('authors', [])]
                })

        elif api_name == 'dictionary':
            word_data = result.data
            content = f"Word: {word_data.get('word', '')}\n"
            content += f"Phonetic: {word_data.get('phonetic', '')}\n\n"

            for meaning in word_data.get('meanings', []):
                content += f"Part of Speech: {meaning.get('part_of_speech', '')}\n"
                for definition in meaning.get('definitions', []):
                    content += f"Definition: {definition.get('definition', '')}\n"
                    if definition.get('example'):
                        content += f"Example: {definition.get('example', '')}\n"
                    content += "\n"

            entries.append({
                'title': f"Definition: {word_data.get('word', '')}",
                'content': content,
                'tags': ['definition', 'language', word_data.get('word', '')],
                'entities': []
            })

        elif api_name == 'restcountries':
            country_data = result.data
            content = f"Country: {country_data.get('name', '')}\n"
            content += f"Official Name: {country_data.get('official_name', '')}\n"
            content += f"Capital: {country_data.get('capital', '')}\n"
            content += f"Region: {country_data.get('region', '')}\n"
            content += f"Population: {country_data.get('population', 0):,}\n"
            content += f"Area: {country_data.get('area', 0):,} km²\n"
            content += f"Languages: {', '.join(country_data.get('languages', []))}\n"
            content += f"Currencies: {', '.join(country_data.get('currencies', []))}"

            entries.append({
                'title': f"Country: {country_data.get('name', '')}",
                'content': content,
                'url': country_data.get('flag_url', ''),
                'tags': ['country', country_data.get('region', ''), country_data.get('subregion', '')],
                'entities': [{'text': country_data.get('name', ''), 'type': 'GPE'}]
            })

        elif api_name == 'arxiv':
            for item in result.data.get('results', []):
                # Enhanced content with full abstract if available
                content = item.get('summary', '')
                if len(content) < 200:
                    # Try to get more detailed content from the abstract URL
                    try:
                        extracted = self.extract_content_from_url(item.get('abstract_url', ''))
                        if extracted and extracted.success and extracted.cleaned_text:
                            content = extracted.cleaned_text
                    except:
                        pass

                entries.append({
                    'title': item.get('title', ''),
                    'content': content,
                    'summary': item.get('summary', ''),
                    'url': item.get('pdf_url', ''),
                    'pdf_url': item.get('pdf_url', ''),
                    'abstract_url': item.get('abstract_url', ''),
                    'tags': item.get('categories', []) + ['academic', 'research', 'arxiv'],
                    'entities': [{'text': author, 'type': 'PERSON'} for author in item.get('authors', [])]
                })

        elif api_name == 'pubmed':
            for item in result.data.get('results', []):
                # Get full abstract if not already present
                content = item.get('abstract', '')
                if not content and item.get('pmid'):
                    content = self.get_pubmed_full_abstract(item['pmid'])

                if not content:
                    content = f"Title: {item.get('title', '')}\nAuthors: {', '.join(item.get('authors', []))}\nJournal: {item.get('journal', '')}"

                entries.append({
                    'title': item.get('title', ''),
                    'content': content,
                    'url': f"https://pubmed.ncbi.nlm.nih.gov/{item.get('pmid', '')}",
                    'tags': ['medical', 'academic', 'pubmed', 'research'],
                    'entities': [{'text': author, 'type': 'PERSON'} for author in item.get('authors', [])]
                })

        elif api_name == 'semantic_scholar':
            for item in result.data.get('results', []):
                content = item.get('abstract', '')
                if not content:
                    content = f"Title: {item.get('title', '')}\nAuthors: {', '.join(item.get('authors', []))}\nYear: {item.get('year', 'N/A')}"

                entries.append({
                    'title': item.get('title', ''),
                    'content': content,
                    'url': f"https://www.semanticscholar.org/paper/{item.get('paper_id', '')}",
                    'tags': ['academic', 'research', 'semantic_scholar'],
                    'entities': [{'text': author.get('name', ''), 'type': 'PERSON'} for author in item.get('authors', [])]
                })

        # Add more API-specific extractors as needed...

        return entries

    def _categorize_knowledge(self, entry: Dict, api_name: str) -> str:
        """Categorize knowledge entry"""
        content = entry.get('content', '').lower()
        tags = entry.get('tags', [])

        # API-specific default categories
        api_categories = {
            'wikipedia': 'reference',
            'openlibrary': 'reference',
            'dictionary': 'reference',
            'restcountries': 'data',
            'pubmed': 'academic',
            'arxiv': 'academic',
            'semantic_scholar': 'academic',
            'newsapi': 'news',
            'currents': 'news',
            'opentdb': 'reference',
            'github': 'technical',
            'stackexchange': 'technical',
            'worldbank': 'data',
            'nasa': 'science',
            'omdb': 'media'
        }

        category = api_categories.get(api_name, 'general')

        # Override based on content analysis
        for cat, keywords in self.categories.items():
            if any(keyword in content for keyword in keywords) or any(keyword in ' '.join(tags).lower() for keyword in keywords):
                category = cat
                break

        return category

    def _calculate_relevance(self, entry: Dict, query: str) -> float:
        """Calculate relevance score for knowledge entry"""
        query_lower = query.lower()
        content_lower = entry.get('content', '').lower()
        title_lower = entry.get('title', '').lower()

        score = 0.0

        # Exact matches in title
        if query_lower in title_lower:
            score += 0.5

        # Word matches in content
        query_words = set(query_lower.split())
        content_words = set(content_lower.split())
        word_matches = len(query_words.intersection(content_words))
        score += min(word_matches * 0.1, 0.3)

        # Length bonus (longer content often more relevant)
        if len(content_lower) > 1000:
            score += 0.1

        return min(score, 1.0)

    def _assess_credibility(self, api_name: str, entry: Dict) -> float:
        """Assess credibility score for knowledge entry"""
        base_credibility = {
            'wikipedia': 0.8,
            'pubmed': 0.9,
            'arxiv': 0.8,
            'semantic_scholar': 0.8,
            'newsapi': 0.7,
            'currents': 0.6,
            'github': 0.8,
            'stackexchange': 0.7,
            'worldbank': 0.9,
            'nasa': 0.9,
            'openlibrary': 0.8,
            'dictionary': 0.8,
            'restcountries': 0.9,
            'opentdb': 0.6,
            'omdb': 0.7,
            'dbpedia': 0.7
        }

        credibility = base_credibility.get(api_name, 0.5)

        # Boost for entries with sources/URLs
        if entry.get('url'):
            credibility += 0.1

        return min(credibility, 1.0)

    def save_knowledge_data(self):
        """Save processed knowledge entries"""
        try:
            knowledge_data = {
                'entries': [asdict(entry) for entry in self.knowledge_entries],
                'last_updated': time.time(),
                'total_entries': len(self.knowledge_entries),
                'api_stats': dict(self.api_stats)
            }

            os.makedirs(os.path.dirname(self.config["output"]["knowledge_data"]), exist_ok=True)
            with open(self.config["output"]["knowledge_data"], 'w') as f:
                json.dump(knowledge_data, f, indent=2)

            logger.info(f"Saved {len(self.knowledge_entries)} knowledge entries")

        except Exception as e:
            logger.error(f"Error saving knowledge data: {e}")

    def run_knowledge_search(self, query: str, apis: List[str] = None,
                           extract_content: bool = True) -> List[KnowledgeEntry]:
        """Run complete knowledge search pipeline with optional content extraction"""
        logger.info(f"Starting knowledge search for: {query}")

        # Multi-API search
        search_results = self.multi_search(query, apis)

        # Process into unified knowledge entries
        knowledge_entries = self.process_knowledge_results(search_results, extract_content)

        # Store in memory
        self.knowledge_entries.extend(knowledge_entries)

        # Limit memory size
        if len(self.knowledge_entries) > 1000:
            self.knowledge_entries = self.knowledge_entries[-1000:]

        # Save results
        search_data = {
            'query': query,
            'timestamp': time.time(),
            'api_results': {api: asdict(result) for api, result in search_results.items()},
            'knowledge_entries': [asdict(entry) for entry in knowledge_entries],
            'content_extraction_enabled': extract_content,
            'extraction_stats': dict(self.extraction_stats)
        }

        os.makedirs(os.path.dirname(self.config["output"]["search_results"]), exist_ok=True)
        with open(self.config["output"]["search_results"], 'w') as f:
            json.dump(search_data, f, indent=2)

        # Save API stats
        stats_data = {
            'timestamp': time.time(),
            'stats': dict(self.api_stats),
            'extraction_stats': dict(self.extraction_stats)
        }

        with open(self.config["output"]["api_stats"], 'w') as f:
            json.dump(stats_data, f, indent=2)

        logger.info(f"Completed knowledge search: {len(knowledge_entries)} entries found")

        return knowledge_entries

    def extract_academic_content(self, query: str, limit: int = 3) -> List[KnowledgeEntry]:
        """Extract detailed content from academic sources (PDFs, papers, etc.)"""
        logger.info(f"Extracting academic content for: {query}")

        # Search academic APIs
        academic_apis = ['arxiv', 'pubmed', 'semantic_scholar']
        search_results = self.multi_search(query, academic_apis)

        # Process with content extraction enabled
        knowledge_entries = self.process_knowledge_results(search_results, extract_content=True)

        # Filter for academic entries and enhance with full content
        academic_entries = []
        for entry in knowledge_entries:
            if entry.category == 'academic':
                # Try to extract full content from PDFs if available
                if hasattr(entry, 'extracted_content') and not entry.extracted_content:
                    pdf_url = entry.url if entry.url and entry.url.endswith('.pdf') else None
                    if not pdf_url and entry.metadata.get('api_result', {}).get('data', {}):
                        # Look for PDF URL in API result
                        api_data = entry.metadata['api_result']['data']
                        for result in api_data.get('results', []):
                            if result.get('pdf_url') and result.get('title') == entry.title:
                                pdf_url = result['pdf_url']
                                break

                    if pdf_url and PDF_AVAILABLE:
                        try:
                            extracted = self.extract_content_from_url(pdf_url)
                            if extracted and extracted.success:
                                entry.extracted_content = extracted
                        except Exception as e:
                            logger.debug(f"Failed to extract PDF content: {e}")

                academic_entries.append(entry)

        # Sort by content quality (prefer entries with extracted content)
        academic_entries.sort(key=lambda x: (
            x.extracted_content is not None and x.extracted_content.success,
            x.relevance_score
        ), reverse=True)

        logger.info(f"Extracted academic content: {len(academic_entries)} entries")
        return academic_entries[:limit]

    def get_content_summary(self, url: str) -> Dict[str, Any]:
        """Get a comprehensive summary of content from any URL"""
        extracted = self.extract_content_from_url(url)

        if not extracted or not extracted.success:
            return {
                'url': url,
                'success': False,
                'error': extracted.error_message if extracted else 'Extraction failed'
            }

        return {
            'url': url,
            'success': True,
            'content_type': extracted.content_type,
            'word_count': extracted.word_count,
            'summary': extracted.summary,
            'key_phrases': extracted.key_phrases,
            'metadata': extracted.metadata,
            'extraction_time': extracted.extraction_timestamp
        }

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    feeder = KnowledgeAPIFeeder()

    # Parse command line arguments
    import sys
    if len(sys.argv) > 1:
        query = ' '.join(sys.argv[1:])
        results = feeder.run_knowledge_search(query)
        print(f"\nFound {len(results)} knowledge entries for: {query}")

        # Display top results
        for i, entry in enumerate(results[:5]):
            print(f"\n{i+1}. {entry.title}")
            print(f"   Source: {entry.source}")
            print(f"   Category: {entry.category}")
            print(f"   Relevance: {entry.relevance_score:.2f}")
            print(f"   Summary: {entry.summary[:200]}...")
    else:
        print("Usage: python knowledge_api_feeder.py <search query>")
        print("Example: python knowledge_api_feeder.py 'machine learning'")

if __name__ == "__main__":
    main()
