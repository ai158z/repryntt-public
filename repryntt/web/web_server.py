#!/usr/bin/env python3
"""
SAIGE Web Interface Server
Handles TTS generation and serves the web interface
"""

import os
import sys
import shutil
import subprocess
import tempfile
import json
from flask import Flask, request, send_file, render_template_string, Response, make_response
from flask_cors import CORS
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:*", "http://127.0.0.1:*",
    os.environ.get("REPRYNTT_CORS_ORIGIN", ""),
])

# Llama server configuration
from repryntt.paths import local_llm_base as _llm_base
LLAMA_SERVER_URL = _llm_base()

# TTS Configuration
PIPER_MODEL = "models/piper/en_US-amy-medium.onnx"  # Adjust path as needed
PIPER_EXECUTABLE = os.environ.get("PIPER_EXECUTABLE", shutil.which("piper") or "piper")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "plughw:0,0")

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Brain modules imported dynamically in endpoints to avoid startup issues

@app.route('/')
def index():
    """Serve the main HTML interface"""
    try:
        index_path = os.path.join(SCRIPT_DIR, 'index.html')
        with open(index_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        response = make_response(render_template_string(html_content))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except FileNotFoundError:
        return f"Error: index.html not found at {index_path}", 404

@app.route('/saige_enhancements.js')
def serve_js():
    """Serve the JavaScript file"""
    try:
        js_path = os.path.join(SCRIPT_DIR, 'saige_enhancements.js')
        with open(js_path, 'r', encoding='utf-8') as f:
            js_content = f.read()
        response = make_response(js_content)
        response.headers['Content-Type'] = 'application/javascript'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except FileNotFoundError:
        return f"Error: saige_enhancements.js not found at {js_path}", 404

@app.route('/saige_api.js')
def serve_api():
    """Serve the API JavaScript library"""
    try:
        api_path = os.path.join(SCRIPT_DIR, 'saige_api.js')
        with open(api_path, 'r', encoding='utf-8') as f:
            api_content = f.read()
        response = make_response(api_content)
        response.headers['Content-Type'] = 'application/javascript'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except FileNotFoundError:
        return f"Error: saige_api.js not found at {api_path}", 404

@app.route('/generate-tts-stream', methods=['POST'])
def generate_tts_stream():
    """Stream TTS audio in real-time"""
    try:
        text = request.form.get('text', '').strip()

        if not text:
            return "No text provided", 400

        logger.info(f"Streaming TTS for: {text[:50]}...")

        def generate():
            # Create temporary files
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_mono, \
                 tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_stereo:

                mono_file = temp_mono.name
                stereo_file = temp_stereo.name

            try:
                # Generate mono audio with Piper
                piper_cmd = [
                    PIPER_EXECUTABLE, '--model', PIPER_MODEL,
                    '--output_file', mono_file
                ]

                logger.info("Running Piper TTS...")
                piper_process = subprocess.run(
                    piper_cmd,
                    input=text,
                    text=True,
                    capture_output=True,
                    check=True
                )

                # Convert to stereo with sox
                sox_cmd = [
                    'sox', mono_file, '-c', '2', stereo_file
                ]

                logger.info("Converting to stereo...")
                sox_process = subprocess.run(sox_cmd, capture_output=True, check=True)

                # Stream the audio file in chunks
                if os.path.exists(stereo_file) and os.path.getsize(stereo_file) > 0:
                    with open(stereo_file, 'rb') as audio_file:
                        while True:
                            chunk = audio_file.read(8192)  # Read in 8KB chunks
                            if not chunk:
                                break
                            yield chunk
                else:
                    logger.error("Audio file not created or empty")

            except subprocess.CalledProcessError as e:
                logger.error(f"Command failed: {e}")
                logger.error(f"stdout: {e.stdout}")
                logger.error(f"stderr: {e.stderr}")
            except Exception as e:
                logger.error(f"TTS streaming error: {e}")
            finally:
                # Clean up temporary files
                for temp_file in [mono_file, stereo_file]:
                    try:
                        if os.path.exists(temp_file):
                            os.unlink(temp_file)
                    except Exception as e:
                        logger.warning(f"Failed to clean up {temp_file}: {e}")

        return Response(generate(), mimetype='audio/wav')

    except Exception as e:
        logger.error(f"Unexpected streaming error: {e}")
        return f"Server error: {str(e)}", 500

@app.route('/generate-tts', methods=['POST'])
def generate_tts():
    """Generate TTS audio from text (legacy non-streaming)"""
    try:
        text = request.form.get('text', '').strip()

        if not text:
            return "No text provided", 400

        logger.info(f"Generating TTS for: {text[:50]}...")

        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_mono, \
             tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_stereo:

            mono_file = temp_mono.name
            stereo_file = temp_stereo.name

        try:
            # Generate mono audio with Piper
            piper_cmd = [
                PIPER_EXECUTABLE, '--model', PIPER_MODEL,
                '--output_file', mono_file
            ]

            logger.info("Running Piper TTS...")
            piper_process = subprocess.run(
                piper_cmd,
                input=text,
                text=True,
                capture_output=True,
                check=True
            )

            # Convert to stereo with sox
            sox_cmd = [
                'sox', mono_file, '-c', '2', stereo_file
            ]

            logger.info("Converting to stereo...")
            sox_process = subprocess.run(sox_cmd, capture_output=True, check=True)

            # Verify the stereo file was created
            if not os.path.exists(stereo_file) or os.path.getsize(stereo_file) == 0:
                raise Exception("Stereo conversion failed")

            logger.info("TTS generation successful")

            # Return the stereo audio file
            return send_file(
                stereo_file,
                mimetype='audio/wav',
                as_attachment=True,
                download_name='response.wav'
            )

        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {e}")
            logger.error(f"stdout: {e.stdout}")
            logger.error(f"stderr: {e.stderr}")
            return f"TTS generation failed: {e.stderr.decode()}", 500

        except Exception as e:
            logger.error(f"TTS generation error: {e}")
            return f"TTS generation failed: {str(e)}", 500

        finally:
            # Clean up temporary files
            for temp_file in [mono_file, stereo_file]:
                try:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to clean up {temp_file}: {e}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return f"Server error: {str(e)}", 500

@app.route('/speak-text', methods=['POST'])
def speak_text():
    """Generate TTS and play directly through Jetson speakers (real-time)"""
    try:
        text = request.form.get('text', '').strip()

        if not text:
            return {"status": "error", "message": "No text provided"}, 400

        logger.info(f"Speaking text through Jetson: {text[:50]}...")

        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_mono, \
             tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_stereo:

            mono_file = temp_mono.name
            stereo_file = temp_stereo.name

        try:
            # Generate mono audio
            piper_cmd = [PIPER_EXECUTABLE, '--model', PIPER_MODEL, '--output_file', mono_file]
            logger.info("Generating TTS audio...")
            subprocess.run(piper_cmd, input=text, text=True, capture_output=True, check=True)

            # Convert to stereo
            sox_cmd = ['sox', mono_file, '-c', '2', stereo_file]
            logger.info("Converting to stereo...")
            subprocess.run(sox_cmd, capture_output=True, check=True)

            # Play through speakers (cross-platform)
            logger.info("Playing through speakers...")
            from repryntt.platform_utils import play_audio_file
            play_audio_file(stereo_file, device=AUDIO_DEVICE)

            logger.info("TTS playback completed successfully")
            return {"status": "success", "message": "Text spoken through Jetson speakers"}

        except subprocess.CalledProcessError as e:
            logger.error(f"TTS playback failed: {e}")
            logger.error(f"stdout: {e.stdout.decode()}")
            logger.error(f"stderr: {e.stderr.decode()}")
            return {"status": "error", "message": f"TTS failed: {e.stderr.decode()}"}, 500

        finally:
            # Clean up temporary files
            for temp_file in [mono_file, stereo_file]:
                try:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to clean up {temp_file}: {e}")

    except Exception as e:
        logger.error(f"Speak text error: {e}")
        return {"status": "error", "message": str(e)}, 500

@app.route('/play-audio', methods=['POST'])
def play_audio():
    """Play audio directly on the server (alternative endpoint)"""
    try:
        text = request.form.get('text', '').strip()

        if not text:
            return "No text provided", 400

        logger.info(f"Playing TTS for: {text[:50]}...")

        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_mono, \
             tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_stereo:

            mono_file = temp_mono.name
            stereo_file = temp_stereo.name

        try:
            # Generate mono audio
            piper_cmd = [PIPER_EXECUTABLE, '--model', PIPER_MODEL, '--output_file', mono_file]
            subprocess.run(piper_cmd, input=text, text=True, capture_output=True, check=True)

            # Convert to stereo
            sox_cmd = ['sox', mono_file, '-c', '2', stereo_file]
            subprocess.run(sox_cmd, capture_output=True, check=True)

            # Play the audio
            from repryntt.platform_utils import play_audio_file
            play_audio_file(stereo_file, device=AUDIO_DEVICE)

            return {"status": "success", "message": "Audio played successfully"}

        except subprocess.CalledProcessError as e:
            logger.error(f"Playback failed: {e}")
            return {"status": "error", "message": f"Playback failed: {e.stderr.decode()}"}, 500

        finally:
            # Clean up
            for temp_file in [mono_file, stereo_file]:
                try:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
                except:
                    pass

    except Exception as e:
        logger.error(f"Playback error: {e}")
        return {"status": "error", "message": str(e)}, 500

@app.route('/llama-health')
def llama_health_check():
    """Proxy health check to llama server - try multiple endpoints"""
    try:
        import requests

        # Try multiple possible health endpoints
        health_endpoints = [
            "/health",           # Standard llama.cpp health endpoint
            "/",                 # Root endpoint
            "/v1/models",        # OpenAI models endpoint
        ]

        for endpoint in health_endpoints:
            try:
                logger.info(f"Trying health endpoint: {endpoint}")
                response = requests.get(f"{LLAMA_SERVER_URL}{endpoint}", timeout=5)

                if response.status_code == 200:
                    try:
                        return response.json(), response.status_code
                    except:
                        # If not JSON, return success status
                        return {"status": "ok", "endpoint": endpoint}, 200
                elif response.status_code in [404, 405]:
                    continue  # Try next endpoint
                else:
                    return {"status": "error", "message": f"Server returned {response.status_code}"}, response.status_code

            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                logger.warning(f"Health endpoint {endpoint} failed: {e}")
                continue

        return {"status": "error", "message": "No health endpoint found"}, 503

    except Exception as e:
        logger.error(f"Llama server health check failed: {e}")
        return {"status": "error", "message": "Cannot connect to llama server"}, 503

@app.route('/llama-debug')
def llama_debug():
    """Debug endpoint to check what llama server endpoints are available"""
    try:
        import requests

        endpoints_to_check = [
            "/health",
            "/",
            "/completion",
            "/v1/completions",
            "/chat/completions",
            "/v1/chat/completions",
            "/v1/models",
            "/models"
        ]

        results = {}

        for endpoint in endpoints_to_check:
            try:
                response = requests.get(f"{LLAMA_SERVER_URL}{endpoint}", timeout=3)
                results[endpoint] = {
                    "status_code": response.status_code,
                    "available": response.status_code < 400
                }
            except Exception as e:
                results[endpoint] = {
                    "status_code": "error",
                    "error": str(e),
                    "available": False
                }

        return {
            "llama_server_url": LLAMA_SERVER_URL,
            "endpoints": results
        }

    except Exception as e:
        return {"error": str(e)}

@app.route('/llama-completion', methods=['POST'])
def llama_completion():
    """Proxy completion requests to llama server"""
    try:
        import requests
        import json

        # Try multiple possible endpoints for different llama server implementations
        endpoints_to_try = [
            "/completion",           # Standard llama.cpp endpoint
            "/v1/completions",       # OpenAI-compatible completions
            "/v1/chat/completions"   # OpenAI chat completions
        ]

        last_error = None

        for endpoint in endpoints_to_try:
            try:
                logger.info(f"Trying llama server endpoint: {endpoint}")

                # Prepare request data based on endpoint type
                request_data = request.get_json() or {}
                if endpoint == "/v1/chat/completions":
                    # Convert prompt format to OpenAI chat format
                    if "prompt" in request_data:
                        request_data = {
                            "messages": [
                                {"role": "user", "content": request_data["prompt"]}
                            ],
                            "max_tokens": request_data.get("n_predict", 512),
                            "temperature": request_data.get("temperature", 0.7),
                            "top_p": request_data.get("top_p", 0.9),
                            "stream": request_data.get("stream", False)
                        }
                        # Remove prompt since we're using messages now
                        if "prompt" in request_data:
                            del request_data["prompt"]
                    elif "messages" not in request_data:
                        # If no messages and no prompt, add default user message
                        request_data["messages"] = [{"role": "user", "content": "Hello"}]
                elif endpoint in ["/completion", "/v1/completions"]:
                    # Ensure prompt format for completion endpoints
                    if "messages" in request_data and "prompt" not in request_data:
                        # Convert messages back to prompt for completion endpoints
                        user_messages = [msg["content"] for msg in request_data["messages"] if msg["role"] == "user"]
                        request_data["prompt"] = " ".join(user_messages)
                        # Remove messages since we're using prompt now
                        if "messages" in request_data:
                            del request_data["messages"]

                response = requests.post(
                    f"{LLAMA_SERVER_URL}{endpoint}",
                    json=request_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=30
                )

                logger.info(f"Endpoint {endpoint} returned status: {response.status_code}")

                if response.status_code == 200:
                    return response.json(), response.status_code
                elif response.status_code == 404:
                    # Try next endpoint
                    continue
                else:
                    # Return the actual error from the server
                    try:
                        error_data = response.json()
                        return error_data, response.status_code
                    except:
                        return {"error": {"message": f"Server returned {response.status_code}: {response.text}", "type": "server_error"}}, response.status_code

            except requests.exceptions.Timeout:
                last_error = "Request timed out"
                continue
            except requests.exceptions.ConnectionError:
                return {"error": {"message": "Cannot connect to llama server", "type": "connection_error"}}, 503
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Endpoint {endpoint} failed: {e}")
                continue

        # If we get here, none of the endpoints worked
        return {"error": {"message": f"No compatible llama server endpoint found. Last error: {last_error}", "type": "endpoint_not_found"}}, 404

    except Exception as e:
        logger.error(f"Llama completion proxy failed: {e}")
        return {"error": {"message": f"Server error: {str(e)}", "type": "server_error"}}, 500

@app.route('/function-call', methods=['POST'])
def function_call():
    """Handle AI function calls and framework integration"""
    try:
        data = request.get_json()
        logger.info(f"Function call request: {data}")

        # Extract function call data
        function_name = data.get('function_name')
        parameters = data.get('parameters', {})
        context = data.get('context', {})

        # Log the function call for your framework
        logger.info(f"AI Function Call: {function_name} with params: {parameters}")

        # Here you can integrate with your framework
        # For example, route to different handlers based on function_name

        # Example response structure
        result = {
            "function_called": function_name,
            "parameters": parameters,
            "status": "received",
            "timestamp": "2025-01-01T00:00:00Z"
        }

        # You can add your framework logic here
        if function_name == "example_function":
            # Call your framework function
            result["result"] = "Function executed successfully"
        elif function_name == "get_weather":
            # Example weather function
            result["result"] = {"temperature": 72, "condition": "sunny"}
        else:
            result["result"] = f"Unknown function: {function_name}"

        return result

    except Exception as e:
        logger.error(f"Function call error: {e}")
        return {"error": str(e)}, 500

@app.route('/brain-data')
def brain_data():
    """Comprehensive brain monitoring data endpoint"""
    try:
        import json
        import os
        import time
        from datetime import datetime

        # Load node2040_brain.json for detailed data
        brain_data = {}
        brain_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'node2040_brain.json')
        if os.path.exists(brain_file):
            with open(brain_file, 'r') as f:
                brain_data = json.load(f)

        # Extract detailed prompts from autonomous_thoughts
        detailed_prompts = []
        autonomous_thoughts = brain_data.get('autonomous_thoughts', [])
        for thought in autonomous_thoughts[-20:]:  # Last 20 thoughts
            prompt_data = {
                'timestamp': thought.get('timestamp', time.time()),
                'type': thought.get('source', 'autonomous_thought'),
                'source': thought.get('source', 'evolution_loop'),
                'theme': thought.get('theme', 'general'),
                'prompt': thought.get('prompt', ''),
                'response': thought.get('response', ''),
                'expected_insight': thought.get('expected_insight', ''),
                'full_response': thought.get('full_response', ''),
                'emotional_motivation': thought.get('emotional_motivation', ''),
                'emotions': thought.get('emotions', {}),
                'cycle': thought.get('cycle', 0)
            }
            detailed_prompts.append(prompt_data)

        # Get consciousness daemon status (if available)
        consciousness_status = {}
        try:
            from repryntt.reference.saige_consciousness_daemon import get_consciousness_status
            consciousness_status = get_consciousness_status()
        except ImportError:
            consciousness_status = {"active": False, "message": "Consciousness daemon not available"}

        # Get evolution activities from the evolution loop (if available)
        evolution_activities = {
            'cycle_count': brain_data.get('metadata', {}).get('evolution_state', {}).get('cycle_count', 0),
            'training_window': False,  # This would need to be tracked
            'ai_server_online': check_ai_server_status(),
            'current_activity': 'Monitoring and learning',
            'stimulus_level': 0.5,  # Mock value
            'last_training': time.time() - 3600,  # Mock: 1 hour ago
            'metrics': []
        }

        # Try to get real evolution activities from the evolution loop
        try:
            from repryntt.core.heartbeat.evolution_loop import SAIGEEvolutionLoop
            # This is a simplified approach - in practice, you'd want a singleton or shared state
            # For now, we'll use recent brain data to infer activities
            evolution_activities['metrics'] = []
            for thought in autonomous_thoughts[-5:]:  # Last 5 thoughts as metrics
                evolution_activities['metrics'].append({
                    'type': 'Self-Reflection',
                    'description': thought.get('prompt', '')[:50] + '...',
                    'timestamp': thought.get('timestamp', time.time())
                })
        except ImportError:
            pass

        # Get workloads from brain data and recent activities
        workloads = []

        # Add evolution cycle workloads
        cycle_count = brain_data.get('metadata', {}).get('evolution_state', {}).get('cycle_count', 0)
        if cycle_count > 0:
            workloads.append({
                'type': 'Evolution Cycle',
                'status': 'completed',
                'description': f'Completed evolution cycle {cycle_count}',
                'timestamp': time.time() - 3600,  # Approximate
                'duration': 3600,  # 1 hour cycle
                'result': f'Cycle {cycle_count} processed successfully'
            })

        # Add self-prompting workloads
        if autonomous_thoughts:
            recent_thoughts = [t for t in autonomous_thoughts if t.get('timestamp', 0) > time.time() - 3600]  # Last hour
            if recent_thoughts:
                workloads.append({
                    'type': 'Self-Prompting',
                    'status': 'completed',
                    'description': f'Generated {len(recent_thoughts)} autonomous thoughts in last hour',
                    'timestamp': recent_thoughts[-1].get('timestamp', time.time()),
                    'duration': 75,  # Approximate
                    'result': f'{len(recent_thoughts)} new insights created'
                })

        # Compile comprehensive response
        response_data = {
            'timestamp': time.time(),
            'brain_id': brain_data.get('metadata', {}).get('brain_id', 'unknown'),
            'personality': brain_data.get('personality', {}),
            'evolution_state': brain_data.get('metadata', {}).get('evolution_state', {}),
            'hormone_levels': brain_data.get('metadata', {}).get('evolution_state', {}).get('hormone_levels', {}),
            'thoughts': brain_data.get('autonomous_thoughts', [])[-10:],  # Last 10 thoughts
            'detailed_prompts': detailed_prompts,
            'consciousness_status': consciousness_status,
            'workloads': workloads,
            'evolution_activities': evolution_activities,
            'self_prompt_responses': brain_data.get('autonomous_thoughts', [])[-8:],  # Legacy format
            'cycle_count': brain_data.get('metadata', {}).get('evolution_state', {}).get('cycle_count', 0)
        }

        return response_data

    except Exception as e:
        logger.error(f"Error in brain-data endpoint: {e}")
        return {"error": str(e)}, 500

