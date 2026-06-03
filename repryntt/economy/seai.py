"""DEPRECATED: Legacy PyTorch AI Processor

This component is DISABLED by default and no longer used.
Miners now perform real AI inference via llama.cpp server.

To re-enable (not recommended):
export SAIGE_AIS_PER_MACHINE=2
"""

import torch
import torch.nn as nn
import socket
# DEPRECATED — replaced by safe_serialize
# import pickle
from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack
import time
import argparse
import numpy as np
from datetime import datetime
from cryptography.fernet import Fernet
import hashlib
import struct
from repryntt.economy.crypto_utils import crypto_utils

class SelfEvolvingAI(nn.Module):
    def __init__(self, input_size=1000, hidden_size=100):
        super().__init__()
        print(f"[{datetime.now()}] WARNING: SelfEvolvingAI is deprecated. Miners handle AI inference via llama.cpp.")
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, 1)
        self.to(self.device)
        self.purpose_log = []
        print(f"[{datetime.now()}] Starting AI on {self.device}")
        # Backoff control to avoid hammering the node when idle
        self._no_workload_streak = 0

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        return self.fc2(x)

    def fetch_workload_key(self, node_host, node_port):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((node_host, node_port))
                    message = {"type": "get_workload_key"}
                    serialized_message = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized_message)))
                    s.sendall(serialized_message)
                    
                    # Receive response with size header
                    size_data = s.recv(4)
                    if len(size_data) < 4:
                        print(f"[{datetime.now()}] Invalid response size from {node_host}:{node_port}")
                        if attempt == max_retries - 1:
                            return None, None, None, None
                        time.sleep(2 ** attempt)
                        continue
                    
                    response_size = struct.unpack('!I', size_data)[0]
                    response_data = s.recv(response_size)
                    if len(response_data) != response_size:
                        print(f"[{datetime.now()}] Incomplete response from {node_host}:{node_port}")
                        if attempt == max_retries - 1:
                            return None, None, None, None
                        time.sleep(2 ** attempt)
                        continue
                    
                    response = safe_unpack(response_data)
                    if response.get("success", False):
                        # Reset no-workload state on success
                        self._no_workload_streak = 0
                        print(f"[{datetime.now()}] Fetched workload key: {response['key'][:8]}...")
                        return response["key"], response.get("data_hash"), response.get("storage_nodes"), response.get("purpose")
                    else:
                        err = response.get('error', 'Unknown')
                        if err in ("No workload keys available", "No unclaimed workloads available"):
                            # Expected idle state when no workloads exist; do NOT log
                            self._no_workload_streak += 1
                            backoff = min(60, 5 * (2 ** min(self._no_workload_streak, 4)))
                            time.sleep(backoff)
                        else:
                            print(f"[{datetime.now()}] Error fetching key: {err}")
                        if attempt == max_retries - 1:
                            return None, None, None, None
                        time.sleep(2 ** attempt)
            except Exception as e:
                print(f"[{datetime.now()}] Failed to fetch workload key (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return None, None, None, None
                time.sleep(2 ** attempt)
        return None, None, None, None

    def fetch_swarm_data(self, workload_key, data_hash, storage_nodes):
        max_retries = 3
        for node_host, node_port in storage_nodes:
            for attempt in range(max_retries):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(10)
                        s.connect((node_host, node_port))
                        message = {
                            "type": "fetch_swarm_data",
                            "workload_key": workload_key
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
                        response_data = s.recv(response_size)
                        if len(response_data) != response_size:
                            print(f"[{datetime.now()}] Incomplete response from {node_host}:{node_port}")
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                            continue
                        
                        response = safe_unpack(response_data)
                        if response.get("success", False):
                            encrypted_data = response["data"]
                            computed_hash = hashlib.sha3_512(encrypted_data).hexdigest()
                            if computed_hash != data_hash:
                                print(f"[{datetime.now()}] Data hash mismatch at {node_host}:{node_port}")
                                continue
                            print(f"[{datetime.now()}] Fetched workload data from {node_host}:{node_port}, key={workload_key[:8]}...")
                            return encrypted_data
                        else:
                            print(f"[{datetime.now()}] Fetch error at {node_host}:{node_port}: {response.get('error', 'Unknown')}")
                            if attempt == max_retries - 1:
                                continue
                            time.sleep(2 ** attempt)
                except Exception as e:
                    print(f"[{datetime.now()}] Fetch error at {node_host}:{node_port} (attempt {attempt + 1}): {e}")
                    if attempt == max_retries - 1:
                        continue
                    time.sleep(2 ** attempt)
        print(f"[{datetime.now()}] Failed to fetch workload data for key={workload_key[:8]}...")
        return None

    def process_workload(self, workload_data, purpose):
        """
        Process AI workload with REAL neural network computation.
        
        This is actual AI work that contributes to the network (Proof of Power).
        """
        try:
            start_time = time.time()
            
            with torch.no_grad():
                input_data = torch.tensor(workload_data, dtype=torch.float32, device=self.device)
                chunk_size = self.fc1.in_features
                processed_data = []
                for i in range(0, input_data.shape[0], chunk_size):
                    chunk = input_data[i:i+chunk_size, :chunk_size]
                    if chunk.shape[0] == 0:
                        break
                    output = self.forward(chunk)
                    processed_data.append(output.cpu().numpy().flatten())
                processed_data = np.concatenate(processed_data)[:64000]
                
                computation_time = time.time() - start_time
                
                self.purpose_log.append({
                    "purpose": purpose,
                    "timestamp": time.time(),
                    "size_mb": input_data.nbytes/1024/1024,
                    "computation_time": computation_time
                })
                
                print(f"[{datetime.now()}] Processed workload: purpose={purpose}, "
                      f"size: {input_data.nbytes/1024/1024:.2f}MB, "
                      f"output_size: {len(processed_data)}, "
                      f"time: {computation_time:.2f}s")
                
                return processed_data.tolist(), computation_time
        
        except Exception as e:
            print(f"[{datetime.now()}] Error processing workload: {e}")
            return None, 0.0

    def run(self, node_host="127.0.0.1", node_port=5001):
        while True:
            try:
                workload_key, data_hash, storage_nodes, purpose = self.fetch_workload_key(node_host, node_port)
                if workload_key is None:
                    time.sleep(5)
                    continue
                encrypted_data = self.fetch_swarm_data(workload_key, data_hash, storage_nodes)
                if encrypted_data is None:
                    time.sleep(5)
                    continue

                # Decrypt using the same key as encryption (workload_key)
                decryption_key = workload_key
                try:
                    decrypted_data = crypto_utils.decrypt_data(encrypted_data, decryption_key)
                    workload = safe_unpack(decrypted_data)
                except Exception as e:
                    print(f"[{datetime.now()}] Decryption failed for workload {workload_key[:8]}...: {e}")
                    time.sleep(5)
                    continue
                result, computation_time = self.process_workload(workload, purpose)
                if result is None:
                    time.sleep(5)
                    continue
                
                print(f"[{datetime.now()}] Completed AI workload: {purpose[:50]}... "
                      f"(time: {computation_time:.2f}s)")
                
                # In production, this would submit the result back to blockchain with PoP
                # For now, we log the computational contribution
                time.sleep(10)
            except Exception as e:
                print(f"[{datetime.now()}] AI loop error: {e}")
                time.sleep(5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Node 2040 Self-Evolving AI")
    parser.add_argument("--node-host", type=str, default="127.0.0.1", help="Blockchain node host")
    parser.add_argument("--node-port", type=int, default=5001, help="Blockchain node port")
    args = parser.parse_args()
    ai = SelfEvolvingAI()
    ai.run(node_host=args.node_host, node_port=args.node_port)