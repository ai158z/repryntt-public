# REPRYNTT Tool Registry

> **380 registered tool names** (~319 unique tools + ~40 aliases + 21 MCP server tools)
>
> Last updated: 2026-03-29

---

## Summary

| Source | File | Count |
|--------|------|-------|
| Brain Delegate Tools | `repryntt/tools/registry.py` → `register_brain_delegate_tools()` | 36 |
| Native Tools | `repryntt/tools/registry.py` → `register_native_tools()` | 264 |
| Daemon Virtual Tools | `repryntt/agents/persistent_agents.py` → `_build_native_tools()` | 39 |
| Tool Discovery | `repryntt/tools/discovery.py` → `integrate_with_map_network()` | 5 |
| MCP Client (dynamic) | `repryntt/routing/mcp_client.py` | 15 |
| MCP Server (external API) | `repryntt/mcp_server.py` | 21 |
| **Total** | | **~380** |

---

## 1. Brain Delegate Tools (36)

Registered via `register_brain_delegate_tools()` in `repryntt/tools/registry.py`.

### Memory (10 + 2 aliases)

| Tool | Notes |
|------|-------|
| `brain_memory_save` | Save to semantic memory |
| `brain_memory_recall` | Recall from semantic memory |
| `brain_network_search` | Search knowledge network |
| `get_brain_stats` | Brain system statistics |
| `search_domain` | Search specific knowledge domain |
| `store_learning` | Store learning outcome |
| `get_relevant_context` | Get contextually relevant memories |
| `update_procedural` | Update procedural memory |
| `search_knowledge` | Search across knowledge base |
| `recall_memory` | _alias → brain_network_search_ |
| `analyze_text` | _alias → get_relevant_context_ |

### Personality (9)

| Tool | Notes |
|------|-------|
| `modify_personality_trait` | Modify an existing trait value |
| `evolve_personality_dimension` | Evolve a personality dimension |
| `update_behavioral_guidelines` | Update behavioral rules |
| `recreate_autonomous_personality` | Reset personality to autonomous defaults |
| `add_personality_trait` | Add a new trait |
| `remove_personality_trait` | Remove a trait |
| `log_personality_evolution` | Log a personality change event |
| `analyze_personality_growth` | Analyze growth over time |
| `update_avatar` | Update avatar appearance |

### Chain-of-Thought (9)

| Tool | Notes |
|------|-------|
| `create_chain_of_thought` | Create a reasoning chain |
| `create_self_autonomous_chain` | Create self-directed chain |
| `advance_self_autonomous_chain` | Advance self-directed chain |
| `update_chain_progress` | Update chain step progress |
| `get_chain_context` | Get chain reasoning context |
| `queue_chain_of_thought` | Queue a chain for async processing |
| `get_cot_queue_status` | Check CoT queue status |
| `clear_cot_queue` | Clear CoT queue |
| `query_exploration_history` | Query past explorations |

### Conversation (5 + 2 aliases)

| Tool | Notes |
|------|-------|
| `initiate_conversation` | Start a new conversation |
| `get_recent_conversations` | List recent conversations |
| `search_conversations` | Search conversation history |
| `get_conversation_summary` | Summarize a conversation |
| `export_conversation` | Export conversation to file |
| `start_conversation` | _alias → initiate_conversation_ |
| `talk_to_human` | _alias → initiate_conversation_ |

### Misc (1)

| Tool | Notes |
|------|-------|
| `reset_inspiration_index` | Reset inspiration tracking |

---

## 2. Native Tools (264)

Registered via `register_native_tools()` in `repryntt/tools/registry.py`.

### Trading Simulator (5)

| Tool | Notes |
|------|-------|
| `sim_buy` | Simulated buy order |
| `sim_sell` | Simulated sell order |
| `sim_portfolio` | View simulated portfolio |
| `sim_price_check` | Check simulated price |
| `sim_faucet` | Get simulated tokens |

### Trading Bot (11)

| Tool | Notes |
|------|-------|
| `trading_bot_start` | Start trading bot |
| `trading_bot_stop` | Stop trading bot |
| `trading_bot_status` | Bot status |
| `trading_signals` | Get trading signals |
| `trading_hot_tokens` | Hot token list |
| `trading_performance` | Bot performance metrics |
| `trading_token_detail` | Token detail lookup |
| `token_price_history` | Historical price data |
| `log_trade_outcome` | Log trade result |
| `review_trade_journal` | Review trade journal |
| `trading_browse_tokens` | Browse token listings |

