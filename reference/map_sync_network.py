"""
Map Sync Network - Dynamic Function & Context Discovery System

This module provides semantic function discovery and context mapping for SAIGE.
Instead of hardcoded function lists, the AI can query what capabilities exist
and discover relevant tools through vector search.

Architecture:
- Function Registry: All available functions with metadata
- Context Graph: Paths, states, and system structure  
- Vector Search: Semantic discovery of capabilities
- Auto-registration: Functions register themselves on init

Benefits:
- Query-driven discovery ("find functions for X")
- Dynamic scaling (new functions auto-indexed)
- Reduced token burden (load only relevant context)
- Intent-based selection (semantic vs keyword)
"""

import logging
import json
import os
import time
from typing import Dict, List, Any, Callable, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class MapSyncNetwork:
    """
    Unified mapping system connecting all SAIGE capabilities.
    
    The AI model queries this to discover:
    - What functions are available
    - Where resources are located
    - How to accomplish specific goals
    - Similar past actions/solutions
    """
    
    def __init__(self, brain_system=None):
        """
        Initialize the MapSyncNetwork.
        
        Args:
            brain_system: Reference to BrainSystem for vector search integration
        """
        self.brain = brain_system
        
        # Core registries
        self.function_registry: Dict[str, Dict[str, Any]] = {}
        self.context_graph: Dict[str, Any] = {}
        self.capability_index: Dict[str, List[str]] = {}  # tag -> function names
        
        # Vector search integration
        self.vector_enabled = False
        self.function_embeddings: Dict[str, Any] = {}
        
        logger.info("🗺️  MapSyncNetwork initialized")
    
    def register_function(
        self,
        name: str,
        func: Callable,
        description: str,
        category: str,
        tags: List[str],
        parameters: Dict[str, str] = None,
        examples: List[str] = None
    ):
        """
        Register a function in the map network.
        
        Args:
            name: Function name
            func: The callable function
            description: What the function does
            category: Category (e.g., 'memory', 'file', 'analysis', 'blockchain')
            tags: Semantic tags for discovery
            parameters: Parameter descriptions {param_name: description}
            examples: Usage examples
        """
        self.function_registry[name] = {
            'callable': func,
            'description': description,
            'category': category,
            'tags': tags,
            'parameters': parameters or {},
            'examples': examples or [],
            'registered_at': time.time()
        }
        
        # Index by tags for quick lookup
        for tag in tags:
            if tag not in self.capability_index:
                self.capability_index[tag] = []
            self.capability_index[tag].append(name)
        
        # Create vector embedding if brain system available
        if self.brain and hasattr(self.brain, 'vector_search_enabled') and self.brain.vector_search_enabled:
            self._create_function_embedding(name, description, tags)
        
        logger.debug(f"📝 Registered function: {name} ({category})")
    
    def _create_function_embedding(self, name: str, description: str, tags: List[str]):
        """Create vector embedding for semantic function search"""
        try:
            # Combine description and tags for richer embedding
            text = f"{name}: {description}. Tags: {', '.join(tags)}"
            
            if hasattr(self.brain, 'encoder'):
                embedding = self.brain.encoder.encode([text], convert_to_numpy=True)
                self.function_embeddings[name] = embedding[0]
                self.vector_enabled = True
                logger.debug(f"🔍 Created embedding for {name}")
        except Exception as e:
            logger.warning(f"Failed to create embedding for {name}: {e}")
    
    def query_capabilities(self, intent: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Query what functions can accomplish a given intent.
        
        Args:
            intent: Natural language description of what to do
            limit: Maximum results to return
            
        Returns:
            List of matching functions with relevance scores
        """
        results = []
        
        # Vector search if available (best results)
        if self.vector_enabled and hasattr(self.brain, 'encoder'):
            try:
                intent_embedding = self.brain.encoder.encode([intent], convert_to_numpy=True)[0]
                
                # Calculate similarity with all function embeddings
                scored_functions = []
                for func_name, func_embedding in self.function_embeddings.items():
                    # Cosine similarity
                    import numpy as np
                    similarity = np.dot(intent_embedding, func_embedding) / (
                        np.linalg.norm(intent_embedding) * np.linalg.norm(func_embedding)
                    )
                    scored_functions.append((func_name, float(similarity)))
                
                # Sort by similarity
                scored_functions.sort(key=lambda x: x[1], reverse=True)
                
                # Get top results
                for func_name, score in scored_functions[:limit]:
                    func_info = self.function_registry[func_name].copy()
                    func_info['name'] = func_name
                    func_info['relevance_score'] = score
                    func_info.pop('callable', None)  # Don't return callable in results
                    results.append(func_info)
                
                logger.info(f"🔍 Vector search found {len(results)} functions for: {intent}")
                return results
                
            except Exception as e:
                logger.warning(f"Vector search failed, falling back to keyword: {e}")
        
        # Fallback: Keyword/tag-based search
        intent_lower = intent.lower()
        scored_functions = []
        
        for func_name, func_info in self.function_registry.items():
            score = 0
            
            # Check description
            if intent_lower in func_info['description'].lower():
                score += 10
            
            # Check tags
            for tag in func_info['tags']:
                if tag.lower() in intent_lower or intent_lower in tag.lower():
                    score += 5
            
            # Check category
            if intent_lower in func_info['category'].lower():
                score += 3
            
            if score > 0:
                scored_functions.append((func_name, score))
        
        # Sort and return top results
        scored_functions.sort(key=lambda x: x[1], reverse=True)
        for func_name, score in scored_functions[:limit]:
            func_info = self.function_registry[func_name].copy()
            func_info['name'] = func_name
            func_info['relevance_score'] = score / 10.0  # Normalize to 0-1
            func_info.pop('callable', None)
            results.append(func_info)
        
        logger.info(f"🔍 Keyword search found {len(results)} functions for: {intent}")
        return results
    
    def get_function(self, name: str) -> Optional[Callable]:
        """Get a callable function by name"""
        if name in self.function_registry:
            return self.function_registry[name]['callable']
        return None
    
    def list_functions_by_category(self, category: str) -> List[str]:
        """Get all functions in a category"""
        return [
            name for name, info in self.function_registry.items()
            if info['category'] == category
        ]
    
    def list_functions_by_tag(self, tag: str) -> List[str]:
        """Get all functions with a specific tag"""
        return self.capability_index.get(tag, [])
    
    def register_context(self, scope: str, context_data: Dict[str, Any]):
        """
        Register context/state information.
        
        Args:
            scope: Context scope (e.g., 'blockchain', 'brain', 'files')
            context_data: Context information (paths, states, etc.)
        """
        self.context_graph[scope] = {
            **context_data,
            'updated_at': time.time()
        }
        logger.debug(f"📍 Registered context: {scope}")
    
    def get_context(self, scope: str) -> Optional[Dict[str, Any]]:
        """Get context information for a scope"""
        return self.context_graph.get(scope)
    
    def get_system_map(self) -> Dict[str, Any]:
        """
        Get complete system map for AI to understand available capabilities.
        
        Returns:
            Comprehensive map of all functions, contexts, and capabilities
        """
        return {
            'total_functions': len(self.function_registry),
            'categories': list(set(f['category'] for f in self.function_registry.values())),
            'tags': list(self.capability_index.keys()),
            'contexts': list(self.context_graph.keys()),
            'vector_search_enabled': self.vector_enabled,
            'functions_by_category': {
                cat: self.list_functions_by_category(cat)
                for cat in set(f['category'] for f in self.function_registry.values())
            }
        }
    
    def get_function_details(self, name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific function"""
        if name not in self.function_registry:
            return None
        
        info = self.function_registry[name].copy()
        info['name'] = name
        info.pop('callable', None)  # Don't return callable
        return info
    
    def search_similar_past_actions(self, current_goal: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Search for similar past actions/solutions from memory.
        
        Args:
            current_goal: What the AI is trying to accomplish
            limit: Maximum results
            
        Returns:
            Similar past actions with context
        """
        # This hooks into brain system's memory search
        if not self.brain:
            return []
        
        try:
            # Query brain's semantic memory for similar goals/tasks
            if hasattr(self.brain, 'search_semantic_memory'):
                memories = self.brain.search_semantic_memory(
                    query=current_goal,
                    limit=limit
                )
                
                return [
                    {
                        'past_action': mem.content,
                        'topic': mem.topic,
                        'tags': mem.tags,
                        'timestamp': mem.timestamp,
                        'relevance': mem.relevance if hasattr(mem, 'relevance') else 0.5
                    }
                    for mem in memories
                ]
        except Exception as e:
            logger.warning(f"Failed to search past actions: {e}")
        
        return []
    
    def export_map(self, filepath: str):
        """Export the function registry to a file for inspection"""
        try:
            export_data = {
                'exported_at': time.time(),
                'total_functions': len(self.function_registry),
                'functions': {
                    name: {
                        'description': info['description'],
                        'category': info['category'],
                        'tags': info['tags'],
                        'parameters': info['parameters'],
                        'examples': info['examples']
                    }
                    for name, info in self.function_registry.items()
                },
                'contexts': self.context_graph
            }
            
            with open(filepath, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            logger.info(f"📄 Exported map to {filepath}")
        except Exception as e:
            logger.error(f"Failed to export map: {e}")
    
    def auto_register_from_brain_system(self):
        """
        Auto-register all functions from the brain system.
        This is called during initialization to populate the registry.
        """
        if not self.brain:
            logger.warning("Cannot auto-register: no brain system reference")
            return
        
        registered_count = 0
        
        # Get available_tools from brain system
        if hasattr(self.brain, 'available_tools'):
            for tool_name, tool_func in self.brain.available_tools.items():
                # Extract docstring for description
                description = tool_func.__doc__ or f"Function: {tool_name}"
                description = description.strip().split('\n')[0]  # First line
                
                # Categorize based on name patterns
                category = self._categorize_function(tool_name)
                tags = self._extract_tags(tool_name, description)
                
                self.register_function(
                    name=tool_name,
                    func=tool_func,
                    description=description,
                    category=category,
                    tags=tags
                )
                registered_count += 1
        
        logger.info(f"🗺️  Auto-registered {registered_count} functions from brain system")
    
    def _categorize_function(self, name: str) -> str:
        """Categorize function based on name patterns"""
        name_lower = name.lower()
        
        if any(x in name_lower for x in ['memory', 'store', 'recall', 'remember']):
            return 'memory'
        elif any(x in name_lower for x in ['file', 'write', 'read', 'create']):
            return 'file'
        elif any(x in name_lower for x in ['search', 'query', 'find', 'lookup']):
            return 'search'
        elif any(x in name_lower for x in ['chain', 'cot', 'pipeline', 'think']):
            return 'reasoning'
        elif any(x in name_lower for x in ['personality', 'trait', 'evolve', 'behavioral']):
            return 'personality'
        elif any(x in name_lower for x in ['analyze', 'evaluate', 'assess']):
            return 'analysis'
        elif any(x in name_lower for x in ['blockchain', 'token', 'supply', 'wallet']):
            return 'blockchain'
        else:
            return 'utility'
    
    def _extract_tags(self, name: str, description: str) -> List[str]:
        """Extract semantic tags from function name and description"""
        tags = []
        text = f"{name} {description}".lower()
        
        # Common capability tags
        tag_keywords = {
            'storage': ['store', 'save', 'persist', 'write'],
            'retrieval': ['get', 'load', 'read', 'fetch', 'retrieve'],
            'analysis': ['analyze', 'evaluate', 'assess', 'calculate'],
            'creation': ['create', 'generate', 'make', 'build'],
            'modification': ['modify', 'update', 'change', 'edit'],
            'search': ['search', 'find', 'query', 'lookup'],
            'reasoning': ['think', 'reason', 'ponder', 'reflect'],
            'memory': ['memory', 'remember', 'recall', 'memorize'],
            'file_ops': ['file', 'directory', 'path'],
            'autonomous': ['autonomous', 'self', 'auto'],
            'personality': ['personality', 'trait', 'behavior'],
            'blockchain': ['blockchain', 'token', 'supply', 'wallet', 'transaction']
        }
        
        for tag, keywords in tag_keywords.items():
            if any(keyword in text for keyword in keywords):
                tags.append(tag)
        
        return tags if tags else ['general']


def create_map_sync_network(brain_system) -> MapSyncNetwork:
    """
    Factory function to create and initialize MapSyncNetwork.
    
    Args:
        brain_system: BrainSystem instance
        
    Returns:
        Initialized MapSyncNetwork
    """
    map_network = MapSyncNetwork(brain_system)
    
    # Auto-register existing functions
    map_network.auto_register_from_brain_system()
    
    # Register system contexts
    if hasattr(brain_system, 'brain_path'):
        map_network.register_context('brain', {
            'path': str(brain_system.brain_path),
            'memory_types': ['semantic', 'episodic', 'procedural'],
            'capabilities': ['storage', 'retrieval', 'reasoning', 'personality']
        })
    
    # Register blockchain context if available
    from pathlib import Path
    blockchain_dir = Path(os.environ.get("REPRYNTT_ROBOT_ECONOMY_DIR", str(Path.home() / ".repryntt" / "robot_economy")))
    if blockchain_dir.exists():
        map_network.register_context('blockchain', {
            'path': str(blockchain_dir),
            'blockchain_file': str(blockchain_dir / 'blockchain.json'),
            'balances_file': str(blockchain_dir / 'balances.json'),
            'capabilities': ['analysis', 'token_tracking', 'supply_metrics']
        })
    
    logger.info("🗺️  MapSyncNetwork ready with full system mapping")
    return map_network
