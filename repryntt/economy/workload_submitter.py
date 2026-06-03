"""DEPRECATED: Dummy Workload Submitter

This component is DISABLED by default.
The brain_system now submits real AI inference workloads directly to the blockchain.

To re-enable for testing (not recommended):
export SAIGE_DUMMY_SUBMITTERS=1
"""

import socket
# DEPRECATED — replaced by safe_serialize
# import pickle
from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack
import numpy as np
import time
import hashlib
import random
import argparse
import struct
import os
import secrets
import json
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.fernet import Fernet
from repryntt.economy.crypto_utils import crypto_utils

class WorkloadSubmitter:
    def __init__(self, host="127.0.0.1", port=5001, machine_id="machine1"):
        print(f"[{datetime.now()}] WARNING: WorkloadSubmitter is deprecated. Brain system submits real workloads.")
        self.host = host
        self.port = port
        self.machine_id = machine_id
        self.plancks_per_credit = 100000000
        self.swarm_dir = "swarm_storage"
        self.wallet_dir = "wallets"
        os.makedirs(self.swarm_dir, exist_ok=True)
        os.makedirs(self.wallet_dir, exist_ok=True)

        # Try to load existing wallet, create new one only if none exists
        self.address, self.key_phrase = self._load_or_create_wallet()
        self.deployment_key = None
        self.swarm_nodes = [("127.0.0.1", 5021), ("127.0.0.1", 5022), ("127.0.0.1", 5023), ("127.0.0.1", 5024), ("127.0.0.1", 5025)]
        print(f"[{datetime.now()}] Workload initiator initialized for {self.machine_id}, address: {self.address}")
        if self.key_phrase:
            print(f"[{datetime.now()}] Key phrase loaded (SAVE SECURELY): {' '.join(self.key_phrase)}")
        else:
            print(f"[{datetime.now()}] Wallet loaded for {self.machine_id} (key phrase not available)")

    def _load_or_create_wallet(self):
        """Load existing wallet for this submitter, or create new one if none exists"""
        try:
            # Look for existing wallet file for this machine ID
            wallet_file = os.path.join(self.wallet_dir, f"{self.machine_id}.json")

            if os.path.exists(wallet_file):
                # Load existing wallet
                with open(wallet_file, "r") as f:
                    wallet_data = json.load(f)

                address = wallet_data['address']
                # For existing wallets, we may not have the key phrase stored securely
                # Return address and None for key_phrase (indicating it was previously created)
                print(f"[{datetime.now()}] Loaded existing wallet for {self.machine_id}: {address}")
                return address, None
            else:
                # Create new wallet
                print(f"[{datetime.now()}] Creating new wallet for {self.machine_id}...")
                return self._create_new_wallet()

        except Exception as e:
            print(f"[{datetime.now()}] Wallet loading error for {self.machine_id}: {e}")
            # Fallback to creating new wallet
            return self._create_new_wallet()

    def _create_new_wallet(self):
        """Create a new wallet for this submitter"""
        try:
            # Use secure crypto_utils for wallet generation
            address, key_phrase = crypto_utils.generate_wallet_seed()

            # Store encrypted key phrase securely
            wallet_password = secrets.token_hex(32)
            encrypted_phrase = crypto_utils.encrypt_data(key_phrase.encode(), wallet_password)

            wallet_data = {
                'address': address,
                'encrypted_phrase': encrypted_phrase.decode(),
                'encryption_key': wallet_password,
                'salt': secrets.token_hex(16),
                'created': datetime.now().isoformat(),
                'machine_id': self.machine_id
            }

            wallet_file = os.path.join(self.wallet_dir, f"{self.machine_id}.json")
            with open(wallet_file, "w") as f:
                json.dump(wallet_data, f)

            print(f"[{datetime.now()}] Secure wallet created for {self.machine_id}: address={address}")
            return address, key_phrase.split()
        except Exception as e:
            print(f"[{datetime.now()}] Wallet creation error for {self.machine_id}: {e}")
            raise

    def generate_key(self):
        nonce = random.randint(0, 1000000)
        data = f"{time.time()}{self.machine_id}.{nonce}".encode()
        digest = hashes.Hash(hashes.SHA3_256())
        digest.update(data)
        key = digest.finalize().hex()
        print(f"[{datetime.now()}] Generated workload key: {key[:8]}...")
        return key
    
    def generate_key_from_workload(self, workload):
        """Generate a deterministic key from workload content"""
        packed = safe_pack(workload)
        digest = hashes.Hash(hashes.SHA3_256())
        digest.update(packed)
        key = digest.finalize().hex()
        return key

    def encrypt_data(self, data, encryption_password=None):
        try:
            # If no password provided, derive from data content (for deterministic encryption)
            if encryption_password is None:
                encryption_password = crypto_utils.hash_data(safe_pack(data), 'sha3_256')
            return crypto_utils.encrypt_data(safe_pack(data), encryption_password)
        except Exception as e:
            print(f"[{datetime.now()}] Data encryption error: {e}")
            raise

    def generate_workload(self):
        """Generate workloads for AI processing (simplified to avoid deadlocks)"""
        try:
            # Generate meaningful computational and AI reasoning workloads
            # Avoid importing BrainSystem to prevent threading deadlocks
            workload_candidates = []
            
            # Generate diverse AI reasoning tasks
            import random
            import uuid
            
            reasoning_tasks = [
                {
                    'type': 'logical_reasoning',
                    'task': 'Analyze this logical puzzle: If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly?',
                    'context': 'Evaluate the logical validity of this syllogism and explain your reasoning',
                    'priority': 'high'
                },
                {
                    'type': 'pattern_recognition',
                    'task': 'Find the pattern: 2, 6, 12, 20, 30, 42, ?',
                    'context': 'Identify the underlying pattern and predict the next number. Explain the mathematical relationship.',
                    'priority': 'medium'
                },
                {
                    'type': 'creative_problem_solving',
                    'task': 'Design an algorithm to efficiently sort 1 million numbers using minimal memory',
                    'context': 'Consider time complexity, space complexity, and practical implementation constraints',
                    'priority': 'high'
                },
                {
                    'type': 'text_analysis',
                    'task': 'Analyze the sentiment and key themes in this text: "The future of AI lies not in replacing human intelligence, but in augmenting it"',
                    'context': 'Identify implicit assumptions, rhetorical devices, and philosophical implications',
                    'priority': 'medium'
                },
                {
                    'type': 'mathematical_reasoning',
                    'task': 'Prove why the sum of any two even numbers is always even',
                    'context': 'Provide a formal mathematical proof using basic number theory',
                    'priority': 'medium'
                }
            ]
            
            # Select random tasks
            workload_candidates.extend(random.sample(reasoning_tasks, min(3, len(reasoning_tasks))))
            
            # Fallback to simple computational task if somehow none were selected
            if not workload_candidates:
                workload_candidates.append({
                    'type': 'computational_task',
                    'task': 'Calculate the sum of squares from 1 to 1000',
                    'context': "Basic computational workload",
                    'priority': 'low'
                })

            # Select the highest priority workload
            if workload_candidates:
                # Sort by priority (high > medium > low)
                priority_order = {'high': 3, 'medium': 2, 'low': 1}
                workload_candidates.sort(key=lambda x: priority_order.get(x.get('priority', 'low'), 0), reverse=True)
                selected_workload = workload_candidates[0]

                # Format as AI prompt for miners to process
                ai_prompt = f"""
TASK TYPE: {selected_workload['type'].upper()}
PRIORITY: {selected_workload.get('priority', 'medium')}

TASK: {selected_workload['task']}

CONTEXT:
{selected_workload.get('context', 'No additional context provided')}

INSTRUCTIONS:
Provide a thoughtful, detailed response to this task. If this involves research, analysis, or problem-solving, show your reasoning process. If this is a computational task, provide the result and explain your approach.

Your response will be used to advance SAIGE's cognitive development and earn credits in the robot economy.
"""
                
                workload = {
                    'type': selected_workload['type'],
                    'task': ai_prompt,
                    'difficulty': priority_order.get(selected_workload.get('priority', 'low'), 0),
                    'timestamp': datetime.now().isoformat(),
                    'unique_id': str(uuid.uuid4()),  # Add unique ID to make each workload unique
                    'data': np.random.randn(4, 100).tolist()
                }

                print(f"[{datetime.now()}] Generated real AI workload: {selected_workload['type']} (high priority)")
                return workload

                print(f"[{datetime.now()}] Generated real AI workload: {selected_workload['type']} ({selected_workload.get('priority', 'medium')} priority)")
                return ai_prompt.strip()

        except Exception as e:
            print(f"[{datetime.now()}] Failed to generate real AI workload, falling back to computational: {e}")

        # Fallback: Generate simple computational workload
        return {
            'type': 'fallback_computation',
            'task': 'Analyze this dataset and find patterns',
            'data': np.random.randn(4, 100).tolist(),
            'context': 'Computational workload fallback - analyze the provided numerical data'
        }

    def store_in_swarm(self, workload, workload_key, encryption_password=None):
        encrypted_data = self.encrypt_data(workload, encryption_password=encryption_password)
        data_hash = hashlib.sha3_512(encrypted_data).hexdigest()
        file_path = os.path.join(self.swarm_dir, f"{workload_key}.dat")
        with open(file_path, "wb") as f:
            f.write(encrypted_data)
        print(f"[{datetime.now()}] Stored workload locally: key={workload_key[:8]}..., hash={data_hash[:8]}..., path={file_path}")
        return data_hash, file_path

    def check_swarm_nodes(self, nodes):
        available_nodes = []
        for node_host, node_port in nodes:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect((node_host, node_port))
                    available_nodes.append((node_host, node_port))
                    print(f"[{datetime.now()}] Swarm node available: {node_host}:{node_port}")
            except Exception as e:
                print(f"[{datetime.now()}] Swarm node unavailable: {node_host}:{node_port}, error: {e}")
        return available_nodes

    def broadcast_storage_nodes(self, workload_key, workload, selected_nodes):
        """Broadcast RAW (unencrypted) workload to swarm nodes"""
        max_retries = 3
        stored_count = 0
        for node_host, node_port in selected_nodes:
            for attempt in range(max_retries):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(10)
                        s.connect((node_host, node_port))
                        message = {
                            "type": "store_swarm_data",
                            "workload_key": workload_key,
                            "data": workload  # Send RAW workload, not encrypted - miner will encrypt it
                        }
                        serialized_message = safe_pack(message)
                        s.sendall(struct.pack('!I', len(serialized_message)))
                        s.sendall(serialized_message)
                        
                        # Receive response with size header
                        size_data = s.recv(4)
                        if len(size_data) < 4:
                            print(f"[{datetime.now()}] Invalid response size from {node_host}:{node_port}")
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                            continue
                        
                        response_size = struct.unpack('!I', size_data)[0]
                        response_data = b''
                        remaining = response_size
                        while remaining > 0:
                            chunk = s.recv(min(4096, remaining))
                            if not chunk:
                                break
                            response_data += chunk
                            remaining -= len(chunk)
                        
                        if len(response_data) != response_size:
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                            continue
                        
                        response = safe_unpack(response_data)
                        if response.get("success", False):
                            stored_count += 1
                            print(f"[{datetime.now()}] Stored workload at {node_host}:{node_port}, key={workload_key[:8]}...")
                            break
                        else:
                            print(f"[{datetime.now()}] Storage error at {node_host}:{node_port}: {response.get('error', 'Unknown')}")
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                except Exception as e:
                    print(f"[{datetime.now()}] Storage error at {node_host}:{node_port} (attempt {attempt + 1}): {e}")
                    if attempt == max_retries - 1:
                        continue
                    time.sleep(2 ** attempt)
        if stored_count >= len(selected_nodes):
            return True
        print(f"[{datetime.now()}] Failed to store workload in swarm: Only {stored_count}/{len(selected_nodes)} nodes successful")
        return False

    def submit_workload(self, node_host="127.0.0.1", node_port=5001, purpose="AI training - test"):
        max_retries = 3
        # Register once per submitter lifecycle (not every workload)
        if not self.deployment_key:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((node_host, node_port))
                    message = {
                        "type": "register_machine",
                        "machine_address": self.address,
                        "company_id": "company1"
                    }
                    serialized_message = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized_message)))
                    s.sendall(serialized_message)
                    
                    # Read response length
                    length_bytes = s.recv(4)
                    if len(length_bytes) < 4:
                        print(f"[{datetime.now()}] Registration failed: Incomplete response")
                        return False
                    length = struct.unpack('!I', length_bytes)[0]
                    
                    # Read response data
                    response_data = b''
                    while len(response_data) < length:
                        packet = s.recv(min(length - len(response_data), 4096))
                        if not packet:
                            print(f"[{datetime.now()}] Registration failed: Connection closed")
                            return False
                        response_data += packet
                    
                    response = safe_unpack(response_data)
                    if not response.get("success", False):
                        print(f"[{datetime.now()}] Registration failed: {response.get('error', 'Unknown')}")
                        return False
                    self.deployment_key = response.get("deployment_key")
                    print(f"[{datetime.now()}] Machine registered successfully, deployment key: {self.deployment_key[:8]}...")
            except Exception as e:
                print(f"[{datetime.now()}] Registration failed: {e}")
                return False
        
        # Submit ONE workload (not infinite loop)
        for attempt in range(max_retries):
            try:
                available_nodes = self.check_swarm_nodes(self.swarm_nodes)
                print(f"[{datetime.now()}] Swarm check: {len(available_nodes)} nodes available out of {len(self.swarm_nodes)}")
                
                # Allow submitting even without swarm nodes (they're optional for AI inference workloads)
                # Continue with submission regardless of swarm node availability
                workload = self.generate_workload()
                # Generate key from workload content (deterministic) so it can be used as encryption password
                workload_key = self.generate_key_from_workload(workload)
                print(f"[{datetime.now()}] Generated workload key from content: {workload_key[:8]}...")
                data_hash, local_path = self.store_in_swarm(workload, workload_key, encryption_password=workload_key)
                selected_nodes = random.sample(available_nodes, min(3, len(available_nodes))) if available_nodes else []
                if len(selected_nodes) > 0 and not self.broadcast_storage_nodes(workload_key, workload, selected_nodes):
                    time.sleep(2 ** attempt)
                    continue
                message = {
                    "type": "submit_workload",
                    "machine_address": self.address,
                    "workload_key": workload_key,
                    "purpose": purpose,
                    "data_hash": data_hash,
                    "storage_nodes": selected_nodes
                }
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((node_host, node_port))
                    serialized_message = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized_message)))
                    s.sendall(serialized_message)
                    print(f"[{datetime.now()}] Submitted workload: key={workload_key[:8]}..., purpose={purpose}, size={len(serialized_message)} bytes")
                    
                    # Read response length
                    length_bytes = s.recv(4)
                    if len(length_bytes) < 4:
                        print(f"[{datetime.now()}] Submission failed: Incomplete response")
                        time.sleep(2 ** attempt)
                        continue
                    length = struct.unpack('!I', length_bytes)[0]
                    
                    # Read response data
                    response_data = b''
                    while len(response_data) < length:
                        packet = s.recv(min(length - len(response_data), 4096))
                        if not packet:
                            print(f"[{datetime.now()}] Submission failed: Connection closed")
                            time.sleep(2 ** attempt)
                            continue
                        response_data += packet
                    
                    response = safe_unpack(response_data)
                    if response["success"]:
                        print(f"[{datetime.now()}] Workload accepted")
                        return True
                    else:
                        print(f"[{datetime.now()}] Submission error: {response.get('error', 'Unknown')}")
                        if "key" in response.get("error", "").lower():
                            time.sleep(2 ** attempt)
                            continue
                    return response["success"]
            except Exception as e:
                print(f"[{datetime.now()}] Error submitting workload (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        
        return False  # Failed after all retries

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Node 2040 Workload Submitter")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Node host")
    parser.add_argument("--port", type=int, default=5001, help="Node port")
    parser.add_argument("--machine-id", type=str, default="machine1", help="Machine identifier")
    args = parser.parse_args()

    submitter = WorkloadSubmitter(host=args.host, port=args.port, machine_id=args.machine_id)
    submitter.submit_workload(node_host=args.host, node_port=args.port)