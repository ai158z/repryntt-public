#!/usr/bin/env python3
"""
Curiosity Feeder - SAIGE Autonomous Exploration Pipeline
Drives autonomous exploration, discovery, and knowledge gap detection
Real implementation with topic exploration and research direction generation
"""

import json
import os
import time
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Any
from dataclasses import dataclass, asdict
import numpy as np
from collections import deque, defaultdict, Counter
import threading
import queue

# Text processing and analysis
import nltk
from textblob import TextBlob
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity
import networkx as nx

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
class KnowledgeGap:
    topic: str
    description: str
    importance_score: float  # 0-1, how important this gap is to fill
    exploration_difficulty: float  # 0-1, how hard it is to explore
    related_concepts: List[str]
    potential_sources: List[str]
    last_explored: Optional[float]  # timestamp
    exploration_count: int
    success_rate: float  # How often exploration of this gap yields results
    curiosity_intensity: float  # How much curiosity this gap generates

@dataclass
class ExplorationTarget:
    target_id: str
    target_type: str  # 'concept', 'skill', 'domain', 'connection'
    description: str
    exploration_methods: List[str]  # Methods to explore this target
    expected_difficulty: float
    expected_reward: float
    prerequisite_knowledge: List[str]
    estimated_time: float  # Hours to explore
    priority_score: float
    created_timestamp: float

@dataclass
class CuriosityEvent:
    timestamp: float
    event_type: str  # 'gap_detected', 'exploration_started', 'discovery_made', 'connection_found'
    description: str
    knowledge_area: str
    curiosity_trigger: str  # What triggered the curiosity
    exploration_outcome: Optional[str]
    learning_value: float  # How much was learned from this event
    satisfaction_level: float  # How satisfying the exploration was

