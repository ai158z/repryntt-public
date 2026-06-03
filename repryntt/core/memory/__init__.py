"""
repryntt.core.memory — Multi-compartment memory service.

Memory types:
    - Episodic: Event history (capped, timestamped)
    - Semantic: Factual knowledge organized by domain
    - Procedural: How-to knowledge and skills
    - Working: Current context window (in-memory)
    - Vector: SentenceTransformer + FAISS for semantic similarity retrieval

Migration source:
    - SAIGE/brain/brain_system.py (memory compartment sections — extract from 18K monolith)
    - SAIGE/brain/context_compaction.py (~200 lines — context window compression)
    - SAIGE/brain/prompt_sync_system.py (~400 lines — master prompt construction)
    - SAIGE/brain/system_prompt_manager.py (~300 lines — system prompt assembly)
"""
