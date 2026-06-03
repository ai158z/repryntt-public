#!/usr/bin/env python3
"""
SAIGE Unified Interface - Single Port, Complete AI System
Combines all SAIGE services into one unified Flask application
"""

import os
import sys
import json
import time
import threading
import subprocess
import tempfile
import requests
from datetime import datetime
from flask import Blueprint, Flask, request, send_file, render_template_string, Response, jsonify
from flask_cors import CORS
import logging

# Import our modular components
try:
    from saige_web.conversations.ai_conversation_system import AVACompanionSystem
    CONVERSATION_AVAILABLE = True
    print("✅ AVA Companion System import successful")
except ImportError as e:
    print(f"⚠️ AVA Companion System not available: {e}")
    AVACompanionSystem = None
    CONVERSATION_AVAILABLE = False

try:
    from repryntt.tools.witness import BrainWitness
    BRAIN_WITNESS_AVAILABLE = True
    print("✅ Brain Witness import successful")
except ImportError as e:
    print(f"⚠️ Brain Witness not available: {e}")
    BrainWitness = None
    BRAIN_WITNESS_AVAILABLE = False

try:
    from feeders.knowledge_api_feeder import KnowledgeAPIFeeder
    KNOWLEDGE_AVAILABLE = True
    print("✅ Knowledge API Feeder import successful")
except ImportError as e:
    print(f"⚠️ Knowledge API Feeder not available: {e}")
    KnowledgeAPIFeeder = None
    KNOWLEDGE_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def classify_query_complexity(message):
    """
    Classify query complexity to determine response strategy
    Returns: 'simple', 'complex', or 'needs_clarification'
    """
    message = message.strip().lower()

    # Simple queries (direct response)
    simple_indicators = [
        len(message.split()) < 8,  # Short messages
        any(word in message for word in ['hello', 'hi', 'hey', 'thanks', 'goodbye', 'bye']),
        message.endswith('?') and len(message.split()) < 6,  # Simple questions
        any(phrase in message for phrase in ['how are you', 'what time', 'tell me a joke'])
    ]

    if any(simple_indicators):
        return 'simple'

    # Complex queries (CoT reasoning)
    complex_indicators = [
        len(message.split()) > 15,  # Long messages
        any(word in message for word in ['explain', 'how does', 'why does', 'what is the relationship',
                                       'analyze', 'compare', 'evaluate', 'explore', 'investigate']),
        any(phrase in message for phrase in ['in depth', 'break down', 'step by step', 'detailed explanation']),
        message.count('?') > 1,  # Multiple questions
        any(topic in message for topic in ['quantum', 'consciousness', 'ai architecture', 'neural network',
                                         'philosophy', 'ethics', 'society', 'future', 'evolution'])
    ]

    if any(complex_indicators):
        return 'complex'

    # Ambiguous - needs clarification
    return 'needs_clarification'

