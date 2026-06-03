#!/usr/bin/env python3
"""
AVA Brain Witness System - Digital Witness Recorder
Standalone service that logs all audio-to-text conversions for AVA AI companion
Provides HTTP API for witness record logging with tamper-proof integrity
"""

import json
import os
import time
import hashlib
import threading
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from typing import Dict, List, Any, Optional
import tempfile
import shutil

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('brain_witness.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('BrainWitness')

class BrainWitness:
    """Digital witness recorder for AVA AI companion system"""

    def __init__(self, brain_file=None, max_file_size_mb=100, backup_count=5):
        # Default to brain folder location
        if brain_file is None:
            script_dir = Path(__file__).parent
            brain_file = script_dir / "ava_brain.json"

        self.brain_file = Path(brain_file)
        self.max_file_size_mb = max_file_size_mb
        self.backup_count = backup_count
        self.lock = threading.Lock()
        self.pending_records = []
        self.flush_interval = 30  # Flush every 30 seconds

        # Initialize brain file if it doesn't exist
        self._initialize_brain_file()

        # Start background flush thread
        self.flush_thread = threading.Thread(target=self._background_flush, daemon=True)
        self.flush_thread.start()

        logger.info(f"Brain Witness initialized - file: {self.brain_file}")

    def _initialize_brain_file(self):
        """Create initial brain file structure if it doesn't exist"""
        if not self.brain_file.exists():
            initial_structure = {
                "metadata": {
                    "creation_date": datetime.now().isoformat(),
                    "version": "1.0",
                    "description": "AVA Digital Witness Brain - Complete audio transcript history",
                    "total_interactions": 0,
                    "total_records": 0,
                    "storage_stats": {
                        "file_size_mb": 0,
                        "last_rotation": None,
                        "integrity_status": "valid"
                    }
                },
                "witness_records": [],
                "integrity_hash": self._calculate_integrity_hash([])
            }

            with open(self.brain_file, 'w') as f:
                json.dump(initial_structure, f, indent=2)

            logger.info(f"Created new brain file: {self.brain_file}")

    def log_witness_record(self, transcribed_text: str, audio_source: str = "unknown",
                          raw_audio_duration: float = 0.0, confidence_score: float = 0.0,
                          processing_time: float = 0.0, context: str = "",
                          additional_metadata: Dict[str, Any] = None) -> bool:
        """
        Log a witness record to the brain file

        Args:
            transcribed_text: The converted speech text
            audio_source: Source of audio ("continuous_listening", "wake_word", "command_input")
            raw_audio_duration: Length of audio in seconds
            confidence_score: Speech recognition confidence (0-1)
            processing_time: Time taken to process audio in seconds
            context: Additional context about the recording
            additional_metadata: Any extra metadata to store

        Returns:
            bool: True if logged successfully, False otherwise
        """
        try:
            # Create witness record
            record = {
                "timestamp": datetime.now().isoformat(),
                "audio_source": audio_source,
                "raw_audio_duration": raw_audio_duration,
                "transcribed_text": transcribed_text.strip(),
                "confidence_score": confidence_score,
                "processing_time": processing_time,
                "context": context,
                "additional_metadata": additional_metadata or {},
                "record_hash": self._calculate_record_hash(transcribed_text, audio_source)
            }

            # Add to pending records (thread-safe)
            with self.lock:
                self.pending_records.append(record)

            logger.debug(f"Queued witness record: {len(transcribed_text)} chars from {audio_source}")
            return True

        except Exception as e:
            logger.error(f"Error queuing witness record: {e}")
            return False

    def _background_flush(self):
        """Background thread that periodically flushes pending records to disk"""
        while True:
            try:
                time.sleep(self.flush_interval)
                self._flush_pending_records()
            except Exception as e:
                logger.error(f"Error in background flush: {e}")
                time.sleep(5)  # Brief pause on error

    def _flush_pending_records(self):
        """Flush all pending records to the brain file"""
        if not self.pending_records:
            return

        with self.lock:
            records_to_flush = self.pending_records.copy()
            self.pending_records.clear()

        if not records_to_flush:
            return

        try:
            # Load current brain data
            with open(self.brain_file, 'r') as f:
                brain_data = json.load(f)

            # Add new records
            brain_data["witness_records"].extend(records_to_flush)

            # Update metadata
            brain_data["metadata"]["total_records"] = len(brain_data["witness_records"])
            brain_data["metadata"]["total_interactions"] = len(set(
                r["transcribed_text"] for r in brain_data["witness_records"] if r["transcribed_text"]
            ))

            # Update storage stats
            file_size_mb = self.brain_file.stat().st_size / (1024 * 1024)
            brain_data["metadata"]["storage_stats"]["file_size_mb"] = round(file_size_mb, 2)

            # Recalculate integrity hash
            brain_data["integrity_hash"] = self._calculate_integrity_hash(brain_data["witness_records"])

            # Check if file needs rotation
            if file_size_mb > self.max_file_size_mb:
                self._rotate_brain_file()

            # Write updated data
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, dir=self.brain_file.parent) as temp_file:
                json.dump(brain_data, temp_file, indent=2)
                temp_path = temp_file.name

            # Atomic move
            shutil.move(temp_path, self.brain_file)

            logger.info(f"Flushed {len(records_to_flush)} witness records to brain file")

        except Exception as e:
            logger.error(f"Error flushing witness records: {e}")
            # Re-queue failed records
            with self.lock:
                self.pending_records.extend(records_to_flush)

    def _rotate_brain_file(self):
        """Rotate brain file when it gets too large"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"ava_brain_{timestamp}.json"

            # Move current file to backup
            shutil.move(self.brain_file, self.brain_file.parent / backup_name)

            # Clean up old backups (keep only backup_count)
            backup_files = sorted(self.brain_file.parent.glob("ava_brain_*.json"))
            if len(backup_files) > self.backup_count:
                for old_backup in backup_files[:-self.backup_count]:
                    old_backup.unlink()

            # Create new brain file
            self._initialize_brain_file()

            # Update rotation timestamp
            with open(self.brain_file, 'r') as f:
                brain_data = json.load(f)
            brain_data["metadata"]["storage_stats"]["last_rotation"] = datetime.now().isoformat()

            with open(self.brain_file, 'w') as f:
                json.dump(brain_data, f, indent=2)

            logger.info(f"Rotated brain file - backup: {backup_name}")

        except Exception as e:
            logger.error(f"Error rotating brain file: {e}")

    def _calculate_record_hash(self, text: str, source: str) -> str:
        """Calculate hash for individual record"""
        content = f"{text}|{source}|{datetime.now().isoformat()}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _calculate_integrity_hash(self, records: List[Dict]) -> str:
        """Calculate integrity hash for all records"""
        if not records:
            return hashlib.sha256(b"empty").hexdigest()

        # Sort records by timestamp for consistent hashing
        sorted_records = sorted(records, key=lambda x: x.get("timestamp", ""))
        content = "|".join([
            f"{r.get('timestamp', '')}|{r.get('transcribed_text', '')}|{r.get('record_hash', '')}"
            for r in sorted_records
        ])

        return hashlib.sha256(content.encode()).hexdigest()

    def get_brain_stats(self) -> Dict[str, Any]:
        """Get current brain statistics"""
        try:
            with open(self.brain_file, 'r') as f:
                brain_data = json.load(f)

            return {
                "total_records": brain_data["metadata"]["total_records"],
                "total_interactions": brain_data["metadata"]["total_interactions"],
                "file_size_mb": brain_data["metadata"]["storage_stats"]["file_size_mb"],
                "creation_date": brain_data["metadata"]["creation_date"],
                "last_rotation": brain_data["metadata"]["storage_stats"].get("last_rotation"),
                "integrity_status": brain_data["metadata"]["storage_stats"]["integrity_status"],
                "pending_records": len(self.pending_records)
            }
        except Exception as e:
            logger.error(f"Error getting brain stats: {e}")
            return {"error": str(e)}

    def verify_integrity(self) -> bool:
        """Verify integrity of brain file"""
        try:
            with open(self.brain_file, 'r') as f:
                brain_data = json.load(f)

            expected_hash = self._calculate_integrity_hash(brain_data["witness_records"])
            actual_hash = brain_data.get("integrity_hash", "")

            is_valid = expected_hash == actual_hash
            brain_data["metadata"]["storage_stats"]["integrity_status"] = "valid" if is_valid else "compromised"

            # Save updated status
            with open(self.brain_file, 'w') as f:
                json.dump(brain_data, f, indent=2)

            return is_valid

        except Exception as e:
            logger.error(f"Error verifying integrity: {e}")
            return False

# Global witness instance
witness = BrainWitness()

# Flask API
app = Flask(__name__)

@app.route('/log', methods=['POST'])
def log_witness():
    """API endpoint to log witness records"""
    try:
        data = request.get_json()

        if not data or 'transcribed_text' not in data:
            return jsonify({"success": False, "error": "Missing transcribed_text"}), 400

        # Extract fields with defaults
        transcribed_text = data['transcribed_text']
        audio_source = data.get('audio_source', 'api_call')
        raw_audio_duration = data.get('raw_audio_duration', 0.0)
        confidence_score = data.get('confidence_score', 0.0)
        processing_time = data.get('processing_time', 0.0)
        context = data.get('context', '')
        additional_metadata = data.get('additional_metadata', {})

        # Log the record
        success = witness.log_witness_record(
            transcribed_text=transcribed_text,
            audio_source=audio_source,
            raw_audio_duration=raw_audio_duration,
            confidence_score=confidence_score,
            processing_time=processing_time,
            context=context,
            additional_metadata=additional_metadata
        )

        if success:
            return jsonify({"success": True, "message": "Witness record logged"})
        else:
            return jsonify({"success": False, "error": "Failed to log record"}), 500

    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get brain statistics"""
    stats = witness.get_brain_stats()
    return jsonify(stats)

@app.route('/verify', methods=['POST'])
def verify_integrity():
    """Verify brain file integrity"""
    is_valid = witness.verify_integrity()
    return jsonify({
        "integrity_valid": is_valid,
        "status": "valid" if is_valid else "compromised"
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "brain_file_exists": witness.brain_file.exists()
    })

if __name__ == '__main__':
    logger.info("Starting AVA Brain Witness API server on port 8081")
    print("🚀 AVA Brain Witness System Active")
    print("📡 HTTP API: http://localhost:8081")
    print("📋 Endpoints:")
    print("  POST /log - Log witness records")
    print("  GET /stats - Get brain statistics")
    print("  POST /verify - Verify file integrity")
    print("  GET /health - Health check")
    print("📁 Brain file: ava_brain.json")
    print("💾 Digital witness recording active...")

    app.run(host='0.0.0.0', port=8081, debug=False, threaded=True)
