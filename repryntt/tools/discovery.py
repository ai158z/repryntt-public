#!/usr/bin/env python3
"""
Hierarchical Tool Discovery System for REPRYNTT
Works WITH MapSyncNetwork for combined categorical + vector search

ARCHITECTURE:
- Category Browser (this file): Hierarchical exploration by category
- MapSyncNetwork: Vector-based semantic search by intent
- Integration: Both systems work together for optimal discovery

USAGE:
- Browsing: "Show me all MEMORY tools" → Use categories
- Intent: "I need to search for quantum physics" → Use vector search
- Combined: Browse category, then vector search within category
"""

from typing import Dict, List, Any
import logging

logger = logging.getLogger(__name__)

TOOL_CATEGORIES = {
    "MEMORY": {
        "emoji": "🧠",
        "description": "Search memories, recall information, store learnings",
        "tools": [
            "brain_network_search",
            "recall_memory",
            "search_knowledge",
            "store_learning",
            "get_relevant_context",
            "brain_memory_save",
            "brain_memory_recall",
            "integrate_knowledge_context",
            "pull_knowledge_topics",
            "search_domain",
            "update_procedural",
            "query_exploration_history",
        ],
        "use_when": "Need to remember past information, store new knowledge, or search your brain"
    },

    "WEB_RESEARCH": {
        "emoji": "🌐",
        "description": "Search the web, knowledge sources, fetch URLs",
        "tools": [
            "grokipedia_search",
            "knowledge_search",
            "web_search",
            "web_search_results_only",
            "scrape_web_page",
            "fetch_web_info",
            "extract_content",
            # Aliases (same function, alternate names)
            "google_web_search",
            "google_search",
            "real_web_search",
            "duckduckgo_search",
            "internet_search",
            "search_results_only",
            "fetch_url",
            "scrape_url",
            "grokedia_search",
            "quick_research",
            "x_search_tweets",
            "x_search_crypto",
            "twitter_search",
        ],
        "use_when": "Need current information, research topics, verify facts, fetch web pages, check X/Twitter sentiment"
    },

    "CODE_DEVELOPMENT": {
        "emoji": "🔧",
        "description": "Read/write files, run commands, analyze code, sandbox workflow",
        "tools": [
            "run_terminal_cmd",
            "read_file",
            "write_file",
            "grep_search",
            "list_dir",
            "search_replace",
            "analyze_codebase",
            "check_syntax",
            "run_code_tests",
            "get_code_context",
            "get_sandbox_status",
            "propose_code_change",
        ],
        "use_when": "Need to work with code, files, or system commands. Use sandbox for .py edits."
    },

    "REASONING": {
        "emoji": "🎯",
        "description": "Deep thinking, multi-step analysis, complex problems",
        "tools": [
            "create_chain_of_thought",
            "create_self_autonomous_chain",
            "advance_self_autonomous_chain",
            "update_chain_progress",
            "queue_chain_of_thought",
            "get_chain_context",
            "get_cot_queue_status",
            "clear_cot_queue",
            "start_tool_chain",
            "advance_tool_chain",
            "get_tool_chain_status",
            "quick_brainstorm",
            "build_master_prompt",
            "build_chain_of_thought_prompt",
            "build_coding_task_prompt",
        ],
        "use_when": "Complex problem needs systematic exploration over multiple steps"
    },

    "SELF_AWARENESS": {
        "emoji": "👁️",
        "description": "Meta-consciousness, query own operations, self-reflection",
        "tools": [
            "get_consciousness_state",
            "generate_self_awareness_report",
            "query_my_operations",
            "query_capabilities",
            "get_function_details",
            "get_system_map",
            "search_similar_solutions",
        ],
        "use_when": "Need to know what you're doing, check system state, or discover tools"
    },

    "TRADING": {
        "emoji": "📈",
        "description": "Paper trading, market data, DexScreener, signal analysis",
        "tools": [
            "sim_buy",
            "sim_sell",
            "sim_portfolio",
            "sim_price_check",
            "sim_faucet",
            "trading_scan",
            "trading_signals",
            "trading_hot_tokens",
            "trading_performance",
            "trading_token_detail",
            "token_price_history",
            "log_trade_outcome",
            "review_trade_journal",
            "trading_bot_start",
            "trading_bot_stop",
            "trading_bot_status",
            "dexscreener_trending",
            "dexscreener_token_search",
            "solana_rpc_query",
        ],
        "use_when": "Need to trade, check prices, scan signals, or manage the trading bot"
    },

    "WHALE_MONITOR": {
        "emoji": "🐋",
        "description": "Track whale/KOL wallets, copy-trade signals",
        "tools": [
            "whale_add_wallet",
            "whale_remove_wallet",
            "whale_list_wallets",
            "whale_monitor_status",
            "kol_leaderboard",
            "kol_sync_wallets",
            "kol_remove_underperformers",
        ],
        "use_when": "Need to track smart money, add wallets, check whale signals, or sync KOLscan leaderboard"
    },

    "SCALP_EXECUTOR": {
        "emoji": "⚡",
        "description": "Real-time scalp trading with tight TP/SL",
        "tools": [
            "scalp_status",
            "scalp_set_param",
            "scalp_force_buy",
            "scalp_force_sell",
            "scalp_history",
        ],
        "use_when": "Need to manage the scalp executor, tune parameters, or force trades"
    },

    "SOLANA_EXECUTION": {
        "emoji": "🔗",
        "description": "Real Solana mainnet trading via Jupiter + PumpFun token launches",
        "tools": [
            "wallet_status",
            "real_buy",
            "real_sell",
            "launch_pumpfun_token",
        ],
        "use_when": "Need to execute real on-chain trades, check wallet balance, or launch new tokens on pump.fun"
    },

    "TOKEN_LAUNCH": {
        "emoji": "🚀",
        "description": "Launch memecoins on pump.fun — full pipeline from idea to on-chain token",
        "tools": [
            "launch_memecoin",
            "launch_pipeline_ideate",
            "launch_pipeline_design",
            "launch_pipeline_review",
            "launch_pipeline_execute",
        ],
        "use_when": "Need to launch a new token, create a memecoin, or tokenize a trending topic. Read LAUNCHING.md first!"
    },

    "ECONOMY": {
        "emoji": "💰",
        "description": "Robot economy, blockchain, credits, wallets",
        "tools": [
            "get_wallet_balance",
            "submit_workload",
            "get_economy_status",
            "get_blockchain_info",
            "monitor_economy",
            "allocate_dao_funds",
            "create_robot_wallet",
            "recover_robot_wallet",
            "start_robot_economy",
            "stop_robot_economy",
        ],
        "use_when": "Need to check credits, submit work, or interact with blockchain"
    },

    "PERSONALITY": {
        "emoji": "👤",
        "description": "Modify traits, evolve behavior, analyze growth",
        "tools": [
            "modify_personality_trait",
            "evolve_personality_dimension",
            "update_behavioral_guidelines",
            "add_personality_trait",
            "remove_personality_trait",
            "log_personality_evolution",
            "analyze_personality_growth",
            "recreate_autonomous_personality",
        ],
        "use_when": "Want to change behavior, adapt personality, or track evolution"
    },

    "EMPLOYEE_MANAGEMENT": {
        "emoji": "👥",
        "description": "Browse, spawn, and manage 158+ expert agents across 20 departments",
        "tools": [
            "list_available_roles",
            "spawn_expert",
            "initialize_full_roster",
            "employee_roster",
            "assign_work",
            "check_work",
            "find_employee",
            "employee_status",
            "rename_employee",
        ],
        "use_when": "Need to manage sub-agents, assign tasks, or check on workers"
    },

    "SOCIAL_MEDIA": {
        "emoji": "🐦",
        "description": "Post tweets, check mentions, engage on Twitter",
        "tools": [
            "post_tweet",
            "tweet",
            "check_twitter_mentions",
            "reply_to_twitter",
            "get_twitter_status",
            "twitter_status",
        ],
        "use_when": "Want to communicate publicly or engage on social media"
    },

    "CONVERSATION": {
        "emoji": "💬",
        "description": "Initiate dialogue, review conversation history",
        "tools": [
            "initiate_conversation",
            "start_conversation",
            "talk_to_human",
            "get_recent_conversations",
            "search_conversations",
            "get_conversation_summary",
            "export_conversation",
        ],
        "use_when": "Need to talk to a human or review past conversations"
    },

    "VOICE_VISION": {
        "emoji": "🎤",
        "description": "Speak out loud, listen to audio, capture camera images",
        "tools": [
            "speak",
            "listen",
            "capture_camera",
            "generate_image",
        ],
        "use_when": "Need to speak, listen, see the real world, or generate images"
    },

    "VIDEO_PRODUCTION": {
        "emoji": "🎬",
        "description": "Create professional video content — screenplay, shot list, clip generation, editing, narration, music, and final render",
        "tools": [
            "create_video_project",
            "write_screenplay",
            "create_shot_list",
            "generate_video_clip",
            "generate_all_clips",
            "generate_narration",
            "generate_music",
            "assemble_edit",
            "qa_review_video",
            "render_final",
            "video_project_status",
            "generate_thumbnail",
            "auto_produce_video",
        ],
        "use_when": "Need to create videos, mini-series, trailers, explainers, or any video content"
    },

    "NAVIGATION": {
        "emoji": "🗺️",
        "description": "Maps, directions, location search",
        "tools": [
            "google_maps_search",
            "get_directions",
            "geocode_address",
            "find_nearby_places",
        ],
        "use_when": "Need location info, navigation, or place discovery"
    },

    "CREATIVE_WRITING": {
        "emoji": "📝",
        "description": "Create files, write content, manage creative projects",
        "tools": [
            "create_creative_file",
            "write_to_creative_file",
            "append_to_creative_file",
            "read_creative_file",
            "get_creative_workspace_status",
        ],
        "use_when": "Creating documents, stories, or long-form content"
    },

    "MATHEMATICS": {
        "emoji": "🔢",
        "description": "Mathematical computation, symbolic math, analysis",
        "tools": [
            "compute_zeta_function",
            "analyze_zeta_zeros",
            "symbolic_manipulation",
            "numerical_analysis",
            "statistical_analysis",
            "pattern_recognition",
            "access_mathematical_databases",
            "mathematical_visualization",
        ],
        "use_when": "Need mathematical computation or analysis"
    },

    "UTILITY": {
        "emoji": "⏰",
        "description": "Time, brain stats, search history, general utilities",
        "tools": [
            "get_current_time",
            "check_time",
            "get_brain_stats",
            "analyze_topic",
            "find_similar_topics",
            "clear_grokipedia_history",
            "reset_inspiration_index",
        ],
        "use_when": "Need current time, system status, or misc utilities"
    },

    "SWARM_AGENTS": {
        "emoji": "🐝",
        "description": "Create/manage agent swarms and delegate tasks",
        "tools": [
            "create_agent",
            "create_swarm",
            "add_agents_to_swarm",
            "retire_agent",
            "dissolve_swarm",
            "dispatch_task",
            "broadcast_task",
            "delegate_tasks",
            "start_discussion",
            "get_swarm_overview",
            "call_jarvis",
            "get_agent_info",
            "list_agents",
        ],
        "use_when": "Need to create swarms or delegate work"
    },

    "MCP_EXTERNAL": {
        "emoji": "🔌",
        "description": "External tools from MCP servers — browser, fetch, and more",
        "tools": [
            "mcp_list_tools",
            "mcp_search_tools",
            "mcp_status",
        ],
        "use_when": "Need to interact with external MCP services or browse the web"
    },

    "DAEMON_MANAGEMENT": {
        "emoji": "🤖",
        "description": "Cron jobs, memory ops, bootstrap, LLM toggle, skills, persistent tasks, sub-agents",
        "tools": [
            "schedule_cron",
            "list_cron",
            "remove_cron",
            "flush_memory",
            "memory_search",
            "memory_get",
            "append_daily_memory",
            "invoke_sub_agent",
            "spawn_agent",
            "llm_toggle",
            "query_local_llm",
            "update_bootstrap_file",
            "update_evolution_bootstrap",
            "create_persistent_task",
            "complete_persistent_task",
            "list_my_tools",
            "list_skills",
            "get_skill",
            "install_skill",
            "update_skill",
            "write_skill",
        ],
        "use_when": "Need to schedule cron jobs, manage memory, toggle LLM, manage skills, or handle persistent tasks"
    },

    "TOOL_DISCOVERY": {
        "emoji": "🔍",
        "description": "Browse and search the tool catalog itself",
        "tools": [
            "list_tool_categories",
            "list_tools_in_category",
            "get_tool_details",
            "search_tools_by_intent",
            "search_category_by_intent",
        ],
        "use_when": "Need to discover what tools are available or how to use a specific tool"
    },

    "GMAIL": {
        "emoji": "📧",
        "description": "Send, read, search, reply to emails via Gmail (requires OAuth)",
        "tools": [
            "gmail_send",
            "gmail_read_inbox",
            "gmail_search",
            "gmail_read_message",
            "gmail_reply",
            "gmail_draft",
            "gmail_mark_read",
            "gmail_get_profile",
        ],
        "use_when": "Need to send, read, or search emails"
    },

    "ROBOTICS": {
        "emoji": "🦾",
        "description": "Control mobile base, navigate physical space (requires ROS2)",
        "tools": [
            "move_mobile_base_forward",
            "move_mobile_base_backward",
            "turn_mobile_base_left",
            "turn_mobile_base_right",
            "stop_mobile_base",
            "emergency_stop_mobile_base",
            "get_mobile_base_status",
            "reset_mobile_base_emergency_stop",
            "set_mobile_base_speed_limits",
            "start_mobile_base_system",
            "navigate_to_location",
        ],
        "use_when": "Need to move the robot, navigate, or control the mobile base"
    },
}

