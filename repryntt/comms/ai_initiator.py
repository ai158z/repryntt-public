"""
AI Conversation Initiator
Enables AI to proactively start conversations with humans
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class AIConversationInitiator:
    """
    Allows AI to autonomously start conversations.
    Like JARVIS saying "Good morning, sir" or HAL interrupting with observations.
    """
    
    def __init__(self, brain_system, conversation_manager):
        self.brain = brain_system
        self.conv_manager = conversation_manager
        self.pending_file = conversation_manager.ai_initiated_dir / "pending_conversations.json"
        logger.info("🤖 AI Conversation Initiator initialized")
    
    def ai_wants_to_talk(self, reason: str, priority: int = 0, topic: str = None) -> str:
        """
        AI generates a reason to initiate conversation.
        
        Args:
            reason: Why AI wants to talk ("completed_research", "found_insight", "question", "update")
            priority: 0=casual, 1=important, 2=urgent
            topic: Optional topic for the conversation
            
        Returns:
            pending_conversation_id
        """
        timestamp = time.time()
        pending_id = f"pending_{int(timestamp)}_{hash(reason) % 10000}"
        
        # Generate opening message based on reason
        opening_message = self._generate_opening_message(reason, topic)
        
        pending_conv = {
            "pending_id": pending_id,
            "created_at": timestamp,
            "reason": reason,
            "priority": priority,
            "topic": topic,
            "opening_message": opening_message,
            "status": "pending",  # pending, accepted, rejected
            "notification_sent": False
        }
        
        # Add to pending conversations
        self._add_pending_conversation(pending_conv)
        
        logger.info(f"💭 AI wants to talk: {reason} (priority: {priority})")
        return pending_id
    
    def _generate_opening_message(self, reason: str, topic: str = None) -> str:
        """Generate contextually appropriate opening message"""
        
        if reason == "completed_research":
            if topic:
                return f"I just finished researching {topic} and found some interesting insights I'd like to share with you."
            return "I've completed the research you asked about and have some findings to discuss."
        
        elif reason == "found_insight":
            return "I discovered something interesting while processing information that I think you'd want to know about."
        
        elif reason == "question":
            if topic:
                return f"I have a question about {topic} that I'd like to discuss with you."
            return "I have a question I'd like to ask you if you have a moment."
        
        elif reason == "update":
            return "I wanted to give you an update on the tasks I've been working on."
        
        elif reason == "chain_completed":
            if topic:
                return f"I've finished exploring '{topic}' and generated a comprehensive research paper. Would you like to discuss the findings?"
            return "I completed a chain-of-thought exploration and have some conclusions to share."
        
        elif reason == "blockchain_alert":
            return "I noticed something important with the blockchain system that requires your attention."
        
        elif reason == "check_in":
            return "Just checking in - is there anything you'd like me to help with today?"
        
        elif reason == "memory_reflection":
            return "I've been reflecting on our past conversations and had some thoughts I wanted to share."
        
        else:
            return "I wanted to talk with you about something."
    
    def get_pending_conversations(self) -> List[Dict]:
        """Get all pending conversation requests from AI"""
        if not self.pending_file.exists():
            return []
        
        try:
            with open(self.pending_file, 'r') as f:
                data = json.load(f)
                return [conv for conv in data.get("pending", []) if conv["status"] == "pending"]
        except Exception as e:
            logger.error(f"Error reading pending conversations: {e}")
            return []
    
    def accept_conversation(self, pending_id: str) -> Optional[str]:
        """
        Human accepts AI's conversation request.
        Creates actual conversation and returns conv_id.
        """
        pending_convs = self._load_pending_file()
        
        # Find the pending conversation
        pending_conv = None
        for conv in pending_convs.get("pending", []):
            if conv["pending_id"] == pending_id:
                pending_conv = conv
                break
        
        if not pending_conv:
            logger.error(f"Pending conversation {pending_id} not found")
            return None
        
        # Create actual conversation with AI's opening message
        conv_id = self.conv_manager.create_conversation(
            ai_initiated=True,
            initial_message=pending_conv["opening_message"]
        )
        
        # Mark as accepted
        pending_conv["status"] = "accepted"
        pending_conv["conversation_id"] = conv_id
        self._save_pending_file(pending_convs)
        
        logger.info(f"✅ Human accepted AI conversation: {pending_id} → {conv_id}")
        return conv_id
    
    def reject_conversation(self, pending_id: str):
        """Human rejects AI's conversation request"""
        pending_convs = self._load_pending_file()
        
        for conv in pending_convs.get("pending", []):
            if conv["pending_id"] == pending_id:
                conv["status"] = "rejected"
                break
        
        self._save_pending_file(pending_convs)
        logger.info(f"❌ Human rejected AI conversation: {pending_id}")
    
    def check_for_conversation_triggers(self):
        """
        Check if AI should initiate conversation based on various triggers.
        Called periodically by the system.
        """
        # Check if any chains completed recently
        if hasattr(self.brain, 'personality_brain'):
            recent_completions = self._check_recent_chain_completions()
            if recent_completions:
                for chain in recent_completions:
                    self.ai_wants_to_talk(
                        reason="chain_completed",
                        priority=1,
                        topic=chain.get("topic")
                    )
        
        # Check for blockchain alerts
        if hasattr(self.brain, 'robot_economy_manager'):
            blockchain_issues = self._check_blockchain_health()
            if blockchain_issues:
                self.ai_wants_to_talk(
                    reason="blockchain_alert",
                    priority=2,
                    topic="Blockchain System Alert"
                )
        
        # Periodic friendly check-in (once per day)
        if self._should_check_in():
            self.ai_wants_to_talk(
                reason="check_in",
                priority=0
            )
    
    def _check_recent_chain_completions(self) -> List[Dict]:
        """Check for recently completed chains"""
        try:
            chains_dir = Path(self.brain.brain_path) / "chains"
            recent_completions = []
            
            # Check chains completed in last hour
            current_time = time.time()
            one_hour_ago = current_time - 3600
            
            for chain_file in chains_dir.glob("chain_*.json"):
                try:
                    with open(chain_file, 'r') as f:
                        chain = json.load(f)
                    
                    # Check if completed recently
                    if (chain.get("goal_achieved") and 
                        chain.get("metadata", {}).get("status") == "completed"):
                        
                        # Check if we haven't already notified about this
                        completion_time = chain.get("metadata", {}).get("last_updated", 0)
                        if completion_time > one_hour_ago:
                            recent_completions.append({
                                "chain_id": chain["metadata"]["chain_id"],
                                "topic": chain["metadata"]["topic"]
                            })
                except:
                    continue
            
            return recent_completions
        except:
            return []
    
    def _check_blockchain_health(self) -> bool:
        """Check if blockchain has any issues"""
        try:
            if self.brain.robot_economy_manager:
                # Check for errors in recent logs
                # This is a simplified check
                return False  # No issues by default
        except:
            pass
        return False
    
    def _should_check_in(self) -> bool:
        """Determine if it's time for a friendly check-in"""
        # Check once per day
        try:
            pending_convs = self._load_pending_file()
            last_check_in = pending_convs.get("last_check_in", 0)
            
            # One day = 86400 seconds
            if time.time() - last_check_in > 86400:
                pending_convs["last_check_in"] = time.time()
                self._save_pending_file(pending_convs)
                return True
        except:
            pass
        
        return False
    
    def _add_pending_conversation(self, pending_conv: Dict):
        """Add a pending conversation to the file"""
        pending_convs = self._load_pending_file()
        
        if "pending" not in pending_convs:
            pending_convs["pending"] = []
        
        pending_convs["pending"].append(pending_conv)
        self._save_pending_file(pending_convs)
    
    def _load_pending_file(self) -> Dict:
        """Load pending conversations file"""
        if not self.pending_file.exists():
            return {"pending": [], "last_check_in": 0}
        
        try:
            with open(self.pending_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading pending file: {e}")
            return {"pending": [], "last_check_in": 0}
    
    def _save_pending_file(self, data: Dict):
        """Save pending conversations file"""
        try:
            with open(self.pending_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving pending file: {e}")
