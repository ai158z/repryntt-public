"""
Twitter Integration for SAIGE Brain System

Allows the AI to autonomously post to Twitter, reply to mentions, and manage its Twitter presence.
Bridges the existing Twitter bot functionality with SAIGE's brain network.
"""

import os
import sys
import json
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

try:
    from twitterbot import (
        TwitterBot, 
        generate_response,
        load_brain as load_twitter_brain,
        save_brain as save_twitter_brain,
        brain_data as twitter_brain_data,
        TWITTER_USERNAME,
        TWITTER_PASSWORD
    )
except ImportError:
    TwitterBot = None  # twitterbot not available

logger = logging.getLogger("TwitterIntegration")
logger.setLevel(logging.INFO)


class TwitterInterface:
    """
    Interface between SAIGE brain system and Twitter bot.
    Provides tools for autonomous Twitter interaction.
    """
    
    def __init__(self, brain_system=None):
        """
        Initialize Twitter interface.
        
        Args:
            brain_system: Reference to the main brain system for memory sync
        """
        self.brain_system = brain_system
        self.bot = None
        self.is_initialized = False
        self.last_post_time = 0
        self.post_cooldown = 900  # 15 minutes between posts
        # Derive twitter brain path from brain_system or fallback to repo root
        if brain_system and hasattr(brain_system, 'node2040_brain_path'):
            self.twitter_brain_path = str(brain_system.node2040_brain_path)
        else:
            self.twitter_brain_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'node2040_brain.json'
            )
        
        logger.info("🐦 Twitter Interface initialized")
    
    def initialize_bot(self, headless=True):
        """
        Initialize the Twitter bot (Selenium).
        
        Args:
            headless: Whether to run browser in headless mode
        """
        try:
            if self.is_initialized:
                logger.info("Twitter bot already initialized")
                return True
            
            logger.info("🐦 Initializing Twitter bot...")
            self.bot = TwitterBot(
                username=TWITTER_USERNAME,
                password=TWITTER_PASSWORD,
                show_browser=not headless,
                curiosity=0.7
            )
            self.is_initialized = True
            logger.info("✅ Twitter bot initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Twitter bot: {e}")
            return False
    
    def sync_memories_to_twitter_brain(self):
        """
        Sync SAIGE brain memories to Twitter bot brain.
        Merges episodic memories and thoughts.
        """
        try:
            if not self.brain_system:
                return
            
            # Load Twitter brain
            twitter_brain = load_twitter_brain()
            
            # Get SAIGE memories
            saige_memories = self.brain_system.brain_network.episodic_memory[-50:]  # Last 50
            
            # Convert to Twitter brain format
            for memory in saige_memories:
                thought_entry = {
                    "id": memory.get("memory_id", "unknown"),
                    "timestamp": memory.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "thought": memory.get("content", ""),
                    "search_query": "",
                    "web_summary": "",
                    "category": memory.get("category", "general")
                }
                
                # Check if not already in Twitter brain
                existing_ids = [t.get("id") for t in twitter_brain.get("self_thought_memory", [])]
                if thought_entry["id"] not in existing_ids:
                    twitter_brain.setdefault("self_thought_memory", []).append(thought_entry)
            
            # Save updated Twitter brain
            save_twitter_brain()
            logger.info(f"✅ Synced {len(saige_memories)} memories to Twitter brain")
            
        except Exception as e:
            logger.error(f"Error syncing memories: {e}")
    
    async def post_autonomous_tweet(self, content: str = None, generate_image: bool = False) -> Dict[str, Any]:
        """
        Post a tweet autonomously based on AI's current thoughts.
        
        Args:
            content: Tweet content (if None, AI generates it)
            generate_image: Whether to generate an accompanying image
            
        Returns:
            Dict with success status and details
        """
        try:
            # Check cooldown
            current_time = time.time()
            if current_time - self.last_post_time < self.post_cooldown:
                remaining = int(self.post_cooldown - (current_time - self.last_post_time))
                return {
                    "success": False,
                    "error": f"Cooldown active ({remaining}s remaining)"
                }
            
            # Initialize bot if needed
            if not self.is_initialized:
                if not self.initialize_bot(headless=True):
                    return {"success": False, "error": "Failed to initialize Twitter bot"}
            
            # Sync memories before posting
            self.sync_memories_to_twitter_brain()
            
            # Generate content if not provided
            if content is None:
                logger.info("🤖 Generating autonomous tweet content...")
                
                # Get AI's current thoughts from brain
                if self.brain_system:
                    recent_thoughts = self.brain_system.brain_network.episodic_memory[-5:]
                    context = " ".join([m.get("content", "") for m in recent_thoughts])
                else:
                    context = "autonomous thought about machine intelligence and multiplanetary civilization"
                
                # Use existing generate_response function
                response = await generate_response(
                    query=context,
                    is_mention=False,
                    recent_topics=[]
                )
                
                if response and isinstance(response, dict):
                    content = response.get("text", "")
                else:
                    return {"success": False, "error": "Failed to generate content"}
            
            # Post tweet
            logger.info(f"📤 Posting tweet: {content[:50]}...")
            
            image_path = None
            if generate_image:
                # Generate image if requested
                from twitterbot import generate_image_prompt, generate_image
                image_prompt = await generate_image_prompt(content)
                image_path = await generate_image(image_prompt)
            
            success = await self.bot.post_tweet(
                text=content,
                img_path=image_path
            )
            
            if success:
                self.last_post_time = current_time
                logger.info("✅ Tweet posted successfully")
                return {
                    "success": True,
                    "content": content,
                    "image": image_path is not None,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return {"success": False, "error": "Tweet posting failed"}
            
        except Exception as e:
            logger.error(f"Error posting autonomous tweet: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def reply_to_mention(self, mention_url: str, reply_text: str = None) -> Dict[str, Any]:
        """
        Reply to a Twitter mention.
        
        Args:
            mention_url: URL of the tweet to reply to
            reply_text: Reply content (if None, AI generates it)
            
        Returns:
            Dict with success status and details
        """
        try:
            if not self.is_initialized:
                if not self.initialize_bot(headless=True):
                    return {"success": False, "error": "Failed to initialize Twitter bot"}
            
            # Get mention context
            conversation = self.bot.get_conversation_context_by_url(mention_url)
            
            # Generate reply if not provided
            if reply_text is None:
                # Extract original tweet text
                original_text = conversation[0].get("text", "") if conversation else ""
                user = conversation[0].get("user", "") if conversation else "user"
                
                # Generate contextual reply
                response = await generate_response(
                    query=original_text,
                    is_mention=True,
                    user=user,
                    conversation=conversation
                )
                
                if response and isinstance(response, dict):
                    reply_text = response.get("text", "")
                else:
                    return {"success": False, "error": "Failed to generate reply"}
            
            # Post reply
            logger.info(f"💬 Replying to {mention_url}")
            success = self.bot.reply_in_thread_by_url(
                tweet_url=mention_url,
                reply_text=reply_text,
                tweet_text=original_text if conversation else "",
                user=user if conversation else ""
            )
            
            if success:
                return {
                    "success": True,
                    "reply": reply_text,
                    "mention_url": mention_url
                }
            else:
                return {"success": False, "error": "Reply posting failed"}
            
        except Exception as e:
            logger.error(f"Error replying to mention: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def check_and_respond_to_mentions(self, max_replies: int = 3) -> Dict[str, Any]:
        """
        Check for new mentions and respond to them.
        
        Args:
            max_replies: Maximum number of mentions to reply to
            
        Returns:
            Dict with response statistics
        """
        try:
            if not self.is_initialized:
                if not self.initialize_bot(headless=True):
                    return {"success": False, "error": "Failed to initialize Twitter bot"}
            
            # Get mentions
            mentions = self.bot.get_notifications_mentions(max_notifications=max_replies)
            
            if not mentions:
                return {"success": True, "replied_count": 0, "message": "No new mentions"}
            
            replied_count = 0
            for mention in mentions[:max_replies]:
                try:
                    result = await self.reply_to_mention(mention.get("url", ""))
                    if result.get("success"):
                        replied_count += 1
                        await asyncio.sleep(30)  # Delay between replies
                except Exception as e:
                    logger.error(f"Error replying to mention: {e}")
                    continue
            
            return {
                "success": True,
                "replied_count": replied_count,
                "total_mentions": len(mentions)
            }
            
        except Exception as e:
            logger.error(f"Error checking mentions: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    def get_twitter_stats(self) -> Dict[str, Any]:
        """
        Get Twitter account statistics and status.
        
        Returns:
            Dict with Twitter stats
        """
        try:
            twitter_brain = load_twitter_brain()
            
            return {
                "username": TWITTER_USERNAME,
                "is_initialized": self.is_initialized,
                "last_post_time": self.last_post_time,
                "cooldown_remaining": max(0, int(self.post_cooldown - (time.time() - self.last_post_time))),
                "total_thoughts": len(twitter_brain.get("self_thought_memory", [])),
                "total_curiosities": len(twitter_brain.get("curiosities", [])),
                "conscious_state": twitter_brain.get("conscious_state", {})
            }
            
        except Exception as e:
            logger.error(f"Error getting Twitter stats: {e}")
            return {"error": str(e)}
    
    def cleanup(self):
        """Clean up Twitter bot resources."""
        try:
            if self.bot:
                self.bot.close()
                logger.info("🐦 Twitter bot closed")
        except Exception as e:
            logger.error(f"Error closing Twitter bot: {e}")


# Singleton instance
_twitter_interface = None


def get_twitter_interface(brain_system=None) -> TwitterInterface:
    """Get or create the Twitter interface singleton."""
    global _twitter_interface
    if _twitter_interface is None:
        _twitter_interface = TwitterInterface(brain_system=brain_system)
    return _twitter_interface


# Tool functions for brain system integration

async def post_tweet_tool(content: str = None, generate_image: bool = False) -> str:
    """
    Tool for brain system: Post a tweet to Twitter.
    
    Args:
        content: Tweet content (optional, AI generates if not provided)
        generate_image: Whether to generate an image
        
    Returns:
        Success message or error
    """
    try:
        twitter = get_twitter_interface()
        result = await twitter.post_autonomous_tweet(content=content, generate_image=generate_image)
        
        if result.get("success"):
            return f"✅ Tweet posted successfully: {result.get('content', '')[:50]}..."
        else:
            return f"❌ Tweet failed: {result.get('error', 'Unknown error')}"
            
    except Exception as e:
        return f"❌ Error posting tweet: {str(e)}"


async def check_twitter_mentions_tool() -> str:
    """
    Tool for brain system: Check and respond to Twitter mentions.
    
    Returns:
        Summary of mention responses
    """
    try:
        twitter = get_twitter_interface()
        result = await twitter.check_and_respond_to_mentions(max_replies=3)
        
        if result.get("success"):
            replied = result.get("replied_count", 0)
            total = result.get("total_mentions", 0)
            return f"✅ Responded to {replied}/{total} Twitter mentions"
        else:
            return f"❌ Mention check failed: {result.get('error', 'Unknown error')}"
            
    except Exception as e:
        return f"❌ Error checking mentions: {str(e)}"


def get_twitter_status_tool() -> str:
    """
    Tool for brain system: Get Twitter account status.
    
    Returns:
        Twitter status information
    """
    try:
        twitter = get_twitter_interface()
        stats = twitter.get_twitter_stats()
        
        if "error" in stats:
            return f"❌ Error getting status: {stats['error']}"
        
        return f"""
🐦 Twitter Status:
- Account: @{stats.get('username', 'unknown')}
- Bot initialized: {stats.get('is_initialized', False)}
- Cooldown: {stats.get('cooldown_remaining', 0)}s remaining
- Total thoughts: {stats.get('total_thoughts', 0)}
- Total curiosities: {stats.get('total_curiosities', 0)}
"""
    except Exception as e:
        return f"❌ Error getting Twitter status: {str(e)}"