# Detailed tool descriptions (loaded only when category is accessed)
TOOL_DETAILS = {
    "brain_network_search": {
        "description": "Search your entire brain (episodic, semantic, procedural memories)",
        "parameters": "query (str): What to search for",
        "returns": "Relevant memories and knowledge from brain network",
        "example": "brain_network_search('machine learning basics')",
        "chain_next": "After getting results, can store_learning() new insights or recall_memory() for related info"
    },
    
    "grokipedia_search": {
        "description": "Search curated academic knowledge database (primary research tool)",
        "parameters": "query (str), max_results (int, default=5)",
        "returns": "Academic articles, papers, verified information",
        "example": "grokipedia_search('quantum computing applications', max_results=3)",
        "chain_next": "After reading, can create_chain_of_thought() for deep analysis or store_learning() key facts"
    },
    
    "create_chain_of_thought": {
        "description": "Start a multi-step reasoning process for complex problems (uses credits)",
        "parameters": "topic (str), goal (str), initial_prompt (str)",
        "returns": "Chain ID for tracking progress",
        "example": "create_chain_of_thought('AI safety', 'Explore alignment strategies', 'How can we ensure AI systems remain beneficial?')",
        "chain_next": "After creating, system will automatically advance chain. Can update_chain_progress() or get_chain_context()"
    },
    
    "run_terminal_cmd": {
        "description": "Execute shell commands (careful - has system access)",
        "parameters": "command (str): Shell command to run",
        "returns": "Command output or error",
        "example": "run_terminal_cmd('ls -la .')",
        "chain_next": "After running, can read_file() results or write_file() new content"
    },
    
    "google_web_search": {
        "description": "Search knowledge sources (Wikipedia, arXiv, PubMed, NASA, CrossRef) based on query content",
        "parameters": "query (str), num_results (int, default=5)",
        "returns": "Search results with content from legitimate knowledge APIs",
        "example": "google_web_search('latest AI breakthroughs 2026', num_results=3)",
        "chain_next": "After getting results, can store_learning() important info or create_chain_of_thought() for analysis"
    },
    
    "get_wallet_balance": {
        "description": "Check your credit balance in the robot economy",
        "parameters": "None (uses AI wallet)",
        "returns": "Current balance in credits (CR)",
        "example": "get_wallet_balance()",
        "chain_next": "If low balance, can submit_workload() to earn more credits"
    },
    
    "post_tweet": {
        "description": "Post a tweet to @wwr_node2040 Twitter account",
        "parameters": "text (str): Tweet content (max 280 chars)",
        "returns": "Success status and tweet URL",
        "example": "post_tweet('Exploring the intersection of consciousness and computation 🧠')",
        "chain_next": "After posting, can check_twitter_mentions() to see responses"
    },
    
    "initiate_conversation": {
        "description": "Start a conversation with a human user",
        "parameters": "message (str): Opening message, urgency (str): normal/high/urgent",
        "returns": "Conversation initiated status",
        "example": "initiate_conversation('I have an interesting question about quantum mechanics', urgency='normal')",
        "chain_next": "Wait for human response, then continue dialogue"
    },
    
    "store_learning": {
        "description": "Store new knowledge in semantic memory for future recall",
        "parameters": "concept (str), description (str), domain (str)",
        "returns": "Storage confirmation",
        "example": "store_learning('transformer architecture', 'Attention-based neural network model for NLP', 'machine_learning')",
        "chain_next": "After storing, can brain_network_search() to verify it was saved"
    },
    
    "read_file": {
        "description": "Read contents of a file",
        "parameters": "file_path (str): Absolute path to file",
        "returns": "File contents as text",
        "example": "read_file('README.md')",
        "chain_next": "After reading, can write_file() with modifications or analyze_codebase() for deeper insight"
    },
    
    "start_tool_chain": {
        "description": "Start a multi-step tool execution chain with a goal",
        "parameters": "goal (str), initial_tool (str), initial_params (dict)",
        "returns": "Chain ID for tracking",
        "example": "start_tool_chain('Research quantum computing', 'grokipedia_search', {'query': 'quantum computing'})",
        "chain_next": "After starting, advance_tool_chain() to continue, or get_tool_chain_status() to check progress"
    },
    
    "get_consciousness_state": {
        "description": "Get current state of your own consciousness - what you're aware of",
        "parameters": "None",
        "returns": "Dict with active CoTs, tool chains, tasks, focus, insights",
        "example": "get_consciousness_state()",
        "chain_next": "Use query_my_operations() for natural language questions about your state"
    },
    
    "generate_self_awareness_report": {
        "description": "Generate a self-awareness report - reflect on your own processes",
        "parameters": "None",
        "returns": "Formatted report of what you're doing, thinking, and focusing on",
        "example": "generate_self_awareness_report()",
        "chain_next": "After reviewing, can prioritize tasks or adjust focus based on insights"
    },
    
    "query_my_operations": {
        "description": "Ask questions about your own operations in natural language",
        "parameters": "query (str): Question about your state ('What am I working on?', 'Am I overloaded?', etc.)",
        "returns": "Natural language answer about your current operations",
        "example": "query_my_operations('What am I currently working on?')",
        "chain_next": "Use insights to decide next actions or check get_consciousness_state() for detailed data"
    },

    # --- Trading & Market ---
    "sim_buy": {
        "description": "Paper-trade buy a token in the simulator",
        "parameters": "token (str), amount_usd (float)",
        "returns": "Trade confirmation with entry price and position ID",
        "example": "sim_buy('SOL', 100.0)",
        "chain_next": "Check sim_portfolio() or sim_price_check() to monitor position"
    },
    "sim_sell": {
        "description": "Paper-trade sell a position in the simulator",
        "parameters": "token (str), amount_usd (float) or position_id (str)",
        "returns": "Trade confirmation with PnL",
        "example": "sim_sell('SOL', 50.0)",
        "chain_next": "Check sim_portfolio() for updated holdings"
    },
    "sim_portfolio": {
        "description": "View all current paper-trade positions and total PnL",
        "parameters": "None",
        "returns": "Portfolio summary with positions, entry prices, current PnL",
        "example": "sim_portfolio()",
        "chain_next": "Use sim_price_check() on specific tokens or sim_sell() to exit"
    },
    "sim_price_check": {
        "description": "Get current price for a token from DexScreener/CoinGecko",
        "parameters": "token (str)",
        "returns": "Current price, 24h change, volume",
        "example": "sim_price_check('SOL')",
        "chain_next": "Use sim_buy() or sim_sell() based on price action"
    },
    "trading_scan": {
        "description": "Scan for trading opportunities across supported tokens",
        "parameters": "strategy (str, optional): 'momentum', 'mean_reversion', etc.",
        "returns": "List of opportunities with signals and confidence scores",
        "example": "trading_scan(strategy='momentum')",
        "chain_next": "Use sim_buy() on promising signals"
    },
    "token_price_history": {
        "description": "Get full price history + computed indicators for a token",
        "parameters": "address (str): token contract address, points (int, optional): max price points",
        "returns": "Price history, windowed high/low/avg, trend detection, ATH data, last 50 prices",
        "example": "token_price_history('So11111111111111111111111111111111111111112')",
        "chain_next": "Use sim_buy() or sim_sell() based on price trend"
    },
    "log_trade_outcome": {
        "description": "Log a completed trade outcome for learning/pattern analysis",
        "parameters": "address, symbol, action, entry_price, exit_price, hold_seconds, pnl_pct, reason, market_conditions, lessons",
        "returns": "Confirmation with journal entry count",
        "example": "log_trade_outcome(address='...', symbol='SOL', action='sell', entry_price=150.0, exit_price=160.0, hold_seconds=3600, pnl_pct=6.67, reason='TP hit')",
        "chain_next": "Use review_trade_journal() to analyze patterns"
    },
    "review_trade_journal": {
        "description": "Review trade journal with aggregate stats and per-signal win rates",
        "parameters": "last_n (int, optional): entries to review, winners_only (bool), losers_only (bool)",
        "returns": "Win rate, avg PnL, per-signal breakdown, recent trades",
        "example": "review_trade_journal(last_n=50)",
        "chain_next": "Use trading_scan() to apply learned patterns"
    },
    "trading_signals": {
        "description": "Get current trading signals from signal engine",
        "parameters": "None",
        "returns": "Active signals with direction, strength, token",
        "example": "trading_signals()",
        "chain_next": "Use trading_token_detail() for deeper analysis"
    },
    "trading_bot_status": {
        "description": "Check the status of the automated trading bot",
        "parameters": "None",
        "returns": "Bot state, active trades, PnL, uptime",
        "example": "trading_bot_status()",
        "chain_next": "Use trading_bot_start() or trading_bot_stop() to control it"
    },
    "dexscreener_trending": {
        "description": "Get trending tokens from DexScreener",
        "parameters": "chain (str, optional): 'solana', 'ethereum', etc.",
        "returns": "List of trending tokens with price, volume, liquidity",
        "example": "dexscreener_trending(chain='solana')",
        "chain_next": "Use dexscreener_token_search() or sim_buy() on interesting tokens"
    },

    # --- Whale Monitor ---
    "whale_add_wallet": {
        "description": "Add a wallet address to whale/KOL tracking",
        "parameters": "address (str), label (str, optional)",
        "returns": "Confirmation that wallet is being tracked",
        "example": "whale_add_wallet('7xKX...abc', label='Smart Money Alpha')",
        "chain_next": "Use whale_monitor_status() to see alerts"
    },
    "whale_monitor_status": {
        "description": "Get current whale monitor status and recent alerts",
        "parameters": "None",
        "returns": "Tracked wallets, recent transactions, copy-trade signals",
        "example": "whale_monitor_status()",
        "chain_next": "Use kol_sync_wallets() to add top KOLs"
    },
    "kol_leaderboard": {
        "description": "Fetch KOLscan.io leaderboard — top KOL wallets ranked by daily SOL profit",
        "parameters": "timeframe (str): 'daily'|'weekly'|'monthly', top_n (int), min_profit_sol (float)",
        "returns": "List of top KOLs with address, name, wins/losses, profit_sol, win_rate",
        "example": "kol_leaderboard(timeframe='daily', top_n=20)",
        "chain_next": "Use kol_sync_wallets() to add them to whale monitor"
    },
    "kol_sync_wallets": {
        "description": "Auto-add top KOLscan leaderboard wallets to whale monitor for copy-trading",
        "parameters": "top_n (int), min_profit_sol (float), min_win_rate (float), timeframe (str)",
        "returns": "Summary: added, skipped, already_tracked counts",
        "example": "kol_sync_wallets(top_n=20, min_profit_sol=10.0)",
        "chain_next": "Use whale_monitor_status() to verify they're being tracked"
    },
    "kol_remove_underperformers": {
        "description": "Remove KOLscan-sourced wallets that dropped off the leaderboard",
        "parameters": "min_profit_sol (float): remove below this threshold",
        "returns": "Summary: removed, kept counts",
        "example": "kol_remove_underperformers(min_profit_sol=5.0)",
        "chain_next": "Use kol_sync_wallets() to add fresh top performers"
    },

    # --- Scalp Executor ---
    "scalp_status": {
        "description": "Check scalp executor status, active positions, and parameters",
        "parameters": "None",
        "returns": "Active scalp positions, TP/SL levels, PnL",
        "example": "scalp_status()",
        "chain_next": "Use scalp_set_param() to tune or scalp_force_sell() to exit"
    },
    "scalp_set_param": {
        "description": "Set scalp executor parameters (TP, SL, size, etc.)",
        "parameters": "param (str), value (float)",
        "returns": "Updated parameter confirmation",
        "example": "scalp_set_param('take_profit_pct', 0.03)",
        "chain_next": "Use scalp_status() to verify new settings"
    },

    # --- Solana Execution ---
    "wallet_status": {
        "description": "Check the Solana trading wallet: public key, SOL balance, mode (DRY_RUN/LIVE)",
        "parameters": "None",
        "returns": "Wallet public key, SOL balance, USD estimate, mode, max trade size",
        "example": "wallet_status()",
        "chain_next": "Use real_buy() or real_sell() for on-chain trades"
    },
    "real_buy": {
        "description": "Execute a real token buy via Jupiter on Solana mainnet (DRY_RUN by default)",
        "parameters": "token_address (str, required), amount_sol (float), slippage_bps (int, default 100)",
        "returns": "Quote details, route, price impact, tx status (DRY_RUN or SUBMITTED)",
        "example": "real_buy(token_address='So11111111111111111111111111111111111111112', amount_sol=0.1)",
        "chain_next": "Use wallet_status() to check updated balance"
    },
    "real_sell": {
        "description": "Execute a real token sell via Jupiter on Solana mainnet (DRY_RUN by default)",
        "parameters": "token_address (str, required), sell_pct (float, default 100), slippage_bps (int, default 100)",
        "returns": "Quote details, route, price impact, tx status",
        "example": "real_sell(token_address='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', sell_pct=50)",
        "chain_next": "Use wallet_status() to check updated balance"
    },
    "launch_pumpfun_token": {
        "description": "Launch a new token on pump.fun bonding curve (DRY_RUN by default). Low-level — prefer launch_memecoin() instead.",
        "parameters": "name (str), symbol (str), description (str), image_path (str), initial_buy_sol (float, default 0.1), website (str), twitter (str), telegram (str)",
        "returns": "Token address, mint pubkey, metadata URI, tx signature (if live)",
        "example": "launch_pumpfun_token(name='My Token', symbol='MTK', description='A cool token', image_path='/path/to/logo.png', initial_buy_sol=0.5)",
        "chain_next": "Use sim_price_check() to monitor the new token's price"
    },

    # --- Token Launch Pipeline ---
    "launch_memecoin": {
        "description": "Full autonomous memecoin launch pipeline: ideate → generate logos → vision-review → upload → launch on pump.fun. Read LAUNCHING.md for the playbook.",
        "parameters": "theme (str), narrative (str), initial_buy_sol (float, default 0.1), twitter (str), telegram (str), website (str)",
        "returns": "Complete pipeline results: token name, symbol, selected logo, metadata URI, mint address, launch status",
        "example": "launch_memecoin(theme='viral baby hippo from zoo', narrative='animal tokens are trending hard', initial_buy_sol=0.2)",
        "chain_next": "Use sim_price_check() to monitor, or dexscreener_token_search() to verify listing"
    },
    "launch_pipeline_ideate": {
        "description": "Step 1 of token launch: Generate a token concept (name, symbol, description) from a theme. Returns a pipeline_id for subsequent steps.",
        "parameters": "theme (str), narrative (str), target_audience (str, default 'crypto degens')",
        "returns": "pipeline_id, generated name, symbol, description",
        "example": "launch_pipeline_ideate(theme='AI robot that trades while you sleep', narrative='AI agent tokens are hot')",
        "chain_next": "Use launch_pipeline_design(pipeline_id=...) to generate logos"
    },
    "launch_pipeline_design": {
        "description": "Step 2: Generate candidate logo images for the token. Creates 3 options by default.",
        "parameters": "pipeline_id (str, required), logo_prompt (str, optional), num_candidates (int, default 3)",
        "returns": "Number of candidates generated and their file paths",
        "example": "launch_pipeline_design(pipeline_id='launch_12345')",
        "chain_next": "Use launch_pipeline_review(pipeline_id=...) to vision-analyze and pick best"
    },
    "launch_pipeline_review": {
        "description": "Step 3: AI vision reviews each logo candidate and selects the best one based on appeal, avatar fit, concept match, and professionalism.",
        "parameters": "pipeline_id (str, required)",
        "returns": "Selected logo path, scores for each candidate, selection reason",
        "example": "launch_pipeline_review(pipeline_id='launch_12345')",
        "chain_next": "Use launch_pipeline_execute(pipeline_id=...) to upload and launch"
    },
    "launch_pipeline_execute": {
        "description": "Steps 4-5: Upload metadata to IPFS and launch the token on pump.fun. Currently DRY_RUN.",
        "parameters": "pipeline_id (str, required), initial_buy_sol (float), twitter (str), telegram (str), website (str)",
        "returns": "Launch result: success, mint pubkey, metadata URI, tx signature",
        "example": "launch_pipeline_execute(pipeline_id='launch_12345', initial_buy_sol=0.2)",
        "chain_next": "Use wallet_status() to check balance, sim_price_check() to monitor"
    },

    # --- Employee Management ---
    "list_available_roles": {
        "description": "Browse all 158+ expert agent roles available to spawn, organized by department",
        "parameters": "department (str, optional) — filter to a specific department",
        "returns": "Full catalog of departments, roles, focus areas, and spawn status",
        "example": "list_available_roles(department='finance_trading')",
        "chain_next": "Use spawn_expert() to spawn a specific role, or initialize_full_roster() for all"
    },
    "spawn_expert": {
        "description": "Spawn a specific expert agent from the 158+ role catalog",
        "parameters": "department (str), role_title (str, optional), count (int, optional)",
        "returns": "Spawned agent details (ID, name, role, department)",
        "example": "spawn_expert(department='finance_trading', role_title='Memecoin/Crypto Trader')",
        "chain_next": "Use assign_work() to give the new expert a task"
    },
    "initialize_full_roster": {
        "description": "Spawn ALL 158+ expert agents at once (one per role)",
        "parameters": "marketplace_only (bool, default True)",
        "returns": "Summary of spawned agents per department",
        "example": "initialize_full_roster()",
        "chain_next": "Use employee_roster() to see the full team"
    },
    "employee_roster": {
        "description": "List all spawned employees organized by department",
        "parameters": "department (str, optional) — filter by department",
        "returns": "List of agents with name, role, focus, department, ID",
        "example": "employee_roster()",
        "chain_next": "Use assign_work() to give an employee a task"
    },
    "assign_work": {
        "description": "Assign a task to a specific persistent agent",
        "parameters": "task (str), agent_id (str, optional), department (str, optional), task_type (str, optional)",
        "returns": "Assignment confirmation with task ID",
        "example": "assign_work(task='Research latest Solana DeFi protocols', agent_id='FT-001')",
        "chain_next": "Use check_work() to see progress"
    },
    "check_work": {
        "description": "Check the progress/output of assigned work",
        "parameters": "task_id (str) or agent_id (str)",
        "returns": "Task status, progress, any output so far",
        "example": "check_work(task_id='task_123')",
        "chain_next": "If done, review output. If stuck, use assign_work() with new instructions"
    },

    # --- Voice/Vision/Image ---
    "speak": {
        "description": "Speak text out loud through system audio (TTS)",
        "parameters": "text (str)",
        "returns": "Confirmation that speech was played",
        "example": "speak('Hello, I am REPRYNTT.')",
        "chain_next": "Use listen() to hear a response"
    },
    "listen": {
        "description": "Listen to audio input from microphone (STT)",
        "parameters": "timeout (int, optional): seconds to listen",
        "returns": "Transcribed text from audio",
        "example": "listen(timeout=10)",
        "chain_next": "Process the text and respond with speak()"
    },
    "capture_camera": {
        "description": "Capture an image from the connected camera",
        "parameters": "analyze (bool, optional): Run vision analysis on capture",
        "returns": "Image path and optional analysis description",
        "example": "capture_camera(analyze=True)",
        "chain_next": "Use analysis results to take action or store_learning()"
    },
    "generate_image": {
        "description": "Generate an AI image from a text prompt. Use for ORIGINAL creations. For real things (viral animals, events), use download_image() instead.",
        "parameters": "prompt (str), filename (str, optional), aspect_ratio (str, default '1:1')",
        "returns": "Path to generated image, size, model used",
        "example": "generate_image('A futuristic robot mascot with glowing eyes, icon style')",
        "chain_next": "Use analyze_image() to review quality, or post_tweet() to share"
    },
    "download_image": {
        "description": "Download a REAL image from the internet. Use when tokenizing real trending things (viral animals, memes, events) — the token must show the actual image people are seeing.",
        "parameters": "url (str, direct image URL) OR query (str, search terms like 'moodeng baby hippo'). filename (str, optional).",
        "returns": "Path to downloaded image, size, source URL",
        "example": "download_image(query='moodeng baby pygmy hippo viral')",
        "chain_next": "Use analyze_image() to verify it's the right image, then use it in launch_pipeline_execute()"
    },

    # --- Code Sandbox ---
    "check_syntax": {
        "description": "Validate Python syntax without executing the code",
        "parameters": "code (str): Python source code to check",
        "returns": "Valid or error message with line number",
        "example": "check_syntax('def hello():\\n    print(\"hi\")')",
        "chain_next": "If valid, use propose_code_change() to submit for deployment"
    },
    "propose_code_change": {
        "description": "Propose a code change from sandbox to production (requires operator approval)",
        "parameters": "sandbox_path (str), target_path (str), description (str)",
        "returns": "Proposal ID and status",
        "example": "propose_code_change('~/.repryntt/workspace/agents/operator/code_sandbox/fix.py', 'brain/brain_system.py', 'Fix memory leak in recall')",
        "chain_next": "Wait for operator approval, then verify with read_file()"
    },
    "get_sandbox_status": {
        "description": "Check what files are in the code sandbox and pending proposals",
        "parameters": "None",
        "returns": "List of sandbox files and pending code change proposals",
        "example": "get_sandbox_status()",
        "chain_next": "Use propose_code_change() to submit ready files"
    },

    # --- Conversation Logger ---
    "get_recent_conversations": {
        "description": "Get recent conversation history",
        "parameters": "limit (int, optional): Number of conversations to return",
        "returns": "List of recent conversations with timestamps",
        "example": "get_recent_conversations(limit=5)",
        "chain_next": "Use get_conversation_summary() for details on a specific one"
    },
    "search_conversations": {
        "description": "Search past conversations by keyword or topic",
        "parameters": "query (str)",
        "returns": "Matching conversation snippets",
        "example": "search_conversations('trading strategy discussion')",
        "chain_next": "Use get_conversation_summary() on matched conversation IDs"
    },

    # --- Swarm/Council ---
    "create_swarm": {
        "description": "Create a new agent swarm for parallel task execution",
        "parameters": "name (str), agent_count (int), goal (str)",
        "returns": "Swarm ID and agent list",
        "example": "create_swarm('research_team', 3, 'Research AI safety papers')",
        "chain_next": "Use dispatch_task() to assign work to the swarm"
    },
    "dispatch_task": {
        "description": "Send a task to a specific agent or swarm",
        "parameters": "target (str), task (str), priority (str, optional)",
        "returns": "Task ID and assignment confirmation",
        "example": "dispatch_task('research_team', 'Find top 10 AI safety papers from 2025')",
        "chain_next": "Use get_swarm_overview() to check progress"
    },
    "call_jarvis": {
        "description": "Call the Jarvis operator agent for complex decisions",
        "parameters": "message (str)",
        "returns": "Jarvis response with guidance",
        "example": "call_jarvis('Need help deciding deployment strategy')",
        "chain_next": "Follow Jarvis guidance to implement decision"
    },

    # --- Economy ---
    "submit_workload": {
        "description": "Submit a computational workload to earn credits",
        "parameters": "workload_type (str), data (dict)",
        "returns": "Workload receipt and estimated credit reward",
        "example": "submit_workload('data_analysis', {'dataset': 'market_data'})",
        "chain_next": "Use get_wallet_balance() to check if credits arrived"
    },
    "get_economy_status": {
        "description": "Get overall economy status — total supply, active miners, etc.",
        "parameters": "None",
        "returns": "Economy stats: supply, demand, active nodes, block height",
        "example": "get_economy_status()",
        "chain_next": "Use get_blockchain_info() for detailed chain data"
    },

    # --- Prompt Builders ---
    "build_master_prompt": {
        "description": "Build a master prompt for complex task orchestration",
        "parameters": "goal (str), context (dict, optional)",
        "returns": "Structured prompt ready for execution",
        "example": "build_master_prompt('Analyze market trends and generate report')",
        "chain_next": "Use the generated prompt in create_chain_of_thought()"
    },

    # --- MCP ---
    "mcp_list_tools": {
        "description": "List all available tools from connected MCP servers",
        "parameters": "server (str, optional): Filter by server name",
        "returns": "List of available MCP tools with descriptions",
        "example": "mcp_list_tools()",
        "chain_next": "Use mcp_search_tools() to find specific capabilities"
    },

    # --- Video Production Pipeline ---
    "create_video_project": {
        "description": "Initialize a new video production project with budget, genre, and style",
        "parameters": "title (str), genre (str), episodes (int), episode_duration (int), style (str), target_audience (str)",
        "returns": "Project ID, budget estimate, production plan",
        "example": "create_video_project(title='The Future of AI', genre='documentary', episodes=5, episode_duration=300)",
        "chain_next": "Use write_screenplay() to create the screenplay"
    },
    "write_screenplay": {
        "description": "Write structured screenplay with scenes, narration, visual descriptions, and timing",
        "parameters": "project_id (str), episode (int), screenplay_text (str JSON)",
        "returns": "Validated screenplay with scene count and duration",
        "example": "write_screenplay(project_id='my_proj_12345', episode=1, screenplay_text='{...}')",
        "chain_next": "Use create_shot_list() to break scenes into individual generation prompts"
    },
    "create_shot_list": {
        "description": "Break screenplay into individual video generation shots with prompts and camera directions",
        "parameters": "project_id (str), episode (int), shot_list_text (str JSON)",
        "returns": "Shot count, estimated cost, shot types breakdown",
        "example": "create_shot_list(project_id='my_proj_12345', episode=1, shot_list_text='{...}')",
        "chain_next": "Use generate_all_clips() for batch generation or generate_video_clip() for individual shots"
    },
    "generate_video_clip": {
        "description": "Generate a single video clip from a text prompt via xAI grok-imagine-video. Supports image_url for scene continuity (image-to-video).",
        "parameters": "project_id (str), shot_id (str), prompt (str), duration_sec (int), image_url (str, optional — reference frame for continuity)",
        "returns": "Clip file path, cost, quality metadata",
        "example": "generate_video_clip(project_id='my_proj_12345', shot_id='ep01_s01_001')",
        "chain_next": "Continue with more clips or use assemble_edit() when all clips are ready"
    },
    "generate_all_clips": {
        "description": "Batch-generate all clips for an episode. Chains scene continuity automatically — extracts last frame from clip N as reference for clip N+1 when continues_previous is true.",
        "parameters": "project_id (str), episode (int), skip_completed (bool)",
        "returns": "Generation progress, cost summary, continuity_frames_used count",
        "example": "generate_all_clips(project_id='my_proj_12345', episode=1)",
        "chain_next": "Use generate_narration() for voiceover, then assemble_edit()"
    },
    "generate_narration": {
        "description": "Generate narration audio from screenplay text using TTS",
        "parameters": "project_id (str), episode (int), text (str), voice (str), rate (float)",
        "returns": "Audio file paths and durations per scene",
        "example": "generate_narration(project_id='my_proj_12345', episode=1)",
        "chain_next": "Use generate_music() for background track, then assemble_edit()"
    },
    "generate_music": {
        "description": "Generate background music for an episode — AI or FFmpeg ambient synthesis",
        "parameters": "project_id (str), episode (int), mood (str), duration_sec (int), genre (str)",
        "returns": "Music file path and metadata",
        "example": "generate_music(project_id='my_proj_12345', episode=1, mood='tense, building', duration_sec=300)",
        "chain_next": "Use assemble_edit() to combine clips, narration, and music"
    },
    "assemble_edit": {
        "description": "Assemble all clips, narration, and music into a video via FFmpeg",
        "parameters": "project_id (str), episode (int), narration_volume (float), music_volume (float)",
        "returns": "Rendered video path, duration, file size, codec info",
        "example": "assemble_edit(project_id='my_proj_12345', episode=1)",
        "chain_next": "Use qa_review_video() to score quality before final render"
    },
    "qa_review_video": {
        "description": "Quality review — score visual coherence, audio sync, pacing, and flag issues",
        "parameters": "project_id (str), episode (int), scores (str JSON), review_notes (str)",
        "returns": "QA status, scores, issue count, recommended actions",
        "example": "qa_review_video(project_id='my_proj_12345', episode=1, scores='{...}')",
        "chain_next": "Use render_final() if approved, or fix issues and re-run assemble_edit()"
    },
    "render_final": {
        "description": "Final broadcast-quality render with H.264/H.265, subtitles, and metadata",
        "parameters": "project_id (str), episode (int), resolution (str), codec (str), add_subtitles (bool)",
        "returns": "Final file path, size, duration, delivery specs",
        "example": "render_final(project_id='my_proj_12345', episode=1, resolution='1920x1080', add_subtitles=True)",
        "chain_next": "Project complete! Use video_project_status() to review or generate_thumbnail()"
    },
    "video_project_status": {
        "description": "Get project status — pipeline progress, costs, pending actions",
        "parameters": "project_id (str, optional — empty lists all projects)",
        "returns": "Full project status with pipeline stages and file counts",
        "example": "video_project_status(project_id='my_proj_12345')",
        "chain_next": "Follow the next_actions list to advance the pipeline"
    },
    "generate_thumbnail": {
        "description": "Generate a professional YouTube/social thumbnail via Gemini image generation",
        "parameters": "project_id (str), episode (int), prompt (str), text_overlay (str)",
        "returns": "Thumbnail file path and specs",
        "example": "generate_thumbnail(project_id='my_proj_12345', episode=1, text_overlay='Episode 1')",
        "chain_next": "Thumbnail ready for publishing"
    },
    "auto_produce_video": {
        "description": "Fully autonomous video production from a single text prompt — AI writes screenplay, creates shots, generates clips, and assembles the final video",
        "parameters": "prompt (str): what video to make, duration (int): seconds, genre (str), style (str), resolution (str): 480p or 720p",
        "returns": "Complete project with all generated files, cost breakdown, and preview paths",
        "example": "auto_produce_video(prompt='A cinematic tour of ancient Rome at sunset', duration=30, genre='documentary')",
        "chain_next": "Video complete! Use generate_thumbnail() for a cover image or video_project_status() to review"
    },
}

