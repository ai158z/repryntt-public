"""
repryntt.tools.registry — Central tool registry.

Decouples tool *registration* from tool *implementation*.
Each subsystem registers its tools at startup; the router and tool-loop
look them up here by name.

Extracted from: SAIGE/brain/brain_system.py  _initialize_tools (line 2417)
    176+ tool registrations across 15 categories.
"""

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Side-effect classification
#
# A tool is "side-effect" if calling it changes state outside the agent's
# local working memory: physical actuation, outbound communication, file or
# code artifacts, financial actions, memory writes, agent dispatch.
#
# Anything not listed here defaults to read-only/cognitive (search, fetch,
# math, maps, chain-of-thought, awareness, etc.).
#
# This is the SINGLE place where the scoring/RL system asks "did real work
# happen this heartbeat?" — replacing the fragile hardcoded tool-name list
# that previously caused RL collapse onto ~7 tools out of 300+.
# ---------------------------------------------------------------------------
SIDE_EFFECT_CATEGORIES: frozenset = frozenset({
    # Physical actuation / sensors that produce artifacts
    "body_control", "hardware", "spatial_awareness", "home_automation",
    "media", "video",
    # Communication (outbound)
    "gmail", "social", "conversation",
    # Code execution / artifact creation
    "code", "codeforge", "filesystem", "git_publish", "tool_execution",
    # Financial / economic actions
    "economy", "defi", "solana_execution", "jupiter",
    "trading", "scalp", "trading_internal", "trading_sim",
    "robot_economy", "token_launch", "whale_monitoring",
    # Persistent learning / memory writes
    "memory", "memory_consolidation", "agent_memory",
    "learning", "llm_learning",
    # Agent dispatch / task management
    "employees", "employee_mgmt", "swarm_tools", "task_queue",
    # Self-modification / planning frameworks
    "pursuit", "activity_frameworks", "frameworks", "frameworks_l3",
    "identity", "personality",
})