def generate_cot_response_for_query(user_message, brain_system, conversation_id="user_chat", relevant_context=""):
    """
    Generate a chain-of-thought response for complex user queries with relevant context
    """
    try:
        logger.info(f"🧠 Creating CoT chain for complex query: {user_message[:50]}...")

        # Create a user query chain with tool access for vector search
        chain_result = brain_system.create_chain_of_thought(
            topic=f"User Query: {user_message[:100]}{'...' if len(user_message) > 100 else ''}",
            goal="Provide comprehensive, well-reasoned answer to user question using chain-of-thought analysis and memory retrieval tools",
            initial_prompt=user_message
        )

        if not chain_result or "Error" in chain_result:
            logger.warning("Failed to create CoT chain, falling back to direct response")
            return None

        # Extract chain ID
        chain_id = None
        if "Created chain-of-thought:" in chain_result:
            try:
                chain_id = chain_result.split()[-1]
            except:
                pass

        if not chain_id:
            logger.warning("Could not extract chain ID from creation result")
            return None

        # Generate initial response using the chain
        context_section = relevant_context if relevant_context else "No additional context available from memory."

        initial_prompt = f"""You are SAIGE providing a comprehensive answer to a user's question.

USER QUESTION: {user_message}

{context_section}

This is part of a chain-of-thought exploration. You have access to powerful tools to gather additional information.

AVAILABLE TOOLS:
- brain_network_search: FIRST CHOICE - Search existing knowledge stored in your brain network
- grokipedia_search / grokedia_search: SECONDARY - Use ONLY for truly new topics when brain search insufficient
- recall_memory: Alias for brain_network_search
- fetch_web_info: Limited web search (use only when grokipedia lacks information)

TOOL USAGE RULES:
• Always check brain_network_search first before external searches
• Avoid redundant searches - build upon existing chain knowledge
• Use grokipedia only when you need genuinely new information

TOOL USAGE FORMAT:
Express your tool needs naturally in conversation. The API will automatically execute appropriate tools.

Examples:
- "Let me search for information about artificial intelligence"
- "I need to check the brain network for sustainable development topics"
- "Recall what I know about machine learning basics"

The system automatically handles tool execution - just communicate naturally!
"""

        # Use BrainSystem's native tool calling — tools are executed internally
        ai_response = brain_system._call_ai_service(
            initial_prompt, include_tools=True, timeout=120
        )

        if ai_response and 'AI_SERVICE_ERROR' not in ai_response:
            # Update the chain with this response
            try:
                brain_system.update_chain_progress(
                    chain_id=chain_id,
                    response=ai_response,
                    insights=["User query analysis completed", f"Query complexity: complex"],
                    next_questions=[]
                )
            except Exception as e:
                logger.warning(f"Failed to update chain progress: {e}")

            return ai_response

        return None

    except Exception as e:
        logger.error(f"Error in CoT response generation: {e}")
        return None

# Import brain system for CoT integration
try:
    from repryntt.brain import get_brain_system, BrainSystemProtocol
    BRAIN_SYSTEM_AVAILABLE = True
    print("✅ Brain System import successful - CoT available for complex queries")
except ImportError as e:
    print(f"⚠️ Brain System not available: {e}")
    get_brain_system = None
    BRAIN_SYSTEM_AVAILABLE = False

app = Flask(__name__)

# Blueprint for consolidated Nexus app
unified_bp = Blueprint('unified_interface', __name__)

# SECURITY: Import auth middleware and setup restricted CORS
try:
    from repryntt.comms.auth import require_auth, require_auth_strict, setup_cors, setup_rate_limit
    setup_cors(app)  # Restricted origins (localhost + LAN only)
    setup_rate_limit(app)
    _AUTH_AVAILABLE = True
except ImportError:
    from flask_cors import CORS
    CORS(app)
    _AUTH_AVAILABLE = False
    print("⚠️ repryntt.comms.auth not available — running WITHOUT authentication")

# Initialize core components
ava_companion = None
brain_witness = None
knowledge_feeder = None
brain_system = None

