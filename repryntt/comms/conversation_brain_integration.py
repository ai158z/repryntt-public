#!/usr/bin/env python3
"""
Brain-Enhanced Conversation System Integration
Adds brain capabilities to the existing conversation system for AI learning and tool calling
"""

import sys
import os
import json
import re
import random
import time
import logging
from typing import Dict, List, Any, Optional

from repryntt.brain import BrainSystemProtocol as BrainSystem
from repryntt.tools.tool_interface import AIToolInterface, process_ai_tool_request

logger = logging.getLogger(__name__)

class BrainEnhancedConversation:
    """
    Wrapper around the existing conversation system that adds brain capabilities
    Enables AI learning, memory, and tool calling during conversations
    """

    def __init__(self, brain_path: str = "brain"):
        # Initialize brain systems
        self.brain = BrainSystem(brain_path)
        self.tool_interface = AIToolInterface(brain_path)

        # Import and initialize the original conversation system
        try:
            from ai_conversation_system import AVACompanion
            self.conversation_system = AVACompanion()
            self.original_available = True
            logger.info("✅ Original conversation system loaded")
        except ImportError as e:
            logger.error(f"❌ Could not load original conversation system: {e}")
            self.conversation_system = None
            self.original_available = False

        # Track current conversation
        self.current_conversation_id = None
        self.conversation_history = []

    def start_conversation(self, conversation_id: str = None) -> str:
        """Start a new brain-enhanced conversation"""
        if conversation_id is None:
            conversation_id = f"conv_{int(time.time())}"

        self.current_conversation_id = conversation_id
        self.tool_interface.initialize_conversation(conversation_id)
        self.conversation_history = []

        # Initialize with some base context
        initial_topic = "General AI conversation and learning"
        self.brain.initialize_working_memory(conversation_id, initial_topic)

        logger.info(f"🧠 Started brain-enhanced conversation: {conversation_id}")

        return f"Brain-enhanced conversation started. ID: {conversation_id}"

    def process_user_input(self, user_input: str) -> Dict[str, Any]:
        """
        Process user input with brain enhancement
        Returns enhanced response with tool usage and learning
        """
        if not self.current_conversation_id:
            return {"error": "No active conversation. Call start_conversation() first."}

        start_time = time.time()
        response_data = {
            "conversation_id": self.current_conversation_id,
            "user_input": user_input,
            "ai_response": "",
            "tool_calls": [],
            "brain_context_used": False,
            "learning_stored": False,
            "processing_time": 0
        }

        try:
            # Step 1: Get brain context for the AI
            brain_context = self.tool_interface.get_context_for_response(user_input)
            response_data["brain_context_used"] = len(brain_context) > 0

            # Step 2: Generate AI response using original system with brain context
            if self.original_available and self.conversation_system:
                # Enhance the personality prompt with brain context
                enhanced_personality = self.conversation_system.personality
                if brain_context:
                    enhanced_personality += f"\n\nRelevant knowledge and context:\n{brain_context}"

                # Temporarily modify the conversation system's personality
                original_personality = self.conversation_system.personality
                self.conversation_system.personality = enhanced_personality

                try:
                    # Generate response using enhanced context
                    ai_response = self.conversation_system.generate_response(user_input)
                    response_data["ai_response"] = ai_response

                    # Analyze if AI wants to use tools
                    tool_analysis = self.tool_interface.detect_tool_needs(user_input, ai_response)

                    if tool_analysis['needs_tools'] and tool_analysis['confidence'] > 0.6:
                        # Execute tools and enhance response
                        tool_calls = []
                        for tool_name in tool_analysis['recommended_tools']:
                            try:
                                parameters = self._extract_tool_parameters(tool_name, user_input, ai_response)
                                tool_result = self.tool_interface.call_tool(tool_name, parameters)

                                tool_calls.append({
                                    "tool_name": tool_name,
                                    "parameters": parameters,
                                    "result": tool_result,
                                    "success": tool_result['result']['success'] if 'result' in tool_result else False
                                })

                                # If tool succeeded, add results to context for follow-up response
                                if tool_result['result'].get('success'):
                                    additional_context = f"\nTool {tool_name} results: {str(tool_result['result'])[:500]}..."
                                    brain_context += additional_context

                            except Exception as e:
                                logger.error(f"Tool execution failed: {e}")
                                tool_calls.append({
                                    "tool_name": tool_name,
                                    "error": str(e),
                                    "success": False
                                })

                        response_data["tool_calls"] = tool_calls

                        # Generate enhanced response with tool results
                        if any(call.get('success', False) for call in tool_calls):
                            enhanced_prompt = f"{enhanced_personality}\n\nNew information from tools:\n"
                            for call in tool_calls:
                                if call.get('success'):
                                    enhanced_prompt += f"- {call['tool_name']}: {str(call['result'])[:300]}...\n"

                            self.conversation_system.personality = enhanced_prompt
                            enhanced_response = self.conversation_system.generate_response(user_input)
                            response_data["ai_response"] = enhanced_response

                finally:
                    # Restore original personality
                    self.conversation_system.personality = original_personality

            else:
                # Fallback: Generate basic response with brain context
                response_data["ai_response"] = self._generate_fallback_response(user_input, brain_context)

            # Step 3: Store learning from this interaction
            self._store_interaction_learning(user_input, response_data["ai_response"], response_data["tool_calls"])

            response_data["learning_stored"] = True

            # Step 4: Update conversation history
            self.conversation_history.append({
                "timestamp": time.time(),
                "user_input": user_input,
                "ai_response": response_data["ai_response"],
                "tool_calls": len(response_data["tool_calls"]),
                "brain_context_used": response_data["brain_context_used"]
            })

        except Exception as e:
            logger.error(f"Error processing user input: {e}")
            response_data["error"] = str(e)
            response_data["ai_response"] = "I apologize, but I encountered an error processing your request."

        finally:
            response_data["processing_time"] = time.time() - start_time

        return response_data

    def _extract_tool_parameters(self, tool_name: str, user_input: str, ai_response: str) -> Dict[str, Any]:
        """Extract appropriate parameters for tool calls"""
        parameters = {}

        if tool_name in ['search_knowledge', 'fetch_web_info']:
            # Extract search query from user input
            parameters['query'] = user_input

        elif tool_name == 'extract_content':
            # Try to extract URL from user input or AI response
            url_pattern = r'https?://[^\s]+'
            urls = re.findall(url_pattern, user_input + " " + ai_response)
            if urls:
                parameters['url'] = urls[0]
            else:
                # Fallback: create Wikipedia URL from topic
                topic = user_input.lower().replace('what is', '').replace('tell me about', '').replace('explain', '').strip()
                parameters['url'] = f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')}"

        elif tool_name == 'analyze_topic':
            parameters['topic'] = user_input

        elif tool_name == 'find_similar_topics':
            parameters['topic'] = user_input

        return parameters

    def _generate_fallback_response(self, user_input: str, brain_context: str) -> str:
        """Generate a basic response when original conversation system is unavailable"""
        responses = [
            f"I understand you're asking about: {user_input[:100]}...",
            f"That's an interesting question about {user_input[:50]}...",
            f"I'd be happy to help you learn about {user_input[:50]}..."
        ]

        base_response = random.choice(responses)

        if brain_context:
            base_response += f" I have some relevant knowledge that might help: {brain_context[:300]}..."

        return base_response

    def _store_interaction_learning(self, user_input: str, ai_response: str, tool_calls: List[Dict]):
        """Store learning from the conversation interaction"""
        if not self.current_conversation_id:
            return

        # Convert tool calls to ToolCall objects
        tool_call_objects = []
        for call in tool_calls:
            if call.get('success'):
                tool_call_objects.append(type('ToolCall', (), {
                    'tool_name': call['tool_name'],
                    'parameters': call.get('parameters', {}),
                    'timestamp': time.time(),
                    'result': call.get('result'),
                    'success': True,
                    'execution_time': call.get('execution_time', 0),
                    'error_message': None
                })())

        # Assess response quality (basic heuristic)
        quality_score = 0.7  # Default good quality
        if len(ai_response.split()) < 10:
            quality_score = 0.4  # Too short
        elif len(tool_calls) > 0 and any(call.get('success') for call in tool_calls):
            quality_score = 0.9  # Used tools successfully

        # Store in brain
        self.tool_interface.store_conversation_memory(
            user_input=user_input,
            ai_response=ai_response,
            tool_calls=tool_call_objects,
            outcome_quality=quality_score
        )

        # Extract and store any new knowledge from the response
        if len(ai_response.split()) > 20:  # Substantial response
            # Simple topic extraction
            potential_topic = user_input.lower().replace('what is', '').replace('tell me about', '').strip()
            if len(potential_topic) > 3:
                self.brain.store_semantic_memory(
                    topic=potential_topic.title(),
                    content=ai_response,
                    source="conversation_learning",
                    confidence=0.6
                )

    def get_conversation_summary(self) -> Dict[str, Any]:
        """Get summary of current conversation"""
        if not self.current_conversation_id:
            return {"error": "No active conversation"}

        return {
            "conversation_id": self.current_conversation_id,
            "turns": len(self.conversation_history),
            "total_tool_calls": sum(turn.get('tool_calls', 0) for turn in self.conversation_history),
            "brain_usage": sum(1 for turn in self.conversation_history if turn.get('brain_context_used')),
            "last_activity": time.time(),
            "brain_stats": self.brain.get_brain_stats()
        }

    def save_conversation(self, filename: str = None):
        """Save the current conversation to file"""
        if not self.current_conversation_id or not self.conversation_history:
            return False

        if not filename:
            filename = f"conversation_{self.current_conversation_id}_{int(time.time())}.json"

        conversation_data = {
            "conversation_id": self.current_conversation_id,
            "start_time": self.conversation_history[0]['timestamp'] if self.conversation_history else time.time(),
            "end_time": time.time(),
            "turns": self.conversation_history,
            "brain_stats": self.brain.get_brain_stats(),
            "summary": self.get_conversation_summary()
        }

        try:
            with open(filename, 'w') as f:
                json.dump(conversation_data, f, indent=2, default=str)
            logger.info(f"💾 Conversation saved to {filename}")
            return True
        except Exception as e:
            logger.error(f"Failed to save conversation: {e}")
            return False

    def load_conversation(self, conversation_id: str) -> bool:
        """Load a previously saved conversation"""
        filename = f"conversation_{conversation_id}_*.json"
        # This would need glob implementation for full functionality
        # For now, return False
        return False

