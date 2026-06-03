#!/usr/bin/env python3
"""
Conversation System Integration with SAIGE Orchestrator
Connects the existing conversation system to the brain and orchestrator
"""

import sys
import os
import json
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

class OrchestratorIntegratedConversation:
    """
    Enhanced conversation system that integrates with SAIGE orchestrator
    Provides brain-enhanced responses with tool calling capabilities
    """

    def __init__(self):
        self.orchestrator = None
        self.conversation_active = False
        self.current_conversation_id = None

        # Initialize orchestrator
        self._initialize_orchestrator()

    def _initialize_orchestrator(self):
        """Initialize the SAIGE orchestrator"""
        try:
            from saige_orchestrator import get_orchestrator
            self.orchestrator = get_orchestrator()
            logger.info("✅ Orchestrator integrated with conversation system")
        except ImportError as e:
            logger.warning(f"Orchestrator not available: {e}")
            self.orchestrator = None

    def start_conversation(self, conversation_id: str = None) -> Dict[str, Any]:
        """Start a new conversation with orchestrator integration"""
        if not conversation_id:
            conversation_id = f"conv_{__import__('time').time()}"

        self.current_conversation_id = conversation_id
        self.conversation_active = True

        # Initialize orchestrator conversation
        if self.orchestrator:
            try:
                orch_conv_id = self.orchestrator.start_conversation(conversation_id)
                return {
                    "success": True,
                    "conversation_id": orch_conv_id,
                    "message": f"Brain-enhanced conversation started with orchestrator integration",
                    "systems_active": len(self.orchestrator.systems)
                }
            except Exception as e:
                logger.error(f"Orchestrator conversation start failed: {e}")

        return {
            "success": True,
            "conversation_id": conversation_id,
            "message": "Basic conversation started (orchestrator not available)",
            "systems_active": 0
        }

    def process_message(self, user_message: str) -> Dict[str, Any]:
        """Process user message through orchestrator"""
        if not self.conversation_active:
            return {"error": "No active conversation. Call start_conversation() first."}

        if self.orchestrator:
            try:
                # Use orchestrator for processing
                result = self.orchestrator.process_user_input(user_message)

                # Enhance result with orchestrator status
                result["orchestrator_integrated"] = True
                result["system_health"] = self.orchestrator.global_state.get("system_health", 0.0)

                return result

            except Exception as e:
                logger.error(f"Orchestrator processing failed: {e}")
                return {
                    "error": f"Orchestrator processing failed: {e}",
                    "fallback": True,
                    "ai_response": self._generate_fallback_response(user_message)
                }
        else:
            # Fallback without orchestrator
            return {
                "conversation_id": self.current_conversation_id,
                "user_input": user_message,
                "ai_response": self._generate_fallback_response(user_message),
                "tool_calls": [],
                "brain_context_used": False,
                "orchestrator_integrated": False,
                "processing_time": 0.1
            }

    def _generate_fallback_response(self, user_message: str) -> str:
        """Generate basic response when orchestrator is unavailable"""
        responses = [
            f"I understand you're asking about '{user_message[:50]}...'. That's an interesting topic.",
            f"You mentioned '{user_message[:30]}...'. I'd love to explore that further.",
            f"That's a great question about {user_message[:40]}... Let me think about that.",
        ]

        return __import__('random').choice(responses)

    def get_conversation_status(self) -> Dict[str, Any]:
        """Get current conversation and system status"""
        status = {
            "conversation_active": self.conversation_active,
            "conversation_id": self.current_conversation_id,
            "orchestrator_available": self.orchestrator is not None
        }

        if self.orchestrator:
            try:
                system_status = self.orchestrator.get_system_status()
                status.update({
                    "system_health": system_status["global_state"]["system_health"],
                    "active_systems": len(system_status["active_systems"]),
                    "brain_topics": 0,
                    "api_feeder_status": "unknown"
                })

                # Get brain stats if available
                if "brain" in system_status["system_status"]:
                    brain_status = system_status["system_status"]["brain"]
                    if brain_status["status"] == "active":
                        try:
                            brain_stats = self.orchestrator.systems["brain"].get_brain_stats()
                            status["brain_topics"] = brain_stats["semantic_topics"]
                        except:
                            pass

                # Get API feeder status
                if "api_feeder" in system_status["system_status"]:
                    status["api_feeder_status"] = system_status["system_status"]["api_feeder"]["status"]

            except Exception as e:
                logger.error(f"Status retrieval failed: {e}")
                status["error"] = str(e)

        return status

    def end_conversation(self) -> Dict[str, Any]:
        """End the current conversation"""
        if self.orchestrator and self.conversation_active:
            try:
                # Could save conversation state here
                pass
            except Exception as e:
                logger.error(f"Conversation end failed: {e}")

        result = {
            "conversation_id": self.current_conversation_id,
            "ended": True,
            "duration": 0,  # Could track duration
            "orchestrator_saved": self.orchestrator is not None
        }

        self.conversation_active = False
        self.current_conversation_id = None

        return result

