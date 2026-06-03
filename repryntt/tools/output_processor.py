"""
Centralized AI Output Processor for SAIGE.

Replaces 27+ scattered parsers with one unified pipeline.
Every AI response passes through here to detect and route:
- Tool calls → extracted for execution
- Chain completion signals → flagged
- Goals → extracted for queuing
- JSON data → parsed with robust fallbacks
- CONCLUDE/CONTINUE → flagged
- Directives → extracted from natural language

Usage:
    processor = AIOutputProcessor(brain_system)
    result = processor.process(ai_response, context='chain_step')
    
    if result.tool_calls:
        processor.execute_tool_calls(result)
    if result.chain_complete:
        # handle chain conclusion
    if result.goals:
        processor.queue_goals(result)
"""

import re
import json
import time
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

logger = logging.getLogger(__name__)


@dataclass
class AIOutputResult:
    """Structured result from processing AI output through the unified pipeline."""
    raw_text: str
    
    # Tool calls detected
    tool_calls: List[Dict] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    tools_executed: bool = False
    
    # Chain completion signals
    chain_complete: bool = False
    chain_complete_summary: str = ""
    conclude_signal: bool = False
    continue_signal: bool = False
    
    # Extracted JSON (if response contained JSON)
    json_data: Optional[Dict] = None
    json_valid: bool = False
    
    # Goals extracted
    goals: List[Dict] = field(default_factory=list)
    
    # Meta-decision fields
    primary_focus: Optional[str] = None
    attention_allocation: Optional[Dict] = None
    
    # Natural language directives
    directives: List[str] = field(default_factory=list)


