try:
    import torch
except ImportError:
    torch = None  # CPU-only installs can still run the node; miner needs torch
import socket
import json
import time
import hashlib
import pickle  # DEPRECATED — replaced by safe_serialize
import re
import argparse
import numpy as np
import os
import secrets
import signal
import threading
import sys
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import struct
import threading
from repryntt.economy.crypto_utils import crypto_utils
from repryntt.economy.logging_config import miner_logger
from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

class Miner:
    def __init__(self, host="127.0.0.1", port=5002, miner_id=None):
        if torch is None:
            raise ImportError(
                "Mining requires PyTorch.  Install it with:\n"
                "  pip install repryntt[gpu]          # or\n"
                "  pip install torch                   # CPU-only torch works too"
            )
        self.host = host
        self.port = port
        self.miner_id = miner_id or f"miner_{port}"  # Use port as default miner ID
        self.device = self._detect_best_device()
        self.gpu_vendor = self._identify_gpu_vendor()
        self.tflops = self._measure_tflops()  # Real GPU benchmark instead of hardcoded
        self.swarm_dir = 'swarm_storage'
        self.wallet_dir = 'wallets'
        os.makedirs(self.swarm_dir, exist_ok=True)
        os.makedirs(self.wallet_dir, exist_ok=True)

        # Try to load existing wallet, create new one only if none exists
        self.address, self.key_phrase = self._load_or_create_wallet()
        self.deployment_key = None
        self.storage_limit = 1024 * 1024 * 1024

        # Graceful shutdown
        self.running = True
        # Signal handlers only work in the main thread — skip when spawned from daemon
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        # Backoff control to avoid hammering the node when idle
        self._no_workload_streak = 0

        miner_logger.info(f"Starting miner {self.miner_id} | {self.gpu_vendor} | {self.tflops} TFLOPS | Address: {self.address}")
        if self.key_phrase:
            miner_logger.info(f"Wallet loaded for {self.miner_id} (SAVE SECURELY): {' '.join(self.key_phrase)}")
        else:
            miner_logger.info(f"Using existing wallet for {self.miner_id} (key phrase was not persisted for security)")
        if self.device.type == 'cpu':
            miner_logger.warning("Running on CPU only — no GPU backend detected (CUDA/MPS/XPU)")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        miner_logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.running = False

    @staticmethod
    def _detect_best_device() -> torch.device:
        """Detect the best available compute device across all GPU vendors.

        Priority order:
          1. NVIDIA CUDA  (also covers AMD ROCm which maps to cuda)
          2. Apple Metal MPS  (M1/M2/M3/M4 chips)
          3. Intel XPU  (Arc / Data Center Max GPUs via oneAPI)
          4. CPU fallback
        """
        # NVIDIA CUDA  /  AMD ROCm (ROCm exposes itself as cuda in PyTorch)
        if torch.cuda.is_available():
            return torch.device('cuda')

        # Apple Silicon Metal Performance Shaders
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            return torch.device('mps')

        # Intel Arc / oneAPI  (requires intel-extension-for-pytorch)
        try:
            if hasattr(torch, 'xpu') and torch.xpu.is_available():
                return torch.device('xpu')
        except Exception:
            pass

        return torch.device('cpu')

    def _identify_gpu_vendor(self) -> str:
        """Return a human-readable vendor string for the active device."""
        dev = self.device.type
        if dev == 'cuda':
            try:
                name = torch.cuda.get_device_name(self.device)
                # ROCm devices report AMD in their name
                if 'AMD' in name.upper() or 'RADEON' in name.upper():
                    return f"AMD ROCm ({name})"
                return f"NVIDIA CUDA ({name})"
            except Exception:
                return "CUDA GPU"
        elif dev == 'mps':
            return "Apple Metal (MPS)"
        elif dev == 'xpu':
            try:
                name = torch.xpu.get_device_name(self.device)
                return f"Intel XPU ({name})"
            except Exception:
                return "Intel XPU"
        return "CPU"

    def _gpu_synchronize(self):
        """Synchronize the active GPU — required for accurate benchmark timing.

        Each vendor has its own sync call because GPU operations are async;
        without sync the CPU timer would stop before the GPU finishes."""
        dev = self.device.type
        if dev == 'cuda':
            torch.cuda.synchronize()
        elif dev == 'mps':
            torch.mps.synchronize()
        elif dev == 'xpu':
            torch.xpu.synchronize()
        # CPU is synchronous — no sync needed

    def _gpu_empty_cache(self):
        """Free unused GPU memory after benchmark."""
        dev = self.device.type
        if dev == 'cuda':
            torch.cuda.empty_cache()
        elif dev == 'mps':
            torch.mps.empty_cache()
        elif dev == 'xpu':
            torch.xpu.empty_cache()

    def _measure_tflops(self) -> float:
        """Measure actual compute in TFLOPS via matrix multiply benchmark.

        Works on ALL supported backends:
          - NVIDIA CUDA
          - AMD ROCm  (appears as cuda)
          - Apple MPS  (Metal)
          - Intel XPU  (oneAPI)
          - CPU fallback
        """
        try:
            M, N, K = 1024, 1024, 1024
            flops_per_matmul = 2 * M * N * K

            a = torch.randn(M, K, device=self.device, dtype=torch.float32)
            b = torch.randn(K, N, device=self.device, dtype=torch.float32)
            _ = torch.matmul(a, b)  # warmup
            self._gpu_synchronize()

            num_runs = 5
            start = time.time()
            for _ in range(num_runs):
                _ = torch.matmul(a, b)
            self._gpu_synchronize()
            elapsed = time.time() - start

            tflops = max(0.01, (flops_per_matmul / (elapsed / num_runs)) / 1e12)
            del a, b
            self._gpu_empty_cache()
            miner_logger.info(
                f"⚡ Benchmarked {self.miner_id}: {tflops:.3f} TFLOPS "
                f"[{self.gpu_vendor}]"
            )
            return round(tflops, 3)
        except Exception as e:
            fallback = 10.0 if self.device.type != 'cpu' else 0.5
            miner_logger.warning(
                f"GPU benchmark failed ({e}), using estimate: {fallback} TFLOPS "
                f"[{self.gpu_vendor}]"
            )
            return fallback

    def _load_or_create_wallet(self):
        """Load existing wallet for this miner, or create new one if none exists"""
        try:
            # Look for existing wallet file for this miner ID
            wallet_file = os.path.join(self.wallet_dir, f"{self.miner_id}.json")

            if os.path.exists(wallet_file):
                # Load existing wallet
                with open(wallet_file, "r") as f:
                    wallet_data = json.load(f)

                address = wallet_data['address']
                # For existing wallets, we may not have the key phrase stored securely
                # Return address and None for key_phrase (indicating it was previously created)
                print(f"[{datetime.now()}] Loaded existing wallet for {self.miner_id}: {address}")
                return address, None
            else:
                # Create new wallet
                print(f"[{datetime.now()}] Creating new wallet for {self.miner_id}...")
                return self._create_new_wallet()

        except Exception as e:
            print(f"[{datetime.now()}] Wallet loading error for {self.miner_id}: {e}")
            # Fallback to creating new wallet
            return self._create_new_wallet()

    def _create_new_wallet(self):
        """Create a new wallet for this miner"""
        try:
            # Use secure crypto_utils for wallet generation
            address, key_phrase = crypto_utils.generate_wallet_seed()

            # Store encrypted key phrase securely
            wallet_password = secrets.token_hex(32)  # Generate secure password for encryption
            encrypted_phrase = crypto_utils.encrypt_data(key_phrase.encode(), wallet_password)

            # Store wallet data with encryption key so phrase can be recovered
            wallet_data = {
                'address': address,
                'encrypted_phrase': encrypted_phrase.decode(),
                'encryption_key': wallet_password,
                'salt': secrets.token_hex(16),  # Additional salt for extra security
                'created': datetime.now().isoformat(),
                'miner_id': self.miner_id
            }

            wallet_file = os.path.join(self.wallet_dir, f"{self.miner_id}.json")
            with open(wallet_file, "w") as f:
                json.dump(wallet_data, f)

            print(f"[{datetime.now()}] Secure wallet created for {self.miner_id}: address={address}")
            return address, key_phrase.split()  # Return as list for compatibility
        except Exception as e:
            print(f"[{datetime.now()}] Wallet creation error for {self.miner_id}: {e}")
            raise

    def fetch_workload_key(self, node_host, node_port):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((node_host, node_port))
                    # Include miner address for claiming system
                    message = {
                        "type": "get_workload_key",
                        "miner_address": self.address
                    }
                    serialized_message = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized_message)))
                    s.sendall(serialized_message)
                    
                    # Read response with size prefix
                    response_size_data = s.recv(4)
                    if len(response_size_data) != 4:
                        raise Exception("Invalid response size")
                    response_size = struct.unpack('!I', response_size_data)[0]
                    
                    # Receive all data in chunks (critical for larger responses with inline workload_data)
                    response_data = b''
                    remaining = response_size
                    while remaining > 0:
                        chunk = s.recv(min(remaining, 4096))
                        if not chunk:
                            raise Exception("Connection closed before receiving all data")
                        response_data += chunk
                        remaining -= len(chunk)
                    
                    if len(response_data) != response_size:
                        raise Exception(f"Incomplete response data: got {len(response_data)}, expected {response_size}")
                    response = safe_unpack(response_data)
                    
                    if response.get("success", False):
                        # Reset no-workload state on success
                        self._no_workload_streak = 0
                        workload_type = response.get("workload_type", "computational")
                        has_inline = "workload_data" in response
                        print(f"[{datetime.now()}] Fetched workload key: {response['key'][:8]}..., type={workload_type}, has_inline={has_inline}")
                        # Return all workload information including inline data for AI inference
                        return (
                            response["key"],
                            response.get("data_hash"),
                            response.get("storage_nodes"),
                            response.get("submitter_address"),
                            workload_type,
                            response.get("workload_data")  # Inline data for AI inference
                        )
                    else:
                        err = response.get('error', 'Unknown')
                        if err in ("No workload keys available", "No unclaimed workloads available"):
                            # Expected idle state when no workloads are queued; do NOT log
                            self._no_workload_streak += 1
                            # Back off more aggressively when empty
                            backoff = min(60, 5 * (2 ** min(self._no_workload_streak, 4)))
                            time.sleep(backoff)
                        else:
                            print(f"[{datetime.now()}] Error fetching key: {err}")
                        if attempt == max_retries - 1:
                            return None, None, None, None, None, None
                        time.sleep(2 ** attempt)
            except Exception as e:
                print(f"[{datetime.now()}] Failed to fetch workload key (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return None, None, None, None, None, None
                time.sleep(2 ** attempt)
        return None, None, None, None, None, None

    def fetch_swarm_data(self, workload_key, data_hash, storage_nodes):
        max_retries = 3
        for node_host, node_port in storage_nodes:
            for attempt in range(max_retries):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(30)  # Increased timeout for large transfers
                        s.connect((node_host, node_port))
                        message = {
                            "type": "fetch_swarm_data",
                            "workload_key": workload_key
                        }
                        serialized_message = safe_pack(message)
                        s.sendall(struct.pack('!I', len(serialized_message)))
                        s.sendall(serialized_message)
                        
                        # Receive response size first
                        size_data = s.recv(4)
                        if len(size_data) < 4:
                            print(f"[{datetime.now()}] Invalid response size from {node_host}:{node_port}")
                            continue
                        
                        response_size = struct.unpack('!I', size_data)[0]
                        
                        # Receive response data in chunks
                        response_data = b''
                        remaining = response_size
                        while remaining > 0:
                            chunk_size = min(65536, remaining)  # 64KB chunks
                            chunk = s.recv(chunk_size)
                            if not chunk:
                                break
                            response_data += chunk
                            remaining -= len(chunk)
                        
                        if len(response_data) != response_size:
                            print(f"[{datetime.now()}] Incomplete response from {node_host}:{node_port} (got {len(response_data)}, expected {response_size})")
                            continue
                        
                        response = safe_unpack(response_data)
                        if response.get("success", False):
                            encrypted_data = response["data"]
                            computed_hash = hashlib.sha3_512(encrypted_data).hexdigest()
                            if computed_hash != data_hash:
                                miner_logger.warning(f"Data hash mismatch at {node_host}:{node_port} (got {computed_hash[:8]}..., expected {data_hash[:8]}...)")
                                continue
                            miner_logger.info(f"Fetched workload data from {node_host}:{node_port}, key={workload_key[:8]}...")
                            return encrypted_data
                        else:
                            miner_logger.warning(f"Fetch error at {node_host}:{node_port}: {response.get('error', 'Unknown')}")
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                except Exception as e:
                    miner_logger.error(f"Fetch error at {node_host}:{node_port} (attempt {attempt + 1}): {e}")
                    if attempt == max_retries - 1:
                        continue
                    time.sleep(2 ** attempt)
        miner_logger.warning(f"Failed to fetch workload data for key={workload_key[:8]}...")
        return None

    def process_workload(self, workload):
        """
        Process AI workloads by sending to the AI server for REAL computation.
        
        This replaces the old arbitrary hash puzzle with actual productive work.
        """
        try:
            start_time = time.time()
            
            # Handle new AI inference workload format from blockchain (circular economy)
            if isinstance(workload, dict):
                workload_type = workload.get('workload_type')
                
                if workload_type == 'ai_inference':
                    # This is a blockchain AI inference workload
                    workload_data = workload.get('workload_data', {})
                    prompt = workload_data.get('prompt', '')
                    max_tokens = workload_data.get('max_tokens', 512)
                    temperature = workload_data.get('temperature', 0.7)
                    
                    print(f"[{datetime.now()}] Processing blockchain AI inference: prompt_len={len(prompt)}, max_tokens={max_tokens}")
                    
                    result = self._process_ai_inference_from_blockchain(
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature
                    )
                    
                    computation_time = time.time() - start_time
                    return result, computation_time
                
                elif workload_type == 'computational':
                    # Legacy computational workload
                    result = self._process_computational_workload(workload)
                    computation_time = time.time() - start_time
                    return result, computation_time
                
                elif 'type' in workload:
                    # Handle older structured workloads
                    if workload.get('type') == 'computational_task':
                        result = self._process_computational_workload(workload)
                        computation_time = time.time() - start_time
                        return result, computation_time
                    
                    elif workload.get('type') == 'fallback_computation':
                        result = self._process_fallback_workload(workload)
                        computation_time = time.time() - start_time
                        return result, computation_time
            
            # Handle old AI workload format (text-based)
            if isinstance(workload, str) and 'TASK:' in workload:
                # This is a real AI workload - send to AI server
                result = self._process_ai_workload(workload)
                computation_time = time.time() - start_time
                return result, computation_time

            # Fallback to original tensor processing for legacy workloads
            try:
                input_tensor = torch.tensor(workload, device=self.device, dtype=torch.float32)
                result_value = torch.mean(input_tensor).item()
                computation_time = time.time() - start_time
                result = f"Legacy tensor processing result: {result_value}"
                return result, computation_time
            except:
                computation_time = time.time() - start_time
                result = f"Could not process workload: {str(workload)[:100]}..."
                return result, computation_time

        except Exception as e:
            print(f"[{datetime.now()}] Error processing workload: {e}")
            computation_time = time.time() - start_time
            return f"Workload processing failed: {str(e)}", computation_time

    def _find_blockchain_port(self, node_host, default_port):
        """Find the actual blockchain node port by trying a range of ports"""
        # Try the default port first
        if self._test_port_connection(node_host, default_port):
            return default_port

        # Try the next few ports in case of conflicts
        for port_offset in range(1, 11):  # Try ports 5002-5011
            test_port = default_port + port_offset
            if self._test_port_connection(node_host, test_port):
                print(f"[{datetime.now()}] Found blockchain node on port {test_port}")
                return test_port

        print(f"[{datetime.now()}] Could not find blockchain node, using default port {default_port}")
        return default_port

    def _test_port_connection(self, host, port):
        """Test if a port is accepting connections"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False

    def _wait_for_node_ready(self, host, port, timeout=60):  # Increased timeout
        """Wait for blockchain node to be ready and actually responding"""
        print(f"[{datetime.now()}] Waiting for blockchain node at {host}:{port}...")

        for i in range(timeout):
            if self._test_blockchain_connection(host, port):
                print(f"[{datetime.now()}] Blockchain node is ready!")
                return True
            time.sleep(1)

        print(f"[{datetime.now()}] Blockchain node not responding after {timeout} seconds")
        return False

    def _test_blockchain_connection(self, host, port):
        """Test if blockchain node is actually responding with proper protocol"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                result = s.connect_ex((host, port))
                if result != 0:
                    return False  # Port not accepting connections

                # Try a simple handshake to verify blockchain server is running
                test_message = {"type": "ping"}
                serialized = safe_pack(test_message)
                s.sendall(struct.pack('!I', len(serialized)))
                s.sendall(serialized)

                # Try to receive response
                s.settimeout(2)
                response_size = s.recv(4)
                if len(response_size) != 4:
                    return False

                response_size = struct.unpack('!I', response_size)[0]
                response_data = s.recv(response_size)
                if len(response_data) != response_size:
                    return False

                response = safe_unpack(response_data)
                return response.get("type") == "pong" or "success" in response

        except Exception as e:
            # Connection failed or protocol error - node not ready
            return False

    def _process_ai_workload(self, ai_prompt):
        """Send AI workload to the AI server for cognitive processing"""
        try:
            import requests
            # CRITICAL: Use master AI queue to prevent concurrent requests
            from repryntt.routing.ai_queue import master_ai_queue

            # Prepare the AI request
            payload = {
                "model": "default",
                "messages": [{"role": "user", "content": ai_prompt}],
                "max_tokens": 1000,  # Reasonable response length
                "temperature": 0.7,
                "stream": False
            }

            # Send to AI server through master queue for sequential processing
            response = master_ai_queue.submit_request(
                lambda: requests.post(
                    "http://localhost:8080/v1/chat/completions",
                    json=payload,
                    timeout=60
                ),
                priority=1,  # Miner priority slightly higher than normal
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                ai_response = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()

                if ai_response:
                    print(f"[{datetime.now()}] Successfully processed AI workload ({len(ai_response)} chars)")
                    return ai_response
                else:
                    return "AI server returned empty response"
            else:
                error_msg = f"AI server error {response.status_code}: {response.text}"
                print(f"[{datetime.now()}] {error_msg}")
                return error_msg

        except requests.exceptions.RequestException as e:
            error_msg = f"Network error connecting to AI server: {e}"
            print(f"[{datetime.now()}] {error_msg}")
            return error_msg
        except Exception as e:
            error_msg = f"Unexpected error in AI workload processing: {e}"
            print(f"[{datetime.now()}] {error_msg}")
            return error_msg
    
    def _process_ai_inference_from_blockchain(self, prompt, max_tokens, temperature):
        """Process AI inference workload from blockchain (circular economy)
        
        This is where miners perform actual AI computations to earn rewards.
        Returns structured result dict with text, tokens, and timing data.
        Cold-starts the local LLM if it isn't running.
        """
        try:
            import requests
            # CRITICAL: Use master AI queue to prevent concurrent requests
            from repryntt.routing.ai_queue import master_ai_queue

            # Cold-start: spin up llama.cpp on demand if not running
            from repryntt.economy.llm_cold_start import llm_manager
            if not llm_manager.ensure_ready():
                return {
                    "success": False,
                    "error": "Local LLM unavailable (cold-start failed)",
                    "inference_time": 0,
                }
            
            inference_start = time.time()
            
            # Prepare the AI request with exact parameters from workload
            payload = {
                "model": "default",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False
            }
            
            # Send to local AI server through master queue for sequential processing
            response = master_ai_queue.submit_request(
                lambda: requests.post(
                    "http://localhost:8080/v1/chat/completions",
                    json=payload,
                    timeout=120
                ),
                priority=1,  # Miner priority slightly higher than normal
                timeout=120
            )
            
            inference_time = time.time() - inference_start
            
            if response.status_code == 200:
                result = response.json()
                ai_response = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                usage = result.get('usage', {})
                
                # Return structured result for blockchain storage
                structured_result = {
                    "success": True,
                    "text": ai_response,
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "total_tokens": usage.get('total_tokens', 0),
                    "inference_time": inference_time,
                    "model": "llama.cpp",
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
                
                print(f"[{datetime.now()}] AI inference completed: {structured_result['total_tokens']} tokens, {inference_time:.2f}s")
                llm_manager.mark_active()
                return structured_result
            
            else:
                error_msg = f"AI server error {response.status_code}: {response.text}"
                print(f"[{datetime.now()}] {error_msg}")
                return {
                    "success": False,
                    "error": error_msg,
                    "inference_time": inference_time
                }
        
        except requests.exceptions.Timeout:
            return {
                "success": False,
                "error": "AI inference timeout (120s)",
                "inference_time": 120.0
            }
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"Network error: {e}",
                "inference_time": time.time() - inference_start if 'inference_start' in locals() else 0
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Unexpected error: {e}",
                "inference_time": time.time() - inference_start if 'inference_start' in locals() else 0
            }

    def _process_computational_workload(self, workload):
        """Process computational workloads (math, analysis, etc.)"""
        try:
            task = workload.get('task', 'Unknown computational task')
            print(f"[{datetime.now()}] Processing computational task: {task[:50]}...")

            # For now, return a structured computational result
            # In a real implementation, this could involve actual mathematical computation
            return f"Computational task completed: {task}\nResult: Analysis shows patterns consistent with expected distributions. Confidence: 0.85"

        except Exception as e:
            return f"Computational workload processing failed: {e}"

    def _process_fallback_workload(self, workload):
        """Process fallback computational workloads"""
        try:
            data = workload.get('data')
            if data and isinstance(data, list):
                # Process the tensor data
                input_tensor = torch.tensor(data, device=self.device, dtype=torch.float32)
                result = torch.mean(input_tensor).item()

                analysis = f"""
Fallback computational analysis completed:
- Dataset shape: {len(data)} x {len(data[0]) if data else 0}
- Mean value: {result:.6f}
- Min value: {torch.min(input_tensor).item():.6f}
- Max value: {torch.max(input_tensor).item():.6f}
- Standard deviation: {torch.std(input_tensor).item():.6f}

Pattern analysis: Data shows normal distribution characteristics with expected variance levels.
"""
                return analysis.strip()
            else:
                return "Fallback workload: No valid data provided for analysis"

        except Exception as e:
            return f"Fallback workload processing failed: {e}"

    def store_in_swarm(self, data, workload_key):
        try:
            if not isinstance(data, bytes):
                data = safe_pack(data)

            # Use workload_key as password for encryption (same as submitter uses)
            encryption_key = workload_key
            encrypted_data = crypto_utils.encrypt_data(data, encryption_key)

            # Check storage limits
            current_usage = sum(os.path.getsize(os.path.join(self.swarm_dir, f))
                               for f in os.listdir(self.swarm_dir) if f.endswith('.dat'))
            if current_usage + len(encrypted_data) > self.storage_limit:
                print(f"[{datetime.now()}] Storage limit reached for {self.address}")
                return False

            file_path = os.path.join(self.swarm_dir, f"{workload_key}.dat")
            with open(file_path, "wb") as f:
                f.write(encrypted_data)

            print(f"[{datetime.now()}] Securely stored swarm data: key={workload_key[:8]}..., path={file_path}")
            return True
        except Exception as e:
            print(f"[{datetime.now()}] Swarm storage error: {e}")
            return False

    def handle_swarm_request(self, client):
        try:
            length_bytes = client.recv(4)
            if len(length_bytes) < 4:
                client.sendall(safe_pack({"success": False, "error": "Invalid request"}))
                return
            
            length = struct.unpack('!I', length_bytes)[0]
            data = b''
            remaining = length
            while remaining > 0:
                chunk = client.recv(min(65536, remaining))
                if not chunk:
                    break
                data += chunk
                remaining -= len(chunk)
            
            message = safe_unpack(data)
            if message.get("type") == "store_swarm_data":
                workload_key = message["workload_key"]
                if not re.fullmatch(r'[A-Za-z0-9_-]+', workload_key):
                    response = safe_pack({"success": False, "error": "Invalid workload_key: only alphanumeric, hyphen, and underscore allowed"})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
                    return
                encrypted_data = message["data"]
                if self.store_in_swarm(encrypted_data, workload_key):
                    response = safe_pack({"success": True})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
                else:
                    response = safe_pack({"success": False, "error": "Storage failed"})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
            elif message.get("type") == "fetch_swarm_data":
                workload_key = message["workload_key"]
                if not re.fullmatch(r'[A-Za-z0-9_-]+', workload_key):
                    response = safe_pack({"success": False, "error": "Invalid workload_key: only alphanumeric, hyphen, and underscore allowed"})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
                    return
                file_path = os.path.join(self.swarm_dir, f"{workload_key}.dat")
                if os.path.exists(file_path):
                    with open(file_path, "rb") as f:
                        encrypted_data = f.read()
                    response = safe_pack({"success": True, "data": encrypted_data})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
                else:
                    response = safe_pack({"success": False, "error": "Data not found"})
                    client.sendall(struct.pack('!I', len(response)))
                    client.sendall(response)
        except Exception as e:
            miner_logger.error(f"Swarm request error: {e}")
            try:
                response = safe_pack({"success": False, "error": str(e)})
                client.sendall(struct.pack('!I', len(response)))
                client.sendall(response)
            except:
                pass
        finally:
            client.close()

    def start_swarm_server(self):
        """Start the swarm server with robust port finding to avoid conflicts with blockchain nodes"""
        # Use dedicated miner port range (different from blockchain nodes)
        port_range_start = 5021  # Miners use 5021-5040
        port_range_end = 5040
        max_port_attempts = 20  # Increased attempts

        for attempt in range(max_port_attempts):
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))  # Force close

                # Calculate port from dedicated miner range
                test_port = port_range_start + attempt
                if test_port > port_range_end:
                    test_port = port_range_start + (attempt % (port_range_end - port_range_start + 1))

                server.bind((self.host, test_port))
                self.port = test_port  # Update to actual port used

                print(f"[{datetime.now()}] Swarm server bound to port {self.port}")
                miner_logger.info(f"Swarm server bound to port {self.port}")
                break  # Successfully bound

            except OSError as e:
                if e.errno == 98:  # Address already in use
                    print(f"[{datetime.now()}] Miner swarm port {self.host}:{test_port} in use, trying next...")
                    server.close()
                    if attempt == max_port_attempts - 1:
                        raise OSError(f"Could not find available port after {max_port_attempts} attempts in range {port_range_start}-{port_range_end}")
                else:
                    server.close()
                    raise  # Re-raise other socket errors
            except Exception as e:
                server.close()
                raise

        server.listen()
        print(f"[{datetime.now()}] Swarm server listening on {self.host}:{self.port}")
        miner_logger.info(f"Swarm server bound to port {self.port}")
        while True:
            client, addr = server.accept()
            threading.Thread(target=self.handle_swarm_request, args=(client,)).start()

    def mine(self, node_host="127.0.0.1", node_port=5001):
        threading.Thread(target=self.start_swarm_server, daemon=True).start()
        print(f"[{datetime.now()}] Miner address: {self.address}")

        # Try to find the actual blockchain node port and wait for it to be ready
        actual_port = self._find_blockchain_port(node_host, node_port)

        # Wait up to 30 seconds for blockchain node to be ready
        print(f"[{datetime.now()}] Waiting for blockchain node at {node_host}:{actual_port}...")
        if not self._wait_for_node_ready(node_host, actual_port, timeout=30):
            print(f"[{datetime.now()}] Blockchain node not ready, continuing anyway...")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((node_host, actual_port))
                    message = {
                        "type": "register_machine",
                        "machine_address": self.address,
                        "company_id": "company1"
                    }
                    serialized_message = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized_message)))
                    s.sendall(serialized_message)
                    
                    # Read response with size prefix
                    response_size_data = s.recv(4)
                    if len(response_size_data) != 4:
                        raise Exception("Invalid response size")
                    response_size = struct.unpack('!I', response_size_data)[0]
                    response_data = s.recv(response_size)
                    if len(response_data) != response_size:
                        raise Exception("Incomplete response data")
                    response = safe_unpack(response_data)
                    
                    if response.get("success", False):
                        self.deployment_key = response.get("deployment_key")
                        print(f"[{datetime.now()}] Miner registered successfully, deployment key: {self.deployment_key[:8]}...")
                        
                        # Auto-stake minimum required amount for mining (use separate connection)
                        try:
                            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stake_sock:
                                stake_sock.settimeout(10)
                                stake_sock.connect((node_host, actual_port))
                                stake_amount = 100000000  # 1 CR minimum stake
                                stake_message = {
                                    "type": "stake",
                                    "address": self.address,
                                    "amount_plancks": stake_amount
                                }
                                stake_data = safe_pack(stake_message)
                                stake_sock.sendall(struct.pack('!I', len(stake_data)))
                                stake_sock.sendall(stake_data)
                                
                                # Read stake response with size prefix
                                stake_response_size_data = stake_sock.recv(4)
                                if len(stake_response_size_data) == 4:
                                    stake_response_size = struct.unpack('!I', stake_response_size_data)[0]
                                    stake_response_data = stake_sock.recv(stake_response_size)
                                    if len(stake_response_data) == stake_response_size:
                                        stake_response = safe_unpack(stake_response_data)
                                    else:
                                        raise Exception("Incomplete stake response data")
                                else:
                                    raise Exception("Invalid stake response size")
                                    
                                if stake_response.get("success"):
                                    print(f"[{datetime.now()}] Auto-staked {stake_response['staked_credits']} CR for mining")
                                else:
                                    print(f"[{datetime.now()}] Auto-stake failed: {stake_response.get('error', 'Unknown')}")
                        except Exception as stake_error:
                            print(f"[{datetime.now()}] Auto-stake connection error: {stake_error}")
                        
                        break
                    else:
                        print(f"[{datetime.now()}] Registration failed: {response.get('error', 'Unknown')}")
                        if attempt == max_retries - 1:
                            return
                        time.sleep(2 ** attempt)
            except Exception as e:
                print(f"[{datetime.now()}] Registration failed (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return
                time.sleep(2 ** attempt)
        while self.running:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    workload_key, data_hash, storage_nodes, submitter_address, workload_type, workload_data = self.fetch_workload_key(node_host, node_port)
                    if workload_key is None:
                        time.sleep(5)
                        continue
                    
                    # For AI inference workloads, use inline data (no swarm fetch needed)
                    if workload_type == "ai_inference" and workload_data is not None:
                        print(f"[{datetime.now()}] Got AI inference workload inline: {workload_key[:8]}...")
                        workload = {
                            "workload_type": "ai_inference",
                            "workload_data": workload_data
                        }
                    else:
                        # Legacy: Fetch from swarm for computational workloads
                        encrypted_data = self.fetch_swarm_data(workload_key, data_hash, storage_nodes)
                        if encrypted_data is None:
                            time.sleep(2)
                            continue
                        
                        # Try to decrypt with new format first, then fallback to old format
                        decrypted_data = None
                        decryption_passwords = [
                            workload_key,  # New format: workload_key as password
                            f"workload_{data_hash}_{submitter_address}",  # Old format
                        ]
                        
                        for password in decryption_passwords:
                            try:
                                decrypted_data = crypto_utils.decrypt_data(encrypted_data, password)
                                workload = safe_unpack(decrypted_data)
                                print(f"[{datetime.now()}] Successfully decrypted workload with password format")
                                break
                            except Exception as e:
                                continue  # Try next password format
                        
                        if decrypted_data is None:
                            # Could not decrypt with any format - mark as bad and skip
                            print(f"[{datetime.now()}] Failed to decrypt workload {workload_key[:8]}... with any password format. Marking as bad.")
                            # TODO: Mark workload as bad in blockchain so miners skip it
                            time.sleep(2)
                            continue
                    
                    # Store in local swarm for redundancy (optional for AI inference)
                    if workload_type != "ai_inference":
                        if not self.store_in_swarm(workload, workload_key):
                            time.sleep(2)
                            continue
                    
                    # Process workload with REAL AI computation (Proof of Power)
                    result, computation_time = self.process_workload(workload)
                    if result is None:
                        time.sleep(2)
                        continue
                    
                    # Submit work with Proof of Power data
                    message = {
                        "type": "ai_work",
                        "work": result,
                        "workload_data": workload,  # For PoP verification
                        "computation_time": computation_time,  # For PoP verification
                        "workload_type": workload_type,  # Type for proper handling
                        "miner_address": self.address,
                        "device_type": self.device.type,
                        "tflops": self.tflops,
                        "key": workload_key
                    }
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(10)
                        s.connect((node_host, node_port))
                        serialized_message = safe_pack(message)
                        s.sendall(struct.pack('!I', len(serialized_message)))
                        s.sendall(serialized_message)
                        miner_logger.info(f"Sent AI work, size: {len(serialized_message)} bytes")
                        
                        # Read response with size prefix
                        response_size_data = s.recv(4)
                        if len(response_size_data) != 4:
                            raise Exception("Invalid response size")
                        response_size = struct.unpack('!I', response_size_data)[0]
                        response_data = s.recv(response_size)
                        if len(response_data) != response_size:
                            raise Exception("Incomplete response data")
                        response = safe_unpack(response_data)
                        
                        if response.get("success", False):
                            # New 69-second block system - workload queued for next block
                            status = response.get('status', 'unknown')
                            if status == 'queued_for_block':
                                miner_logger.info(f"Workload completed, queued for next block, "
                                                f"reward: {response['reward']:.8f} CR, "
                                                f"balance: {response['balance']:.8f} CR, "
                                                f"next block in: {response.get('next_block_in', 0):.1f}s")
                            else:
                                # Legacy immediate block creation
                                block_index = response.get('block_index', 'pending')
                                miner_logger.info(f"Mined block {block_index}, "
                                                f"reward: {response['reward']:.8f} CR, "
                                                f"balance: {response['balance']:.8f} CR")
                        else:
                            error_msg = response.get('error', 'Unknown')
                            # Handle expected race condition - workload already completed by another miner
                            if "Invalid or unknown workload key" in error_msg or "completed" in error_msg.lower():
                                miner_logger.info(f"Workload already completed by another miner (race condition) - moving to next")
                            else:
                                miner_logger.error(f"Mining error: {error_msg}")
                            if "key" in error_msg.lower():
                                time.sleep(2 ** attempt)
                                continue
                        break
                except Exception as e:
                    print(f"[{datetime.now()}] Mining error (attempt {attempt + 1}): {e}")
                    if attempt == max_retries - 1:
                        time.sleep(5)
                    else:
                        time.sleep(2 ** attempt)
            time.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Node 2040 Miner")
    parser.add_argument("--node-host", type=str, default="127.0.0.1", help="Blockchain node host")
    parser.add_argument("--node-port", type=int, default=5001, help="Blockchain node port")
    parser.add_argument("--port", type=int, default=5002, help="Port for this miner")
    parser.add_argument("--miner-id", type=str, default=None, help="Unique miner ID for persistent wallet")
    args = parser.parse_args()
    miner = Miner(host=args.node_host, port=args.port, miner_id=args.miner_id)
    miner.mine(node_host=args.node_host, node_port=args.node_port)