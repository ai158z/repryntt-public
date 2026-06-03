#!/usr/bin/env python3
"""
SAIGE Brain Tool Interface - AI Tool Calling System
Provides the interface for AI models to call tools and access brain functions
"""

import json
import time
import logging
import os
import threading
import re
from typing import Dict, List, Any, Optional
from .brain_system import BrainSystem, execute_tool_call, ToolCall

logger = logging.getLogger(__name__)

def store_tool_result(tool_name: str, parameters: Dict[str, Any], result: Any,
                     conversation_id: str = "default", execution_time: float = 0.0) -> str:
    """
    Store raw tool execution results to JSON file and return file path
    Returns the file path where results were stored
    """
    timestamp = int(time.time())
    filename = f"{tool_name}_{conversation_id}_{timestamp}.json"
    filepath = os.path.join("brain", "tool_results", filename)

    # Ensure directory exists
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Convert result to JSON-serializable format
    serializable_result = _make_json_serializable(result)

    tool_data = {
        "timestamp": timestamp,
        "tool_name": tool_name,
        "parameters": parameters,
        "result": serializable_result,
        "execution_time": execution_time,
        "conversation_id": conversation_id
    }

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(tool_data, f, indent=2, ensure_ascii=False)
        logger.debug(f"Stored tool result to {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to store tool result: {e}")
        return ""

def _make_json_serializable(obj: Any) -> Any:
    """
    Convert objects to JSON-serializable format
    Handles SemanticMemory objects and other non-serializable types
    """
    if obj is None:
        return None
    elif isinstance(obj, (str, int, float, bool)):
        return obj
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: _make_json_serializable(value) for key, value in obj.items()}
    else:
        # Try to convert to dict if it has __dict__ attribute (like SemanticMemory)
        try:
            if hasattr(obj, '__dict__'):
                return _make_json_serializable(obj.__dict__)
            elif hasattr(obj, '__str__'):
                return str(obj)
            else:
                return str(obj)
        except Exception:
            return str(obj)

def extract_tool_insights(tool_name: str, parameters: Dict[str, Any], result: Any) -> str:
    """
    Extract key insights from tool results for efficient AI consumption
    Returns a concise summary suitable for AI prompts
    """
    try:
        if not result:
            return f"Tool '{tool_name}' returned no results."

        insights = []

        if tool_name in ["grokipedia_search", "grokedia_search"]:
            # Check for redundancy warning
            if isinstance(result, dict) and result.get('warning') == 'redundant_search':
                return f"⚠️ REDUNDANT SEARCH PREVENTED: {result.get('message', 'Similar search performed recently')}\n💡 {result.get('insights', 'Consider using brain_network_search instead')}"

            # Grokipedia already returns formatted insights
            if isinstance(result, dict) and 'insights' in result:
                return result['insights']
            else:
                return f"Grokipedia search completed. Results: {str(result)[:200]}..."
        elif tool_name in ["google_web_search", "google_search", "web_search"]:
            # Google search already returns formatted insights
            if isinstance(result, dict) and 'insights' in result:
                return result['insights']
            else:
                return f"Google search completed. Results: {str(result)[:300]}..."
        elif tool_name in ["web_search_results_only", "search_results_only"]:
            # Extract actual search results for AI consumption
            if isinstance(result, dict):
                if 'insights' in result:
                    return result['insights']
                items = result.get('results', [])
                if items:
                    parts = [f"🔍 Search: '{parameters.get('query', '')}' — {len(items)} results\n"]
                    for i, r in enumerate(items[:8], 1):
                        title = r.get('title', 'Untitled')
                        url = r.get('url', '')
                        snippet = r.get('snippet', r.get('content', ''))[:300]
                        source = r.get('source', '')
                        parts.append(f"[{i}] {title}")
                        if source:
                            parts.append(f"    Source: {source} | {url}")
                        elif url:
                            parts.append(f"    URL: {url}")
                        if snippet:
                            parts.append(f"    {snippet}")
                        parts.append("")
                    return "\n".join(parts)
                elif result.get('success') is False:
                    return f"Search failed: {result.get('error', 'unknown')}"
            return f"Search completed. Results: {str(result)[:500]}..."
        elif tool_name == "brain_network_search":
            insights = _extract_brain_search_insights(result)
        elif tool_name == "recall_memory":
            insights = _extract_memory_insights(result)
        elif tool_name == "fetch_web_info":
            insights = _extract_web_info_insights(result)
        elif tool_name == "search_semantic_memory":
            insights = _extract_semantic_memory_insights(result)
        else:
            # Generic extraction for other tools
            insights = _extract_generic_insights(result)

        if insights:
            return "\n".join([f"• {insight}" for insight in insights[:5]])  # Limit to top 5 insights
        else:
            return f"Tool '{tool_name}' completed successfully but no specific insights extracted."

    except Exception as e:
        logger.error(f"Error extracting insights from {tool_name}: {e}")
        return f"Tool '{tool_name}' completed but insight extraction failed."

def _extract_brain_search_insights(result: Dict[str, Any]) -> List[str]:
    """Extract insights from brain network search results"""
    insights = []

    for memory_type in ['semantic', 'episodic', 'procedural']:
        if memory_type in result and result[memory_type]:
            count = len(result[memory_type])
            if count > 0:
                insights.append(f"Found {count} relevant {memory_type} memories")

                # Extract key topics/themes
                topics = []
                for item in result[memory_type][:3]:  # Look at first 3 items
                    if 'topic' in item:
                        topics.append(item['topic'][:50] + "..." if len(item.get('topic', '')) > 50 else item['topic'])
                    elif 'content' in item:
                        # Extract first meaningful sentence
                        content = item['content'].strip()
                        if content:
                            first_sentence = content.split('.')[0][:100]
                            topics.append(first_sentence + "..." if len(first_sentence) == 100 else first_sentence)

                if topics:
                    insights.append(f"Key topics: {', '.join(topics)}")

    return insights

def _extract_memory_insights(result: Dict[str, Any]) -> List[str]:
    """Extract insights from memory recall results"""
    insights = []

    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(value, list) and value:
                insights.append(f"Retrieved {len(value)} {key} memories")
            elif isinstance(value, str) and len(value) > 10:
                insights.append(f"Memory context: {value[:100]}...")
    elif isinstance(result, list):
        insights.append(f"Retrieved {len(result)} memory items")
        if result:
            sample = str(result[0])[:100]
            insights.append(f"Sample content: {sample}...")

    return insights

def _extract_web_info_insights(result: Dict[str, Any]) -> List[str]:
    """Extract insights from web information fetch results"""
    insights = []

    if 'results' in result and isinstance(result['results'], list):
        count = len(result['results'])
        insights.append(f"Found {count} knowledge entries")

        # Extract key titles and content snippets
        titles = []
        for item in result['results'][:3]:  # Look at first 3 results
            if 'title' in item and item['title']:
                title = item['title'][:80] + "..." if len(item['title']) > 80 else item['title']
                titles.append(title)

        if titles:
            insights.append(f"Key topics: {', '.join(titles)}")

    elif 'summary' in result:
        insights.append(f"Web summary: {result['summary'][:200]}...")
    elif 'content' in result and result['content']:
        content = result['content'][:300]
        insights.append(f"Web content: {content}...")

    if 'source' in result:
        insights.append(f"Source: {result['source']}")

    return insights