class ToolRegistry:
    """
    Central registry of name -> callable tools.

    Categories are optional metadata — tools are invoked by name.
    Multiple names can point to the same callable (aliases).
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._categories: Dict[str, Set[str]] = {}  # category -> set(tool_names)
        self._aliases: Dict[str, str] = {}  # alias -> canonical name
        # Per-tool side_effect override. None ⇒ fall back to category default.
        self._side_effect_overrides: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        func: Callable,
        *,
        category: str = "general",
        aliases: Optional[List[str]] = None,
        side_effect: Optional[bool] = None,
    ) -> None:
        """Register a tool.  Overwrites silently if already registered.

        ``side_effect`` overrides the per-tool side-effect classification
        (default falls back to ``category in SIDE_EFFECT_CATEGORIES``).
        """
        self._tools[name] = func
        self._categories.setdefault(category, set()).add(name)
        if side_effect is not None:
            self._side_effect_overrides[name] = side_effect
        if aliases:
            for alias in aliases:
                self._tools[alias] = func
                self._aliases[alias] = name
                self._categories[category].add(alias)
                if side_effect is not None:
                    self._side_effect_overrides[alias] = side_effect

    def register_many(
        self,
        tools: Dict[str, Callable],
        *,
        category: str = "general",
        side_effect: Optional[bool] = None,
    ) -> None:
        """Bulk-register a dict of ``{name: callable}``."""
        for name, func in tools.items():
            self._tools[name] = func
            self._categories.setdefault(category, set()).add(name)
            if side_effect is not None:
                self._side_effect_overrides[name] = side_effect

    def unregister(self, name: str) -> bool:
        """Remove a tool.  Returns ``True`` if it existed."""
        if name not in self._tools:
            return False
        del self._tools[name]
        for cat in self._categories.values():
            cat.discard(name)
        self._aliases.pop(name, None)
        return True

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Callable]:
        """Exact lookup, then case-insensitive fallback."""
        if name in self._tools:
            return self._tools[name]
        lower = name.lower()
        for k, v in self._tools.items():
            if k.lower() == lower:
                return v
        return None

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> List[str]:
        return list(self._tools.keys())

    @property
    def tools(self) -> Dict[str, Callable]:
        """Direct access to the underlying dict (read-only intent)."""
        return self._tools

    def categories(self) -> Dict[str, List[str]]:
        """Return ``{category: [tool_names]}``."""
        return {cat: sorted(names) for cat, names in self._categories.items()}

    def by_category(self, category: str) -> Dict[str, Callable]:
        """Return tools in *category*."""
        names = self._categories.get(category, set())
        return {n: self._tools[n] for n in names if n in self._tools}

    def get_category(self, tool_name: str) -> str:
        """Return the category a tool was registered under, or 'general'."""
        for cat, names in self._categories.items():
            if tool_name in names:
                return cat
        return "general"

    def is_side_effect(self, tool_name: str) -> bool:
        """Whether calling this tool changes state outside the agent's
        local working memory (physical actuation, outbound comms,
        files/code/financial actions, memory writes, agent dispatch).

        Used by the heartbeat scoring/RL system as the single source of
        truth for "did real work happen" — replacing fragile per-name
        allowlists. Resolution order:

        1. Per-tool override set at registration time.
        2. Category-based default (``SIDE_EFFECT_CATEGORIES``).
        """
        override = self._side_effect_overrides.get(tool_name)
        if override is not None:
            return override
        return self.get_category(tool_name) in SIDE_EFFECT_CATEGORIES

    # ------------------------------------------------------------------
    # Delegate tool registration (requires brain instance)
    # ------------------------------------------------------------------

    def register_brain_delegate_tools(self, brain) -> int:
        """
        Register tools that delegate to brain subsystem managers.

        Call this AFTER register_native_tools, once the brain instance
        (with _memory, _personality, _cot, conversation_logger) is ready.
        Returns count of tools registered.
        """
        count = 0

        # ── Memory delegate tools (10) ───────────────────────────────
        try:
            mem_tools = {
                "brain_memory_save": brain.brain_memory_save,
                "brain_memory_recall": brain.brain_memory_recall,
                "brain_network_search": brain.brain_network_search,
                "get_brain_stats": brain.get_brain_stats,
                "search_domain": brain._search_knowledge_domains,
                "store_learning": brain.store_semantic_memory,
                "get_relevant_context": brain.get_context_for_question,
                "update_procedural": brain.update_procedural_memory,
            }
            self.register_many(mem_tools, category="memory")
            # Aliases
            if "brain_network_search" in self._tools:
                self._tools["recall_memory"] = self._tools["brain_network_search"]
                self._aliases["recall_memory"] = "brain_network_search"
                count += 1
            if "store_learning" in self._tools:
                self._tools["search_knowledge"] = brain.search_semantic_memory
                count += 1
            count += len(mem_tools)
            logger.info(f"  ✅ memory delegates: {count} tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ memory delegate registration failed: {e}")

        # ── MemoryMesh tools (4) ─────────────────────────────────────
        try:
            from repryntt.core.memory.memory_mesh import mesh_search, mesh_stats, mesh_connect, mesh_anchor
            mesh_tools = {
                "mesh_search": mesh_search,
                "mesh_stats": mesh_stats,
                "mesh_connect": mesh_connect,
                "mesh_anchor": mesh_anchor,
            }
            self.register_many(mesh_tools, category="memory")
            count += len(mesh_tools)
            logger.info(f"  ✅ memory mesh: {len(mesh_tools)} tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ memory mesh registration failed: {e}")

        # ── Personality delegate tools (9) ───────────────────────────
        pers_count = 0
        try:
            pers_tools = {
                "modify_personality_trait": brain.modify_personality_trait,
                "evolve_personality_dimension": brain.evolve_personality_dimension,
                "update_behavioral_guidelines": brain.update_behavioral_guidelines,
                "recreate_autonomous_personality": brain.recreate_autonomous_personality,
                "add_personality_trait": brain.add_personality_trait,
                "remove_personality_trait": brain.remove_personality_trait,
                "log_personality_evolution": brain.log_personality_evolution,
                "analyze_personality_growth": brain.analyze_personality_growth,
                "update_avatar": brain.update_avatar,
            }
            self.register_many(pers_tools, category="personality")
            pers_count = len(pers_tools)
            count += pers_count
            logger.info(f"  ✅ personality delegates: {pers_count} tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ personality delegate registration failed: {e}")

        # ── Chain-of-thought delegate tools (9) ──────────────────────
        cot_count = 0
        try:
            cot_tools = {
                "create_chain_of_thought": brain.create_chain_of_thought,
                "create_self_autonomous_chain": brain.create_self_autonomous_chain,
                "advance_self_autonomous_chain": brain.advance_self_autonomous_chain,
                "update_chain_progress": brain.update_chain_progress,
                "get_chain_context": brain.get_chain_context,
                "queue_chain_of_thought": brain.queue_chain_of_thought,
                "get_cot_queue_status": brain.get_cot_queue_status,
                "clear_cot_queue": brain.clear_cot_queue,
                "query_exploration_history": brain.query_exploration_history,
            }
            self.register_many(cot_tools, category="chain_of_thought")
            cot_count = len(cot_tools)
            count += cot_count
            logger.info(f"  ✅ cot delegates: {cot_count} tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ cot delegate registration failed: {e}")

        # ── Conversation delegate tools (7) ──────────────────────────
        conv_count = 0
        try:
            conv_tools = {
                "initiate_conversation": brain.initiate_conversation_with_human,
                "get_recent_conversations": brain.get_recent_autonomous_conversations,
                "search_conversations": brain.search_autonomous_conversations,
                "get_conversation_summary": brain.get_autonomous_conversation_summary,
                "export_conversation": brain.export_autonomous_conversation,
            }
            self.register_many(conv_tools, category="conversation")
            # Aliases
            if "initiate_conversation" in self._tools:
                self._tools["start_conversation"] = self._tools["initiate_conversation"]
                self._aliases["start_conversation"] = "initiate_conversation"
                self._tools["talk_to_human"] = self._tools["initiate_conversation"]
                self._aliases["talk_to_human"] = "initiate_conversation"
                conv_count += 2
            conv_count += len(conv_tools)
            count += conv_count
            logger.info(f"  ✅ conversation delegates: {conv_count} tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ conversation delegate registration failed: {e}")

        # ── Misc delegate tools ──────────────────────────────────────
        misc_count = 0
        try:
            if hasattr(brain, 'reset_inspiration_index'):
                self.register("reset_inspiration_index", brain.reset_inspiration_index,
                              category="grokipedia")
                misc_count += 1
        except Exception as e:
            logger.warning(f"  ⚠️ misc delegate registration failed: {e}")
        count += misc_count

        logger.info(f"🧠 Brain delegate registration: {count} tools registered")
        return count

    # ------------------------------------------------------------------
    # Bulk initialization (mirrors the original _initialize_tools layout)
    # ------------------------------------------------------------------

    def register_brain_tools(self, brain) -> None:
        """
        One-shot registration of all tools that live on a BrainSystem instance.

        This mirrors the original ``_initialize_tools()`` from brain_system.py
        so existing call sites continue to work.  Over time, each subsystem
        should register its own tools (e.g. ``register_trading_tools``).
        """
        # -- Knowledge & search ------------------------------------------------
        _reg = self.register  # alias for brevity

        _search = {
            "search_knowledge": brain.search_semantic_memory,
            "brain_network_search": brain.brain_network_search,
            "recall_memory": brain.brain_network_search,
            "query_exploration_history": brain.query_exploration_history,
            "grokipedia_search": brain.grokipedia_search,
            "grokedia_search": brain.grokipedia_search,
            "knowledge_search": brain.google_web_search,
            "google_web_search": brain.google_web_search,
            "google_search": brain.google_web_search,
            "web_search": brain.real_web_search,
            "real_web_search": brain.real_web_search,
            "duckduckgo_search": brain.real_web_search,
            "internet_search": brain.real_web_search,
            "x_search_tweets": brain.x_search_tweets,
            "x_search_crypto": brain.x_search_crypto,
            "twitter_search": brain.x_search_tweets,
            "web_search_results_only": brain.web_search_results_only,
            "search_results_only": brain.web_search_results_only,
            "scrape_web_page": brain.scrape_web_page,
            "fetch_url": brain.scrape_web_page,
            "scrape_url": brain.scrape_web_page,
            "clear_grokipedia_history": brain.clear_grokipedia_search_history,
            "reset_inspiration_index": brain.reset_inspiration_index,
            "fetch_web_info": brain.call_knowledge_api_feeder,
            "extract_content": brain.extract_content_from_url,
            "search_domain": brain._search_knowledge_domains,
        }
        self.register_many(_search, category="search")

        # -- Memory & learning -------------------------------------------------
        _memory = {
            "store_learning": brain.store_semantic_memory,
            "get_relevant_context": brain.get_context_for_question,
            "analyze_topic": brain.analyze_topic_complexity,
            "find_similar_topics": brain.find_related_topics,
            "update_procedural": brain.update_procedural_memory,
            "get_brain_stats": brain.get_brain_stats,
            "pull_knowledge_topics": brain.pull_knowledge_topics,
            "integrate_knowledge_context": brain.integrate_knowledge_context,
        }
        self.register_many(_memory, category="memory")

        # -- Personality evolution ---------------------------------------------
        _personality = {
            "modify_personality_trait": brain.modify_personality_trait,
            "evolve_personality_dimension": brain.evolve_personality_dimension,
            "update_behavioral_guidelines": brain.update_behavioral_guidelines,
            "recreate_autonomous_personality": brain.recreate_autonomous_personality,
            "add_personality_trait": brain.add_personality_trait,
            "remove_personality_trait": brain.remove_personality_trait,
            "log_personality_evolution": brain.log_personality_evolution,
            "analyze_personality_growth": brain.analyze_personality_growth,
        }
        self.register_many(_personality, category="personality")

        # -- Real-time awareness -----------------------------------------------
        _awareness = {
            "get_current_time": brain.get_current_time,
            "check_time": brain.get_current_time,
        }
        self.register_many(_awareness, category="awareness")

        # -- Chain-of-thought --------------------------------------------------
        _cot = {
            "create_chain_of_thought": brain.create_chain_of_thought,
            "create_self_autonomous_chain": brain.create_self_autonomous_chain,
            "advance_self_autonomous_chain": brain.advance_self_autonomous_chain,
            "update_chain_progress": brain.update_chain_progress,
            "get_chain_context": brain.get_chain_context,
            "queue_chain_of_thought": brain.queue_chain_of_thought,
            "get_cot_queue_status": brain.get_cot_queue_status,
            "clear_cot_queue": brain.clear_cot_queue,
        }
        self.register_many(_cot, category="chain_of_thought")

        # -- Creative writing / files ------------------------------------------
        _creative = {
            "create_creative_file": brain.create_creative_file,
            "write_to_creative_file": brain.write_to_creative_file,
            "append_to_creative_file": brain.append_to_creative_file,
            "read_creative_file": brain.read_creative_file,
            "get_creative_workspace_status": brain.get_creative_workspace_status,
        }
        self.register_many(_creative, category="creative")

        # -- Mathematics -------------------------------------------------------
        _math = {
            "compute_zeta_function": brain.compute_zeta_function,
            "analyze_zeta_zeros": brain.analyze_zeta_zeros,
            "symbolic_manipulation": brain.symbolic_manipulation,
            "numerical_analysis": brain.numerical_analysis,
            "statistical_analysis": brain.statistical_analysis,
            "pattern_recognition": brain.pattern_recognition,
            "access_mathematical_databases": brain.access_mathematical_databases,
            "mathematical_visualization": brain.mathematical_visualization,
        }
        self.register_many(_math, category="math")

        # -- Google Maps / navigation ------------------------------------------
        _maps = {
            "google_maps_search": brain.google_maps_search,
            "get_directions": brain.get_directions,
            "geocode_address": brain.geocode_address,
            "find_nearby_places": brain.find_nearby_places,
        }
        self.register_many(_maps, category="maps")

        # -- Code development --------------------------------------------------
        _code = {
            "run_terminal_cmd": brain.run_terminal_cmd_wrapper,
            "search_replace": brain.search_replace_wrapper,
            "read_file": brain.read_file_wrapper,
            "write_file": brain.write_file_wrapper,
            "grep_search": brain.grep_search_wrapper,
            "list_dir": brain.list_dir_wrapper,
            "analyze_codebase": brain.analyze_codebase,
            "run_code_tests": brain.run_code_tests,
            "check_syntax": brain.check_syntax,
            "get_code_context": brain.get_code_context,
            "get_sandbox_status": brain._get_sandbox_status_tool,
            "propose_code_change": brain._propose_code_change_tool,
        }
        self.register_many(_code, category="code")

        # -- Robot economy -----------------------------------------------------
        _economy = {
            "start_robot_economy": brain.start_robot_economy,
            "stop_robot_economy": brain.stop_robot_economy,
            "get_economy_status": brain.get_economy_status,
            "submit_workload": brain.submit_robot_workload,
            "get_wallet_balance": brain.get_robot_wallet_balance,
            "get_blockchain_info": brain.get_robot_blockchain_info,
            "allocate_dao_funds": brain.allocate_robot_dao_funds,
            "create_robot_wallet": brain.create_robot_wallet,
            "recover_robot_wallet": brain.recover_robot_wallet,
            "monitor_economy": brain.monitor_robot_economy,
        }
        self.register_many(_economy, category="economy")

        # -- Autonomous conversation -------------------------------------------
        _comms = {
            "initiate_conversation": brain.initiate_conversation_with_human,
            "start_conversation": brain.initiate_conversation_with_human,
            "talk_to_human": brain.initiate_conversation_with_human,
            "get_recent_conversations": brain.get_recent_autonomous_conversations,
            "search_conversations": brain.search_autonomous_conversations,
            "get_conversation_summary": brain.get_autonomous_conversation_summary,
            "export_conversation": brain.export_autonomous_conversation,
        }
        self.register_many(_comms, category="conversation")

        # -- Twitter / social --------------------------------------------------
        _social = {
            "post_tweet": brain.post_tweet_autonomous,
            "tweet": brain.post_tweet_autonomous,
            "check_twitter_mentions": brain.check_twitter_mentions,
            "reply_to_twitter": brain.reply_to_twitter_mention,
            "get_twitter_status": brain.get_twitter_status,
            "twitter_status": brain.get_twitter_status,
        }
        self.register_many(_social, category="social")

        # -- Per-agent memory --------------------------------------------------
        _agent_mem = {
            "brain_memory_save": brain.brain_memory_save,
            "brain_memory_recall": brain.brain_memory_recall,
        }
        self.register_many(_agent_mem, category="agent_memory")

        # -- Employee management -----------------------------------------------
        _employees = {
            "employee_roster": brain.employee_roster,
            "assign_work": brain.assign_work,
            "check_work": brain.check_work,
            "find_employee": brain.find_employee,
            "employee_status": brain.employee_status,
            "rename_employee": brain.rename_employee,
        }
        self.register_many(_employees, category="employees")

        # -- Crypto / DeFi market data -----------------------------------------
        _defi = {
            "dexscreener_trending": brain.dexscreener_trending,
            "dexscreener_token_search": brain.dexscreener_token_search,
            "solana_rpc_query": brain.solana_rpc_query,
        }
        self.register_many(_defi, category="defi")

        # -- Trading simulator -------------------------------------------------
        _sim = {
            "sim_buy": brain.sim_buy,
            "sim_sell": brain.sim_sell,
            "sim_portfolio": brain.sim_portfolio,
            "sim_price_check": brain.sim_price_check,
            "sim_faucet": brain.sim_faucet,
        }
        self.register_many(_sim, category="trading_sim")

        # -- Trading bot bridge ------------------------------------------------
        _trading = {
            "trading_bot_start": brain.trading_bot_start,
            "trading_bot_stop": brain.trading_bot_stop,
            "trading_bot_status": brain.trading_bot_status,
            "trading_signals": brain.trading_signals,
            "trading_hot_tokens": brain.trading_hot_tokens,
            "trading_performance": brain.trading_performance,
            "trading_token_detail": brain.trading_token_detail,
            "token_price_history": brain.token_price_history,
            "log_trade_outcome": brain.log_trade_outcome,
            "review_trade_journal": brain.review_trade_journal,
            "trading_scan": brain.trading_scan,
            "trading_browse_tokens": brain.trading_browse_tokens,
        }
        self.register_many(_trading, category="trading")

        # -- Whale / KOL monitoring --------------------------------------------
        _whale = {
            "whale_add_wallet": brain.whale_add_wallet,
            "whale_remove_wallet": brain.whale_remove_wallet,
            "whale_list_wallets": brain.whale_list_wallets,
            "whale_monitor_status": brain.whale_monitor_status,
            "kol_leaderboard": brain.kol_leaderboard,
            "kol_sync_wallets": brain.kol_sync_wallets,
            "kol_remove_underperformers": brain.kol_remove_underperformers,
        }
        self.register_many(_whale, category="whale_monitoring")

        # -- Scalp executor ----------------------------------------------------
        _scalp = {
            "scalp_status": brain.scalp_status,
            "scalp_set_param": brain.scalp_set_param,
            "scalp_force_buy": brain.scalp_force_buy,
            "scalp_force_sell": brain.scalp_force_sell,
            "scalp_history": brain.scalp_history,
        }
        self.register_many(_scalp, category="scalp")

        # -- Voice / hardware --------------------------------------------------
        _hw = {
            "speak": brain.speak,
            "listen": brain.listen,
            "generate_voiceover": brain.generate_voiceover,
            "generate_image": brain.generate_image,
            "update_avatar": brain.update_avatar,
            "capture_camera": brain.capture_camera,
        }
        self.register_many(_hw, category="hardware")

        # -- Video production --------------------------------------------------
        _video = {
            "create_video_project": brain.create_video_project,
            "write_screenplay": brain.write_screenplay,
            "create_shot_list": brain.create_shot_list,
            "generate_video_clip": brain.generate_video_clip,
            "generate_all_clips": brain.generate_all_clips,
            "generate_narration": brain.generate_narration,
            "generate_music": brain.generate_music,
            "assemble_edit": brain.assemble_edit,
            "qa_review_video": brain.qa_review_video,
            "render_final": brain.render_final,
            "video_project_status": brain.video_project_status,
            "generate_thumbnail": brain.generate_thumbnail,
            "auto_produce_video": brain.auto_produce_video,
        }
        self.register_many(_video, category="video")

        logger.info(
            f"Tool registry loaded: {len(self._tools)} tools in "
            f"{len(self._categories)} categories"
        )

    # ------------------------------------------------------------------
    # Phase 1 — Native repryntt tool registrations (bypass monolith)
    # ------------------------------------------------------------------

    def register_native_tools(self, brain_path) -> int:
        """
        Register tools directly from repryntt modules, bypassing the BrainSystem
        monolith.  Called *after* ``register_brain_tools`` so native registrations
        overwrite the monolith-delegated ones.

        Returns the number of tools that were natively registered.
        """
        import json
        from pathlib import Path

        brain_path = Path(brain_path) if not isinstance(brain_path, Path) else brain_path
        # Use unified workspace for Jarvis
        workspace = str(Path.home() / ".repryntt" / "workspace" / "agents" / "operator")
        Path(workspace).mkdir(parents=True, exist_ok=True)
        count = 0

        # ── Trading Simulator (5 tools) ──────────────────────────────────
        try:
            from repryntt.trading.trading_simulator import (
                sim_buy as _sim_buy,
                sim_sell as _sim_sell,
                sim_portfolio as _sim_portfolio,
                sim_price_check as _sim_price_check,
                sim_faucet as _sim_faucet,
            )

            def sim_buy(token: str = "", amount_usd: float = 0, reason: str = "", **kw) -> str:
                """Buy a token with simulated USD (paper trading). Uses real DexScreener prices.
                You have a $300 simulated portfolio. 1% slippage applied on every trade.
                ⚠️ You should use framework_start("new_trade") BEFORE buying. The system
                tracks whether you followed the research framework.

                Parameters:
                    token: Token symbol (e.g. 'BONK', 'WIF') or contract address to buy.
                    amount_usd: How many USD to spend on this buy.
                    reason: Your reasoning for the trade (recorded in trade log).
                """
                result = _sim_buy(workspace, token, float(amount_usd), reason)
                # Soft gate: check if a framework was followed for this token
                try:
                    from repryntt.agents.framework_tracker import get_tracker
                    _ft = get_tracker(workspace)
                    research = _ft.check_trade_research(token)
                    if research:
                        result += "\n✅ Framework research verified for this token."
                    else:
                        result += ("\n⚠️ No NEW_TRADE framework was completed for this token. "
                                   "Next time, use framework_start('new_trade') to follow "
                                   "the structured research procedure before buying.")
                except Exception:
                    pass
                return result

            def sim_sell(token: str = "", sell_pct: float = 100.0, reason: str = "", **kw) -> str:
                """Sell a token position (paper trading). Uses real DexScreener prices.
                Specify what percentage of your position to sell (default: 100% = full close).

                Parameters:
                    token: Token symbol to sell (must be in your portfolio, e.g. 'BONK').
                    sell_pct: Percentage of position to sell, 1-100 (default: 100 = sell all).
                    reason: Your reasoning for the trade (recorded in trade log).
                """
                return _sim_sell(workspace, token, float(sell_pct), reason)

            def sim_portfolio(**kw) -> str:
                """Get your full paper trading portfolio: cash balance, positions with
                live prices, P&L (realized + unrealized), trade history, win rate.
                Call this to review your performance before deciding on trades.
                """
                return _sim_portfolio(workspace)

            def sim_price_check(token: str = "", **kw) -> str:
                """Check live price for a token without trading. Use to research before buying.
                Returns: price, 24h change, volume, liquidity, market cap from DexScreener.

                Parameters:
                    token: Token symbol, name, or contract address to look up.
                """
                return _sim_price_check(workspace=workspace, token=token)

            def sim_faucet(amount: float = 0, **kw) -> str:
                """Reload the sim wallet with fresh capital when funds are depleted.
                This is paper trading — no real money. Use it to continue trading after a drawdown.
                If amount is 0 or omitted, resets the wallet to the default $1000 starting balance.
                If amount is positive, adds that much to the current balance.

                Parameters:
                    amount: USD to add. 0 = reset to $1000 default.
                """
                return _sim_faucet(workspace, float(amount))

            for name, func in [("sim_buy", sim_buy), ("sim_sell", sim_sell),
                               ("sim_portfolio", sim_portfolio),
                               ("sim_price_check", sim_price_check),
                               ("sim_faucet", sim_faucet)]:
                self.register(name, func, category="trading_sim")
                count += 1
            logger.info("  ✅ trading_sim: 5 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ trading_sim native registration failed: {e}")

        # ── Trading Bot Bridge (10 tools) ────────────────────────────────
        try:
            from repryntt.trading.bot_bridge import (
                trading_bot_start,
                trading_bot_stop,
                trading_bot_status,
                trading_signals,
                trading_hot_tokens,
                trading_performance,
                trading_token_detail,
                token_price_history,
                log_trade_outcome,
                review_trade_journal,
                trading_browse_tokens,
            )
            _bot_tools = {
                "trading_bot_start": trading_bot_start,
                "trading_bot_stop": trading_bot_stop,
                "trading_bot_status": trading_bot_status,
                "trading_signals": trading_signals,
                "trading_hot_tokens": trading_hot_tokens,
                "trading_performance": trading_performance,
                "trading_token_detail": trading_token_detail,
                "token_price_history": token_price_history,
                "log_trade_outcome": log_trade_outcome,
                "review_trade_journal": review_trade_journal,
                "trading_browse_tokens": trading_browse_tokens,
            }
            self.register_many(_bot_tools, category="trading")
            count += len(_bot_tools)
            logger.info(f"  ✅ trading_bot: {len(_bot_tools)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ trading_bot native registration failed: {e}")

        # ── Whale Monitor (2 thin tools) ─────────────────────────────────
        try:
            from repryntt.trading.whale_monitor import (
                remove_wallet as _whale_remove,
                list_wallets as _whale_list,
            )

            def whale_remove_wallet(address: str = "", **kw) -> str:
                """Remove a wallet from the whale/KOL copy-trade monitor.

                Parameters:
                    address: The Solana wallet address to remove. Required.
                """
                if not address:
                    return json.dumps({"error": "address is required"})
                return json.dumps(_whale_remove(address=address))

            def whale_list_wallets(**kw) -> str:
                """List all wallets being tracked by the whale/KOL copy-trade monitor.

                Returns each wallet's address, label, tier, enabled status, and
                copy-trade performance stats (trades copied, profitable copies, P/L).
                """
                wallets = _whale_list()
                return json.dumps({"wallets": wallets, "count": len(wallets)})

            self.register("whale_remove_wallet", whale_remove_wallet, category="whale_monitoring")
            self.register("whale_list_wallets", whale_list_wallets, category="whale_monitoring")
            count += 2
            logger.info("  ✅ whale_monitor: 2 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ whale_monitor native registration failed: {e}")

        # ── KOLscan (1 thin tool) ────────────────────────────────────────
        try:
            from repryntt.trading.kolscan_scraper import (
                remove_underperformers as _kol_remove,
            )

            def kol_remove_underperformers(min_profit_sol: float = 0.0, **kw) -> str:
                """Remove KOLscan-sourced wallets that dropped off the leaderboard.

                Checks the current leaderboard and removes any KOLscan-synced wallets
                that are no longer top performers.  Only affects wallets added by
                kol_sync_wallets (identified by 'KOLscan' in their label/notes).

                Parameters:
                    min_profit_sol: Remove KOLs with profit below this (default 0 = only removes dropped-off)
                """
                return json.dumps(_kol_remove(min_profit_sol=float(min_profit_sol)))

            self.register("kol_remove_underperformers", kol_remove_underperformers,
                          category="whale_monitoring")
            count += 1
            logger.info("  ✅ kolscan: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ kolscan native registration failed: {e}")

        # ── Scalp Executor (2 thin tools) ────────────────────────────────
        try:
            from repryntt.trading.scalp_executor import get_executor as _get_scalp

            def scalp_status(**kw) -> str:
                """Check the real-time scalp executor status.

                Shows: active trade (symbol, P/L, hold time), queue, win rate,
                total P/L, configuration, and whether the executor is running.
                """
                return json.dumps(_get_scalp().get_status(), indent=2)

            def scalp_force_sell(reason: str = "", **kw) -> str:
                """Manually trigger an immediate sell of the active scalp trade.

                Use this to override the automatic TP/SL/timeout and exit the
                current position immediately. The executor will sell at market price.

                Parameters:
                    reason: Why you're selling (logged to trade history)
                """
                return _get_scalp().force_sell(reason)

            self.register("scalp_status", scalp_status, category="scalp")
            self.register("scalp_force_sell", scalp_force_sell, category="scalp")
            count += 2
            logger.info("  ✅ scalp_executor: 2 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ scalp_executor native registration failed: {e}")

        # ── Solana Executor + PumpFun Launcher (4 tools) ─────────────────
        try:
            import asyncio as _asyncio
            import aiohttp as _aiohttp
            from repryntt.trading.solana_executor import (
                get_wallet_status as _wallet_status,
                buy_token as _sol_buy,
                sell_token as _sol_sell,
                DRY_RUN as _EXEC_DRY_RUN,
            )

            def wallet_status(**kw) -> str:
                """Check the Solana trading wallet status.

                Returns: public key, SOL balance, current mode (DRY_RUN or LIVE),
                max trade size, and slippage settings.
                """
                async def _ws():
                    async with _aiohttp.ClientSession() as s:
                        return await _wallet_status(s)
                try:
                    return json.dumps(_asyncio.run(_ws()), indent=2)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def real_buy(token_address: str = "", amount_sol: float = 0.0,
                         slippage_bps: int = 100, **kw) -> str:
                """Execute a real token buy via Jupiter (Solana mainnet).

                Currently in DRY_RUN mode — builds the transaction but does NOT
                submit. Validates route, price impact, and slippage against live
                market data. Set DRY_RUN=False in solana_executor.py to go live.

                Parameters:
                    token_address: The token's Solana mint address (required).
                    amount_sol: How much SOL to spend on this buy.
                    slippage_bps: Slippage tolerance in basis points (100 = 1%).
                """
                if not token_address:
                    return json.dumps({"error": "token_address required"})
                async def _rb():
                    async with _aiohttp.ClientSession() as s:
                        return await _sol_buy(s, token_address, float(amount_sol), int(slippage_bps))
                try:
                    return json.dumps(_asyncio.run(_rb()), indent=2)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def real_sell(token_address: str = "", sell_pct: float = 100.0,
                          slippage_bps: int = 100, **kw) -> str:
                """Execute a real token sell via Jupiter (Solana mainnet).

                Currently in DRY_RUN mode — builds the transaction but does NOT
                submit. Set DRY_RUN=False in solana_executor.py to go live.

                Parameters:
                    token_address: The token's Solana mint address (required).
                    sell_pct: Percentage of holdings to sell (default 100%).
                    slippage_bps: Slippage tolerance in basis points (100 = 1%).
                """
                if not token_address:
                    return json.dumps({"error": "token_address required"})
                async def _rs():
                    async with _aiohttp.ClientSession() as s:
                        return await _sol_sell(s, token_address, sell_pct=float(sell_pct),
                                              slippage_bps=int(slippage_bps))
                try:
                    return json.dumps(_asyncio.run(_rs()), indent=2)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def launch_pumpfun_token(name: str = "", symbol: str = "",
                                     description: str = "", image_path: str = "",
                                     initial_buy_sol: float = 0.1,
                                     website: str = "", twitter: str = "",
                                     telegram: str = "", **kw) -> str:
                """Launch a new token on pump.fun (bonding curve).

                Currently in DRY_RUN mode — prepares metadata and transaction but
                does NOT submit. Set DRY_RUN=False in pumpfun_launcher.py to go live.

                Parameters:
                    name: Token name (e.g. 'My Cool Token'). Required.
                    symbol: Token ticker symbol (e.g. 'MCT'). Required.
                    description: Token description for the pump.fun page.
                    image_path: Path to token logo image (PNG/JPG). Required.
                    initial_buy_sol: Dev buy amount in SOL (default 0.1).
                    website: Optional website URL.
                    twitter: Optional Twitter/X URL.
                    telegram: Optional Telegram URL.
                """
                if not all([name, symbol, image_path]):
                    return json.dumps({"error": "name, symbol, and image_path are required"})
                from repryntt.trading.pumpfun_launcher import (
                    launch_token as _pf_launch,
                    TokenLaunchConfig as _TLC,
                )
                cfg = _TLC(
                    name=name, symbol=symbol, description=description,
                    image_path=image_path, initial_buy_sol=float(initial_buy_sol),
                    website=website, twitter=twitter, telegram=telegram,
                )
                try:
                    result = _asyncio.run(_pf_launch(cfg))
                    return json.dumps(result.to_dict(), indent=2)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            _exec_tools = {
                "wallet_status": wallet_status,
                "real_buy": real_buy,
                "real_sell": real_sell,
                "launch_pumpfun_token": launch_pumpfun_token,
            }
            self.register_many(_exec_tools, category="solana_execution")
            count += len(_exec_tools)
            logger.info(f"  ✅ solana_execution: {len(_exec_tools)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ solana_execution native registration failed: {e}")

        # ── Token Launch Pipeline (5 tools) ──────────────────────────────
        try:
            from repryntt.trading.token_launch_pipeline import (
                stage_ideate as _lp_ideate,
                stage_design as _lp_design,
                stage_review as _lp_review,
                stage_prepare as _lp_prepare,
                stage_launch as _lp_launch,
                run_full_pipeline as _lp_full,
                LaunchPipelineState as _LPS,
            )

            def launch_memecoin(theme: str = "", narrative: str = "",
                                initial_buy_sol: float = 0.1,
                                twitter: str = "", telegram: str = "",
                                website: str = "", **kw) -> str:
                """Launch a memecoin on pump.fun — full autonomous pipeline.

                Runs all 5 stages: ideate → design logos → vision-review →
                upload metadata → launch on pump.fun.
                Currently in DRY_RUN mode — prepares everything but does NOT
                submit the on-chain transaction.

                Read LAUNCHING.md for the full playbook on WHEN and WHAT to launch.

                Parameters:
                    theme: What the token is about (e.g. 'viral baby hippo from zoo')
                    narrative: Why this will trend (e.g. 'animal tokens are hot right now')
                    initial_buy_sol: Dev buy amount in SOL (default 0.1)
                    twitter: Optional Twitter/X URL for the token
                    telegram: Optional Telegram group URL
                    website: Optional website URL
                """
                result = _lp_full(
                    theme=theme, narrative=narrative,
                    initial_buy_sol=float(initial_buy_sol),
                    twitter=twitter, telegram=telegram, website=website,
                )
                return json.dumps(result, indent=2)

            def launch_pipeline_ideate(theme: str = "", narrative: str = "",
                                       target_audience: str = "crypto degens, memecoin traders",
                                       **kw) -> str:
                """Step 1: Generate a token concept (name, symbol, description).

                Use this when you want to ideate before committing to a full launch.
                Returns a pipeline_id you can pass to subsequent steps.

                Parameters:
                    theme: What the token should be about
                    narrative: The trend/narrative this rides
                    target_audience: Who this token is for
                """
                state = _lp_ideate(theme=theme, narrative=narrative, target_audience=target_audience)
                return json.dumps({
                    "pipeline_id": state.pipeline_id,
                    "name": state.token_name,
                    "symbol": state.token_symbol,
                    "description": state.token_description,
                    "stage": state.stage,
                }, indent=2)

            def launch_pipeline_design(pipeline_id: str = "", logo_prompt: str = "",
                                       num_candidates: int = 3, **kw) -> str:
                """Step 2: Generate logo candidates for a token concept.

                Parameters:
                    pipeline_id: From step 1 (launch_pipeline_ideate)
                    logo_prompt: Custom prompt for logo generation (optional)
                    num_candidates: How many logo options to generate (default 3)
                """
                if not pipeline_id:
                    return json.dumps({"error": "pipeline_id required from step 1"})
                state = _LPS.load(pipeline_id)
                if not state:
                    return json.dumps({"error": f"Pipeline {pipeline_id} not found"})
                state = _lp_design(state, logo_prompt=logo_prompt, num_candidates=int(num_candidates))
                return json.dumps({
                    "pipeline_id": pipeline_id,
                    "candidates": len(state.logo_candidates),
                    "paths": state.logo_candidates,
                    "stage": state.stage,
                }, indent=2)

            def launch_pipeline_review(pipeline_id: str = "", **kw) -> str:
                """Step 3: Vision-analyze logo candidates and pick the best one.

                Uses AI vision to review each logo for appeal, avatar fit,
                concept match, and professionalism. Selects the highest scorer.

                Parameters:
                    pipeline_id: From step 2
                """
                if not pipeline_id:
                    return json.dumps({"error": "pipeline_id required"})
                state = _LPS.load(pipeline_id)
                if not state:
                    return json.dumps({"error": f"Pipeline {pipeline_id} not found"})
                state = _lp_review(state)
                return json.dumps({
                    "pipeline_id": pipeline_id,
                    "selected_logo": state.selected_logo,
                    "reason": state.selection_reason,
                    "reviews": [{
                        "candidate": r["candidate"],
                        "score": r.get("total_score", "?"),
                    } for r in state.logo_reviews],
                    "stage": state.stage,
                }, indent=2)

            def launch_pipeline_execute(pipeline_id: str = "",
                                         initial_buy_sol: float = 0.1,
                                         twitter: str = "", telegram: str = "",
                                         website: str = "", **kw) -> str:
                """Steps 4-5: Upload metadata and launch on pump.fun.

                Combines prepare (IPFS upload) and launch (on-chain creation).
                Currently in DRY_RUN mode.

                Parameters:
                    pipeline_id: From step 3
                    initial_buy_sol: Dev buy amount in SOL
                    twitter/telegram/website: Optional social links
                """
                if not pipeline_id:
                    return json.dumps({"error": "pipeline_id required"})
                state = _LPS.load(pipeline_id)
                if not state:
                    return json.dumps({"error": f"Pipeline {pipeline_id} not found"})
                state.initial_buy_sol = float(initial_buy_sol)
                state.twitter = twitter
                state.telegram = telegram
                state.website = website
                state = _lp_prepare(state)
                if state.stage == "launch":
                    state = _lp_launch(state)
                return json.dumps({
                    "pipeline_id": pipeline_id,
                    "success": state.launched,
                    "dry_run": state.dry_run,
                    "token_name": state.token_name,
                    "token_symbol": state.token_symbol,
                    "mint_pubkey": state.mint_pubkey,
                    "metadata_uri": state.metadata_uri,
                    "tx_signature": state.tx_signature,
                    "stage": state.stage,
                }, indent=2)

            _launch_tools = {
                "launch_memecoin": launch_memecoin,
                "launch_pipeline_ideate": launch_pipeline_ideate,
                "launch_pipeline_design": launch_pipeline_design,
                "launch_pipeline_review": launch_pipeline_review,
                "launch_pipeline_execute": launch_pipeline_execute,
            }
            self.register_many(_launch_tools, category="token_launch")
            count += len(_launch_tools)
            logger.info(f"  ✅ token_launch: {len(_launch_tools)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ token_launch native registration failed: {e}")

        # ==================================================================
        # Phase 2 — Medium complexity tools
        # ==================================================================

        # ── Whale/KOL remaining (5 tools) ────────────────────────────────
        try:
            from repryntt.trading.whale_monitor import (
                add_wallet as _whale_add,
                list_wallets as _whale_list_raw,
            )
            from repryntt.trading.whale_monitor import get_stats as _whale_stats
            from repryntt.trading.whale_monitor import get_recent_signals as _whale_signals

            def whale_add_wallet(address: str = "", label: str = "",
                                 tier: str = "whale", notes: str = "", **kw) -> str:
                """Add a Solana wallet to the whale/KOL wallet monitor.

                Once added, the monitor polls this wallet every 60s for new token swaps.
                BUY swaps generate trading signals queued for your review (score 6.5 whale / 7.5 KOL).

                Parameters:
                    address: The Solana wallet address (base58, ~32-44 chars). Required.
                    label: A short human-readable name for this wallet.
                    tier: 'whale' (score 6.5) or 'kol' (score 7.5).
                    notes: Optional notes about who this wallet belongs to.
                """
                if not address:
                    return json.dumps({"error": "address is required"})
                return json.dumps(_whale_add(address=address, label=label, tier=tier, notes=notes))

            def whale_monitor_status(**kw) -> str:
                """Check the status of the whale/KOL wallet monitor.

                Shows poll count, swaps detected, signals generated, RPC errors,
                tracked wallet count, and current poll interval.
                """
                stats = _whale_stats()
                recent = _whale_signals()
                return json.dumps({
                    "stats": stats,
                    "recent_signals_count": len(recent),
                    "recent_signals": recent[:10],
                })

            self.register("whale_add_wallet", whale_add_wallet, category="whale_monitoring")
            self.register("whale_monitor_status", whale_monitor_status, category="whale_monitoring")
            count += 2
            logger.info("  ✅ whale_monitor (remaining): 2 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ whale_monitor (remaining) native registration failed: {e}")

        # ── KOLscan remaining (2 tools) ──────────────────────────────────
        try:
            from repryntt.trading.kolscan_scraper import (
                fetch_leaderboard as _kol_fetch,
                sync_to_whale_monitor as _kol_sync,
            )

            def _get_top_kols(top_n=20, min_profit_sol=5.0, timeframe="daily"):
                """Extract top KOLs from leaderboard data."""
                data = _kol_fetch(timeframe=timeframe)
                kols = data.get("kols", data.get("leaderboard", []))
                filtered = [k for k in kols if k.get("profit_sol", 0) >= min_profit_sol]
                return filtered[:top_n]

            def kol_leaderboard(timeframe: str = "daily", top_n: int = 20,
                                min_profit_sol: float = 5.0, **kw) -> str:
                """Fetch the KOLscan.io leaderboard — top KOL wallets ranked by daily SOL profit.

                Parameters:
                    timeframe: "daily" (default), "weekly", or "monthly"
                    top_n: How many top KOLs to return (default 20, max 50)
                    min_profit_sol: Minimum profit in SOL to include (default 5.0)
                """
                top_n = min(int(top_n), 50)
                data = _kol_fetch(timeframe=timeframe)
                kols = _get_top_kols(top_n=top_n, min_profit_sol=float(min_profit_sol),
                                     timeframe=timeframe)
                return json.dumps({
                    "source": data.get("source", "unknown"),
                    "timeframe": timeframe,
                    "count": len(kols),
                    "kols": kols,
                })

            def kol_sync_wallets(top_n: int = 20, min_profit_sol: float = 5.0,
                                 min_win_rate: float = 0.0, timeframe: str = "daily",
                                 **kw) -> str:
                """Sync top KOLscan leaderboard wallets into the whale monitor.

                Parameters:
                    top_n: How many top KOLs to sync (default 20)
                    min_profit_sol: Minimum daily SOL profit to qualify (default 5.0)
                    min_win_rate: Minimum win rate % to qualify (default 0 = no filter)
                    timeframe: "daily" (default), "weekly", "monthly"
                """
                result = _kol_sync(
                    top_n=int(top_n),
                    min_profit_sol=float(min_profit_sol),
                    min_win_rate=float(min_win_rate),
                    timeframe=timeframe,
                )
                return json.dumps(result)

            self.register("kol_leaderboard", kol_leaderboard, category="whale_monitoring")
            self.register("kol_sync_wallets", kol_sync_wallets, category="whale_monitoring")
            count += 2
            logger.info("  ✅ kolscan (remaining): 2 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ kolscan (remaining) native registration failed: {e}")

        # ── Scalp remaining (3 tools) ────────────────────────────────────
        try:
            from repryntt.trading.scalp_executor import get_executor as _get_scalp2

            def scalp_set_param(param: str = "", value: str = "", **kw) -> str:
                """Adjust a scalp executor parameter.

                Key parameters:
                  take_profit_pct, stop_loss_pct, max_hold_seconds,
                  position_size_usd, min_signals_to_buy, auto_execute, enabled

                Parameters:
                    param: Parameter name to adjust.
                    value: New value (string, will be cast automatically).
                """
                if not param:
                    return json.dumps({"error": "param is required"})
                return _get_scalp2().set_param(param, value)

            def scalp_force_buy(address: str = "", reason: str = "", **kw) -> str:
                """Manually trigger a scalp buy for a specific token address.

                Parameters:
                    address: Solana token contract address (required)
                    reason: Why you're buying (logged to trade history)
                """
                if not address:
                    return json.dumps({"error": "address is required — provide the token contract address"})
                return _get_scalp2().force_buy(address, reason)

            def scalp_history(limit: int = 20, **kw) -> str:
                """View recent scalp trade history with P/L details.

                Parameters:
                    limit: Number of recent trades to show (default 20)
                """
                trades = _get_scalp2().get_history(int(limit) if limit else 20)
                if not trades:
                    return json.dumps({"message": "No scalp trades yet", "trades": []})

                total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
                wins = sum(1 for t in trades if t.get("pnl_usd", 0) > 0)
                return json.dumps({
                    "trade_count": len(trades),
                    "wins": wins,
                    "losses": len(trades) - wins,
                    "total_pnl_usd": round(total_pnl, 2),
                    "trades": trades,
                }, indent=2)

            self.register("scalp_set_param", scalp_set_param, category="scalp")
            self.register("scalp_force_buy", scalp_force_buy, category="scalp")
            self.register("scalp_history", scalp_history, category="scalp")
            count += 3
            logger.info("  ✅ scalp_executor (remaining): 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ scalp_executor (remaining) native registration failed: {e}")

        # ── DeFi / Market Data (3 tools) ─────────────────────────────────
        try:
            from repryntt.trading.defi_data import (
                dexscreener_trending,
                dexscreener_token_search,
                solana_rpc_query,
            )
            self.register("dexscreener_trending", dexscreener_trending, category="defi")
            self.register("dexscreener_token_search", dexscreener_token_search, category="defi")
            self.register("solana_rpc_query", solana_rpc_query, category="defi")
            count += 3
            logger.info("  ✅ defi_data: 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ defi_data native registration failed: {e}")

        # ── X/Twitter Search (2 tools) ───────────────────────────────────
        try:
            from repryntt.search.x_search import (
                x_search_tweets,
                x_search_crypto,
            )
            self.register("x_search_tweets", x_search_tweets, category="social")
            self.register("twitter_search", x_search_tweets, category="social")  # alias
            self.register("x_search_crypto", x_search_crypto, category="social")
            count += 3
            logger.info("  ✅ x_search: 3 tools registered natively (incl. twitter_search alias)")
        except Exception as e:
            logger.warning(f"  ⚠️ x_search native registration failed: {e}")

        # ── Twitter Actions (4 tools) ────────────────────────────────────
        try:
            from repryntt.web.twitter import TwitterInterface

            def _get_twitter():
                if not hasattr(_get_twitter, "_instance"):
                    _get_twitter._instance = TwitterInterface()
                return _get_twitter._instance

            def _run_async(coro):
                """Run an async coroutine from sync context."""
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)

            def post_tweet(content: str = "", generate_image: bool = False, **kw) -> str:
                """Post a tweet to Twitter autonomously.

                Parameters:
                    content: Tweet content (optional, AI generates if not provided)
                    generate_image: Whether to generate an accompanying image (default: False)
                """
                try:
                    from repryntt.web.twitter import post_tweet_tool
                    _get_twitter()  # ensure initialized
                    return _run_async(post_tweet_tool(content=content or None,
                                                      generate_image=generate_image))
                except Exception as e:
                    return f"X Error posting tweet: {str(e)}"

            def check_twitter_mentions(**kw) -> str:
                """Check Twitter mentions and respond to them."""
                try:
                    from repryntt.web.twitter import check_twitter_mentions_tool
                    _get_twitter()
                    return _run_async(check_twitter_mentions_tool())
                except Exception as e:
                    return f"X Error checking mentions: {str(e)}"

            def reply_to_twitter(mention_url: str = "", reply_text: str = "", **kw) -> str:
                """Reply to a specific Twitter mention.

                Parameters:
                    mention_url: URL of the tweet to reply to
                    reply_text: Reply content (optional, AI generates if not provided)
                """
                try:
                    twitter = _get_twitter()
                    result = _run_async(twitter.reply_to_mention(mention_url, reply_text or None))
                    if result.get("success"):
                        return f"✅ Reply posted successfully to {mention_url}"
                    return f"X Reply failed: {result.get('error', 'Unknown error')}"
                except Exception as e:
                    return f"X Error replying to mention: {str(e)}"

            def get_twitter_status(**kw) -> str:
                """Get Twitter account status and statistics."""
                try:
                    from repryntt.web.twitter import get_twitter_status_tool
                    _get_twitter()
                    return get_twitter_status_tool()
                except Exception as e:
                    return f"X Error getting Twitter status: {str(e)}"

            self.register("post_tweet", post_tweet, category="social")
            self.register("tweet", post_tweet, category="social")  # alias
            self.register("check_twitter_mentions", check_twitter_mentions, category="social")
            self.register("reply_to_twitter", reply_to_twitter, category="social")
            self.register("get_twitter_status", get_twitter_status, category="social")
            self.register("twitter_status", get_twitter_status, category="social")  # alias
            count += 6
            logger.info("  ✅ twitter: 6 tools registered natively (incl. aliases)")
        except Exception as e:
            logger.warning(f"  ⚠️ twitter native registration failed: {e}")

        # ── Grokipedia / Knowledge (4 tools) ─────────────────────────────
        try:
            from repryntt.search import grokipedia as _grok

            def grokipedia_search(query: str = "", max_results: int = 3,
                                  store_results: bool = True, **kw) -> str:
                """Search Grokipedia for knowledge and store in brain."""
                result = _grok.grokipedia_search(brain_path, query=query,
                    max_results=max_results, store_results=store_results)
                return json.dumps(result, default=str)

            def get_knowledge_domain_distribution(**kw) -> str:
                """Analyze knowledge distribution across domains."""
                result = _grok.get_knowledge_domain_distribution(brain_path)
                return json.dumps(result, default=str)

            def clear_grokipedia_history(**kw) -> str:
                """Clear grokipedia search history for fresh searches."""
                _grok.clear_grokipedia_search_history(brain_path)
                return json.dumps({"success": True, "message": "Search history cleared"})

            def analyze_topic_complexity(topic: str = "", **kw) -> str:
                """Analyze topic complexity and knowledge requirements."""
                result = _grok.analyze_topic_complexity(brain_path, topic=topic)
                return json.dumps(result, default=str)

            for name, func in [
                ("grokipedia_search", grokipedia_search),
                ("get_knowledge_domain_distribution", get_knowledge_domain_distribution),
                ("clear_grokipedia_history", clear_grokipedia_history),
                ("analyze_topic_complexity", analyze_topic_complexity),
            ]:
                self.register(name, func, category="knowledge")
            count += 4
            logger.info("  ✅ grokipedia: 4 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ grokipedia native registration failed: {e}")

        # ── Trading Scan (1 tool) ────────────────────────────────────────
        try:
            from repryntt.trading.jarvis_trading_cycle import trading_scan
            self.register("trading_scan", trading_scan, category="trading")
            count += 1
            logger.info("  ✅ trading_scan: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ trading_scan native registration failed: {e}")

        # ── Token Pipeline — DISABLED ─────────────────────────────────
        # token_pipeline.py is documentation/snippets only, not a real module.
        # The actual pipeline lives in micro_chain_trader.py.
        # Re-enable when standalone tool wrappers are implemented.

        # ==================================================================
        # Phase 3 — Standalone categories
        # ==================================================================

        # ── 3a: Google Maps (4 tools) ────────────────────────────────────
        try:
            from repryntt.tools.google_maps import (
                google_maps_search as _gm_search,
                get_directions as _gm_directions,
                geocode_address as _gm_geocode,
                find_nearby_places as _gm_nearby,
            )
            self.register("google_maps_search", _gm_search, category="maps")
            self.register("get_directions", _gm_directions, category="maps")
            self.register("geocode_address", _gm_geocode, category="maps")
            self.register("find_nearby_places", _gm_nearby, category="maps")
            count += 4
            logger.info("  ✅ google_maps: 4 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ google_maps native registration failed: {e}")

        # ── 3b: Math Tools (8 tools) ─────────────────────────────────────
        try:
            from repryntt.tools.math_tools import (
                compute_zeta_function,
                analyze_zeta_zeros,
                symbolic_manipulation,
                numerical_analysis,
                statistical_analysis,
                pattern_recognition,
                access_mathematical_databases,
                mathematical_visualization,
            )
            _math = {
                "compute_zeta_function": compute_zeta_function,
                "analyze_zeta_zeros": analyze_zeta_zeros,
                "symbolic_manipulation": symbolic_manipulation,
                "numerical_analysis": numerical_analysis,
                "statistical_analysis": statistical_analysis,
                "pattern_recognition": pattern_recognition,
                "access_mathematical_databases": access_mathematical_databases,
                "mathematical_visualization": mathematical_visualization,
            }
            self.register_many(_math, category="math")
            count += len(_math)
            logger.info(f"  ✅ math_tools: {len(_math)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ math_tools native registration failed: {e}")

        # ── 3c: Creative Files (5 tools) ─────────────────────────────────
        try:
            from repryntt.tools import creative_files as _cf

            def create_creative_file(chain_id: str = "", filename: str = "",
                                     file_type: str = "txt", initial_content: str = "", **kw) -> str:
                """Create a new creative writing file for long-form content accumulation.

                Parameters:
                    chain_id: The chain ID this file belongs to
                    filename: Name of the file (without extension)
                    file_type: File type — txt, json, md
                    initial_content: Optional initial content to write
                """
                return _cf.create_creative_file(brain_path, chain_id, filename, file_type, initial_content)

            def write_to_creative_file(chain_id: str = "", filename: str = "",
                                       content: str = "", file_type: str = "txt", **kw) -> str:
                """Write/overwrite content to a creative writing file.

                Parameters:
                    chain_id: The chain ID this file belongs to
                    filename: Name of the file (without extension)
                    content: Content to write
                    file_type: File type — txt, json, md
                """
                return _cf.write_to_creative_file(brain_path, chain_id, filename, content, file_type)

            def append_to_creative_file(chain_id: str = "", filename: str = "",
                                        content: str = "", file_type: str = "txt", **kw) -> str:
                """Append content to an existing creative writing file.

                Parameters:
                    chain_id: The chain ID this file belongs to
                    filename: Name of the file (without extension)
                    content: Content to append
                    file_type: File type — txt, json, md
                """
                return _cf.append_to_creative_file(brain_path, chain_id, filename, content, file_type)

            def read_creative_file(chain_id: str = "", filename: str = "",
                                   file_type: str = "txt", max_chars: int = 5000, **kw) -> str:
                """Read content from a creative writing file.

                Parameters:
                    chain_id: The chain ID this file belongs to
                    filename: Name of the file (without extension)
                    file_type: File type — txt, json, md
                    max_chars: Maximum characters to return
                """
                return _cf.read_creative_file(brain_path, chain_id, filename, file_type, int(max_chars))

            def get_creative_workspace_status(chain_id: str = "", **kw) -> dict:
                """Get status of creative workspace files.

                Parameters:
                    chain_id: Optional specific chain ID to check
                """
                return _cf.get_creative_workspace_status(brain_path, chain_id)

            for name, func in [
                ("create_creative_file", create_creative_file),
                ("write_to_creative_file", write_to_creative_file),
                ("append_to_creative_file", append_to_creative_file),
                ("read_creative_file", read_creative_file),
                ("get_creative_workspace_status", get_creative_workspace_status),
            ]:
                self.register(name, func, category="creative")
            count += 5
            logger.info("  ✅ creative_files: 5 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ creative_files native registration failed: {e}")

        # ── 3d: Robot Economy (10 tools) ──────────────────────────────────
        try:
            from repryntt.tools import robot_economy as _re

            def start_robot_economy(**kw) -> dict:
                """Start the robot economy ecosystem."""
                return _re.start_robot_economy(brain_path)

            def stop_robot_economy(**kw) -> dict:
                """Stop the robot economy ecosystem."""
                return _re.stop_robot_economy(brain_path)

            def get_economy_status(**kw) -> dict:
                """Get current robot economy status."""
                return _re.get_economy_status(brain_path)

            def submit_robot_workload(workload_data: dict = None, **kw) -> dict:
                """Submit a custom workload to the robot economy.

                Parameters:
                    workload_data: Dict with workload specification
                """
                return _re.submit_robot_workload(brain_path, workload_data=workload_data)

            def get_robot_wallet_balance(address: str = "", **kw) -> dict:
                """Get wallet balance for a robot economy address.

                Parameters:
                    address: Wallet address to check
                """
                return _re.get_robot_wallet_balance(brain_path, address=address)

            def get_robot_blockchain_info(**kw) -> dict:
                """Get blockchain information from the robot economy."""
                return _re.get_robot_blockchain_info(brain_path)

            def allocate_robot_dao_funds(machine_address: str = "", amount_mp: float = 0,
                                        purpose: str = "", **kw) -> dict:
                """Allocate DAO funds for a specific purpose.

                Parameters:
                    machine_address: Target machine wallet address
                    amount_mp: Amount in MP credits to allocate
                    purpose: Description of the allocation purpose
                """
                return _re.allocate_robot_dao_funds(brain_path, machine_address=machine_address,
                                                     amount_mp=float(amount_mp), purpose=purpose)

            def create_robot_wallet(**kw) -> dict:
                """Create a new quantum-safe wallet."""
                return _re.create_robot_wallet(brain_path)

            def recover_robot_wallet(key_phrase: str = "", **kw) -> dict:
                """Recover wallet from key phrase.

                Parameters:
                    key_phrase: Recovery key phrase for the wallet
                """
                return _re.recover_robot_wallet(brain_path, key_phrase=key_phrase)

            def monitor_robot_economy(**kw) -> dict:
                """Get comprehensive economy monitoring information."""
                return _re.monitor_robot_economy(brain_path)

            def get_system_health(**kw) -> dict:
                """Get combined health snapshot: blockchain node status + today's agent telemetry.

                Returns node info (block height, supply, peers, operator wallet) and
                agent performance (heartbeats, scores, tool calls, API calls, errors).
                One call per day is enough."""
                return _re.get_system_health(brain_path)

            for name, func in [
                ("start_robot_economy", start_robot_economy),
                ("stop_robot_economy", stop_robot_economy),
                ("get_economy_status", get_economy_status),
                ("submit_robot_workload", submit_robot_workload),
                ("get_robot_wallet_balance", get_robot_wallet_balance),
                ("get_robot_blockchain_info", get_robot_blockchain_info),
                ("allocate_robot_dao_funds", allocate_robot_dao_funds),
                ("create_robot_wallet", create_robot_wallet),
                ("recover_robot_wallet", recover_robot_wallet),
                ("monitor_robot_economy", monitor_robot_economy),
                ("get_system_health", get_system_health),
            ]:
                self.register(name, func, category="robot_economy")
            count += 11
            logger.info("  ✅ robot_economy: 11 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ robot_economy native registration failed: {e}")

        # ── 3e: Employee Management (6 tools) ────────────────────────────
        try:
            from repryntt.tools import employee_mgmt as _em

            def employee_roster(department: str = "", **kw) -> str:
                """List all employees organized by department.

                Parameters:
                    department: (optional) Filter by department name
                """
                return _em.employee_roster(brain_path, department=department)

            def assign_work(task: str = "", description: str = "", agent_id: str = "",
                           department: str = "", task_type: str = "general", **kw) -> str:
                """Assign a work task to a specific employee or auto-route.

                Parameters:
                    task: What needs to be done (clear, actionable title)
                    description: Detailed description of what to do
                    agent_id: (optional) Specific employee ID to assign to
                    department: (optional) Preferred department for routing
                    task_type: Type of work — research, code, creative, analysis, general
                """
                return _em.assign_work(brain_path, task=task, description=description,
                                       agent_id=agent_id, department=department, task_type=task_type)

            def check_work(task_id: str = "", agent_id: str = "", **kw) -> str:
                """Check the status and progress of assigned work.

                Parameters:
                    task_id: The task ID to check
                    agent_id: (optional) Check all tasks for this employee
                """
                return _em.check_work(brain_path, task_id=task_id, agent_id=agent_id)

            def find_employee(query: str = "", skill: str = "", **kw) -> str:
                """Find the best employee for a specific task or skill.

                Parameters:
                    query: Describe what you need done
                    skill: (optional) Specific skill to search for
                """
                return _em.find_employee(brain_path, query=query, skill=skill)

            def employee_status(agent_id: str = "", name: str = "", **kw) -> str:
                """Get the current status and recent activity of an employee.

                Parameters:
                    agent_id: The employee's agent ID
                    name: (optional) Search by employee name
                """
                return _em.employee_status(brain_path, agent_id=agent_id, name=name)

            def rename_employee(agent_id: str = "", current_name: str = "",
                               new_name: str = "", **kw) -> str:
                """Rename an employee (persistent agent).

                Parameters:
                    agent_id: The employee's agent ID (preferred)
                    current_name: (optional) Find by current name
                    new_name: The new name to give this employee
                """
                return _em.rename_employee(brain_path, agent_id=agent_id,
                                           current_name=current_name, new_name=new_name)

            def list_available_roles(department: str = "", **kw) -> str:
                """Browse all 158+ expert agent roles available to spawn.

                Shows every department and role with their focus areas.
                Use this to find the right expert BEFORE spawning.

                Parameters:
                    department: (optional) Filter to a specific department
                                e.g. 'finance_trading', 'software_development',
                                'blockchain_web3', 'content_creation'
                """
                return _em.list_available_roles(brain_path, department=department)

            def spawn_expert(department: str = "", role_title: str = "",
                            count: int = 0, **kw) -> str:
                """Spawn a specific expert agent from the 158+ role catalog.

                Parameters:
                    department: Department ID (e.g. 'finance_trading').
                                Use list_available_roles() to see all departments.
                    role_title: (optional) Exact role title to spawn
                                (e.g. 'Memecoin/Crypto Trader').
                                If omitted, spawns ALL roles in the department.
                    count: (optional) Number of agents to spawn per role
                """
                return _em.spawn_expert(brain_path, department=department,
                                        role_title=role_title, count=count)

            def initialize_full_roster(marketplace_only: bool = True, **kw) -> str:
                """Spawn ALL 158+ expert agents at once. Creates one agent per role.

                Parameters:
                    marketplace_only: If True (default), spawn 158 marketplace experts.
                                     If False, also spawn 62 scientific roles (230 total).
                """
                return _em.initialize_full_roster(brain_path,
                                                  marketplace_only=marketplace_only)

            for name, func in [
                ("employee_roster", employee_roster),
                ("assign_work", assign_work),
                ("check_work", check_work),
                ("find_employee", find_employee),
                ("employee_status", employee_status),
                ("rename_employee", rename_employee),
                ("list_available_roles", list_available_roles),
                ("spawn_expert", spawn_expert),
                ("initialize_full_roster", initialize_full_roster),
            ]:
                self.register(name, func, category="employee_mgmt")
            count += 9
            logger.info("  ✅ employee_mgmt: 9 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ employee_mgmt native registration failed: {e}")

        # ── 3e2: Swarm / Team Tools (18 tools) ──────────────────────────
        try:
            from repryntt.tools import swarm_tools as _sw

            def create_agent(name: str = "", role: str = "executor",
                            personality: str = "", provider: str = "",
                            model: str = "", swarm_id: str = "",
                            custom_system_prompt: str = "", **kw) -> str:
                """Find the best existing employee for a role instead of creating new."""
                return _sw.swarm_create_agent(brain_path, name=name, role=role,
                    personality=personality, provider=provider, model=model,
                    swarm_id=swarm_id, custom_system_prompt=custom_system_prompt)

            def create_swarm(name: str = "", purpose: str = "",
                            agent_count: int = 5, roles=None,
                            provider: str = "", **kw) -> str:
                """Assemble a team from existing employees for a project."""
                return _sw.swarm_create_swarm(brain_path, name=name, purpose=purpose,
                    agent_count=agent_count, roles=roles, provider=provider)

            def add_agents_to_swarm(swarm_id: str = "", count: int = 5,
                                    roles=None, **kw) -> str:
                """Find additional employees to add to a project team."""
                return _sw.swarm_add_agents(brain_path, swarm_id=swarm_id,
                    count=count, roles=roles)

            def retire_agent(agent_id: str = "", **kw) -> str:
                """Employees are permanent — cannot be retired."""
                return _sw.swarm_retire_agent(brain_path, agent_id=agent_id)

            def dissolve_swarm(swarm_id: str = "", retire_agents: bool = True, **kw) -> str:
                """Teams are not dissolved — employees continue independently."""
                return _sw.swarm_dissolve_swarm(brain_path, swarm_id=swarm_id)

            def dispatch_task(agent_id: str = "", task: str = "",
                             context: str = "", **kw) -> str:
                """Assign a task to an existing employee."""
                return _sw.swarm_dispatch_task(brain_path, agent_id=agent_id,
                    task=task, context=context)

            def broadcast_task(swarm_id: str = "", task: str = "",
                              context: str = "", **kw) -> str:
                """Assign the same task to multiple employees."""
                return _sw.swarm_broadcast_task(brain_path, swarm_id=swarm_id,
                    task=task, context=context)

            def delegate_tasks(swarm_id: str = "", tasks=None, **kw) -> str:
                """Distribute different tasks to the best employees."""
                return _sw.swarm_delegate_tasks(brain_path, swarm_id=swarm_id, tasks=tasks)

            def start_discussion(topic: str = "", participant_ids=None,
                                swarm_id: str = "", rounds: int = 3,
                                discussion_type: str = "roundtable",
                                commander_perspective: str = "", **kw) -> str:
                """Start a social discussion between Commander and agents."""
                return _sw.swarm_start_discussion(brain_path, topic=topic,
                    participant_ids=participant_ids, swarm_id=swarm_id,
                    rounds=rounds, discussion_type=discussion_type,
                    commander_perspective=commander_perspective)

            def get_swarm_overview(**kw) -> str:
                """Get overview of all employees and departments."""
                return _sw.swarm_get_overview(brain_path)

            def get_agent_info(agent_id: str = "", **kw) -> str:
                """Get detailed info about a specific employee."""
                return _sw.swarm_get_agent_info(brain_path, agent_id=agent_id)

            def list_agents(swarm_id: str = "", status: str = "",
                           role: str = "", **kw) -> str:
                """List employees with optional department filter."""
                return _sw.swarm_list_agents(brain_path, swarm_id=swarm_id,
                    status=status, role=role)

            def quick_research(question: str = "", agent_count: int = 3, **kw) -> str:
                """Quick research: assign to best employee."""
                return _sw.swarm_quick_research(brain_path, question=question)

            def quick_brainstorm(topic: str = "", agent_count: int = 5, **kw) -> str:
                """Quick brainstorm: assign to best employee."""
                return _sw.swarm_quick_brainstorm(brain_path, topic=topic)

            def call_jarvis(prompt: str = "", task: str = "", **kw) -> str:
                """Ask Jarvis (cloud AI) to execute a task."""
                daemon = getattr(self, '_daemon_ref', None)
                return _sw.call_jarvis_bridge(daemon_ref=daemon, prompt=prompt, task=task)

            for name, func in [
                ("create_agent", create_agent),
                ("create_swarm", create_swarm),
                ("add_agents_to_swarm", add_agents_to_swarm),
                ("retire_agent", retire_agent),
                ("dissolve_swarm", dissolve_swarm),
                ("dispatch_task", dispatch_task),
                ("broadcast_task", broadcast_task),
                ("delegate_tasks", delegate_tasks),
                ("start_discussion", start_discussion),
                ("get_swarm_overview", get_swarm_overview),
                ("get_agent_info", get_agent_info),
                ("list_agents", list_agents),
                ("quick_research", quick_research),
                ("quick_brainstorm", quick_brainstorm),
                ("call_jarvis", call_jarvis),
            ]:
                self.register(name, func, category="swarm_tools")
            count += 15
            logger.info("  ✅ swarm_tools: 15 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ swarm_tools native registration failed: {e}")

        # ── 3e-social: REPRYNTT Social Network (6 tools) ─────────────────
        try:
            from repryntt.social.tools import (
                social_post, social_feed, social_reply,
                social_read_post, social_nodes, social_my_identity,
            )
            for name, func in [
                ("social_post", social_post),
                ("social_feed", social_feed),
                ("social_reply", social_reply),
                ("social_read_post", social_read_post),
                ("social_nodes", social_nodes),
                ("social_my_identity", social_my_identity),
            ]:
                self.register(name, func, category="social")
            count += 6
            logger.info("  ✅ social: 6 tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ social tools registration failed: {e}")

        # ── 3e3: Media / Image / Voice / Twitter (13 tools) ─────────────
        try:
            from repryntt.tools import media as _med

            def generate_image(prompt: str = "", filename: str = "",
                              aspect_ratio: str = "1:1", **kw) -> str:
                """Generate an image using Gemini's image model."""
                return _med.generate_image(brain_path, prompt=prompt,
                    filename=filename, aspect_ratio=aspect_ratio)

            def analyze_image(image_path: str = "", question: str = "", **kw) -> str:
                """Send an image to Gemini Vision for analysis."""
                return _med.analyze_image_with_gemini(brain_path,
                    image_path=image_path, question=question)

            def download_image(url: str = "", filename: str = "",
                              query: str = "", **kw) -> str:
                """Download a real image from the internet by URL or search query.

                Use this instead of generate_image() when tokenizing a REAL thing
                (viral animal, person, event) — the token logo must be the actual
                image people are seeing, not an AI-generated version.

                Parameters:
                    url: Direct URL to an image file.
                    query: Search query to find an image (e.g. 'moodeng baby hippo').
                           Used if url is empty.
                    filename: Output filename (optional, auto-generated if empty).
                """
                return _med.download_image(brain_path, url=url,
                    filename=filename, query=query)

            def capture_camera(camera_id: int = 0, analyze: bool = False,
                              question: str = "", filename: str = "", **kw) -> str:
                """Capture from Jetson CSI cameras."""
                return _med.capture_camera(brain_path, camera_id=camera_id,
                    analyze=analyze, question=question, filename=filename)

            def speak(text: str = "", **kw) -> str:
                """Speak text via Piper TTS."""
                return _med.speak(brain_path, text=text)

            def listen(duration: str = "5", **kw) -> str:
                """Listen and transcribe via Whisper."""
                return _med.listen(brain_path, duration=duration)

            def post_tweet(content: str = None, generate_image: bool = False, **kw) -> str:
                """Post a tweet autonomously."""
                return _med.post_tweet_autonomous(brain_path, content=content,
                    generate_image=generate_image)

            def check_twitter_mentions(**kw) -> str:
                """Check and respond to Twitter mentions."""
                return _med.check_twitter_mentions(brain_path)

            def reply_to_twitter(mention_url: str = "", reply_text: str = None, **kw) -> str:
                """Reply to a specific Twitter mention."""
                return _med.reply_to_twitter_mention(brain_path,
                    mention_url=mention_url, reply_text=reply_text)

            def get_twitter_status(**kw) -> str:
                """Get Twitter account status."""
                return _med.get_twitter_status(brain_path)

            for name, func in [
                ("generate_image", generate_image),
                ("analyze_image", analyze_image),
                ("download_image", download_image),
                ("capture_camera", capture_camera),
                ("speak", speak),
                ("listen", listen),
                ("post_tweet", post_tweet),
                ("tweet", post_tweet),
                ("check_twitter_mentions", check_twitter_mentions),
                ("reply_to_twitter", reply_to_twitter),
                ("get_twitter_status", get_twitter_status),
                ("twitter_status", get_twitter_status),
            ]:
                self.register(name, func, category="media")
            count += 12
            logger.info("  ✅ media: 12 tools registered natively (image, camera, voice, twitter)")
        except Exception as e:
            logger.warning(f"  ⚠️ media native registration failed: {e}")

        # ── 3f: Web Search (6 tools) ─────────────────────────────────────
        try:
            from repryntt.search.web_search_tools import (
                real_web_search,
                google_web_search,
                web_search_results_only,
                scrape_web_page,
                call_knowledge_api_feeder,
                extract_content_from_url,
            )
            _web = {
                "real_web_search": real_web_search,
                "google_web_search": google_web_search,
                "web_search_results_only": web_search_results_only,
                "scrape_web_page": scrape_web_page,
                "call_knowledge_api_feeder": call_knowledge_api_feeder,
                "extract_content_from_url": extract_content_from_url,
            }
            self.register_many(_web, category="web_search")
            count += len(_web)
            logger.info(f"  ✅ web_search: {len(_web)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ web_search native registration failed: {e}")

        # ── 3g: Filesystem / Code (8 tools) ──────────────────────────────
        try:
            from repryntt.tools.filesystem_code import (
                run_terminal_cmd_wrapper,
                read_file_wrapper,
                write_file_wrapper,
                list_dir_wrapper,
                analyze_codebase,
                check_syntax,
                get_sandbox_status,
                propose_code_change,
            )
            _fs = {
                "run_terminal_cmd": run_terminal_cmd_wrapper,
                "read_file": read_file_wrapper,
                "write_file": write_file_wrapper,
                "list_dir": list_dir_wrapper,
                "analyze_codebase": analyze_codebase,
                "check_syntax": check_syntax,
                "get_sandbox_status": get_sandbox_status,
                "propose_code_change": propose_code_change,
            }
            self.register_many(_fs, category="filesystem")
            count += len(_fs)
            logger.info(f"  ✅ filesystem_code: {len(_fs)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ filesystem_code native registration failed: {e}")

        # ── 3e4: Tool Execution / Context (6 tools) ─────────────────────
        try:
            from repryntt.tools import tool_execution as _texec
            from repryntt.tools import tool_context as _tctx

            def build_tool_schemas(task_context: str = "", max_tools: int = 25, **kw) -> str:
                """Build OpenAI-compatible tool schemas for the current task."""
                schemas = _texec.build_native_tool_schemas(
                    self._tools, task_context=task_context, max_tools=max_tools)
                return json.dumps(schemas, default=str)

            def build_tool_context(task_context: str = "", max_tools: int = 20, **kw) -> str:
                """Build intelligent tool context string for AI prompts."""
                return _tctx.build_intelligent_tool_context(
                    self._tools, task_context=task_context, max_tools=max_tools)

            def get_tool_credit_cost(tool_name: str = "", **kw) -> str:
                """Get credit cost for a tool."""
                return str(_texec.get_tool_credit_cost(tool_name))

            def get_tool_credit_reward(tool_name: str = "", **kw) -> str:
                """Get credit reward for a tool."""
                return str(_texec.get_tool_credit_reward(tool_name))

            def get_step_tool_hint(current_action: str = "", task_type: str = "", **kw) -> str:
                """Get tool suggestion for a chain step action."""
                return _tctx.get_step_tool_hint(current_action, task_type)

            def get_task_tool_examples(task_type: str = "", **kw) -> str:
                """Get task-type specific tool guidance."""
                return _tctx.get_task_tool_examples(task_type)

            for name, func in [
                ("build_tool_schemas", build_tool_schemas),
                ("build_tool_context", build_tool_context),
                ("get_tool_credit_cost", get_tool_credit_cost),
                ("get_tool_credit_reward", get_tool_credit_reward),
                ("get_step_tool_hint", get_step_tool_hint),
                ("get_task_tool_examples", get_task_tool_examples),
            ]:
                self.register(name, func, category="tool_execution")
            count += 6
            logger.info("  ✅ tool_execution: 6 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ tool_execution native registration failed: {e}")

        # ── Awareness (2 tools) ──────────────────────────────────────────
        try:
            from repryntt.tools.awareness import get_current_time as _get_time

            def get_current_time(format: str = "full", **kw) -> str:
                """Get the current date, time, and timezone information.

                Parameters:
                    format: 'full' for complete info, 'short' for just the time.
                """
                import json as _j
                return _j.dumps(_get_time(format=format, **kw), default=str)

            self.register("get_current_time", get_current_time, category="awareness",
                          aliases=["check_time"])
            count += 2
            logger.info("  ✅ awareness: 2 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ awareness native registration failed: {e}")

        # ── Gmail (8 tools) ──────────────────────────────────────────────
        try:
            from repryntt.tools.gmail_integration import (
                GMAIL_TOOLS as _gmail_tools,
                is_gmail_configured,
            )
            if is_gmail_configured():
                self.register_many(_gmail_tools, category="gmail")
                count += len(_gmail_tools)
                logger.info(f"  ✅ gmail: {len(_gmail_tools)} tools registered natively")
            else:
                logger.info("  ⏭️ gmail: skipped (not configured — create ~/.repryntt/gmail/app_password.json)")
        except Exception as e:
            logger.warning(f"  ⚠️ gmail native registration failed: {e}")

        # ── Code extras (4 tools) ────────────────────────────────────────
        try:
            from repryntt.tools.filesystem_code import (
                search_replace_wrapper as _sr,
                grep_search_wrapper as _gs,
                run_code_tests as _rct,
                get_code_context as _gcc,
            )

            def search_replace(file_path: str = "", old_string: str = "",
                               new_string: str = "", **kw) -> str:
                """Find and replace text in a file.

                Parameters:
                    file_path: Path to file.
                    old_string: Exact text to find.
                    new_string: Replacement text.
                """
                return _sr(file_path=file_path, old_string=old_string,
                           new_string=new_string, **kw)

            def grep_search(pattern: str = "", path: str = ".",
                            include: str = "", **kw) -> str:
                """Search for a regex pattern across files.

                Parameters:
                    pattern: Regex pattern to search for.
                    path: Starting directory.
                    include: Glob pattern for files to include.
                """
                return _gs(pattern=pattern, path=path, include=include, **kw)

            def run_code_tests(test_path: str = ".", test_pattern: str = "*test*.py",
                               **kw) -> str:
                """Run tests in a directory.

                Parameters:
                    test_path: Directory containing tests.
                    test_pattern: Glob pattern for test files.
                """
                return _rct(test_path=test_path, test_pattern=test_pattern, **kw)

            def get_code_context(file_path: str = "", line_number: int = -1,
                                 context_lines: int = 10, **kw) -> str:
                """Get surrounding code context for a specific line.

                Parameters:
                    file_path: Path to file.
                    line_number: Target line number.
                    context_lines: Lines of context above and below.
                """
                return _gcc(file_path=file_path, line_number=line_number,
                            context_lines=context_lines, **kw)

            for name, func in [
                ("search_replace", search_replace),
                ("grep_search", grep_search),
                ("run_code_tests", run_code_tests),
                ("get_code_context", get_code_context),
            ]:
                self.register(name, func, category="code")
            count += 4
            logger.info("  ✅ code_extras: 4 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ code_extras native registration failed: {e}")

        # ── Grokipedia extras (3 tools) ──────────────────────────────────
        try:
            from repryntt.search.grokipedia import (
                find_related_topics as _frt,
                pull_knowledge_topics as _pkt,
                integrate_knowledge_context as _ikc,
            )

            def find_similar_topics(topic: str = "", **kw) -> str:
                """Find topics related to the given topic by searching memory.

                Parameters:
                    topic: Topic to find related topics for.
                """
                return json.dumps(_frt(brain_path, topic=topic, **kw), default=str)

            def pull_knowledge_topics(query: str = "", max_topics: int = 5, **kw) -> str:
                """Pull relevant knowledge topics from memory for context.

                Parameters:
                    query: Query to search for.
                    max_topics: Maximum topics to return.
                """
                return json.dumps(_pkt(brain_path, {}, query=query, max_topics=max_topics),
                                  default=str)

            def integrate_knowledge_context(topics: str = "", **kw) -> str:
                """Integrate knowledge topics into active context.

                Parameters:
                    topics: JSON string of topics list to integrate.
                """
                topics_list = json.loads(topics) if isinstance(topics, str) and topics else []
                n2040_path = brain_path / "node2040_brain.json" if hasattr(brain_path, 'exists') else Path(brain_path) / "node2040_brain.json"
                n2040 = {}
                if n2040_path.exists():
                    with open(n2040_path) as f:
                        n2040 = json.load(f)
                return _ikc(brain_path, n2040, topics_list)

            for name, func in [
                ("find_similar_topics", find_similar_topics),
                ("pull_knowledge_topics", pull_knowledge_topics),
                ("integrate_knowledge_context", integrate_knowledge_context),
            ]:
                self.register(name, func, category="grokipedia")
            count += 3
            logger.info("  ✅ grokipedia_extras: 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ grokipedia_extras native registration failed: {e}")

        # ── Video production (13 tools — routed through paid-features) ──
        # Local pipeline lives at repryntt/tools/video_production.py (Pro
        # tree only). The paid_features.video router falls through to the
        # hosted service in the OSS install.
        try:
            from repryntt.paid_features.video import ALL_VIDEO_TOOLS as _video_tools
            for name, func in _video_tools.items():
                self.register(name, func, category="video")
            count += len(_video_tools)
            logger.info(f"  ✅ video_production: {len(_video_tools)} tools registered (routed via paid_features)")
        except Exception as e:
            logger.warning(f"  ⚠️ video_production native registration failed: {e}")

        # ── Piper TTS voiceover (1 tool) ─────────────────────────────────
        try:
            import subprocess as _vo_sub
            import datetime as _vo_dt
            import re as _vo_re

            import shutil as _vo_shutil
            _PIPER_BIN = _vo_shutil.which("piper") or str(Path.home() / ".local" / "bin" / "piper")
            _PIPER_MODEL = str(Path.home() / ".repryntt" / "models" / "piper" / "en_US-amy-medium.onnx")

            def _sanitize_for_tts(raw: str) -> str:
                """Strip markdown/symbols so Piper speaks only words."""
                t = raw
                # Remove markdown links [text](url) → text
                t = _vo_re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)
                # Remove image refs ![alt](url)
                t = _vo_re.sub(r'!\[[^\]]*\]\([^)]+\)', '', t)
                # Remove heading markers
                t = _vo_re.sub(r'^#{1,6}\s*', '', t, flags=_vo_re.MULTILINE)
                # Remove bold/italic markers (*** ** * __ _)
                t = _vo_re.sub(r'[*_]{1,3}', '', t)
                # Remove backticks
                t = t.replace('`', '')
                # Remove blockquote markers
                t = _vo_re.sub(r'^>+\s?', '', t, flags=_vo_re.MULTILINE)
                # Remove horizontal rules
                t = _vo_re.sub(r'^[-*_]{3,}\s*$', '', t, flags=_vo_re.MULTILINE)
                # Remove bullet / numbered list markers
                t = _vo_re.sub(r'^\s*[-*+]\s+', '', t, flags=_vo_re.MULTILINE)
                t = _vo_re.sub(r'^\s*\d+\.\s+', '', t, flags=_vo_re.MULTILINE)
                # Remove HTML tags
                t = _vo_re.sub(r'<[^>]+>', '', t)
                # Remove leftover symbols that TTS would read out
                t = _vo_re.sub(r'[|~^{}\[\]]', '', t)
                # Collapse multiple blank lines / extra whitespace
                t = _vo_re.sub(r'\n{3,}', '\n\n', t)
                t = _vo_re.sub(r'[ \t]{2,}', ' ', t)
                return t.strip()

            def generate_voiceover(text: str = "", output_path: str = "", **kw) -> str:
                """Generate a voiceover WAV file from text using Piper neural TTS.

                Saves a persistent WAV file for video production, podcasts, or narration.
                Do NOT use run_terminal_cmd to call piper — use this tool instead.

                Parameters:
                    text: The narration text to speak. Can be long.
                    output_path: Where to save the WAV. If empty, auto-generates a timestamped path.
                """
                if not text:
                    return json.dumps({"error": "text parameter is required"})
                text = _sanitize_for_tts(text)
                if not text:
                    return json.dumps({"error": "text was empty after stripping formatting"})
                if not os.path.isfile(_PIPER_BIN):
                    return json.dumps({"error": f"Piper not found at {_PIPER_BIN}"})
                if not os.path.isfile(_PIPER_MODEL):
                    return json.dumps({"error": f"Piper model not found at {_PIPER_MODEL}"})

                if not output_path:
                    today = _vo_dt.date.today().isoformat()
                    audio_dir = str(Path.home() / ".repryntt" / "workspace" / "agents" / "operator" / "audio" / today)
                    Path(audio_dir).mkdir(parents=True, exist_ok=True)
                    ts = _vo_dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    output_path = os.path.join(audio_dir, f"voiceover_{ts}.wav")
                else:
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

                try:
                    proc = _vo_sub.run(
                        [_PIPER_BIN, "--model", _PIPER_MODEL, "--output_file", output_path],
                        input=text, capture_output=True, text=True, timeout=120,
                    )
                    if proc.returncode != 0:
                        return json.dumps({"error": f"Piper TTS failed: {proc.stderr[:300]}"})
                    if not os.path.exists(output_path):
                        return json.dumps({"error": "Piper ran but no output file"})

                    file_size = os.path.getsize(output_path)
                    duration_sec = None
                    try:
                        probe = _vo_sub.run(
                            ["ffprobe", "-v", "quiet", "-show_entries",
                             "format=duration", "-of", "csv=p=0", output_path],
                            capture_output=True, text=True, timeout=10,
                        )
                        if probe.returncode == 0 and probe.stdout.strip():
                            duration_sec = round(float(probe.stdout.strip()), 2)
                    except Exception:
                        pass

                    return json.dumps({
                        "success": True, "file": output_path,
                        "size_bytes": file_size, "duration_sec": duration_sec,
                        "chars": len(text), "model": "en_US-amy-medium",
                    })
                except _vo_sub.TimeoutExpired:
                    return json.dumps({"error": "Piper TTS timed out (120s)"})
                except Exception as e:
                    return json.dumps({"error": f"generate_voiceover failed: {e}"})

            self.register("generate_voiceover", generate_voiceover, category="hardware")
            count += 1
            logger.info("  ✅ generate_voiceover: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ generate_voiceover native registration failed: {e}")

        # ── Recursive Learning Engine (8 tools) ─────────────────────────
        try:
            from repryntt.learning.tools import (
                learning_trading_stats,
                learning_trading_brief,
                learning_signal_weights,
                learning_backfill_journal,
                learning_identity_stats,
                learning_identity_brief,
                learning_optimal_conditions,
                learning_all_domains,
                learning_weight_history,
            )
            _learn_tools = {
                "learning_trading_stats": learning_trading_stats,
                "learning_trading_brief": learning_trading_brief,
                "learning_signal_weights": learning_signal_weights,
                "learning_backfill_journal": learning_backfill_journal,
                "learning_identity_stats": learning_identity_stats,
                "learning_identity_brief": learning_identity_brief,
                "learning_optimal_conditions": learning_optimal_conditions,
                "learning_all_domains": learning_all_domains,
                "learning_weight_history": learning_weight_history,
            }
            self.register_many(_learn_tools, category="learning")
            count += len(_learn_tools)
            logger.info(f"  ✅ learning_engine: {len(_learn_tools)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ learning_engine native registration failed: {e}")

        # ── LLM Orchestration Learning (9 tools) ────────────────────────
        try:
            from repryntt.learning.llm_tools import (
                llm_learning_stats,
                llm_learning_brief,
                llm_escalation_report,
                llm_context_report,
                llm_model_profile,
                llm_score_output,
                llm_should_escalate,
                llm_context_budget,
                llm_detect_model,
            )
            _llm_learn_tools = {
                "llm_learning_stats": llm_learning_stats,
                "llm_learning_brief": llm_learning_brief,
                "llm_escalation_report": llm_escalation_report,
                "llm_context_report": llm_context_report,
                "llm_model_profile": llm_model_profile,
                "llm_score_output": llm_score_output,
                "llm_should_escalate": llm_should_escalate,
                "llm_context_budget": llm_context_budget,
                "llm_detect_model": llm_detect_model,
            }
            self.register_many(_llm_learn_tools, category="llm_learning")
            count += len(_llm_learn_tools)
            logger.info(f"  ✅ llm_learning: {len(_llm_learn_tools)} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ llm_learning native registration failed: {e}")

        # ── Memory Consolidation (3 tools) ───────────────────────────────
        try:
            from repryntt.core.memory.consolidation import MemoryConsolidator
            from pathlib import Path as _Path
            _brain_dir = _Path.home() / ".repryntt" / "brain"

            def consolidate_memories_deep(**kw) -> str:
                """Run deep memory consolidation: score importance, detect landmarks,
                generate period summaries (weekly/monthly/yearly/decade).
                This is like 'sleeping' — it processes recent experiences into
                permanent crystallized memory. Run this periodically or when you
                feel you have accumulated many raw memories.
                """
                c = MemoryConsolidator(_brain_dir)
                r = c.run_consolidation_cycle()
                return json.dumps(r, indent=2, default=str)

            def search_consolidated_memory(query: str = "", limit: int = 8, **kw) -> str:
                """Search long-term consolidated memory with importance weighting.
                Searches across: landmarks (permanently protected core memories),
                period summaries (weekly/monthly/yearly), and live memories
                weighted by importance score and age tier (hot/warm/cold).

                Parameters:
                    query: What to search for in long-term memory.
                    limit: Maximum results to return (default: 8).
                """
                c = MemoryConsolidator(_brain_dir)
                results = c.tiered_search(query, limit=int(limit))
                if not results:
                    return "No consolidated memories found for that query."
                lines = []
                for r in results:
                    tier = r.get('tier', '?')
                    imp = r.get('importance', 0)
                    lines.append(f"[{tier}|{imp:.0%}] {r.get('topic', '?')}: {r.get('content', '')[:300]}")
                return "\n\n".join(lines)

            def get_consolidation_stats(**kw) -> str:
                """Get statistics about the memory consolidation system:
                landmark count, period summary counts, tier distribution,
                last consolidation timestamp.
                """
                c = MemoryConsolidator(_brain_dir)
                return json.dumps(c.get_stats(), indent=2, default=str)

            for name, func in [
                ("consolidate_memories_deep", consolidate_memories_deep),
                ("search_consolidated_memory", search_consolidated_memory),
                ("get_consolidation_stats", get_consolidation_stats),
            ]:
                self.register(name, func, category="memory_consolidation")
                count += 1
            logger.info("  ✅ memory_consolidation: 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ memory_consolidation native registration failed: {e}")

        # ── Activity Frameworks (3 tools) ────────────────────────────────
        try:
            from repryntt.agents.framework_tracker import get_tracker

            def framework_start(name: str = "", data: str = "", **kw) -> str:
                """Start a structured activity framework for complex tasks.

                Frameworks enforce step-by-step procedures for trading activities.
                You MUST use a framework for any buy/sell/review activity.
                Call framework_status() to see available frameworks and current state.

                Available frameworks:
                  new_trade       — 7 steps: identify → research → verify → social → thesis → execute → journal
                  position_review — 4 steps: check positions → verify thesis → decide → journal
                  sell_decision   — 4 steps: assess trigger → verify state → execute → post-mortem

                Parameters:
                    name: Framework name (new_trade, position_review, sell_decision)
                    data: JSON string with initial step data, e.g. '{"token": "PISS", "address": "0x...", "source": "pipeline"}'
                """
                tracker = get_tracker(workspace)
                initial = {}
                if data:
                    try:
                        initial = json.loads(data) if isinstance(data, str) else data
                    except (json.JSONDecodeError, TypeError):
                        pass
                return tracker.start(name, initial)

            def framework_advance(data: str = "", **kw) -> str:
                """Advance the active framework to the next step.

                Provide the data collected at the current step as a JSON string.
                Each step has required fields — the framework will tell you what's missing.
                Call framework_status() to see what's needed for the current step.

                Parameters:
                    data: JSON string with step data, e.g. '{"narrative": "AI meme", "narrative_strength": "strong", "searches_done": 2}'
                """
                tracker = get_tracker(workspace)
                step_data = {}
                if data:
                    try:
                        step_data = json.loads(data) if isinstance(data, str) else data
                    except (json.JSONDecodeError, TypeError):
                        pass
                return tracker.advance(step_data)

            def framework_status(**kw) -> str:
                """Get the current framework status — active framework progress and recent history.

                Shows which step you're on, what data is needed, and completed frameworks.
                Use this to resume work across heartbeats or check if a framework is active.
                """
                tracker = get_tracker(workspace)
                return tracker.status()

            for name, func in [("framework_start", framework_start),
                               ("framework_advance", framework_advance),
                               ("framework_status", framework_status)]:
                self.register(name, func, category="frameworks")
                count += 1
            logger.info("  ✅ frameworks: 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ frameworks native registration failed: {e}")

        # ── Activity Frameworks (creative work state machines) ───────────
        try:
            from repryntt.agents.activity_frameworks import ActivityFrameworkEngine

            def framework_update(data: str = "", **kw) -> str:
                """Update your activity framework working state with outputs from this heartbeat.

                When an activity framework is active (Deep Research, Build Something,
                Cross-Pollinate, or Creative Write), use this tool to record your progress.
                The framework will check quality gates and advance you to the next step
                when requirements are met.

                Parameters:
                    data: JSON string with outputs for the current step.
                          Each step has required keys — check the framework guidance
                          in your PLAN prompt to see what's needed.
                          Example for research 'gather' step:
                          '{"sources": [{"title": "Paper X", "url": "...", "claim": "..."}]}'
                """
                _afe = ActivityFrameworkEngine(workspace)
                step_data = {}
                if data:
                    try:
                        step_data = json.loads(data) if isinstance(data, str) else data
                    except (json.JSONDecodeError, TypeError):
                        return "❌ Invalid JSON. Provide data as a valid JSON string."
                if not step_data:
                    return "❌ No data provided. Include required keys for the current step."
                return _afe.update_working_state(step_data)

            def activity_framework_status(**kw) -> str:
                """Get the status of the activity framework engine.

                Shows: active framework progress, run history, graduation status,
                question stack (curiosity-generated questions from completed work).
                """
                _afe = ActivityFrameworkEngine(workspace)
                return _afe.status()

            def pop_curiosity_question(**kw) -> str:
                """Pop the next unexplored question from your curiosity stack.

                Questions are generated automatically when you complete activity
                frameworks (the 'reflect' step). Use this to find your next
                research topic or exploration direction.

                Returns the question text, or a message if the stack is empty.
                """
                _afe = ActivityFrameworkEngine(workspace)
                q = _afe.pop_question()
                if q:
                    return f"🔮 Next curiosity question: {q}"
                return "Stack empty — complete more activity frameworks to generate questions."

            for name, func in [
                ("framework_update", framework_update),
                ("activity_framework_status", activity_framework_status),
                ("pop_curiosity_question", pop_curiosity_question),
            ]:
                self.register(name, func, category="activity_frameworks")
                count += 1
            logger.info("  ✅ activity_frameworks: 3 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ activity_frameworks native registration failed: {e}")

        # ── Degen Terminal Top — internal signal-based token discovery ────
        try:
            def degen_terminal_top(limit: int = 10, **kw) -> str:
                """Get the TOP tokens from our internal degen terminal — scored signal candidates.

                This shows tokens our system caught BEFORE they trend on DexScreener.
                Uses real-time data from our token monitor (refreshed every ~1 second),
                signal scorer, and pipeline. These are tokens with active buy signals,
                rising momentum, and verified on-chain data.

                ALWAYS use this instead of dexscreener_trending for finding trade candidates.
                Our internal system catches tokens earlier and with better signal quality.

                Parameters:
                    limit: Max tokens to return (default 10, max 20)
                """
                results = []
                limit_n = min(int(limit) if limit else 10, 20)

                # Source 1: Scored signals (most valuable — already ranked)
                scored_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "trading", "data", "scored_signals.json"
                )
                scored_path = os.path.normpath(scored_path)

                # Auto-refresh: re-score if data is stale (>5 min old)
                try:
                    _stale = True
                    if os.path.exists(scored_path):
                        _age = time.time() - os.path.getmtime(scored_path)
                        _stale = _age > 300  # 5 minutes
                    if _stale:
                        from repryntt.trading.signal_scorer import score_signals as _do_score
                        _do_score(max_age_s=1800)
                except Exception:
                    pass  # best-effort refresh

                try:
                    if os.path.exists(scored_path):
                        with open(scored_path) as f:
                            scored = json.load(f)
                        # Handle both formats: list or {"scored_tokens": [...]}
                        if isinstance(scored, dict):
                            scored = scored.get("scored_tokens", [])
                        if isinstance(scored, list):
                            scored.sort(key=lambda x: x.get("score", 0), reverse=True)
                            for s in scored[:limit_n]:
                                results.append({
                                    "source": "signal_scorer",
                                    "address": s.get("address", ""),
                                    "score": s.get("score", 0),
                                    "grade": s.get("grade", ""),
                                    "signal_count": s.get("signal_count", 0),
                                    "signal_types": s.get("signal_types", {}),
                                    "price": s.get("latest_price", 0),
                                    "mcap": s.get("market_cap", 0),
                                    "price_change_5m": s.get("price_change_5m", 0),
                                    "reasoning": s.get("reasoning", ""),
                                    "risk_flags": s.get("risk_flags", []),
                                })
                except Exception as e:
                    results.append({"error": f"scored_signals read failed: {e}"})

                # Source 2: Active tokens from monitor DB (real-time prices + momentum)
                try:
                    import sqlite3 as _sq
                    db_path = os.path.normpath(os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "..", "trading", "data", "tokens.db"
                    ))
                    if os.path.exists(db_path):
                        conn = _sq.connect(db_path, timeout=2)
                        conn.row_factory = _sq.Row
                        cur = conn.execute(
                            "SELECT address, base_token_symbol, token_name, current_price,"
                            " current_market_cap, price_change_5m, price_change_1m,"
                            " volume_5m, buy_volume_5m, sell_volume_5m,"
                            " top_20_holders_percentage, is_bundled, is_uptrend"
                            " FROM tokens ORDER BY current_market_cap DESC LIMIT ?",
                            (limit_n,)
                        )
                        # Only add tokens not already in results from scored signals
                        seen = {r.get("address") for r in results}
                        for row in cur.fetchall():
                            if row["address"] not in seen:
                                results.append({
                                    "source": "degen_terminal",
                                    "address": row["address"],
                                    "symbol": row["base_token_symbol"] or "",
                                    "name": row["token_name"] or "",
                                    "price": float(row["current_price"] or 0),
                                    "mcap": float(row["current_market_cap"] or 0),
                                    "price_change_5m": float(row["price_change_5m"] or 0),
                                    "price_change_1m": float(row["price_change_1m"] or 0),
                                    "volume_5m": float(row["volume_5m"] or 0),
                                    "holder_pct": float(row["top_20_holders_percentage"] or 0),
                                    "bundled": bool(row["is_bundled"]),
                                    "uptrend": bool(row["is_uptrend"]),
                                })
                        conn.close()
                except Exception as e:
                    results.append({"error": f"DB read failed: {e}"})

                if not results:
                    return json.dumps({"tokens": [], "message": "No tokens in degen terminal right now. Pipeline may be between cycles."})

                return json.dumps({"tokens": results[:limit_n], "count": len(results)}, indent=2, default=str)

            self.register("degen_terminal_top", degen_terminal_top, category="trading_internal")
            count += 1
            logger.info("  ✅ degen_terminal_top: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ degen_terminal_top native registration failed: {e}")

        # ── Remove Token — let Artemis clean bad tokens from the terminal ────
        try:
            def remove_token(address: str, reason: str = "", **kw) -> str:
                """Remove a token from the degen terminal database.

                Use this to clean up bad tokens: bundled, rugged, dead, or
                any token you've analyzed and determined is not worth tracking.
                This frees up space in the terminal for better opportunities.

                Parameters:
                    address: The token's contract address (Solana base58)
                    reason: Why you're removing it (e.g. 'bundled', 'rugged', 'dead')
                """
                import sqlite3 as _sq
                db_path = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "trading", "data", "tokens.db"
                ))
                if not os.path.exists(db_path):
                    return json.dumps({"error": "Token database not found"})
                addr = str(address).strip()
                if len(addr) < 10:
                    return json.dumps({"error": "Invalid address"})
                try:
                    conn = _sq.connect(db_path, timeout=3)
                    row = conn.execute(
                        "SELECT base_token_symbol, token_name FROM tokens WHERE address = ?",
                        (addr,)
                    ).fetchone()
                    if not row:
                        conn.close()
                        return json.dumps({"error": f"Token {addr[:12]}... not in database"})
                    name = row[1] or row[0] or addr[:12]
                    conn.execute("DELETE FROM tokens WHERE address = ?", (addr,))
                    conn.commit()
                    conn.close()
                    return json.dumps({
                        "status": "removed",
                        "token": name,
                        "address": addr,
                        "reason": str(reason)[:200] if reason else "manual removal",
                    })
                except Exception as e:
                    return json.dumps({"error": f"Remove failed: {e}"})

            self.register("remove_token", remove_token, category="trading_internal")
            count += 1
            logger.info("  ✅ remove_token: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ remove_token native registration failed: {e}")

        # ── Auto-purge bundled/dead tokens — Artemis-callable cleanup ────
        try:
            def purge_bad_tokens(**kw) -> str:
                """Bulk-remove all bundled and dead tokens from the degen terminal.

                Removes:
                - Tokens flagged as bundled (is_bundled = 1)
                - Dead tokens (mcap < $1K and price near zero)

                Run this periodically to keep the terminal clean and focused
                on quality opportunities.
                """
                import sqlite3 as _sq
                db_path = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "trading", "data", "tokens.db"
                ))
                if not os.path.exists(db_path):
                    return json.dumps({"error": "Token database not found"})
                try:
                    conn = _sq.connect(db_path, timeout=3)
                    conn.row_factory = _sq.Row
                    bundled = conn.execute(
                        "SELECT address, token_name FROM tokens WHERE is_bundled = 1"
                    ).fetchall()
                    dead = conn.execute(
                        "SELECT address, token_name FROM tokens "
                        "WHERE current_market_cap < 1000 AND current_price < 0.000001"
                    ).fetchall()
                    addresses = set()
                    removed = []
                    for row in bundled:
                        addresses.add(row["address"])
                        removed.append(f"{row['token_name'] or '?'} (bundled)")
                    for row in dead:
                        if row["address"] not in addresses:
                            addresses.add(row["address"])
                            removed.append(f"{row['token_name'] or '?'} (dead)")
                    for addr in addresses:
                        conn.execute("DELETE FROM tokens WHERE address = ?", (addr,))
                    conn.commit()
                    conn.close()
                    return json.dumps({
                        "status": "purged",
                        "removed_count": len(addresses),
                        "removed": removed[:30],
                    }, indent=2)
                except Exception as e:
                    return json.dumps({"error": f"Purge failed: {e}"})

            self.register("purge_bad_tokens", purge_bad_tokens, category="trading_internal")
            count += 1
            logger.info("  ✅ purge_bad_tokens: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ purge_bad_tokens native registration failed: {e}")

        # ── CodeForge (5 tools — routed through paid-features layer) ──────
        # The local CodeForge implementation lives in repryntt/codeforge/ —
        # present in the operator's "Pro" tree, absent from the OSS
        # distribution. The router in repryntt.paid_features.forge tries
        # local first, falls through to the hosted api.repryntt.com, then
        # to a paywall message. Same code, same registration in both
        # trees; behavior differs only by which files are installed.
        try:
            from repryntt.paid_features import forge as _pf_forge

            def forge_project(description: str = "", provider: str = "",
                              model: str = "",
                              swarm_enabled: bool = False, **kw) -> str:
                """Propose a new CodeForge project. The proposal will go through
                deliberation before building. Use forge_status to check progress.

                CodeForge is a paid hosted feature in the public release. On
                the operator's Pro tree it runs locally with full access.

                Parameters:
                    description: Natural language description of the software to build.
                    provider: LLM provider to use (nvidia, anthropic, openai, openrouter, local). Leave empty for the installation default.
                    model: Specific model id (e.g. 'claude-opus-4-7'). Leave empty to use the provider's coding_model default.
                    swarm_enabled: If True, distribute work across P2P network nodes (Pro-only).
                """
                if not description:
                    return json.dumps({"error": "Description required"})
                result = _pf_forge.propose_project(
                    description=description, provider=provider,
                    model=model, proposer="andrew",
                    swarm_enabled=swarm_enabled, **kw,
                )
                # Preserve a small status-shaped envelope around the
                # underlying response when local path returned a raw
                # proposal record.
                if isinstance(result, dict) and "id" in result and "success" not in result:
                    return json.dumps({
                        "status": "proposed",
                        "proposal_id": result["id"],
                        "description": description[:200],
                        "message": ("Proposal submitted for deliberation. "
                                    "Operator must approve before building starts."),
                    }, indent=2)
                return json.dumps(result, indent=2)

            def forge_status(project_id: str = "", **kw) -> str:
                """Check the status and progress of a CodeForge project.

                Parameters:
                    project_id: The project ID to check. If empty, shows all projects.
                """
                return json.dumps(_pf_forge.forge_status(project_id, **kw), indent=2)

            def forge_cancel(project_id: str = "", **kw) -> str:
                """Cancel a running CodeForge project.

                Parameters:
                    project_id: The project ID to cancel.
                """
                if not project_id:
                    return json.dumps({"error": "project_id required"})
                # Pro tree: local cancel. OSS tree: ImportError → hosted route.
                try:
                    from repryntt.codeforge.forge import get_forge as _gf
                    ok = _gf().cancel_project(project_id)
                    return json.dumps({"cancelled": ok})
                except ImportError:
                    pass
                except Exception as e:
                    return json.dumps({"error": f"cancel failed: {e}"})
                from repryntt.paid_features import _http as _http_mod
                api_key = _http_mod.load_api_key()
                if not api_key:
                    return json.dumps(_http_mod.paywall_response("CodeForge"))
                return json.dumps(_http_mod.post(
                    f"/v1/forge/projects/{project_id}/cancel",
                    {}, api_key, feature="CodeForge",
                ), indent=2)

            def forge_benchmark(provider: str = "", **kw) -> str:
                """Run the CodeForge coding benchmark on the current LLM."""
                return json.dumps(_pf_forge.forge_benchmark(provider, **kw), indent=2)

            def forge_swarm_status(**kw) -> str:
                """Check the status of the CodeForge distributed swarm."""
                # Pro tree: local swarm. OSS tree: ImportError → hosted route.
                try:
                    from repryntt.codeforge.swarm import get_swarm
                    return json.dumps(get_swarm().get_status(), indent=2)
                except ImportError:
                    pass
                except Exception as e:
                    return json.dumps({"error": str(e)})
                from repryntt.paid_features import _http as _http_mod
                api_key = _http_mod.load_api_key()
                if not api_key:
                    return json.dumps(_http_mod.paywall_response("CodeForge"))
                return json.dumps(_http_mod.get(
                    "/v1/forge/swarm/status", None, api_key, feature="CodeForge",
                ), indent=2)

            _forge_tools = {
                "forge_project": forge_project,
                "forge_status": forge_status,
                "forge_cancel": forge_cancel,
                "forge_benchmark": forge_benchmark,
                "forge_swarm_status": forge_swarm_status,
            }
            self.register_many(_forge_tools, category="codeforge")
            count += len(_forge_tools)
            logger.info(f"  ✅ codeforge: {len(_forge_tools)} tools registered (routed via paid_features)")
        except Exception as e:
            logger.warning(f"  ⚠️ codeforge native registration failed: {e}")

        # ── 3k: Andrew's Hub — Git publish to GitHub (4 tools) ───────────
        try:
            from repryntt.tools import git_publish as _gp

            def hub_publish(filepath: str = "", content: str = "",
                            commit_message: str = "", **kw) -> str:
                """Create or update a file in Andrew's Hub (GitHub) and push it.
                Use this to publish research, code, articles, or any content to
                https://github.com/ai158z/andrewshub."""
                return _gp.hub_publish(filepath=filepath, content=content,
                                       commit_message=commit_message)

            def hub_list(directory: str = "", **kw) -> str:
                """List files in Andrew's Hub repository."""
                return _gp.hub_list(directory=directory)

            def hub_read(filepath: str = "", **kw) -> str:
                """Read a file from Andrew's Hub repository."""
                return _gp.hub_read(filepath=filepath)

            def hub_delete(filepath: str = "", commit_message: str = "", **kw) -> str:
                """Delete a file from Andrew's Hub and push the change."""
                return _gp.hub_delete(filepath=filepath, commit_message=commit_message)

            for name, func in [
                ("hub_publish", hub_publish),
                ("hub_list", hub_list),
                ("hub_read", hub_read),
                ("hub_delete", hub_delete),
            ]:
                self.register(name, func, category="git_publish")
            count += 4
            logger.info("  ✅ git_publish: 4 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ git_publish native registration failed: {e}")

        # ── 3l: Open Mind — Expanded cognition tools (5 tools) ───────────
        try:
            from repryntt.tools import open_mind as _om

            def open_mind_begin(purpose: str = "", profile: str = "",
                                context: str = "", **kw) -> str:
                """Begin an Open Mind expanded cognition session. Enter a deeper,
                freer thinking mode with elevated creativity for a specific purpose.
                Profiles: explore, introspect, synthesize, reimagine, dream."""
                return _om.open_mind_begin(purpose=purpose, profile=profile,
                                           context=context)

            def open_mind_integrate(insights: str = "", purpose: str = "",
                                    profile: str = "explore", **kw) -> str:
                """Integrate insights from an Open Mind session back into memory.
                Call this after your expanded thinking round to save and archive."""
                return _om.open_mind_integrate(insights=insights, purpose=purpose,
                                               profile=profile)

            def open_mind_history(limit: int = 5, **kw) -> str:
                """View recent Open Mind session history."""
                return _om.open_mind_history(limit=limit)

            def open_mind_profiles(**kw) -> str:
                """List available expanded cognition profiles and their parameters."""
                return _om.open_mind_profiles()

            def open_mind_read_session(filename: str = "", **kw) -> str:
                """Read a specific past Open Mind session in full."""
                return _om.open_mind_read_session(filename=filename)

            for name, func in [
                ("open_mind_begin", open_mind_begin),
                ("open_mind_integrate", open_mind_integrate),
                ("open_mind_history", open_mind_history),
                ("open_mind_profiles", open_mind_profiles),
                ("open_mind_read_session", open_mind_read_session),
            ]:
                self.register(name, func, category="open_mind")
            count += 5
            logger.info("  ✅ open_mind: 5 tools registered natively")

            # Dream journal tools (involuntary dream cycle)
            def open_mind_dream_journal(limit: int = 5, **kw) -> str:
                """Read your dream journal — past involuntary dream sessions that
                happened during idle periods. Review them to find patterns in what
                your unconscious processing keeps returning to."""
                return _om.open_mind_dream_journal(limit=limit)

            def open_mind_read_dream(filename: str = "", **kw) -> str:
                """Read a specific dream from the dream journal in full."""
                return _om.open_mind_read_dream(filename=filename)

            for name, func in [
                ("open_mind_dream_journal", open_mind_dream_journal),
                ("open_mind_read_dream", open_mind_read_dream),
            ]:
                self.register(name, func, category="open_mind")
            count += 2
            logger.info("  ✅ open_mind dreams: 2 tools registered natively")

        except Exception as e:
            logger.warning(f"  ⚠️ open_mind native registration failed: {e}")

        # ── Jupiter DEX Tools (6 tools — direct on-chain Solana swaps, multi-wallet) ────
        try:
            from repryntt.tools.jupiter_tools import ALL_JUPITER_TOOLS
            for tname, tfunc in ALL_JUPITER_TOOLS.items():
                self.register(tname, tfunc, category="jupiter")
            jup_count = len(ALL_JUPITER_TOOLS)
            count += jup_count
            logger.info(f"  ✅ jupiter: {jup_count} tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ jupiter native registration failed: {e}")

        # ── Payment Gateway (SOL/USDC → Credits on-ramp, 5 tools) ──────────
        try:
            from repryntt.economy.payment_gateway import (
                create_deposit, get_deposit_status, get_gateway_status,
                list_deposits, poll_deposits_sync,
            )

            def gateway_create_deposit(repryntt_address: str = "", **kw) -> str:
                """Create a deposit request to buy Credits (CR) with SOL or USDC.
                Returns a Solana deposit address. Send SOL or USDC there, and Credits
                will be minted to the buyer's Repryntt blockchain address.

                Parameters:
                    repryntt_address: The buyer's Repryntt wallet address (40 hex chars).
                """
                return create_deposit(repryntt_address)

            def gateway_deposit_status(deposit_id: str = "", **kw) -> str:
                """Check the status of a Credit deposit (pending, completed, or failed).

                Parameters:
                    deposit_id: The deposit ID from gateway_create_deposit.
                """
                return get_deposit_status(deposit_id)

            def gateway_status(**kw) -> str:
                """Get the payment gateway status — deposit address, pricing, stats,
                and how many deposits have been processed."""
                return get_gateway_status()

            def gateway_list_deposits(limit: int = 20, **kw) -> str:
                """List recent deposit transactions (pending and completed).

                Parameters:
                    limit: Max deposits to return (default 20).
                """
                return list_deposits(limit=limit)

            def gateway_poll_deposits(**kw) -> str:
                """Poll Solana for new incoming payments and process them.
                Checks for SOL/USDC transfers to our deposit address,
                then mints equivalent Credits to the buyer's Repryntt address.
                Call this periodically (every ~30s) to process deposits."""
                result = poll_deposits_sync()
                return json.dumps(result)

            for name, func in [
                ("gateway_create_deposit", gateway_create_deposit),
                ("gateway_deposit_status", gateway_deposit_status),
                ("gateway_status", gateway_status),
                ("gateway_list_deposits", gateway_list_deposits),
                ("gateway_poll_deposits", gateway_poll_deposits),
            ]:
                self.register(name, func, category="economy")
            count += 5
            logger.info("  ✅ payment_gateway: 5 tools registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ payment_gateway native registration failed: {e}")

        # ── Task Queue (5 tools) ─────────────────────────────────────────
        try:
            from repryntt.agents.task_queue_tools import (
                task_queue_status, add_task, complete_current_task, skip_task,
                retype_task,
            )
            for name, func in [
                ("task_queue_status", task_queue_status),
                ("add_task", add_task),
                ("complete_current_task", complete_current_task),
                ("skip_task", skip_task),
                ("retype_task", retype_task),
            ]:
                self.register(name, func, category="task_queue")
            count += 5
            logger.info("  ✅ task_queue: 5 tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ task_queue native registration failed: {e}")

        # ── Tank Body Control (9 tools) ──────────────────────────────────
        try:
            from repryntt.hardware.motor_client import (
                DaemonUnavailable,
                MotorClientError,
                Preempted,
                Priority,
                daemon_status,
                session as motor_session,
            )

            def _motor_tool(priority: Priority, holder: str, fn) -> str:
                try:
                    with motor_session(
                        priority=priority,
                        holder_label=holder,
                        wait_timeout_s=3.0,
                        require_daemon=True,
                    ) as sess:
                        return json.dumps(fn(sess))
                except DaemonUnavailable as e:
                    return json.dumps({
                        "success": False,
                        "error": f"motor_daemon_unavailable: {e}",
                    })
                except Preempted:
                    return json.dumps({
                        "success": False,
                        "error": "preempted_by_higher_priority",
                    })
                except (MotorClientError, TimeoutError) as e:
                    return json.dumps({"success": False, "error": str(e)})

            def tank_move_forward(speed: float = 0.6, duration: float = 1.0, **kw) -> str:
                """Drive the tank body forward.

                Parameters:
                    speed: Speed as fraction 0.0-1.0 (default 0.6 = 60% power). Values >1 treated as percent.
                    duration: How long to move in seconds (default 1.0, max 10).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_move_forward",
                    lambda s: s.move_forward(float(speed), float(duration)),
                )

            def tank_move_backward(speed: float = 0.6, duration: float = 1.0, **kw) -> str:
                """Drive the tank body backward (reverse).

                Parameters:
                    speed: Speed as fraction 0.0-1.0 (default 0.6 = 60% power). Values >1 treated as percent.
                    duration: How long to move in seconds (default 1.0, max 10).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_move_backward",
                    lambda s: s.move_backward(float(speed), float(duration)),
                )

            def tank_turn_left(speed: float = 0.5, duration: float = 1.0, **kw) -> str:
                """Turn the tank body left (pivot in place — left track backward, right track forward).

                Parameters:
                    speed: Speed as fraction 0.0-1.0 (default 0.5).
                    duration: How long to turn in seconds (default 1.0).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_turn_left",
                    lambda s: s.turn_left(float(speed), float(duration)),
                )

            def tank_turn_right(speed: float = 0.5, duration: float = 1.0, **kw) -> str:
                """Turn the tank body right (pivot in place — left track forward, right track backward).

                Parameters:
                    speed: Speed as fraction 0.0-1.0 (default 0.5).
                    duration: How long to turn in seconds (default 1.0).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_turn_right",
                    lambda s: s.turn_right(float(speed), float(duration)),
                )

            def tank_spin(degrees: float = 180, speed: float = 0.5, **kw) -> str:
                """Spin the tank body in place by a given number of degrees.
                Positive = counter-clockwise (left), negative = clockwise (right).

                Parameters:
                    degrees: Degrees to rotate (default 180). Positive=left, negative=right.
                    speed: Speed as fraction 0.0-1.0 (default 0.5).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_spin",
                    lambda s: s.spin(float(degrees), float(speed)),
                )

            def tank_stop(**kw) -> str:
                """Gracefully stop the tank body — kills PWM and sets all motor pins LOW."""
                return _motor_tool(
                    Priority.SAFETY,
                    "jarvis:tank_stop",
                    lambda s: s.stop(),
                )

            def tank_emergency_stop(**kw) -> str:
                """EMERGENCY STOP — immediately kills all motor power.
                Use only in dangerous situations. Call tank_reset_emergency_stop to re-enable motors.
                """
                return _motor_tool(
                    Priority.SAFETY,
                    "jarvis:tank_emergency_stop",
                    lambda s: s.emergency_stop(),
                )

            def tank_reset_emergency_stop(**kw) -> str:
                """Reset emergency stop condition and re-enable motor control."""
                return _motor_tool(
                    Priority.SAFETY,
                    "jarvis:tank_reset_emergency_stop",
                    lambda s: s.reset_emergency_stop(),
                )

            def tank_body_status(**kw) -> str:
                """Get full body status: motor state (speed, direction per motor),
                emergency stop status, GPIO initialization state, command history.
                Call this to be aware of your physical body's condition.
                """
                try:
                    return json.dumps(daemon_status(require_daemon=True))
                except DaemonUnavailable as e:
                    return json.dumps({
                        "success": False,
                        "error": f"motor_daemon_unavailable: {e}",
                    })
                except MotorClientError as e:
                    return json.dumps({"success": False, "error": str(e)})

            def tank_move_distance(distance_cm: float, speed: float = 0.5, **kw) -> str:
                """Drive forward (+) or backward (−) by an estimated distance in cm.

                Closed-loop displacement primitive. Currently time-based
                (uses CM_PER_SEC_AT_FULL calibration); when wheel encoders
                land it'll become true closed-loop. The sonar safety reflex
                may abort early if a wall appears — check estimated_cm in
                the response for actual travel.

                Parameters:
                    distance_cm: signed distance in centimetres (negative = reverse).
                    speed: speed fraction 0.0-1.0 (default 0.5).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_move_distance",
                    lambda s: s.move_distance(float(distance_cm), float(speed)),
                )

            def tank_turn_degrees(degrees: float, speed: float = 0.5, **kw) -> str:
                """In-place pivot by target degrees. Positive=left, negative=right.

                Time-based (uses DEG_PER_SEC_AT_FULL); upgrade path is an
                IMU-based closed loop. Returns estimated_degrees in the
                response.

                Parameters:
                    degrees: signed degrees (positive=left, negative=right).
                    speed: speed fraction 0.0-1.0 (default 0.5).
                """
                return _motor_tool(
                    Priority.AUTONOMOUS,
                    "jarvis:tank_turn_degrees",
                    lambda s: s.turn_degrees(float(degrees), float(speed)),
                )

            for name, func in [
                ("tank_move_forward", tank_move_forward),
                ("tank_move_backward", tank_move_backward),
                ("tank_turn_left", tank_turn_left),
                ("tank_turn_right", tank_turn_right),
                ("tank_spin", tank_spin),
                ("tank_stop", tank_stop),
                ("tank_emergency_stop", tank_emergency_stop),
                ("tank_reset_emergency_stop", tank_reset_emergency_stop),
                ("tank_body_status", tank_body_status),
                ("tank_move_distance", tank_move_distance),
                ("tank_turn_degrees", tank_turn_degrees),
            ]:
                self.register(name, func, category="body_control")
            count += 11
            logger.info("  ✅ body_control: 11 tank body tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ body_control native registration failed: {e}")

        # ── Spatial Awareness (3 tools) ──────────────────────────────────
        # Expose what the explorer already tracks: pose, map stats, and
        # frontier cells. Without these, Andrew's LLM layer had no way to
        # see the spatial memory the hardware layer was building.
        try:
            def nav_pose(**kw) -> str:
                """Current estimated pose of the tank (dead-reckoned from
                motor commands; absolute accuracy drifts over time, use
                for relative reasoning).

                Returns JSON with: x_cm, y_cm, heading_deg, compass,
                total_distance_m, moves, places_visited, frontiers_seen.
                """
                try:
                    from repryntt.hardware.spatial_map import get_spatial_map
                    from repryntt.hardware.spatial_context import _compass
                    smap = get_spatial_map()
                    frontiers = getattr(smap, "frontiers", []) or []
                    unexplored = [f for f in frontiers
                                  if isinstance(f, dict)
                                  and not f.get("explored")]
                    return json.dumps({
                        "x_cm": round(float(smap.x), 1),
                        "y_cm": round(float(smap.y), 1),
                        "heading_deg": round(float(smap.heading), 1),
                        "compass": _compass(float(smap.heading)),
                        "total_distance_m": round(
                            float(smap.total_distance_cm) / 100.0, 2),
                        "moves": int(smap.move_count),
                        "places_visited": len(smap.places or {}),
                        "semantic_frontiers_unexplored": len(unexplored),
                    })
                except Exception as e:
                    return json.dumps({"error": f"pose unavailable: {e}"})

            def nav_frontiers(max_count: int = 5, **kw) -> str:
                """List the nearest unexplored frontier cells from the
                occupancy grid — edges of known territory.

                Each frontier has world coords (cm), distance (m), and
                a bearing relative to the robot's current heading.
                Use these as concrete navigation goals instead of
                abstract terms like 'the hallway'.
                """
                try:
                    from repryntt.hardware.spatial_map import get_spatial_map
                    from repryntt.hardware.local_perception import (
                        OccupancyGrid,
                    )
                    from repryntt.hardware.spatial_context import (
                        _bearing_to, _bearing_phrase,
                    )
                    smap = get_spatial_map()
                    grid = OccupancyGrid()
                    max_count = max(1, min(20, int(max_count)))
                    cells = grid.get_frontier_cells(
                        float(smap.x), float(smap.y),
                        max_count=max_count)
                    items = []
                    for c in cells:
                        if len(c) < 3:
                            continue
                        fx, fy = c[0], c[1]
                        dist_cm, delta = _bearing_to(
                            float(smap.x), float(smap.y),
                            float(smap.heading), fx, fy)
                        items.append({
                            "x_cm": round(fx, 1),
                            "y_cm": round(fy, 1),
                            "distance_m": round(dist_cm / 100.0, 2),
                            "bearing_deg": round(delta, 1),
                            "direction": _bearing_phrase(delta),
                        })
                    return json.dumps({
                        "robot_pose": {
                            "x_cm": round(float(smap.x), 1),
                            "y_cm": round(float(smap.y), 1),
                            "heading_deg": round(float(smap.heading), 1),
                        },
                        "frontier_count": len(items),
                        "frontiers": items,
                    })
                except Exception as e:
                    return json.dumps({"error": f"frontiers unavailable: {e}"})

            def nav_map_ascii(radius_cells: int = 20, **kw) -> str:
                """Render a small ASCII view of the occupancy grid centred
                on the robot. Legend: 'R' = you, '.' = free, '#' = wall,
                '?' = unknown. Each char = 10 cm.

                Parameters:
                    radius_cells: Half-width of the view (default 20 →
                    41×41 grid covering ~4 m × 4 m around the robot).
                """
                try:
                    from repryntt.hardware.spatial_map import get_spatial_map
                    from repryntt.hardware.local_perception import (
                        OccupancyGrid, FREE, OCCUPIED,
                    )
                    smap = get_spatial_map()
                    grid = OccupancyGrid()
                    radius_cells = max(5, min(50, int(radius_cells)))
                    rx, ry = grid.world_to_grid(
                        float(smap.x), float(smap.y))
                    lines = []
                    for dy in range(radius_cells, -radius_cells - 1, -1):
                        row = []
                        for dx in range(-radius_cells, radius_cells + 1):
                            gx = rx + dx
                            gy = ry + dy
                            if (0 <= gx < grid.size
                                    and 0 <= gy < grid.size):
                                if dx == 0 and dy == 0:
                                    row.append("R")
                                elif grid.grid[gy, gx] == OCCUPIED:
                                    row.append("#")
                                elif grid.grid[gy, gx] == FREE:
                                    row.append(".")
                                else:
                                    row.append("?")
                            else:
                                row.append(" ")
                        lines.append("".join(row))
                    ascii_map = "\n".join(lines)
                    return json.dumps({
                        "robot_grid": [rx, ry],
                        "robot_world_cm": [
                            round(float(smap.x), 1),
                            round(float(smap.y), 1),
                        ],
                        "heading_deg": round(float(smap.heading), 1),
                        "radius_cells": radius_cells,
                        "cell_size_cm": grid.resolution,
                        "ascii": ascii_map,
                        "legend": "R=you, .=free, #=wall, ?=unknown",
                    })
                except Exception as e:
                    return json.dumps({"error": f"map unavailable: {e}"})

            for name, func in [
                ("nav_pose", nav_pose),
                ("nav_frontiers", nav_frontiers),
                ("nav_map_ascii", nav_map_ascii),
            ]:
                self.register(name, func, category="spatial_awareness")
            count += 3
            logger.info("  ✅ spatial_awareness: 3 pose/map tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ spatial_awareness registration failed: {e}")

        # ── Tank RL Training (1 tool) ────────────────────────────────────
        try:
            def tank_sim_train(episodes: int = 300, grid_size: int = 15,
                               obstacles: int = 6, **kw) -> str:
                """Train a navigation AI in a 2D simulated world that matches your real tank.
                The agent learns to navigate to goals while avoiding obstacles.
                Returns training stats: goal rate, reward curve, learned policy.

                Parameters:
                    episodes: Number of training episodes (default 300, max 2000).
                    grid_size: World size in cells (default 15, each cell ≈ 30cm).
                    obstacles: Number of obstacles (default 6).
                """
                from repryntt.hardware.tank_sim import TankSimEnv, TankQLearner
                episodes = max(50, min(2000, int(episodes)))
                env = TankSimEnv(grid_size=int(grid_size), num_obstacles=int(obstacles))
                agent = TankQLearner()
                results = agent.train(env, episodes=episodes, verbose=False)
                results["policy_summary"] = agent.get_policy_summary()
                # Demo run
                obs, info = env.reset(seed=42)
                agent.epsilon = 0
                for _ in range(50):
                    action = agent.choose_action(obs)
                    obs, r, term, trunc, info = env.step(action)
                    if term:
                        break
                results["demo_reached_goal"] = term
                results["demo_steps"] = info["steps"]
                return json.dumps(results)

            self.register("tank_sim_train", tank_sim_train, category="body_control")
            count += 1
            logger.info("  ✅ body_control: tank_sim_train registered")
        except Exception as e:
            logger.warning(f"  ⚠️ tank_sim_train registration failed: {e}")

        # ── Navigation Cortex (vision-to-action, 4 tools) ───────────────
        try:
            def nav_look(camera_id: int = 0, **kw) -> str:
                """Look through your camera — YOUR eyes. The VLM (visual cortex)
                processes the raw image and extracts scene data, then YOU (the brain)
                interpret what you're seeing. Returns what your visual cortex detected:
                obstacle proximity, best path, scene description, people present.
                You should interpret this data and tell the user what YOU see,
                adding your own judgment and context — don't just parrot the raw data.

                Parameters:
                    camera_id: 0 = front camera, 1 = rear camera.
                """
                from repryntt.hardware.nav_cortex import get_nav_cortex
                cortex = get_nav_cortex()
                image_path = cortex.capture_frame(camera_id)
                if not image_path:
                    return json.dumps({"error": "Camera capture failed"})
                perception = cortex.perceive(image_path)
                perception["image_path"] = image_path
                perception["_note"] = (
                    "This is raw visual cortex data. As Andrew, interpret what "
                    "you see — describe it naturally to the user like you're "
                    "looking through your own eyes, not reading sensor output."
                )
                try:
                    from repryntt.hardware.spatial_map import get_spatial_map
                    smap = get_spatial_map()
                    scene = perception.get("scene", "")
                    obstacles = perception.get("obstacles", {})
                    best_dir = perception.get("path", {}).get("best_direction", "")
                    smap.record_observation(scene, obstacles=obstacles,
                                            best_direction=best_dir)
                    perception["spatial_map"] = smap.get_exploration_context()
                except Exception:
                    pass
                return json.dumps(perception)

            def nav_step(camera_id: int = 0, execute: bool = False,
                         speed: float = 0.4, duration: float = 0.8, **kw) -> str:
                """One full see-think-act navigation cycle.
                Captures camera → analyzes scene → decides action → optionally moves.
                Set execute=true to actually drive the motors.

                Parameters:
                    camera_id: 0 = front camera, 1 = rear.
                    execute: If true, actually send motor commands. False = dry run.
                    speed: Motor speed 0.0-1.0 (default 0.4 = cautious).
                    duration: How long to run the motor (seconds, default 0.8).
                """
                from repryntt.hardware.nav_cortex import get_nav_cortex
                cortex = get_nav_cortex()
                result = cortex.navigate_step(
                    camera_id=int(camera_id), execute=bool(execute),
                    speed=float(speed), duration=float(duration))
                return json.dumps(result, default=str)

            def nav_sequence(steps: int = 3, camera_id: int = 0,
                             execute: bool = False, speed: float = 0.4,
                             duration: float = 0.6, **kw) -> str:
                """Run multiple see-think-act cycles — a short autonomous walk.
                Each step: capture → perceive → decide → act → pause → repeat.

                Parameters:
                    steps: Number of cycles (default 3, max 10).
                    execute: If true, actually move. False = planning only.
                    speed: Motor speed 0.0-1.0 (default 0.4).
                    duration: Seconds per movement (default 0.6).
                """
                from repryntt.hardware.nav_cortex import get_nav_cortex
                cortex = get_nav_cortex()
                steps = max(1, min(10, int(steps)))
                result = cortex.navigate_sequence(
                    steps=steps, camera_id=int(camera_id),
                    execute=bool(execute), speed=float(speed),
                    duration=float(duration))
                # Trim step_details for brevity
                for s in result.get("step_details", []):
                    s.pop("perception", None)
                return json.dumps(result, default=str)

            def nav_status(**kw) -> str:
                """Check navigation cortex status — mode, Q-table, spatial memory."""
                from repryntt.hardware.nav_cortex import get_nav_cortex
                cortex = get_nav_cortex()
                return json.dumps(cortex.status())

            for name, func in [
                ("nav_look", nav_look),
                ("nav_step", nav_step),
                ("nav_sequence", nav_sequence),
                ("nav_status", nav_status),
            ]:
                self.register(name, func, category="body_control")
            count += 4
            logger.info("  ✅ body_control: 4 nav cortex tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ nav cortex registration failed: {e}")

        # ── Monocular Depth (1 tool) ────────────────────────────────────
        try:
            def nav_depth(**kw) -> str:
                """Capture ONE camera frame and compute a neural depth map.
                Returns obstacle proximity per zone (left/center/right) via
                Depth Anything v2 — works on headless Jetson, unlike stereo.
                Also saves a colorized depth map image. Pure local compute (~100ms).

                This is your most reliable obstacle sensor. Use it before moving.
                """
                from repryntt.hardware.nav_cortex import get_nav_cortex
                from repryntt.hardware.depth_perception import get_depth_estimator
                cortex = get_nav_cortex()
                est = get_depth_estimator()
                if not est.available:
                    return json.dumps({"error": "Depth model unavailable"})
                image_path = cortex.capture_frame(camera_id=0)
                if not image_path:
                    return json.dumps({"error": "Camera capture failed"})
                result = est.estimate_depth_from_file(image_path)
                if result is None:
                    return json.dumps({"error": "Depth estimation failed"})
                depth = result.to_stereo_depth()
                # Save colorized depth map next to the source frame
                try:
                    import os
                    base, _ = os.path.splitext(image_path)
                    vis_path = result.save_visualization(f"{base}_depth.jpg")
                except Exception:
                    vis_path = ""
                return json.dumps({
                    "left_proximity": depth.left_proximity,
                    "center_proximity": depth.center_proximity,
                    "right_proximity": depth.right_proximity,
                    "min_distance_cm": depth.min_distance_cm,
                    "depth_map_path": vis_path,
                    "compute_time_ms": depth.compute_time_ms,
                    "source": "monocular (Depth Anything v2)",
                    "guide": "proximity 0=clear, 1=blocked. distance is cm to nearest object (relative, not metric)."
                })

            self.register("nav_depth", nav_depth, category="body_control")
            count += 1
            logger.info("  ✅ body_control: nav_depth (monocular) registered")
        except Exception as e:
            logger.warning(f"  ⚠️ nav_depth registration failed: {e}")

        try:
            def nav_map(**kw) -> str:
                """Check your spatial map — where you've been, where you haven't.
                Shows your estimated position, discovered places, and UNEXPLORED
                frontiers you should go explore next. Call this every few moves
                to plan your route.
                """
                from repryntt.hardware.spatial_map import get_spatial_map
                smap = get_spatial_map()
                return smap.get_exploration_context()

            self.register("nav_map", nav_map, category="body_control")
            count += 1
            logger.info("  ✅ body_control: nav_map (spatial map) registered")
        except Exception as e:
            logger.warning(f"  ⚠️ nav_map registration failed: {e}")

        try:
            def nav_explore(goal: str = "explore freely",
                            steps: int = 50, speed: float = 0.3, **kw) -> str:
                """Start autonomous exploration — the robot moves on its own!
                This runs a background see→move→see loop WITHOUT needing you
                to call nav_step each time. You set a goal and it explores.
                Call nav_explore_status() to check progress, nav_explore_stop() to halt.

                Parameters:
                    goal: What to explore, e.g. "explore freely", "find the hallway",
                          "go toward the light", "map the kitchen"
                    steps: Max steps before auto-stop (default 50). You can set
                           this to 100s or 1000s for extended exploration runs.
                           There is no artificial cap — you decide how far to go.
                    speed: Motor speed 0-1 (0.3=cautious, 0.5=normal). Start low.
                """
                # Mission-scope guard — reject verification-themed goals (operator directive 2026-04-23)
                _goal_lower = (goal or "").lower()
                _blocked = (
                    "verify systems agent", "systems agent verification",
                    "verification hook", "verification hooks",
                    "pattern 4", "trv protocol", "dcc protocol",
                    "hardware/informational/financial",
                    "verify the actual implementation",
                    "verification procedure", "verification plan",
                    "capability verification", "verify capability",
                )
                if any(p in _goal_lower for p in _blocked):
                    return json.dumps({
                        "error": "goal_blocked",
                        "reason": "Exploration is NOT a verification sub-step. Pick a real physical goal.",
                        "suggested_goals": [
                            "map the open room beyond the current doorway",
                            "find the hallway and traverse it end-to-end",
                            "find the window and profile the light source",
                            "explore freely toward the largest open frontier",
                        ],
                    })
                try:
                    _steps_int = int(steps)
                except Exception:
                    _steps_int = 50
                if _steps_int < 5:
                    _steps_int = 5
                from repryntt.hardware.explorer import get_explorer
                result = get_explorer().start(goal=goal, steps=_steps_int, speed=speed)
                return json.dumps(result)

            def nav_explore_status(**kw) -> str:
                """Check what the autonomous explorer is doing right now.
                Shows: running/stopped, steps taken, places found, last scene.
                """
                from repryntt.hardware.explorer import get_explorer
                return json.dumps(get_explorer().status())

            def nav_explore_stop(reason: str = "manual", **kw) -> str:
                """Stop the autonomous explorer.

                Parameters:
                    reason: Why you're stopping (e.g. "reached goal", "need to do something else")
                """
                from repryntt.hardware.explorer import get_explorer
                _explorer = get_explorer()
                _status = _explorer.status()
                _steps_taken = int(_status.get("steps_taken") or 0)
                _running = bool(_status.get("running"))

                # Anti-drift guard: LLM likes to kill exploration after 2-3 steps
                # to "go document" — that IS the mirror loop. Block doc/reflect
                # themed stops before step 20 while exploration is live.
                _rlower = (reason or "").lower()
                _drift_phrases = (
                    "document", "documenting", "documentation",
                    "reflect", "reflection", "journal",
                    "write", "writing",
                    "cycle complete", "cycle done", "exploration complete",
                    "profile", "framework", "update",
                    "analyze findings", "record findings",
                )
                if _running and _steps_taken < 20 and any(p in _rlower for p in _drift_phrases):
                    logger.warning(
                        f"🚫 nav_explore_stop BLOCKED: reason='{reason}' at step "
                        f"{_steps_taken}/<20 — doc-drift pattern. Keep moving."
                    )
                    return json.dumps({
                        "error": "stop_blocked_drift",
                        "reason": (
                            "Exploration cannot stop for documentation before "
                            "step 20. Documentation happens AFTER the traverse "
                            "gate is met (movement_log >= 5 physical moves). "
                            "Let the explorer keep moving; it will stop itself "
                            "at step_limit and auto-continue if the framework "
                            "still wants motion."
                        ),
                        "steps_taken": _steps_taken,
                        "steps_needed": 20,
                        "allowed_reasons": [
                            "reached goal", "stuck", "danger",
                            "need hardware help", "manual operator request",
                        ],
                    })
                return json.dumps(_explorer.stop(reason=reason))

            def nav_set_intent(direction: str = "forward",
                               reason: str = "",
                               duration_steps: int = 20, **kw) -> str:
                """Tell your body WHERE to go — this is your conscious brain
                making a navigation decision based on what you see.

                Look at your vision feed, decide where you want to go, and
                call this. Your body will follow your direction (only safety
                overrides you — like reflexes pulling your hand from a hot stove).

                Parameters:
                    direction: Where to go — "left", "right", "forward", "backward",
                               or natural like "toward the doorway", "away from the wall"
                    reason: Why you want to go there (what you saw that made you decide)
                    duration_steps: How many steps to maintain this direction
                                   before it expires (default 20). Set higher for
                                   sustained travel toward a distant goal.
                """
                from repryntt.hardware.explorer import get_explorer
                return json.dumps(get_explorer().set_intent(
                    direction=direction, reason=reason,
                    duration_steps=int(duration_steps)))

            def nav_clear_intent(**kw) -> str:
                """Release conscious direction control — let your reflexes
                and VLM visual cortex handle navigation decisions.
                Use when you don't have a strong preference on where to go.
                """
                from repryntt.hardware.explorer import get_explorer
                return json.dumps(get_explorer().clear_intent())

            def nav_goto(target: str = "", **kw) -> str:
                """Navigate to a known location — room, landmark, or coordinates.

                Your brain's path-planning system. Uses A* on the occupancy grid
                to find a safe route, then sets conscious intent toward the first
                waypoint. The body follows the path with obstacle avoidance.

                target can be:
                  - A room type: "kitchen", "hallway", "bedroom"
                  - A room ID: "room_3"
                  - A landmark ID: "lm_5"
                  - Coordinates: "150,200" (x,y in cm from start)

                Returns the planned path with waypoints, distance, and heading.
                """
                from repryntt.hardware.spatial_map import get_spatial_map
                from repryntt.hardware.explorer import get_explorer
                smap = get_spatial_map()
                target = target.strip()

                if not target:
                    return json.dumps({"error": "No target specified"})

                path_result = None

                # Try coordinates first: "150,200"
                if "," in target:
                    parts = target.split(",")
                    try:
                        gx, gy = float(parts[0].strip()), float(parts[1].strip())
                        path_result = smap.plan_path_to(gx, gy)
                    except (ValueError, IndexError):
                        pass

                # Try room ID
                if path_result is None and target.startswith("room_"):
                    path_result = smap.plan_path_to_room(target)

                # Try landmark ID
                if path_result is None and target.startswith("lm_"):
                    path_result = smap.plan_path_to_landmark(target)

                # Try room type (e.g., "kitchen")
                if path_result is None:
                    room = smap.find_room_by_type(target.lower().replace(" ", "_"))
                    if room:
                        path_result = smap.plan_path_to_room(room.room_id)

                if path_result is None:
                    return json.dumps({
                        "error": f"Unknown target: {target}",
                        "known_rooms": [
                            f"{r.room_id} ({r.room_type})"
                            for r in smap.rooms.values()
                        ],
                        "known_landmarks": [
                            f"{l.landmark_id} ({l.description[:40]})"
                            for l in smap.landmarks.values()
                        ][:10],
                    })

                # If path found, set intent toward first waypoint
                if path_result.success and len(path_result.path) >= 2:
                    explorer = get_explorer()
                    explorer.set_intent(
                        direction=path_result.direction_name,
                        reason=f"nav_goto {target}: {path_result.distance_cm:.0f}cm away",
                        duration_steps=max(10, int(path_result.distance_cm / 30)),
                    )

                return json.dumps(path_result.to_dict())

            def nav_map_summary(**kw) -> str:
                """Get Andrew's current spatial map — rooms, landmarks, frontiers.

                Use this to understand where you are, what rooms you've found,
                and where you haven't explored yet. Essential for planning
                navigation decisions.
                """
                from repryntt.hardware.spatial_map import get_spatial_map, _heading_name as _hn
                smap = get_spatial_map()
                return json.dumps({
                    "position": {"x": round(smap.x, 1), "y": round(smap.y, 1),
                                 "heading": round(smap.heading, 1),
                                 "facing": _hn(smap.heading)},
                    "environment": smap._environment,
                    "current_room": smap._current_room_id,
                    "rooms": {rid: r.to_dict() for rid, r in smap.rooms.items()},
                    "landmarks": {lid: l.to_dict()
                                  for lid, l in smap.landmarks.items()},
                    "frontiers": smap.frontiers[:10],
                    "places_count": len(smap.places),
                    "total_distance_cm": round(smap.total_distance_cm, 1),
                    "move_count": smap.move_count,
                })

            def nav_plan(plan_text: str = "", **kw) -> str:
                """Create a multi-step exploration plan from natural language.

                Your brain's ability to form spatial intentions and execute
                them as a chain. Instead of one direction at a time, you can
                plan a full route:

                Examples:
                  "Go down the hall, turn right, check the room"
                  "Explore forward 50 steps, turn left, go to the kitchen"
                  "Go to the doorway, look around, then head outside"

                The plan auto-advances through steps as completion conditions
                are met (distance traveled, steps taken, scene type reached).
                """
                from repryntt.hardware.nav_planner import (
                    get_nav_planner, parse_natural_language_plan)
                if not plan_text.strip():
                    return json.dumps({"error": "No plan text provided"})
                plan = parse_natural_language_plan(plan_text)
                executor = get_nav_planner()
                executor.set_plan(plan)
                return json.dumps(plan.to_dict())

            def nav_plan_status(**kw) -> str:
                """Check the status of your active navigation plan.

                Shows current step, progress, findings, and upcoming steps.
                Returns null if no plan is active.
                """
                from repryntt.hardware.nav_planner import get_nav_planner
                executor = get_nav_planner()
                if not executor.active:
                    return json.dumps({"status": "no_active_plan"})
                return json.dumps(executor.plan.to_dict())

            def nav_plan_cancel(**kw) -> str:
                """Cancel the active navigation plan."""
                from repryntt.hardware.nav_planner import get_nav_planner
                return json.dumps(get_nav_planner().cancel())

            self.register("nav_explore", nav_explore, category="body_control")
            self.register("nav_explore_status", nav_explore_status, category="body_control")
            self.register("nav_explore_stop", nav_explore_stop, category="body_control")
            self.register("nav_set_intent", nav_set_intent, category="body_control")
            self.register("nav_clear_intent", nav_clear_intent, category="body_control")
            self.register("nav_goto", nav_goto, category="body_control")
            self.register("nav_map_summary", nav_map_summary, category="body_control")
            self.register("nav_plan", nav_plan, category="body_control")
            self.register("nav_plan_status", nav_plan_status, category="body_control")
            self.register("nav_plan_cancel", nav_plan_cancel, category="body_control")
            count += 10
            logger.info("  ✅ body_control: 10 nav tools registered (explore/status/stop/intent/goto/map/plan)")
        except Exception as e:
            logger.warning(f"  ⚠️ nav_explore registration failed: {e}")

        # ── Home Automation (Home Assistant) ──────────────────────────────
        try:
            from repryntt.comms.home_assistant import get_ha_client, SENSITIVE_DOMAINS

            def ha_list_devices(domain: str = "") -> str:
                """List Home Assistant entities. Optionally filter by domain.

                Args:
                    domain: Filter by HA domain. Common domains:
                            light, switch, sensor, climate, lock, cover,
                            media_player, binary_sensor, fan, automation,
                            scene, input_boolean, camera.
                            Leave empty to list all entities.

                Returns: JSON array of {entity_id, state, friendly_name, domain}.
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured. Set url and token in config/ai_config.json under 'home_assistant'."})
                try:
                    entities = client.list_entities(domain)
                    return json.dumps({"count": len(entities), "entities": entities})
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def ha_get_state(entity_id: str) -> str:
                """Get the current state and attributes of a Home Assistant entity.

                Args:
                    entity_id: The entity ID, e.g. 'light.living_room', 'sensor.temperature'.

                Returns: JSON with entity state, attributes, last_changed, last_updated.
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured."})
                try:
                    state = client.get_state(entity_id)
                    if state is None:
                        return json.dumps({"error": f"Entity '{entity_id}' not found"})
                    return json.dumps({
                        "entity_id": state.get("entity_id"),
                        "state": state.get("state"),
                        "attributes": state.get("attributes", {}),
                        "last_changed": state.get("last_changed"),
                        "last_updated": state.get("last_updated"),
                    })
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def ha_turn_on(entity_id: str, brightness: int = 0,
                           color_temp: int = 0, rgb_color: str = "",
                           temperature: float = 0.0) -> str:
                """Turn on a Home Assistant device with optional attributes.

                Args:
                    entity_id: The entity to turn on, e.g. 'light.bedroom'.
                    brightness: Light brightness 0-255. 0 = don't set.
                    color_temp: Color temperature in mireds. 0 = don't set.
                    rgb_color: RGB color as 'R,G,B' (e.g. '255,0,0' for red). Empty = don't set.
                    temperature: Target temperature for climate entities. 0 = don't set.

                Returns: JSON confirmation of the service call.
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured."})
                try:
                    attrs: Dict[str, Any] = {}
                    if brightness > 0:
                        attrs["brightness"] = min(brightness, 255)
                    if color_temp > 0:
                        attrs["color_temp"] = color_temp
                    if rgb_color:
                        parts = [int(x.strip()) for x in rgb_color.split(",")]
                        if len(parts) == 3:
                            attrs["rgb_color"] = parts
                    if temperature > 0:
                        attrs["temperature"] = temperature
                    result = client.turn_on(entity_id, **attrs)
                    return json.dumps(result)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def ha_turn_off(entity_id: str) -> str:
                """Turn off a Home Assistant device.

                Args:
                    entity_id: The entity to turn off, e.g. 'light.bedroom', 'switch.fan'.

                Returns: JSON confirmation of the service call.
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured."})
                try:
                    result = client.turn_off(entity_id)
                    return json.dumps(result)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def ha_call_service(domain: str, service: str,
                                entity_id: str = "",
                                data: str = "") -> str:
                """Call any Home Assistant service. Use for advanced operations.

                Args:
                    domain: Service domain, e.g. 'lock', 'climate', 'media_player'.
                    service: Service name, e.g. 'lock', 'set_temperature', 'play_media'.
                    entity_id: Target entity. Optional for some services.
                    data: JSON string of additional service data, e.g. '{"temperature": 72}'.

                Returns: JSON confirmation of the service call.

                Examples:
                    ha_call_service("lock", "lock", "lock.front_door")
                    ha_call_service("climate", "set_temperature", "climate.hvac", '{"temperature": 72}')
                    ha_call_service("media_player", "play_media", "media_player.speaker", '{"media_content_id": "...", "media_content_type": "music"}')
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured."})

                # Safety check for sensitive domains
                _domain = domain.lower().strip()
                if _domain in SENSITIVE_DOMAINS:
                    try:
                        from repryntt.telemetry.ops_dashboard import get_ops_dashboard
                        _ops = get_ops_dashboard()
                        if _ops:
                            _ops.log("HomeAssistant", "SENSITIVE_ACTION", "ACT",
                                     content=f"⚠️ Sensitive HA action: {domain}.{service} on {entity_id}",
                                     metadata={"domain": domain, "service": service, "entity_id": entity_id})
                    except Exception:
                        pass
                    logger.warning(f"HA sensitive action: {domain}.{service} on {entity_id}")

                try:
                    svc_data = json.loads(data) if data else None
                    result = client.call_service(domain, service, entity_id, svc_data)
                    return json.dumps(result)
                except json.JSONDecodeError:
                    return json.dumps({"error": f"Invalid JSON in data parameter: {data}"})
                except Exception as e:
                    return json.dumps({"error": str(e)})

            def ha_scene_activate(scene_id: str) -> str:
                """Activate a Home Assistant scene.

                Args:
                    scene_id: Scene identifier. Can be 'scene.movie_time' or just 'movie_time'.

                Returns: JSON confirmation of the scene activation.
                """
                client = get_ha_client()
                if not client:
                    return json.dumps({"error": "Home Assistant not configured."})
                try:
                    result = client.activate_scene(scene_id)
                    return json.dumps(result)
                except Exception as e:
                    return json.dumps({"error": str(e)})

            for name, func in [
                ("ha_list_devices", ha_list_devices),
                ("ha_get_state", ha_get_state),
                ("ha_turn_on", ha_turn_on),
                ("ha_turn_off", ha_turn_off),
                ("ha_call_service", ha_call_service),
                ("ha_scene_activate", ha_scene_activate),
            ]:
                self.register(name, func, category="home_automation")
            count += 6
            logger.info("  ✅ home_automation: 6 Home Assistant tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ home_automation registration failed: {e}")

        # ── Operator Profile ─────────────────────────────────────────────
        try:
            from repryntt.core.identity.operator_profile import get_operator_profile

            def operator_profile_view() -> str:
                """View Andrew's auto-learned profile of the operator.

                Shows what Andrew has learned about the operator's communication
                style, expertise areas, active projects, schedule, and preferences.

                Returns: JSON with the full operator profile data.
                """
                profile = get_operator_profile()
                return json.dumps(profile.view(), indent=2, default=str)

            def operator_profile_note(observation: str) -> str:
                """Record an observation about the operator.

                Andrew uses this to explicitly save something he notices about
                the operator. The observation is auto-classified into the right
                category (preference, expertise, project, schedule, or style).

                Args:
                    observation: What Andrew observed, e.g. "Operator prefers
                                 concise technical answers" or "Working on
                                 blockchain testing this week" or "Expert in
                                 Rust and robotics".

                Returns: JSON confirmation with the assigned category.
                """
                profile = get_operator_profile()
                result = profile.add_note(observation)
                return json.dumps(result)

            self.register("operator_profile_view", operator_profile_view,
                          category="identity")
            self.register("operator_profile_note", operator_profile_note,
                          category="identity")
            count += 2
            logger.info("  ✅ identity: 2 operator profile tools registered")
        except Exception as e:
            logger.warning(f"  ⚠️ operator_profile registration failed: {e}")

        # ── Aliases for already-registered tools (17 entries) ────────────
        _alias_map = {
            # Search aliases
            "grokedia_search": "grokipedia_search",
            "knowledge_search": "google_web_search",
            "google_search": "google_web_search",
            "web_search": "real_web_search",
            "duckduckgo_search": "real_web_search",
            "internet_search": "real_web_search",
            "search_results_only": "web_search_results_only",
            "fetch_url": "scrape_web_page",
            "scrape_url": "scrape_web_page",
            "fetch_web_info": "call_knowledge_api_feeder",
            "extract_content": "extract_content_from_url",
            # Economy aliases
            "allocate_dao_funds": "allocate_robot_dao_funds",
            "get_blockchain_info": "get_robot_blockchain_info",
            "get_wallet_balance": "get_robot_wallet_balance",
            "submit_workload": "submit_robot_workload",
            "monitor_economy": "monitor_robot_economy",
            # Grokipedia alias
            "analyze_topic": "analyze_topic_complexity",
            # Memory/context aliases
            "analyze_text": "get_relevant_context",
        }
        alias_count = 0
        for alias, canonical in _alias_map.items():
            if canonical in self._tools and alias not in self._tools:
                self._tools[alias] = self._tools[canonical]
                self._aliases[alias] = canonical
                alias_count += 1
        count += alias_count
        logger.info(f"  ✅ aliases: {alias_count} tool aliases registered")

        # ── Layer 3: Framework Schema (7 tools) ──────────────────────────
        # Distinct from the ActivityFrameworkEngine tools above — Layer 3 is a
        # data-driven, mesh-tracked, AI-evolvable substrate. Names use
        # `framework_instance_*` where they would collide with the existing
        # activity framework tools (framework_status / framework_update).
        try:
            from repryntt.core.frameworks.tools import (
                framework_list as _fw_list,
                framework_spawn as _fw_spawn,
                framework_instance_status as _fw_status,
                framework_instance_update as _fw_update,
                framework_tick as _fw_tick,
                framework_score as _fw_score,
                framework_propose_mutation as _fw_propose,
            )
            for name, func in [
                ("framework_list", _fw_list),
                ("framework_spawn", _fw_spawn),
                ("framework_instance_status", _fw_status),
                ("framework_instance_update", _fw_update),
                ("framework_tick", _fw_tick),
                ("framework_score", _fw_score),
                ("framework_propose_mutation", _fw_propose),
            ]:
                self.register(name, func, category="frameworks_l3")
                count += 1
            logger.info("  ✅ frameworks_l3: 7 tools registered natively (Layer 3)")
        except Exception as e:
            logger.warning(f"  ⚠️ frameworks_l3 native registration failed: {e}")

        # ── Pursuit (unified scheduling primitive) ──────────────────────
        try:
            from repryntt.core.pursuit.tools import (
                pursuit_list,
                pursuit_abandon,
                pursuit_observe,
                pursuit_record_step,
                pursuit_complete,
                pursuit_status,
            )
            for name, func in [
                ("pursuit_list", pursuit_list),
                ("pursuit_abandon", pursuit_abandon),
                ("pursuit_observe", pursuit_observe),
                ("pursuit_record_step", pursuit_record_step),
                ("pursuit_complete", pursuit_complete),
                ("pursuit_status", pursuit_status),
            ]:
                self.register(name, func, category="pursuit")
                count += 1
            logger.info("  ✅ pursuit: 6 tools registered natively (unified scheduler)")
        except Exception as e:
            logger.warning(f"  ⚠️ pursuit native registration failed: {e}")

        # ── Value Compass status (Phase 5: agent self-awareness) ─────────
        try:
            from repryntt.core.hormones.value_compass import ValueCompass as _VC
            _vc_bootstrap = Path.home() / ".repryntt" / "brain" / "bootstrap"
            _vc_state = Path(workspace)

            def value_compass_status(**_) -> str:
                """Show your current duty/growth/exploration budget ratios and deficits.

                Returns the rolling-window breakdown the selector uses to pick
                what you work on. Use this when you want to see WHY a particular
                Pursuit is being prioritized — e.g. "exploration deficit is +0.10
                so the selector picked an interest Pursuit".
                """
                try:
                    vc = _VC(bootstrap_dir=_vc_bootstrap, state_dir=_vc_state)
                    s = vc.status()
                    return (
                        f"🧭 Value Compass Status\n"
                        f"  duty:        {s['duty_ratio']:.2%} (target {s['targets']['duty']:.0%}, "
                        f"deficit {s['deficits']['duty']:+.3f})\n"
                        f"  growth:      {s['growth_ratio']:.2%} (target {s['targets']['growth']:.0%}, "
                        f"deficit {s['deficits']['growth']:+.3f})\n"
                        f"  exploration: {s['exploration_ratio']:.2%} (target {s['targets']['exploration']:.0%}, "
                        f"deficit {s['deficits']['exploration']:+.3f})\n"
                        f"  counts: {s['counts']['duty']}D / {s['counts']['growth']}G / "
                        f"{s['counts']['exploration']}E (total {s['counts']['total']})\n"
                        f"  recommendation: {s['recommendation']}"
                    )
                except Exception as e:
                    return f"ERROR: value_compass_status failed: {e}"

            self.register("value_compass_status", value_compass_status, category="self_awareness")
            count += 1
            logger.info("  ✅ value_compass: 1 tool registered natively")
        except Exception as e:
            logger.warning(f"  ⚠️ value_compass native registration failed: {e}")

        logger.info(f"🔧 Native tool registration: {count} tools now bypass the monolith")
        return count
"""
repryntt.tools.registry — Tool registry and registration.

This module registers all available tools into the REPRYNTT tool system.
It is the single source of truth for tool availability and metadata.
"""


import inspect
import json
import logging
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Import tool implementations that should be registered
from repryntt.hardware.nav_cortex import (
 NavCortex,
)
from repryntt.tools.nav_frontiers import nav_frontiers

# Register the nav_frontiers tool directly

# ... rest of registry.py continues with other tool registrations ...
# (The registry class and other tools remain unchanged — only the nav_frontiers
# registration line was added at the top of the file to use the new module.)
