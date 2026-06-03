#!/usr/bin/env python3
"""
Web Research Feeder - SAIGE Learning Pipeline
Autonomously scrapes educational content and feeds knowledge into the learning system
Real implementation with content quality filtering and knowledge extraction
"""

import json
import os
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, asdict
import hashlib
import re
from urllib.parse import urljoin, urlparse
import concurrent.futures
from collections import defaultdict, Counter

# Web scraping
import feedparser
from bs4 import BeautifulSoup
import newspaper
from newspaper import Article

# Text processing
import nltk
from textblob import TextBlob
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
import numpy as np

# Download required NLTK data
for dataset in ['punkt', 'stopwords', 'averaged_perceptron_tagger', 'wordnet']:
    try:
        nltk.data.find(f'tokenizers/{dataset}')
    except LookupError:
        try:
            nltk.download(dataset)
        except:
            pass

from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize, sent_tokenize
from nltk.stem import WordNetLemmatizer

logger = logging.getLogger(__name__)

@dataclass
class KnowledgeEntry:
    source_url: str
    title: str
    content: str
    timestamp: float
    category: str
    keywords: List[str]
    quality_score: float
    relevance_score: float
    difficulty_level: float  # 0-1, beginner to expert
    source_type: str  # 'academic', 'news', 'documentation', 'tutorial'
    language: str
    content_hash: str
    extraction_method: str