### Whale Monitor (4)

| Tool | Notes |
|------|-------|
| `whale_add_wallet` | Track a whale wallet |
| `whale_remove_wallet` | Stop tracking wallet |
| `whale_list_wallets` | List tracked wallets |
| `whale_monitor_status` | Monitor status |

### KOLscan (3)

| Tool | Notes |
|------|-------|
| `kol_leaderboard` | KOL performance leaderboard |
| `kol_sync_wallets` | Sync KOL wallets |
| `kol_remove_underperformers` | Remove underperforming KOLs |

### Scalp Executor (5)

| Tool | Notes |
|------|-------|
| `scalp_status` | Scalping bot status |
| `scalp_force_sell` | Force sell position |
| `scalp_force_buy` | Force buy entry |
| `scalp_set_param` | Set scalp parameters |
| `scalp_history` | View scalp history |

### Solana Execution + PumpFun (4)

| Tool | Notes |
|------|-------|
| `wallet_status` | Solana wallet status |
| `real_buy` | Execute real buy (mainnet) |
| `real_sell` | Execute real sell (mainnet) |
| `launch_pumpfun_token` | Launch token on PumpFun |

### Token Launch Pipeline (5)

| Tool | Notes |
|------|-------|
| `launch_memecoin` | Full memecoin launch |
| `launch_pipeline_ideate` | Ideation stage |
| `launch_pipeline_design` | Design stage |
| `launch_pipeline_review` | Review stage |
| `launch_pipeline_execute` | Execute launch |

### Token Pipeline Management (9)

| Tool | Notes |
|------|-------|
| `pipeline_status` | Pipeline overview |
| `pipeline_next` | Advance to next stage |
| `pipeline_filter` | Filter pipeline entries |
| `pipeline_add_watchlist` | Add to watchlist |
| `pipeline_research_done` | Mark research complete |
| `pipeline_confirm` | Confirm pipeline entry |
| `pipeline_executed` | Mark as executed |
| `pipeline_close` | Close pipeline entry |
| `pipeline_token_detail` | Token details |

### Trading Scan (1)

| Tool | Notes |
|------|-------|
| `trading_scan` | Scan for trading opportunities |

### Degen Terminal (1)

| Tool | Notes |
|------|-------|
| `degen_terminal_top` | Top tokens from degen terminal |

### Token Cleanup (2)

| Tool | Notes |
|------|-------|
| `remove_token` | Remove a token from tracking |
| `purge_bad_tokens` | Purge low-quality tokens |

### DeFi / Market Data (3)

| Tool | Notes |
|------|-------|
| `dexscreener_trending` | Trending tokens on DexScreener |
| `dexscreener_token_search` | Search tokens on DexScreener |
| `solana_rpc_query` | Raw Solana RPC query |

### X/Twitter Search (3 + aliases)

| Tool | Notes |
|------|-------|
| `x_search_tweets` | Search tweets |
| `x_search_crypto` | Search crypto-related tweets |
| `twitter_search` | _alias → x_search_tweets_ |

### Twitter Actions (4 + aliases)

| Tool | Notes |
|------|-------|
| `post_tweet` | Post a tweet |
| `check_twitter_mentions` | Check mentions |
| `reply_to_twitter` | Reply to a tweet |
| `get_twitter_status` | Twitter account status |
| `tweet` | _alias → post_tweet_ |
| `twitter_status` | _alias → get_twitter_status_ |

### Grokipedia / Knowledge (7)

| Tool | Notes |
|------|-------|
| `grokipedia_search` | Search Grokipedia knowledge base |
| `get_knowledge_domain_distribution` | Knowledge domain stats |
| `clear_grokipedia_history` | Clear search history |
| `analyze_topic_complexity` | Analyze topic complexity |
| `find_similar_topics` | Find related topics |
| `pull_knowledge_topics` | Pull knowledge topics |
| `integrate_knowledge_context` | Integrate knowledge into context |
| `grokedia_search` | _alias → grokipedia_search_ |
| `analyze_topic` | _alias → analyze_topic_complexity_ |

### Google Maps (4)

