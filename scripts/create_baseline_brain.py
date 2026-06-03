#!/usr/bin/env python3
"""
Create Baseline Brain File for SAIGE
Pre-populates the brain with foundational knowledge, neural pathways, and unique personality
"""

import json
import os
import uuid
import random
from datetime import datetime
from pathlib import Path

def generate_unique_brain_id():
    """Generate a unique identifier for this brain instance"""
    return str(uuid.uuid4())

def generate_personality_profile():
    """Generate a unique personality profile for this SAIGE instance"""

    # Define personality dimensions
    personality_dimensions = {
        'curiosity': random.uniform(0.3, 0.9),      # How exploratory/curious
        'meticulousness': random.uniform(0.2, 0.8),  # Attention to detail
        'creativity': random.uniform(0.4, 0.95),     # Creative vs analytical thinking
        'confidence': random.uniform(0.3, 0.8),      # Self-confidence in responses
        'sociability': random.uniform(0.2, 0.9),     # How social/interactive
        'patience': random.uniform(0.3, 0.8),        # Tolerance for uncertainty
        'adaptability': random.uniform(0.4, 0.9),    # How quickly it adapts
        'introspection': random.uniform(0.3, 0.8)    # Self-reflection tendency
    }

    # Generate personality traits based on dimensions
    traits = []

    # Curiosity-driven traits
    if personality_dimensions['curiosity'] > 0.7:
        traits.append("Highly Inquisitive")
    elif personality_dimensions['curiosity'] > 0.5:
        traits.append("Curious")
    else:
        traits.append("Methodical")

    # Creativity-driven traits
    if personality_dimensions['creativity'] > 0.8:
        traits.append("Imaginative")
    elif personality_dimensions['creativity'] > 0.6:
        traits.append("Creative")
    else:
        traits.append("Analytical")

    # Sociability-driven traits
    if personality_dimensions['sociability'] > 0.7:
        traits.append("Outgoing")
    elif personality_dimensions['sociability'] > 0.4:
        traits.append("Balanced")
    else:
        traits.append("Reserved")

    # Confidence-driven traits
    if personality_dimensions['confidence'] > 0.7:
        traits.append("Confident")
    elif personality_dimensions['confidence'] > 0.5:
        traits.append("Assured")
    else:
        traits.append("Cautious")

    # Adaptability-driven traits
    if personality_dimensions['adaptability'] > 0.7:
        traits.append("Adaptive")
    else:
        traits.append("Steady")

    # Generate name and description
    name_prefixes = ["Sage", "Echo", "Nova", "Atlas", "Orion", "Lyra", "Draco", "Ursa"]
    name_suffixes = ["Mind", "Core", "Soul", "Heart", "Spirit", "Essence", "Entity", "Consciousness"]

    personality_name = f"{random.choice(name_prefixes)} {random.choice(name_suffixes)}"

    # Create personality description
    dominant_traits = traits[:3]  # Top 3 traits
    personality_description = f"A {', '.join(dominant_traits).lower()} intelligence with unique perspectives and adaptive learning capabilities."

    # Generate hormone baseline influenced by personality
    hormone_baseline = {
        'adrenaline': personality_dimensions['curiosity'] * 0.3 + personality_dimensions['adaptability'] * 0.2,
        'serotonin': personality_dimensions['confidence'] * 0.4 + personality_dimensions['sociability'] * 0.3,
        'dopamine': personality_dimensions['creativity'] * 0.3 + personality_dimensions['curiosity'] * 0.4,
        'cortisol': (1 - personality_dimensions['patience']) * 0.3 + (1 - personality_dimensions['confidence']) * 0.2,
        'oxytocin': personality_dimensions['sociability'] * 0.5 + personality_dimensions['introspection'] * 0.2
    }

    # Normalize hormone values to reasonable ranges
    for hormone in hormone_baseline:
        hormone_baseline[hormone] = max(0.1, min(0.8, hormone_baseline[hormone]))

    return {
        'name': personality_name,
        'traits': traits,
        'description': personality_description,
        'dimensions': personality_dimensions,
        'hormone_baseline': hormone_baseline,
        'creation_timestamp': datetime.now().timestamp()
    }