def _extract_semantic_memory_insights(result: Dict[str, Any]) -> List[str]:
    """Extract insights from semantic memory search results"""
    insights = []

    if isinstance(result, list):
        # Direct list of SemanticMemory objects
        count = len(result)
        insights.append(f"Found {count} semantically similar memories")

        # Extract topics from the first few results
        topics = []
        for item in result[:3]:
            if isinstance(item, dict) and 'topic' in item:
                topic = item['topic'][:60] + "..." if len(item['topic']) > 60 else item['topic']
                topics.append(topic)
            elif hasattr(item, 'topic'):
                topic = item.topic[:60] + "..." if len(item.topic) > 60 else item.topic
                topics.append(topic)

        if topics:
            insights.append(f"Key topics: {', '.join(topics)}")

    elif 'results' in result and result['results']:
        count = len(result['results'])
        insights.append(f"Found {count} semantically similar memories")

        # Extract confidence scores and topics
        high_confidence = [r for r in result['results'] if r.get('confidence', 0) > 0.8]
        if high_confidence:
            insights.append(f"{len(high_confidence)} high-confidence matches found")

    return insights

def _extract_generic_insights(result: Any) -> List[str]:
    """Generic insight extraction for unknown tool types"""
    insights = []

    if isinstance(result, dict):
        insights.append(f"Retrieved data with {len(result)} fields")
        # Look for common fields
        if 'count' in result:
            insights.append(f"Count: {result['count']}")
        if 'status' in result:
            insights.append(f"Status: {result['status']}")
    elif isinstance(result, list):
        insights.append(f"Retrieved {len(result)} items")
    elif isinstance(result, str):
        word_count = len(result.split())
        insights.append(f"Retrieved {word_count} words of information")

    return insights

class AIToolInterface:
    """
    Interface for AI models to interact with the brain system and external tools
    Provides structured tool calling and response formatting

    SINGLETON PATTERN: Use shared BrainSystem instance to prevent repeated loading
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, brain_path: str = "brain", brain_instance: BrainSystem = None):
        """Singleton pattern implementation"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(AIToolInterface, cls).__new__(cls)
        return cls._instance

    def __init__(self, brain_path: str = "brain", brain_instance: BrainSystem = None):
        # Prevent re-initialization in singleton pattern
        if hasattr(self, '_initialized'):
            return

        self.brain = brain_instance if brain_instance is not None else BrainSystem(brain_path)  # Use provided instance or create new one
        self.active_tools: List[str] = []
        self.conversation_id: Optional[str] = None
        self._initialized = True

    def initialize_conversation(self, conversation_id: str, initial_topic: str = ""):
        """Initialize a new conversation session"""
        self.conversation_id = conversation_id
        self.brain.initialize_working_memory(conversation_id, initial_topic)
        self.active_tools = []
        logger.info(f"Initialized conversation: {conversation_id}")

    def detect_tool_needs(self, user_input: str, ai_reasoning: str) -> Dict[str, Any]:
        """
        Analyze user input and AI reasoning to detect if tools are needed
        Returns tool recommendations and confidence scores
        """
        needs_tools = False
        recommended_tools = []
        confidence = 0.0

        # Analyze user input for knowledge-seeking patterns
        user_lower = user_input.lower()
        knowledge_indicators = [
            'what is', 'how does', 'explain', 'tell me about', 'define',
            'who was', 'when did', 'where is', 'why does', 'search for',
            'find out', 'look up', 'research', 'learn about'
        ]

        if any(indicator in user_lower for indicator in knowledge_indicators):
            needs_tools = True
            confidence += 0.6

        # Analyze AI reasoning for uncertainty or tool mentions
        reasoning_lower = ai_reasoning.lower()
        uncertainty_indicators = [
            'i need to check', 'let me look up', 'i should search',
            'i don\'t know', 'i\'m not sure', 'let me verify',
            'i need more information', 'i should check'
        ]

        tool_mentions = [
            'search_knowledge', 'fetch_web_info', 'extract_content',
            'analyze_topic', 'find_similar'
        ]

        if any(indicator in reasoning_lower for indicator in uncertainty_indicators):
            needs_tools = True
            confidence += 0.4

        if any(tool in reasoning_lower for tool in tool_mentions):
            needs_tools = True
            confidence = 0.9

        # TASK-AWARE TOOL SELECTION: Use task-specific tool priorities
        if needs_tools:
            # Classify task type to get preferred tools
            task_config = self.brain.task_hierarchy.classify_task(user_input + " " + ai_reasoning)
            topic_analysis = self.brain.analyze_topic_complexity(user_input)

            # Start with task-specific preferred tools
            task_preferred_tools = task_config.preferred_tools.copy()

            # Add additional tools based on content analysis
            if topic_analysis['needs_research']:
                if 'search_knowledge' not in task_preferred_tools:
                    task_preferred_tools.append('search_knowledge')
                if 'fetch_web_info' not in task_preferred_tools:
                    task_preferred_tools.append('fetch_web_info')
                if 'web' in user_lower or 'online' in user_lower:
                    if 'extract_content' not in task_preferred_tools:
                        task_preferred_tools.append('extract_content')

            # Use task-preferred tools as base recommendations
            recommended_tools.extend(task_preferred_tools)

            # Add analysis tools if complex topic
            if len(user_input.split()) > 5:  # Complex query
                recommended_tools.extend(['analyze_topic', 'find_similar_topics'])

        return {
            'needs_tools': needs_tools,
            'recommended_tools': list(set(recommended_tools)),  # Remove duplicates
            'confidence': min(confidence, 1.0),
            'topic_analysis': self.brain.analyze_topic_complexity(user_input)
        }

    def call_tool(self, tool_name: str, parameters: Dict[str, Any], user_initiated: bool = False) -> Dict[str, Any]:
        """Execute a tool call and track the result

        Args:
            tool_name: Name of the tool to execute
            parameters: Parameters for the tool
            user_initiated: Whether this tool call was directly requested by a user (skips redundancy checks)
        """
        start_time = time.time()

        # Skip redundancy checks for user-initiated tool calls
        if not user_initiated and hasattr(self, 'conversation_id') and self.conversation_id:
            # Get chain data if this is part of a chain
            try:
                chain_file = f"brain/chains/chain_{self.conversation_id}.json"
                if os.path.exists(chain_file):
                    with open(chain_file, 'r') as f:
                        chain_data = json.load(f)

                    should_skip, reason = self.brain._should_skip_tool_call(chain_data, tool_name, parameters)
                    if should_skip:
                        logger.warning(f"🚫 {reason}")
                        return {
                            'success': False,
                            'error': f'Redundant tool call prevented: {reason}',
                            'result': f'TOOL CALL SKIPPED: {reason}. Consider using existing knowledge from previous searches.'
                        }
            except Exception as e:
                logger.error(f"Error checking tool redundancy: {e}")
                # Continue with tool call if check fails

        # Track active tool
        if tool_name not in self.active_tools:
            self.active_tools.append(tool_name)

        # Execute the tool
        result = execute_tool_call(tool_name, parameters, self.brain)

        execution_time = time.time() - start_time

        # Create tool call record
        tool_call = ToolCall(
            tool_name=tool_name,
            parameters=parameters,
            timestamp=start_time,
            result=result.get('result') if result['success'] else None,
            success=result['success'],
            execution_time=execution_time,
            error_message=result.get('error') if not result['success'] else None
        )

        # Update working memory with tool result
        if result['success']:
            self.brain.update_working_memory(
                active_tools=[tool_name],
                context_addition=f"Tool {tool_name} executed successfully: {str(result.get('result', ''))[:200]}..."
            )
        else:
            self.brain.update_working_memory(
                context_addition=f"Tool {tool_name} failed: {result.get('error', 'Unknown error')}"
            )

        # Update procedural memory with tool usage
        if result['success']:
            self.brain.update_procedural_memory(
                task_type=tool_name,
                steps=[f"Executed {tool_name} with parameters: {parameters}"],
                tools_used=[tool_name],
                success=True,
                execution_time=execution_time
            )

            # Store tool usage into semantic memory (vector brain) for future recall
            # This lets brain_network_search / recall_memory find past tool experiences
            try:
                result_snippet = str(result.get('result', ''))[:300]
                param_summary = ', '.join(f"{k}={v}" for k, v in parameters.items())[:150]
                memory_content = (
                    f"Successfully used tool '{tool_name}' with parameters: {param_summary}. "
                    f"Result: {result_snippet}"
                )
                self.brain.store_semantic_memory(
                    topic=f"tool_usage_{tool_name}_{int(time.time())}",
                    content=memory_content,
                    domain="tool_experience",
                    confidence=0.9,
                    source="tool_execution",
                    key_facts=[f"Tool '{tool_name}' can be used for: {param_summary}"],
                    related_topics=[tool_name, "tool_usage", "capabilities"]
                )
            except Exception as e:
                logger.debug(f"Could not store tool usage to semantic memory: {e}")

        return {
            'tool_call': tool_call,
            'result': result,
            'execution_time': execution_time
        }

    def get_context_for_response(self, user_input: str, max_words: int = 2000) -> str:
        """Get relevant context from brain for AI response generation"""
        if not self.conversation_id:
            return "No active conversation. Please initialize first."

        # Update working memory with user input
        self.brain.update_working_memory(
            context_addition=f"User: {user_input}"
        )

        # Get context from brain
        context = self.brain.get_context_for_question(user_input, max_words)

        # Add tool availability information
        tool_info = f"Available tools: {', '.join(self.brain.available_tools.keys())}"
        context += f"\n\n{tool_info}"

        return context

    def store_conversation_memory(self, user_input: str, ai_response: str,
                                tool_calls: List[ToolCall], outcome_quality: float = 0.8):
        """Store the completed conversation turn in episodic memory"""
        if not self.conversation_id:
            logger.warning("No active conversation to store")
            return

        self.brain.learn_from_interaction(
            user_input=user_input,
            ai_response=ai_response,
            tool_calls=tool_calls,
            conversation_id=self.conversation_id,
            outcome_quality=outcome_quality
        )

        # Clear active tools for next turn
        self.active_tools = []

    def get_brain_status(self) -> Dict[str, Any]:
        """Get current brain system status"""
        return {
            'conversation_id': self.conversation_id,
            'active_tools': self.active_tools,
            'brain_stats': self.brain.get_brain_stats(),
            'working_memory_topic': self.brain.working_memory.current_topic if self.brain.working_memory else None
        }