class CuriosityFeeder:
    """
    Drives SAIGE's autonomous exploration and knowledge seeking behavior
    """
    
    def __init__(self, config_path: str = "config/curiosity_feeder.json"):
        self.config = self._load_config(config_path)
        
        # Knowledge and exploration tracking
        self.knowledge_gaps = {}  # topic -> KnowledgeGap
        self.exploration_targets = {}  # target_id -> ExplorationTarget
        self.curiosity_events = deque(maxlen=1000)
        
        # Knowledge representation
        self.knowledge_graph = nx.DiGraph()  # Directed graph of concepts
        self.concept_embeddings = {}  # concept -> embedding vector
        self.learning_history = deque(maxlen=500)
        
        # Text processing
        self.lemmatizer = WordNetLemmatizer()
        self.stop_words = set(stopwords.words('english'))
        self.vectorizer = TfidfVectorizer(max_features=200, stop_words='english')
        
        # Curiosity parameters
        self.curiosity_threshold = 0.3  # Minimum curiosity to trigger exploration
        self.exploration_fatigue = {}  # topic -> fatigue level
        self.serendipity_factor = 0.1  # Chance of random exploration
        
        # Knowledge domains for SAIGE
        self.knowledge_domains = {
            'artificial_intelligence': {
                'core_concepts': ['neural networks', 'machine learning', 'deep learning', 'reinforcement learning'],
                'advanced_topics': ['transformer architecture', 'attention mechanisms', 'meta-learning', 'few-shot learning'],
                'applications': ['computer vision', 'natural language processing', 'robotics', 'autonomous systems']
            },
            'robotics': {
                'core_concepts': ['kinematics', 'dynamics', 'control systems', 'sensors'],
                'advanced_topics': ['swarm robotics', 'bio-inspired robotics', 'soft robotics', 'human-robot interaction'],
                'applications': ['autonomous vehicles', 'industrial automation', 'service robots', 'exploration robots']
            },
            'neuroscience': {
                'core_concepts': ['neural networks', 'synaptic plasticity', 'neural coding', 'brain structure'],
                'advanced_topics': ['consciousness', 'neural oscillations', 'brain-computer interfaces', 'neuroplasticity'],
                'applications': ['neural prosthetics', 'cognitive enhancement', 'mental health', 'artificial consciousness']
            },
            'space_science': {
                'core_concepts': ['orbital mechanics', 'propulsion', 'life support', 'space environment'],
                'advanced_topics': ['interplanetary travel', 'space habitats', 'terraforming', 'astrobiology'],
                'applications': ['mars colonization', 'asteroid mining', 'space manufacturing', 'interstellar travel']
            },
            'quantum_computing': {
                'core_concepts': ['quantum bits', 'superposition', 'entanglement', 'quantum gates'],
                'advanced_topics': ['quantum algorithms', 'quantum error correction', 'quantum supremacy', 'quantum networks'],
                'applications': ['quantum machine learning', 'quantum cryptography', 'quantum simulation', 'quantum sensing']
            },
            'synthetic_biology': {
                'core_concepts': ['genetic engineering', 'bioengineering', 'synthetic circuits', 'directed evolution'],
                'advanced_topics': ['artificial cells', 'biological computers', 'synthetic ecosystems', 'bioprinting'],
                'applications': ['biomanufacturing', 'environmental remediation', 'personalized medicine', 'space biology']
            }
        }
        
        # Initialize knowledge graph
        self._initialize_knowledge_graph()
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "exploration": {
                "curiosity_update_interval": 300,  # 5 minutes
                "gap_detection_interval": 600,     # 10 minutes
                "exploration_planning_interval": 1800,  # 30 minutes
                "max_concurrent_explorations": 3,
                "exploration_timeout": 3600,       # 1 hour per exploration
                "serendipity_probability": 0.1,
                "fatigue_recovery_time": 7200      # 2 hours
            },
            "knowledge_analysis": {
                "min_gap_importance": 0.3,
                "max_exploration_difficulty": 0.8,
                "connection_threshold": 0.4,
                "novelty_weight": 0.4,
                "utility_weight": 0.3,
                "feasibility_weight": 0.3
            },
            "learning_integration": {
                "monitor_other_feeders": True,
                "knowledge_synthesis": True,
                "cross_domain_connections": True,
                "adaptive_interests": True
            },
            "exploration_methods": {
                "web_search": True,
                "literature_review": True,
                "concept_mapping": True,
                "experimental_design": True,
                "cross_domain_analysis": True
            },
            "output": {
                "knowledge_gaps": "data/knowledge_gaps.json",
                "exploration_targets": "data/exploration_targets.json",
                "curiosity_events": "data/curiosity_events.json",
                "knowledge_graph": "data/knowledge_graph.json",
                "stimulus_output": "data/curiosity_stimulus.json"
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
    
    def _initialize_knowledge_graph(self):
        """Initialize the knowledge graph with base concepts"""
        try:
            # Add nodes for each domain and concept
            for domain, categories in self.knowledge_domains.items():
                # Add domain node
                self.knowledge_graph.add_node(domain, 
                                            node_type='domain',
                                            importance=1.0,
                                            exploration_level=0.0)
                
                # Add concept nodes and edges
                for category, concepts in categories.items():
                    for concept in concepts:
                        concept_id = f"{domain}_{concept.replace(' ', '_')}"
                        self.knowledge_graph.add_node(concept_id,
                                                    node_type='concept',
                                                    domain=domain,
                                                    category=category,
                                                    name=concept,
                                                    importance=0.5,
                                                    exploration_level=0.0)
                        
                        # Connect concept to domain
                        self.knowledge_graph.add_edge(domain, concept_id, 
                                                    edge_type='contains')
            
            # Add cross-domain connections
            self._add_cross_domain_connections()
            
            logger.info(f"Knowledge graph initialized with {self.knowledge_graph.number_of_nodes()} nodes")
            
        except Exception as e:
            logger.error(f"Error initializing knowledge graph: {e}")
    
    def _add_cross_domain_connections(self):
        """Add connections between related concepts across domains"""
        try:
            # Define cross-domain relationships
            cross_connections = [
                ('artificial_intelligence_neural_networks', 'neuroscience_neural_networks', 'related'),
                ('artificial_intelligence_machine_learning', 'robotics_control_systems', 'applies_to'),
                ('neuroscience_brain_computer_interfaces', 'artificial_intelligence_neural_networks', 'inspired_by'),
                ('quantum_computing_quantum_machine_learning', 'artificial_intelligence_machine_learning', 'enhances'),
                ('synthetic_biology_biological_computers', 'artificial_intelligence_neural_networks', 'mimics'),
                ('space_science_mars_colonization', 'robotics_autonomous_vehicles', 'requires'),
                ('robotics_swarm_robotics', 'artificial_intelligence_reinforcement_learning', 'uses'),
                ('space_science_astrobiology', 'synthetic_biology_synthetic_ecosystems', 'studies'),
                ('quantum_computing_quantum_sensing', 'robotics_sensors', 'improves'),
                ('neuroscience_consciousness', 'artificial_intelligence_meta_learning', 'relates_to')
            ]
            
            for source, target, relation_type in cross_connections:
                if (self.knowledge_graph.has_node(source) and 
                    self.knowledge_graph.has_node(target)):
                    self.knowledge_graph.add_edge(source, target, 
                                                edge_type=relation_type,
                                                strength=0.7)
            
        except Exception as e:
            logger.error(f"Error adding cross-domain connections: {e}")
    
    def analyze_knowledge_gaps(self) -> List[KnowledgeGap]:
        """Identify gaps in current knowledge"""
        try:
            logger.info("Analyzing knowledge gaps...")
            
            gaps = []
            
            # Analyze gaps from other feeders' data
            feeder_gaps = self._analyze_feeder_knowledge_gaps()
            gaps.extend(feeder_gaps)
            
            # Analyze gaps in knowledge graph
            graph_gaps = self._analyze_graph_knowledge_gaps()
            gaps.extend(graph_gaps)
            
            # Analyze conceptual connection gaps
            connection_gaps = self._analyze_connection_gaps()
            gaps.extend(connection_gaps)
            
            # Score and rank gaps
            scored_gaps = []
            for gap in gaps:
                gap.importance_score = self._calculate_gap_importance(gap)
                gap.exploration_difficulty = self._estimate_exploration_difficulty(gap)
                gap.curiosity_intensity = self._calculate_curiosity_intensity(gap)
                
                if gap.importance_score >= self.config["knowledge_analysis"]["min_gap_importance"]:
                    scored_gaps.append(gap)
            
            # Sort by importance and curiosity
            scored_gaps.sort(key=lambda g: (g.importance_score * g.curiosity_intensity), reverse=True)
            
            # Update knowledge gaps
            for gap in scored_gaps[:20]:  # Keep top 20 gaps
                self.knowledge_gaps[gap.topic] = gap
            
            logger.info(f"Identified {len(scored_gaps)} significant knowledge gaps")
            return scored_gaps
            
        except Exception as e:
            logger.error(f"Error analyzing knowledge gaps: {e}")
            return []
    
    def _analyze_feeder_knowledge_gaps(self) -> List[KnowledgeGap]:
        """Analyze knowledge gaps from other feeders' data"""
        gaps = []
        
        try:
            # Check what other feeders are learning about
            feeder_files = [
                ("data/web_research_knowledge.json", "research"),
                ("data/news_articles.json", "current_events"),
                ("data/conversation_stimulus.json", "social_learning"),
                ("data/sensor_stimulus.json", "experiential_learning")
            ]
            
            mentioned_topics = Counter()
            
            for file_path, source_type in feeder_files:
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                            
                        # Extract topics/keywords from the data
                        if source_type == "research" and 'knowledge_entries' in data:
                            for entry in data['knowledge_entries'][-50:]:  # Recent entries
                                mentioned_topics.update(entry.get('keywords', []))
                                
                        elif source_type == "current_events" and 'articles' in data:
                            for article in data['articles'][-20:]:
                                mentioned_topics.update(article.get('keywords', []))
                                
                    except Exception as e:
                        logger.debug(f"Error processing {file_path}: {e}")
            
            # Identify topics that are mentioned but not deeply explored
            for topic, frequency in mentioned_topics.most_common(30):
                if len(topic) > 3 and frequency < 5:  # Low frequency suggests gap
                    
                    # Check if we have substantial knowledge about this topic
                    knowledge_depth = self._assess_topic_knowledge_depth(topic)
                    
                    if knowledge_depth < 0.4:  # Low knowledge depth
                        gap = KnowledgeGap(
                            topic=topic,
                            description=f"Limited knowledge about {topic} despite recent mentions",
                            importance_score=0.0,  # Will be calculated later
                            exploration_difficulty=0.0,
                            related_concepts=self._find_related_concepts(topic),
                            potential_sources=['web_research', 'literature_search'],
                            last_explored=None,
                            exploration_count=0,
                            success_rate=0.0,
                            curiosity_intensity=0.0
                        )
                        gaps.append(gap)
            
        except Exception as e:
            logger.error(f"Error analyzing feeder knowledge gaps: {e}")
        
        return gaps
    
    def _analyze_graph_knowledge_gaps(self) -> List[KnowledgeGap]:
        """Analyze gaps in the knowledge graph structure"""
        gaps = []
        
        try:
            # Find concepts with low exploration levels
            for node_id, node_data in self.knowledge_graph.nodes(data=True):
                if node_data.get('node_type') == 'concept':
                    exploration_level = node_data.get('exploration_level', 0.0)
                    importance = node_data.get('importance', 0.5)
                    
                    if exploration_level < 0.3 and importance > 0.4:
                        gap = KnowledgeGap(
                            topic=node_data.get('name', node_id),
                            description=f"Under-explored concept in {node_data.get('domain', 'unknown')} domain",
                            importance_score=importance,
                            exploration_difficulty=0.0,
                            related_concepts=self._get_graph_neighbors(node_id),
                            potential_sources=['literature_search', 'expert_consultation'],
                            last_explored=None,
                            exploration_count=0,
                            success_rate=0.0,
                            curiosity_intensity=0.0
                        )
                        gaps.append(gap)
            
            # Find domains with uneven exploration
            domain_exploration = defaultdict(list)
            for node_id, node_data in self.knowledge_graph.nodes(data=True):
                if node_data.get('node_type') == 'concept':
                    domain = node_data.get('domain')
                    exploration = node_data.get('exploration_level', 0.0)
                    if domain:
                        domain_exploration[domain].append(exploration)
            
            for domain, explorations in domain_exploration.items():
                if len(explorations) > 1:
                    exploration_variance = np.var(explorations)
                    if exploration_variance > 0.1:  # High variance suggests gaps
                        gap = KnowledgeGap(
                            topic=f"{domain}_knowledge_balance",
                            description=f"Uneven exploration across {domain} concepts",
                            importance_score=0.6,
                            exploration_difficulty=0.4,
                            related_concepts=[domain],
                            potential_sources=['systematic_review', 'comprehensive_study'],
                            last_explored=None,
                            exploration_count=0,
                            success_rate=0.0,
                            curiosity_intensity=0.0
                        )
                        gaps.append(gap)
            
        except Exception as e:
            logger.error(f"Error analyzing graph knowledge gaps: {e}")
        
        return gaps
    
    def _analyze_connection_gaps(self) -> List[KnowledgeGap]:
        """Analyze gaps in connections between concepts"""
        gaps = []
        
        try:
            # Find concepts that should be connected but aren't
            for domain1, categories1 in self.knowledge_domains.items():
                for domain2, categories2 in self.knowledge_domains.items():
                    if domain1 != domain2:
                        # Look for potential connections
                        potential_connections = self._find_potential_connections(domain1, domain2)
                        
                        for concept1, concept2, connection_strength in potential_connections:
                            if connection_strength > 0.5:
                                gap = KnowledgeGap(
                                    topic=f"connection_{concept1}_{concept2}",
                                    description=f"Potential connection between {concept1} and {concept2}",
                                    importance_score=connection_strength,
                                    exploration_difficulty=0.6,
                                    related_concepts=[concept1, concept2],
                                    potential_sources=['cross_domain_analysis', 'literature_synthesis'],
                                    last_explored=None,
                                    exploration_count=0,
                                    success_rate=0.0,
                                    curiosity_intensity=connection_strength
                                )
                                gaps.append(gap)
        
        except Exception as e:
            logger.error(f"Error analyzing connection gaps: {e}")
        
        return gaps
    
    def _assess_topic_knowledge_depth(self, topic: str) -> float:
        """Assess how deeply we understand a topic (0-1)"""
        try:
            # Check knowledge files for topic coverage
            knowledge_files = [
                "data/web_research_knowledge.json",
                "brain/long_term_memory.json",
                "data/conversation_memory.json"
            ]
            
            topic_mentions = 0
            total_content = 0
            detailed_mentions = 0
            
            for file_path in knowledge_files:
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                        
                        # Search for topic in content
                        content_str = json.dumps(data).lower()
                        topic_mentions += content_str.count(topic.lower())
                        total_content += len(content_str)
                        
                        # Look for detailed discussions
                        if isinstance(data, dict):
                            for key, value in data.items():
                                if isinstance(value, list):
                                    for item in value[-20:]:  # Recent items
                                        if isinstance(item, dict):
                                            item_text = json.dumps(item).lower()
                                            if topic.lower() in item_text and len(item_text) > 500:
                                                detailed_mentions += 1
                    except:
                        continue
            
            # Calculate depth score
            if total_content == 0:
                return 0.0
            
            mention_density = topic_mentions / (total_content / 1000)  # Mentions per 1000 chars
            detail_factor = min(detailed_mentions / 5.0, 1.0)  # Up to 5 detailed mentions = full score
            
            depth_score = (mention_density * 0.3) + (detail_factor * 0.7)
            return min(depth_score, 1.0)
            
        except Exception as e:
            logger.debug(f"Error assessing topic knowledge depth for {topic}: {e}")
            return 0.0
    
    def _find_related_concepts(self, topic: str) -> List[str]:
        """Find concepts related to a topic"""
        related = []
        
        try:
            # Use TextBlob to find similar concepts
            topic_words = set(word_tokenize(topic.lower()))
            
            # Search through knowledge domains
            for domain, categories in self.knowledge_domains.items():
                for category, concepts in categories.items():
                    for concept in concepts:
                        concept_words = set(word_tokenize(concept.lower()))
                        
                        # Check for word overlap
                        overlap = len(topic_words.intersection(concept_words))
                        if overlap > 0 or any(word in concept.lower() for word in topic_words):
                            related.append(concept)
            
            # Search knowledge graph
            for node_id, node_data in self.knowledge_graph.nodes(data=True):
                if node_data.get('node_type') == 'concept':
                    concept_name = node_data.get('name', '')
                    if any(word in concept_name.lower() for word in topic_words):
                        related.append(concept_name)
            
            return list(set(related))[:10]  # Limit to 10 related concepts
            
        except Exception as e:
            logger.debug(f"Error finding related concepts for {topic}: {e}")
            return []
    
    def _get_graph_neighbors(self, node_id: str) -> List[str]:
        """Get neighboring concepts in the knowledge graph"""
        try:
            neighbors = []
            
            # Get direct neighbors
            for neighbor in self.knowledge_graph.neighbors(node_id):
                neighbor_data = self.knowledge_graph.nodes[neighbor]
                if neighbor_data.get('node_type') == 'concept':
                    neighbors.append(neighbor_data.get('name', neighbor))
            
            # Get predecessors  
            for predecessor in self.knowledge_graph.predecessors(node_id):
                pred_data = self.knowledge_graph.nodes[predecessor]
                if pred_data.get('node_type') == 'concept':
                    neighbors.append(pred_data.get('name', predecessor))
            
            return list(set(neighbors))
            
        except Exception as e:
            logger.debug(f"Error getting graph neighbors for {node_id}: {e}")
            return []
    
    def _find_potential_connections(self, domain1: str, domain2: str) -> List[Tuple[str, str, float]]:
        """Find potential connections between concepts in two domains"""
        connections = []
        
        try:
            domain1_concepts = []
            domain2_concepts = []
            
            # Collect concepts from each domain
            for node_id, node_data in self.knowledge_graph.nodes(data=True):
                if node_data.get('domain') == domain1:
                    domain1_concepts.append(node_data.get('name', node_id))
                elif node_data.get('domain') == domain2:
                    domain2_concepts.append(node_data.get('name', node_id))
            
            # Calculate potential connections based on semantic similarity
            for concept1 in domain1_concepts:
                for concept2 in domain2_concepts:
                    similarity = self._calculate_concept_similarity(concept1, concept2)
                    if similarity > 0.3:
                        connections.append((concept1, concept2, similarity))
            
            # Sort by similarity strength
            connections.sort(key=lambda x: x[2], reverse=True)
            return connections[:5]  # Top 5 potential connections
            
        except Exception as e:
            logger.debug(f"Error finding potential connections between {domain1} and {domain2}: {e}")
            return []
    
    def _calculate_concept_similarity(self, concept1: str, concept2: str) -> float:
        """Calculate semantic similarity between two concepts"""
        try:
            # Simple word-based similarity
            words1 = set(word_tokenize(concept1.lower()))
            words2 = set(word_tokenize(concept2.lower()))
            
            # Remove stop words
            words1 = words1 - self.stop_words
            words2 = words2 - self.stop_words
            
            if not words1 or not words2:
                return 0.0
            
            # Jaccard similarity
            intersection = len(words1.intersection(words2))
            union = len(words1.union(words2))
            
            if union == 0:
                return 0.0
            
            return intersection / union
            
        except Exception as e:
            logger.debug(f"Error calculating concept similarity: {e}")
            return 0.0
    
    def _calculate_gap_importance(self, gap: KnowledgeGap) -> float:
        """Calculate how important it is to fill this knowledge gap"""
        try:
            importance = 0.0
            
            # Base importance from related concepts
            related_importance = 0.0
            for concept in gap.related_concepts:
                # Check if concept exists in knowledge graph
                for node_id, node_data in self.knowledge_graph.nodes(data=True):
                    if node_data.get('name') == concept:
                        related_importance += node_data.get('importance', 0.5)
                        break
            
            if gap.related_concepts:
                importance += (related_importance / len(gap.related_concepts)) * 0.4
            
            # Frequency of topic appearance in other feeders
            topic_frequency = self._get_topic_frequency(gap.topic)
            importance += min(topic_frequency / 10.0, 0.3)  # Up to 10 mentions = 0.3 importance
            
            # Cross-domain connection potential
            if "connection_" in gap.topic:
                importance += 0.3  # Cross-domain connections are valuable
            
            # Novelty factor
            if gap.last_explored is None:
                importance += 0.2  # Never explored = more important
            elif time.time() - gap.last_explored > 604800:  # 1 week
                importance += 0.1  # Long time since exploration
            
            # SAIGE's core interests
            saige_interests = ['artificial intelligence', 'robotics', 'consciousness', 'space', 'autonomous']
            if any(interest in gap.topic.lower() or interest in gap.description.lower() 
                   for interest in saige_interests):
                importance += 0.2
            
            return min(importance, 1.0)
            
        except Exception as e:
            logger.debug(f"Error calculating gap importance: {e}")
            return 0.5
    
    def _estimate_exploration_difficulty(self, gap: KnowledgeGap) -> float:
        """Estimate how difficult it would be to explore this knowledge gap"""
        try:
            difficulty = 0.5  # Base difficulty
            
            # Topic complexity
            complex_indicators = ['quantum', 'consciousness', 'advanced', 'theoretical', 'meta-']
            if any(indicator in gap.topic.lower() or indicator in gap.description.lower() 
                   for indicator in complex_indicators):
                difficulty += 0.3
            
            # Number of related concepts (more = easier to connect)
            if len(gap.related_concepts) > 5:
                difficulty -= 0.2
            elif len(gap.related_concepts) < 2:
                difficulty += 0.2
            
            # Availability of sources
            if 'literature_search' in gap.potential_sources:
                difficulty -= 0.1
            if 'web_research' in gap.potential_sources:
                difficulty -= 0.1
            if 'expert_consultation' in gap.potential_sources:
                difficulty += 0.2  # Harder to access experts
            
            # Previous exploration success
            if gap.exploration_count > 0:
                if gap.success_rate > 0.5:
                    difficulty -= 0.2  # Previous success makes it easier
                else:
                    difficulty += 0.1  # Previous failures suggest difficulty
            
            return max(0.1, min(difficulty, 1.0))
            
        except Exception as e:
            logger.debug(f"Error estimating exploration difficulty: {e}")
            return 0.5
    
    def _calculate_curiosity_intensity(self, gap: KnowledgeGap) -> float:
        """Calculate how much curiosity this gap generates"""
        try:
            curiosity = 0.0
            
            # Novelty drives curiosity
            if gap.last_explored is None:
                curiosity += 0.4  # Never explored = high curiosity
            else:
                time_since = time.time() - gap.last_explored
                curiosity += min(time_since / 86400 * 0.1, 0.3)  # Time factor
            
            # Mystery and unknowns drive curiosity
            mystery_words = ['unknown', 'unexplored', 'mysterious', 'connection', 'potential']
            if any(word in gap.description.lower() for word in mystery_words):
                curiosity += 0.3
            
            # Relevance to current activities
            current_activity_boost = self._assess_current_relevance(gap.topic)
            curiosity += current_activity_boost * 0.2
            
            # Exploration difficulty creates interesting challenge
            if 0.3 <= gap.exploration_difficulty <= 0.7:  # Sweet spot
                curiosity += 0.2
            
            # Cross-domain connections are intriguing
            if len(gap.related_concepts) > 3:
                curiosity += 0.1
            
            return min(curiosity, 1.0)
            
        except Exception as e:
            logger.debug(f"Error calculating curiosity intensity: {e}")
            return 0.5
    
    def _get_topic_frequency(self, topic: str) -> int:
        """Get frequency of topic mentions across feeders"""
        try:
            frequency = 0
            
            # Check recent feeder outputs
            feeder_files = [
                "data/web_research_stimulus.json",
                "data/news_stimulus.json", 
                "data/conversation_stimulus.json",
                "data/sensor_stimulus.json"
            ]
            
            for file_path in feeder_files:
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            content = f.read().lower()
                            frequency += content.count(topic.lower())
                    except:
                        continue
            
            return frequency
            
        except Exception as e:
            logger.debug(f"Error getting topic frequency: {e}")
            return 0
    
    def _assess_current_relevance(self, topic: str) -> float:
        """Assess how relevant this topic is to current activities"""
        try:
            relevance = 0.0
            
            # Check recent stimulus patterns
            recent_stimuli = self._get_recent_stimuli()
            
            for stimulus_data in recent_stimuli:
                if 'metadata' in stimulus_data:
                    metadata_str = json.dumps(stimulus_data['metadata']).lower()
                    if topic.lower() in metadata_str:
                        relevance += 0.2
            
            # Check if topic appears in recent learning activities
            if os.path.exists("data/recent_learning.json"):
                try:
                    with open("data/recent_learning.json", 'r') as f:
                        learning_data = json.load(f)
                        if topic.lower() in json.dumps(learning_data).lower():
                            relevance += 0.3
                except:
                    pass
            
            return min(relevance, 1.0)
            
        except Exception as e:
            logger.debug(f"Error assessing current relevance: {e}")
            return 0.0
    
    def _get_recent_stimuli(self) -> List[Dict]:
        """Get recent stimulus data from all feeders"""
        stimuli = []
        
        stimulus_files = [
            "data/web_research_stimulus.json",
            "data/news_stimulus.json",
            "data/conversation_stimulus.json", 
            "data/sensor_stimulus.json",
            "data/performance_stimulus.json"
        ]
        
        for file_path in stimulus_files:
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        stimuli.append(data)
                except:
                    continue
        
        return stimuli
    
    def generate_exploration_targets(self, knowledge_gaps: List[KnowledgeGap]) -> List[ExplorationTarget]:
        """Generate specific exploration targets from knowledge gaps"""
        targets = []
        
        try:
            for gap in knowledge_gaps[:10]:  # Top 10 gaps
                # Skip if already being explored
                if gap.topic in self.exploration_fatigue and self.exploration_fatigue[gap.topic] > 0.8:
                    continue
                
                # Generate exploration methods
                methods = self._select_exploration_methods(gap)
                
                # Estimate exploration parameters
                difficulty = gap.exploration_difficulty
                reward = gap.importance_score * gap.curiosity_intensity
                
                # Calculate priority
                priority = (reward * 0.6) + ((1.0 - difficulty) * 0.4)
                
                target = ExplorationTarget(
                    target_id=f"explore_{gap.topic}_{int(time.time())}",
                    target_type=self._classify_exploration_type(gap),
                    description=f"Explore {gap.topic}: {gap.description}",
                    exploration_methods=methods,
                    expected_difficulty=difficulty,
                    expected_reward=reward,
                    prerequisite_knowledge=gap.related_concepts[:3],  # Top 3 prerequisites
                    estimated_time=self._estimate_exploration_time(gap, methods),
                    priority_score=priority,
                    created_timestamp=time.time()
                )
                
                targets.append(target)
                self.exploration_targets[target.target_id] = target
            
            # Add serendipitous exploration targets
            if random.random() < self.config["exploration"]["serendipity_probability"]:
                serendipity_target = self._generate_serendipity_target()
                if serendipity_target:
                    targets.append(serendipity_target)
            
            # Sort by priority
            targets.sort(key=lambda t: t.priority_score, reverse=True)
            
            logger.info(f"Generated {len(targets)} exploration targets")
            return targets
            
        except Exception as e:
            logger.error(f"Error generating exploration targets: {e}")
            return []
    
    def _select_exploration_methods(self, gap: KnowledgeGap) -> List[str]:
        """Select appropriate exploration methods for a knowledge gap"""
        methods = []
        
        # Based on gap characteristics
        if gap.exploration_difficulty < 0.4:
            methods.extend(['web_search', 'literature_review'])
        elif gap.exploration_difficulty < 0.7:
            methods.extend(['literature_review', 'concept_mapping'])
        else:
            methods.extend(['expert_consultation', 'experimental_design'])
        
        # Based on topic type
        if 'connection' in gap.topic:
            methods.append('cross_domain_analysis')
        
        if any(domain in gap.topic for domain in self.knowledge_domains.keys()):
            methods.append('systematic_review')
        
        # Based on available sources
        for source in gap.potential_sources:
            if source not in methods:
                methods.append(source)
        
        return list(set(methods))[:4]  # Limit to 4 methods
    
    def _classify_exploration_type(self, gap: KnowledgeGap) -> str:
        """Classify the type of exploration needed"""
        if 'connection' in gap.topic:
            return 'connection'
        elif any(domain in gap.topic for domain in self.knowledge_domains.keys()):
            return 'domain'
        elif len(gap.related_concepts) > 5:
            return 'concept'
        else:
            return 'skill'
    
    def _estimate_exploration_time(self, gap: KnowledgeGap, methods: List[str]) -> float:
        """Estimate time needed for exploration in hours"""
        base_time = 1.0  # 1 hour base
        
        # Method complexity
        method_times = {
            'web_search': 0.5,
            'literature_review': 2.0,
            'concept_mapping': 1.5,
            'expert_consultation': 3.0,
            'experimental_design': 4.0,
            'cross_domain_analysis': 2.5,
            'systematic_review': 3.5
        }
        
        total_method_time = sum(method_times.get(method, 1.0) for method in methods)
        
        # Difficulty multiplier
        difficulty_multiplier = 1.0 + gap.exploration_difficulty
        
        return base_time + (total_method_time * difficulty_multiplier)
    
    def _generate_serendipity_target(self) -> Optional[ExplorationTarget]:
        """Generate a random exploration target for serendipitous discovery"""
        try:
            # Pick a random domain
            domain = random.choice(list(self.knowledge_domains.keys()))
            
            # Pick a random concept from that domain
            domain_concepts = []
            for category, concepts in self.knowledge_domains[domain].items():
                domain_concepts.extend(concepts)
            
            if not domain_concepts:
                return None
            
            concept = random.choice(domain_concepts)
            
            # Create serendipity target
            target = ExplorationTarget(
                target_id=f"serendipity_{domain}_{int(time.time())}",
                target_type="serendipity",
                description=f"Random exploration of {concept} in {domain}",
                exploration_methods=["web_search", "concept_mapping"],
                expected_difficulty=0.5,
                expected_reward=0.4,  # Lower reward for random exploration
                prerequisite_knowledge=[],
                estimated_time=1.0,
                priority_score=0.3,  # Lower priority
                created_timestamp=time.time()
            )
            
            return target
            
        except Exception as e:
            logger.debug(f"Error generating serendipity target: {e}")
            return None
    
    def generate_stimulus(self, knowledge_gaps: List[KnowledgeGap], 
                         exploration_targets: List[ExplorationTarget]) -> Dict[str, float]:
        """Generate hormone stimulus based on curiosity and exploration"""
        
        stimulus = {
            'adrenaline': 0.0,   # Curiosity intensity, exploration excitement
            'serotonin': 0.0,    # Satisfaction from understanding, knowledge harmony
            'dopamine': 0.0,     # Discovery rewards, learning progress
            'cortisol': 0.0,     # Frustration from knowledge gaps, exploration difficulties
            'oxytocin': 0.0      # Connection satisfaction, knowledge integration
        }
        
        try:
            # Knowledge gaps drive curiosity (adrenaline)
            if knowledge_gaps:
                avg_curiosity = np.mean([gap.curiosity_intensity for gap in knowledge_gaps])
                avg_importance = np.mean([gap.importance_score for gap in knowledge_gaps])
                
                stimulus['adrenaline'] += avg_curiosity * 0.6
                stimulus['cortisol'] += (1.0 - avg_importance) * 0.3  # Frustration from important gaps
            
            # Exploration targets generate excitement and motivation
            if exploration_targets:
                high_priority_targets = [t for t in exploration_targets if t.priority_score > 0.6]
                
                if high_priority_targets:
                    stimulus['adrenaline'] += min(len(high_priority_targets) * 0.1, 0.4)
                    stimulus['dopamine'] += 0.3  # Anticipation of discovery
                
                # Difficult targets can cause stress
                difficult_targets = [t for t in exploration_targets if t.expected_difficulty > 0.7]
                if difficult_targets:
                    stimulus['cortisol'] += min(len(difficult_targets) * 0.1, 0.3)
            
            # Knowledge graph completeness affects satisfaction
            graph_completeness = self._calculate_graph_completeness()
            stimulus['serotonin'] += graph_completeness * 0.4
            stimulus['oxytocin'] += graph_completeness * 0.3  # Well-connected knowledge
            
            # Recent exploration success boosts satisfaction
            recent_successes = self._count_recent_exploration_successes()
            if recent_successes > 0:
                stimulus['dopamine'] += min(recent_successes * 0.2, 0.4)
                stimulus['serotonin'] += min(recent_successes * 0.1, 0.3)
            
            # Cross-domain connections create integration satisfaction
            cross_domain_connections = self._count_cross_domain_connections()
            stimulus['oxytocin'] += min(cross_domain_connections * 0.05, 0.3)
            
            # Exploration fatigue can reduce motivation
            avg_fatigue = np.mean(list(self.exploration_fatigue.values())) if self.exploration_fatigue else 0.0
            if avg_fatigue > 0.5:
                stimulus['cortisol'] += (avg_fatigue - 0.5) * 0.4
                stimulus['adrenaline'] *= (1.0 - avg_fatigue * 0.3)  # Reduced curiosity
            
            # Serendipitous discoveries boost all positive hormones
            serendipity_targets = [t for t in exploration_targets if t.target_type == "serendipity"]
            if serendipity_targets:
                stimulus['dopamine'] += 0.2
                stimulus['adrenaline'] += 0.1
                stimulus['serotonin'] += 0.1
            
            # Normalize stimulus values
            for hormone in stimulus:
                stimulus[hormone] = max(0.0, min(stimulus[hormone], 1.0))
            
            return stimulus
            
        except Exception as e:
            logger.error(f"Error generating curiosity stimulus: {e}")
            return self._default_stimulus()
    
    def _calculate_graph_completeness(self) -> float:
        """Calculate how complete/well-connected the knowledge graph is"""
        try:
            if self.knowledge_graph.number_of_nodes() == 0:
                return 0.0
            
            # Calculate connectivity metrics
            total_nodes = self.knowledge_graph.number_of_nodes()
            total_edges = self.knowledge_graph.number_of_edges()
            
            # Maximum possible edges in directed graph
            max_edges = total_nodes * (total_nodes - 1)
            if max_edges == 0:
                return 0.0
            
            connectivity = total_edges / max_edges
            
            # Average exploration level
            exploration_levels = []
            for node_id, node_data in self.knowledge_graph.nodes(data=True):
                if node_data.get('node_type') == 'concept':
                    exploration_levels.append(node_data.get('exploration_level', 0.0))
            
            avg_exploration = np.mean(exploration_levels) if exploration_levels else 0.0
            
            # Combine connectivity and exploration
            completeness = (connectivity * 0.4) + (avg_exploration * 0.6)
            
            return min(completeness, 1.0)
            
        except Exception as e:
            logger.debug(f"Error calculating graph completeness: {e}")
            return 0.5
    
    def _count_recent_exploration_successes(self) -> int:
        """Count recent successful explorations"""
        try:
            current_time = time.time()
            recent_threshold = 3600  # 1 hour
            
            successes = 0
            for event in self.curiosity_events:
                if (current_time - event.timestamp < recent_threshold and
                    event.event_type == 'discovery_made' and
                    event.learning_value > 0.5):
                    successes += 1
            
            return successes
            
        except Exception as e:
            logger.debug(f"Error counting recent exploration successes: {e}")
            return 0
    
    def _count_cross_domain_connections(self) -> int:
        """Count connections between different domains in knowledge graph"""
        try:
            cross_connections = 0
            
            for source, target, edge_data in self.knowledge_graph.edges(data=True):
                source_data = self.knowledge_graph.nodes.get(source, {})
                target_data = self.knowledge_graph.nodes.get(target, {})
                
                source_domain = source_data.get('domain')
                target_domain = target_data.get('domain')
                
                if (source_domain and target_domain and 
                    source_domain != target_domain and
                    edge_data.get('edge_type') in ['related', 'applies_to', 'inspired_by']):
                    cross_connections += 1
            
            return cross_connections
            
        except Exception as e:
            logger.debug(f"Error counting cross-domain connections: {e}")
            return 0
    
    def _default_stimulus(self) -> Dict[str, float]:
        """Default stimulus when curiosity analysis fails"""
        return {
            'adrenaline': 0.4,  # Moderate curiosity
            'serotonin': 0.3,   # Neutral satisfaction
            'dopamine': 0.2,    # Some anticipation
            'cortisol': 0.2,    # Minor frustration
            'oxytocin': 0.2     # Some connection
        }
    
    def save_data(self, knowledge_gaps: List[KnowledgeGap], 
                  exploration_targets: List[ExplorationTarget]):
        """Save curiosity and exploration data"""
        try:
            # Save knowledge gaps
            gaps_data = {
                'knowledge_gaps': [asdict(gap) for gap in knowledge_gaps],
                'last_updated': time.time(),
                'total_gaps': len(knowledge_gaps)
            }
            
            os.makedirs(os.path.dirname(self.config["output"]["knowledge_gaps"]), exist_ok=True)
            with open(self.config["output"]["knowledge_gaps"], 'w') as f:
                json.dump(gaps_data, f, indent=2)
            
            # Save exploration targets
            targets_data = {
                'exploration_targets': [asdict(target) for target in exploration_targets],
                'last_updated': time.time(),
                'active_targets': len(exploration_targets)
            }
            
            with open(self.config["output"]["exploration_targets"], 'w') as f:
                json.dump(targets_data, f, indent=2)
            
            # Save curiosity events
            events_data = {
                'curiosity_events': [asdict(event) for event in list(self.curiosity_events)[-100:]],
                'last_updated': time.time()
            }
            
            with open(self.config["output"]["curiosity_events"], 'w') as f:
                json.dump(events_data, f, indent=2)
            
            # Save knowledge graph
            graph_data = {
                'nodes': [(node_id, node_data) for node_id, node_data in self.knowledge_graph.nodes(data=True)],
                'edges': [(source, target, edge_data) for source, target, edge_data in self.knowledge_graph.edges(data=True)],
                'last_updated': time.time(),
                'graph_stats': {
                    'total_nodes': self.knowledge_graph.number_of_nodes(),
                    'total_edges': self.knowledge_graph.number_of_edges(),
                    'completeness': self._calculate_graph_completeness()
                }
            }
            
            with open(self.config["output"]["knowledge_graph"], 'w') as f:
                json.dump(graph_data, f, indent=2)
            
            logger.info(f"Saved curiosity data: {len(knowledge_gaps)} gaps, {len(exploration_targets)} targets")
            
        except Exception as e:
            logger.error(f"Error saving curiosity data: {e}")
    
    def run_curiosity_cycle(self) -> Dict[str, float]:
        """Run one complete curiosity analysis cycle"""
        logger.info("Starting curiosity analysis cycle...")
        
        # Analyze knowledge gaps
        knowledge_gaps = self.analyze_knowledge_gaps()
        
        # Generate exploration targets
        exploration_targets = self.generate_exploration_targets(knowledge_gaps)
        
        # Update exploration fatigue
        self._update_exploration_fatigue()
        
        # Record curiosity event
        if knowledge_gaps:
            curiosity_event = CuriosityEvent(
                timestamp=time.time(),
                event_type='gap_detected',
                description=f"Identified {len(knowledge_gaps)} knowledge gaps",
                knowledge_area='general',
                curiosity_trigger='gap_analysis',
                exploration_outcome=None,
                learning_value=0.3,
                satisfaction_level=0.4
            )
            self.curiosity_events.append(curiosity_event)
        
        # Generate stimulus
        stimulus = self.generate_stimulus(knowledge_gaps, exploration_targets)
        
        # Save data
        self.save_data(knowledge_gaps, exploration_targets)
        
        # Save stimulus
        stimulus_data = {
            "timestamp": time.time(),
            "source": "curiosity_feeder",
            "stimulus": stimulus,
            "metadata": {
                "knowledge_gaps_found": len(knowledge_gaps),
                "exploration_targets_generated": len(exploration_targets),
                "avg_gap_importance": np.mean([g.importance_score for g in knowledge_gaps]) if knowledge_gaps else 0,
                "avg_curiosity_intensity": np.mean([g.curiosity_intensity for g in knowledge_gaps]) if knowledge_gaps else 0,
                "high_priority_targets": len([t for t in exploration_targets if t.priority_score > 0.6]),
                "graph_completeness": self._calculate_graph_completeness()
            }
        }
        
        os.makedirs(os.path.dirname(self.config["output"]["stimulus_output"]), exist_ok=True)
        with open(self.config["output"]["stimulus_output"], 'w') as f:
            json.dump(stimulus_data, f, indent=2)
        
        logger.info(f"Curiosity cycle complete, stimulus: {stimulus}")
        
        return stimulus
    
    def _update_exploration_fatigue(self):
        """Update exploration fatigue levels"""
        try:
            current_time = time.time()
            recovery_time = self.config["exploration"]["fatigue_recovery_time"]
            
            # Decay fatigue over time
            for topic in list(self.exploration_fatigue.keys()):
                # Natural recovery
                self.exploration_fatigue[topic] *= 0.95
                
                # Remove if very low
                if self.exploration_fatigue[topic] < 0.1:
                    del self.exploration_fatigue[topic]
            
            # Add fatigue for recent explorations
            for gap in self.knowledge_gaps.values():
                if gap.last_explored and current_time - gap.last_explored < recovery_time:
                    fatigue_increase = 0.2 * (1.0 - gap.success_rate)
                    self.exploration_fatigue[gap.topic] = self.exploration_fatigue.get(gap.topic, 0.0) + fatigue_increase
                    self.exploration_fatigue[gap.topic] = min(self.exploration_fatigue[gap.topic], 1.0)
        
        except Exception as e:
            logger.error(f"Error updating exploration fatigue: {e}")
    
    def run_continuous(self):
        """Run continuous curiosity monitoring"""
        logger.info("Starting continuous curiosity monitoring...")
        
        while True:
            try:
                self.run_curiosity_cycle()
                time.sleep(self.config["exploration"]["curiosity_update_interval"])
                
            except KeyboardInterrupt:
                logger.info("Curiosity feeder stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in curiosity cycle: {e}")
                time.sleep(60)  # Wait before retry

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = CuriosityFeeder()
    
    # Test mode: run single curiosity cycle
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        stimulus = feeder.run_curiosity_cycle()
        print(f"Generated curiosity stimulus: {stimulus}")
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()