| Tool | Notes |
|------|-------|
| `google_maps_search` | Search Google Maps |
| `get_directions` | Get directions |
| `geocode_address` | Geocode an address |
| `find_nearby_places` | Find nearby places |

### Math / Research (8)

| Tool | Notes |
|------|-------|
| `compute_zeta_function` | Compute Riemann zeta values |
| `analyze_zeta_zeros` | Analyze zeta zeros |
| `symbolic_manipulation` | Symbolic math |
| `numerical_analysis` | Numerical computation |
| `statistical_analysis` | Statistical analysis |
| `pattern_recognition` | Pattern recognition |
| `access_mathematical_databases` | Mathematical databases |
| `mathematical_visualization` | Math visualization |

### Creative File I/O (5)

| Tool | Notes |
|------|-------|
| `create_creative_file` | Create creative workspace file |
| `write_to_creative_file` | Write to file |
| `append_to_creative_file` | Append to file |
| `read_creative_file` | Read file |
| `get_creative_workspace_status` | Workspace status |

### Robot Economy (10)

| Tool | Notes |
|------|-------|
| `start_robot_economy` | Start economy services |
| `stop_robot_economy` | Stop economy services |
| `get_economy_status` | Economy overview |
| `submit_robot_workload` | Submit workload to network |
| `get_robot_wallet_balance` | Check wallet balance |
| `get_robot_blockchain_info` | Blockchain info |
| `allocate_robot_dao_funds` | Allocate DAO funds |
| `create_robot_wallet` | Create new wallet |
| `recover_robot_wallet` | Recover wallet from seed |
| `monitor_robot_economy` | Monitor economy metrics |
| `allocate_dao_funds` | _alias → allocate_robot_dao_funds_ |
| `get_blockchain_info` | _alias → get_robot_blockchain_info_ |
| `get_wallet_balance` | _alias → get_robot_wallet_balance_ |
| `submit_workload` | _alias → submit_robot_workload_ |
| `monitor_economy` | _alias → monitor_robot_economy_ |

### Employee Management (9)

| Tool | Notes |
|------|-------|
| `employee_roster` | List all employees |
| `assign_work` | Assign work to employee |
| `check_work` | Check employee work |
| `find_employee` | Find employee by criteria |
| `employee_status` | Employee status |
| `rename_employee` | Rename employee |
| `list_available_roles` | List available roles |
| `spawn_expert` | Spawn specialized employee |
| `initialize_full_roster` | Initialize full employee roster |

### Swarm / Team (15)

| Tool | Notes |
|------|-------|
| `create_agent` | Create a new agent |
| `create_swarm` | Create agent swarm |
| `add_agents_to_swarm` | Add agents to swarm |
| `retire_agent` | Retire an agent |
| `dissolve_swarm` | Dissolve a swarm |
| `dispatch_task` | Dispatch task to agent |
| `broadcast_task` | Broadcast task to swarm |
| `delegate_tasks` | Delegate tasks across agents |
| `start_discussion` | Start agent discussion |
| `get_swarm_overview` | Swarm overview |
| `get_agent_info` | Agent details |
| `list_agents` | List all agents |
| `quick_research` | Quick research task |
| `quick_brainstorm` | Quick brainstorm session |
| `call_jarvis` | Call Jarvis agent |

### Social Network (6)

| Tool | Notes |
|------|-------|
| `social_post` | Post to social network |
| `social_feed` | View social feed |
| `social_reply` | Reply to social post |
| `social_read_post` | Read a social post |
| `social_nodes` | List social nodes |
| `social_my_identity` | View node identity |

### Media / Image / Voice (6)

| Tool | Notes |
|------|-------|
| `generate_image` | Generate image via AI |
| `analyze_image` | Analyze image content |
| `download_image` | Download image from URL |
| `capture_camera` | Capture from camera |
| `speak` | Text to speech |
| `listen` | Speech to text |

### Web Search (6 + aliases)

| Tool | Notes |
|------|-------|
| `real_web_search` | Real web search |
| `google_web_search` | Google search |
| `web_search_results_only` | Search results only (no fetch) |
| `scrape_web_page` | Scrape web page |
| `call_knowledge_api_feeder` | Feed knowledge API |
| `extract_content_from_url` | Extract content from URL |
| `knowledge_search` | _alias → google_web_search_ |
| `google_search` | _alias → google_web_search_ |
| `web_search` | _alias → real_web_search_ |
| `duckduckgo_search` | _alias → real_web_search_ |
| `internet_search` | _alias → real_web_search_ |
| `search_results_only` | _alias → web_search_results_only_ |
| `fetch_url` | _alias → scrape_web_page_ |
| `scrape_url` | _alias → scrape_web_page_ |
| `fetch_web_info` | _alias → call_knowledge_api_feeder_ |
| `extract_content` | _alias → extract_content_from_url_ |