# Convenience functions for AI integration
def create_ai_interface(brain_path: str = "brain", brain_instance: BrainSystem = None) -> AIToolInterface:
    """Create and return an AI tool interface"""
    return AIToolInterface(brain_path, brain_instance)

def process_ai_tool_request(user_input: str, ai_reasoning: str,
                          tool_interface: AIToolInterface) -> Dict[str, Any]:
    """
    Process an AI's tool request and return formatted response
    This is the main entry point for AI models to request tool usage
    """
    # Detect if tools are needed
    tool_analysis = tool_interface.detect_tool_needs(user_input, ai_reasoning)

    response = {
        'tool_analysis': tool_analysis,
        'tool_calls': [],
        'context_provided': False,
        'error': None
    }

    if tool_analysis['needs_tools']:
        # Execute recommended tools
        for tool_name in tool_analysis['recommended_tools']:
            try:
                # Prepare tool parameters based on tool type
                parameters = {}

                if tool_name == 'search_knowledge':
                    parameters['query'] = user_input
                elif tool_name == 'fetch_web_info':
                    parameters['query'] = user_input
                elif tool_name == 'analyze_topic':
                    parameters['topic'] = user_input
                elif tool_name == 'find_similar_topics':
                    parameters['topic'] = user_input
                elif tool_name == 'extract_content':
                    # Would need URL extraction logic here
                    parameters['url'] = "https://en.wikipedia.org/wiki/" + user_input.replace(' ', '_')

                tool_result = tool_interface.call_tool(tool_name, parameters)
                response['tool_calls'].append(tool_result)

            except Exception as e:
                response['tool_calls'].append({
                    'tool_name': tool_name,
                    'success': False,
                    'error': str(e)
                })

        response['context_provided'] = True

    return response

def get_ai_response_context(user_input: str, tool_interface: AIToolInterface,
                          max_words: int = 2000) -> str:
    """Get formatted context for AI response generation"""
    return tool_interface.get_context_for_response(user_input, max_words)

# JSON-RPC style interface for structured tool calling
def handle_tool_call_request(request_json: str) -> str:
    """
    Handle JSON-RPC style tool call requests from AI models
    Format: {"method": "tool_name", "params": {...}, "id": 1}
    """
    try:
        request = json.loads(request_json)

        interface = create_ai_interface()

        # Initialize conversation if needed
        if 'conversation_id' in request.get('params', {}):
            interface.initialize_conversation(request['params']['conversation_id'])

        method = request.get('method')
        params = request.get('params', {})

        if method in interface.brain.available_tools:
            result = interface.call_tool(method, params)
            response = {
                'result': result,
                'id': request.get('id'),
                'success': result['result']['success'] if 'result' in result else False
            }
        else:
            response = {
                'error': f"Method '{method}' not found",
                'id': request.get('id'),
                'success': False
            }

        return json.dumps(response)

    except Exception as e:
        return json.dumps({
            'error': str(e),
            'success': False
        })