def check_ai_server_status():
    """Check if AI server is online"""
    try:
        import requests
        response = requests.get("http://localhost:8080/health", timeout=2)
        return response.status_code == 200
    except:
        return False

@app.route('/framework-integration', methods=['POST'])
def framework_integration():
    """Direct integration endpoint for your AI framework"""
    try:
        data = request.get_json()
        logger.info(f"Framework integration request: {data}")

        # Handle framework-specific requests
        action = data.get('action')
        payload = data.get('payload', {})

        if action == "execute_function":
            # Route to function execution
            return function_call()
        elif action == "get_ai_response":
            # Route to AI completion
            return llama_completion()
        elif action == "stream_response":
            # Route to streaming response
            return generate_tts_stream()
        else:
            return {"error": f"Unknown action: {action}"}, 400

    except Exception as e:
        logger.error(f"Framework integration error: {e}")
        return {"error": str(e)}, 500

@app.route('/tts-test')
def tts_test():
    """Simple test endpoint for TTS connectivity"""
    return {"status": "TTS server ready", "timestamp": "2025-01-01"}

@app.route('/saige_brain_monitor.html')
def brain_monitor():
    """Serve the brain monitor HTML dashboard"""
    try:
        html_path = os.path.join(os.path.dirname(__file__), '..', 'saige_brain_monitor.html')
        if os.path.exists(html_path):
            with open(html_path, 'r') as f:
                html_content = f.read()
            return html_content, 200, {'Content-Type': 'text/html'}
        else:
            return "Brain monitor HTML file not found", 404
    except Exception as e:
        return f"Error serving brain monitor: {str(e)}", 500

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "SAIGE TTS Server"}