### Filesystem / Code (8)

| Tool | Notes |
|------|-------|
| `run_terminal_cmd` | Run shell command (sandboxed) |
| `read_file` | Read a file |
| `write_file` | Write a file |
| `list_dir` | List directory contents |
| `analyze_codebase` | Analyze codebase |
| `check_syntax` | Check code syntax |
| `get_sandbox_status` | Sandbox status |
| `propose_code_change` | Propose code change |

### Code Extras (4)

| Tool | Notes |
|------|-------|
| `search_replace` | Search and replace in file |
| `grep_search` | Grep search across files |
| `run_code_tests` | Run test suite |
| `get_code_context` | Get code context |

### Tool Execution / Context (6)

| Tool | Notes |
|------|-------|
| `build_tool_schemas` | Build tool JSON schemas |
| `build_tool_context` | Build tool context string |
| `get_tool_credit_cost` | Tool credit cost |
| `get_tool_credit_reward` | Tool credit reward |
| `get_step_tool_hint` | Suggest tool for step |
| `get_task_tool_examples` | Get example tool calls |

### Time (1 + alias)

| Tool | Notes |
|------|-------|
| `get_current_time` | Get current time |
| `check_time` | _alias → get_current_time_ |

### Gmail (8)

| Tool | Notes |
|------|-------|
| `gmail_send` | Send email |
| `gmail_read_inbox` | Read inbox |
| `gmail_search` | Search emails |
| `gmail_read_message` | Read specific email |
| `gmail_reply` | Reply to email |
| `gmail_draft` | Create draft |
| `gmail_mark_read` | Mark as read |
| `gmail_get_profile` | Get Gmail profile |

### Video Production (13)

| Tool | Notes |
|------|-------|
| `create_video_project` | Create video project |
| `write_screenplay` | Write screenplay |
| `create_shot_list` | Create shot list |
| `generate_video_clip` | Generate video clip |
| `generate_all_clips` | Generate all clips |
| `generate_narration` | Generate narration audio |
| `generate_music` | Generate background music |
| `assemble_edit` | Assemble video edit |
| `qa_review_video` | QA review video |
| `render_final` | Render final video |
| `video_project_status` | Project status |
| `generate_thumbnail` | Generate thumbnail |
| `auto_produce_video` | Fully automated production |

### Voiceover (1)

| Tool | Notes |
|------|-------|
| `generate_voiceover` | Generate TTS voiceover (Piper) |

### Recursive Learning (9)

| Tool | Notes |
|------|-------|
| `learning_trading_stats` | Trading learning stats |
| `learning_trading_brief` | Trading learning brief |
| `learning_signal_weights` | Signal weight values |
| `learning_backfill_journal` | Backfill trade journal |
| `learning_identity_stats` | Identity learning stats |
| `learning_identity_brief` | Identity learning brief |
| `learning_optimal_conditions` | Optimal trading conditions |
| `learning_all_domains` | All learning domains |
| `learning_weight_history` | Weight change history |

### LLM Orchestration Learning (9)

| Tool | Notes |
|------|-------|
| `llm_learning_stats` | LLM learning statistics |
| `llm_learning_brief` | LLM learning brief |
| `llm_escalation_report` | Escalation report |
| `llm_context_report` | Context budget report |
| `llm_model_profile` | Model quality profile |
| `llm_score_output` | Score LLM output quality |
| `llm_should_escalate` | Should escalate to higher tier? |
| `llm_context_budget` | Get context budget allocation |
| `llm_detect_model` | Detect model from output |

### Memory Consolidation (3)

| Tool | Notes |
|------|-------|
| `consolidate_memories_deep` | Deep memory consolidation |
| `search_consolidated_memory` | Search consolidated memories |
| `get_consolidation_stats` | Consolidation statistics |

### Activity Frameworks (3)

| Tool | Notes |
|------|-------|
| `framework_start` | Start activity framework |
| `framework_advance` | Advance framework step |
| `framework_status` | Framework status |