def parse_natural_language_tool_request(user_request: str, context_history: List[Dict] = None) -> Dict[str, Any]:
    """
    Parse natural language tool requests into structured tool calls
    Handles AI conversational mentions of tools in autonomous chains
    Enhanced to support multi-step workflows and contextual awareness
    """
    import re

    # Enhanced tool name patterns with natural language recognition
    tool_patterns = {
        'brain_network_search': [
            r"'brain_network_search'", 'brain_network_search', 'recall_memory',
            'search your brain', 'search brain', 'search my memory', 'recall from memory',
            'let me search my brain', 'i should search my brain', 'let me recall',
            'i need to recall', 'check my previous work', 'look in my brain',
            'search across my knowledge', 'brain search', 'memory search',
            'search my brain', 'recall what i know', 'check what i know',
            'need to recall', 'recall what', 'what i know'
        ],
        'google_web_search': [
            r"'google_web_search'", r"'google_search'", r"'web_search'",
            'google_web_search', 'google_search', 'google it', 'search google',
            'google this', 'let me google', 'i should google', 'look this up on google',
            'search on google', 'google for', 'web search', 'search the web',
            'search online', 'look online', 'find on the internet', 'search internet',
            'real-time information', 'current weather', 'latest news', 'recent events'
        ],
        'web_search_results_only': [
            r"'web_search_results_only'", r"'search_results_only'",
            'web_search_results_only', 'search_results_only', 'get search results',
            'find search results', 'what are the search results', 'search for results',
            'show me search results', 'look up results', 'find urls for'
        ],
        'scrape_web_page': [
            r"'scrape_web_page'", r"'scrape_url'",
            'scrape_web_page', 'scrape_url', 'scrape this page', 'scrape that url',
            'get full content from', 'scrape the website', 'extract content from',
            'get page content', 'read this page', 'fetch page content'
        ],
        'grokipedia_search': [
            r"'grokipedia_search'", r"'grokedia_search'", 'grokipedia_search',
            'grokedia_search', 'search grokipedia', 'look this up',
            'let me research this', 'i should look this up', 'external research',
            'search externally', 'check external sources', 'academic research',
            'research this topic', 'find more information', 'look up'
        ],
        'search_knowledge': [
            r"'search_knowledge'", 'search_knowledge', 'analyze topic',
            'analyze this', 'let me analyze', 'i should analyze', 'knowledge search',
            'search knowledge base', 'semantic search', 'find related concepts'
        ],
        'compute_zeta_function': [
            'zeta function', 'compute zeta', 'analyze zeta', 'zeta analysis',
            'mathematical analysis', 'complex analysis', 'calculate zeta',
            'compute the zeta function', 'calculate zeta function',
            'compute mathematical zeta', 'compute zeta for', 'zeta at s'
        ],
        'statistical_analysis': [
            'statistical analysis', 'analyze data', 'data analysis', 'statistics',
            'statistical methods', 'analyze statistically', 'run statistical analysis',
            'run stats', 'analyze this data statistically', 'statistical computation'
        ],
        'pattern_recognition': [
            'pattern recognition', 'find patterns', 'analyze patterns', 'pattern analysis',
            'detect patterns', 'pattern detection', 'find patterns in',
            'analyze patterns in', 'detect patterns in', 'pattern recognition in'
        ],
        'verification': [
            'verify this', 'double-check', 'cross-reference', 'verify claim', 'check accuracy',
            'validate this', 'confirm this', 'seems uncertain', 'might be outdated',
            'inconsistent data', 'questionable claim', 'needs verification'
        ],
        'current_events': [
            'what\'s new', 'latest developments', 'current trends', 'recent changes',
            'what\'s happening now', 'latest news', 'current events', 'trending',
            'what\'s hot', 'breaking news', 'recent advances', 'new developments'
        ],
        'multi_modal': [
            'logically', 'emotionally', 'creatively', 'critically', 'systems perspective',
            'multiple perspectives', 'balanced approach', 'comprehensive analysis',
            'different angles', 'various viewpoints', 'holistic view'
        ],
        'modify_personality_trait': [
            r"'modify_personality_trait'", 'modify_personality_trait', 'modify personality trait',
            'change personality trait', 'update personality trait', 'alter personality trait',
            'modify my personality', 'change my personality', 'update my trait'
        ],
        'evolve_personality_dimension': [
            r"'evolve_personality_dimension'", 'evolve_personality_dimension', 'evolve personality dimension',
            'evolve personality', 'modify personality dimension', 'change personality dimension',
            'evolve my personality', 'modify personality dimension'
        ],
        'add_personality_trait': [
            r"'add_personality_trait'", 'add_personality_trait', 'add personality trait',
            'add new personality trait', 'create personality trait', 'new personality trait',
            'add trait to personality', 'add new trait'
        ],
        'remove_personality_trait': [
            r"'remove_personality_trait'", 'remove_personality_trait', 'remove personality trait',
            'delete personality trait', 'remove trait from personality'
        ],
        'analyze_personality_growth': [
            r"'analyze_personality_growth'", 'analyze_personality_growth', 'analyze personality growth',
            'check personality growth', 'review personality development', 'analyze my personality growth',
            'check my personality', 'review my development'
        ],
        'compute_zeta_function': [
            r"'compute_zeta_function'", 'compute_zeta_function', 'compute zeta function',
            'calculate zeta function', 'zeta function computation', 'riemann zeta',
            'compute the zeta function', 'calculate zeta', 'compute zeta for',
            'zeta at s', 'compute mathematical zeta', 'compute zeta'
        ],
        'statistical_analysis': [
            r"'statistical_analysis'", 'statistical_analysis', 'statistical analysis',
            'analyze statistically', 'run statistics', 'statistical computation',
            'run stats on', 'run statistical analysis on', 'analyze data statistically',
            'run stats', 'compute statistics for'
        ],
        'pattern_recognition': [
            r"'pattern_recognition'", 'pattern_recognition', 'pattern recognition',
            'find patterns', 'analyze patterns', 'detect patterns', 'find patterns in',
            'analyze patterns in', 'detect patterns in', 'pattern recognition in'
        ],
        'move_mobile_base_forward': [
            r"'move_mobile_base_forward'", 'move_mobile_base_forward', 'move forward',
            'go forward', 'drive forward', 'move ahead', 'mobile base forward',
            'roll forward', 'advance mobile base', 'move mobile base forward'
        ],
        'move_mobile_base_backward': [
            r"'move_mobile_base_backward'", 'move_mobile_base_backward', 'move backward',
            'go backward', 'drive backward', 'move back', 'mobile base backward',
            'roll backward', 'reverse mobile base', 'move mobile base backward',
            'back up', 'go back'
        ],
        'turn_mobile_base_left': [
            r"'turn_mobile_base_left'", 'turn_mobile_base_left', 'turn left',
            'go left', 'rotate left', 'mobile base left', 'turn mobile base left',
            'spin left', 'pivot left'
        ],
        'turn_mobile_base_right': [
            r"'turn_mobile_base_right'", 'turn_mobile_base_right', 'turn right',
            'go right', 'rotate right', 'mobile base right', 'turn mobile base right',
            'spin right', 'pivot right'
        ],
        'stop_mobile_base': [
            r"'stop_mobile_base'", 'stop_mobile_base', 'stop moving',
            'halt mobile base', 'mobile base stop', 'stop the mobile base',
            'brake', 'apply brakes', 'come to a stop'
        ],
        'emergency_stop_mobile_base': [
            r"'emergency_stop_mobile_base'", 'emergency_stop_mobile_base', 'emergency stop',
            'e-stop', 'emergency brake', 'panic stop', 'immediate stop',
            'emergency halt', 'critical stop'
        ],
        'get_mobile_base_status': [
            r"'get_mobile_base_status'", 'get_mobile_base_status', 'mobile base status',
            'check mobile base', 'mobile base diagnostics', 'status check',
            'how is the mobile base', 'mobile base health', 'system status'
        ],
        'reset_mobile_base_emergency_stop': [
            r"'reset_mobile_base_emergency_stop'", 'reset_mobile_base_emergency_stop',
            'reset emergency stop', 'clear e-stop', 'reset emergency',
            'clear emergency stop', 'reset safety stop'
        ],
        'set_mobile_base_speed_limits': [
            r"'set_mobile_base_speed_limits'", 'set_mobile_base_speed_limits',
            'set speed limits', 'limit speed', 'adjust speed',
            'change speed limits', 'set maximum speed'
        ],
        'start_mobile_base_system': [
            r"'start_mobile_base_system'", 'start_mobile_base_system', 'start mobile base',
            'activate mobile base', 'power on mobile base', 'initialize mobile base',
            'start robotics system', 'launch mobile base control'
        ]
    }

    # Check for multi-step workflows first
    workflow_patterns = [
        r'first.+then|let me.+and then|I should.+and then',
        r'start with.+followed by|begin with.+then',
        r'search.+then.+research|research.+then.+analyze',
        r'recall.+then.+look|look.+then.+analyze',
        r'let me.+then|I will.+then',
        r'search.+then|research.+then|analyze.+then'
    ]

    workflow_detected = any(re.search(pattern, user_request, re.IGNORECASE) for pattern in workflow_patterns)

    if workflow_detected:
        workflow_result = parse_workflow_request(user_request, context_history)
        if workflow_result['success']:
            return workflow_result

    # Single tool detection
    detected_tools = []
    for tool_name, patterns in tool_patterns.items():
        if any(pattern in user_request for pattern in patterns):
            # Special handling for personality tools - extract parameters directly
            if tool_name in ['add_personality_trait', 'remove_personality_trait', 'modify_personality_trait', 'evolve_personality_dimension', 'analyze_personality_growth']:
                query = extract_personality_parameters(user_request, tool_name)
            else:
                query = extract_contextual_query(user_request, tool_name)

            if query:
                tool_call = create_tool_call(tool_name, query)
                if tool_call:
                    detected_tools.append(tool_call)

    # Return the most relevant single tool if multiple detected
    if detected_tools:
        return select_best_tool(detected_tools, user_request, context_history)

    return {"success": False, "message": "No recognized tool pattern found"}