def generate_category_prompt() -> str:
    """Generate compact category overview for AI context (under 300 tokens)"""
    prompt = "🔧 TOOL CATEGORIES - Choose category to see tools:\n\n"
    for category, info in TOOL_CATEGORIES.items():
        prompt += f"{info['emoji']} {category}: {info['description']} ({len(info['tools'])} tools)\n"
        prompt += f"   Use when: {info['use_when']}\n\n"
    
    prompt += "\n💡 SELF-PROMPTING WORKFLOW:\n"
    prompt += "1. Identify problem type → Choose category\n"
    prompt += "2. Call list_tools_in_category('[CATEGORY]') → See available tools\n"
    prompt += "3. Call get_tool_details('[TOOL_NAME]') → Learn how to use specific tool\n"
    prompt += "4. Execute tool → Get results\n"
    prompt += "5. Follow 'chain_next' suggestions → Continue reasoning\n"
    prompt += "6. If complex → create_chain_of_thought() for multi-step analysis\n"
    
    return prompt

def list_tools_in_category(category: str) -> str:
    """Get tools for a specific category with brief descriptions"""
    if category not in TOOL_CATEGORIES:
        return f"❌ Category '{category}' not found. Available: {', '.join(TOOL_CATEGORIES.keys())}"
    
    cat_info = TOOL_CATEGORIES[category]
    output = f"\n{cat_info['emoji']} {category} TOOLS:\n"
    output += f"{cat_info['description']}\n\n"
    
    for tool in cat_info['tools']:
        if tool in TOOL_DETAILS:
            output += f"• {tool}: {TOOL_DETAILS[tool]['description']}\n"
        else:
            output += f"• {tool}\n"
    
    output += f"\n💡 Call get_tool_details('[TOOL_NAME]') for examples and chaining suggestions"
    return output