def initialize_components():
    """Initialize all SAIGE components - just like the standalone AI conversation script"""
    global ava_companion, brain_witness, knowledge_feeder, brain_system

    try:
        # Initialize AVA Companion System FIRST (includes voice interaction)
        # This is the core component that handles all voice interactions
        if CONVERSATION_AVAILABLE and AVACompanionSystem:
            try:
                print("🤖 Initializing AVA Companion System...")
                ava_companion = AVACompanionSystem()
                print("✅ AVA Companion System initialized")

                # Start voice interaction system IMMEDIATELY (like standalone script)
                if hasattr(ava_companion, 'start_companionship'):
                    print("🎤 Starting voice interaction system...")
                    success = ava_companion.start_companionship()
                    if success:
                        print("🎤 AVA voice interaction system started - ready for wake words")
                        print("🎯 Wake words: 'AVA', 'hello are you there', 'help', 'hey you'")
                    else:
                        print("⚠️ Failed to start AVA voice interaction system")
                        print("   This means voice input will not work")
                else:
                    print("⚠️ AVA companion missing start_companionship method")

            except Exception as e:
                print(f"⚠️ AVA Companion System initialization failed: {e}")
                print("   Voice interaction will not be available")
                ava_companion = None
        else:
            print("⚠️ AVA Companion System not available")
            print("   Voice interaction will not be available")
            ava_companion = None

        # Initialize Brain System (for CoT reasoning in complex queries)
        if BRAIN_SYSTEM_AVAILABLE and get_brain_system is not None:
            try:
                print("🧠 Initializing Brain System for CoT reasoning...")
                brain_system = get_brain_system()
                print("✅ Brain System initialized - complex queries will use chain-of-thought reasoning")
            except Exception as e:
                print(f"⚠️ Brain System initialization failed: {e}")
                print("   Chain-of-thought reasoning will not be available for complex queries")
                brain_system = None

        # Initialize Brain Witness (digital witness logging)
        if BRAIN_WITNESS_AVAILABLE and BrainWitness:
            try:
                print("📊 Initializing Brain Witness...")
                brain_witness = BrainWitness()

                # Start Brain Witness Flask server in background thread
                from repryntt.tools.witness import app as brain_app
                import threading
                def run_brain_witness():
                    print("🚀 Starting Brain Witness API server on port 8081...")
                    brain_app.run(host=os.environ.get('SAIGE_BIND_HOST', '0.0.0.0'), port=8081, debug=False, threaded=True, use_reloader=False)

                brain_thread = threading.Thread(target=run_brain_witness, daemon=True)
                brain_thread.start()

                # Wait a moment for server to start
                import time
                time.sleep(2)

                print("✅ Brain Witness initialized and API server started")
            except Exception as e:
                print(f"⚠️ Brain Witness initialization failed: {e}")
                brain_witness = None
        else:
            print("⚠️ Brain Witness not available - digital witness logging disabled")
            brain_witness = None

        # Initialize Knowledge Feeder
        if KNOWLEDGE_AVAILABLE and KnowledgeAPIFeeder:
            try:
                print("📚 Initializing Knowledge API Feeder...")
                knowledge_feeder = KnowledgeAPIFeeder()
                print("✅ Knowledge API Feeder initialized")
            except Exception as e:
                print(f"⚠️ Knowledge API Feeder initialization failed: {e}")
                knowledge_feeder = None
        else:
            print("⚠️ Knowledge API Feeder not available")
            knowledge_feeder = None

        print("\n🚀 SAIGE Unified Interface Ready!")
        print("   • Voice interaction:", "✅ Active" if ava_companion else "❌ Not available")
        print("   • Brain witness:", "✅ Active" if brain_witness else "❌ Not available")
        print("   • Knowledge feeder:", "✅ Active" if knowledge_feeder else "❌ Not available")
        print("\n📡 Access the interface at: http://localhost:3000")

    except Exception as e:
        print(f"❌ Component initialization error: {e}")
        print("   Some services may not be available")

# Only auto-initialize when run as standalone (not when imported as blueprint)
if __name__ == '__main__' or os.environ.get('UNIFIED_INIT', ''):
    initialize_components()

# Service URLs (for when components run separately)
LLAMA_URL = "http://localhost:8080"
TTS_URL = "http://localhost:5000"
BRAIN_WITNESS_URL = "http://localhost:8081"
UNIFIED_INTERFACE_URL = "http://localhost:3000"