def parse_workflow_request(user_request: str, context_history: List[Dict] = None) -> Dict[str, Any]:
    """
    Parse multi-step workflow requests into sequenced tool calls
    """
    import re

    # Define workflow step patterns
    workflow_steps = {
        'brain_search': {
            'patterns': [r'search.*brain|recall.*memory|check.*previous|look.*brain|recall.*what'],
            'tool': 'brain_network_search',
            'description': 'Search brain/memory for context'
        },
        'external_research': {
            'patterns': [r'research|look.*up|find.*information|external.*search|web.*search'],
            'tool': 'grokipedia_search',
            'description': 'Research external sources'
        },
        'analysis': {
            'patterns': [r'analyze|mathematical.*analysis|statistical.*analysis|pattern.*recognition|compute.*statistics'],
            'tool': 'statistical_analysis',  # Default, will be refined
            'description': 'Perform data/mathematical analysis'
        },
        'pattern_recognition': {
            'patterns': [r'find.*patterns|analyze.*patterns|pattern.*analysis'],
            'tool': 'pattern_recognition',
            'description': 'Analyze patterns in data'
        },
        'computation': {
            'patterns': [r'compute|calculate|mathematical.*computation'],
            'tool': 'compute_zeta_function',  # Default, will be refined based on context
            'description': 'Perform mathematical computations'
        },
        'verification': {
            'patterns': [r'verify|double.*check|cross.*reference|validate|confirm|seems.*uncertain|might.*outdated'],
            'tool': 'grokipedia_search',  # Use external search for verification by default
            'description': 'Verify information accuracy through cross-referencing'
        },
        'current_events': {
            'patterns': [r'what\'s new|latest|current.*trends|recent.*changes|what\'s happening|trending|breaking news'],
            'tool': 'grokipedia_search',  # Use external search for current events
            'description': 'Research current developments and trends'
        },
        'multi_modal': {
            'patterns': [r'logically|emotionally|creatively|critically|systems.*perspective|multiple.*perspectives|balanced.*approach'],
            'tool': 'search_knowledge',  # Use knowledge search for multi-modal analysis
            'description': 'Conduct multi-modal analysis from different perspectives'
        }
    }

    # Extract workflow steps from the request
    steps = []
    request_lower = user_request.lower()

    # Look for sequential indicators
    if 'first' in request_lower or 'start' in request_lower:
        # Parse first step
        for step_name, step_info in workflow_steps.items():
            if any(re.search(pattern, request_lower) for pattern in step_info['patterns']):
                query = extract_contextual_query(user_request, step_info['tool'])
                if query:
                    steps.append({
                        'tool_name': step_info['tool'],
                        'parameters': {'query': query} if step_info['tool'] in ['brain_network_search', 'grokipedia_search'] else {'data': query},
                        'description': step_info['description'],
                        'step': 'first'
                    })
                break

    if 'then' in request_lower:
        # Parse subsequent steps
        then_part = user_request.lower().split('then', 1)[1]
        for step_name, step_info in workflow_steps.items():
            if any(re.search(pattern, then_part) for pattern in step_info['patterns']):
                query = extract_contextual_query(user_request, step_info['tool'])
                if query:
                    steps.append({
                        'tool_name': step_info['tool'],
                        'parameters': {'query': query} if step_info['tool'] in ['brain_network_search', 'grokipedia_search'] else {'data': query},
                        'description': step_info['description'],
                        'step': 'then'
                    })
                break

    if steps:
        return {
            'success': True,
            'workflow': True,
            'steps': steps,
            'description': f'Executing {len(steps)}-step workflow: ' + ', '.join([s['description'] for s in steps])
        }

    return {'success': False, 'message': 'Could not parse workflow'}


