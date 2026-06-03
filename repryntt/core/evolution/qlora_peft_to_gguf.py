#!/usr/bin/env python3
"""
Convert HuggingFace PEFT LoRa adapters to GGUF format for llama.cpp
"""

import os
import json
import torch
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def convert_peft_to_gguf(adapter_dir: str):
    """Convert PEFT adapter to GGUF format"""
    try:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        adapter_dir = Path(adapter_dir)

        # Load adapter config
        config_file = adapter_dir / "adapter_config.json"
        if not config_file.exists():
            raise FileNotFoundError(f"Adapter config not found: {config_file}")

        with open(config_file, 'r') as f:
            adapter_config = json.load(f)

        # Load base model (we need this to merge adapters)
        base_model_path = adapter_config.get("base_model_name_or_path", "microsoft/phi-3-mini-4k-instruct")

        logger.info(f"Loading base model: {base_model_path}")
        model = AutoModelForCausalLM.from_pretrained(
            base_model_path,
            torch_dtype=torch.float16,
            device_map="cpu",  # Use CPU for conversion
            trust_remote_code=True
        )

        # Load PEFT model
        logger.info("Loading PEFT adapter")
        model = PeftModel.from_pretrained(model, str(adapter_dir))

        # Merge adapters into base model
        logger.info("Merging LoRa adapters")
        merged_model = model.merge_and_unload()

        # Save merged model temporarily
        temp_dir = adapter_dir / "temp_merged"
        temp_dir.mkdir(exist_ok=True)

        logger.info("Saving merged model")
        merged_model.save_pretrained(str(temp_dir))

        # Convert to GGUF and quantize to Q4
        gguf_output = adapter_dir / "qlora_fine_tuned.gguf"
        temp_gguf = adapter_dir / "temp.gguf"

        import subprocess

        # First, convert the merged model to GGUF (FP16)
        convert_cmd = [
            "python", "-m", "llama.cpp.convert",
            "--model", str(temp_dir),
            "--output", str(temp_gguf),
            "--type", "f16"
        ]

        logger.info("Converting merged model to GGUF format")
        result = subprocess.run(convert_cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            logger.error(f"GGUF conversion failed: {result.stderr}")
            raise Exception("GGUF conversion failed")

        # Now quantize to Q4_K_M (same as your current model)
        quantize_cmd = [
            "llama.cpp.quantize",
            str(temp_gguf),
            str(gguf_output),
            "Q4_K_M"  # Use same quantization as your Phi-3 model
        ]

        logger.info("Quantizing to Q4_K_M format (matching your Phi-3 model)")
        result = subprocess.run(quantize_cmd, capture_output=True, text=True, timeout=600)

        if result.returncode == 0:
            logger.info(f"✅ Successfully created Q4 quantized fine-tuned model: {gguf_output}")

            # Get file size for logging
            size_mb = os.path.getsize(gguf_output) / (1024 * 1024)
            logger.info(f"📊 Fine-tuned model size: {size_mb:.1f} MB")

            # Clean up temp GGUF
            temp_gguf.unlink(missing_ok=True)

        else:
            logger.error(f"❌ Quantization failed: {result.stderr}")
            raise Exception("Quantization failed")

        # Clean up temp directory
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"PEFT to GGUF conversion failed: {e}")
        raise

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python qlora_peft_to_gguf.py <adapter_directory>")
        sys.exit(1)

    convert_peft_to_gguf(sys.argv[1])