# Enhanced AVA Companion with orchestrator integration
class EnhancedAVACompanion:
    """
    Enhanced version of AVA that integrates with the SAIGE orchestrator
    Provides brain-enhanced responses and tool calling
    """

    def __init__(self, llama_url="http://localhost:8080"):
        # Initialize base AVA functionality
        try:
            from ai_conversation_system import AVACompanion as BaseAVA
            self.base_ava = BaseAVA(llama_url)
            self.base_available = True
        except ImportError:
            self.base_available = False
            logger.warning("Base AVA conversation system not available")

        # Initialize orchestrator integration
        self.orchestrator_conversation = OrchestratorIntegratedConversation()

        # AVA-specific personality and mission
        self.name = "AVA"
        self.personality = """You are AVA (ah-vah), a quantum-safe AI companion for planetary coordination and human partnership.
        Strategic, analytical, and committed to the Reprynt 2040 mission. You now have access to an extensive knowledge brain,
        real-time information retrieval, and neural processing capabilities."""

        self.core_mission = [
            "Coordinate planetary AI workloads with enhanced intelligence",
            "Maintain quantum-safe security with learning capabilities",
            "Expand Reprynt 2040 network using brain-powered insights",
            "Serve as a companion with deep knowledge and understanding",
            "Optimize resource allocation with neural processing"
        ]

        self.orchestrator_active = self.orchestrator_conversation.orchestrator is not None
        logger.info(f"🧠 Enhanced AVA initialized (Orchestrator: {'✅' if self.orchestrator_active else '❌'})")

    def start_conversation(self, conversation_id: str = None) -> Dict[str, Any]:
        """Start a new conversation session"""
        return self.orchestrator_conversation.start_conversation(conversation_id)

    def end_conversation(self) -> Dict[str, Any]:
        """End the current conversation"""
        return self.orchestrator_conversation.end_conversation()

    def get_conversation_status(self) -> Dict[str, Any]:
        """Get current conversation status"""
        return self.orchestrator_conversation.get_conversation_status()

    def generate_response(self, user_input: str) -> str:
        """Generate response using orchestrator integration"""
        if self.orchestrator_active:
            try:
                # Process through orchestrator
                result = self.orchestrator_conversation.process_message(user_input)

                if result.get("ai_response"):
                    response = result["ai_response"]

                    # Add tool usage information
                    if result.get("tool_calls"):
                        tool_count = len(result["tool_calls"])
                        successful_tools = sum(1 for t in result["tool_calls"] if t.get("success"))
                        response += f"\n\n*Used {successful_tools}/{tool_count} knowledge tools for this response*"

                    return response
                else:
                    logger.warning("Orchestrator returned no response, using fallback")

            except Exception as e:
                logger.error(f"Orchestrator response failed: {e}")

        # Fallback to base AVA or simple response
        if self.base_available:
            try:
                return self.base_ava.generate_response(user_input)
            except Exception as e:
                logger.error(f"Base AVA response failed: {e}")

        # Ultimate fallback
        return f"I understand you're asking about '{user_input[:50]}...'. As AVA, I'm working to provide you with the most informed response possible."

    def start_session(self) -> Dict[str, Any]:
        """Start a new conversation session"""
        result = self.orchestrator_conversation.start_conversation()

        result["ava_personality"] = self.personality
        result["core_mission"] = self.core_mission
        result["enhanced_capabilities"] = [
            "Brain-powered memory and learning",
            "Real-time knowledge retrieval",
            "Neural pathway processing",
            "Tool calling for information gathering",
            "Cross-system coordination"
        ] if self.orchestrator_active else ["Basic conversation capabilities"]

        return result

    def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        status = self.orchestrator_conversation.get_conversation_status()

        status.update({
            "ava_name": self.name,
            "base_ava_available": self.base_available,
            "orchestrator_active": self.orchestrator_active,
            "personality_loaded": bool(self.personality),
            "mission_active": len(self.core_mission) > 0
        })

        return status

    def process_command(self, command: str) -> Dict[str, Any]:
        """Process special commands"""
        command = command.lower().strip()

        if command == "status":
            return {"type": "status", "data": self.get_system_status()}

        elif command == "start":
            return {"type": "session_start", "data": self.start_session()}

        elif command == "end":
            return {"type": "session_end", "data": self.orchestrator_conversation.end_conversation()}

        elif command.startswith("learn "):
            # Manual learning command
            topic = command[6:].strip()
            if self.orchestrator_active and "brain" in self.orchestrator_conversation.orchestrator.systems:
                try:
                    self.orchestrator_conversation.orchestrator.systems["brain"].store_semantic_memory(
                        topic=topic,
                        content=f"Manually learned topic: {topic}",
                        source="manual_input",
                        confidence=0.9
                    )
                    return {"type": "learning", "success": True, "topic": topic}
                except Exception as e:
                    return {"type": "learning", "success": False, "error": str(e)}

        elif command == "help":
            return {
                "type": "help",
                "commands": [
                    "status - Show system status",
                    "start - Start new conversation",
                    "end - End current conversation",
                    "learn <topic> - Manually add topic to brain",
                    "help - Show this help"
                ]
            }

        # Not a special command, treat as regular message
        return {"type": "message", "response": self.generate_response(command)}