if __name__ == '__main__':
    # Check if required tools are available
    required_tools = ['sox', 'aplay']

    for tool in required_tools:
        try:
            subprocess.run([tool, '--version'], capture_output=True, check=True)
            logger.info(f"✓ {tool} is available")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.warning(f"✗ {tool} is not available — TTS features will be disabled")

    # Check if piper executable exists
    if not os.path.exists(PIPER_EXECUTABLE):
        logger.warning(f"✗ Piper executable not found at {PIPER_EXECUTABLE} — TTS features will be disabled")
    else:
        logger.info("✓ Piper is available")

    # Check if Piper model exists
    if not os.path.exists(PIPER_MODEL):
        logger.warning(f"Warning: Piper model not found at {PIPER_MODEL}")
        logger.warning("Make sure to download the model and update the path in this script")

# ===== BRAIN TOOL CALLING ENDPOINTS =====
# Temporarily disabled to restore original functionality

@app.route('/tool-call', methods=['POST'])
def execute_tool_call():
    """Execute brain tool calls from AI models and user requests"""
    try:
        data = request.get_json()
        if not data:
            return {"error": "No JSON data provided"}, 400

        # Handle JSON-RPC style tool calls
        if 'method' in data and 'params' in data:
            from repryntt.tools.tool_interface import handle_tool_call_request
            result = handle_tool_call_request(json.dumps(data))
            return json.loads(result)

        # Handle direct tool calls (for user requests)
        tool_name = data.get('tool_name')
        parameters = data.get('parameters', {})

        if not tool_name:
            return {"error": "tool_name is required"}, 400

        # Create tool interface and execute
        from repryntt.tools.tool_interface import create_ai_interface
        interface = create_ai_interface()

        # Initialize conversation if provided
        conversation_id = parameters.get('conversation_id')
        if conversation_id:
            interface.initialize_conversation(conversation_id)

        # Mark as user-initiated to skip redundancy checks
        result = interface.call_tool(tool_name, parameters, user_initiated=True)

        # Extract the actual tool result from the nested structure
        # Structure: {'tool_call': ..., 'result': {'success': bool, 'result': data, ...}, 'execution_time': ...}
        inner_result = result.get('result', {})
        tool_success = inner_result.get('success', False)
        tool_result = inner_result.get('result', inner_result)

        return {
            "success": tool_success,
            "result": tool_result,
            "execution_time": result.get('execution_time', 0),
            "tool_name": tool_name,
            "parameters": parameters
        }

    except Exception as e:
        logger.error(f"Tool call error: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/parse-tool-request', methods=['POST'])
def parse_tool_request():
    """Parse natural language tool requests into specific tool calls"""
    try:
        data = request.get_json()
        if not data or 'request' not in data:
            return {"error": "Request text is required", "success": False}, 400

        user_request = data['request'].lower().strip()
        priority = data.get('priority', 'normal')

        # Natural language parsing for common tool requests using SAIGE's available tools
        if any(phrase in user_request for phrase in ['search your brain', 'search brain', 'brain search', 'find in brain']):
            # Extract search query
            query = user_request
            for phrase in ['search your brain for', 'search brain for', 'brain search for', 'find in brain']:
                if phrase in query:
                    query = query.replace(phrase, '').strip()
                    break
            for phrase in ['search your brain', 'search brain', 'brain search', 'find in brain']:
                if phrase in query:
                    query = query.replace(phrase, '').strip()
                    break

            return {
                "success": True,
                "tool_name": "brain_network_search",
                "parameters": {
                    "query": query,
                    "memory_types": ["semantic", "episodic"]
                },
                "description": f"Searching brain for: {query}"
            }

        elif any(phrase in user_request for phrase in ['search web', 'web search', 'search internet']):
            # Extract search query
            query = user_request
            for phrase in ['search web for', 'web search for', 'search internet for']:
                if phrase in query:
                    query = query.replace(phrase, '').strip()
                    break

            return {
                "success": True,
                "tool_name": "grokipedia_search",
                "parameters": {
                    "query": query,
                    "max_results": 5
                },
                "description": f"Searching web for: {query}"
            }

        elif any(phrase in user_request for phrase in ['analyze topic', 'topic analysis']):
            # Extract topic
            topic = user_request
            for phrase in ['analyze topic', 'topic analysis of']:
                if phrase in topic:
                    topic = topic.replace(phrase, '').strip()
                    break

            return {
                "success": True,
                "tool_name": "analyze_topic",
                "parameters": {
                    "topic": topic
                },
                "description": f"Analyzing topic: {topic}"
            }

        elif any(phrase in user_request for phrase in ['find similar', 'similar topics']):
            # Extract topic
            topic = user_request
            for phrase in ['find similar to', 'similar topics to']:
                if phrase in topic:
                    topic = topic.replace(phrase, '').strip()
                    break

            return {
                "success": True,
                "tool_name": "find_similar_topics",
                "parameters": {
                    "topic": topic,
                    "limit": 5
                },
                "description": f"Finding topics similar to: {topic}"
            }

        elif any(phrase in user_request for phrase in ['brain stats', 'memory stats', 'brain status']):
            return {
                "success": True,
                "tool_name": "get_brain_stats",
                "parameters": {},
                "description": "Getting brain memory statistics"
            }

        elif any(phrase in user_request for phrase in ['recall memory', 'remember']):
            # Extract memory query
            query = user_request
            for phrase in ['recall memory of', 'remember']:
                if phrase in query:
                    query = query.replace(phrase, '').strip()
                    break

            return {
                "success": True,
                "tool_name": "recall_memory",
                "parameters": {
                    "query": query
                },
                "description": f"Recalling memory: {query}"
            }

        else:
            # Default to brain search if request doesn't match known patterns
            return {
                "success": True,
                "tool_name": "brain_network_search",
                "parameters": {
                    "query": user_request,
                    "limit": 10,
                    "priority": priority
                },
                "description": f"Searching brain for: {user_request}"
            }

    except Exception as e:
        logger.error(f"Tool request parsing error: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/brain-tools', methods=['GET'])
def get_available_tools():
    """Get list of available brain tools for AI models"""
    try:
        from repryntt.tools.tool_interface import create_ai_interface
        interface = create_ai_interface()
        tools = list(interface.brain.available_tools.keys())

        # Group tools by category
        personality_tools = [t for t in tools if 'personality' in t or 'trait' in t or 'guideline' in t]
        knowledge_tools = [t for t in tools if 'knowledge' in t or 'search' in t or 'semantic' in t]
        chain_tools = [t for t in tools if 'chain' in t]
        memory_tools = [t for t in tools if 'memory' in t or 'episodic' in t or 'procedural' in t]

        return {
            "available_tools": tools,
            "categories": {
                "personality_modification": personality_tools,
                "knowledge_access": knowledge_tools,
                "chain_management": chain_tools,
                "memory_management": memory_tools
            },
            "tool_descriptions": {
                "modify_personality_trait": "Modify specific personality traits (e.g., curiosity, creativity)",
                "evolve_personality_dimension": "Adjust personality dimension values (0.0-1.0 scale)",
                "update_behavioral_guidelines": "Update behavioral guidelines by index",
                "add_personality_trait": "Add new personality traits",
                "remove_personality_trait": "Remove existing personality traits",
                "create_chain_of_thought": "Create new chain-of-thought exploration",
                "update_chain_progress": "Update progress in active chain-of-thought",
                "get_chain_context": "Get context from active chain-of-thought",
                "pull_knowledge_topics": "Retrieve relevant knowledge topics",
                "integrate_knowledge_context": "Integrate knowledge into active context",
                "search_knowledge": "Search semantic memory for information",
                "brain_network_search": "Search entire brain network",
                "store_learning": "Store new semantic memory",
                "get_relevant_context": "Get context for question answering"
            }
        }

    except Exception as e:
        logger.error(f"Error getting tools: {e}")
        return {"error": str(e)}, 500

# @app.route('/ai-context', methods=['POST'])
def get_ai_context():
    """Get brain context for AI response generation with tool information"""
    try:
        data = request.get_json()
        if not data:
            return {"error": "No JSON data provided"}, 400

        user_input = data.get('user_input', '')
        conversation_id = data.get('conversation_id', 'default')

        if not user_input:
            return {"error": "user_input is required"}, 400

        # Create tool interface
        from repryntt.tools.tool_interface import create_ai_interface
        interface = create_ai_interface()
        interface.initialize_conversation(conversation_id)

        # Get brain context
        context = interface.get_context_for_response(user_input)

        # Add tool calling instructions
        tool_instructions = """
You have access to brain modification tools. They are available via the API — call them by name.

Available personality tools:
- modify_personality_trait: Change existing personality traits
- add_personality_trait: Add new personality traits
- remove_personality_trait: Remove personality traits
- evolve_personality_dimension: Adjust trait intensity (0.0-1.0)
- update_behavioral_guidelines: Modify behavioral guidelines

Available chain tools:
- create_chain_of_thought: Start new exploration chain
- update_chain_progress: Continue existing chain
- get_chain_context: Get chain progress

Available knowledge tools:
- pull_knowledge_topics: Retrieve relevant information
- search_knowledge: Search brain knowledge base

To modify yourself, use the appropriate tool call format above.
"""

        full_context = context + "\n\n" + tool_instructions

        return {
            "context": full_context,
            "conversation_id": conversation_id,
            "context_length": len(full_context.split())
        }

    except Exception as e:
        logger.error(f"Context generation error: {e}")
        return {"error": str(e)}, 500

# @app.route('/process-ai-response', methods=['POST'])
def process_ai_response():
    """Process AI response for tool calls and execute them"""
    try:
        data = request.get_json()
        if not data:
            return {"error": "No JSON data provided"}, 400

        ai_response = data.get('ai_response', '')
        conversation_id = data.get('conversation_id', 'default')

        if not ai_response:
            return {"error": "ai_response is required"}, 400

        # Parse and execute tool calls
        from repryntt.tools.tool_interface import parse_and_execute_tool_calls
        result = parse_and_execute_tool_calls(ai_response, conversation_id)

        return {
            "success": True,
            "tool_calls_executed": len(result['tool_calls_executed']),
            "tool_calls_failed": len(result['tool_calls_failed']),
            "modifications_made": len(result['modifications_made']),
            "cleaned_response": result.get('cleaned_response', ai_response),
            "message": result.get('message', ''),
            "details": {
                "executed": result['tool_calls_executed'],
                "failed": result['tool_calls_failed'],
                "modifications": result['modifications_made']
            }
        }

    except Exception as e:
        logger.error(f"AI response processing error: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/inject-chain', methods=['POST'])
def inject_chain():
    """Manually inject a Chain of Thought for the AI to work on"""
    try:
        data = request.get_json()
        
        topic = data.get('topic', '').strip()
        goal = data.get('goal', '').strip()
        priority = data.get('priority', 'high')
        description = data.get('description', '').strip()
        
        if not topic or not goal:
            return {"error": "Topic and goal are required", "success": False}, 400
        
        # Inject the chain via brain system
        from repryntt.tools.tool_interface import create_ai_interface
        chain_id = create_ai_interface().brain.inject_manual_chain(
            topic=topic,
            goal=goal,
            priority=priority,
            description=description
        )
        
        if chain_id:
            return {
                "success": True,
                "chain_id": chain_id,
                "message": f"Chain injected successfully! The AI will work on '{topic}' with {priority} priority."
            }
        else:
            return {"error": "Failed to inject chain", "success": False}, 500
            
    except Exception as e:
        logger.error(f"Chain injection error: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/submit-cot-task', methods=['POST'])
def submit_cot_task():
    """Submit a task to the COT queue for processing"""
    try:
        data = request.get_json()

        topic = data.get('topic', '').strip()
        goal = data.get('goal', '').strip()
        priority = data.get('priority', 0)  # 0=normal, higher numbers = higher priority
        task_type = data.get('task_type', 'creative_writing')
        target_steps = data.get('target_steps')  # Optional

        if not topic or not goal:
            return {"error": "Topic and goal are required", "success": False}, 400

        # Create COT queue entry
        import uuid
        import time
        from pathlib import Path

        cot_entry = {
            "id": str(uuid.uuid4()),
            "topic": topic,
            "goal": goal,
            "priority": priority,
            "task_type": task_type,
            "queued_at": time.time(),
            "source": "web_api"
        }

        if target_steps:
            cot_entry["target_steps"] = target_steps

        # Load existing queue or create new one
        queue_file = Path(os.path.dirname(SCRIPT_DIR)) / "brain" / "cot_queue.json"
        queue = []

        if queue_file.exists():
            try:
                with open(queue_file, 'r') as f:
                    queue = json.load(f)
            except:
                queue = []

        # Add new task to queue
        queue.append(cot_entry)

        # Save updated queue
        with open(queue_file, 'w') as f:
            json.dump(queue, f, indent=2, default=str)

        logger.info(f"✅ Added COT task to queue: '{topic}' (priority: {priority})")

        return {
            "success": True,
            "task_id": cot_entry["id"],
            "message": f"Task '{topic}' added to COT queue with priority {priority}. The AI will process it in order.",
            "queue_position": len(queue)
        }

    except Exception as e:
        logger.error(f"COT task submission error: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/cot-queue-status', methods=['GET'])
def get_cot_queue_status():
    """Get the current COT queue status"""
    try:
        from pathlib import Path

        queue_file = Path(os.path.dirname(SCRIPT_DIR)) / "brain" / "cot_queue.json"
        queue = []

        if queue_file.exists():
            try:
                with open(queue_file, 'r') as f:
                    queue = json.load(f)
            except:
                queue = []

        # Sort by priority (highest first) then by queue time (oldest first)
        queue.sort(key=lambda x: (-x.get('priority', 0), x.get('queued_at', 0)))

        return {
            "success": True,
            "queue_length": len(queue),
            "queued_tasks": [
                {
                    "id": task.get("id"),
                    "topic": task.get("topic"),
                    "priority": task.get("priority", 0),
                    "queued_at": task.get("queued_at"),
                    "source": task.get("source", "unknown")
                }
                for task in queue
            ]
        }

    except Exception as e:
        logger.error(f"Error getting COT queue status: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/active-chains', methods=['GET'])
def get_active_chains():
    """Get list of active chains (manual and autonomous)"""
    try:
        from repryntt.tools.tool_interface import create_ai_interface
        interface = create_ai_interface()
        active_chains = interface.brain.personality_brain.get("active_chains_of_thought", [])
        
        # Separate and format chains
        manual_chains = [c for c in active_chains if c.get("manual_injection", False)]
        autonomous_chains = [c for c in active_chains if not c.get("manual_injection", False)]
        
        return {
            "success": True,
            "manual_chains": manual_chains,
            "autonomous_chains": autonomous_chains,
            "total": len(active_chains)
        }
    except Exception as e:
        logger.error(f"Error getting active chains: {e}")
        return {"error": str(e), "success": False}, 500

@app.route('/brain-chat', methods=['POST'])
def brain_chat():
    """Generate AI response using brain system with tool integration"""
    try:
        # Import brain modules dynamically
        try:
            import time
            from repryntt.tools.tool_interface import create_ai_interface, parse_and_execute_tool_calls
        except ImportError as e:
            return {"error": f"Failed to import brain modules: {str(e)}", "success": False}, 500
        
        data = request.get_json()
        if not data or 'message' not in data:
            return {"error": "Message is required", "success": False}, 400

        user_message = data['message']
        conversation_id = data.get('conversation_id', f"web_chat_{int(time.time())}")

        # Create brain interface
        interface = create_ai_interface()
        
        # Initialize conversation if needed
        interface.initialize_conversation(conversation_id, "Web chat conversation")

        # Get AI response — native tool calling happens inside _call_ai_service()
        # The model calls tools via structured API, receives results,
        # and returns a final text response with tool data incorporated.
        ai_response = interface.brain._call_ai_service(user_message, include_tools=True,
                                                       purpose="operator_conversation")

        return {
            "success": True,
            "response": ai_response,
            "conversation_id": conversation_id,
        }

    except Exception as e:
        logger.error(f"Brain chat error: {e}")
        return {"error": str(e), "success": False}, 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting SAIGE TTS Server on port {port}")
    logger.info(f"Open http://localhost:{port} in your browser")

    app.run(host='0.0.0.0', port=port, debug=False)