# HTML Template for Unified Interface
UNIFIED_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAIGE - Unified AI Interface</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            min-height: 100vh;
            color: white;
        }
        .header {
            background: rgba(255,255,255,0.1);
            padding: 20px;
            text-align: center;
            backdrop-filter: blur(10px);
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .service-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }
        .service-card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            border: 1px solid rgba(255,255,255,0.2);
        }
        .service-card h3 {
            color: #00ff88;
            margin-bottom: 10px;
        }
        .status {
            display: inline-block;
            padding: 5px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }
        .status.online { background: #00ff88; color: black; }
        .status.offline { background: #ff4444; color: white; }
        .voice-interface {
            background: rgba(0,0,0,0.3);
            border-radius: 15px;
            padding: 30px;
            margin-top: 30px;
            text-align: center;
        }

        .record-btn {
            background: linear-gradient(45deg, #ff6b6b, #ee5a52);
            border: none;
            color: white;
            padding: 15px 30px;
            border-radius: 50px;
            font-size: 18px;
            cursor: pointer;
            transition: transform 0.2s;
        }
        .record-btn:hover { transform: scale(1.05); }
        .record-btn.recording {
            background: linear-gradient(45deg, #ff4444, #cc3333);
            animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🚀 SAIGE - Unified AI Interface</h1>
        <p>Single access point to all AI services</p>
    </div>

    <div class="container">
        <div class="service-grid">
            <div class="service-card">
                <h3>🧠 AI Companion (AVA)</h3>
                <div class="status online" id="ava-status">Online</div>
                <p>Voice-activated AI conversations with digital witness logging</p>
            </div>

            <div class="service-card">
                <h3>📚 Knowledge Feeder</h3>
                <div class="status online" id="knowledge-status">Online</div>
                <p>Real-time knowledge aggregation from multiple APIs</p>
            </div>

            <div class="service-card">
                <h3>🎤 TTS Engine</h3>
                <div class="status" id="tts-status">Checking...</div>
                <p>High-quality text-to-speech synthesis</p>
            </div>

            <div class="service-card">
                <h3>📊 Brain Witness</h3>
                <div class="status online" id="brain-status">Online</div>
                <div class="status online" id="cot-status">CoT Available</div>
                <div class="status online" id="vector-status">Vector Search Available</div>
                <p>Complete audio transcript history with integrity verification</p>
            </div>

            <div class="service-card">
                <h3>🤖 Llama AI Model</h3>
                <div class="status" id="llama-status">Checking...</div>
                <p>Local LLM for private, secure AI processing</p>
            </div>

            <div class="service-card">
                <h3>📈 System Monitor</h3>
                <div class="status online" id="monitor-status">Online</div>
                <p>Real-time performance and health monitoring</p>
            </div>
        </div>

        <div class="voice-interface">
            <h2>🎙️ Voice Interaction</h2>
            <p>Speak naturally - AVA is always listening</p>
            <br>
            <div id="voice-status" style="margin-bottom: 15px; font-weight: bold; color: #666;"></div>
            <div id="transcript" style="margin-top: 10px; font-style: italic; min-height: 60px;"></div>
            <div style="margin-top: 15px; font-size: 0.9em; color: #888;">
                💡 Voice input is handled by the edge device microphone, not your browser
            </div>
        </div>

        <div style="margin-top: 20px; padding: 15px; background: #f0f8ff; border: 1px solid #add8e6; border-radius: 8px;">
            <h3>💬 Chat Interface</h3>
            <p>For text conversations, use the dedicated chat server at <a href="http://localhost:4001" target="_blank">http://localhost:4001</a></p>
            <p>This provides persistent conversation history and full AI brain system access.</p>
        </div>
    </div>

    <script>
        // Voice interaction handled by edge device (server-side)
        function updateVoiceStatus() {
            fetch('api/ava/status')
                .then(response => response.json())
                .then(data => {
                    const status = data.voice_active ? '🟢 ACTIVE' : '⚪ STANDBY';
                    document.getElementById('voice-status').textContent = `Voice Status: ${status}`;
                    document.getElementById('transcript').textContent =
                        data.last_transcript || 'Waiting for voice input on edge device...';
                })
                .catch(error => {
                    document.getElementById('voice-status').textContent = 'Voice Status: ⚠️ UNAVAILABLE';
                    document.getElementById('transcript').textContent = 'Edge device voice system not responding';
                });
        }

        // Update voice status every 3 seconds
        function updateCotStatus() {
            fetch('api/health')
                .then(response => response.json())
                .then(data => {
                    // Check if we can make a test API call to see if CoT is available
                    fetch('api/text_chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({message: 'test', response_type: 'complex'})
                    })
                    .then(response => response.json())
                    .then(testData => {
                        if (testData.success !== false) {
                            document.getElementById('cot-status').className = 'status online';
                            document.getElementById('cot-status').textContent = 'CoT Available';
                        } else {
                            document.getElementById('cot-status').className = 'status offline';
                            document.getElementById('cot-status').textContent = 'CoT Offline';
                        }
                    })
                    .catch(error => {
                        document.getElementById('cot-status').className = 'status offline';
                        document.getElementById('cot-status').textContent = 'CoT Offline';
                    });
                })
                .catch(error => {
                    document.getElementById('cot-status').className = 'status offline';
                    document.getElementById('cot-status').textContent = 'CoT Offline';
                });
        }

        function updateVectorStatus() {
            fetch('api/health')
                .then(response => response.json())
                .then(data => {
                    // Check brain system stats for vector search
                    fetch('brain-data')
                        .then(response => response.json())
                        .then(brainData => {
                            if (brainData.vector_search_enabled) {
                                document.getElementById('vector-status').className = 'status online';
                                document.getElementById('vector-status').textContent = 'Vector Search Available';
                            } else {
                                document.getElementById('vector-status').className = 'status offline';
                                document.getElementById('vector-status').textContent = 'Vector Search Offline';
                            }
                        })
                        .catch(error => {
                            document.getElementById('vector-status').className = 'status offline';
                            document.getElementById('vector-status').textContent = 'Vector Search Offline';
                        });
                })
                .catch(error => {
                    document.getElementById('vector-status').className = 'status offline';
                    document.getElementById('vector-status').textContent = 'Vector Search Offline';
                });
        }

        setInterval(updateVoiceStatus, 3000);
        updateVoiceStatus(); // Initial update
        updateCotStatus(); // Initial CoT status check
        updateVectorStatus(); // Initial vector search status check

        // Check service statuses
        async function checkStatuses() {
            try {
                const response = await fetch('service_status');
                const statuses = await response.json();

                Object.keys(statuses).forEach(service => {
                    const element = document.getElementById(service + '-status');
                    if (element) {
                        element.textContent = statuses[service] ? 'Online' : 'Offline';
                        element.className = 'status ' + (statuses[service] ? 'online' : 'offline');
                    }
                });
            } catch (error) {
                console.error('Error checking statuses:', error);
            }
        }

        // Check statuses on load and every 30 seconds
        checkStatuses();
        setInterval(checkStatuses, 30000);
    </script>
</body>
</html>
"""

@unified_bp.route('/')
def unified_interface():
    """Serve the unified web interface"""
    return render_template_string(UNIFIED_HTML)

@unified_bp.route('/api/health')
def health_check():
    # Health check is intentionally unauthenticated for monitoring
    """Health check endpoint for status monitoring"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'brain_system': brain_system is not None,
        'brain_witness': brain_witness is not None,
        'ava_companion': ava_companion is not None,
        'knowledge_feeder': knowledge_feeder is not None
    })

@unified_bp.route('/brain-data')
@require_auth
def brain_data():
    """Get brain system status and capabilities"""
    if brain_system:
        try:
            # Check if vector search is available
            vector_search_enabled = hasattr(brain_system, 'brain_network_search') and brain_system.brain_network_search is not None
            return jsonify({
                'vector_search_enabled': vector_search_enabled,
                'brain_system_available': True,
                'chains_active': len(brain_system.personality_brain.get("active_chains_of_thought", [])) if hasattr(brain_system, 'personality_brain') else 0
            })
        except Exception as e:
            logger.error(f"Error getting brain data: {e}")
            return jsonify({
                'vector_search_enabled': False,
                'brain_system_available': False,
                'error': str(e)
            })
    else:
        return jsonify({
            'vector_search_enabled': False,
            'brain_system_available': False
        })

@unified_bp.route('/service_status')
@require_auth
def service_status():
    """Check status of all integrated services"""
    statuses = {
        'ava': ava_companion is not None and CONVERSATION_AVAILABLE,
        'brain': brain_witness is not None and BRAIN_WITNESS_AVAILABLE,
        'knowledge': knowledge_feeder is not None and KNOWLEDGE_AVAILABLE,
        'tts': check_service_status(TTS_URL + '/health'),
        'llama': check_service_status(LLAMA_URL + '/health'),
        'monitor': True  # This service itself
    }
    return jsonify(statuses)

def check_service_status(url):
    """Check if a service is responding"""
    try:
        response = requests.get(url, timeout=2)
        return response.status_code == 200
    except:
        return False

@unified_bp.route('/api/ava/status')
@require_auth
def ava_status():
    """Get AVA voice system status"""
    try:
        if ava_companion:
            # Check if companionship system is running
            companionship_active = hasattr(ava_companion, 'is_running') and ava_companion.is_running

            # Check if wake word detection is active
            wake_word_active = hasattr(ava_companion, 'wake_word_active') and ava_companion.wake_word_active

            # Overall voice system status
            voice_active = companionship_active and wake_word_active

            status_msg = 'Voice system ready - edge device microphone active'
            if not companionship_active:
                status_msg = 'Voice system not started'
            elif not wake_word_active:
                status_msg = 'Voice system active but wake word detection paused'

            return jsonify({
                'voice_active': voice_active,
                'companionship_active': companionship_active,
                'wake_word_active': wake_word_active,
                'last_transcript': status_msg,
                'status': status_msg
            })
        else:
            return jsonify({
                'voice_active': False,
                'companionship_active': False,
                'wake_word_active': False,
                'last_transcript': 'AVA companion system not available',
                'status': 'AVA companion system not available'
            })
    except Exception as e:
        logger.error(f"AVA status error: {e}")
        return jsonify({
            'voice_active': False,
            'companionship_active': False,
            'wake_word_active': False,
            'last_transcript': f'System error: {str(e)}',
            'status': 'Error checking AVA status'
        })

@unified_bp.route('/api/text_chat', methods=['POST'])
@require_auth
def text_chat():
    """Process text messages through AI system with hybrid CoT reasoning"""
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({'success': False, 'error': 'No message provided'})

        user_message = data['message'].strip()
        if not user_message:
            return jsonify({'success': False, 'error': 'Empty message'})

        conversation_id = data.get('conversation_id', 'default')
        response_type = data.get('response_type', 'auto')  # 'auto', 'simple', 'complex'

        # Classify query complexity (unless user specified)
        if response_type == 'auto':
            query_complexity = classify_query_complexity(user_message)
        elif response_type == 'simple':
            query_complexity = 'simple'
        elif response_type == 'complex':
            query_complexity = 'complex'
        else:
            query_complexity = 'simple'  # Default to simple

        logger.info(f"Query classification: {query_complexity} for message: {user_message[:50]}...")

        # Get relevant context from brain vector search for all queries
        relevant_context = ""
        if brain_system and BRAIN_SYSTEM_AVAILABLE:
            try:
                # Get context from brain network search
                context_results = brain_system.get_context_for_question(user_message)
                if context_results and len(context_results.strip()) > 50:  # Only use substantial context
                    relevant_context = f"\n\nRELEVANT CONTEXT FROM MEMORY:\n{context_results}\n"
                    logger.info(f"📚 Retrieved {len(context_results.split())} words of relevant context")
                else:
                    logger.info("📚 No substantial relevant context found")
            except Exception as e:
                logger.warning(f"Failed to retrieve context: {e}")

        # Handle complex queries with CoT reasoning
        if query_complexity == 'complex' and brain_system and BRAIN_SYSTEM_AVAILABLE:
            logger.info("🔍 Using chain-of-thought reasoning for complex query")
            cot_response = generate_cot_response_for_query(user_message, brain_system, conversation_id, relevant_context)

            if cot_response:
                # Store conversation in episodic memory
                try:
                    brain_system.store_episodic_memory(
                        conversation_id=conversation_id,
                        user_input=user_message,
                        ai_response=cot_response,
                        tool_calls=[],
                        outcome="complex_query_answered"
                    )
                except Exception as e:
                    logger.warning(f"Failed to store complex conversation in memory: {e}")

                return jsonify({
                    'success': True,
                    'response': cot_response,
                    'response_type': 'complex',
                    'reasoning_method': 'chain_of_thought'
                })

        # Handle simple queries or fallback for complex queries
        if query_complexity == 'simple' or query_complexity == 'needs_clarification':
            logger.info("💬 Using direct response for simple query")

            # Create enhanced prompt with tool awareness for all queries
            system_prompt = """You are SAIGE, a helpful and knowledgeable AI assistant with access to powerful tools and extensive knowledge.

AVAILABLE TOOLS (use when needed):
- grokipedia_search: Search academic and technical knowledge from Grok-ipedia database
- knowledge_search: Search knowledge sources (Wikipedia, arXiv, PubMed, NASA, etc.)
- brain_network_search: Search SAIGE brain network for relevant information
- read_file: Read content from a file
- write_file: Write content to a file
- run_terminal_cmd: Execute a terminal command
- analyze_topic: Analyze a topic using AI reasoning
- generate_creative_content: Generate creative content (stories, poems, etc.)
- compute_zeta_function: Compute Riemann zeta function values
- symbolic_manipulation: Perform symbolic mathematics
- get_wallet_balance: Get wallet balance from blockchain
- submit_workload: Submit AI workload to blockchain network
- google_maps_search: Search for places using Google Maps
- get_directions: Get directions between locations

TOOL USAGE RULES:
• Use tools when you need additional information to provide a complete answer
• Always prefer brain_network_search for topics you know exist in your knowledge base
• Use grokipedia_search for genuinely new topics or current events
• Use knowledge_search for researching topics across Wikipedia, arXiv, PubMed, NASA, etc.
• Express your tool needs naturally - the API will automatically execute appropriate tools

You have access to a vast knowledge base covering science, technology, philosophy, consciousness, AI development, and many other topics. Use your tools when appropriate to provide the most accurate and helpful responses."""

            enhanced_message = user_message
            if relevant_context:
                enhanced_message = f"{user_message}\n\nContext from memory: {relevant_context.strip()}"

            # Always include tool awareness in the prompt
            full_prompt = f"{system_prompt}\n\nUSER MESSAGE: {enhanced_message}\n\nProvide a helpful, accurate response. Use tools if you need additional information to give a complete answer."

            try:
                # Use BrainSystem's native tool calling if available
                if brain_system and BRAIN_SYSTEM_AVAILABLE:
                    ai_response = brain_system._call_ai_service(
                        full_prompt, include_tools=True,
                        timeout=30 if query_complexity == 'simple' else 45
                    )
                else:
                    # Fallback: raw HTTP without tool execution
                    from repryntt.routing.ai_queue import master_ai_queue
                    llama_response = master_ai_queue.submit_request(
                        lambda: requests.post(
                            LLAMA_URL + '/chat/completions',
                            json={
                                'messages': [{'role': 'user', 'content': full_prompt}],
                                'temperature': data.get('temperature', 0.7),
                                'max_tokens': data.get('max_tokens', 1024)
                            },
                            timeout=15 if query_complexity == 'simple' else 30
                        ),
                        priority=0,
                        timeout=30 if query_complexity == 'simple' else 45
                    )
                    ai_response = ''
                    if llama_response.status_code == 200:
                        ai_response = llama_response.json().get('choices', [{}])[0].get('message', {}).get('content', '')

                if ai_response and 'AI_SERVICE_ERROR' not in ai_response:
                    # Store conversation in episodic memory if brain system available
                    if brain_system and BRAIN_SYSTEM_AVAILABLE:
                        try:
                            brain_system.store_episodic_memory(
                                conversation_id=conversation_id,
                                user_input=user_message,
                                ai_response=ai_response,
                                tool_calls=[],
                                outcome=f"{query_complexity}_query_answered"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to store conversation in memory: {e}")

                    return jsonify({
                        'success': True,
                        'response': ai_response,
                        'response_type': query_complexity,
                        'reasoning_method': 'direct_with_tools',
                        'context_used': bool(relevant_context)
                    })

            except Exception as e:
                logger.warning(f"Direct Llama call failed: {e}")

            # Final fallback
            return jsonify({
                'success': False,
                'error': 'AI services temporarily unavailable. Please check server connections.'
            })

    except Exception as e:
        logger.error(f"Text chat error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# SECURITY: Proxy endpoint restricted — auth required + only allows specific safe prefixes
@unified_bp.route('/api/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@require_auth_strict
def proxy_to_services(path):
    """Proxy requests to appropriate backend services (auth required)"""
    # SECURITY: Only allow specific known service prefixes (no open proxy)
    ALLOWED_PREFIXES = {'llama', 'tts', 'brain'}
    prefix = path.split('/')[0] if '/' in path else path
    if prefix not in ALLOWED_PREFIXES:
        return jsonify({'error': 'Unknown service'}), 404

    if prefix == 'llama':
        target_url = f"{LLAMA_URL}/{path}"
    elif prefix == 'tts':
        target_url = f"{TTS_URL}/{path}"
    elif prefix == 'brain':
        target_url = f"{BRAIN_WITNESS_URL}/{path}"
    else:
        return jsonify({'error': 'Unknown service'}), 404

    try:
        response = requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ['host', 'content-length']},
            data=request.get_data(),
            params=request.args,
            timeout=10
        )
        return response.content, response.status_code, dict(response.headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 502

if __name__ == '__main__':
    app.register_blueprint(unified_bp)
    print("🚀 Starting SAIGE Unified Interface...")
    print("📡 Single access point for all AI services")
    print("🌐 Open http://localhost:3000 in your browser")
    print("")
    print("Available Services:")
    if CONVERSATION_AVAILABLE:
        print("  ✅ AI Companion (AVA)")
    else:
        print("  ❌ AI Companion (AVA) - Import failed")

    if BRAIN_WITNESS_AVAILABLE:
        print("  ✅ Brain Witness Logger")
    else:
        print("  ❌ Brain Witness Logger - Import failed")

    if KNOWLEDGE_AVAILABLE:
        print("  ✅ Knowledge API Feeder")
    else:
        print("  ❌ Knowledge API Feeder - Import failed")

    print("  • TTS Engine (external)")
    print("  • Llama AI Model (external)")
    print("  • System Monitoring (integrated)")
    print("")

    # SECURITY: Bind to LAN interface only (not 0.0.0.0 which exposes to all networks)
    # Use 0.0.0.0 only if explicitly needed for WAN access behind a firewall
    bind_host = os.environ.get('SAIGE_BIND_HOST', '0.0.0.0')

    # SECURITY: Enable TLS if certificates are available
    ssl_ctx = None
    try:
        from repryntt.comms.auth import get_tls_context
        ssl_ctx = get_tls_context()
        if ssl_ctx:
            print("  🔒 TLS ENABLED — HTTPS mode")
        else:
            print("  ⚠️  TLS not configured — run: openssl req -x509 -newkey rsa:4096 -nodes \\")
            print("       -keyout ~/.saige/saige.key -out ~/.saige/saige.crt -days 365 -subj '/CN=SAIGE'")
    except Exception:
        pass

    app.run(host=bind_host, port=3000, debug=False, threaded=True, ssl_context=ssl_ctx)