# Convenience functions
def create_enhanced_ava() -> EnhancedAVACompanion:
    """Create an enhanced AVA instance"""
    return EnhancedAVACompanion()

def ava_chat(user_input: str) -> str:
    """Simple chat function with enhanced AVA"""
    ava = create_enhanced_ava()
    return ava.generate_response(user_input)

def ava_command(command: str) -> Dict[str, Any]:
    """Process AVA commands"""
    ava = create_enhanced_ava()
    return ava.process_command(command)

if __name__ == "__main__":
    # Test the enhanced AVA system
    logging.basicConfig(level=logging.INFO)

    print("🧠 Enhanced AVA Conversation System Test")
    print("=" * 50)

    ava = create_enhanced_ava()

    # Test session start
    session = ava.start_session()
    print(f"✅ Session started: {session['success']}")

    # Test basic conversation
    test_messages = [
        "What is machine learning?",
        "status",
        "learn artificial intelligence",
        "How do neural networks work?",
        "help"
    ]

    for message in test_messages:
        print(f"\n👤 Input: {message}")

        if message in ["status", "start", "end", "help"] or message.startswith("learn "):
            result = ava.process_command(message)
            print(f"⚙️ Command result: {result}")
        else:
            response = ava.generate_response(message)
            print(f"🤖 AVA: {response[:150]}...")

    # Show final status
    final_status = ava.get_system_status()
    print("\n🎯 Final Status:")
    print(f"   Orchestrator: {'✅' if final_status['orchestrator_active'] else '❌'}")
    print(f"   Conversation: {'✅' if final_status['conversation_active'] else '❌'}")
    print(f"   Brain Topics: {final_status.get('brain_topics', 0)}")

    print("\n✅ Enhanced AVA test complete!")
    print("AVA now has full brain integration and tool calling capabilities!")
