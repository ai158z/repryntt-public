#!/usr/bin/env python3
"""
DuckDuckGo Search Scraper with Content Extraction
Uses Selenium to perform DuckDuckGo searches and scrape result content.
Designed to integrate with SAIGE AI system.
"""

import json
import sys
import time
import re
import logging
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse, quote_plus
from datetime import datetime

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
    from selenium.webdriver.chrome.service import Service
except ImportError:
    print("ERROR: Selenium not installed. Install with: pip install selenium")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: BeautifulSoup not installed. Install with: pip install beautifulsoup4")
    sys.exit(1)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DuckDuckGoSearchScraper:
    """
    A robust DuckDuckGo search scraper that can:
    1. Perform DuckDuckGo searches
    2. Extract search results (title, URL, snippet)
    3. Visit result pages and extract full content
    4. Handle various content types and structures
    5. Support site-specific searches and advanced operators
    """
    
    def __init__(self, headless: bool = True, timeout: int = 30):
        """
        Initialize the DuckDuckGo Search Scraper
        
        Args:
            headless: Run browser in headless mode (no GUI)
            timeout: Default timeout for page loads in seconds
        """
        self.headless = headless
        self.timeout = timeout
        self.driver = None
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        
    def setup_driver(self) -> bool:
        """
        Setup Chrome WebDriver with appropriate options
        
        Returns:
            bool: True if setup successful, False otherwise
        """
        try:
            chrome_options = Options()
            
            if self.headless:
                chrome_options.add_argument('--headless')
            
            # Essential arguments for stability (simpler = more reliable for DDG)
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            
            # User agent
            import random
            user_agent = random.choice(self.user_agents)
            chrome_options.add_argument(f'--user-agent={user_agent}')
            
            # Detect snap Chromium binary (common on Ubuntu)
            import os
            snap_chrome = '/snap/chromium/current/usr/lib/chromium-browser/chrome'
            if os.path.exists(snap_chrome):
                chrome_options.binary_location = snap_chrome
                logger.info(f"🔧 Using snap Chromium binary: {snap_chrome}")
            
            # Try different chromedriver paths
            import shutil as _shutil_chrome
            chromedriver_paths = [
                _shutil_chrome.which('chromedriver'),
                '/usr/bin/chromedriver',
                '/snap/chromium/current/usr/lib/chromium-browser/chromedriver',
                '/usr/lib/bin/chromedriver',
                '/usr/local/bin/chromedriver',
                'chromedriver'
            ]
            
            driver = None
            for path in chromedriver_paths:
                try:
                    service = Service(executable_path=path)
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                    logger.info(f"✅ Using chromedriver at: {path}")
                    break
                except Exception:
                    continue
            
            if driver is None:
                # Fallback to auto-detection
                driver = webdriver.Chrome(options=chrome_options)
            
            # Set page load timeout
            driver.set_page_load_timeout(self.timeout)
            driver.implicitly_wait(10)
            
            self.driver = driver
            logger.info("✅ Chrome WebDriver initialized successfully for DuckDuckGo")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error setting up Chrome driver: {e}")
            logger.error("Make sure Chrome and chromedriver are installed.")
            return False
    
    def duckduckgo_search(self, query: str, num_results: int = 10, region: str = 'us-en') -> Dict[str, Any]:
        """
        Perform a DuckDuckGo search and extract search results
        
        Args:
            query: The search query (supports operators like site:, filetype:, etc.)
            num_results: Number of results to extract (default 10)
            region: Region for search results (default 'us-en')
            
        Returns:
            Dict containing search results with metadata
        """
        if not self.driver:
            return {"success": False, "error": "Driver not initialized"}
        
        try:
            # Construct DuckDuckGo search URL
            search_url = f"https://duckduckgo.com/?q={quote_plus(query)}&kl={region}"
            
            logger.info(f"🔍 Searching DuckDuckGo for: '{query}'")
            self.driver.get(search_url)
            
            # Wait for search results to load
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "links"))
                )
            except TimeoutException:
                logger.warning("Timeout waiting for search results container")
            
            # Small delay to let dynamic content load
            time.sleep(3)
            
            # Extract search results
            results = self._extract_search_results()
            
            return {
                "success": True,
                "query": query,
                "num_results": len(results),
                "results": results,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"❌ Search failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "query": query
            }
    
    def _extract_search_results(self) -> List[Dict[str, Any]]:
        """
        Extract search results from the current DuckDuckGo search page
        
        Returns:
            List of dictionaries containing result data
        """
        results = []
        
        try:
            # Get page source and parse with BeautifulSoup for more reliable extraction
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            
            # DuckDuckGo uses article tags with data-testid for organic results
            search_results = soup.find_all('article', {'data-testid': 'result'})
            
            # If no results with data-testid, try alternative selectors
            if not search_results:
                search_results = soup.find_all('div', class_=lambda x: x and 'result' in x.lower())
            
            for result_elem in search_results:
                try:
                    result_data = {}
                    
                    # Extract title and URL
                    # DuckDuckGo uses h2 with a link inside
                    title_elem = result_elem.find('h2')
                    if title_elem:
                        link = title_elem.find('a', href=True)
                        if link:
                            result_data['title'] = link.get_text(strip=True)
                            result_data['url'] = link['href']
                    
                    # If still no link found, try direct link search
                    if 'url' not in result_data:
                        link = result_elem.find('a', href=True)
                        if link and link['href'].startswith('http'):
                            result_data['url'] = link['href']
                            if not result_data.get('title'):
                                result_data['title'] = link.get_text(strip=True)
                    
                    # Extract snippet/description
                    # DuckDuckGo snippets are usually in div or span elements
                    snippet_elem = result_elem.find('div', {'data-result': 'snippet'})
                    if not snippet_elem:
                        # Try alternative selectors
                        snippet_elem = result_elem.find('span', class_=lambda x: x and 'snippet' in x.lower())
                    if not snippet_elem:
                        # Last resort: get all text and filter
                        all_text = result_elem.get_text(separator=' ', strip=True)
                        # Remove title from text to get snippet
                        if result_data.get('title'):
                            snippet = all_text.replace(result_data['title'], '').strip()
                            if len(snippet) > 20:
                                result_data['snippet'] = snippet[:300]
                    else:
                        result_data['snippet'] = snippet_elem.get_text(strip=True)
                    
                    # Only add if we have at least a URL and title
                    if 'url' in result_data and 'title' in result_data and result_data['title']:
                        results.append(result_data)
                        logger.debug(f"  ✓ Found result: {result_data['title'][:50]}...")
                
                except Exception as e:
                    logger.debug(f"Failed to extract result: {e}")
                    continue
            
            logger.info(f"📊 Extracted {len(results)} search results from DuckDuckGo")
            
        except Exception as e:
            logger.error(f"❌ Failed to extract search results: {e}")
        
        return results
    
    def scrape_page_content(self, url: str, extract_main_content: bool = True) -> Dict[str, Any]:
        """
        Visit a URL and scrape its content
        
        Args:
            url: The URL to scrape
            extract_main_content: Try to extract only main content (remove nav, footer, etc.)
            
        Returns:
            Dict containing page content and metadata
        """
        if not self.driver:
            return {"success": False, "error": "Driver not initialized"}
        
        try:
            logger.info(f"📄 Scraping content from: {url}")
            
            # Visit the page
            self.driver.get(url)
            
            # Wait for page to load
            time.sleep(2)
            
            # Get page source
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            
            # Extract metadata
            page_data = {
                "success": True,
                "url": url,
                "title": self.driver.title,
                "timestamp": datetime.now().isoformat()
            }
            
            # Extract meta description
            meta_desc = soup.find('meta', {'name': 'description'})
            if meta_desc:
                page_data['description'] = meta_desc.get('content', '')
            
            # Extract meta keywords
            meta_keywords = soup.find('meta', {'name': 'keywords'})
            if meta_keywords:
                page_data['keywords'] = meta_keywords.get('content', '')
            
            # Extract Open Graph data
            og_title = soup.find('meta', {'property': 'og:title'})
            if og_title:
                page_data['og_title'] = og_title.get('content', '')
            
            og_desc = soup.find('meta', {'property': 'og:description'})
            if og_desc:
                page_data['og_description'] = og_desc.get('content', '')
            
            # Extract main content
            if extract_main_content:
                content = self._extract_main_content(soup)
            else:
                # Get all text from body
                body = soup.find('body')
                content = body.get_text(separator='\n', strip=True) if body else ''
            
            page_data['content'] = content
            page_data['content_length'] = len(content)
            
            # Extract headings for structure
            headings = []
            for heading in soup.find_all(['h1', 'h2', 'h3']):
                headings.append({
                    'level': int(heading.name[1]),
                    'text': heading.get_text(strip=True)
                })
            page_data['headings'] = headings
            
            # Extract links
            links = []
            for link in soup.find_all('a', href=True)[:50]:  # Limit to 50 links
                href = link['href']
                text = link.get_text(strip=True)
                if text and len(text) > 3:
                    links.append({'text': text, 'url': href})
            page_data['links'] = links
            
            logger.info(f"✅ Scraped {len(content)} characters of content")
            
            return page_data
            
        except Exception as e:
            logger.error(f"❌ Failed to scrape page: {e}")
            return {
                "success": False,
                "error": str(e),
                "url": url
            }
    
    def _extract_main_content(self, soup: BeautifulSoup) -> str:
        """
        Extract main content from page, removing navigation, ads, etc.
        
        Args:
            soup: BeautifulSoup object of the page
            
        Returns:
            Extracted main content as string
        """
        # Remove unwanted elements
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
            element.decompose()
        
        # Try to find main content container
        main_selectors = [
            ('main', {}),
            ('article', {}),
            ('div', {'id': 'content'}),
            ('div', {'class': 'content'}),
            ('div', {'id': 'main'}),
            ('div', {'class': 'main'}),
            ('div', {'role': 'main'}),
        ]
        
        main_content = None
        for tag, attrs in main_selectors:
            main_content = soup.find(tag, attrs)
            if main_content:
                break
        
        # If no main content found, use body
        if not main_content:
            main_content = soup.find('body')
        
        if main_content:
            # Extract text with some structure preservation
            text = main_content.get_text(separator='\n', strip=True)
            # Clean up excessive newlines
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text
        
        return ""
    
    def search_and_scrape(self, query: str, num_results: int = 5, scrape_top_n: int = 3) -> Dict[str, Any]:
        """
        Perform a DuckDuckGo search and scrape content from top results
        
        Args:
            query: Search query (supports operators: site:, filetype:, intitle:, etc.)
            num_results: Number of search results to extract
            scrape_top_n: Number of top results to scrape full content from
            
        Returns:
            Dict containing search results and scraped content
        """
        # Perform search
        search_results = self.duckduckgo_search(query, num_results)
        
        if not search_results.get('success'):
            return search_results
        
        # Scrape top N results
        scraped_pages = []
        results = search_results.get('results', [])
        
        for i, result in enumerate(results[:scrape_top_n]):
            url = result.get('url')
            if url:
                logger.info(f"📖 Scraping result {i+1}/{scrape_top_n}: {result.get('title', 'Unknown')}")
                
                # Scrape the page
                page_data = self.scrape_page_content(url)
                
                if page_data.get('success'):
                    # Combine search result metadata with scraped content
                    combined_data = {**result, **page_data}
                    scraped_pages.append(combined_data)
                    
                    # Small delay between requests
                    time.sleep(1)
        
        return {
            "success": True,
            "query": query,
            "search_results": results,
            "scraped_pages": scraped_pages,
            "num_scraped": len(scraped_pages),
            "timestamp": datetime.now().isoformat()
        }
    
    def cleanup(self):
        """Clean up the browser driver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("🔒 Browser closed")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")


def main():
    """Command-line interface for the DuckDuckGo Search Scraper"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='DuckDuckGo Search Scraper with Content Extraction',
        epilog='''
Examples:
  %(prog)s "weather dunnellon florida"
  %(prog)s "site:weather.com dunnellon" --scrape-top 1
  %(prog)s "filetype:pdf machine learning" --search-only
        '''
    )
    parser.add_argument('query', help='The search query (supports site:, filetype:, intitle:, etc.)')
    parser.add_argument('-n', '--num-results', type=int, default=10,
                       help='Number of search results to extract (default: 10)')
    parser.add_argument('-s', '--scrape-top', type=int, default=3,
                       help='Number of top results to scrape full content from (default: 3)')
    parser.add_argument('-o', '--output', help='Output file (default: print to stdout)')
    parser.add_argument('--visible', action='store_true',
                       help='Run browser in visible mode (not headless)')
    parser.add_argument('--search-only', action='store_true',
                       help='Only perform search, do not scrape page content')
    parser.add_argument('--timeout', type=int, default=30,
                       help='Page load timeout in seconds (default: 30)')
    
    args = parser.parse_args()
    
    # Create scraper
    scraper = DuckDuckGoSearchScraper(headless=not args.visible, timeout=args.timeout)
    
    if not scraper.setup_driver():
        logger.error("Failed to initialize browser driver")
        sys.exit(1)
    
    try:
        # Perform search (and optionally scrape)
        if args.search_only:
            result = scraper.duckduckgo_search(args.query, args.num_results)
        else:
            result = scraper.search_and_scrape(args.query, args.num_results, args.scrape_top)
        
        # Output results
        output = json.dumps(result, indent=2, ensure_ascii=False)
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output)
            logger.info(f"📝 Results saved to {args.output}")
        else:
            print(output)
    
    except KeyboardInterrupt:
        logger.info("\n⚠️ Interrupted by user")
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
    finally:
        scraper.cleanup()


if __name__ == "__main__":
    main()