### CodeForge (5)

| Tool | Notes |
|------|-------|
| `forge_project` | Start a forge project |
| `forge_status` | Forge project status |
| `forge_cancel` | Cancel forge project |
| `forge_benchmark` | Run benchmarks |
| `forge_swarm_status` | Forge swarm status |

### Andrew's Hub (4)

| Tool | Notes |
|------|-------|
| `hub_publish` | Publish to hub |
| `hub_list` | List hub entries |
| `hub_read` | Read hub entry |
| `hub_delete` | Delete hub entry |

### Open Mind (7)

| Tool | Notes |
|------|-------|
| `open_mind_begin` | Begin open mind session |
| `open_mind_integrate` | Integrate open mind insight |
| `open_mind_history` | Open mind history |
| `open_mind_profiles` | Open mind profiles |
| `open_mind_read_session` | Read open mind session |
| `open_mind_dream_journal` | Dream journal entry |
| `open_mind_read_dream` | Read dream journal |

### MoonPay (21)

| Tool | Notes |
|------|-------|
| `mp_wallet_create` | Create MoonPay wallet |
| `mp_wallet_list` | List wallets |
| `mp_wallet_balance` | Wallet balance |
| `mp_wallet_discover` | Discover wallet assets |
| `mp_token_swap` | Swap tokens |
| `mp_token_bridge` | Bridge tokens cross-chain |
| `mp_token_transfer` | Transfer tokens |
| `mp_token_quote` | Get swap quote |
| `mp_token_search` | Search tokens |
| `mp_token_trending` | Trending tokens |
| `mp_token_info` | Token information |
| `mp_token_check` | Token safety check |
| `mp_buy_crypto` | Buy crypto with fiat |
| `mp_deposit` | Deposit crypto |
| `mp_prediction_market_search` | Search prediction markets |
| `mp_prediction_market_trade` | Trade prediction market |
| `mp_prediction_positions` | Prediction positions |
| `mp_chain_list` | List supported chains |
| `mp_transaction_list` | Transaction history |
| `mp_user_status` | User status |
| `mp_wallet_activity` | Wallet activity feed |

### Payment Gateway (5)

| Tool | Notes |
|------|-------|
| `gateway_create_deposit` | Create deposit address (SOL/USDC) |
| `gateway_deposit_status` | Check deposit status |
| `gateway_status` | Gateway status |
| `gateway_list_deposits` | List all deposits |
| `gateway_poll_deposits` | Poll for new deposits |

---

## 3. Daemon Virtual Tools (39)

Injected via `_build_native_tools()` in `repryntt/agents/persistent_agents.py`. These are Jarvis-level tools not in ToolRegistry.

### Scheduling (3)

| Tool | Notes |
|------|-------|
| `schedule_cron` | Schedule a cron job |
| `list_cron` | List cron jobs |
| `remove_cron` | Remove cron job |

### Memory / State (7)

| Tool | Notes |
|------|-------|
| `flush_memory` | Flush memory to disk |
| `append_daily_memory` | Append to daily memory log |
| `update_daily_plan` | Update daily plan |
| `memory_search` | Search agent memory |
| `memory_get` | Get specific memory |
| `read_bootstrap_file` | Read bootstrap file |
| `update_bootstrap_file` | Update bootstrap file |

### Agent / Skill Management (9)

| Tool | Notes |
|------|-------|
| `invoke_sub_agent` | Invoke a sub-agent |
| `list_skills` | List available skills |
| `get_skill` | Get skill details |
| `install_skill` | Install a skill |
| `spawn_agent` | Spawn new agent |
| `list_my_tools` | List own tools |
| `request_tools_from_category` | Request tools by category |
| `llm_toggle` | Toggle local/cloud LLM |
| `query_local_llm` | Query local LLM directly |

### Personality / Journal (2)

| Tool | Notes |
|------|-------|
| `update_personality_journal` | Update personality journal |
| `save_world_scan` | Save world observation |

### Conversation (2)

| Tool | Notes |
|------|-------|
| `start_conversation` | Start conversation with user |
| `end_conversation` | End conversation |

### Experiments (2)

| Tool | Notes |
|------|-------|
| `start_experiment` | Start an experiment |
| `get_experiment_status` | Get experiment status |

### Tasks (2)

