#!/usr/bin/env python3
"""
News Feeder - SAIGE World Awareness Pipeline
Monitors real-time news and current events to maintain world model
Real implementation with sentiment analysis and trend detection
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
from collections import defaultdict, Counter, deque
import threading
import queue

# News sources and APIs
import feedparser
from bs4 import BeautifulSoup
import newspaper
from newspaper import Article

# Text processing and sentiment analysis
import nltk
from textblob import TextBlob
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
import numpy as np

# Named entity recognition
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except (ImportError, OSError):
    SPACY_AVAILABLE = False
    logging.warning("spaCy not available - using simplified entity extraction")

# Download required NLTK data
for dataset in ['punkt', 'stopwords', 'averaged_perceptron_tagger', 'vader_lexicon']:
    try:
        nltk.data.find(f'tokenizers/{dataset}')
    except LookupError:
        try:
            nltk.download(dataset)
        except:
            pass

from nltk.sentiment import SentimentIntensityAnalyzer
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize, sent_tokenize

logger = logging.getLogger(__name__)

@dataclass
class NewsArticle:
    url: str
    title: str
    content: str
    summary: str
    timestamp: float
    publish_date: str
    source: str
    category: str
    sentiment_score: float  # -1 (negative) to 1 (positive)
    importance_score: float  # 0-1 based on content analysis
    keywords: List[str]
    entities: List[Dict[str, str]]  # Named entities (people, places, organizations)
    topics: List[str]
    emotional_impact: str  # 'neutral', 'positive', 'negative', 'alarming', 'inspiring'
    credibility_score: float  # 0-1 based on source and content analysis
    content_hash: str
    geographic_focus: List[str]  # Countries/regions mentioned
    temporal_relevance: float  # How time-sensitive the news is

@dataclass
class NewsTrend:
    topic: str
    article_count: int
    total_importance: float
    avg_sentiment: float
    first_seen: float
    last_updated: float
    growth_rate: float  # Articles per hour
    geographic_spread: List[str]
    key_entities: List[str]

class NewsFeeder:
    """
    Monitors news sources and processes current events for SAIGE's world awareness
    """
    
    def __init__(self, config_path: str = "config/news_feeder.json"):
        self.config = self._load_config(config_path)
        
        # Sentiment analysis
        self.sentiment_analyzer = SentimentIntensityAnalyzer()
        
        # Text processing
        self.stop_words = set(stopwords.words('english'))
        self.vectorizer = TfidfVectorizer(max_features=500, stop_words='english')
        
        # News tracking
        self.processed_articles = set()  # URLs already processed
        self.article_memory = deque(maxlen=2000)  # Recent articles
        self.trend_tracker = {}  # Topic -> NewsTrend
        
        # World model categories
        self.world_categories = {
            'technology': ['ai', 'artificial intelligence', 'robot', 'computer', 'internet', 'space', 'science'],
            'politics': ['government', 'election', 'policy', 'law', 'congress', 'president', 'minister'],
            'economy': ['market', 'stock', 'economy', 'financial', 'trade', 'business', 'crypto'],
            'environment': ['climate', 'weather', 'pollution', 'renewable', 'carbon', 'environment'],
            'health': ['health', 'medical', 'disease', 'vaccine', 'hospital', 'doctor', 'covid'],
            'society': ['social', 'culture', 'education', 'protest', 'rights', 'community'],
            'international': ['war', 'conflict', 'diplomacy', 'trade', 'alliance', 'country', 'global'],
            'disasters': ['earthquake', 'flood', 'fire', 'hurricane', 'disaster', 'emergency', 'crisis']
        }
        
        # Geographic regions for world awareness
        self.regions = {
            'north_america': ['usa', 'canada', 'mexico', 'united states', 'america'],
            'europe': ['eu', 'europe', 'germany', 'france', 'uk', 'britain', 'italy', 'spain'],
            'asia': ['china', 'japan', 'india', 'korea', 'asia', 'singapore', 'thailand'],
            'middle_east': ['israel', 'iran', 'saudi', 'turkey', 'middle east', 'palestine'],
            'africa': ['africa', 'south africa', 'nigeria', 'egypt', 'kenya', 'ghana'],
            'oceania': ['australia', 'new zealand', 'pacific'],
            'south_america': ['brazil', 'argentina', 'chile', 'colombia', 'venezuela']
        }
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "news_sources": {
                "rss_feeds": [
                    "http://rss.cnn.com/rss/edition.rss",
                    "https://feeds.bbci.co.uk/news/world/rss.xml",
                    "https://www.npr.org/rss/rss.php?id=1001",
                    "https://rss.reuters.com/reuters/topNews",
                    "https://feeds.feedburner.com/ap/topstories",
                    "https://www.aljazeera.com/xml/rss/all.xml",
                    "https://www.theguardian.com/world/rss",
                    "https://www.washingtonpost.com/rss/",
                    "https://feeds.nytimes.com/nyt/rss/World",
                    "https://feeds.feedburner.com/time/topstories"
                ],
                "tech_feeds": [
                    "https://feeds.feedburner.com/oreilly/radar",
                    "https://www.technologyreview.com/feed/",
                    "https://spectrum.ieee.org/feeds/blog.rss",
                    "https://feeds.feedburner.com/venturebeat/SZYF",
                    "https://techcrunch.com/feed/"
                ],
                "science_feeds": [
                    "https://www.nature.com/nature.rss",
                    "https://www.sciencemag.org/rss/news_current.xml",
                    "https://feeds.feedburner.com/ScienceDaily",
                    "https://www.space.com/feeds/all"
                ]
            },
            "news_apis": {
                "newsapi": {
                    "enabled": False,  # Requires API key
                    "api_key": "",
                    "endpoints": {
                        "top_headlines": "https://newsapi.org/v2/top-headlines",
                        "everything": "https://newsapi.org/v2/everything"
                    }
                }
            },
            "processing": {
                "update_interval": 1800,  # 30 minutes
                "max_articles_per_cycle": 100,
                "min_article_length": 300,
                "max_article_age_hours": 48,
                "sentiment_threshold": 0.1,
                "importance_threshold": 0.3,
                "trend_min_articles": 3,
                "trend_window_hours": 24
            },
            "world_model": {
                "track_entities": True,
                "track_geographic_mentions": True,
                "track_temporal_patterns": True,
                "credibility_scoring": True,
                "emotional_impact_analysis": True
            },
            "output": {
                "articles_data": "data/news_articles.json",
                "trends_data": "data/news_trends.json", 
                "world_model_data": "data/world_model.json",
                "stimulus_output": "data/news_stimulus.json"
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
    
    def fetch_news_from_rss(self) -> List[NewsArticle]:
        """Fetch news articles from RSS feeds
        DISABLED: All RSS feeds removed per API reduction policy
        Keeping only: arxiv, pubmed, github, mit, openlibrary
        """
        logger.info("News RSS feeds DISABLED - all external news APIs removed")
        return []  # Return empty results
    
    def _extract_article_content(self, url: str) -> str:
        """Extract clean article content from URL"""
        try:
            # SSRF protection
            from repryntt.search.url_guard import validate_url
            url = validate_url(url)

            article = Article(url)
            article.download()
            article.parse()
            
            # Use newspaper3k's text extraction
            content = article.text
            
            # Fallback to manual extraction if newspaper3k fails
            if len(content) < 100:
                response = requests.get(url, timeout=10, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Remove unwanted elements
                for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    element.decompose()
                
                # Try common content selectors
                content_selectors = ['article', '.article-content', '.content', '.story', '.post-content']
                for selector in content_selectors:
                    content_elem = soup.select_one(selector)
                    if content_elem:
                        content = content_elem.get_text(strip=True)
                        break
                
                if not content:
                    # Last resort: get all paragraph text
                    paragraphs = soup.find_all('p')
                    content = ' '.join([p.get_text(strip=True) for p in paragraphs])
            
            # Clean up content
            content = re.sub(r'\s+', ' ', content).strip()
            
            return content
            
        except Exception as e:
            logger.debug(f"Error extracting content from {url}: {e}")
            return ""
    
    def _create_news_article(self, url: str, title: str, content: str, 
                           source: str, publish_date: str) -> Optional[NewsArticle]:
        """Create a news article with full analysis"""
        
        if len(content) < self.config["processing"]["min_article_length"]:
            return None
        
        # Content hash for deduplication
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if any(article.content_hash == content_hash for article in self.article_memory):
            return None
        
        timestamp = time.time()
        
        # Generate summary
        summary = self._generate_summary(content)
        
        # Sentiment analysis
        sentiment_score = self._analyze_sentiment(content)
        
        # Importance scoring
        importance_score = self._assess_importance(title, content, source)
        
        # Extract keywords
        keywords = self._extract_keywords(content)
        
        # Named entity recognition
        entities = self._extract_entities(content)
        
        # Topic classification
        topics = self._classify_topics(content, keywords)
        
        # Emotional impact assessment
        emotional_impact = self._assess_emotional_impact(content, sentiment_score)
        
        # Credibility scoring
        credibility_score = self._assess_credibility(source, content, entities)
        
        # Geographic focus
        geographic_focus = self._extract_geographic_focus(content, entities)
        
        # Temporal relevance
        temporal_relevance = self._assess_temporal_relevance(content, title)
        
        # Categorize
        category = self._categorize_article(content, keywords, topics)
        
        return NewsArticle(
            url=url,
            title=title,
            content=content[:5000],  # Truncate very long articles
            summary=summary,
            timestamp=timestamp,
            publish_date=publish_date,
            source=source,
            category=category,
            sentiment_score=sentiment_score,
            importance_score=importance_score,
            keywords=keywords,
            entities=entities,
            topics=topics,
            emotional_impact=emotional_impact,
            credibility_score=credibility_score,
            content_hash=content_hash,
            geographic_focus=geographic_focus,
            temporal_relevance=temporal_relevance
        )
    
    def _generate_summary(self, content: str, max_sentences: int = 3) -> str:
        """Generate article summary"""
        try:
            sentences = sent_tokenize(content)
            if len(sentences) <= max_sentences:
                return content
            
            # Simple extractive summarization based on sentence position and length
            sentence_scores = []
            for i, sentence in enumerate(sentences):
                score = 0
                
                # Position bias (first and last sentences often important)
                if i < 2:
                    score += 0.3
                elif i >= len(sentences) - 2:
                    score += 0.2
                
                # Length bias (not too short, not too long)
                words = len(sentence.split())
                if 10 <= words <= 25:
                    score += 0.2
                
                # Keyword presence
                sentence_lower = sentence.lower()
                if any(keyword in sentence_lower for keyword in ['said', 'according', 'reported']):
                    score += 0.1
                
                sentence_scores.append((score, sentence))
            
            # Select top sentences
            sentence_scores.sort(reverse=True)
            top_sentences = [sent for _, sent in sentence_scores[:max_sentences]]
            
            # Sort by original order
            original_order = []
            for sentence in top_sentences:
                try:
                    idx = sentences.index(sentence)
                    original_order.append((idx, sentence))
                except ValueError:
                    continue
            
            original_order.sort()
            summary = ' '.join([sent for _, sent in original_order])
            
            return summary
            
        except Exception as e:
            logger.error(f"Error generating summary: {e}")
            return content[:500] + "..."
    
    def _analyze_sentiment(self, content: str) -> float:
        """Analyze sentiment of article content"""
        try:
            # VADER sentiment analysis
            vader_scores = self.sentiment_analyzer.polarity_scores(content)
            vader_sentiment = vader_scores['compound']
            
            # TextBlob sentiment analysis
            blob = TextBlob(content)
            textblob_sentiment = blob.sentiment.polarity
            
            # Combine both methods (VADER is better for social media/news)
            combined_sentiment = (vader_sentiment * 0.7) + (textblob_sentiment * 0.3)
            
            return max(-1.0, min(combined_sentiment, 1.0))
            
        except Exception as e:
            logger.error(f"Error analyzing sentiment: {e}")
            return 0.0
    
    def _assess_importance(self, title: str, content: str, source: str) -> float:
        """Assess importance of the article (0-1)"""
        try:
            importance = 0.0
            
            # Source credibility factor
            trusted_sources = ['reuters', 'bbc', 'ap', 'npr', 'nature', 'science']
            if any(trusted in source.lower() for trusted in trusted_sources):
                importance += 0.2
            
            # Title indicators
            title_lower = title.lower()
            urgent_words = ['breaking', 'urgent', 'crisis', 'disaster', 'war', 'dies', 'killed']
            importance_words = ['major', 'significant', 'historic', 'unprecedented', 'groundbreaking']
            
            importance += sum(0.1 for word in urgent_words if word in title_lower)
            importance += sum(0.05 for word in importance_words if word in title_lower)
            
            # Content length and structure
            word_count = len(content.split())
            if word_count > 500:
                importance += 0.1
            if word_count > 1000:
                importance += 0.1
            
            # Quote and source indicators (well-researched articles)
            quote_count = content.count('"')
            importance += min(quote_count * 0.01, 0.1)
            
            # Numbers and statistics (data-driven reporting)
            number_count = len(re.findall(r'\b\d+(?:\.\d+)?%?\b', content))
            importance += min(number_count * 0.005, 0.1)
            
            # Global scope indicators
            global_words = ['world', 'global', 'international', 'countries', 'nations']
            importance += sum(0.02 for word in global_words if word in content.lower())
            
            return max(0.0, min(importance, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing importance: {e}")
            return 0.5
    
    def _extract_keywords(self, content: str, max_keywords: int = 15) -> List[str]:
        """Extract important keywords from content"""
        try:
            # Tokenize and clean
            words = word_tokenize(content.lower())
            words = [word for word in words if word.isalnum() and 
                    word not in self.stop_words and len(word) > 3]
            
            # Count frequency
            word_freq = Counter(words)
            
            # Get noun phrases from TextBlob
            blob = TextBlob(content)
            noun_phrases = [phrase.lower() for phrase in blob.noun_phrases 
                          if len(phrase.split()) <= 3 and len(phrase) > 3]
            phrase_freq = Counter(noun_phrases)
            
            # Combine single words and phrases
            keywords = []
            keywords.extend([word for word, count in word_freq.most_common(10)])
            keywords.extend([phrase for phrase, count in phrase_freq.most_common(8)])
            
            return list(set(keywords))[:max_keywords]
            
        except Exception as e:
            logger.error(f"Error extracting keywords: {e}")
            return []
    
    def _extract_entities(self, content: str) -> List[Dict[str, str]]:
        """Extract named entities from content"""
        entities = []
        
        try:
            if SPACY_AVAILABLE:
                # Use spaCy for better entity recognition
                doc = nlp(content[:10000])  # Limit length for processing
                
                for ent in doc.ents:
                    if ent.label_ in ['PERSON', 'ORG', 'GPE', 'LOC', 'EVENT']:
                        entities.append({
                            'text': ent.text,
                            'type': ent.label_,
                            'confidence': 0.9  # spaCy is generally reliable
                        })
            else:
                # Fallback: simple pattern-based entity extraction
                
                # Country names (simplified list)
                countries = ['china', 'russia', 'india', 'japan', 'germany', 'france', 'britain', 
                           'italy', 'spain', 'canada', 'australia', 'brazil', 'mexico', 'iran',
                           'israel', 'egypt', 'saudi arabia', 'turkey', 'south africa', 'nigeria']
                
                content_lower = content.lower()
                for country in countries:
                    if country in content_lower:
                        entities.append({
                            'text': country.title(),
                            'type': 'GPE',
                            'confidence': 0.7
                        })
                
                # Organizations (simple patterns)
                org_patterns = [r'\b([A-Z][A-Za-z]+ (?:Corp|Inc|Ltd|LLC|Company|Organization|Agency))\b',
                              r'\b(UN|WHO|NATO|EU|IMF|UNESCO|UNICEF)\b']
                
                for pattern in org_patterns:
                    matches = re.findall(pattern, content)
                    for match in matches:
                        entities.append({
                            'text': match,
                            'type': 'ORG',
                            'confidence': 0.6
                        })
            
            # Deduplicate entities
            seen = set()
            unique_entities = []
            for entity in entities:
                key = (entity['text'].lower(), entity['type'])
                if key not in seen:
                    seen.add(key)
                    unique_entities.append(entity)
            
            return unique_entities[:20]  # Limit number of entities
            
        except Exception as e:
            logger.error(f"Error extracting entities: {e}")
            return []
    
    def _classify_topics(self, content: str, keywords: List[str]) -> List[str]:
        """Classify article topics"""
        topics = []
        content_lower = content.lower()
        keyword_text = ' '.join(keywords).lower()
        
        for category, category_keywords in self.world_categories.items():
            score = 0
            for keyword in category_keywords:
                score += content_lower.count(keyword) * 2
                score += keyword_text.count(keyword) * 3
            
            if score > 0:
                topics.append((category, score))
        
        # Sort by score and return top topics
        topics.sort(key=lambda x: x[1], reverse=True)
        return [topic for topic, score in topics[:3]]
    
    def _assess_emotional_impact(self, content: str, sentiment_score: float) -> str:
        """Assess emotional impact of the article"""
        try:
            content_lower = content.lower()
            
            # Check for alarming content
            alarm_words = ['crisis', 'disaster', 'emergency', 'killed', 'died', 'injured', 'attack', 
                          'war', 'conflict', 'threat', 'danger', 'urgent', 'critical']
            alarm_count = sum(1 for word in alarm_words if word in content_lower)
            
            # Check for inspiring content
            inspire_words = ['breakthrough', 'discovery', 'achievement', 'success', 'victory', 
                           'progress', 'innovation', 'solution', 'hope', 'heal', 'cure']
            inspire_count = sum(1 for word in inspire_words if word in content_lower)
            
            if alarm_count >= 3 or sentiment_score < -0.5:
                return 'alarming'
            elif inspire_count >= 2 or sentiment_score > 0.5:
                return 'inspiring'
            elif sentiment_score > 0.1:
                return 'positive'
            elif sentiment_score < -0.1:
                return 'negative'
            else:
                return 'neutral'
                
        except Exception as e:
            logger.error(f"Error assessing emotional impact: {e}")
            return 'neutral'
    
    def _assess_credibility(self, source: str, content: str, entities: List[Dict]) -> float:
        """Assess credibility of the article"""
        try:
            credibility = 0.5  # Base credibility
            
            # Source reputation
            high_credibility_sources = ['reuters', 'bbc', 'ap', 'npr', 'nature', 'science', 
                                      'new york times', 'washington post', 'guardian']
            medium_credibility_sources = ['cnn', 'abc', 'cbs', 'nbc', 'time', 'newsweek']
            
            source_lower = source.lower()
            if any(trusted in source_lower for trusted in high_credibility_sources):
                credibility += 0.3
            elif any(medium in source_lower for medium in medium_credibility_sources):
                credibility += 0.2
            
            # Content quality indicators
            if len(content.split()) > 300:  # Substantial content
                credibility += 0.1
            
            # Presence of quotes (indicates sourcing)
            quote_count = content.count('"')
            if quote_count > 0:
                credibility += min(quote_count * 0.02, 0.1)
            
            # Named entities (proper nouns suggest factual reporting)
            if len(entities) > 3:
                credibility += 0.1
            
            # Statistical information
            number_count = len(re.findall(r'\b\d+(?:\.\d+)?%?\b', content))
            if number_count > 5:
                credibility += 0.1
            
            return max(0.0, min(credibility, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing credibility: {e}")
            return 0.5
    
    def _extract_geographic_focus(self, content: str, entities: List[Dict]) -> List[str]:
        """Extract geographic focus from content"""
        geographic_mentions = []
        content_lower = content.lower()
        
        # From entities
        for entity in entities:
            if entity['type'] in ['GPE', 'LOC']:  # Geopolitical entities, locations
                geographic_mentions.append(entity['text'].lower())
        
        # From region keywords
        for region, keywords in self.regions.items():
            for keyword in keywords:
                if keyword in content_lower:
                    geographic_mentions.append(region)
                    break
        
        return list(set(geographic_mentions))[:5]
    
    def _assess_temporal_relevance(self, content: str, title: str) -> float:
        """Assess how time-sensitive the news is"""
        try:
            text = (title + ' ' + content).lower()
            
            # Breaking news indicators
            urgent_indicators = ['breaking', 'just in', 'developing', 'live', 'now', 'today']
            urgency_score = sum(0.2 for indicator in urgent_indicators if indicator in text)
            
            # Time-sensitive events
            time_sensitive = ['election', 'vote', 'deadline', 'expires', 'emergency', 'crisis']
            time_score = sum(0.15 for event in time_sensitive if event in text)
            
            # Ongoing vs completed events
            ongoing_indicators = ['continues', 'ongoing', 'still', 'remains', 'persists']
            completed_indicators = ['completed', 'finished', 'ended', 'concluded', 'announced']
            
            ongoing_score = sum(0.1 for indicator in ongoing_indicators if indicator in text)
            completed_score = sum(-0.1 for indicator in completed_indicators if indicator in text)
            
            temporal_relevance = urgency_score + time_score + ongoing_score + completed_score
            
            return max(0.0, min(temporal_relevance, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing temporal relevance: {e}")
            return 0.5
    
    def _categorize_article(self, content: str, keywords: List[str], topics: List[str]) -> str:
        """Categorize article into main category"""
        if topics:
            return topics[0]  # Return top topic
        
        # Fallback categorization
        content_lower = content.lower()
        keyword_text = ' '.join(keywords).lower()
        
        category_scores = {}
        for category, category_keywords in self.world_categories.items():
            score = sum(1 for keyword in category_keywords 
                       if keyword in content_lower or keyword in keyword_text)
            category_scores[category] = score
        
        if category_scores:
            return max(category_scores, key=category_scores.get)
        else:
            return 'general'
    
    def update_trends(self, articles: List[NewsArticle]):
        """Update trend tracking with new articles"""
        current_time = time.time()
        trend_window = self.config["processing"]["trend_window_hours"] * 3600
        
        # Clean old trends
        expired_trends = []
        for topic, trend in self.trend_tracker.items():
            if current_time - trend.last_updated > trend_window:
                expired_trends.append(topic)
        
        for topic in expired_trends:
            del self.trend_tracker[topic]
        
        # Process new articles
        for article in articles:
            if article.importance_score < self.config["processing"]["importance_threshold"]:
                continue
            
            # Update trends for each topic/keyword
            for keyword in article.keywords[:5]:  # Top keywords only
                if len(keyword) < 4:  # Skip short keywords
                    continue
                
                if keyword in self.trend_tracker:
                    trend = self.trend_tracker[keyword]
                    trend.article_count += 1
                    trend.total_importance += article.importance_score
                    trend.avg_sentiment = ((trend.avg_sentiment * (trend.article_count - 1)) + 
                                         article.sentiment_score) / trend.article_count
                    trend.last_updated = current_time
                    
                    # Update growth rate
                    time_span = (current_time - trend.first_seen) / 3600  # hours
                    trend.growth_rate = trend.article_count / max(time_span, 1)
                    
                    # Update geographic spread
                    trend.geographic_spread = list(set(trend.geographic_spread + article.geographic_focus))
                    
                    # Update key entities
                    entity_names = [e['text'] for e in article.entities]
                    trend.key_entities = list(set(trend.key_entities + entity_names))[:10]
                    
                else:
                    # Create new trend
                    self.trend_tracker[keyword] = NewsTrend(
                        topic=keyword,
                        article_count=1,
                        total_importance=article.importance_score,
                        avg_sentiment=article.sentiment_score,
                        first_seen=current_time,
                        last_updated=current_time,
                        growth_rate=1.0,
                        geographic_spread=article.geographic_focus,
                        key_entities=[e['text'] for e in article.entities][:5]
                    )
    
    def get_trending_topics(self, min_articles: int = None) -> List[NewsTrend]:
        """Get current trending topics"""
        if min_articles is None:
            min_articles = self.config["processing"]["trend_min_articles"]
        
        # Filter and sort trends
        significant_trends = [
            trend for trend in self.trend_tracker.values()
            if trend.article_count >= min_articles
        ]
        
        # Sort by combination of growth rate and total importance
        significant_trends.sort(
            key=lambda t: (t.growth_rate * t.total_importance), 
            reverse=True
        )
        
        return significant_trends[:20]  # Top 20 trends
    
    def generate_world_model_update(self, articles: List[NewsArticle]) -> Dict:
        """Generate world model update based on processed articles"""
        current_time = time.time()
        
        # Categorize articles by region and topic
        regional_updates = defaultdict(list)
        topical_updates = defaultdict(list)
        
        for article in articles:
            # Regional updates
            for region in article.geographic_focus:
                regional_updates[region].append({
                    'title': article.title,
                    'sentiment': article.sentiment_score,
                    'importance': article.importance_score,
                    'category': article.category,
                    'emotional_impact': article.emotional_impact,
                    'timestamp': article.timestamp
                })
            
            # Topical updates
            for topic in article.topics:
                topical_updates[topic].append({
                    'title': article.title,
                    'sentiment': article.sentiment_score,
                    'importance': article.importance_score,
                    'geographic_focus': article.geographic_focus,
                    'timestamp': article.timestamp
                })
        
        # Calculate regional sentiment and activity
        regional_summary = {}
        for region, region_articles in regional_updates.items():
            avg_sentiment = np.mean([a['sentiment'] for a in region_articles])
            total_importance = sum([a['importance'] for a in region_articles])
            activity_level = len(region_articles)
            
            regional_summary[region] = {
                'activity_level': activity_level,
                'avg_sentiment': float(avg_sentiment),
                'total_importance': float(total_importance),
                'dominant_categories': [a['category'] for a in region_articles],
                'emotional_climate': Counter([a['emotional_impact'] for a in region_articles]).most_common(1)[0][0]
            }
        
        # Calculate topical trends
        topical_summary = {}
        for topic, topic_articles in topical_updates.items():
            avg_sentiment = np.mean([a['sentiment'] for a in topic_articles])
            total_importance = sum([a['importance'] for a in topic_articles])
            geographic_spread = []
            for a in topic_articles:
                geographic_spread.extend(a['geographic_focus'])
            
            topical_summary[topic] = {
                'article_count': len(topic_articles),
                'avg_sentiment': float(avg_sentiment),
                'total_importance': float(total_importance),
                'geographic_spread': list(set(geographic_spread)),
                'recent_activity': len([a for a in topic_articles if current_time - a['timestamp'] < 3600])  # Last hour
            }
        
        # Get trending topics
        trending_topics = self.get_trending_topics()
        
        return {
            'timestamp': current_time,
            'articles_processed': len(articles),
            'regional_updates': regional_summary,
            'topical_updates': topical_summary,
            'trending_topics': [asdict(trend) for trend in trending_topics],
            'global_sentiment': float(np.mean([a.sentiment_score for a in articles])) if articles else 0.0,
            'crisis_indicators': self._detect_crisis_indicators(articles),
            'positive_developments': self._detect_positive_developments(articles)
        }
    
    def _detect_crisis_indicators(self, articles: List[NewsArticle]) -> List[Dict]:
        """Detect potential crisis situations from news"""
        crisis_indicators = []
        
        crisis_keywords = ['crisis', 'emergency', 'disaster', 'conflict', 'war', 'attack', 
                          'pandemic', 'outbreak', 'collapse', 'threat']
        
        for article in articles:
            if (article.emotional_impact == 'alarming' and 
                article.importance_score > 0.6 and
                any(keyword in article.content.lower() for keyword in crisis_keywords)):
                
                crisis_indicators.append({
                    'title': article.title,
                    'category': article.category,
                    'geographic_focus': article.geographic_focus,
                    'sentiment': article.sentiment_score,
                    'importance': article.importance_score,
                    'credibility': article.credibility_score,
                    'url': article.url
                })
        
        return crisis_indicators[:5]  # Top 5 crisis indicators
    
    def _detect_positive_developments(self, articles: List[NewsArticle]) -> List[Dict]:
        """Detect positive developments and breakthroughs"""
        positive_developments = []
        
        positive_keywords = ['breakthrough', 'discovery', 'cure', 'solution', 'progress', 
                           'achievement', 'success', 'innovation', 'improvement']
        
        for article in articles:
            if (article.emotional_impact == 'inspiring' and 
                article.sentiment_score > 0.3 and
                any(keyword in article.content.lower() for keyword in positive_keywords)):
                
                positive_developments.append({
                    'title': article.title,
                    'category': article.category,
                    'sentiment': article.sentiment_score,
                    'importance': article.importance_score,
                    'credibility': article.credibility_score,
                    'url': article.url
                })
        
        return positive_developments[:5]  # Top 5 positive developments
    
    def generate_stimulus(self, articles: List[NewsArticle], world_model: Dict) -> Dict[str, float]:
        """Generate hormone stimulus based on news analysis"""
        
        if not articles:
            return self._default_stimulus()
        
        stimulus = {
            'adrenaline': 0.0,   # Breaking news, crises, urgent developments
            'serotonin': 0.0,    # Positive news, social harmony
            'dopamine': 0.0,     # Discoveries, achievements, progress
            'cortisol': 0.0,     # Stressful news, conflicts, disasters
            'oxytocin': 0.0      # Social connections, human interest stories
        }
        
        # Process each article
        for article in articles:
            weight = article.importance_score * article.credibility_score
            
            # Temporal relevance increases stimulus intensity
            temporal_factor = 1.0 + (article.temporal_relevance * 0.5)
            
            # Emotional impact mapping
            if article.emotional_impact == 'alarming':
                stimulus['adrenaline'] += weight * 0.4 * temporal_factor
                stimulus['cortisol'] += weight * 0.5 * temporal_factor
            elif article.emotional_impact == 'inspiring':
                stimulus['dopamine'] += weight * 0.5 * temporal_factor
                stimulus['serotonin'] += weight * 0.3 * temporal_factor
            elif article.emotional_impact == 'positive':
                stimulus['serotonin'] += weight * 0.4 * temporal_factor
                stimulus['dopamine'] += weight * 0.2 * temporal_factor
            elif article.emotional_impact == 'negative':
                stimulus['cortisol'] += weight * 0.3 * temporal_factor
            
            # Category-specific stimulus
            if article.category == 'technology':
                stimulus['dopamine'] += weight * 0.3  # Tech progress is rewarding
                stimulus['adrenaline'] += weight * 0.2  # Tech changes are exciting
            elif article.category == 'disasters':
                stimulus['cortisol'] += weight * 0.4  # Disasters cause stress
                stimulus['adrenaline'] += weight * 0.3  # Disasters require attention
            elif article.category == 'health':
                if article.sentiment_score > 0:
                    stimulus['serotonin'] += weight * 0.3  # Health improvements
                else:
                    stimulus['cortisol'] += weight * 0.2  # Health concerns
            elif article.category == 'society':
                stimulus['oxytocin'] += weight * 0.3  # Social connection
        
        # World model factors
        if world_model:
            # Global sentiment influence
            global_sentiment = world_model.get('global_sentiment', 0.0)
            if global_sentiment > 0.2:
                stimulus['serotonin'] += 0.2
            elif global_sentiment < -0.2:
                stimulus['cortisol'] += 0.2
            
            # Crisis indicators create high stress/alertness
            crisis_count = len(world_model.get('crisis_indicators', []))
            if crisis_count > 0:
                stimulus['adrenaline'] += min(crisis_count * 0.2, 0.5)
                stimulus['cortisol'] += min(crisis_count * 0.15, 0.4)
            
            # Positive developments boost satisfaction
            positive_count = len(world_model.get('positive_developments', []))
            if positive_count > 0:
                stimulus['dopamine'] += min(positive_count * 0.15, 0.4)
                stimulus['serotonin'] += min(positive_count * 0.1, 0.3)
            
            # High activity/trending topics increase curiosity
            trending_count = len(world_model.get('trending_topics', []))
            if trending_count > 3:
                stimulus['adrenaline'] += min((trending_count - 3) * 0.1, 0.3)
        
        # Normalize stimulus values
        for hormone in stimulus:
            stimulus[hormone] = max(0.0, min(stimulus[hormone], 1.0))
        
        return stimulus
    
    def _default_stimulus(self) -> Dict[str, float]:
        """Default stimulus when no news processed"""
        return {
            'adrenaline': 0.1,  # Minimal curiosity
            'serotonin': 0.2,   # Neutral mood
            'dopamine': 0.1,    # Minimal satisfaction
            'cortisol': 0.1,    # Minimal stress
            'oxytocin': 0.1     # Minimal connection
        }
    
    def save_data(self, articles: List[NewsArticle], world_model: Dict):
        """Save processed news data"""
        try:
            # Save articles
            articles_data = {
                'articles': [asdict(article) for article in articles],
                'last_updated': time.time(),
                'total_articles': len(articles)
            }
            
            os.makedirs(os.path.dirname(self.config["output"]["articles_data"]), exist_ok=True)
            with open(self.config["output"]["articles_data"], 'w') as f:
                json.dump(articles_data, f, indent=2)
            
            # Save trends
            trends_data = {
                'trends': [asdict(trend) for trend in self.get_trending_topics()],
                'last_updated': time.time()
            }
            
            with open(self.config["output"]["trends_data"], 'w') as f:
                json.dump(trends_data, f, indent=2)
            
            # Save world model
            with open(self.config["output"]["world_model_data"], 'w') as f:
                json.dump(world_model, f, indent=2)
            
            logger.info(f"Saved {len(articles)} articles and world model data")
            
        except Exception as e:
            logger.error(f"Error saving news data: {e}")
    
    def run_news_cycle(self) -> Dict[str, float]:
        """Run one complete news processing cycle"""
        logger.info("Starting news processing cycle...")
        
        # Fetch news articles
        articles = self.fetch_news_from_rss()
        
        # Filter by importance and recency
        filtered_articles = [
            article for article in articles
            if article.importance_score >= self.config["processing"]["importance_threshold"]
        ]
        
        # Limit total articles
        if len(filtered_articles) > self.config["processing"]["max_articles_per_cycle"]:
            filtered_articles.sort(key=lambda x: x.importance_score, reverse=True)
            filtered_articles = filtered_articles[:self.config["processing"]["max_articles_per_cycle"]]
        
        # Update trends
        self.update_trends(filtered_articles)
        
        # Generate world model update
        world_model = self.generate_world_model_update(filtered_articles)
        
        # Generate stimulus
        stimulus = self.generate_stimulus(filtered_articles, world_model)
        
        # Save data
        self.save_data(filtered_articles, world_model)
        
        # Save stimulus
        stimulus_data = {
            "timestamp": time.time(),
            "source": "news_feeder",
            "stimulus": stimulus,
            "metadata": {
                "articles_processed": len(filtered_articles),
                "avg_importance": np.mean([a.importance_score for a in filtered_articles]) if filtered_articles else 0,
                "avg_sentiment": np.mean([a.sentiment_score for a in filtered_articles]) if filtered_articles else 0,
                "crisis_indicators": len(world_model.get('crisis_indicators', [])),
                "positive_developments": len(world_model.get('positive_developments', [])),
                "trending_topics": len(world_model.get('trending_topics', []))
            }
        }
        
        os.makedirs(os.path.dirname(self.config["output"]["stimulus_output"]), exist_ok=True)
        with open(self.config["output"]["stimulus_output"], 'w') as f:
            json.dump(stimulus_data, f, indent=2)
        
        logger.info(f"Processed {len(filtered_articles)} articles, generated stimulus: {stimulus}")
        
        return stimulus
    
    def run_continuous(self):
        """Run continuous news monitoring"""
        logger.info("Starting continuous news monitoring...")
        
        while True:
            try:
                self.run_news_cycle()
                time.sleep(self.config["processing"]["update_interval"])
                
            except KeyboardInterrupt:
                logger.info("News feeder stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in news cycle: {e}")
                time.sleep(300)  # Wait 5 minutes before retry

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = NewsFeeder()
    
    # Test mode: run single news cycle
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        stimulus = feeder.run_news_cycle()
        print(f"Generated news stimulus: {stimulus}")
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()