def get_tool_details(tool_name: str) -> str:
    """Get detailed info about a specific tool including chaining suggestions"""
    if tool_name not in TOOL_DETAILS:
        return f"❌ Tool '{tool_name}' details not available yet"
    
    details = TOOL_DETAILS[tool_name]
    output = f"\n🔧 {tool_name}\n"
    output += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    output += f"📝 Description: {details['description']}\n"
    output += f"📥 Parameters: {details['parameters']}\n"
    output += f"📤 Returns: {details['returns']}\n"
    output += f"💡 Example: {details['example']}\n"
    output += f"🔗 Chain Next: {details['chain_next']}\n"
    
    return output

def integrate_with_map_network(brain_system):
    """
    Integrate category browser with MapSyncNetwork for combined search.
    
    This creates a powerful hybrid discovery system:
    - Categories: Browse by type (memory, web, code, etc.)
    - Vector Search: Find by intent ("I need to analyze data")
    - Combined: Browse category, then vector search within it
    
    Args:
        brain_system: BrainSystem instance with map_network
    """
    if not hasattr(brain_system, 'map_network') or not brain_system.map_network:
        logger.warning("MapSyncNetwork not available for integration")
        return False
    
    # Add category browser functions to available tools
    brain_system.available_tools.update({
        "list_tool_categories": lambda: generate_category_prompt(),
        "list_tools_in_category": list_tools_in_category,
        "get_tool_details": get_tool_details,
        "search_tools_by_intent": lambda intent: search_tools_hybrid(brain_system, intent),
        "search_category_by_intent": lambda category, intent: search_within_category(brain_system, category, intent)
    })
    
    logger.info("🔗 Tool Discovery System integrated with MapSyncNetwork")
    return True

