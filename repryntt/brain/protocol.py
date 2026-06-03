"""
BrainSystem protocol — the interface contract.

Every method and attribute listed here is actually called by at least one
file in the repryntt package.  The SAIGE BrainSystem (18 K lines) already
satisfies this protocol via duck-typing; no base class needed.
"""

from __future__ import annotations

from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)


@runtime_checkable
class BrainSystemProtocol(Protocol):
    """Interface contract for BrainSystem implementations.

    26 methods + 12 attributes, derived from an exhaustive audit of every
    ``from brain.brain_system import BrainSystem`` site in the repryntt
    package (6 files, 9 import sites).

    Categories:
        Core AI .............. _call_ai_service
        Chain-of-Thought ..... create_self_autonomous_chain, advance_*, get_chain_context, ...
        Memory ............... store_semantic_memory, store_episodic_memory, brain_network_search, ...
        Personality / State .. personality_brain, recreate_autonomous_personality, ...
        Chat / Expression .... send_to_persistent_chat, express_casual_thought, ...
        Tools ................ execute_tool_call, available_tools
        Lifecycle ............ set_hormone_system
    """

    # ── Attributes ────────────────────────────────────────────────────────
    available_tools: Dict[str, Any]
    personality_brain: Dict[str, Any]
    personality_brain_path: Any          # str | Path
    brain_path: Any                      # str | Path
    output_processor: Any                # AIOutputProcessor
    prompt_generator: Any                # has _build_task_aware_identity()
    ai_provider_config: Dict[str, Any]
    robot_economy_manager: Any           # optional, accessed via getattr
    chat_interface: Any                  # optional, checked via hasattr
    master_ai_queue: Any                 # has .shutdown()
    _daemon_ref: Any                     # set-only back-reference
    _current_agent_id: Optional[str]     # set-only

    # ── Core AI ───────────────────────────────────────────────────────────
    def _call_ai_service(
        self,
        prompt: str,
        *,
        priority: int = 5,
        timeout: int = 120,
        include_tools: bool = True,
    ) -> str: ...

    # ── Chain-of-Thought ──────────────────────────────────────────────────
    def get_chain_context(
        self, chain_id: str, max_tokens: int = 2000
    ) -> str: ...

    def create_self_autonomous_chain(
        self, *, topic: str, goal: str, task_type: str = "research"
    ) -> str: ...

    def advance_self_autonomous_chain(
        self,
        chain_id: str,
        response: str,
        tool_results: Optional[Any] = None,
    ) -> Any: ...

    def advance_chain_of_thought(
        self, chain_id: str, ai_response: str
    ) -> Any: ...

    def create_chain_of_thought(
        self, *, topic: str, goal: str, initial_prompt: str = ""
    ) -> str: ...

    def update_chain_progress(
        self,
        *,
        chain_id: str,
        response: str,
        insights: Optional[List[str]] = None,
        next_questions: Optional[List[str]] = None,
    ) -> Any: ...

    def prompt_ai_conclusion_evaluation(self, chain_id: str) -> Any: ...

    # ── Memory ────────────────────────────────────────────────────────────
    def store_semantic_memory(
        self,
        topic: str = "",
        content: str = "",
        *,
        domain: str = "",
        confidence: float = 1.0,
        source: str = "",
        key_facts: Optional[List[str]] = None,
        related_topics: Optional[List[str]] = None,
    ) -> Any: ...

    def store_episodic_memory(
        self,
        *,
        conversation_id: str = "",
        user_input: str = "",
        ai_response: str = "",
        tool_calls: Optional[List[Any]] = None,
        outcome: str = "",
    ) -> Any: ...

    def brain_network_search(
        self,
        query: str = "",
        *,
        memory_types: Optional[List[str]] = None,
        limit: int = 5,
    ) -> Any: ...

    def get_context_for_question(
        self, prompt: str, max_words: int = 600
    ) -> str: ...

    # ── Personality / State ───────────────────────────────────────────────
    def store_thoughts(self, thoughts: List[str], emotions: Dict[str, float]) -> Any: ...
    def store_self_prompts(self, self_prompts: List[Any]) -> Any: ...
    def update_brain_state(self, thoughts: List[str], self_prompts: List[Any]) -> Any: ...
    def recreate_autonomous_personality(self) -> Any: ...
    def _save_personality_brain(self) -> Any: ...
    def _generate_external_self_prompts(self, limit: int = 3) -> Any: ...
    def _analyze_brain_knowledge_for_gaps(self) -> Any: ...

    # ── Chat / Expression ─────────────────────────────────────────────────
    def send_to_persistent_chat(self, expression: str, type: str = "") -> Any: ...
    def express_casual_thought(self, expression: str, type: str = "") -> Any: ...
    def ask_human_question(self, question: str, reason: str = "") -> Any: ...
    def initiate_conversation(self, topic: str, message: str, mood: str = "") -> Any: ...
    def get_chat_interface_status(self) -> Any: ...

    # ── Tools ─────────────────────────────────────────────────────────────
    def execute_tool_call(self, tool_name: str, parameters: Dict[str, Any]) -> Any: ...

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def set_hormone_system(self, hormone_system: Any) -> None: ...
    def get_current_time(self) -> Dict[str, Any]: ...
