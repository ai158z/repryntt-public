import hashlib
import time
import pickle
import threading
import json
import os
from datetime import datetime

class WorkloadContract:
    def __init__(self):
        try:
            self.workloads = {}
            self.valid_keys = set()
            self.deployment_keys = {}
            self.plancks_per_credit = 100000000
            self.fee = 1000000  # 0.01 Credits
            self.reward = 10000000  # 0.1 Credits
            
            # AI inference workload results storage
            self.workload_results = {}  # Store completed workload results
            
            # Workload claiming system (first-come-first-served)
            # Format: {workload_key: {"miner": address, "claimed_at": timestamp}}
            self.claimed_workloads = {}
            self.claim_timeout = 60  # Seconds before claim expires and returns to pool

            # Thread safety
            self.lock = threading.Lock()

            # Stable data path (next to this module)
            _data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'robot_economy_data')
            os.makedirs(_data_dir, exist_ok=True)
            self._state_file = os.path.join(_data_dir, 'contract_state.json')

            # Load persisted state
            self.load_state()

            print(f"[{datetime.now()}] Workload contract initialized with {len(self.workloads)} workloads")
        except Exception as e:
            print(f"[{datetime.now()}] Contract initialization failed: {e}")
            raise

    def claim_workload(self, workload_key, miner_address):
        """Claim a workload for exclusive processing (first-come-first-served)
        
        Args:
            workload_key: Key of workload to claim
            miner_address: Address of miner claiming the workload
            
        Returns:
            dict with success status and workload data if claimed
        """
        with self.lock:
            try:
                # Clean up expired claims first
                current_time = time.time()
                expired_claims = [k for k, v in self.claimed_workloads.items() 
                                 if current_time - v["claimed_at"] > self.claim_timeout]
                for expired_key in expired_claims:
                    print(f"[{datetime.now()}] Claim expired for {expired_key[:8]}... (>{self.claim_timeout}s), returning to pool")
                    del self.claimed_workloads[expired_key]
                
                # Check if workload exists and is available
                if workload_key not in self.valid_keys:
                    return {"success": False, "error": "Workload not available"}
                
                # Check if already claimed by another miner
                if workload_key in self.claimed_workloads:
                    claim_info = self.claimed_workloads[workload_key]
                    if claim_info["miner"] != miner_address:
                        return {"success": False, "error": "Workload already claimed by another miner"}
                    # Already claimed by this miner, allow re-fetch
                    return {"success": True, "workload": self.workloads[workload_key]}
                
                # Claim the workload
                self.claimed_workloads[workload_key] = {
                    "miner": miner_address,
                    "claimed_at": current_time
                }
                
                print(f"[{datetime.now()}] 🎯 Workload {workload_key[:8]}... claimed by {miner_address[:8]}...")
                return {"success": True, "workload": self.workloads[workload_key]}
                
            except Exception as e:
                print(f"[{datetime.now()}] Claim error: {e}")
                return {"success": False, "error": str(e)}
    
    def release_claim(self, workload_key):
        """Release a workload claim (on completion or failure)"""
        with self.lock:
            if workload_key in self.claimed_workloads:
                del self.claimed_workloads[workload_key]
                print(f"[{datetime.now()}] Released claim on {workload_key[:8]}...")
    
    def get_unclaimed_workload(self):
        """Get a random unclaimed workload key
        
        Returns:
            workload_key if available, None otherwise
        """
        with self.lock:
            # Clean up expired claims
            current_time = time.time()
            expired_claims = [k for k, v in self.claimed_workloads.items() 
                             if current_time - v["claimed_at"] > self.claim_timeout]
            for expired_key in expired_claims:
                del self.claimed_workloads[expired_key]
            
            # Find unclaimed workloads
            unclaimed = [k for k in self.valid_keys if k not in self.claimed_workloads]
            
            if unclaimed:
                import random
                return random.choice(unclaimed)
            return None

    def validate_key(self, key, blockchain=None):
        try:
            is_valid_format = isinstance(key, str) and len(key) == 64 and all(c in '0123456789abcdef' for c in key)
            if not is_valid_format:
                print(f"[{datetime.now()}] Validating key {key[:8] if key else 'None'}...: Invalid format")
                return False
            if key in self.valid_keys:
                print(f"[{datetime.now()}] Validating key {key[:8]}...: Found in valid_keys")
                return True
            if blockchain:
                for block in blockchain.chain:
                    if block.data.get("type") == "ai_work" and block.data.get("key") == key:
                        if block.data.get("status") == "pending":
                            print(f"[{datetime.now()}] Validating key {key[:8]}...: Found in blockchain")
                            return True
            print(f"[{datetime.now()}] Validating key {key[:8]}...: Not found")
            return False
        except Exception as e:
            print(f"[{datetime.now()}] Key validation error: {e}")
            return False

    def validate_purpose(self, purpose):
        try:
            return isinstance(purpose, str) and len(purpose) <= 200
        except Exception as e:
            print(f"[{datetime.now()}] Purpose validation error: {e}")
            return False

    def submit_workload(self, machine_address, workload_key, purpose, data_hash, storage_nodes, balances, workload_type="computational"):
        """Submit a workload to the contract
        
        Args:
            machine_address: Address of the machine submitting
            workload_key: Unique key for the workload
            purpose: Description of the workload
            data_hash: Hash of the workload data
            storage_nodes: List of nodes storing the workload data
            balances: Current balance state
            workload_type: Type of workload ('computational' or 'ai_inference')
        """
        with self.lock:
            try:
                print(f"[{datetime.now()}] Attempting workload submission: key={workload_key[:8] if workload_key else 'None'}..., type={workload_type}, machine={machine_address[:8] if machine_address else 'None'}...")
                # Check key format instead of existence
                if not isinstance(workload_key, str) or len(workload_key) != 64 or not all(c in '0123456789abcdef' for c in workload_key):
                    print(f"[{datetime.now()}] Submission failed: Invalid workload key format")
                    return {"success": False, "error": "Invalid workload key"}
                if workload_key in self.valid_keys:
                    print(f"[{datetime.now()}] Submission failed: Duplicate workload key")
                    return {"success": False, "error": "Duplicate workload key"}
                if not self.validate_purpose(purpose):
                    print(f"[{datetime.now()}] Submission failed: Invalid purpose")
                    return {"success": False, "error": "Invalid purpose (max 200 chars)"}
                if machine_address not in self.deployment_keys:
                    print(f"[{datetime.now()}] Submission failed: Machine not registered")
                    return {"success": False, "error": "Machine not registered"}
                if balances.get(machine_address, 0) < self.fee:
                    print(f"[{datetime.now()}] Submission failed: Insufficient balance")
                    return {"success": False, "error": "Insufficient balance for fee"}
                if not isinstance(storage_nodes, list) or len(storage_nodes) < 0:
                    print(f"[{datetime.now()}] Submission failed: Invalid storage nodes")
                    return {"success": False, "error": "Storage nodes must be a list"}
                
                self.workloads[workload_key] = {
                    "machine_address": machine_address,
                    "purpose": purpose,
                    "data_hash": data_hash,
                    "status": "pending",
                    "storage_nodes": storage_nodes,
                    "workload_type": workload_type,  # NEW: Track workload type
                    "submitted_at": time.time()
                }
                self.valid_keys.add(workload_key)
                balances[machine_address] -= self.fee
                balances["dao"] = balances.get("dao", 0) + self.fee
                print(f"[{datetime.now()}] Workload submitted: key={workload_key[:8]}..., type={workload_type}, purpose={purpose}, fee={self.fee/self.plancks_per_credit:.8f} CR, stored at {len(storage_nodes)} nodes")
                return {"success": True, "workload_key": workload_key}
            except Exception as e:
                print(f"[{datetime.now()}] Workload submission error: {e}")
                return {"success": False, "error": f"Submission failed: {e}"}

    def complete_workload(self, workload_key, miner_address, result, balances):
        """Complete a workload and store the result
        
        Args:
            workload_key: Unique key for the workload
            miner_address: Address of the miner completing the workload
            result: Result of the computation (can be dict for AI inference)
            balances: Current balance state
        """
        with self.lock:
            try:
                print(f"[{datetime.now()}] Attempting workload completion: key={workload_key[:8] if workload_key else 'None'}..., miner={miner_address[:8] if miner_address else 'None'}...")
                if workload_key not in self.workloads or workload_key not in self.valid_keys:
                    print(f"[{datetime.now()}] Completion failed: Unknown or completed workload key")
                    return {"success": False, "error": "Unknown or completed workload key"}
                if self.workloads[workload_key]["status"] != "pending":
                    print(f"[{datetime.now()}] Completion failed: Workload not pending")
                    return {"success": False, "error": "Workload not pending"}
                
                # Store result for retrieval (especially important for AI inference)
                workload_type = self.workloads[workload_key].get("workload_type", "computational")
                self.workload_results[workload_key] = {
                    "result": result,
                    "miner_address": miner_address,
                    "completed_at": time.time(),
                    "workload_type": workload_type
                }
                
                self.workloads[workload_key]["status"] = "completed"
                self.valid_keys.discard(workload_key)
                
                # Release the claim on this workload
                if workload_key in self.claimed_workloads:
                    del self.claimed_workloads[workload_key]
                
                del self.workloads[workload_key]  # Remove completed workload from pending list
                balances[miner_address] = balances.get(miner_address, 0) + self.reward
                print(f"[{datetime.now()}] Workload completed: key={workload_key[:8]}..., type={workload_type}, reward={self.reward/self.plancks_per_credit:.8f} CR to {miner_address}")
                return {"success": True, "reward": self.reward/self.plancks_per_credit}
            except Exception as e:
                print(f"[{datetime.now()}] Workload completion error: {e}")
                return {"success": False, "error": f"Completion failed: {e}"}

    def register_machine(self, machine_address, deployment_key):
        try:
            self.deployment_keys[machine_address] = deployment_key
            print(f"[{datetime.now()}] Machine registered: {machine_address}, deployment key={deployment_key[:8]}...")
            return {"success": True, "deployment_key": deployment_key}
        except Exception as e:
            print(f"[{datetime.now()}] Machine registration error: {e}")
            return {"success": False, "error": f"Registration failed: {e}"}
    
    def submit_ai_inference_workload(self, requester_address, prompt, max_tokens, temperature, fee_plancks, balances):
        """Submit an AI inference request as a blockchain workload
        
        Args:
            requester_address: Address requesting the inference
            prompt: AI prompt text
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            fee_plancks: Fee in plancks (100000000 plancks = 1 Credit)
            balances: Current balance state
            
        Returns:
            dict with success status and workload_key
        """
        # SECURITY: Enforce prompt size limit (10 KB max) to prevent OOM/DoS
        MAX_PROMPT_SIZE = 10 * 1024
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_SIZE:
            return {"success": False, "error": f"Prompt too large (max {MAX_PROMPT_SIZE // 1024} KB)"}
        if not prompt.strip():
            return {"success": False, "error": "Empty prompt"}
        # Clamp max_tokens to reasonable range
        max_tokens = max(1, min(int(max_tokens or 512), 4096))
        with self.lock:
            try:
                # Generate deterministic workload key from prompt
                import hashlib
                workload_key = hashlib.sha3_256(
                    f"{prompt}{time.time()}{requester_address}".encode()
                ).hexdigest()
                
                # Check requester has sufficient balance
                if balances.get(requester_address, 0) < fee_plancks:
                    return {"success": False, "error": "Insufficient balance for AI inference fee"}
                
                # Store workload data
                workload_data = {
                    "type": "ai_inference",
                    "prompt": prompt,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "requester": requester_address,
                    "fee": fee_plancks,
                    "timestamp": time.time()
                }
                
                # Add to workloads
                self.workloads[workload_key] = {
                    "machine_address": requester_address,
                    "purpose": f"AI Inference: {prompt[:50]}...",
                    "data_hash": hashlib.sha3_256(json.dumps(workload_data).encode()).hexdigest(),
                    "status": "pending",
                    "storage_nodes": [],  # AI workloads don't need swarm storage
                    "workload_type": "ai_inference",
                    "workload_data": workload_data,  # Store inline for quick access
                    "submitted_at": time.time()
                }
                
                self.valid_keys.add(workload_key)
                
                # Charge fee
                balances[requester_address] -= fee_plancks
                balances["dao"] = balances.get("dao", 0) + fee_plancks
                
                print(f"[{datetime.now()}] AI inference workload submitted: key={workload_key[:8]}..., prompt_len={len(prompt)}, fee={fee_plancks/self.plancks_per_credit:.4f} CR")
                
                return {
                    "success": True,
                    "workload_key": workload_key,
                    "status": "pending"
                }
                
            except Exception as e:
                print(f"[{datetime.now()}] AI inference workload submission error: {e}")
                return {"success": False, "error": str(e)}
    
    def get_workload_result(self, workload_key, timeout=120):
        """Get the result of a completed workload (blocking with timeout)
        
        Args:
            workload_key: Unique key for the workload
            timeout: Maximum seconds to wait for completion
            
        Returns:
            dict with result or error
        """
        start_time = time.time()
        
        # Clean up old results (older than 24 hours) to prevent bloat
        current_time = time.time()
        with self.lock:
            old_keys = [k for k, v in self.workload_results.items() 
                       if current_time - v.get('completed_at', 0) > 86400]
            for old_key in old_keys:
                del self.workload_results[old_key]
            if old_keys:
                print(f"[{datetime.now()}] Cleaned up {len(old_keys)} old workload results (>24h)")
        
        while time.time() - start_time < timeout:
            with self.lock:
                # Check if result is available
                if workload_key in self.workload_results:
                    result_data = self.workload_results[workload_key]
                    print(f"[{datetime.now()}] Retrieved workload result: key={workload_key[:8]}...")
                    return {
                        "success": True,
                        "result": result_data["result"],
                        "miner_address": result_data["miner_address"],
                        "completed_at": result_data["completed_at"]
                    }
                
                # Check if still pending
                if workload_key in self.valid_keys:
                    # Still processing, continue waiting
                    pass
                else:
                    # Not found and not pending - may have failed or been removed
                    return {
                        "success": False,
                        "error": "Workload not found or was removed"
                    }
            
            time.sleep(1)  # Poll every second
        
        # Timeout reached
        return {
            "success": False,
            "error": f"Timeout waiting for workload completion ({timeout}s)"
        }

    def update_deployment_key(self, machine_address, new_key):
        try:
            if machine_address not in self.deployment_keys:
                print(f"[{datetime.now()}] Key update failed: Machine not registered")
                return {"success": False, "error": "Machine not registered"}
            if not self.validate_key(new_key):
                print(f"[{datetime.now()}] Key update failed: Invalid new deployment key")
                return {"success": False, "error": "Invalid new deployment key"}
            old_key = self.deployment_keys[machine_address]
            self.deployment_keys[machine_address] = new_key
            print(f"[{datetime.now()}] Deployment key updated for {machine_address}: {old_key[:8]}... -> {new_key[:8]}...")
            return {"success": True, "notification": f"Deployment key updated to {new_key}"}
        except Exception as e:
            print(f"[{datetime.now()}] Key update error: {e}")
            return {"success": False, "error": f"Key update failed: {e}"}

    def reset_valid_keys(self):
        try:
            completed_keys = [k for k, v in self.workloads.items() if v["status"] == "completed"]
            for key in completed_keys:
                self.valid_keys.discard(key)
                self.workloads.pop(key, None)
            print(f"[{datetime.now()}] Cleared {len(completed_keys)} completed workload keys")

            # Save state after cleanup
            self.save_state()
        except Exception as e:
            print(f"[{datetime.now()}] Error resetting valid keys: {e}")

    def save_state(self):
        """Persist contract state to disk"""
        try:
            state_data = {
                "workloads": self.workloads,
                "valid_keys": list(self.valid_keys),
                "deployment_keys": self.deployment_keys,
                "workload_results": self.workload_results,  # FIXED: Persist results
                "fee": self.fee,
                "reward": self.reward,
                "last_saved": time.time()
            }

            with open(self._state_file, "w") as f:
                json.dump(state_data, f, indent=2)

            print(f"[{datetime.now()}] Contract state saved: {len(self.workloads)} workloads, {len(self.valid_keys)} valid keys, {len(self.workload_results)} results")
        except Exception as e:
            print(f"[{datetime.now()}] Contract state save error: {e}")

    def load_state(self):
        """Load contract state from disk"""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    state_data = json.load(f)

                self.workloads = state_data.get("workloads", {})
                self.valid_keys = set(state_data.get("valid_keys", []))
                self.deployment_keys = state_data.get("deployment_keys", {})
                self.workload_results = state_data.get("workload_results", {})  # FIXED: Load results
                self.fee = state_data.get("fee", 1000000)
                self.reward = state_data.get("reward", 10000000)

                # Rebuild valid_keys from workloads with pending status to ensure consistency
                self.valid_keys = {key for key, workload in self.workloads.items() if workload.get("status") == "pending"}

                print(f"[{datetime.now()}] Contract state loaded: {len(self.workloads)} workloads, {len(self.valid_keys)} valid keys, {len(self.workload_results)} results")
            else:
                print(f"[{datetime.now()}] No contract state found, starting fresh")
        except Exception as e:
            print(f"[{datetime.now()}] Contract state load error: {e}")
            print(f"[{datetime.now()}] Starting with fresh contract state")

    def process_block_workloads(self, block):
        try:
            if block.data.get("type") == "ai_work":
                workload_key = block.data.get("key")
                purpose = block.data.get("purpose")
                data_hash = block.data.get("data_hash")
                storage_nodes = block.data.get("storage_nodes")
                status = block.data.get("status")
                if workload_key and purpose and data_hash and storage_nodes:
                    if status == "pending" and workload_key not in self.workloads:
                        self.workloads[workload_key] = {
                            "machine_address": block.data.get("machine_address", ""),
                            "purpose": purpose,
                            "data_hash": data_hash,
                            "status": "pending",
                            "storage_nodes": storage_nodes
                        }
                        self.valid_keys.add(workload_key)
                        print(f"[{datetime.now()}] Added workload from block: key={workload_key[:8]}..., purpose={purpose}")
                    elif status == "completed" and workload_key in self.workloads:
                        self.workloads[workload_key]["status"] = "completed"
                        self.valid_keys.discard(workload_key)
                        del self.workloads[workload_key]  # Remove completed workload entirely
                        print(f"[{datetime.now()}] Marked workload completed from block: key={workload_key[:8]}...")
        except Exception as e:
            print(f"[{datetime.now()}] Error processing block workloads: {e}")

if __name__ == "__main__":
    contract = WorkloadContract()
    balances = {"test_machine": 1000000000, "dao": 0}
    result = contract.register_machine("test_machine", hashlib.sha3_256(str(time.time()).encode()).hexdigest())
    print(f"Registration result: {result}")
    result = contract.submit_workload("test_machine", hashlib.sha3_256(str(time.time()).encode()).hexdigest(), "Test purpose", "test_hash", [("127.0.0.1", 5002)], balances)
    print(f"Submission result: {result}")