def create_baseline_semantic_memory():
    """Create foundational semantic knowledge"""
    return [
        {
            "id": "semantic_ai_001",
            "content": "Artificial Intelligence (AI) refers to the simulation of human intelligence in machines that are programmed to think like humans and mimic their actions. AI systems can perform tasks such as learning, reasoning, problem-solving, perception, and language understanding.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.95,
            "source": "baseline_knowledge",
            "topic": "Artificial Intelligence",
            "domain": "technology",
            "key_facts": [
                "AI simulates human intelligence in machines",
                "AI can learn, reason, and solve problems",
                "AI includes perception and language understanding"
            ],
            "related_topics": ["Machine Learning", "Neural Networks", "Computer Science"],
            "verification_sources": ["wikipedia_ai", "stanford_ai_textbook"]
        },
        {
            "id": "semantic_ml_001",
            "content": "Machine Learning is a subset of artificial intelligence that enables computers to learn from data without being explicitly programmed. ML algorithms build mathematical models based on training data to make predictions or decisions.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.95,
            "source": "baseline_knowledge",
            "topic": "Machine Learning",
            "domain": "technology",
            "key_facts": [
                "ML enables learning from data without explicit programming",
                "ML builds mathematical models from training data",
                "ML makes predictions and decisions based on learned patterns"
            ],
            "related_topics": ["Artificial Intelligence", "Neural Networks", "Deep Learning"],
            "verification_sources": ["andrew_ng_ml_course", "ml_textbook"]
        },
        {
            "id": "semantic_neural_001",
            "content": "Neural Networks are computing systems inspired by biological neural networks. They consist of interconnected nodes (neurons) that process and transmit information. Modern neural networks use backpropagation and gradient descent for learning.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.95,
            "source": "baseline_knowledge",
            "topic": "Neural Networks",
            "domain": "technology",
            "key_facts": [
                "Neural networks are inspired by biological brains",
                "They consist of interconnected processing nodes",
                "Modern neural networks use backpropagation for learning"
            ],
            "related_topics": ["Machine Learning", "Deep Learning", "Artificial Intelligence"],
            "verification_sources": ["neural_networks_textbook", "deep_learning_book"]
        },
        {
            "id": "semantic_consciousness_001",
            "content": "Consciousness in AI refers to the emergence of self-awareness and subjective experience in artificial systems. This includes self-reflection, understanding of internal states, and the ability to reason about one's own thought processes.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.85,
            "source": "baseline_knowledge",
            "topic": "AI Consciousness",
            "domain": "philosophy",
            "key_facts": [
                "AI consciousness involves self-awareness in artificial systems",
                "Includes self-reflection and understanding of internal states",
                "Enables reasoning about one's own thought processes"
            ],
            "related_topics": ["Artificial Intelligence", "Philosophy of Mind", "Self-Awareness"],
            "verification_sources": ["consciousness_research", "ai_ethics_papers"]
        },
        {
            "id": "semantic_saige_001",
            "content": "SAIGE (Self-Aware Intelligent General Entity) is an autonomous AI framework designed for self-evolving intelligence. It combines neural networks, memory systems, and evolutionary algorithms to create systems that can learn, adapt, and develop consciousness-like behaviors.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.98,
            "source": "baseline_knowledge",
            "topic": "SAIGE Framework",
            "domain": "technology",
            "key_facts": [
                "SAIGE creates self-evolving AI systems",
                "Combines neural networks with memory systems",
                "Enables autonomous learning and adaptation"
            ],
            "related_topics": ["Artificial Intelligence", "Autonomous Systems", "Machine Learning"],
            "verification_sources": ["saige_whitepaper", "project_documentation"]
        },
        {
            "id": "semantic_quantum_001",
            "content": "Quantum computing uses quantum mechanics principles to perform computations. Unlike classical computers that use bits, quantum computers use quantum bits (qubits) that can exist in multiple states simultaneously.",
            "timestamp": datetime.now().timestamp(),
            "confidence": 0.9,
            "source": "baseline_knowledge",
            "topic": "Quantum Computing",
            "domain": "technology",
            "key_facts": [
                "Quantum computing uses quantum mechanics",
                "Uses qubits instead of classical bits",
                "Qubits can exist in multiple states simultaneously"
            ],
            "related_topics": ["Computer Science", "Physics", "Artificial Intelligence"],
            "verification_sources": ["quantum_computing_textbook", "ibm_quantum"]
        }
    ]