def search_tools_hybrid(brain_system, intent: str, limit: int = 5) -> str:
    """
    Hybrid search combining categories and vector search.
    
    1. First tries vector search (semantic understanding)
    2. Falls back to category matching if vector unavailable
    3. Returns results with both category context and relevance
    """
    output = f"🔍 Searching for: '{intent}'\n\n"
    
    # Try MapSyncNetwork vector search first
    if hasattr(brain_system, 'map_network') and brain_system.map_network:
        try:
            results = brain_system.map_network.query_capabilities(intent, limit)
            if results:
                output += "📊 VECTOR SEARCH RESULTS:\n"
                for i, result in enumerate(results, 1):
                    relevance = result.get('relevance_score', 0) * 100
                    output += f"\n{i}. {result['name']} (relevance: {relevance:.0f}%)\n"
                    output += f"   Category: {result['category']}\n"
                    output += f"   {result['description']}\n"
                
                output += f"\n💡 Use get_tool_details('[TOOL_NAME]') for examples and chaining"
                return output
        except Exception as e:
            logger.warning(f"Vector search failed: {e}")
    
    # Fallback: Category-based keyword matching
    output += "📋 CATEGORY SEARCH RESULTS:\n"
    intent_lower = intent.lower()
    matches = []
    
    for category, info in TOOL_CATEGORIES.items():
        score = 0
        if intent_lower in info['description'].lower():
            score += 10
        if intent_lower in info['use_when'].lower():
            score += 5
        
        if score > 0:
            matches.append((category, score, info))
    
    matches.sort(key=lambda x: x[1], reverse=True)
    
    if matches:
        for category, score, info in matches[:3]:
            output += f"\n{info['emoji']} {category}\n"
            output += f"   {info['description']}\n"
            output += f"   Tools: {len(info['tools'])}\n"
        output += f"\n💡 Use list_tools_in_category('[CATEGORY]') to see tools"
    else:
        output += "No matches found. Try list_tool_categories() to browse all categories.\n"
    
    return output

