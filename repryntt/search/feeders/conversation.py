#!/usr/bin/env python3
"""
Conversation Feeder - SAIGE Learning Pipeline
Converts chat logs and human interactions into stimulus data for evolution loop
Real implementation with sentiment analysis and pattern recognition
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import hashlib

# Text processing
import nltk
from textblob import TextBlob
from collections import Counter, defaultdict
import numpy as np

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

try:
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('stopwords')

from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize, sent_tokenize

logger = logging.getLogger(__name__)

@dataclass
class ConversationEntry:
    timestamp: float
    user_message: str
    ai_response: str
    sentiment_user: float  # -1 to 1
    sentiment_ai: float
    complexity: float  # 0 to 1
    success_indicators: List[str]
    topic_keywords: List[str]
    response_time: float
    interaction_id: str

class ConversationFeeder:
    """
    Processes conversation logs to extract learning experiences and emotional stimulus
    """
    
    def __init__(self, config_path: str = "config/conversation_feeder.json"):
        self.config = self._load_config(config_path)
        self.stop_words = set(stopwords.words('english'))
        
        # Hormone mapping based on conversation patterns
        self.hormone_triggers = {
            'curiosity_keywords': ['how', 'why', 'what', 'explain', 'learn', 'understand', 'teach'],
            'success_keywords': ['thanks', 'perfect', 'excellent', 'great', 'helpful', 'solved'],
            'frustration_keywords': ['error', 'wrong', 'failed', 'broken', 'confused', 'stuck'],
            'social_keywords': ['please', 'thank you', 'sorry', 'appreciate', 'help'],
            'technical_keywords': ['code', 'function', 'algorithm', 'debug', 'optimize', 'build']
        }
        
        # Track conversation patterns over time
        self.conversation_history = []
        self.topic_trends = defaultdict(list)
        self.user_satisfaction_history = []
        
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "log_sources": [
                "logs/saige_chat.log",
                "chats/general_chat.txt",
                "chats/general_chat2.txt",
                "chats/real_time_tts_chat.txt"
            ],
            "brain_file": "brainfile2.json",
            "stimulus_output": "data/conversation_stimulus.json",
            "min_interaction_gap": 5.0,  # seconds
            "sentiment_weight": 0.7,
            "complexity_weight": 0.3,
            "success_threshold": 0.6,
            "update_interval": 30  # seconds
        }
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config
    
    def parse_chat_logs(self) -> List[ConversationEntry]:
        """Parse all available chat logs into structured conversations"""
        conversations = []
        
        for log_file in self.config["log_sources"]:
            if not os.path.exists(log_file):
                logger.warning(f"Log file not found: {log_file}")
                continue
                
            try:
                conversations.extend(self._parse_log_file(log_file))
                logger.info(f"Parsed {log_file}: found conversations")
            except Exception as e:
                logger.error(f"Error parsing {log_file}: {e}")
        
        # Sort by timestamp
        conversations.sort(key=lambda x: x.timestamp)
        return conversations
    
    def _parse_log_file(self, log_file: str) -> List[ConversationEntry]:
        """Parse individual log file based on its format"""
        conversations = []
        
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        if log_file.endswith('.log'):
            # Parse structured log format
            conversations = self._parse_structured_log(content)
        else:
            # Parse plain text chat format
            conversations = self._parse_plain_text(content, log_file)
        
        return conversations
    
    def _parse_structured_log(self, content: str) -> List[ConversationEntry]:
        """Parse structured log files (saige_chat.log format)"""
        conversations = []
        entries = content.split('---\n')
        
        for entry in entries:
            if not entry.strip():
                continue
                
            lines = entry.strip().split('\n')
            user_msg = ""
            ai_response = ""
            timestamp = time.time()
            
            for line in lines:
                if line.startswith('User: '):
                    user_msg = line[6:]
                elif line.startswith('AI: '):
                    ai_response = line[4:]
                elif 'Signature:' in line:
                    # Extract timestamp from signature or use current time
                    timestamp = time.time()
            
            if user_msg and ai_response:
                conv_entry = self._analyze_conversation(user_msg, ai_response, timestamp)
                conversations.append(conv_entry)
        
        return conversations
    
    def _parse_plain_text(self, content: str, filename: str) -> List[ConversationEntry]:
        """Parse plain text chat files"""
        conversations = []
        lines = content.split('\n')
        
        current_user_msg = ""
        current_ai_msg = ""
        timestamp = os.path.getmtime(filename)
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect conversation patterns
            if self._is_user_message(line):
                # Process previous conversation if exists
                if current_user_msg and current_ai_msg:
                    conv_entry = self._analyze_conversation(current_user_msg, current_ai_msg, timestamp)
                    conversations.append(conv_entry)
                
                current_user_msg = self._clean_message(line)
                current_ai_msg = ""
                timestamp += 60  # Approximate timestamp spacing
                
            elif self._is_ai_message(line):
                current_ai_msg = self._clean_message(line)
            else:
                # Continue building current message
                if current_ai_msg:
                    current_ai_msg += " " + line
                elif current_user_msg:
                    current_user_msg += " " + line
        
        # Process final conversation
        if current_user_msg and current_ai_msg:
            conv_entry = self._analyze_conversation(current_user_msg, current_ai_msg, timestamp)
            conversations.append(conv_entry)
        
        return conversations
    
    def _is_user_message(self, line: str) -> bool:
        """Detect if line is start of user message"""
        user_indicators = ['user:', 'human:', 'you:', 'question:', 'q:']
        return any(line.lower().startswith(indicator) for indicator in user_indicators)
    
    def _is_ai_message(self, line: str) -> bool:
        """Detect if line is start of AI message"""
        ai_indicators = ['ai:', 'assistant:', 'saige:', 'response:', 'a:']
        return any(line.lower().startswith(indicator) for indicator in ai_indicators)
    
    def _clean_message(self, line: str) -> str:
        """Clean message text by removing prefixes and formatting"""
        # Remove common prefixes
        prefixes = ['user:', 'human:', 'ai:', 'assistant:', 'saige:', 'question:', 'response:', 'q:', 'a:']
        line_lower = line.lower()
        
        for prefix in prefixes:
            if line_lower.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        
        return line
    
    def _analyze_conversation(self, user_msg: str, ai_response: str, timestamp: float) -> ConversationEntry:
        """Analyze conversation to extract learning signals"""
        
        # Sentiment analysis
        user_sentiment = TextBlob(user_msg).sentiment.polarity
        ai_sentiment = TextBlob(ai_response).sentiment.polarity
        
        # Complexity analysis
        complexity = self._calculate_complexity(user_msg, ai_response)
        
        # Success indicators
        success_indicators = self._detect_success_indicators(user_msg, ai_response)
        
        # Topic extraction
        topic_keywords = self._extract_topics(user_msg + " " + ai_response)
        
        # Response quality estimation
        response_time = self._estimate_response_time(ai_response)
        
        # Generate unique interaction ID
        interaction_id = hashlib.md5(f"{timestamp}{user_msg[:50]}".encode()).hexdigest()[:8]
        
        return ConversationEntry(
            timestamp=timestamp,
            user_message=user_msg,
            ai_response=ai_response,
            sentiment_user=user_sentiment,
            sentiment_ai=ai_sentiment,
            complexity=complexity,
            success_indicators=success_indicators,
            topic_keywords=topic_keywords,
            response_time=response_time,
            interaction_id=interaction_id
        )
    
    def _calculate_complexity(self, user_msg: str, ai_response: str) -> float:
        """Calculate conversation complexity (0-1)"""
        # Factors: length, vocabulary diversity, technical terms, sentence structure
        
        combined_text = user_msg + " " + ai_response
        words = word_tokenize(combined_text.lower())
        
        # Length factor
        length_factor = min(len(words) / 100, 1.0)
        
        # Vocabulary diversity (unique words / total words)
        diversity_factor = len(set(words)) / max(len(words), 1)
        
        # Technical complexity (presence of technical keywords)
        technical_words = sum(1 for word in words if word in self.hormone_triggers['technical_keywords'])
        technical_factor = min(technical_words / 10, 1.0)
        
        # Sentence complexity (average sentence length)
        sentences = sent_tokenize(combined_text)
        avg_sentence_length = sum(len(word_tokenize(sent)) for sent in sentences) / max(len(sentences), 1)
        sentence_factor = min(avg_sentence_length / 20, 1.0)
        
        complexity = (length_factor + diversity_factor + technical_factor + sentence_factor) / 4
        return min(complexity, 1.0)
    
    def _detect_success_indicators(self, user_msg: str, ai_response: str) -> List[str]:
        """Detect indicators of successful interaction"""
        indicators = []
        combined_text = (user_msg + " " + ai_response).lower()
        
        for category, keywords in self.hormone_triggers.items():
            if any(keyword in combined_text for keyword in keywords):
                indicators.append(category)
        
        return indicators
    
    def _extract_topics(self, text: str) -> List[str]:
        """Extract key topics from conversation"""
        words = word_tokenize(text.lower())
        words = [word for word in words if word.isalnum() and word not in self.stop_words]
        
        # Get most common meaningful words
        word_freq = Counter(words)
        topics = [word for word, count in word_freq.most_common(5) if len(word) > 3]
        
        return topics
    
    def _estimate_response_time(self, ai_response: str) -> float:
        """Estimate response generation time based on complexity"""
        # Simple heuristic: longer responses take more time
        word_count = len(word_tokenize(ai_response))
        return max(0.5, min(word_count * 0.1, 10.0))  # 0.5-10 seconds
    
    def generate_stimulus(self, conversations: List[ConversationEntry]) -> Dict[str, float]:
        """Generate hormone stimulus based on conversation analysis"""
        
        if not conversations:
            return self._default_stimulus()
        
        # Analyze recent conversations (last hour)
        recent_cutoff = time.time() - 3600
        recent_convs = [c for c in conversations if c.timestamp > recent_cutoff]
        
        if not recent_convs:
            recent_convs = conversations[-10:]  # Last 10 if no recent ones
        
        # Calculate stimulus values
        stimulus = {}
        
        # Dopamine: Success and positive sentiment
        success_rate = sum(1 for c in recent_convs if 'success_keywords' in c.success_indicators) / len(recent_convs)
        avg_user_sentiment = np.mean([c.sentiment_user for c in recent_convs])
        dopamine = (success_rate * 0.6) + (max(0, avg_user_sentiment) * 0.4)
        stimulus['dopamine'] = min(dopamine, 1.0)
        
        # Curiosity: Questions and learning indicators
        curiosity_rate = sum(1 for c in recent_convs if 'curiosity_keywords' in c.success_indicators) / len(recent_convs)
        avg_complexity = np.mean([c.complexity for c in recent_convs])
        curiosity = (curiosity_rate * 0.7) + (avg_complexity * 0.3)
        stimulus['adrenaline'] = min(curiosity, 1.0)  # Maps to alertness/exploration
        
        # Cortisol: Frustration and negative sentiment
        frustration_rate = sum(1 for c in recent_convs if 'frustration_keywords' in c.success_indicators) / len(recent_convs)
        avg_negative_sentiment = abs(min(0, avg_user_sentiment))
        cortisol = (frustration_rate * 0.8) + (avg_negative_sentiment * 0.2)
        stimulus['cortisol'] = min(cortisol, 1.0)
        
        # Serotonin: Social connection and helpfulness
        social_rate = sum(1 for c in recent_convs if 'social_keywords' in c.success_indicators) / len(recent_convs)
        avg_response_quality = 1.0 - (np.mean([c.response_time for c in recent_convs]) / 10.0)
        serotonin = (social_rate * 0.6) + (max(0, avg_response_quality) * 0.4)
        stimulus['serotonin'] = min(serotonin, 1.0)
        
        # Oxytocin: Sustained positive interactions
        interaction_consistency = len(recent_convs) / 10.0  # Normalize by expected frequency
        avg_positive_sentiment = max(0, avg_user_sentiment)
        oxytocin = (interaction_consistency * 0.5) + (avg_positive_sentiment * 0.5)
        stimulus['oxytocin'] = min(oxytocin, 1.0)
        
        return stimulus
    
    def _default_stimulus(self) -> Dict[str, float]:
        """Default stimulus when no conversations available"""
        return {
            'adrenaline': 0.3,  # Mild curiosity
            'serotonin': 0.4,   # Neutral mood
            'dopamine': 0.2,    # Low reward
            'cortisol': 0.1,    # Minimal stress
            'oxytocin': 0.2     # Minimal social connection
        }
    
    def update_brain_memory(self, conversations: List[ConversationEntry]) -> bool:
        """Update brain file with conversation insights"""
        try:
            brain_file = self.config["brain_file"]
            
            # Load existing brain data
            if os.path.exists(brain_file):
                with open(brain_file, 'r') as f:
                    brain_data = json.load(f)
            else:
                brain_data = {
                    "self_thought_memory": [],
                    "conversation_insights": {}
                }
            
            # Add conversation insights
            recent_convs = conversations[-5:] if conversations else []
            
            for conv in recent_convs:
                memory_entry = {
                    "id": f"conv_{conv.interaction_id}",
                    "timestamp": conv.timestamp,
                    "thought": f"Conversation analysis: {', '.join(conv.success_indicators)}",
                    "search_query": conv.user_message[:100],
                    "web_summary": conv.ai_response[:200],
                    "tweeted": False,
                    "conversation_metadata": {
                        "sentiment_user": conv.sentiment_user,
                        "sentiment_ai": conv.sentiment_ai,
                        "complexity": conv.complexity,
                        "topics": conv.topic_keywords,
                        "success_indicators": conv.success_indicators
                    }
                }
                
                # Avoid duplicates
                existing_ids = [m.get("id", "") for m in brain_data["self_thought_memory"]]
                if memory_entry["id"] not in existing_ids:
                    brain_data["self_thought_memory"].append(memory_entry)
            
            # Update conversation insights summary
            brain_data["conversation_insights"] = {
                "total_conversations": len(conversations),
                "last_updated": time.time(),
                "common_topics": list(Counter([topic for conv in conversations 
                                             for topic in conv.topic_keywords]).most_common(10)),
                "avg_user_sentiment": np.mean([c.sentiment_user for c in conversations]) if conversations else 0,
                "success_rate": len([c for c in conversations if 'success_keywords' in c.success_indicators]) / max(len(conversations), 1)
            }
            
            # Save updated brain data
            with open(brain_file, 'w') as f:
                json.dump(brain_data, f, indent=2)
            
            logger.info(f"Updated brain memory with {len(recent_convs)} conversation insights")
            return True
            
        except Exception as e:
            logger.error(f"Error updating brain memory: {e}")
            return False
    
    def run_continuous(self):
        """Run continuous conversation monitoring"""
        logger.info("Starting continuous conversation monitoring...")
        
        while True:
            try:
                # Parse conversations
                conversations = self.parse_chat_logs()
                
                # Generate stimulus
                stimulus = self.generate_stimulus(conversations)
                
                # Save stimulus data
                stimulus_data = {
                    "timestamp": time.time(),
                    "source": "conversation_feeder",
                    "stimulus": stimulus,
                    "metadata": {
                        "total_conversations": len(conversations),
                        "recent_conversations": len([c for c in conversations 
                                                   if c.timestamp > time.time() - 3600])
                    }
                }
                
                os.makedirs(os.path.dirname(self.config["stimulus_output"]), exist_ok=True)
                with open(self.config["stimulus_output"], 'w') as f:
                    json.dump(stimulus_data, f, indent=2)
                
                # Update brain memory
                self.update_brain_memory(conversations)
                
                logger.info(f"Generated stimulus: {stimulus}")
                
                # Wait for next update
                time.sleep(self.config["update_interval"])
                
            except KeyboardInterrupt:
                logger.info("Conversation feeder stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in conversation feeder: {e}")
                time.sleep(10)  # Brief pause before retry

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = ConversationFeeder()
    
    # Test mode: analyze existing conversations
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        conversations = feeder.parse_chat_logs()
        stimulus = feeder.generate_stimulus(conversations)
        print(f"Found {len(conversations)} conversations")
        print(f"Generated stimulus: {stimulus}")
        feeder.update_brain_memory(conversations)
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()