class WebResearchFeeder:
    """
    Autonomously discovers and processes educational content from the web
    """
    
    def __init__(self, config_path: str = "config/web_research_feeder.json"):
        self.config = self._load_config(config_path)
        self.lemmatizer = WordNetLemmatizer()
        self.stop_words = set(stopwords.words('english'))
        self.vectorizer = TfidfVectorizer(max_features=1000, stop_words='english')
        
        # Knowledge categories for SAIGE
        self.categories = {
            'artificial_intelligence': ['ai', 'machine learning', 'neural network', 'deep learning', 'nlp', 'computer vision'],
            'robotics': ['robot', 'automation', 'sensor', 'actuator', 'control system', 'embedded'],
            'programming': ['python', 'c++', 'javascript', 'algorithm', 'data structure', 'software'],
            'mathematics': ['calculus', 'linear algebra', 'statistics', 'probability', 'optimization'],
            'physics': ['mechanics', 'thermodynamics', 'electromagnetism', 'quantum', 'relativity'],
            'space_science': ['astronomy', 'astrophysics', 'spacecraft', 'mars', 'satellite'],
            'blockchain': ['cryptocurrency', 'bitcoin', 'ethereum', 'smart contract', 'decentralized'],
            'neuroscience': ['brain', 'neuron', 'consciousness', 'cognition', 'neural pathway'],
            'biology': ['genetics', 'evolution', 'cell', 'molecular', 'biochemistry'],
            'engineering': ['mechanical', 'electrical', 'chemical', 'civil', 'design']
        }
        
        # Tracked sources for continuous learning
        self.knowledge_cache = {}  # URL -> KnowledgeEntry
        self.processed_urls = set()
        
        # Quality filters
        self.min_content_length = 500
        self.max_content_length = 50000
        self.quality_keywords = set(['research', 'study', 'analysis', 'theory', 'method', 'experiment'])
        
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "data_sources": {
                "rss_feeds": [
                    "http://rss.arxiv.org/rss/cs.AI",
                    "http://rss.arxiv.org/rss/cs.RO",
                    "http://rss.arxiv.org/rss/cs.LG"
                    # REMOVED: O'Reilly, Technology Review, IEEE Spectrum, Nature (not in approved APIs)
                ],
                "news_apis": [
                    # REMOVED: NewsAPI and Reddit (not in approved APIs)
                ],
                "educational_sites": [
                    # REMOVED: Wikipedia, Stack Overflow, Coursera (not in approved APIs)
                    "https://github.com",
                    "https://ocw.mit.edu"
                ]
            },
            "research_topics": [
                "autonomous AI systems",
                "neural network evolution", 
                "blockchain robotics",
                "space robotics",
                "synthetic biology",
                "consciousness preservation",
                "liquid neural networks",
                "memristive computing"
            ],
            "max_articles_per_run": 50,
            "update_interval": 3600,  # 1 hour
            "quality_threshold": 0.6,
            "relevance_threshold": 0.4,
            "output_file": "data/web_research_knowledge.json",
            "stimulus_output": "data/web_research_stimulus.json"
        }
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config
    
    def scrape_rss_feeds(self) -> List[KnowledgeEntry]:
        """Scrape content from RSS feeds"""
        knowledge_entries = []
        
        for feed_url in self.config["data_sources"]["rss_feeds"]:
            try:
                logger.info(f"Processing RSS feed: {feed_url}")
                feed = feedparser.parse(feed_url)
                
                for entry in feed.entries[:10]:  # Limit per feed
                    if entry.link in self.processed_urls:
                        continue
                    
                    article_content = self._extract_article_content(entry.link)
                    if not article_content:
                        continue
                    
                    knowledge_entry = self._create_knowledge_entry(
                        url=entry.link,
                        title=entry.title,
                        content=article_content,
                        source_type='academic' if 'arxiv' in feed_url else 'news',
                        extraction_method='rss_feed'
                    )
                    
                    if knowledge_entry and knowledge_entry.quality_score >= self.config["quality_threshold"]:
                        knowledge_entries.append(knowledge_entry)
                        self.processed_urls.add(entry.link)
                
            except Exception as e:
                logger.error(f"Error processing RSS feed {feed_url}: {e}")
        
        return knowledge_entries
    
    def search_educational_content(self) -> List[KnowledgeEntry]:
        """Search for educational content based on research topics"""
        knowledge_entries = []
        
        for topic in self.config["research_topics"]:
            try:
                # REMOVED: Wikipedia search (not in approved APIs)

                # Search arXiv papers (approved)
                arxiv_entries = self._search_arxiv(topic)
                knowledge_entries.extend(arxiv_entries)

                # Search GitHub repositories (approved)
                github_entries = self._search_github(topic)
                knowledge_entries.extend(github_entries)
                
                time.sleep(1)  # Rate limiting
                
            except Exception as e:
                logger.error(f"Error searching for topic '{topic}': {e}")
        
        return knowledge_entries
    
    def _search_wikipedia(self, topic: str) -> List[KnowledgeEntry]:
        """Search Wikipedia for educational content"""
        entries = []
        
        try:
            # Wikipedia API search
            search_url = "https://en.wikipedia.org/api/rest_v1/page/summary/"
            topic_formatted = topic.replace(' ', '_')
            
            response = requests.get(search_url + topic_formatted, timeout=10)
            if response.status_code == 200:
                data = response.json()
                
                if 'extract' in data and len(data['extract']) > self.min_content_length:
                    # Get full article content
                    full_content = self._get_wikipedia_full_content(data['title'])
                    if full_content:
                        entry = self._create_knowledge_entry(
                            url=data.get('content_urls', {}).get('desktop', {}).get('page', ''),
                            title=data['title'],
                            content=full_content,
                            source_type='encyclopedia',
                            extraction_method='wikipedia_api'
                        )
                        if entry:
                            entries.append(entry)
            
        except Exception as e:
            logger.error(f"Error searching Wikipedia for '{topic}': {e}")
        
        return entries
    
    def _get_wikipedia_full_content(self, title: str) -> str:
        """Get full content from Wikipedia article"""
        try:
            api_url = "https://en.wikipedia.org/api/rest_v1/page/html/"
            response = requests.get(api_url + title.replace(' ', '_'), timeout=10)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Remove unwanted elements
                for element in soup(['script', 'style', 'nav', 'header', 'footer']):
                    element.decompose()
                
                # Extract main content
                content = soup.get_text()
                
                # Clean up text
                content = re.sub(r'\n\s*\n', '\n\n', content)
                content = re.sub(r'\s+', ' ', content)
                
                # Limit length
                if len(content) > self.max_content_length:
                    content = content[:self.max_content_length] + "..."
                
                return content.strip()
                
        except Exception as e:
            logger.error(f"Error getting full Wikipedia content for '{title}': {e}")
        
        return ""
    
    def _search_arxiv(self, topic: str) -> List[KnowledgeEntry]:
        """Search arXiv for research papers"""
        entries = []
        
        try:
            # arXiv API search
            search_url = "http://export.arxiv.org/api/query"
            params = {
                'search_query': f'all:{topic}',
                'start': 0,
                'max_results': 5,
                'sortBy': 'relevance',
                'sortOrder': 'descending'
            }
            
            response = requests.get(search_url, params=params, timeout=15)
            if response.status_code == 200:
                # Parse XML response
                soup = BeautifulSoup(response.content, 'xml')
                
                for entry in soup.find_all('entry'):
                    title = entry.find('title').text.strip()
                    summary = entry.find('summary').text.strip()
                    link = entry.find('id').text.strip()
                    
                    # Create knowledge entry from abstract
                    knowledge_entry = self._create_knowledge_entry(
                        url=link,
                        title=title,
                        content=summary,
                        source_type='academic',
                        extraction_method='arxiv_api'
                    )
                    
                    if knowledge_entry and knowledge_entry.quality_score >= self.config["quality_threshold"]:
                        entries.append(knowledge_entry)
            
        except Exception as e:
            logger.error(f"Error searching arXiv for '{topic}': {e}")
        
        return entries
    
    def _search_github(self, topic: str) -> List[KnowledgeEntry]:
        """Search GitHub for relevant repositories and documentation"""
        entries = []
        
        try:
            # GitHub API search (public, no auth needed for basic search)
            search_url = "https://api.github.com/search/repositories"
            params = {
                'q': f'{topic} in:readme',
                'sort': 'stars',
                'order': 'desc',
                'per_page': 3
            }
            
            headers = {'Accept': 'application/vnd.github.v3+json'}
            response = requests.get(search_url, params=params, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for repo in data.get('items', []):
                    # Get README content
                    readme_content = self._get_github_readme(repo['full_name'])
                    if readme_content and len(readme_content) > self.min_content_length:
                        entry = self._create_knowledge_entry(
                            url=repo['html_url'],
                            title=f"{repo['name']} - {repo.get('description', '')}",
                            content=readme_content,
                            source_type='documentation',
                            extraction_method='github_api'
                        )
                        if entry:
                            entries.append(entry)
            
        except Exception as e:
            logger.error(f"Error searching GitHub for '{topic}': {e}")
        
        return entries
    
    def _get_github_readme(self, repo_full_name: str) -> str:
        """Get README content from GitHub repository"""
        try:
            readme_url = f"https://api.github.com/repos/{repo_full_name}/readme"
            headers = {'Accept': 'application/vnd.github.v3.raw'}
            
            response = requests.get(readme_url, headers=headers, timeout=10)
            if response.status_code == 200:
                content = response.text
                
                # Remove markdown formatting for cleaner text
                content = re.sub(r'#{1,6}\s+', '', content)  # Headers
                content = re.sub(r'\*\*(.*?)\*\*', r'\1', content)  # Bold
                content = re.sub(r'\*(.*?)\*', r'\1', content)  # Italic
                content = re.sub(r'`(.*?)`', r'\1', content)  # Code
                content = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', content)  # Links
                
                return content.strip()
                
        except Exception as e:
            logger.error(f"Error getting README for {repo_full_name}: {e}")
        
        return ""
    
    def _extract_article_content(self, url: str) -> str:
        """Extract clean article content from URL"""
        try:
            article = Article(url)
            article.download()
            article.parse()
            
            if len(article.text) > self.min_content_length:
                return article.text
                
        except Exception as e:
            logger.debug(f"Error extracting article content from {url}: {e}")
        
        return ""
    
    def _create_knowledge_entry(self, url: str, title: str, content: str, 
                              source_type: str, extraction_method: str) -> Optional[KnowledgeEntry]:
        """Create a knowledge entry with analysis"""
        
        if len(content) < self.min_content_length:
            return None
        
        # Content hash for deduplication
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if content_hash in [entry.content_hash for entry in self.knowledge_cache.values()]:
            return None
        
        # Extract keywords
        keywords = self._extract_keywords(content)
        
        # Categorize content
        category = self._categorize_content(content, keywords)
        
        # Quality assessment
        quality_score = self._assess_quality(content, title, source_type)
        
        # Relevance to SAIGE's interests
        relevance_score = self._assess_relevance(content, keywords, category)
        
        # Difficulty level
        difficulty_level = self._assess_difficulty(content, keywords)
        
        # Language detection
        language = self._detect_language(content)
        
        return KnowledgeEntry(
            source_url=url,
            title=title,
            content=content[:self.max_content_length],  # Truncate if too long
            timestamp=time.time(),
            category=category,
            keywords=keywords,
            quality_score=quality_score,
            relevance_score=relevance_score,
            difficulty_level=difficulty_level,
            source_type=source_type,
            language=language,
            content_hash=content_hash,
            extraction_method=extraction_method
        )
    
    def _extract_keywords(self, content: str) -> List[str]:
        """Extract important keywords from content"""
        try:
            # Tokenize and clean
            words = word_tokenize(content.lower())
            words = [self.lemmatizer.lemmatize(word) for word in words 
                    if word.isalnum() and word not in self.stop_words and len(word) > 3]
            
            # Get most frequent meaningful terms
            word_freq = Counter(words)
            
            # Also extract noun phrases using TextBlob
            blob = TextBlob(content)
            noun_phrases = [phrase.lower() for phrase in blob.noun_phrases 
                          if len(phrase.split()) <= 3]
            
            # Combine and rank
            keywords = []

            # Extract words from word frequency counter
            for word, count in word_freq.most_common(10):
                if isinstance(word, str) and word.strip():
                    keywords.append(word)

            # Extract phrases from noun phrases counter
            noun_phrase_counts = Counter(noun_phrases)
            for phrase, count in noun_phrase_counts.most_common(5):
                if isinstance(phrase, str) and phrase.strip():
                    keywords.append(phrase)

            return list(set(keywords))[:15]  # Limit to 15 keywords
            
        except Exception as e:
            logger.error(f"Error extracting keywords: {e}")
            return []
    
    def _categorize_content(self, content: str, keywords: List[str]) -> str:
        """Categorize content based on keywords and content analysis"""
        content_lower = content.lower()
        # Ensure all keywords are strings before joining
        safe_keywords = [str(k) for k in keywords if k is not None]
        keyword_text = ' '.join(safe_keywords).lower()
        
        category_scores = {}
        
        for category, category_keywords in self.categories.items():
            score = 0
            for keyword in category_keywords:
                # Direct content matches
                score += content_lower.count(keyword) * 2
                # Keyword matches
                score += keyword_text.count(keyword) * 3
            
            category_scores[category] = score
        
        # Return highest scoring category, or 'general' if no clear match
        if category_scores and max(category_scores.values()) > 0:
            return max(category_scores, key=category_scores.get)
        else:
            return 'general'
    
    def _assess_quality(self, content: str, title: str, source_type: str) -> float:
        """Assess content quality (0-1)"""
        score = 0.0
        
        # Length factor (reasonable content length)
        length_score = min(len(content) / 2000, 1.0) * 0.2
        score += length_score
        
        # Source type credibility
        source_scores = {
            'academic': 0.9,
            'documentation': 0.8,
            'encyclopedia': 0.8,
            'news': 0.6,
            'tutorial': 0.7,
            'blog': 0.4
        }
        score += source_scores.get(source_type, 0.5) * 0.3
        
        # Quality indicators in content
        quality_indicators = ['research', 'study', 'analysis', 'method', 'experiment', 
                            'theory', 'algorithm', 'implementation', 'results', 'conclusion']
        quality_count = sum(1 for indicator in quality_indicators if indicator in content.lower())
        quality_factor = min(quality_count / 5, 1.0) * 0.2
        score += quality_factor
        
        # Title informativeness
        title_words = len(title.split())
        title_score = min(title_words / 10, 1.0) * 0.1
        score += title_score
        
        # Structure indicators (headings, paragraphs)
        structure_score = 0.0
        if content.count('\n\n') > 3:  # Multiple paragraphs
            structure_score += 0.1
        if any(marker in content for marker in ['1.', '2.', '3.', '•', '-']):  # Lists
            structure_score += 0.1
        score += structure_score * 0.2
        
        return min(score, 1.0)
    
    def _assess_relevance(self, content: str, keywords: List[str], category: str) -> float:
        """Assess relevance to SAIGE's interests (0-1)"""
        
        # High-priority topics for SAIGE
        priority_topics = {
            'artificial_intelligence': 1.0,
            'robotics': 1.0,
            'neuroscience': 0.9,
            'space_science': 0.8,
            'blockchain': 0.8,
            'programming': 0.7,
            'mathematics': 0.6,
            'physics': 0.6,
            'engineering': 0.5,
            'biology': 0.4
        }
        
        base_relevance = priority_topics.get(category, 0.3)
        
        # SAIGE-specific keyword bonuses
        saige_keywords = [
            'autonomous', 'artificial intelligence', 'neural network', 'machine learning',
            'robotics', 'automation', 'blockchain', 'cryptocurrency', 'space', 'mars',
            'consciousness', 'evolution', 'synthetic biology', 'quantum', 'future'
        ]
        
        content_lower = content.lower()
        keyword_matches = sum(1 for keyword in saige_keywords if keyword in content_lower)
        keyword_bonus = min(keyword_matches * 0.1, 0.4)
        
        # Research vs practical content (SAIGE prefers practical)
        practical_indicators = ['implementation', 'tutorial', 'how to', 'example', 'code', 'practical']
        practical_score = sum(1 for indicator in practical_indicators if indicator in content_lower)
        practical_bonus = min(practical_score * 0.05, 0.2)
        
        total_relevance = base_relevance + keyword_bonus + practical_bonus
        return min(total_relevance, 1.0)
    
    def _assess_difficulty(self, content: str, keywords: List[str]) -> float:
        """Assess content difficulty level (0=beginner, 1=expert)"""
        
        # Technical complexity indicators
        technical_terms = ['algorithm', 'implementation', 'optimization', 'complexity', 
                          'theoretical', 'mathematical', 'statistical', 'computational']
        
        beginner_terms = ['introduction', 'basic', 'simple', 'tutorial', 'beginner', 
                         'overview', 'fundamentals', 'getting started']
        
        advanced_terms = ['advanced', 'complex', 'optimization', 'research', 'novel',
                         'sophisticated', 'state-of-the-art', 'cutting-edge']
        
        content_lower = content.lower()
        
        technical_score = sum(1 for term in technical_terms if term in content_lower)
        beginner_score = sum(1 for term in beginner_terms if term in content_lower)
        advanced_score = sum(1 for term in advanced_terms if term in content_lower)
        
        # Mathematical content indicators
        math_indicators = ['equation', 'formula', 'theorem', 'proof', 'derivative', 'integral']
        math_score = sum(1 for indicator in math_indicators if indicator in content_lower)
        
        # Code complexity
        code_indicators = ['function', 'class', 'import', 'def', 'return', 'if', 'for']
        code_score = sum(1 for indicator in code_indicators if indicator in content_lower)
        
        difficulty = (technical_score * 0.2 + advanced_score * 0.3 + math_score * 0.2 + 
                     code_score * 0.1 - beginner_score * 0.2) / 10
        
        return max(0.0, min(difficulty, 1.0))
    
    def _detect_language(self, content: str) -> str:
        """Detect content language"""
        try:
            blob = TextBlob(content[:1000])  # Sample first 1000 chars
            return blob.detect_language()
        except:
            return 'en'  # Default to English
    
    def generate_stimulus(self, knowledge_entries: List[KnowledgeEntry]) -> Dict[str, float]:
        """Generate hormone stimulus based on knowledge acquisition"""
        
        if not knowledge_entries:
            return self._default_stimulus()
        
        # Analyze knowledge patterns
        avg_quality = np.mean([entry.quality_score for entry in knowledge_entries])
        avg_relevance = np.mean([entry.relevance_score for entry in knowledge_entries])
        avg_difficulty = np.mean([entry.difficulty_level for entry in knowledge_entries])
        
        # Category distribution
        categories = [entry.category for entry in knowledge_entries]
        category_diversity = len(set(categories)) / len(categories)
        
        # Source type analysis
        source_types = [entry.source_type for entry in knowledge_entries]
        academic_ratio = source_types.count('academic') / len(source_types)
        
        # Generate stimulus
        stimulus = {}
        
        # Curiosity (adrenaline): High quality diverse content
        curiosity = (avg_quality * 0.4) + (category_diversity * 0.3) + (avg_relevance * 0.3)
        stimulus['adrenaline'] = min(curiosity, 1.0)
        
        # Satisfaction (dopamine): Relevant, high-quality learning
        satisfaction = (avg_relevance * 0.5) + (avg_quality * 0.3) + (academic_ratio * 0.2)
        stimulus['dopamine'] = min(satisfaction, 1.0)
        
        # Focus (serotonin): Consistent learning pattern
        learning_consistency = min(len(knowledge_entries) / 10, 1.0)  # Up to 10 articles = full focus
        stimulus['serotonin'] = learning_consistency
        
        # Stress (cortisol): Difficult content or poor quality
        stress = (avg_difficulty * 0.6) + ((1.0 - avg_quality) * 0.4)
        stimulus['cortisol'] = min(stress, 1.0)
        
        # Connection (oxytocin): Diverse sources and collaborative content
        connection = category_diversity * 0.7 + (len(set(source_types)) / 5) * 0.3
        stimulus['oxytocin'] = min(connection, 1.0)
        
        return stimulus
    
    def _default_stimulus(self) -> Dict[str, float]:
        """Default stimulus when no knowledge acquired"""
        return {
            'adrenaline': 0.2,  # Low curiosity
            'serotonin': 0.3,   # Neutral focus
            'dopamine': 0.1,    # Minimal satisfaction
            'cortisol': 0.2,    # Slight stress from lack of learning
            'oxytocin': 0.1     # Minimal connection
        }
    
    def save_knowledge(self, knowledge_entries: List[KnowledgeEntry]):
        """Save acquired knowledge to file"""
        try:
            # Convert to serializable format
            knowledge_data = [asdict(entry) for entry in knowledge_entries]
            
            # Load existing knowledge
            output_file = self.config["output_file"]
            existing_knowledge = []
            
            if os.path.exists(output_file):
                with open(output_file, 'r') as f:
                    existing_data = json.load(f)
                    existing_knowledge = existing_data.get('knowledge_entries', [])
            
            # Merge and deduplicate
            all_knowledge = existing_knowledge + knowledge_data
            unique_knowledge = []
            seen_hashes = set()
            
            for entry in all_knowledge:
                if entry['content_hash'] not in seen_hashes:
                    unique_knowledge.append(entry)
                    seen_hashes.add(entry['content_hash'])
            
            # Keep only recent entries (last 1000)
            unique_knowledge.sort(key=lambda x: x['timestamp'], reverse=True)
            unique_knowledge = unique_knowledge[:1000]
            
            # Save
            final_data = {
                'last_updated': time.time(),
                'total_entries': len(unique_knowledge),
                'knowledge_entries': unique_knowledge,
                'statistics': self._generate_statistics(unique_knowledge)
            }
            
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(final_data, f, indent=2)
            
            logger.info(f"Saved {len(unique_knowledge)} knowledge entries")
            
        except Exception as e:
            logger.error(f"Error saving knowledge: {e}")
    
    def _generate_statistics(self, knowledge_entries: List[Dict]) -> Dict:
        """Generate statistics about acquired knowledge"""
        if not knowledge_entries:
            return {}
        
        categories = [entry['category'] for entry in knowledge_entries]
        source_types = [entry['source_type'] for entry in knowledge_entries]
        
        return {
            'category_distribution': dict(Counter(categories)),
            'source_type_distribution': dict(Counter(source_types)),
            'avg_quality_score': np.mean([entry['quality_score'] for entry in knowledge_entries]),
            'avg_relevance_score': np.mean([entry['relevance_score'] for entry in knowledge_entries]),
            'avg_difficulty_level': np.mean([entry['difficulty_level'] for entry in knowledge_entries]),
            'language_distribution': dict(Counter([entry['language'] for entry in knowledge_entries]))
        }
    
    def run_research_cycle(self) -> List[KnowledgeEntry]:
        """Run one complete research cycle"""
        logger.info("Starting web research cycle...")
        
        all_knowledge = []
        
        # Scrape RSS feeds
        rss_knowledge = self.scrape_rss_feeds()
        all_knowledge.extend(rss_knowledge)
        logger.info(f"Found {len(rss_knowledge)} entries from RSS feeds")
        
        # Search educational content
        search_knowledge = self.search_educational_content()
        all_knowledge.extend(search_knowledge)
        logger.info(f"Found {len(search_knowledge)} entries from searches")
        
        # Filter by quality and relevance
        filtered_knowledge = [
            entry for entry in all_knowledge
            if entry.quality_score >= self.config["quality_threshold"] and
               entry.relevance_score >= self.config["relevance_threshold"]
        ]
        
        # Limit total entries
        if len(filtered_knowledge) > self.config["max_articles_per_run"]:
            # Sort by relevance * quality and take top entries
            filtered_knowledge.sort(
                key=lambda x: x.relevance_score * x.quality_score, 
                reverse=True
            )
            filtered_knowledge = filtered_knowledge[:self.config["max_articles_per_run"]]
        
        logger.info(f"Acquired {len(filtered_knowledge)} high-quality knowledge entries")
        
        return filtered_knowledge
    
    def run_continuous(self):
        """Run continuous web research monitoring"""
        logger.info("Starting continuous web research...")
        
        while True:
            try:
                # Run research cycle
                knowledge_entries = self.run_research_cycle()
                
                # Generate stimulus
                stimulus = self.generate_stimulus(knowledge_entries)
                
                # Save knowledge
                self.save_knowledge(knowledge_entries)
                
                # Save stimulus data
                stimulus_data = {
                    "timestamp": time.time(),
                    "source": "web_research_feeder",
                    "stimulus": stimulus,
                    "metadata": {
                        "entries_acquired": len(knowledge_entries),
                        "avg_quality": np.mean([e.quality_score for e in knowledge_entries]) if knowledge_entries else 0,
                        "avg_relevance": np.mean([e.relevance_score for e in knowledge_entries]) if knowledge_entries else 0,
                        "categories": list(set([e.category for e in knowledge_entries]))
                    }
                }
                
                os.makedirs(os.path.dirname(self.config["stimulus_output"]), exist_ok=True)
                with open(self.config["stimulus_output"], 'w') as f:
                    json.dump(stimulus_data, f, indent=2)
                
                logger.info(f"Generated research stimulus: {stimulus}")
                
                # Wait for next cycle
                time.sleep(self.config["update_interval"])
                
            except KeyboardInterrupt:
                logger.info("Web research feeder stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in web research cycle: {e}")
                time.sleep(60)  # Wait before retry

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = WebResearchFeeder()
    
    # Test mode: run single research cycle
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        knowledge_entries = feeder.run_research_cycle()
        stimulus = feeder.generate_stimulus(knowledge_entries)
        print(f"Acquired {len(knowledge_entries)} knowledge entries")
        print(f"Generated stimulus: {stimulus}")
        feeder.save_knowledge(knowledge_entries)
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()