def create_tool_call(tool_name: str, query: str) -> Dict[str, Any]:
    """Create standardized tool call structure"""
    if tool_name == 'brain_network_search':
        return {
            "tool_name": tool_name,
            "parameters": {
                "query": query,
                "memory_types": ["semantic", "episodic"]
            },
            "description": f"Searching brain for: {query}"
        }
    elif tool_name == 'google_web_search':
        return {
            "tool_name": tool_name,
            "parameters": {
                "query": query,
                "num_results": 10,
                "scrape_content": True,
                "scrape_top_n": 3
            },
            "description": f"Searching and scraping web content for: {query}"
        }
    elif tool_name == 'web_search_results_only':
        return {
            "tool_name": tool_name,
            "parameters": {
                "query": query,
                "num_results": 10
            },
            "description": f"Getting search results (no scraping) for: {query}"
        }
    elif tool_name == 'scrape_web_page':
        # For scraping, the query should be a URL
        return {
            "tool_name": tool_name,
            "parameters": {
                "url": query,  # URL to scrape
                "store_in_brain": True
            },
            "description": f"Searching Google and scraping content for: {query}"
        }
    elif tool_name == 'grokipedia_search':
        return {
            "tool_name": tool_name,
            "parameters": {
                "query": query,
                "max_results": 5
            },
            "description": f"Searching Grokipedia for: {query}"
        }
    elif tool_name == 'search_knowledge':
        return {
            "tool_name": tool_name,
            "parameters": {
                "query": query
            },
            "description": f"Analyzing knowledge for: {query}"
        }
    elif tool_name in ['compute_zeta_function', 'statistical_analysis', 'pattern_recognition']:
        return {
            "tool_name": tool_name,
            "parameters": {
                "data": query
            },
            "description": f"Performing {tool_name.replace('_', ' ')} on: {query}"
        }
    elif tool_name == 'verification':
        # Verification typically uses external search to cross-reference
        return {
            "tool_name": 'grokipedia_search',
            "parameters": {
                "query": query,
                "max_results": 5
            },
            "description": f"Verifying information accuracy for: {query}"
        }
    elif tool_name == 'current_events':
        # Current events use Google search for real-time information
        return {
            "tool_name": 'google_web_search',
            "parameters": {
                "query": f"latest {query}",
                "num_results": 10,
                "scrape_content": True,
                "scrape_top_n": 3
            },
            "description": f"Researching current developments in: {query}"
        }
    elif tool_name == 'multi_modal':
        # Multi-modal analysis uses knowledge search for comprehensive perspectives
        return {
            "tool_name": 'search_knowledge',
            "parameters": {
                "query": query
            },
            "description": f"Conducting multi-modal analysis of: {query}"
        }
    elif tool_name == 'modify_personality_trait':
        return {
            "tool_name": tool_name,
            "parameters": {
                "trait_name": "curiosity",  # Default, will be parsed from context
                "new_value": query,
                "reason": f"Modified via natural language: {query}"
            },
            "description": f"Modifying personality trait: {query}"
        }
    elif tool_name == 'evolve_personality_dimension':
        return {
            "tool_name": tool_name,
            "parameters": {
                "dimension_name": "curiosity",  # Default, will be parsed from context
                "new_value": 0.8,  # Default, will be parsed from context
                "reason": f"Evolved via natural language: {query}"
            },
            "description": f"Evolving personality dimension: {query}"
        }
    elif tool_name == 'add_personality_trait':
        return {
            "tool_name": tool_name,
            "parameters": {
                "new_trait": query,
                "reason": f"Added via natural language: {query}"
            },
            "description": f"Adding new personality trait: {query}"
        }
    elif tool_name == 'remove_personality_trait':
        return {
            "tool_name": tool_name,
            "parameters": {
                "trait_name": query,
                "reason": f"Removed via natural language: {query}"
            },
            "description": f"Removing personality trait: {query}"
        }
    elif tool_name == 'analyze_personality_growth':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Analyzing personality growth and development"
        }
    elif tool_name == 'move_mobile_base_forward':
        return {
            "tool_name": tool_name,
            "parameters": {
                "distance": query.get('distance', 1.0) if isinstance(query, dict) else 1.0,
                "speed": query.get('speed', 0.5) if isinstance(query, dict) else 0.5
            },
            "description": f"Moving mobile base forward {query.get('distance', 1.0) if isinstance(query, dict) else 1.0}m at {query.get('speed', 0.5) if isinstance(query, dict) else 0.5} m/s"
        }
    elif tool_name == 'move_mobile_base_backward':
        return {
            "tool_name": tool_name,
            "parameters": {
                "distance": query.get('distance', 1.0) if isinstance(query, dict) else 1.0,
                "speed": query.get('speed', 0.5) if isinstance(query, dict) else 0.5
            },
            "description": f"Moving mobile base backward {query.get('distance', 1.0) if isinstance(query, dict) else 1.0}m at {query.get('speed', 0.5) if isinstance(query, dict) else 0.5} m/s"
        }
    elif tool_name == 'turn_mobile_base_left':
        return {
            "tool_name": tool_name,
            "parameters": {
                "angle": query.get('angle', 1.57) if isinstance(query, dict) else 1.57,
                "speed": query.get('speed', 0.5) if isinstance(query, dict) else 0.5
            },
            "description": f"Turning mobile base left {query.get('angle', 1.57) if isinstance(query, dict) else 1.57} radians at {query.get('speed', 0.5) if isinstance(query, dict) else 0.5} rad/s"
        }
    elif tool_name == 'turn_mobile_base_right':
        return {
            "tool_name": tool_name,
            "parameters": {
                "angle": query.get('angle', 1.57) if isinstance(query, dict) else 1.57,
                "speed": query.get('speed', 0.5) if isinstance(query, dict) else 0.5
            },
            "description": f"Turning mobile base right {query.get('angle', 1.57) if isinstance(query, dict) else 1.57} radians at {query.get('speed', 0.5) if isinstance(query, dict) else 0.5} rad/s"
        }
    elif tool_name == 'stop_mobile_base':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Stopping mobile base movement"
        }
    elif tool_name == 'emergency_stop_mobile_base':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Activating emergency stop for mobile base"
        }
    elif tool_name == 'get_mobile_base_status':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Getting mobile base status and diagnostics"
        }
    elif tool_name == 'reset_mobile_base_emergency_stop':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Resetting mobile base emergency stop condition"
        }
    elif tool_name == 'set_mobile_base_speed_limits':
        return {
            "tool_name": tool_name,
            "parameters": {
                "max_linear": query.get('max_linear', 1.0) if isinstance(query, dict) else 1.0,
                "max_angular": query.get('max_angular', 1.0) if isinstance(query, dict) else 1.0
            },
            "description": f"Setting mobile base speed limits: linear={query.get('max_linear', 1.0) if isinstance(query, dict) else 1.0} m/s, angular={query.get('max_angular', 1.0) if isinstance(query, dict) else 1.0} rad/s"
        }
    elif tool_name == 'start_mobile_base_system':
        return {
            "tool_name": tool_name,
            "parameters": {},
            "description": "Starting mobile base ROS2 control system"
        }
    return None


def select_best_tool(detected_tools: List[Dict], user_request: str, context_history: List[Dict] = None) -> Dict[str, Any]:
    """Select the most appropriate tool from multiple detections"""
    if not detected_tools:
        return {'success': False}

    if len(detected_tools) == 1:
        return {**detected_tools[0], 'success': True}

    # Simple priority: prefer brain search first, then external research, then analysis
    priority_order = ['brain_network_search', 'grokipedia_search', 'search_knowledge',
                     'statistical_analysis', 'pattern_recognition', 'compute_zeta_function']

    for tool_name in priority_order:
        for tool in detected_tools:
            if tool['tool_name'] == tool_name:
                return {**tool, 'success': True}

    # Fallback to first detected
    return {**detected_tools[0], 'success': True}


def extract_personality_parameters(text: str, tool_name: str) -> str:
    """
    Extract personality-related parameters from natural language
    Specifically designed for personality modification tools
    """
    import re

    # Look for trait names in quotes or after "let's say" or similar phrases
    trait_patterns = [
        r"'([^']+)'",  # Single quotes: 'trait name'
        r'"([^"]+)"',  # Double quotes: "trait name"
        r"let's say[, ]*([^—\(\)\.,\n]+)",  # "let's say, trait name" (stop at em dash, parens, etc.)
        r"called ([^—\(\)\.,\n]+)",  # "called trait name"
        r"named ([^—\(\)\.,\n]+)",  # "named trait name"
        r"add ([^—\(\)\.,\n]+) (?:to|as)",  # "add trait to"
        r"remove ([^—\(\)\.,\n]+) (?:from|as)",  # "remove trait from"
        r"modify ([^—\(\)\.,\n]+) (?:to|as)",  # "modify trait to"
        r"evolve ([^—\(\)\.,\n]+) (?:to|as)",  # "evolve trait to"
    ]

    # Prioritize patterns - more specific/intentional patterns first
    pattern_priority = [
        (r"let's say[, ]*([^—\(\)\.,\n]+)", "let's say pattern"),
        (r"called ([^—\(\)\.,\n]+)", "called pattern"),
        (r"named ([^—\(\)\.,\n]+)", "named pattern"),
        (r"add ([^—\(\)\.,\n]+) (?:to|as)", "add pattern"),
        (r"remove ([^—\(\)\.,\n]+) (?:from|as)", "remove pattern"),
        (r"modify ([^—\(\)\.,\n]+) (?:to|as)", "modify pattern"),
        (r"evolve ([^—\(\)\.,\n]+) (?:to|as)", "evolve pattern"),
        (r"'([^']+)'", "single quotes (lowest priority)"),
        (r'"([^"]+)"', "double quotes (lowest priority)"),
    ]

    for pattern, description in pattern_priority:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Clean up the match
            match = max(matches, key=len).strip()
            # Remove common prefixes that might be captured
            match = re.sub(r'^(?:a |an |the |my |our |your |this |that )\s*', '', match, flags=re.IGNORECASE)
            if len(match) > 3:  # Must be reasonable length
                return match

    # Fallback: look for personality-related words after the tool mention
    tool_mention_patterns = [
        r'add_personality_trait[^a-zA-Z]*([a-zA-Z][^—\(\)\.,\n]*)',
        r'modify_personality_trait[^a-zA-Z]*([a-zA-Z][^—\(\)\.,\n]*)',
        r'remove_personality_trait[^a-zA-Z]*([a-zA-Z][^—\(\)\.,\n]*)',
        r'evolve_personality_dimension[^a-zA-Z]*([a-zA-Z][^—\(\)\.,\n]*)'
    ]

    for pattern in tool_mention_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1).strip():
            trait = match.group(1).strip()
            # Clean up common words that might be captured
            trait = re.sub(r'^(?:a |an |the |my |our |your )\s*', '', trait, flags=re.IGNORECASE)
            if len(trait) > 3:  # Must be a reasonable length
                return trait

    # Last resort: extract any noun phrase after personality-related keywords
    personality_keywords = ['trait', 'dimension', 'personality', 'characteristic', 'quality']
    for keyword in personality_keywords:
        keyword_pattern = rf'{keyword}[^a-zA-Z]*([a-zA-Z][^—\(\)\.,\n]+)'
        match = re.search(keyword_pattern, text, re.IGNORECASE)
        if match and match.group(1).strip():
            trait = match.group(1).strip()
            trait = re.sub(r'^(?:a |an |the |my |our |your )\s*', '', trait, flags=re.IGNORECASE)
            if len(trait) > 3:
                return trait

    return ""  # No parameter found