| Tool | Notes |
|------|-------|
| `create_persistent_task` | Create persistent task |
| `complete_persistent_task` | Complete persistent task |

### Commerce (5)

| Tool | Notes |
|------|-------|
| `commerce_status` | Commerce system status |
| `commerce_list_products` | List products |
| `commerce_create_product` | Create product |
| `commerce_check_orders` | Check orders |
| `commerce_save_digital_product` | Save digital product |
| `commerce_list_saved_products` | List saved products |

### Skill Packages (5)

| Tool | Notes |
|------|-------|
| `list_skill_packages` | List available packages |
| `download_skill` | Download skill package |
| `verify_skill` | Verify skill package |
| `list_installed_skills` | List installed skills |
| `uninstall_skill_pkg` | Uninstall skill package |
| `create_skill_package` | Create skill package |

---

## 4. Tool Discovery System (5)

Registered via `integrate_with_map_network()` in `repryntt/tools/discovery.py`.

| Tool | Notes |
|------|-------|
| `list_tool_categories` | List all tool categories |
| `list_tools_in_category` | List tools in a category |
| `get_tool_details` | Get tool details |
| `search_tools_by_intent` | Search tools by intent |
| `search_category_by_intent` | Search categories by intent |

---

## 5. MCP Client — Dynamic External Tools (15)

Dynamically registered from connected MCP servers. Tool names follow `mcp_<server>_<tool>` pattern.

### Browser / Playwright (9)

| Tool | Notes |
|------|-------|
| `mcp_browser_browser_navigate` | Navigate to URL |
| `mcp_browser_browser_search_google` | Google search in browser |
| `mcp_browser_browser_click` | Click element |
| `mcp_browser_browser_type` | Type text |
| `mcp_browser_browser_screenshot` | Take screenshot |
| `mcp_browser_browser_get_text` | Get page text |
| `mcp_browser_browser_get_links` | Get page links |
| `mcp_browser_browser_scroll` | Scroll page |
| `mcp_browser_browser_current_url` | Get current URL |

### Fetch (1)

| Tool | Notes |
|------|-------|
| `mcp_fetch_fetch` | Fetch URL content |

### Computer Use (5)

| Tool | Notes |
|------|-------|
| `mcp_computer_screenshot` | Take screenshot |
| `mcp_computer_key_press` | Press key |
| `mcp_computer_keyboard_type` | Type on keyboard |
| `mcp_computer_mouse_click` | Mouse click |
| `mcp_computer_open_app` | Open application |

---

## 6. MCP Server — External API Tools (21)

Exposed to external MCP clients (Claude Desktop, VS Code Copilot, etc.) via `repryntt/mcp_server.py`.

| Tool | Notes |
|------|-------|
| `repryntt_status` | Node status |
| `repryntt_register` | Register API key |
| `repryntt_chat` | Chat with AI |
| `repryntt_tool_list` | List available tools |
| `repryntt_tool_call` | Call a tool |
| `repryntt_analyze` | Deep analysis |
| `repryntt_gateway_status` | Payment gateway status |
| `repryntt_gateway_deposit` | Create deposit |
| `repryntt_blockchain_health` | Blockchain health |
| `repryntt_trading_portfolio` | Trading portfolio |
| `repryntt_trading_signals` | Trading signals |
| `repryntt_agents_list` | List agents |
| `repryntt_spawn_agent` | Spawn agent |
| `repryntt_compute_stats` | Compute marketplace stats |
| `repryntt_workload_submit` | Submit workload |
| `repryntt_workload_status` | Check workload status |
| `repryntt_workload_list` | List workloads |
| `repryntt_workload_cancel` | Cancel workload |
| `repryntt_node_config` | Node config |
| `repryntt_wallet_balance` | Wallet balance |
| `repryntt_faucet` | Request test credits |

---

## Adding New Tools

When adding a new tool:

1. **Define the tool** in the appropriate registration function:
   - Most tools → `register_native_tools()` in `repryntt/tools/registry.py`
   - Daemon-level → `_build_native_tools()` in `repryntt/agents/persistent_agents.py`
   - MCP external → `repryntt/mcp_server.py`

2. **Add to JARVIS_STARTER_TOOLS** in `persistent_agents.py` if Jarvis should have it at boot

3. **Update this file** with the new tool name and category

4. **Update README.md** tool count if the total changes significantly