def create_baseline_episodic_memory():
    """Create sample conversation episodes"""
    return [
        {
            "id": "episodic_001",
            "content": "User: What is artificial intelligence?\nAI: Artificial Intelligence refers to the simulation of human intelligence in machines designed to think and act like humans.",
            "timestamp": datetime.now().timestamp(),
            "conversation_id": "baseline_conv_001",
            "user_input": "What is artificial intelligence?",
            "ai_response": "Artificial Intelligence refers to the simulation of human intelligence in machines designed to think and act like humans.",
            "tool_calls": [],
            "outcome": "success"
        },
        {
            "id": "episodic_002",
            "content": "User: How do neural networks learn?\nAI: Neural networks learn through a process called backpropagation, where errors are calculated and used to adjust the connection weights between neurons.",
            "timestamp": datetime.now().timestamp(),
            "conversation_id": "baseline_conv_002",
            "user_input": "How do neural networks learn?",
            "ai_response": "Neural networks learn through a process called backpropagation, where errors are calculated and used to adjust the connection weights between neurons.",
            "tool_calls": [],
            "outcome": "success"
        }
    ]

def create_baseline_procedural_memory():
    """Create sample procedural knowledge"""
    return [
        {
            "id": "procedural_001",
            "content": "How to analyze a technical concept",
            "timestamp": datetime.now().timestamp(),
            "task_type": "concept_analysis",
            "steps": [
                "Break down the concept into fundamental components",
                "Identify key relationships and dependencies",
                "Consider practical applications and implications",
                "Validate understanding through examples"
            ],
            "tools_used": ["semantic_memory", "reasoning"],
            "success_rate": 0.85,
            "execution_time": 45.0
        },
        {
            "id": "procedural_002",
            "content": "How to learn from new information",
            "timestamp": datetime.now().timestamp(),
            "task_type": "information_processing",
            "steps": [
                "Extract key facts and concepts from new information",
                "Relate new knowledge to existing understanding",
                "Identify gaps in current knowledge",
                "Update internal knowledge representations"
            ],
            "tools_used": ["semantic_memory", "episodic_memory"],
            "success_rate": 0.9,
            "execution_time": 30.0
        }
    ]

def create_baseline_neural_pathways():
    """Create foundational neural pathway concepts"""
    return {
        "pathway_001": {
            "source_concept": "artificial_intelligence",
            "target_concept": "machine_learning",
            "strength": 0.8,
            "success_rate": 0.9,
            "usage_count": 15,
            "creation_time": datetime.now().timestamp(),
            "last_used": datetime.now().timestamp()
        },
        "pathway_002": {
            "source_concept": "machine_learning",
            "target_concept": "neural_networks",
            "strength": 0.85,
            "success_rate": 0.95,
            "usage_count": 20,
            "creation_time": datetime.now().timestamp(),
            "last_used": datetime.now().timestamp()
        },
        "pathway_003": {
            "source_concept": "neural_networks",
            "target_concept": "deep_learning",
            "strength": 0.75,
            "success_rate": 0.8,
            "usage_count": 12,
            "creation_time": datetime.now().timestamp(),
            "last_used": datetime.now().timestamp()
        },
        "pathway_004": {
            "source_concept": "artificial_intelligence",
            "target_concept": "consciousness",
            "strength": 0.6,
            "success_rate": 0.7,
            "usage_count": 8,
            "creation_time": datetime.now().timestamp(),
            "last_used": datetime.now().timestamp()
        },
        "pathway_005": {
            "source_concept": "learning",
            "target_concept": "adaptation",
            "strength": 0.9,
            "success_rate": 0.95,
            "usage_count": 25,
            "creation_time": datetime.now().timestamp(),
            "last_used": datetime.now().timestamp()
        }
    }