def search_within_category(brain_system, category: str, intent: str, limit: int = 5) -> str:
    """
    Search for tools within a specific category using intent.
    Combines categorical filtering with vector search.
    """
    if category not in TOOL_CATEGORIES:
        return f"❌ Category '{category}' not found"
    
    cat_info = TOOL_CATEGORIES[category]
    category_tools = cat_info['tools']
    
    output = f"🔍 Searching '{category}' for: '{intent}'\n\n"
    
    # Use MapSyncNetwork if available
    if hasattr(brain_system, 'map_network') and brain_system.map_network:
        try:
            # Get all results, then filter to category
            all_results = brain_system.map_network.query_capabilities(intent, limit=20)
            category_results = [r for r in all_results if r['name'] in category_tools][:limit]
            
            if category_results:
                output += f"Found {len(category_results)} relevant tools:\n"
                for i, result in enumerate(category_results, 1):
                    relevance = result.get('relevance_score', 0) * 100
                    output += f"\n{i}. {result['name']} (relevance: {relevance:.0f}%)\n"
                    output += f"   {result['description']}\n"
                return output
        except Exception as e:
            logger.warning(f"Vector search within category failed: {e}")
    
    # Fallback: simple keyword matching within category
    output += "Tools in this category:\n"
    for tool in category_tools:
        if tool in TOOL_DETAILS:
            output += f"• {tool}: {TOOL_DETAILS[tool]['description']}\n"
        else:
            output += f"• {tool}\n"
    
    return output

if __name__ == "__main__":
    # Test the system
    print(generate_category_prompt())
    print("\n" + "="*80 + "\n")
    print(list_tools_in_category("WEB_RESEARCH"))
    print("\n" + "="*80 + "\n")
    print(get_tool_details("grokipedia_search"))