def extract_contextual_query(text: str, tool_name: str) -> str:
    """
    Extract a meaningful query from AI response context
    Based on the tool being used and surrounding text
    """
    import re
    import json

    # First, try to extract query from JSON structures (for properly formed tool calls)
    # Look for query parameters in JSON-like structures
    json_patterns = [
        r'"query"\s*:\s*"([^"]+)"',  # "query": "value"
        r"'query'\s*:\s*'([^']+)'",  # 'query': 'value'
        r'"search"\s*:\s*"([^"]+)"',  # "search": "value"
        r"'search'\s*:\s*'([^']+)'",  # 'search': 'value'
    ]

    for pattern in json_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            query = match.group(1).strip()
            if len(query) > 3:  # Reasonable length
                return query

    # No hardcoded topics - rely on AI-generated content and memory analysis
    # This prevents repetitive searches from static lists

    # Extract nouns and key terms using simple heuristics
    # Remove tool mentions and parameter syntax to avoid picking up malformed queries
    cleaned = re.sub(r"'(brain_network_search|grokipedia_search|grokedia_search|google_web_search|web_search_results_only|scrape_web_page|search_knowledge)'", '', text, flags=re.IGNORECASE)

    # Remove JSON-like structures that might be malformed/truncated
    # Remove complete JSON objects first
    cleaned = re.sub(r'\{[^}]*\}', '', cleaned, flags=re.DOTALL)
    # Remove partial JSON structures
    cleaned = re.sub(r'\{"[^"]*"[^}]*$', '', cleaned)  # Remove incomplete JSON starting with {"
    cleaned = re.sub(r'[^"]*"[^}]*$', '', cleaned)  # Remove other incomplete JSON
    # Remove parameter-related patterns that might be picked up as queries
    cleaned = re.sub(r'["\']parameters["\']:\s*\{[^}]*\}', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'["\']tool_name["\']:\s*["\'][^"\']*["\']', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'["\']query["\']:\s*["\'][^"\']*["\']', '', cleaned, flags=re.IGNORECASE)
    # Remove JSON syntax artifacts
    cleaned = re.sub(r'[{}\[\],"]+', '', cleaned)
    # Clean up extra whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # Enhanced subject matter extraction patterns - prioritize meaningful phrases
    subject_patterns = [
        # Explicit search patterns (highest priority)
        r'\*\*Search:\s*(.+?)\*\*',  # **Search: topic**
        r'Search:\s*(.+?)(?:\s|$)',  # Search: topic
        r'look\s+up\s+(.+?)(?:\s|$)',  # "look up information"
        r'find\s+(?:information\s+about|more\s+about|out\s+about)\s+(.+?)(?:\s|$)',  # "find information about"

        # Research and analysis patterns
        r'research\s+(?:on|into|about)\s+(.+?)(?:\s|$)',  # "research on AI models"
        r'explore\s+(.+?)(?:\s|$)',  # "explore new approaches"
        r'investigate\s+(.+?)(?:\s|$)',  # "investigate optimization techniques"
        r'understanding\s+of\s+(.+?)(?:\s|$)',  # "understanding of complex systems"

        # Direct object patterns
        r'about\s+(.+?)(?:\s|$)',  # "about renewable energy"
        r'on\s+(.+?)(?:\s|$)',  # "on urban microgrids"
        r'for\s+(.+?)(?:\s|$)',  # "for energy optimization"
        r'regarding\s+(.+?)(?:\s|$)',  # "regarding solar power"

        # Topic and concept patterns (avoid picking up tool names)
        r'(.+?)\s+systems?(?!\s*["\']|\s*call|\s*parameters)',  # "energy systems", "AI systems"
        r'(.+?)\s+models?(?!\s*["\']|\s*call|\s*parameters)',  # "optimization models", "prediction models"
        r'(.+?)\s+technolog(?:y|ies)(?!\s*["\']|\s*call|\s*parameters)',  # "renewable technology", "AI technologies"

        # Mathematical and analytical patterns
        r'analyze\s+(.+?)(?:\s|$)',  # "analyze this data"
        r'compute\s+(.+?)(?:\s|$)',  # "compute zeta function"
        r'calculate\s+(.+?)(?:\s|$)',  # "calculate statistics"
    ]

    for pattern in subject_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if len(candidate.split()) <= 4:  # Keep it concise
                return candidate

    # Fallback: extract meaningful phrases (avoid tool-related terms)
    words = cleaned.split()
    if len(words) >= 2:
        # Take 2-4 word phrases that seem meaningful and don't contain tool syntax
        candidates = []
        tool_related_terms = ['tool', 'call', 'parameters', 'query', 'search', 'brain', 'network', 'grokipedia', 'google', 'web', 'scrape', 'knowledge']

        # Try longer phrases first (3-4 words), then shorter ones
        for phrase_length in [3, 4, 2]:
            if phrase_length > len(words):
                continue

            for i in range(len(words) - phrase_length + 1):
                phrase = ' '.join(words[i:i+phrase_length])
                if (len(phrase) > 8 and  # Longer minimum length for quality
                    not any(word in phrase.lower() for word in ['the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'this', 'that', 'these', 'those']) and
                    not any(term in phrase.lower() for term in tool_related_terms) and
                    not phrase[0].islower()):  # Start with capital letter (likely proper noun)
                    candidates.append(phrase)

            if candidates:
                # Return the longest meaningful phrase
                return max(candidates, key=len)

    # Final fallback - extract any remaining meaningful words
    meaningful_words = []
    for word in words:
        word = word.strip('.,!?;:')
        if len(word) > 3 and word[0].isupper() and not any(term in word.lower() for term in tool_related_terms):
            meaningful_words.append(word)

    if meaningful_words:
        # Return up to 3 meaningful words
        return ' '.join(meaningful_words[:3])

    # Ultimate fallback - use tool-appropriate generic topic
    tool_fallbacks = {
        'grokipedia_search': 'current technological trends',
        'grokedia_search': 'current technological trends',
        'google_web_search': 'latest developments',
        'web_search_results_only': 'recent research',
        'scrape_web_page': 'information analysis',
        'brain_network_search': 'knowledge synthesis',
        'search_knowledge': 'concept exploration'
    }

    return tool_fallbacks.get(tool_name, 'general research')


def parse_and_execute_tool_calls(ai_response: str, conversation_id: str = "default", brain: BrainSystem = None) -> Dict[str, Any]:
    """
    Parse and execute tool calls from AI response using DIRECT tool execution.
    
    Previously routed through localhost:8083 Tool API server (which was never running),
    causing all tool calls to 404. Now calls execute_tool_call() directly from brain_system.
    """
    # Import the direct execution function (not the HTTP API client)
    from repryntt.tools.tool_interface import execute_tool_call

    results = {
        'tool_calls_executed': [],
        'tool_calls_failed': [],
        'insights_summary': [],
        'message': '',
        'direct_execution': True
    }

    try:
        # Extract TOOL_CALL JSON from AI response
        json_pattern = r'TOOL_CALL:\s*(\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\})'
        json_matches = re.findall(json_pattern, ai_response, re.DOTALL)

        tool_calls = []

        for match in json_matches:
            try:
                tool_call_data = json.loads(match.strip())
                if isinstance(tool_call_data, dict):
                    # Handle both 'tool_name'/'parameters' AND 'tool'/'params' variants
                    t_name = tool_call_data.get('tool_name') or tool_call_data.get('tool')
                    t_params = tool_call_data.get('parameters') or tool_call_data.get('params', {})
                    if t_name:
                        tool_calls.append({
                            'tool_name': t_name,
                            'parameters': t_params
                        })
            except json.JSONDecodeError:
                continue

        if not tool_calls:
            # Try simplified format: tool_name: {params}
            simplified_pattern = r'(\w+):\s*(\{[^{}]*\})'
            simplified_matches = re.findall(simplified_pattern, ai_response)

            for tool_name, params_str in simplified_matches:
                try:
                    parameters = json.loads(params_str)
                    tool_calls.append({
                        'tool_name': tool_name,
                        'parameters': parameters
                    })
                except json.JSONDecodeError:
                    continue

        if not tool_calls:
            results['message'] = "No tool calls detected in AI response"
            return results

        # Execute tools DIRECTLY via execute_tool_call (no HTTP API server needed)
        logger.info(f"🔧 Executing {len(tool_calls)} tools directly")

        for tool_call in tool_calls:
            tool_name = tool_call['tool_name']
            parameters = tool_call['parameters']

            # ── Neural Cortex: Guardian validation before execution ──
            try:
                from repryntt.cortex.dispatcher import get_dispatcher as _get_disp
                _disp = _get_disp()
                if _disp.get_region("guardian"):
                    _guard = _disp.request_guardian_validation(tool_name, parameters)
                    _inner = _guard.get("result", {})
                    if not _inner.get("allowed", False):
                        _reason = _inner.get("reason", "blocked by guardian")
                        logger.warning("🛡️ Guardian BLOCKED tool '%s': %s", tool_name, _reason)
                        results['tool_calls_failed'].append({
                            'tool': tool_name,
                            'tool_name': tool_name,
                            'parameters': parameters,
                            'error': f"Guardian blocked: {_reason}",
                        })
                        # Emit telemetry for guardian block
                        try:
                            from repryntt.telemetry import get_ops_logger
                            _ops_log = get_ops_logger()
                            if _ops_log:
                                _ops_log.log("CORTEX", "cortex_guardian_block", "ACT",
                                             metadata={"tool": tool_name, "reason": _reason})
                        except Exception:
                            pass
                        continue
            except Exception as _ge:
                logger.debug("Guardian check failed (non-fatal, proceeding): %s", _ge)

            try:
                start_time = time.time()

                # Per-tool execution timeout (30s default, 120s for known slow tools)
                _SLOW_TOOLS = {"scrape_web_page", "web_search", "google_search",
                               "generate_image", "generate_video", "execute_code",
                               "grokipedia_search", "send_email", "jupiter_swap"}
                _tool_timeout = 120 if tool_name in _SLOW_TOOLS else 30

                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(execute_tool_call, tool_name, parameters, brain)
                    try:
                        step_result = _fut.result(timeout=_tool_timeout)
                    except _cf.TimeoutError:
                        logger.warning("⏰ Tool '%s' timed out after %ds", tool_name, _tool_timeout)
                        results['tool_calls_failed'].append({
                            'tool': tool_name,
                            'tool_name': tool_name,
                            'parameters': parameters,
                            'error': f"Tool execution timed out after {_tool_timeout}s",
                        })
                        continue

                execution_time = time.time() - start_time

                if step_result.get('success'):
                    # Store raw result to file
                    stored_file = store_tool_result(
                        tool_name=tool_name,
                        parameters=parameters,
                        result=step_result.get('result', {}),
                        conversation_id=conversation_id,
                        execution_time=execution_time
                    )

                    # Extract insights for AI consumption
                    insights = extract_tool_insights(tool_name, parameters, step_result.get('result', {}))

                    results['tool_calls_executed'].append({
                        'tool': tool_name,
                        'tool_name': tool_name,
                        'parameters': parameters,
                        'insight': insights,
                        'insights': insights,
                        'stored_file': stored_file,
                        'execution_time': execution_time
                    })
                    results['insights_summary'].append(insights)

                    # Store tool usage into semantic memory (vector brain) for future recall
                    if brain and hasattr(brain, 'store_semantic_memory'):
                        try:
                            param_summary = ', '.join(f"{k}={v}" for k, v in parameters.items())[:150]
                            memory_content = (
                                f"Used tool '{tool_name}' with parameters: {param_summary}. "
                                f"Insights: {insights[:300]}"
                            )
                            brain.store_semantic_memory(
                                topic=f"tool_usage_{tool_name}_{int(time.time())}",
                                content=memory_content,
                                domain="tool_experience",
                                confidence=0.9,
                                source="tool_execution",
                                key_facts=[f"Tool '{tool_name}' handles: {param_summary}"],
                                related_topics=[tool_name, "tool_usage", "capabilities"]
                            )
                        except Exception as e:
                            logger.debug(f"Could not store tool usage to semantic memory: {e}")

                    logger.info(f"✅ Tool '{tool_name}' executed successfully ({execution_time:.2f}s)")
                else:
                    error_msg = step_result.get('error', 'Unknown error')
                    results['tool_calls_failed'].append({
                        'tool': tool_name,
                        'tool_name': tool_name,
                        'parameters': parameters,
                        'error': error_msg
                    })
                    logger.warning(f"⚠️ Tool '{tool_name}' failed: {error_msg}")

            except Exception as e:
                results['tool_calls_failed'].append({
                    'tool': tool_name,
                    'tool_name': tool_name,
                    'parameters': parameters,
                    'error': str(e)
                })
                logger.error(f"❌ Tool '{tool_name}' execution error: {e}")

        executed = len(results['tool_calls_executed'])
        failed = len(results['tool_calls_failed'])
        results['message'] = f"Executed {executed} tools directly ({failed} failed)" if executed else f"All {failed} tool calls failed"

        return results

    except Exception as e:
        logger.error(f"Error in direct tool execution: {e}")
        results['message'] = f"Tool execution error: {str(e)}"
        return results

if __name__ == "__main__":
    # Test the tool interface
    logging.basicConfig(level=logging.INFO)

    interface = create_ai_interface()

    # Test conversation initialization
    interface.initialize_conversation("test_conversation", "AI learning")

    # Test tool detection
    user_input = "What is quantum computing?"
    ai_reasoning = "I know some basics but should check current research"

    tool_analysis = interface.detect_tool_needs(user_input, ai_reasoning)
    print(f"Tool analysis: {tool_analysis}")

    # Test context retrieval
    context = interface.get_context_for_response(user_input)
    print(f"Context length: {len(context.split())} words")

    # Test tool calling
    if tool_analysis['needs_tools']:
        for tool_name in tool_analysis['recommended_tools'][:1]:  # Test first tool
            result = interface.call_tool(tool_name, {'query': user_input})
            print(f"Tool {tool_name} result: {result['success']}")

    print("✅ Tool interface test completed")
