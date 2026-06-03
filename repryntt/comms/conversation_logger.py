#!/usr/bin/env python3
"""
Autonomous Conversation Logger - Full, Untruncated Storage

SAVES COMPLETE SELF-AUTONOMOUS PROMPTS AND RESPONSES WITHOUT ANY TRUNCATION.
No matter how long the AI response is, it will be stored in full length.
This ensures complete conversation history for learning and analysis.
"""

import json
import time
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class AutonomousConversationLogger:
    """
    Logs full self-autonomous AI conversations without truncation.
    
    Creates organized storage in brain/autonomous_conversations/
    Each conversation gets its own file with full prompts and responses.
    """
    
    def __init__(self, brain_path: str):
        self.brain_path = Path(brain_path)
        self.conversations_dir = self.brain_path / "autonomous_conversations"
        self.conversations_dir.mkdir(exist_ok=True)
        
        # Current conversation session
        self.current_conversation_id = None
        self.current_conversation = None
        self.exchanges_count = 0
        
        # Try to resume recent conversation on startup
        self._resume_recent_conversation()
    
    def _resume_recent_conversation(self) -> None:
        """Resume the most recent conversation if it's within 24 hours"""
        try:
            recent_files = self.get_recent_conversations(limit=1)
            if recent_files:
                recent_file = recent_files[0]
                conversation_id = Path(recent_file).stem  # Remove .json extension
                
                # Load the conversation
                conversation_data = self.load_conversation(conversation_id)
                if conversation_data:
                    started_at = conversation_data.get('started_at', 0)
                    current_time = time.time()
                    
                    # Check if within 24 hours (86400 seconds)
                    if current_time - started_at < 86400:
                        logger.info(f"📝 Resuming recent conversation: {conversation_id} (started {datetime.fromtimestamp(started_at).strftime('%Y-%m-%d %H:%M:%S')})")
                        
                        self.current_conversation_id = conversation_id
                        self.current_conversation = conversation_data
                        self.exchanges_count = len(conversation_data.get('exchanges', []))
                        
                        # Update metadata to show resumption
                        if 'metadata' not in self.current_conversation:
                            self.current_conversation['metadata'] = {}
                        self.current_conversation['metadata']['resumed_at'] = current_time
                        self.current_conversation['metadata']['resumed_at_human'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        return
                
            logger.info("📝 No recent conversation to resume (starting fresh)")
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to resume recent conversation: {e}")
    
    def start_conversation(self, topic: str = "General exploration", context: str = "") -> str:
        """Start a new conversation session, or return existing if already active"""
        # If we already have an active conversation, return its ID
        if self.current_conversation_id and self.current_conversation:
            logger.info(f"📝 Using existing conversation: {self.current_conversation_id}")
            return self.current_conversation_id
        
        # Start a new conversation
        conversation_id = f"conv_{int(time.time())}_{hash(topic) % 10000}"
        
        self.current_conversation_id = conversation_id
        self.exchanges_count = 0
        self.current_conversation = {
            "conversation_id": conversation_id,
            "topic": topic,
            "context": context,
            "started_at": time.time(),
            "started_at_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exchanges": [],
            "metadata": {
                "total_prompt_chars": 0,
                "total_response_chars": 0,
                "total_exchanges": 0
            }
        }
        
        return conversation_id
    
    def log_exchange(self, 
                    prompt: str, 
                    response: str, 
                    tools_included: bool = False,
                    metadata: Optional[Dict] = None) -> None:
        """
        Log a single prompt-response exchange with FULL text (no truncation).
        
        Args:
            prompt: The full prompt sent to AI (no truncation)
            response: The full AI response (no truncation)
            tools_included: Whether tools were included in the prompt
            metadata: Additional metadata (tokens, cost, etc.)
        """
        if not self.current_conversation:
            # Auto-start conversation if not started
            self.start_conversation("Autonomous exploration")
        
        self.exchanges_count += 1
        
        exchange = {
            "exchange_number": self.exchanges_count,
            "timestamp": time.time(),
            "timestamp_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": {
                "text": prompt,  # FULL TEXT - NO TRUNCATION
                "length_chars": len(prompt),
                "tools_included": tools_included
            },
            "response": {
                "text": response,  # FULL TEXT - NO TRUNCATION
                "length_chars": len(response)
            },
            "metadata": metadata or {}
        }
        
        self.current_conversation["exchanges"].append(exchange)
        
        # Check for potential truncation (responses shouldn't end mid-sentence)
        if response.strip() and not response.rstrip().endswith(('.', '!', '?', ':', ';', ')', ']', '}', '"', "'")):
            logger.warning(f"⚠️  Response may be truncated - doesn't end with sentence terminator: '{response[-50:]}...'")

        # Update totals
        self.current_conversation["metadata"]["total_prompt_chars"] += len(prompt)
        self.current_conversation["metadata"]["total_response_chars"] += len(response)
        self.current_conversation["metadata"]["total_exchanges"] = self.exchanges_count

        # Check if conversation has exceeded 24 hours - if so, end it and start a new one
        current_time = time.time()
        started_at = self.current_conversation.get("started_at", current_time)
        if current_time - started_at >= 86400:  # 24 hours in seconds
            logger.info(f"📅 Conversation {self.current_conversation_id} has exceeded 24 hours, ending and starting new daily log")
            self.end_conversation("24_hour_limit_reached")
            # Start a new conversation for the next 24-hour cycle
            self.start_conversation("Daily operations log", "Continued autonomous reasoning session")

        # Save after each exchange (in case of crash)
        self._save_current_conversation()
    
    def end_conversation(self, outcome: str = "completed") -> str:
        """End the current conversation and save final state"""
        if not self.current_conversation:
            return ""
        
        self.current_conversation["ended_at"] = time.time()
        self.current_conversation["ended_at_human"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.current_conversation["outcome"] = outcome
        
        # Calculate duration
        duration = self.current_conversation["ended_at"] - self.current_conversation["started_at"]
        self.current_conversation["duration_seconds"] = duration
        self.current_conversation["duration_human"] = f"{int(duration // 60)}m {int(duration % 60)}s"
        
        # Final save
        filepath = self._save_current_conversation()
        
        # Clear current conversation
        conversation_id = self.current_conversation_id
        self.current_conversation = None
        self.current_conversation_id = None
        self.exchanges_count = 0
        
        return conversation_id
    
    def _save_current_conversation(self) -> Path:
        """Save current conversation to disk"""
        if not self.current_conversation:
            return None
        
        filename = f"{self.current_conversation_id}.json"
        filepath = self.conversations_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(self.current_conversation, f, indent=2, default=str)
        
        return filepath
    
    def get_recent_conversations(self, limit: int = 10) -> list:
        """Get most recent conversation files"""
        conversation_files = sorted(
            self.conversations_dir.glob("conv_*.json"),
            key=os.path.getmtime,
            reverse=True
        )[:limit]
        
        return [str(f) for f in conversation_files]
    
    def load_conversation(self, conversation_id: str) -> Optional[Dict]:
        """Load a specific conversation by ID"""
        filepath = self.conversations_dir / f"{conversation_id}.json"
        
        if not filepath.exists():
            return None
        
        with open(filepath, 'r') as f:
            return json.load(f)
    
    def get_conversation_summary(self, conversation_id: str) -> Optional[str]:
        """Get a brief summary of a conversation"""
        conv = self.load_conversation(conversation_id)
        if not conv:
            return None
        
        summary = f"""
Conversation: {conv['conversation_id']}
Topic: {conv['topic']}
Started: {conv['started_at_human']}
Duration: {conv.get('duration_human', 'In progress')}
Exchanges: {conv['metadata']['total_exchanges']}
Total Prompt Text: {conv['metadata']['total_prompt_chars']:,} chars
Total Response Text: {conv['metadata']['total_response_chars']:,} chars
"""
        return summary.strip()
    
    def search_conversations(self, keyword: str, limit: int = 20) -> list:
        """Search conversations by keyword in topic or exchanges"""
        results = []
        
        for conv_file in sorted(
            self.conversations_dir.glob("conv_*.json"),
            key=os.path.getmtime,
            reverse=True
        )[:100]:  # Search last 100 conversations
            
            try:
                with open(conv_file, 'r') as f:
                    conv = json.load(f)
                
                # Search in topic
                if keyword.lower() in conv['topic'].lower():
                    results.append({
                        "conversation_id": conv['conversation_id'],
                        "topic": conv['topic'],
                        "match_type": "topic",
                        "started_at": conv['started_at_human']
                    })
                    continue
                
                # Search in exchanges
                for exchange in conv['exchanges']:
                    if (keyword.lower() in exchange['prompt']['text'].lower() or
                        keyword.lower() in exchange['response']['text'].lower()):
                        results.append({
                            "conversation_id": conv['conversation_id'],
                            "topic": conv['topic'],
                            "match_type": "exchange",
                            "started_at": conv['started_at_human']
                        })
                        break
                
                if len(results) >= limit:
                    break
            
            except Exception:
                continue
        
        return results
    
    def export_conversation_as_text(self, conversation_id: str) -> str:
        """Export conversation as readable text format"""
        conv = self.load_conversation(conversation_id)
        if not conv:
            return ""
        
        text = f"""
{'=' * 80}
AUTONOMOUS CONVERSATION
{'=' * 80}

ID: {conv['conversation_id']}
Topic: {conv['topic']}
Started: {conv['started_at_human']}
Duration: {conv.get('duration_human', 'In progress')}
Total Exchanges: {conv['metadata']['total_exchanges']}

"""
        
        for exchange in conv['exchanges']:
            text += f"""
{'-' * 80}
EXCHANGE #{exchange['exchange_number']} - {exchange['timestamp_human']}
{'-' * 80}

PROMPT ({exchange['prompt']['length_chars']:,} chars, tools: {exchange['prompt']['tools_included']}):
{exchange['prompt']['text']}

RESPONSE ({exchange['response']['length_chars']:,} chars):
{exchange['response']['text']}

"""
        
        text += f"\n{'=' * 80}\n"
        return text


# Global instance for brain system to use
_conversation_logger = None


def get_conversation_logger(brain_path: str) -> AutonomousConversationLogger:
    """Get or create the global conversation logger instance"""
    global _conversation_logger
    if _conversation_logger is None:
        _conversation_logger = AutonomousConversationLogger(brain_path)
    return _conversation_logger
