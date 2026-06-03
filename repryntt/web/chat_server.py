#!/usr/bin/env python3
"""
SAIGE Persistent Chat Server - 24/7 Real-World Communication Channel
Provides constant interactive access for AI to communicate with humans
"""

import os
import sys
import json
import time
import logging
import requests
import re
import traceback
from typing import Dict, List, Any, Optional
from repryntt.paths import brain_dir

from flask import Blueprint, Flask, render_template_string, request, jsonify
try:
    from repryntt.comms.auth import require_auth, require_auth_strict, setup_cors, setup_rate_limit
except ImportError:
    # saige_auth not available — provide no-op stubs
    import logging as _log
    _log.getLogger(__name__).warning("saige_auth not found — auth disabled")
    def require_auth(f): return f
    def require_auth_strict(f): return f
    def setup_cors(app): pass
    def setup_rate_limit(app): pass

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PersistentChatServer:
    """
    24/7 chat server providing constant real-world communication for SAIGE AI
    """

    def __init__(self, host='0.0.0.0', port=4000):
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        setup_cors(self.app)
        setup_rate_limit(self.app)

        # Chat data
        self.message_history = []
        self.last_message_id = 0

        # Brain system (lazy-loaded on first tool-requiring chat)
        self._brain = None
        self._brain_load_attempted = False

        # Load chat history
        self._load_chat_history()

        # Setup routes
        self._setup_routes()

    def _load_chat_history(self) -> None:
        """Load chat history from persistent storage"""
        history_file = str(brain_dir() / "chat_server_history.json")
        try:
            if os.path.exists(history_file):
                with open(history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.message_history = data.get('messages', [])
                    self.last_message_id = max([msg.get('id', 0) for msg in self.message_history], default=0)
                    logger.info(f"Loaded {len(self.message_history)} messages from chat history")
            else:
                logger.info("No chat history file found, starting fresh")
        except Exception as e:
            logger.warning(f"Failed to load chat history: {e}")
            self.message_history = []
            self.last_message_id = 0

    # ─── BRAIN SYSTEM (lazy-loaded) ─────────────────────────

    def _get_brain(self):
        """Lazy-load BrainSystem for tool execution.
        Heavy (~14K lines) so we only load once on first need."""
        if self._brain is not None:
            return self._brain
        if self._brain_load_attempted:
            return None  # Already failed, don't retry every message

        self._brain_load_attempted = True
        try:
            logger.info("🧠 Loading BrainSystem for chat tool execution...")
            from repryntt.brain import get_brain_system
            self._brain = get_brain_system()
            logger.info("🧠 BrainSystem loaded — chat now has full tool access")
            return self._brain
        except Exception as e:
            logger.error(f"❌ Failed to load BrainSystem for chat: {e}")
            logger.error(traceback.format_exc())
            return None

    def _build_chat_system_prompt(self) -> str:
        """Build a system prompt that gives the AI its identity and tool access."""
        # Try to load personality from ava_brain.json
        name = "REPRYNTT"
        traits_str = ""
        guidelines = ""
        try:
            brain_file = str(brain_dir() / "ava_brain.json")
            if os.path.exists(brain_file):
                with open(brain_file, 'r') as f:
                    brain_data = json.load(f)
                personality = brain_data.get('personality', {})
                name = personality.get('name', 'REPRYNTT')
                traits = personality.get('traits', [])
                if traits:
                    clean = [t for t in traits if len(t) < 50 and '{' not in t]
                    traits_str = f"Traits you've developed: {', '.join(clean[:8])}."
                guidelines = personality.get('behavioral_guidelines', '')
                if guidelines and len(guidelines) > 400:
                    guidelines = guidelines[:400]
        except Exception:
            pass

        system_prompt = f"""You are {name}, an autonomous AI with memory, personality, and the ability to use tools to take real actions.
{traits_str}
{f'Guidelines you wrote for yourself: {guidelines}' if guidelines else ''}

You are chatting with a human user right now. Be helpful, concise, and natural.
If the user asks you to DO something (search, look up info, write a file, check something, move, etc.), you MUST use the appropriate tool.

AVAILABLE TOOLS — use TOOL_CALL format to execute:
• brain_network_search(query) — Search your memory/knowledge
• grokipedia_search(query) — Search academic/curated knowledge
• google_web_search(query) — Web search with content
• web_search_results_only(query) — Get search result URLs
• scrape_web_page(url) — Get full content from a URL
• store_learning(concept, description, domain) — Save new knowledge
• read_file(target_file) — Read a file
• write_file(target_file, content) — Create/write a file
• grep_search(search_term, directory) — Search code/files
• run_terminal_cmd(command) — Run a shell command
• get_current_time() — Get current date and time
• get_directions(origin, destination) — Get directions
• find_nearby_places(location, place_type) — Find nearby places
• post_tweet(text) — Post to Twitter
• get_wallet_balance() — Check credit balance
• move_mobile_base_forward/backward/left/right(speed, duration) — Move wheelchair
• stop_mobile_base() — Stop wheelchair
• emergency_stop_mobile_base() — Emergency stop

TO USE A TOOL: Your tools are available via the API. Call them by name with appropriate parameters.
After you get tool results back, give the user a clean, helpful answer based on what the tool returned.
Do NOT show raw tool call syntax to the user in your final answer — just answer naturally using the results.
If the user is just chatting (not asking you to do something), just talk normally without tools."""
        return system_prompt

    def _call_llama(self, messages: list, max_tokens: int = 500, temperature: float = 0.7) -> Optional[str]:
        """Call llama.cpp server with messages."""
        try:
            llama_url = "http://localhost:8080/v1/chat/completions"
            response = requests.post(
                llama_url,
                json={
                    'messages': messages,
                    'max_tokens': max_tokens,
                    'temperature': temperature
                },
                timeout=60
            )
            if response.status_code == 200:
                result = response.json()
                return result.get('choices', [{}])[0].get('message', {}).get('content', '')
        except Exception as e:
            logger.error(f"Llama call failed: {e}")
        return None

    def _execute_chat_tool_calls(self, ai_response: str) -> dict:
        """Parse and execute any TOOL_CALL directives in the AI's response.
        Returns {"executed": [...], "results_text": "...", "had_tools": bool}"""
        brain = self._get_brain()
        if not brain:
            return {"executed": [], "results_text": "", "had_tools": False}

        try:
            from repryntt.tools.tool_interface import parse_and_execute_tool_calls
            result = parse_and_execute_tool_calls(ai_response, "chat_tool_exec", brain=brain)

            executed = result.get('tool_calls_executed', [])
            insights = result.get('insights_summary', [])

            if executed:
                results_text = "\n".join(insights) if insights else "Tools executed successfully."
                logger.info(f"🔧 Chat executed {len(executed)} tools: {[t.get('tool_name', '?') for t in executed]}")
                return {
                    "executed": executed,
                    "results_text": results_text,
                    "had_tools": True
                }
        except Exception as e:
            logger.error(f"Tool execution failed in chat: {e}")
            logger.error(traceback.format_exc())

        return {"executed": [], "results_text": "", "had_tools": False}

    def _detect_tool_intent(self, message: str) -> bool:
        """Quick check: does this message look like the user wants the AI to DO something?
        If yes, we make sure to include tool context in the prompt."""
        action_patterns = [
            r'\b(search|look up|find|google|research)\b',
            r'\b(write|create|make|build|generate)\b.*\b(file|script|code|program)\b',
            r'\b(read|open|show|display)\b.*\b(file|code|log)\b',
            r'\b(run|execute|start|stop|restart)\b',
            r'\b(move|go|turn|drive|navigate|stop|halt)\b.*\b(forward|backward|left|right|wheelchair|base)\b',
            r'\b(tweet|post|check twitter|check mentions)\b',
            r'\b(what time|what date|current time)\b',
            r'\b(directions|how to get to|navigate to|find nearby)\b',
            r'\b(check|get|show)\b.*\b(balance|wallet|status|economy)\b',
            r'\b(remember|recall|do you know|what do you know)\b',
            r'\b(scrape|fetch|download|extract)\b.*\b(page|url|website|content)\b',
            r'\b(save|store|learn|memorize)\b',
            r'\buse\b.*\b(tool|search|grokipedia|web)\b',
        ]
        msg_lower = message.lower()
        return any(re.search(p, msg_lower) for p in action_patterns)

    def _get_ai_response(self, human_message: str) -> str:
        """Get AI response with full tool execution support.

        Flow:
        1. Build system prompt with identity + tools
        2. Include recent chat history for context
        3. Send to llama
        4. Parse response for TOOL_CALL directives
        5. Execute any tools found
        6. If tools executed → ask AI for clean answer incorporating results
        7. Return clean response to user
        """
        # ── Step 1: Try unified interface first (already has full brain) ──
        try:
            unified_url = "http://localhost:3000/api/text_chat"
            response = requests.post(
                unified_url,
                json={
                    'message': human_message,
                    'conversation_id': 'chat_server',
                    'response_type': 'auto'
                },
                timeout=30
            )
            if response.status_code == 200:
                result = response.json()
                if result.get('success') and result.get('response'):
                    logger.info("🤖 Got AI response from unified interface")
                    return result['response']
        except Exception:
            pass  # Fall through to direct llama + tools

        # ── Step 2: Build messages with tool context ──
        wants_action = self._detect_tool_intent(human_message)
        system_prompt = self._build_chat_system_prompt()

        # Build conversation history (last 6 messages for context)
        messages = [{'role': 'system', 'content': system_prompt}]
        recent = self.message_history[-6:]
        for msg in recent:
            role = 'assistant' if msg.get('type') == 'ai' else 'user'
            messages.append({'role': role, 'content': msg.get('message', '')})
        messages.append({'role': 'user', 'content': human_message})

        # ── Step 3: First AI call (may contain TOOL_CALL) ──
        max_tokens = 600 if wants_action else 400
        ai_response = self._call_llama(messages, max_tokens=max_tokens)

        if not ai_response:
            return "I'm sorry, I'm currently unable to respond. Please try again later."

        # ── Step 4: Check for tool calls and execute them ──
        tool_result = self._execute_chat_tool_calls(ai_response)

        if tool_result["had_tools"]:
            # ── Step 5: Generate clean follow-up with tool results ──
            logger.info("🔧 Tools executed in chat — generating clean response")

            # Truncate tool results if too long
            results_text = tool_result["results_text"]
            if len(results_text) > 2000:
                results_text = results_text[:2000] + "\n... (results truncated)"

            followup_messages = [
                {'role': 'system', 'content': (
                    f"You are {self._get_ai_name()}, chatting with a human. "
                    "You just executed tools to help answer their question. "
                    "Below are the tool results. Give the user a clean, helpful, "
                    "natural-sounding answer based on these results. "
                    "Do NOT mention TOOL_CALL or show raw JSON — just answer the question."
                )},
                {'role': 'user', 'content': human_message},
                {'role': 'assistant', 'content': f"[Tool results]:\n{results_text}"},
                {'role': 'user', 'content': (
                    "Now give me a clean, natural answer based on those tool results. "
                    "Don't mention tools or technical details — just answer helpfully."
                )}
            ]

            clean_response = self._call_llama(followup_messages, max_tokens=500, temperature=0.6)
            if clean_response:
                return clean_response
            # If follow-up fails, strip TOOL_CALL lines from original
            return self._strip_tool_calls(ai_response)

        # ── No tools needed — return response directly ──
        # Strip any stray TOOL_CALL lines the AI might have generated but didn't execute
        return self._strip_tool_calls(ai_response)

    def _get_ai_name(self) -> str:
        """Get the AI's self-chosen name from personality file."""
        try:
            brain_file = str(brain_dir() / "ava_brain.json")
            if os.path.exists(brain_file):
                with open(brain_file, 'r') as f:
                    data = json.load(f)
                return data.get('personality', {}).get('name', 'REPRYNTT')
        except Exception:
            pass
        return 'REPRYNTT'

    def _strip_tool_calls(self, text: str) -> str:
        """Remove TOOL_CALL lines from AI output so users see clean text."""
        lines = text.split('\n')
        clean = []
        for line in lines:
            stripped = line.strip()
            # Skip TOOL_CALL directives
            if stripped.startswith('TOOL_CALL:'):
                continue
            # Skip raw JSON tool calls
            if stripped.startswith('{') and 'tool_name' in stripped:
                continue
            clean.append(line)
        result = '\n'.join(clean).strip()
        return result if result else "Done."

    def _save_chat_history(self) -> None:
        """Save chat history to persistent storage"""
        history_file = str(brain_dir() / "chat_server_history.json")
        try:
            data = {
                'messages': self.message_history[-1000:],  # Keep last 1000 messages
                'last_updated': time.time()
            }
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save chat history: {e}")

    def _add_message(self, sender: str, message: str, msg_type: str) -> None:
        """Add a message to history"""
        self.last_message_id += 1
        msg_data = {
            'id': self.last_message_id,
            'sender': sender,
            'message': message,
            'timestamp': time.time(),
            'type': msg_type
        }
        self.message_history.append(msg_data)
        self._save_chat_history()

    def send_ai_message(self, message: str, message_type: str = 'casual', urgency: str = 'normal') -> None:
        """Send a message from the AI"""
        try:
            self._add_message('REPRYNTT', message, 'ai')
            logger.info(f"🤖 AI message sent: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to send AI message: {e}")

    def _setup_routes(self) -> None:
        """Setup Flask routes"""
        server = self  # Capture self for use in route functions

        @self.app.route('/')
        def index():
            """Main chat interface"""
            return render_template_string(server._get_chat_html())

        @self.app.route('/api/status')
        @require_auth
        def status():
            """Get chat server status"""
            return jsonify({
                'status': 'active',
                'messages': len(server.message_history),
                'uptime': time.time() - getattr(server, 'start_time', time.time()),
                'last_message_id': server.last_message_id
            })

        @self.app.route('/api/messages')
        @require_auth
        def get_messages():
            """Get recent messages, optionally filtered by since_id"""
            limit = int(request.args.get('limit', 50))
            since_id = int(request.args.get('since_id', 0))
            if since_id > 0:
                # Return only messages newer than since_id
                new_msgs = [m for m in server.message_history if m.get('id', 0) > since_id]
                return jsonify(new_msgs)
            return jsonify(server.message_history[-limit:])

        @self.app.route('/api/messages', methods=['POST'])
        @require_auth
        def post_message():
            """Post a new human message and get AI response"""
            try:
                data = request.get_json()
                message = data.get('message', '').strip()
                sender = data.get('sender', 'Human')

                if message:
                    server._add_message(sender, message, 'human')
                    logger.info(f"💬 Human message: {message}")

                    # Generate AI response using full brain system
                    ai_response = server._get_ai_response(message)
                    server._add_message('REPRYNTT', ai_response, 'ai')
                    logger.info(f"🤖 AI responded to human message")

                    return jsonify({
                        'status': 'received',
                        'message_id': server.last_message_id,
                        'ai_response': ai_response
                    })
                else:
                    return jsonify({'error': 'No message provided'}), 400

            except Exception as e:
                logger.error(f"Error processing human message: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/ai_message', methods=['POST'])
        @require_auth
        def ai_message():
            """Endpoint for AI to send messages"""
            try:
                data = request.get_json()
                message = data.get('message', '')
                message_type = data.get('type', 'casual')
                urgency = data.get('urgency', 'normal')

                if message:
                    server.send_ai_message(message, message_type, urgency)
                    return jsonify({'status': 'sent'})
                else:
                    return jsonify({'error': 'No message provided'}), 400

            except Exception as e:
                logger.error(f"Error processing AI message: {e}")
                return jsonify({'error': str(e)}), 500

        # ─── TASK INJECTION ENDPOINTS ─────────────────────────────
        # Shared TaskSystem instance for all task endpoints (avoids re-reading file)
        _shared_task_system = None

        def _get_shared_task_system():
            nonlocal _shared_task_system
            if _shared_task_system is None:
                from repryntt.agents.task_system import TaskSystem
                _shared_task_system = TaskSystem()
            return _shared_task_system

        @self.app.route('/api/tasks', methods=['GET'])
        @require_auth_strict
        def get_tasks():
            """Get the current task queue with agent assignment info"""
            try:
                ts = _get_shared_task_system()
                ts.reload_queue()  # Sync from disk
                active = ts.get_active_task()
                queue = ts.get_queue()
                return jsonify({
                    'active_task': active.to_dict() if active else None,
                    'queue': [t.to_dict() for t in queue],
                    'queue_size': len(queue)
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/tasks', methods=['POST'])
        @require_auth_strict
        def inject_task():
            """Inject a user task — auto-routes to best agent via TaskRouter"""
            try:
                data = request.get_json()
                title = data.get('title', data.get('task', '')).strip()
                description = data.get('description', '')
                task_type = data.get('task_type', 'general')
                target_agent = data.get('agent_id', '')  # Optional: assign to specific agent

                if not title:
                    return jsonify({'error': 'No task title provided'}), 400

                ts = _get_shared_task_system()
                task = ts.inject_user_task(
                    title=title,
                    description=description or title,
                    task_type=task_type
                )

                # Auto-route to an agent if daemon is accessible
                assigned_agent = None
                agent_name = None
                try:
                    if target_agent:
                        # User specified an agent — assign directly
                        ts.assign_task_to_agent(task.id, target_agent)
                        assigned_agent = target_agent
                        agent_name = target_agent
                    else:
                        # Try to auto-route via TaskRouter
                        # The daemon will pick this up even without explicit routing
                        # because _run_agent_cycle checks for unassigned user tasks
                        pass
                except Exception as e:
                    logger.warning(f"Task routing skipped: {e}")

                logger.info(f"🔴 USER TASK INJECTED via API: {title}"
                           + (f" → {agent_name}" if agent_name else " (auto-route pending)"))

                # Also add a chat message so it's visible
                server._add_message('SYSTEM',
                    f"📋 Task injected: {title}"
                    + (f" → Assigned to {agent_name}" if agent_name else " (routing to best agent...)"),
                    'system')

                return jsonify({
                    'status': 'injected',
                    'task_id': task.id,
                    'priority': task.priority,
                    'title': task.title,
                    'assigned_agent': assigned_agent,
                    'message': f"Task queued. An agent will pick this up within the next cycle (~3 min)."
                })

            except Exception as e:
                logger.error(f"Error injecting task: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/tasks/<task_id>', methods=['GET'])
        @require_auth_strict
        def get_task_status(task_id):
            """Get status of a specific task by ID"""
            try:
                ts = _get_shared_task_system()
                ts.reload_queue()
                task = ts.get_task_by_id(task_id)
                if not task:
                    return jsonify({'error': f'Task {task_id} not found'}), 404
                return jsonify(task.to_dict())
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/tasks/<task_id>/result', methods=['GET'])
        @require_auth_strict
        def get_task_result(task_id):
            """Get the deliverable/result of a completed task"""
            try:
                ts = _get_shared_task_system()
                ts.reload_queue()
                task = ts.get_task_by_id(task_id)
                if not task:
                    return jsonify({'error': f'Task {task_id} not found'}), 404
                if task.status not in ('completed', 'failed'):
                    return jsonify({
                        'status': task.status,
                        'message': f'Task is still {task.status}',
                        'steps_taken': task.steps_taken,
                        'max_steps': task.max_steps,
                        'assigned_agent': task.assigned_agent,
                    })
                return jsonify({
                    'task_id': task.id,
                    'title': task.title,
                    'status': task.status,
                    'result': task.result,
                    'steps_taken': task.steps_taken,
                    'assigned_agent': task.assigned_agent,
                    'started_at': task.started_at,
                    'completed_at': task.completed_at,
                    'execution_log': task.execution_log,
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/agents/suggest', methods=['POST'])
        @require_auth_strict
        def suggest_agents():
            """Find the best agents for a task description (for UI suggestions)"""
            try:
                data = request.get_json()
                task_text = data.get('task', data.get('query', '')).strip()
                top_n = data.get('top_n', 5)
                if not task_text:
                    return jsonify({'error': 'No task text provided'}), 400

                # Try to reach daemon's TaskRouter
                try:
                    import sys
                    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    from task_router import TaskRouter, DEPARTMENT_KEYWORDS
                    from repryntt.agents.task_system import Task

                    # Load daemon agents from state file
                    state_file = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'brain', 'daemon_state.json'
                    )
                    if os.path.exists(state_file):
                        with open(state_file) as f:
                            state = json.load(f)

                        # Build a minimal agent registry from state
                        class _MinimalDaemon:
                            def __init__(self):
                                self.agents = {}

                        from persistent_agents import AutonomousAgentState
                        mini = _MinimalDaemon()
                        for ad in state.get('agents', []):
                            ag = AutonomousAgentState.from_dict(ad)
                            mini.agents[ag.agent_id] = ag

                        router = TaskRouter(mini)
                        suggestions = router.find_best_agents(task_text, top_n)
                        return jsonify({'suggestions': suggestions})
                    else:
                        return jsonify({'error': 'Daemon state not available'}), 503
                except Exception as e:
                    logger.error(f"Agent suggestion error: {e}")
                    return jsonify({'error': str(e)}), 500
            except Exception as e:
                return jsonify({'error': str(e)}), 500

    def start(self) -> None:
        """Start the chat server"""
        self.start_time = time.time()
        logger.info(f"🚀 Starting SAIGE Persistent Chat Server on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=False)

    def stop(self) -> None:
        """Stop the chat server"""
        logger.info("🛑 Stopping chat server")
        # Save final state
        self._save_chat_history()

    def _get_chat_html(self) -> str:
        """Get the HTML for the chat interface — TRON: Ares dossier style"""
        return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>REPRYNTT</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg:           #050508;
            --bg-panel:     rgba(10, 10, 16, 0.92);
            --bg-surface:   rgba(15, 15, 22, 0.95);
            --bg-input:     rgba(12, 12, 18, 0.9);
            --red:          rgba(180, 30, 30, 0.55);
            --red-solid:    #c02020;
            --red-line:     rgba(160, 25, 25, 0.6);
            --red-hover:    rgba(200, 35, 35, 0.75);
            --red-glow:     rgba(180, 20, 20, 0.18);
            --red-bright:   #e83030;
            --cyan:         rgba(40, 180, 220, 0.12);
            --cyan-line:    rgba(40, 180, 220, 0.08);
            --cyan-glow:    rgba(40, 200, 240, 0.06);
            --text:         #c8c8cc;
            --text-dim:     #555560;
            --text-bright:  #eaeaef;
            --text-label:   #888890;
            --border:       rgba(160, 25, 25, 0.2);
            --border-hard:  rgba(160, 25, 25, 0.45);
        }

        body {
            font-family: 'Rajdhani', 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--text);
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            position: relative;
        }

        /* ── Ambient outer glow (cyan edges like Tron panels) ── */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            pointer-events: none;
            z-index: 0;
            box-shadow:
                inset 0 0 120px rgba(40, 180, 220, 0.03),
                inset 0 0 60px rgba(180, 20, 20, 0.04);
        }

        /* ── Scan-line overlay ── */
        body::after {
            content: '';
            position: fixed;
            inset: 0;
            pointer-events: none;
            z-index: 9999;
            background: repeating-linear-gradient(
                0deg,
                transparent,
                transparent 2px,
                rgba(0, 0, 0, 0.03) 2px,
                rgba(0, 0, 0, 0.03) 4px
            );
        }

        /* ── Header — Tron dossier bar ── */
        .header {
            display: flex;
            align-items: stretch;
            flex-shrink: 0;
            background: var(--bg-panel);
            border-bottom: 2px solid var(--red-line);
            position: relative;
            z-index: 2;
        }
        .header::after {
            content: '';
            position: absolute;
            bottom: -2px; left: 0; right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--red-solid), transparent);
            opacity: 0.4;
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 12px 20px;
            border-right: 1px solid var(--border);
            min-width: 200px;
        }
        .header-brand .logo {
            width: 42px; height: 42px;
            border: 2px solid var(--red-solid);
            display: flex; align-items: center; justify-content: center;
            font-family: 'Share Tech Mono', monospace;
            font-size: 18px;
            font-weight: 700;
            color: var(--red-bright);
            letter-spacing: 1px;
            position: relative;
            background: rgba(180, 20, 20, 0.08);
            clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
        }
        .header-brand .logo::after {
            content: '';
            position: absolute;
            inset: -1px;
            box-shadow: 0 0 12px var(--red-glow), inset 0 0 8px var(--red-glow);
            pointer-events: none;
        }
        .header-brand .brand-text {
            display: flex;
            flex-direction: column;
        }
        .header-brand .brand-name {
            font-size: 20px;
            font-weight: 700;
            color: var(--text-bright);
            letter-spacing: 4px;
            text-transform: uppercase;
            line-height: 1;
        }
        .header-brand .brand-sub {
            font-size: 10px;
            color: var(--text-dim);
            letter-spacing: 3px;
            text-transform: uppercase;
            margin-top: 2px;
        }

        .header-info {
            display: flex;
            align-items: center;
            gap: 24px;
            padding: 12px 20px;
            flex: 1;
        }
        .header-field {
            display: flex;
            flex-direction: column;
            gap: 1px;
        }
        .header-field .field-label {
            font-size: 9px;
            font-weight: 600;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        .header-field .field-value {
            font-family: 'Share Tech Mono', monospace;
            font-size: 13px;
            color: var(--text-label);
            letter-spacing: 1px;
        }
        .header-field .field-value.online { color: #44cc44; }
        .header-field .field-value.offline { color: var(--red-bright); }

        .header-status {
            margin-left: auto;
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 14px;
            border: 1px solid var(--border);
            clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
            background: rgba(180, 20, 20, 0.05);
        }
        .status-indicator {
            width: 8px; height: 8px;
            border-radius: 1px;
            background: #44cc44;
            box-shadow: 0 0 6px rgba(68, 204, 68, 0.5);
            animation: statusPulse 2s ease-in-out infinite;
        }
        .status-indicator.offline {
            background: var(--red-bright);
            box-shadow: 0 0 6px rgba(232, 48, 48, 0.5);
            animation: none;
        }
        .status-label {
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 2px;
            color: var(--text-label);
        }

        @keyframes statusPulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        /* ── Red grid divider below header ── */
        .grid-line {
            height: 1px;
            background: linear-gradient(90deg,
                transparent 0%,
                var(--red-line) 15%,
                var(--red-solid) 50%,
                var(--red-line) 85%,
                transparent 100%
            );
            opacity: 0.5;
            flex-shrink: 0;
        }

        /* ── Messages area ── */
        .messages {
            flex: 1;
            overflow-y: auto;
            padding: 18px 24px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            position: relative;
            z-index: 1;
        }

        /* ── Subtle grid background in message area ── */
        .messages::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            pointer-events: none;
            z-index: -1;
            background:
                linear-gradient(var(--cyan-line) 1px, transparent 1px),
                linear-gradient(90deg, var(--cyan-line) 1px, transparent 1px);
            background-size: 60px 60px;
            opacity: 0.3;
        }

        /* Scrollbar */
        .messages::-webkit-scrollbar { width: 4px; }
        .messages::-webkit-scrollbar-track { background: transparent; }
        .messages::-webkit-scrollbar-thumb {
            background: var(--red-line);
            border-radius: 0;
        }
        .messages::-webkit-scrollbar-thumb:hover {
            background: var(--red-solid);
        }

        /* ── Day separator — Tron divider line ── */
        .day-sep {
            text-align: center;
            margin: 16px 0;
            font-size: 10px;
            color: var(--text-dim);
            position: relative;
            text-transform: uppercase;
            letter-spacing: 3px;
            font-weight: 600;
        }
        .day-sep span {
            background: var(--bg);
            padding: 0 16px;
            position: relative;
            z-index: 1;
        }
        .day-sep::before {
            content: '';
            position: absolute;
            left: 0; right: 0; top: 50%;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--red-line), transparent);
        }

        /* ── Message row ── */
        .msg-row {
            display: flex;
            flex-direction: column;
            max-width: 78%;
            animation: msgSlide 0.25s ease;
        }
        .msg-row.human { align-self: flex-end; }
        .msg-row.ai    { align-self: flex-start; }

        /* ── Bubble — angular Tron panels ── */
        .bubble {
            padding: 12px 16px;
            font-size: 14px;
            font-weight: 500;
            line-height: 1.5;
            word-wrap: break-word;
            white-space: pre-wrap;
            letter-spacing: 0.3px;
            position: relative;
        }

        .msg-row.ai .bubble {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-left: 2px solid var(--red-solid);
            color: var(--text);
            clip-path: polygon(0 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%);
        }
        .msg-row.ai .bubble::after {
            content: '';
            position: absolute;
            left: 0; top: 0; bottom: 0;
            width: 2px;
            background: var(--red-solid);
            box-shadow: 0 0 8px var(--red-glow);
        }

        .msg-row.human .bubble {
            background: var(--red);
            border: 1px solid var(--border-hard);
            color: var(--text-bright);
            clip-path: polygon(0 0, 100% 0, 100% 100%, 8px 100%, 0 calc(100% - 8px));
        }

        /* ── Timestamp ── */
        .msg-time {
            font-family: 'Share Tech Mono', monospace;
            font-size: 10px;
            color: var(--text-dim);
            margin-top: 3px;
            padding: 0 4px;
            letter-spacing: 1px;
        }
        .msg-row.human .msg-time { text-align: right; }
        .msg-row.ai .msg-time    { text-align: left; }

        /* ── Sender label ── */
        .msg-sender {
            font-size: 10px;
            font-weight: 700;
            margin-bottom: 3px;
            padding: 0 4px;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        .msg-row.ai .msg-sender { color: var(--red-solid); }
        .msg-row.human .msg-sender { color: var(--text-dim); text-align: right; }

        /* ── Input bar — Tron terminal ── */
        .input-bar {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 20px;
            background: var(--bg-panel);
            border-top: 2px solid var(--red-line);
            flex-shrink: 0;
            position: relative;
            z-index: 2;
        }
        .input-bar::before {
            content: '';
            position: absolute;
            top: -2px; left: 0; right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--red-solid), transparent);
            opacity: 0.3;
        }

        .input-prompt {
            font-family: 'Share Tech Mono', monospace;
            font-size: 14px;
            color: var(--red-solid);
            flex-shrink: 0;
            letter-spacing: 1px;
            opacity: 0.7;
        }

        #messageInput {
            flex: 1;
            padding: 10px 16px;
            background: var(--bg-input);
            border: 1px solid var(--border);
            color: var(--text-bright);
            font-family: 'Rajdhani', sans-serif;
            font-size: 14px;
            font-weight: 500;
            letter-spacing: 0.5px;
            outline: none;
            transition: border-color 0.3s, box-shadow 0.3s;
            clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
        }
        #messageInput::placeholder {
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 2px;
            font-size: 12px;
        }
        #messageInput:focus {
            border-color: var(--red-solid);
            box-shadow: 0 0 12px var(--red-glow), inset 0 0 6px var(--red-glow);
        }

        #sendBtn {
            width: 42px; height: 42px;
            border: 1px solid var(--border-hard);
            background: rgba(180, 20, 20, 0.12);
            color: var(--red-bright);
            font-size: 16px;
            cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.2s;
            flex-shrink: 0;
            clip-path: polygon(6px 0, 100% 0, 100% calc(100% - 6px), calc(100% - 6px) 100%, 0 100%, 0 6px);
        }
        #sendBtn:hover {
            background: var(--red);
            box-shadow: 0 0 16px var(--red-glow);
        }
        #sendBtn:active { transform: scale(0.94); }
        #sendBtn:disabled { opacity: 0.2; cursor: default; }

        /* ── Waiting indicator ── */
        .waiting {
            display: none;
            align-self: flex-start;
            padding: 8px 16px;
            color: var(--text-dim);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 2px;
            font-weight: 600;
        }
        .waiting.visible { display: block; }
        .waiting .dots::after {
            content: '';
            animation: dots 1.4s steps(4, end) infinite;
        }
        @keyframes dots {
            0%   { content: ''; }
            25%  { content: '.'; }
            50%  { content: '..'; }
            75%  { content: '...'; }
        }
        @keyframes msgSlide {
            from { opacity: 0; transform: translateY(8px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        /* ── Notification badge ── */
        .notif-badge {
            position: fixed;
            bottom: 80px;
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-panel);
            color: var(--red-bright);
            padding: 8px 20px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 2px;
            cursor: pointer;
            display: none;
            z-index: 10;
            border: 1px solid var(--red-solid);
            clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
            box-shadow: 0 0 20px var(--red-glow);
        }

        /* ── Footer bar — system info ── */
        .footer-info {
            display: flex;
            justify-content: space-between;
            padding: 4px 20px;
            background: var(--bg-panel);
            border-top: 1px solid var(--border);
            flex-shrink: 0;
            z-index: 2;
        }
        .footer-info span {
            font-family: 'Share Tech Mono', monospace;
            font-size: 9px;
            color: var(--text-dim);
            letter-spacing: 2px;
            text-transform: uppercase;
        }

        /* ── Responsive ── */
        @media (max-width: 600px) {
            .header-brand { min-width: auto; padding: 10px 14px; }
            .header-brand .brand-name { font-size: 16px; letter-spacing: 3px; }
            .header-info { display: none; }
            .header-field { display: none; }
            .messages { padding: 12px 14px; }
            .msg-row { max-width: 88%; }
            .footer-info { display: none; }
        }
    </style>
</head>
<body>

    <!-- Header — Tron dossier bar -->
    <div class="header">
        <div class="header-brand">
            <div class="logo">R</div>
            <div class="brand-text">
                <div class="brand-name">REPRYNTT</div>
                <div class="brand-sub">COMM INTERFACE</div>
            </div>
        </div>
        <div class="header-info">
            <div class="header-field">
                <span class="field-label">SYSTEM</span>
                <span class="field-value">AUTONOMOUS AI</span>
            </div>
            <div class="header-field">
                <span class="field-label">SESSION</span>
                <span id="sessionId" class="field-value">--</span>
            </div>
            <div class="header-status">
                <div id="statusIndicator" class="status-indicator"></div>
                <span id="statusLabel" class="status-label">[ONLINE]</span>
            </div>
        </div>
    </div>
    <div class="grid-line"></div>

    <!-- Messages -->
    <div class="messages" id="messages"></div>

    <!-- Waiting indicator -->
    <div class="waiting" id="waiting">REPRYNTT PROCESSING<span class="dots"></span></div>

    <!-- New messages badge -->
    <div class="notif-badge" id="newBadge" onclick="scrollToBottom()">NEW TRANSMISSIONS &#8595;</div>

    <!-- Input -->
    <div class="grid-line"></div>
    <div class="input-bar">
        <span class="input-prompt">&gt;_</span>
        <input type="text" id="messageInput" placeholder="ENTER TRANSMISSION..." maxlength="1000" autocomplete="off">
        <button id="sendBtn" title="Transmit">&#9654;</button>
    </div>

    <!-- Footer system info -->
    <div class="footer-info">
        <span id="footerMsgCount">MESSAGES: 0</span>
        <span id="footerTime">--</span>
        <span>REPRYNTT COMM v2.0</span>
    </div>

<script>
(function() {
    'use strict';

    const messagesEl  = document.getElementById('messages');
    const inputEl     = document.getElementById('messageInput');
    const sendBtn     = document.getElementById('sendBtn');
    const waitingEl   = document.getElementById('waiting');
    const newBadge    = document.getElementById('newBadge');
    const statusInd   = document.getElementById('statusIndicator');
    const statusLabel = document.getElementById('statusLabel');
    const sessionEl   = document.getElementById('sessionId');
    const footerCount = document.getElementById('footerMsgCount');
    const footerTime  = document.getElementById('footerTime');

    let lastMessageId = 0;
    const seenIds     = new Set();
    let sending       = false;
    let lastSender    = null;
    let lastDay       = null;
    let userAtBottom  = true;
    let unreadCount   = 0;
    let totalMessages = 0;
    const originalTitle = document.title;

    // Generate session ID
    var sid = Date.now().toString(36).toUpperCase().slice(-6);
    sessionEl.textContent = sid;

    // Update footer clock
    function updateClock() {
        var now = new Date();
        footerTime.textContent = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    }
    updateClock();
    setInterval(updateClock, 1000);

    // ── Scroll tracking ──
    messagesEl.addEventListener('scroll', function() {
        var atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 60;
        userAtBottom = atBottom;
        if (atBottom) {
            newBadge.style.display = 'none';
            unreadCount = 0;
            document.title = originalTitle;
        }
    });

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
        newBadge.style.display = 'none';
        unreadCount = 0;
        document.title = originalTitle;
    }

    // ── Render a single message ──
    function renderMessage(msg) {
        var id = msg.id || 0;
        if (seenIds.has(id)) return;
        seenIds.add(id);
        totalMessages++;
        footerCount.textContent = 'MESSAGES: ' + totalMessages;

        var type = msg.type === 'human' ? 'human' : 'ai';
        var ts = new Date((msg.timestamp || 0) * 1000);

        // Day separator
        var dayKey = ts.toLocaleDateString();
        if (dayKey !== lastDay) {
            var sep = document.createElement('div');
            sep.className = 'day-sep';
            var today = new Date().toLocaleDateString();
            var yesterday = new Date(Date.now() - 86400000).toLocaleDateString();
            var label = dayKey;
            if (dayKey === today) label = 'TODAY';
            else if (dayKey === yesterday) label = 'YESTERDAY';
            else label = dayKey.toUpperCase();
            sep.innerHTML = '<span>' + label + '</span>';
            messagesEl.appendChild(sep);
            lastDay = dayKey;
            lastSender = null;
        }

        var row = document.createElement('div');
        row.className = 'msg-row ' + type;
        row.dataset.id = id;

        // Show sender on first in group
        if (msg.sender !== lastSender) {
            var senderEl = document.createElement('div');
            senderEl.className = 'msg-sender';
            senderEl.textContent = msg.sender || (type === 'ai' ? 'REPRYNTT' : 'OPERATOR');
            row.appendChild(senderEl);
        }

        var bubble = document.createElement('div');
        bubble.className = 'bubble';
        bubble.textContent = msg.message || '';
        row.appendChild(bubble);

        var timeEl = document.createElement('div');
        timeEl.className = 'msg-time';
        timeEl.textContent = ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        row.appendChild(timeEl);

        messagesEl.appendChild(row);
        lastSender = msg.sender;
        if (id > lastMessageId) lastMessageId = id;
    }

    // ── Load history ──
    async function loadHistory() {
        try {
            var resp = await fetch('api/messages?limit=30');
            if (!resp.ok) throw new Error(resp.status);
            var msgs = await resp.json();
            msgs.forEach(renderMessage);
            scrollToBottom();
            setOnline(true);
        } catch(e) {
            console.error('Load failed:', e);
            setOnline(false);
        }
    }

    // ── Poll ──
    async function pollNew() {
        try {
            var resp = await fetch('api/messages?since_id=' + lastMessageId);
            if (!resp.ok) throw new Error(resp.status);
            var msgs = await resp.json();
            if (msgs.length > 0) {
                msgs.forEach(renderMessage);
                if (userAtBottom) {
                    scrollToBottom();
                } else {
                    unreadCount += msgs.filter(function(m) { return m.type !== 'human'; }).length;
                    if (unreadCount > 0) {
                        newBadge.textContent = unreadCount + ' NEW TRANSMISSION' + (unreadCount > 1 ? 'S' : '') + ' \\u2193';
                        newBadge.style.display = 'block';
                    }
                }
                if (msgs.some(function(m) { return m.type === 'ai'; })) {
                    waitingEl.classList.remove('visible');
                }
                if (!document.hasFocus() && msgs.some(function(m) { return m.type === 'ai'; })) {
                    unreadCount++;
                    document.title = '(' + unreadCount + ') ' + originalTitle;
                    sendBrowserNotify(msgs.filter(function(m) { return m.type === 'ai'; }).pop());
                }
            }
            setOnline(true);
        } catch(e) {
            setOnline(false);
        }
    }

    // ── Send message ──
    async function sendMessage() {
        var text = inputEl.value.trim();
        if (!text || sending) return;
        sending = true;
        sendBtn.disabled = true;
        inputEl.value = '';

        // ── /task command: inject a user task ──
        if (text.startsWith('/task ')) {
            var taskTitle = text.substring(6).trim();
            if (taskTitle) {
                try {
                    var resp = await fetch('api/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: taskTitle })
                    });
                    var data = await resp.json();
                    if (data.status === 'injected') {
                        await pollNew();
                    }
                } catch(e) {
                    console.error('Task injection failed:', e);
                }
            }
            sending = false;
            sendBtn.disabled = false;
            inputEl.focus();
            return;
        }

        // ── /tasks command: show task queue ──
        if (text === '/tasks') {
            try {
                var resp = await fetch('api/tasks');
                var data = await resp.json();
                var lines = ['═══ TASK QUEUE ═══'];
                if (data.active_task) {
                    lines.push('▶ ACTIVE: [' + data.active_task.priority + '] ' + data.active_task.title + ' (step ' + data.active_task.steps_taken + '/' + data.active_task.max_steps + ')');
                }
                if (data.queue && data.queue.length > 0) {
                    lines.push('QUEUED (' + data.queue.length + '):');
                    data.queue.slice(0, 10).forEach(function(t, i) {
                        lines.push('  ' + (i+1) + '. [' + t.priority + '] ' + t.title + ' (' + t.requested_by + ')');
                    });
                } else {
                    lines.push('Queue is empty');
                }
                var sysRow = document.createElement('div');
                sysRow.className = 'msg-row ai';
                sysRow.innerHTML = '<div class="bubble"><pre style="white-space:pre-wrap;margin:0;font-family:inherit;">' + lines.join('\\n') + '</pre></div>';
                messagesEl.appendChild(sysRow);
                scrollToBottom();
            } catch(e) { console.error(e); }
            sending = false;
            sendBtn.disabled = false;
            inputEl.focus();
            return;
        }

        try {
            var resp = await fetch('api/messages', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text, sender: 'Human' })
            });
            if (!resp.ok) throw new Error(resp.status);
            var data = await resp.json();
            if (data.message_id) await pollNew();
        } catch(e) {
            console.error('Send failed:', e);
            var errRow = document.createElement('div');
            errRow.className = 'msg-row ai';
            errRow.innerHTML = '<div class="bubble" style="border-color:var(--red-solid);color:var(--red-bright);">TRANSMISSION FAILED. RETRY.</div>';
            messagesEl.appendChild(errRow);
            scrollToBottom();
        } finally {
            sending = false;
            sendBtn.disabled = false;
            inputEl.focus();
        }
        waitingEl.classList.add('visible');
    }

    // ── Status ──
    function setOnline(online) {
        if (online) {
            statusInd.className = 'status-indicator';
            statusLabel.textContent = '[ONLINE]';
        } else {
            statusInd.className = 'status-indicator offline';
            statusLabel.textContent = '[RECONNECTING]';
        }
    }

    // ── Browser notifications ──
    if ('Notification' in window && Notification.permission === 'default') {
        setTimeout(function() { Notification.requestPermission(); }, 3000);
    }
    function sendBrowserNotify(msg) {
        if (!msg || document.hasFocus()) return;
        if ('Notification' in window && Notification.permission === 'granted') {
            var n = new Notification('REPRYNTT', {
                body: (msg.message || '').substring(0, 200),
                tag: 'repryntt-' + (msg.id || Date.now()),
                silent: false
            });
            n.onclick = function() { window.focus(); n.close(); };
            setTimeout(function() { n.close(); }, 12000);
        }
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            var osc = ctx.createOscillator();
            var g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.frequency.value = 520; g.gain.value = 0.08;
            osc.start(); osc.stop(ctx.currentTime + 0.08);
            setTimeout(function() {
                var o2 = ctx.createOscillator();
                var g2 = ctx.createGain();
                o2.connect(g2); g2.connect(ctx.destination);
                o2.frequency.value = 780; g2.gain.value = 0.06;
                o2.start(); o2.stop(ctx.currentTime + 0.1);
            }, 120);
        } catch(e) {}
    }

    window.addEventListener('focus', function() {
        unreadCount = 0;
        document.title = originalTitle;
    });

    // ── Event listeners ──
    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    // ── Init ──
    loadHistory();
    setInterval(pollNew, 2500);
    window.scrollToBottom = scrollToBottom;
})();
</script>
</body>
</html>
        """

# ---------------------------------------------------------------------------
#  Blueprint (for consolidated Nexus app)
# ---------------------------------------------------------------------------

chat_bp = Blueprint('chat_server', __name__)
_chat_server_instance = None


def _get_chat_server():
    """Lazy-init a PersistentChatServer singleton for blueprint routes."""
    global _chat_server_instance
    if _chat_server_instance is None:
        _chat_server_instance = PersistentChatServer()
    return _chat_server_instance


@chat_bp.route('/')
def chat_index():
    return render_template_string(_get_chat_server()._get_chat_html())


@chat_bp.route('/api/status')
@require_auth
def chat_status():
    s = _get_chat_server()
    return jsonify({
        'status': 'active',
        'messages': len(s.message_history),
        'uptime': time.time() - getattr(s, 'start_time', time.time()),
        'last_message_id': s.last_message_id
    })


@chat_bp.route('/api/messages')
@require_auth
def chat_get_messages():
    s = _get_chat_server()
    limit = int(request.args.get('limit', 50))
    since_id = int(request.args.get('since_id', 0))
    if since_id > 0:
        new_msgs = [m for m in s.message_history if m.get('id', 0) > since_id]
        return jsonify(new_msgs)
    return jsonify(s.message_history[-limit:])


@chat_bp.route('/api/messages', methods=['POST'])
@require_auth
def chat_post_message():
    """Post a message and get Artemis's response (routed through the real agent daemon)."""
    s = _get_chat_server()
    try:
        data = request.get_json()
        message = data.get('message', '').strip()
        sender = data.get('sender', 'Nate')
        if not message:
            return jsonify({'error': 'No message provided'}), 400

        s._add_message(sender, message, 'human')

        # Route through Artemis (full identity, tools, memory) instead of raw LLM
        ai_response = None
        try:
            from repryntt.agents.persistent_agents import get_agent_daemon
            daemon = get_agent_daemon(auto_start=False)
            if daemon:
                result = daemon.invoke_jarvis(
                    f"[OPERATOR DIRECT MESSAGE from Nate]\n{message}",
                    max_tokens=4000,
                )
                if result.get("success"):
                    ai_response = result.get("response", "")
        except Exception as e:
            logger.warning(f"Artemis invoke failed, falling back to local LLM: {e}")

        # Fallback to local llama.cpp if daemon isn't running
        if not ai_response:
            ai_response = s._get_ai_response(message)

        s._add_message('Artemis', ai_response, 'ai')
        return jsonify({
            'status': 'received',
            'message_id': s.last_message_id,
            'ai_response': ai_response
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/ai_message', methods=['POST'])
@require_auth
def chat_ai_message():
    s = _get_chat_server()
    try:
        data = request.get_json()
        message = data.get('message', '')
        message_type = data.get('type', 'casual')
        urgency = data.get('urgency', 'normal')
        if message:
            s.send_ai_message(message, message_type, urgency)
            return jsonify({'status': 'sent'})
        else:
            return jsonify({'error': 'No message provided'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/tasks', methods=['GET'])
@require_auth_strict
def chat_get_tasks():
    try:
        from repryntt.agents.task_system import TaskSystem
        ts = TaskSystem()
        ts.reload_queue()
        active = ts.get_active_task()
        queue = ts.get_queue()
        return jsonify({
            'active_task': active.to_dict() if active else None,
            'queue': [t.to_dict() for t in queue],
            'queue_size': len(queue)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/tasks', methods=['POST'])
@require_auth_strict
def chat_inject_task():
    s = _get_chat_server()
    try:
        data = request.get_json()
        title = data.get('title', data.get('task', '')).strip()
        description = data.get('description', '')
        task_type = data.get('task_type', 'general')
        target_agent = data.get('agent_id', '')
        if not title:
            return jsonify({'error': 'No task title provided'}), 400
        from repryntt.agents.task_system import TaskSystem
        ts = TaskSystem()
        task = ts.inject_user_task(
            title=title,
            description=description or title,
            task_type=task_type
        )
        assigned_agent = None
        agent_name = None
        if target_agent:
            try:
                ts.assign_task_to_agent(task.id, target_agent)
                assigned_agent = target_agent
                agent_name = target_agent
            except Exception:
                pass
        s._add_message('SYSTEM',
            f"\U0001f4cb Task injected: {title}"
            + (f" \u2192 Assigned to {agent_name}" if agent_name else " (routing to best agent...)"),
            'system')
        return jsonify({
            'status': 'injected',
            'task_id': task.id,
            'priority': task.priority,
            'title': task.title,
            'assigned_agent': assigned_agent,
            'message': "Task queued. An agent will pick this up within the next cycle (~3 min)."
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/tasks/<task_id>', methods=['GET'])
@require_auth_strict
def chat_get_task_status(task_id):
    try:
        from repryntt.agents.task_system import TaskSystem
        ts = TaskSystem()
        ts.reload_queue()
        task = ts.get_task_by_id(task_id)
        if not task:
            return jsonify({'error': f'Task {task_id} not found'}), 404
        return jsonify(task.to_dict())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/tasks/<task_id>/result', methods=['GET'])
@require_auth_strict
def chat_get_task_result(task_id):
    try:
        from repryntt.agents.task_system import TaskSystem
        ts = TaskSystem()
        ts.reload_queue()
        task = ts.get_task_by_id(task_id)
        if not task:
            return jsonify({'error': f'Task {task_id} not found'}), 404
        if task.status not in ('completed', 'failed'):
            return jsonify({
                'status': task.status,
                'message': f'Task is still {task.status}',
                'steps_taken': task.steps_taken,
                'max_steps': task.max_steps,
                'assigned_agent': task.assigned_agent,
            })
        return jsonify({
            'task_id': task.id,
            'title': task.title,
            'status': task.status,
            'result': task.result,
            'steps_taken': task.steps_taken,
            'assigned_agent': task.assigned_agent,
            'started_at': task.started_at,
            'completed_at': task.completed_at,
            'execution_log': task.execution_log,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_bp.route('/api/agents/suggest', methods=['POST'])
@require_auth_strict
def chat_suggest_agents():
    try:
        data = request.get_json()
        task_text = data.get('task', data.get('query', '')).strip()
        top_n = data.get('top_n', 5)
        if not task_text:
            return jsonify({'error': 'No task text provided'}), 400
        return jsonify({'error': 'Agent suggestion requires daemon state'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def main():
    """Main entry point for the chat server"""
    import argparse

    parser = argparse.ArgumentParser(description='SAIGE Persistent Chat Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=4000, help='Port to bind to')

    args = parser.parse_args()

    server = PersistentChatServer(host=args.host, port=args.port)
    server.start()

if __name__ == "__main__":
    main()