class AIOutputProcessor:
    """Centralized processor for ALL AI model outputs.
    
    Replaces scattered parsing across evolution_loop, consciousness_daemon,
    brain_system, tool_interface, and daily_plan_executor with one unified
    detection + action pipeline.
    
    Detection is automatic (process() finds everything).
    Execution is caller-controlled (execute_tool_calls(), queue_goals()).
    """
    
    # ---- Compiled Patterns ----
    
    TOOL_CALL_JSON = re.compile(
        r'TOOL_CALL:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})',
        re.IGNORECASE
    )
    
    # Known tool names — used for simplified format: tool_name: {params}
    # Must match brain_system._initialize_tools() registry
    KNOWN_TOOLS = [
        # Knowledge & memory
        'brain_network_search', 'recall_memory', 'search_knowledge',
        'grokipedia_search', 'grokedia_search',  # grokedia = common AI misspelling
        'store_learning', 'store_memory', 'save_thought',
        'get_relevant_context', 'analyze_topic', 'find_similar_topics',
        'search_domain', 'query_exploration_history',
        'pull_knowledge_topics', 'integrate_knowledge_context',
        
        # Web search & scraping
        'google_web_search', 'google_search', 'web_search',
        'web_search_results_only', 'search_results_only',
        'scrape_web_page', 'scrape_url',
        'duckduckgo_search', 'fetch_web_info', 'extract_content',
        
        # Chain-of-thought management
        'create_chain_of_thought', 'create_self_autonomous_chain',
        'advance_self_autonomous_chain', 'update_chain_progress',
        'get_chain_context', 'queue_chain_of_thought',
        'get_cot_queue_status', 'clear_cot_queue',
        
        # File & code tools
        'read_file', 'write_file', 'grep_search', 'list_dir',
        'create_creative_file', 'write_to_creative_file',
        'append_to_creative_file', 'read_creative_file',
        'get_creative_workspace_status',
        'run_terminal_cmd', 'search_replace',
        'analyze_codebase', 'run_code_tests', 'check_syntax', 'get_code_context',
        
        # Personality & self-modification
        'modify_personality_trait', 'evolve_personality_dimension',
        'update_behavioral_guidelines', 'recreate_autonomous_personality',
        'add_personality_trait', 'remove_personality_trait',
        'log_personality_evolution', 'analyze_personality_growth',
        
        # System & capabilities
        'query_capabilities', 'get_function_details', 'get_system_map',
        'get_brain_stats', 'get_current_time', 'check_time',
        'update_procedural',
        'clear_grokipedia_history', 'reset_inspiration_index',
        
        # Math tools
        'compute_zeta_function', 'analyze_zeta_zeros',
        'symbolic_manipulation', 'numerical_analysis',
        'statistical_analysis', 'pattern_recognition',
        'access_mathematical_databases', 'mathematical_visualization',
        
        # Maps & navigation
        'google_maps_search', 'get_directions',
        'geocode_address', 'find_nearby_places',
        
        # Robot economy
        'start_robot_economy', 'stop_robot_economy', 'get_economy_status',
        'submit_workload', 'get_wallet_balance', 'get_blockchain_info',
        'allocate_dao_funds', 'create_robot_wallet',
        'recover_robot_wallet', 'monitor_economy',
        
        # Conversation & social
        'initiate_conversation', 'start_conversation', 'talk_to_human',
        'post_tweet', 'tweet', 'check_twitter_mentions',
        'reply_to_twitter', 'get_twitter_status', 'twitter_status',
        'get_recent_conversations', 'search_conversations',
        'get_conversation_summary', 'export_conversation',
    ]
    
    CHAIN_COMPLETE_MARKERS = [
        'chain complete:',
        'chain complete',
        'exploration complete',
        'goal achieved',
        'conclusion reached',
        'investigation complete',
        'research complete',
        'analysis complete',
    ]
    
    def __init__(self, brain_system=None):
        self.brain_system = brain_system
        # Build simplified tool pattern dynamically
        tool_names_pattern = '|'.join(re.escape(t) for t in self.KNOWN_TOOLS)
        self.SIMPLIFIED_TOOL = re.compile(
            rf'({tool_names_pattern}):\s*(\{{[^{{}}]*\}})',
            re.IGNORECASE
        )
    
    def set_brain_system(self, brain_system):
        """Set/update brain system reference."""
        self.brain_system = brain_system
    
    # ================================================================
    # MAIN PIPELINE
    # ================================================================
    
    def process(self, raw_text: str, context: str = None) -> AIOutputResult:
        """Main processing pipeline. Call on any AI response.
        
        Runs ALL detectors and returns structured result.
        Context-specific extraction runs when context hint is provided.
        
        Args:
            raw_text: Raw AI response text
            context: Optional hint for context-specific parsing:
                     'chain_step'      - chain contribution (check tools, completion)
                     'meta_decision'   - consciousness meta-decision (extract focus, allocation)
                     'goal_formation'  - goal formation (extract goals)
                     'conclude_check'  - CONCLUDE/CONTINUE check
                     'daily_plan'      - daily plan (extract tools, chains)
                     'consciousness'   - background consciousness reasoning
                     'self_reflection' - AI self-reflection
        
        Returns:
            AIOutputResult with all detected elements
        """
        if not raw_text:
            return AIOutputResult(raw_text="")
        
        result = AIOutputResult(raw_text=raw_text)
        
        # === Universal detection (always runs) ===
        result.tool_calls = self._extract_tool_calls(raw_text)
        result.chain_complete, result.chain_complete_summary = self._detect_chain_complete(raw_text)
        result.conclude_signal, result.continue_signal = self._detect_conclude_continue(raw_text)
        result.json_data, result.json_valid = self._extract_json(raw_text)
        result.directives = self._extract_directives(raw_text)
        
        # === Context-specific extraction ===
        if result.json_valid and result.json_data:
            if context == 'goal_formation':
                result.goals = self._extract_goals(result.json_data)
            elif context == 'meta_decision':
                result.primary_focus = result.json_data.get('primary_focus')
                result.attention_allocation = result.json_data.get('attention_allocation')
        
        # If goal_formation context but JSON failed, try regex fallback
        if context == 'goal_formation' and not result.goals and not result.json_valid:
            result.goals = self._extract_goals_fallback(raw_text)
        
        # If meta_decision context but JSON failed, try regex fallback
        if context == 'meta_decision' and not result.primary_focus and not result.json_valid:
            result.primary_focus = self._extract_primary_focus_fallback(raw_text)
            result.attention_allocation = self._extract_attention_fallback(raw_text)
        
        # Log findings
        findings = []
        if result.tool_calls:
            findings.append(f"{len(result.tool_calls)} tool calls")
        if result.chain_complete:
            findings.append("CHAIN_COMPLETE")
        if result.conclude_signal:
            findings.append("CONCLUDE")
        if result.continue_signal:
            findings.append("CONTINUE")
        if result.goals:
            findings.append(f"{len(result.goals)} goals")
        if result.json_valid:
            findings.append("valid_JSON")
        if result.directives:
            findings.append(f"{len(result.directives)} directives")
        if findings:
            logger.info(f"📡 Output processor: {', '.join(findings)}")
        
        return result
    
    # ================================================================
    # TOOL CALL EXTRACTION (LEGACY — text-based TOOL_CALL: format)
    # With native OpenAI tool calling, the AI uses structured tool_calls 
    # via the API instead of writing TOOL_CALL: text. This extraction
    # remains as a fallback for any callers still using the old pattern.
    # ================================================================
    
    def _extract_tool_calls(self, text: str) -> List[Dict]:
        """Extract text-based tool calls from AI output (legacy fallback).
        
        NOTE: With native tool calling via _call_ai_service(), the AI no longer
        writes TOOL_CALL: text. This will typically return empty lists.
        Kept for backward compatibility with old autonomous cycle methods.
        """
        tool_calls = []
        
        # Method 1: TOOL_CALL: {json} format (most common)
        for match in self.TOOL_CALL_JSON.finditer(text):
            try:
                tc = json.loads(match.group(1))
                if 'tool_name' in tc:
                    tool_calls.append(tc)
            except json.JSONDecodeError:
                pass
        
        # Method 2: tool_name: {params} simplified format (fallback)
        if not tool_calls:
            for match in self.SIMPLIFIED_TOOL.finditer(text):
                try:
                    params = json.loads(match.group(2))
                    tool_calls.append({
                        'tool_name': match.group(1),
                        'parameters': params
                    })
                except json.JSONDecodeError:
                    pass
        
        return tool_calls
    
    # ================================================================
    # CHAIN COMPLETION DETECTION (replaces 3 scattered implementations)
    # ================================================================
    
    def _detect_chain_complete(self, text: str) -> Tuple[bool, str]:
        """Unified chain completion detection.
        
        Replaces:
        - brain_system.py advance_self_autonomous_chain() inline check
        - evolution_loop.py _should_pivot_to_new_chain() keyword list
        - evolution_loop.py _check_goal_achievement() keyword heuristics
        - consciousness_daemon.py _contribute_to_active_chain() check
        
        Detection rules (strict to prevent false positives):
        1. "CHAIN COMPLETE" must appear at the START of the response or start of a line
        2. Reject if followed by future-intent language ("my next step", "will investigate", etc.)
           because the AI is describing what it WOULD do, not actually concluding
        """
        text_stripped = text.strip()
        text_lower = text_stripped.lower()
        
        # Check if CHAIN COMPLETE appears at the very start of the response
        starts_with_cc = text_lower.startswith('chain complete')
        
        # Also check if it appears at the start of any line (after a newline)
        line_starts_with_cc = False
        for line in text_stripped.split('\n'):
            line_stripped = line.strip().lower()
            if line_stripped.startswith('chain complete'):
                line_starts_with_cc = True
                # Get the text after the marker for summary extraction
                idx = text_lower.index(line_stripped[:20])
                break
        
        if not starts_with_cc and not line_starts_with_cc:
            return False, ""
        
        # Found "CHAIN COMPLETE" at start of response or line — now check for false positives
        # If the text AFTER "CHAIN COMPLETE:" describes a future action, it's not a real conclusion
        if starts_with_cc:
            after_marker = text_stripped[len('CHAIN COMPLETE'):].lstrip(':').strip()
        else:
            # Find the marker position
            for line in text_stripped.split('\n'):
                if line.strip().lower().startswith('chain complete'):
                    after_marker = line.strip()[len('CHAIN COMPLETE'):].lstrip(':').strip()
                    break
            else:
                after_marker = ""
        
        after_lower = after_marker.lower()[:200]
        
        # Reject false positives: AI is saying what it WOULD do next, not concluding
        false_positive_phrases = [
            'my next step', 'next step involves', 'i will investigate',
            'will conduct', 'will search', 'will explore', 'will research',
            'should investigate', 'should explore', 'plan to',
            'i need to', 'need to investigate', 'need to search',
            'going to search', 'going to investigate', 'going to explore'
        ]
        
        for phrase in false_positive_phrases:
            if phrase in after_lower:
                return False, ""
        
        # Passed all checks — this is a genuine chain completion
        summary = after_marker[:500] if after_marker else "Chain completed"
        return True, summary
    
    def _detect_conclude_continue(self, text: str) -> Tuple[bool, bool]:
        """Detect CONCLUDE or CONTINUE signals.
        
        Replaces:
        - brain_system.py AutonomousConclusionEvaluator keyword check
        - consciousness_daemon.py CONCLUDE/CONTINUE response parsing
        """
        text_stripped = text.strip().upper()
        first_50 = text_stripped[:50]
        
        conclude = 'CONCLUDE' in first_50
        continue_ = 'CONTINUE' in first_50
        
        return conclude, continue_
    
    # ================================================================
    # JSON EXTRACTION (replaces 4+ duplicated implementations)
    # ================================================================
    
    def _extract_json(self, text: str) -> Tuple[Optional[Dict], bool]:
        """Robust JSON extraction with multiple fallback strategies.
        
        Replaces:
        - consciousness_daemon.py meta-decision JSON extraction
        - consciousness_daemon.py goal formation JSON extraction
        - consciousness_daemon.py attention allocation JSON extraction
        - evolution_loop.py _extract_json_from_response()
        - brain_system.py _integrate_autonomous_personality() JSON extraction
        """
        # Strategy 1: Direct parse (response is pure JSON)
        try:
            data = json.loads(text.strip())
            if isinstance(data, dict):
                return data, True
        except (json.JSONDecodeError, ValueError):
            pass
        
        # Strategy 2: Markdown code block (```json ... ```)
        code_block = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        if code_block:
            json_str = code_block.group(1).strip()
            result = self._try_parse_json(json_str)
            if result is not None:
                return result, True
        
        # Strategy 3: Brace matching (first { to last })
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace >= 0 and last_brace > first_brace:
            json_str = text[first_brace:last_brace + 1]
            result = self._try_parse_json(json_str)
            if result is not None:
                return result, True
        
        return None, False
    
    def _try_parse_json(self, json_str: str) -> Optional[Dict]:
        """Try to parse JSON with common AI output fixups."""
        # Direct attempt
        try:
            data = json.loads(json_str)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        
        # Fix common AI JSON issues
        try:
            fixed = json_str
            # Remove trailing commas before } or ]
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            # Remove control characters
            fixed = re.sub(r'[\x00-\x1f]', ' ', fixed)
            # Fix unquoted keys (common Phi-3 output: cursor_focus: { instead of "cursor_focus": {)
            fixed = re.sub(r'(?<=[{,\s])([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'"\1":', fixed)
            # Fix newlines inside JSON (common with Phi-3 output)
            lines = fixed.split('\n')
            fixed = ' '.join(line.strip() for line in lines)
            data = json.loads(fixed)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        
        # Last resort: try to fix broken nesting by removing malformed inner objects
        try:
            # Remove content between unmatched braces that break the structure
            fixed = json_str
            fixed = re.sub(r'[\x00-\x1f]', ' ', fixed)
            fixed = re.sub(r'(?<=[{,\s])([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'"\1":', fixed)
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            # Collapse to single line
            fixed = ' '.join(fixed.split())
            # Try incremental truncation — find the last valid JSON prefix
            for end_pos in range(len(fixed), max(len(fixed) - 200, 0), -1):
                candidate = fixed[:end_pos]
                # Count braces
                open_b = candidate.count('{')
                close_b = candidate.count('}')
                if open_b > close_b:
                    candidate += '}' * (open_b - close_b)
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, ValueError):
                    continue
        except Exception:
            pass
        
        return None
    
    # ================================================================
    # GOAL EXTRACTION (replaces consciousness_daemon goal parsing)
    # ================================================================
    
    def _extract_goals(self, json_data: Dict) -> List[Dict]:
        """Extract goal objects from parsed JSON.
        
        Replaces consciousness_daemon.py goal formation parsing (~L3720-3730).
        """
        goals = []
        for goal_data in json_data.get('new_goals', []):
            goals.append({
                'id': goal_data.get('goal_id', f"ai_goal_{int(time.time())}"),
                'title': goal_data.get('title', 'AI-generated goal'),
                'description': goal_data.get('description', ''),
                'goal_type': goal_data.get('goal_type', 'ai_generated'),
                'status': 'pending',
                'priority': goal_data.get('priority_score', 0.5),
                'created_at': time.time(),
                'ai_generated': True
            })
        return goals
    
    def _extract_goals_fallback(self, text: str) -> List[Dict]:
        """Regex fallback for goal extraction when JSON parsing fails."""
        goals = []
        # Try to find goal-like structures in text
        title_match = re.findall(r'"title"\s*:\s*"([^"]+)"', text)
        desc_match = re.findall(r'"description"\s*:\s*"([^"]+)"', text)
        
        for i, title in enumerate(title_match[:3]):
            goals.append({
                'id': f"ai_goal_fallback_{int(time.time())}_{i}",
                'title': title,
                'description': desc_match[i] if i < len(desc_match) else title,
                'goal_type': 'ai_generated',
                'status': 'pending',
                'priority': 0.5,
                'created_at': time.time(),
                'ai_generated': True
            })
        return goals
    
    # ================================================================
    # META-DECISION FALLBACK (replaces _extract_fallback_from_malformed_response)
    # ================================================================
    
    def _extract_primary_focus_fallback(self, text: str) -> Optional[str]:
        """Regex fallback for primary_focus when JSON parsing fails."""
        valid_focuses = [
            'system_monitoring', 'reflection', 'chain_processing',
            'learning', 'creation', 'brain_system', 'evolution_loop',
            'consciousness_core'
        ]
        # Try regex
        match = re.search(r'primary_focus["\s:]+([^"\s,}]+)', text)
        if match:
            focus = match.group(1).strip('"').strip()
            if focus in valid_focuses:
                return focus
        # Try keyword matching
        text_lower = text.lower()
        for focus in valid_focuses:
            if focus in text_lower:
                return focus
        return 'system_monitoring'  # safe default
    
    def _extract_attention_fallback(self, text: str) -> Optional[Dict]:
        """Regex fallback for attention_allocation when JSON parsing fails."""
        allocation = {}
        for subsystem in ['evolution_loop', 'brain_system', 'consciousness_core']:
            match = re.search(rf'{subsystem}["\s:]+([0-9.]+)', text)
            if match:
                try:
                    allocation[subsystem] = float(match.group(1))
                except ValueError:
                    allocation[subsystem] = 0.3
            else:
                allocation[subsystem] = 0.3
        return allocation if allocation else None
    
    # ================================================================
    # DIRECTIVE EXTRACTION (natural language actions)
    # ================================================================
    
    def _extract_directives(self, text: str) -> List[str]:
        """Extract action directives from natural language AI output."""
        directives = []
        patterns = [
            r'(?:I will|I plan to|I want to|I need to|Let me|I should)\s+([^.!?\n]{10,100})',
        ]
        for pattern in patterns:
            directives.extend(re.findall(pattern, text, re.IGNORECASE))
        return directives[:5]  # Cap to avoid noise
    
    # ================================================================
    # ACTION METHODS (caller-invoked, not automatic)
    # ================================================================
    
    def execute_tool_calls(self, result: AIOutputResult, brain_system=None) -> AIOutputResult:
        """Execute detected tool calls via the tool interface.
        
        Caller must invoke this explicitly — detection is automatic, execution is not.
        This ensures tools only run when the caller's context is appropriate
        (e.g., chain steps, not meta-decisions).
        """
        bs = brain_system or self.brain_system
        if not result.tool_calls or not bs:
            return result
        
        try:
            # Lazy import to avoid circular dependency
            from repryntt.tools.tool_interface import parse_and_execute_tool_calls
            
            # Reconstruct TOOL_CALL text for the existing executor
            tool_text = '\n'.join(
                f"TOOL_CALL: {json.dumps(tc)}" for tc in result.tool_calls
            )
            tool_output = parse_and_execute_tool_calls(tool_text, conversation_id="output_processor", brain=bs)
            if tool_output:
                result.tool_results = tool_output.get('tool_calls_executed', [])
                result.tools_executed = len(result.tool_results) > 0
                logger.info(f"🔧 Executed {len(result.tool_results)} tool calls via output processor")
        except Exception as e:
            logger.warning(f"⚠️ Tool execution failed: {e}")
        
        return result
    
    def queue_goals(self, result: AIOutputResult, brain_system=None) -> AIOutputResult:
        """Queue detected goals to the chain-of-thought queue.
        
        Replaces the goal→queue bridge in consciousness_daemon.
        Caller must invoke explicitly after checking result.goals.
        """
        bs = brain_system or self.brain_system
        if not result.goals or not bs:
            return result
        
        queued_count = 0
        for goal in sorted(result.goals, key=lambda g: g.get('priority', 0.5), reverse=True)[:2]:
            if goal.get('status') == 'pending':
                try:
                    topic = goal.get('title', 'AI exploration')
                    description = goal.get('description', topic)
                    priority = int(goal.get('priority', 0.5) * 10)
                    
                    bs.queue_chain_of_thought(
                        topic=topic,
                        goal=description,
                        priority=priority,
                        requested_by="output_processor"
                    )
                    goal['status'] = 'queued'
                    queued_count += 1
                    logger.info(f"🔗 Output processor queued goal: '{topic}' (priority: {priority})")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to queue goal: {e}")
        
        if queued_count:
            logger.info(f"📋 Output processor queued {queued_count} goals to chain queue")
        
        return result