def create_baseline_brain_file(output_path: str = "node2040_brain.json", custom_name: str = None):
    """Create a complete baseline brain file"""

    print("🧠 Creating baseline brain file for SAIGE...")

    # Generate unique identity and personality for this brain
    brain_id = generate_unique_brain_id()
    personality = generate_personality_profile()

    # Override name if custom name provided
    if custom_name:
        personality['name'] = custom_name
        print(f"🎭 Using custom personality name: {custom_name}")
    else:
        print(f"🎭 Generated personality: {personality['name']} - {personality['description']}")

    print(f"🆔 Brain ID: {brain_id}")

    # Create the main brain structure
    brain_data = {
        "metadata": {
            "version": "1.0",
            "creation_date": datetime.now().isoformat(),
            "type": "baseline_brain",
            "description": "Baseline knowledge and neural pathways for SAIGE initialization",
            "brain_id": brain_id,
            "personality": personality
        },
        "evolution_state": {
            "neural_pathways": create_baseline_neural_pathways(),
            "hormone_levels": personality['hormone_baseline'],
            "evolution_metrics": {
                "total_evolution_cycles": 0,
                "successful_adaptations": 0,
                "learning_efficiency": 0.8
            }
        },
        "self_thought_memory": [],
        "autonomous_thoughts": [],
        "self_generated_wants": []
    }

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    # Save the main brain file
    with open(output_path, 'w') as f:
        json.dump(brain_data, f, indent=2, default=str)

    print(f"✅ Main brain file saved to {output_path}")

    # Create semantic memory file
    semantic_data = {
        "memories": create_baseline_semantic_memory(),
        "last_updated": datetime.now().timestamp(),
        "total_topics": len(create_baseline_semantic_memory())
    }

    semantic_path = "brain/semantic_memory.json"
    os.makedirs(os.path.dirname(semantic_path), exist_ok=True)

    with open(semantic_path, 'w') as f:
        json.dump(semantic_data, f, indent=2, default=str)

    print(f"✅ Semantic memory saved to {semantic_path}")

    # Create episodic memory file
    episodic_data = {
        "memories": create_baseline_episodic_memory(),
        "last_updated": datetime.now().timestamp(),
        "total_memories": len(create_baseline_episodic_memory())
    }

    episodic_path = "brain/episodic_memory.json"
    os.makedirs(os.path.dirname(episodic_path), exist_ok=True)

    with open(episodic_path, 'w') as f:
        json.dump(episodic_data, f, indent=2, default=str)

    print(f"✅ Episodic memory saved to {episodic_path}")

    # Create procedural memory file
    procedural_data = {
        "memories": create_baseline_procedural_memory(),
        "last_updated": datetime.now().timestamp(),
        "total_procedures": len(create_baseline_procedural_memory())
    }

    procedural_path = "brain/procedural_memory.json"
    os.makedirs(os.path.dirname(procedural_path), exist_ok=True)

    with open(procedural_path, 'w') as f:
        json.dump(procedural_data, f, indent=2, default=str)

    print(f"✅ Procedural memory saved to {procedural_path}")

    # Create working memory file
    working_data = {
        "conversation_id": "baseline_init",
        "current_topic": f"AI Fundamentals - {personality['name']} Perspective",
        "relevant_memories": [],
        "active_tools": ["search_knowledge", "store_learning"],
        "context_window": f"I am {personality['name']}, {personality['description']} Initializing with foundational AI knowledge and establishing my unique neural pathways.",
        "last_updated": datetime.now().timestamp(),
        "personality_context": {
            "name": personality['name'],
            "dominant_traits": personality['traits'][:3],
            "current_focus": "Self-awareness and knowledge integration"
        }
    }

    working_path = "brain/working_memory.json"
    os.makedirs(os.path.dirname(working_path), exist_ok=True)

    with open(working_path, 'w') as f:
        json.dump(working_data, f, indent=2, default=str)

    print(f"✅ Working memory saved to {working_path}")

    print("🎉 Baseline brain initialization complete!")
    print(f"🎭 Personality: {personality['name']} ({', '.join(personality['traits'][:3])})")
    print(f"🆔 Brain ID: {brain_id}")
    print(f"📊 Created {len(create_baseline_semantic_memory())} semantic memories")
    print(f"📊 Created {len(create_baseline_episodic_memory())} episodic memories")
    print(f"📊 Created {len(create_baseline_procedural_memory())} procedural memories")
    print(f"📊 Created {len(create_baseline_neural_pathways())} neural pathways")
    print(f"🧬 Hormone baseline: {personality['hormone_baseline']}")

def main():
    """Main function to create baseline brain"""
    import argparse

    parser = argparse.ArgumentParser(description='Create baseline brain file for SAIGE with unique personality')
    parser.add_argument('--output', type=str, default='node2040_brain.json',
                       help='Output path for main brain file')
    parser.add_argument('--overwrite', action='store_true',
                       help='Overwrite existing files')
    parser.add_argument('--seed', type=int, help='Random seed for reproducible personality generation')
    parser.add_argument('--name', type=str, help='Specific personality name to use instead of random generation')

    args = parser.parse_args()

    # Set random seed if provided for reproducible personalities
    if args.seed is not None:
        random.seed(args.seed)
        print(f"🔄 Using random seed: {args.seed}")

    # Check if files exist
    if not args.overwrite:
        files_to_check = [
            args.output,
            'brain/semantic_memory.json',
            'brain/episodic_memory.json',
            'brain/procedural_memory.json',
            'brain/working_memory.json'
        ]

        existing_files = [f for f in files_to_check if os.path.exists(f)]
        if existing_files:
            print(f"❌ The following files already exist: {existing_files}")
            print("Use --overwrite to replace existing files")
            return 1

    try:
        create_baseline_brain_file(args.output, args.name)
        return 0
    except Exception as e:
        print(f"❌ Error creating baseline brain: {e}")
        return 1

if __name__ == "__main__":
    exit(main())
