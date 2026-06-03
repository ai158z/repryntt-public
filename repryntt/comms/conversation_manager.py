"""
Persistent Conversation Manager for Human-AI Dialogue
Enables indefinite conversations with full context retention
"""

import json
import time
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class ConversationManager:
    """
    Manages persistent human-AI conversations.
    Unlike chains (AI talking to itself), this is human ↔ AI dialogue.
    """
    
    def __init__(self, brain_system):
        self.brain = brain_system
        self.conversations_dir = Path(brain_system.brain_path) / "conversations"
        self.conversations_dir.mkdir(exist_ok=True)
        
        self.ai_initiated_dir = self.conversations_dir / "ai_initiated"
        self.ai_initiated_dir.mkdir(exist_ok=True)
        
        self.active_conversation = None
        logger.info("💬 Conversation Manager initialized")
    
    def create_conversation(self, ai_initiated: bool = False, initial_message: str = None) -> str:
        """Create a new conversation"""
        conv_id = f"conv_{int(time.time())}_{hash(str(time.time())) % 10000}"
        
        conversation = {
            "conversation_id": conv_id,
            "created_at": time.time(),
            "last_updated": time.time(),
            "participants": ["human", "SAIGE"],
            "voice_enabled": False,
            "ai_initiated": ai_initiated,
            "messages": [],
            "context_summary": "",
            "metadata": {
                "total_messages": 0,
                "human_messages": 0,
                "ai_messages": 0,
                "tools_used": []
            }
        }
        
        # If AI initiated, add the opening message
        if ai_initiated and initial_message:
            conversation["messages"].append({
                "timestamp": time.time(),
                "sender": "SAIGE",
                "content": initial_message,
                "audio_file": None,
                "tool_results": None
            })
            conversation["metadata"]["total_messages"] = 1
            conversation["metadata"]["ai_messages"] = 1
        
        self._save_conversation(conv_id, conversation)
        self.active_conversation = conv_id
        
        logger.info(f"✨ Created conversation: {conv_id} (AI initiated: {ai_initiated})")
        return conv_id
    
    def load_conversation(self, conv_id: str) -> Optional[Dict]:
        """Load a conversation by ID"""
        conv_path = self.conversations_dir / f"{conv_id}.json"
        
        if not conv_path.exists():
            logger.error(f"Conversation {conv_id} not found")
            return None
        
        try:
            with open(conv_path, 'r', encoding='utf-8') as f:
                conversation = json.load(f)
            
            self.active_conversation = conv_id
            logger.info(f"📂 Loaded conversation: {conv_id} ({len(conversation['messages'])} messages)")
            return conversation
        except Exception as e:
            logger.error(f"Error loading conversation {conv_id}: {e}")
            return None
    
    def add_message(self, conv_id: str, sender: str, content: str, 
                    tool_results: Dict = None, audio_file: str = None) -> bool:
        """Add a message to the conversation"""
        conversation = self.load_conversation(conv_id)
        if not conversation:
            return False
        
        message = {
            "timestamp": time.time(),
            "sender": sender,
            "content": content,
            "audio_file": audio_file,
            "tool_results": tool_results
        }
        
        conversation["messages"].append(message)
        conversation["last_updated"] = time.time()
        conversation["metadata"]["total_messages"] += 1
        
        if sender == "human":
            conversation["metadata"]["human_messages"] += 1
        else:
            conversation["metadata"]["ai_messages"] += 1
        
        # Track tools used
        if tool_results and tool_results.get('tool_calls_executed'):
            for tool_call in tool_results['tool_calls_executed']:
                tool_name = tool_call.get('tool', 'unknown')
                if tool_name not in conversation["metadata"]["tools_used"]:
                    conversation["metadata"]["tools_used"].append(tool_name)
        
        self._save_conversation(conv_id, conversation)
        return True
    
    def get_conversation_history(self, conv_id: str, limit: int = None) -> List[Dict]:
        """Get conversation history (optionally limited to recent messages)"""
        conversation = self.load_conversation(conv_id)
        if not conversation:
            return []
        
        messages = conversation["messages"]
        if limit:
            messages = messages[-limit:]
        
        return messages
    
    def get_context_for_ai(self, conv_id: str, max_messages: int = 20) -> str:
        """
        Build context string for AI from conversation history.
        Returns formatted conversation for AI prompt.
        """
        conversation = self.load_conversation(conv_id)
        if not conversation:
            return ""
        
        # Get recent messages
        messages = conversation["messages"][-max_messages:]
        
        context = f"""ACTIVE CONVERSATION (ID: {conv_id})
Created: {datetime.fromtimestamp(conversation['created_at']).strftime('%Y-%m-%d %H:%M')}
Voice Mode: {'Enabled' if conversation['voice_enabled'] else 'Disabled'}
Total Messages: {conversation['metadata']['total_messages']}

CONVERSATION HISTORY:
"""
        
        for msg in messages:
            timestamp = datetime.fromtimestamp(msg['timestamp']).strftime('%H:%M:%S')
            sender = "Human" if msg['sender'] == "human" else "You (SAIGE)"
            content = msg['content']
            
            context += f"\n[{timestamp}] {sender}: {content}\n"
            
            # Add tool usage info if present
            if msg.get('tool_results') and msg['tool_results'].get('tool_calls_executed'):
                tools = [t['tool'] for t in msg['tool_results']['tool_calls_executed']]
                context += f"  └─ Tools used: {', '.join(tools)}\n"
        
        context += f"\n---\nYou are continuing this conversation. Respond naturally to the human's last message."
        
        return context
    
    def list_conversations(self, limit: int = 10) -> List[Dict]:
        """List all conversations, most recent first"""
        conversations = []
        
        for conv_file in sorted(self.conversations_dir.glob("conv_*.json"), reverse=True):
            try:
                with open(conv_file, 'r', encoding='utf-8') as f:
                    conv = json.load(f)
                    
                    # Extract summary info
                    summary = {
                        "conversation_id": conv["conversation_id"],
                        "created_at": conv["created_at"],
                        "last_updated": conv["last_updated"],
                        "message_count": conv["metadata"]["total_messages"],
                        "ai_initiated": conv.get("ai_initiated", False),
                        "preview": conv["messages"][-1]["content"][:50] + "..." if conv["messages"] else "Empty"
                    }
                    conversations.append(summary)
                    
                    if len(conversations) >= limit:
                        break
            except Exception as e:
                logger.error(f"Error reading conversation {conv_file}: {e}")
        
        return conversations
    
    def set_voice_enabled(self, conv_id: str, enabled: bool):
        """Enable/disable voice for a conversation"""
        conversation = self.load_conversation(conv_id)
        if conversation:
            conversation["voice_enabled"] = enabled
            self._save_conversation(conv_id, conversation)
            logger.info(f"🎤 Voice {'enabled' if enabled else 'disabled'} for conversation {conv_id}")
    
    def _save_conversation(self, conv_id: str, conversation: Dict):
        """Save conversation to disk"""
        conv_path = self.conversations_dir / f"{conv_id}.json"
        
        try:
            with open(conv_path, 'w', encoding='utf-8') as f:
                json.dump(conversation, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving conversation {conv_id}: {e}")
    
    def export_conversation(self, conv_id: str, format: str = "txt") -> Optional[str]:
        """Export conversation to text file"""
        conversation = self.load_conversation(conv_id)
        if not conversation:
            return None
        
        export_path = self.conversations_dir / f"{conv_id}_export.{format}"
        
        try:
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(f"Conversation Export: {conv_id}\n")
                f.write(f"Created: {datetime.fromtimestamp(conversation['created_at'])}\n")
                f.write(f"Total Messages: {conversation['metadata']['total_messages']}\n")
                f.write("=" * 80 + "\n\n")
                
                for msg in conversation["messages"]:
                    timestamp = datetime.fromtimestamp(msg['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                    sender = msg['sender'].upper()
                    content = msg['content']
                    
                    f.write(f"[{timestamp}] {sender}:\n{content}\n\n")
            
            logger.info(f"📄 Exported conversation to: {export_path}")
            return str(export_path)
        except Exception as e:
            logger.error(f"Error exporting conversation: {e}")
            return None