# Convenience functions for easy integration
def create_brain_conversation(conversation_id: str = None, brain_path: str = "brain"):
    """Create a new brain-enhanced conversation"""
    conversation = BrainEnhancedConversation(brain_path)
    result = conversation.start_conversation(conversation_id)
    return conversation, result

def chat_with_brain(conversation: BrainEnhancedConversation, user_input: str) -> str:
    """Simple chat function that returns just the AI response"""
    result = conversation.process_user_input(user_input)
    return result.get('ai_response', 'Error: No response generated')

# Example usage and testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("🧠 Brain-Enhanced Conversation System Test")
    print("=" * 50)

    # Create brain-enhanced conversation
    conversation, start_msg = create_brain_conversation("test_brain_conversation")
    print(f"✅ {start_msg}")

    # Test basic interaction
    test_inputs = [
        "What is machine learning?",
        "Tell me about quantum computing",
        "How does photosynthesis work?"
    ]

    for user_input in test_inputs:
        print(f"\n👤 User: {user_input}")

        result = conversation.process_user_input(user_input)

        print(f"🤖 AI: {result['ai_response'][:200]}...")
        print(f"🛠️ Tools used: {len(result['tool_calls'])}")
        print(f"🧠 Brain context: {result['brain_context_used']}")
        print(f"💾 Learning stored: {result['learning_stored']}")
        print(f"⏱️ Processing time: {result['processing_time']:.2f} seconds")
    # Save conversation
    conversation.save_conversation()

    # Show brain stats
    stats = conversation.get_conversation_summary()
    print(f"\n📊 Conversation Summary: {stats}")

    print("\n✅ Brain-enhanced conversation test completed!")
