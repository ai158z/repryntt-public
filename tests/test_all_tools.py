#!/usr/bin/env python3
"""
Comprehensive tool connectivity test — verifies every registered tool
is callable, has valid signatures, and returns without import errors.

Run:  python tests/test_all_tools.py
"""
import inspect
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ── Boot the brain ───────────────────────────────────────────────────────────

print("Loading ReprynttBrainSystem...", flush=True)
from repryntt.brain.brain_impl import ReprynttBrainSystem
brain = ReprynttBrainSystem()
reg = brain._tool_registry
print(f"Registry loaded: {len(reg)} tools\n", flush=True)

# ── Safe-call arguments for tools that need specific params ──────────────────
# These are minimal args that won't cause side effects but will prove the
# function is reachable and callable. Tools not listed here get called
# with no args (expecting either a result or a clean TypeError about missing args).

SAFE_CALL_ARGS = {
    # Awareness
    "get_current_time": {},
    "check_time": {},

    # Memory
    "get_brain_stats": {},
    "brain_memory_recall": {"query": "test"},
    "brain_network_search": {"query": "test"},
    "search_domain": {"query": "test"},
    "get_relevant_context": {"question": "test"},

    # Knowledge
    "grokipedia_search": {"query": "test"},
    "get_knowledge_domain_distribution": {},
    "clear_grokipedia_history": {},
    "analyze_topic_complexity": {"topic": "math"},
    "find_similar_topics": {"topic": "test"},
    "pull_knowledge_topics": {"domain": "test"},
    "integrate_knowledge_context": {"query": "test"},

    # Personality
    "analyze_personality_growth": {},

    # Chain of thought
    "get_cot_queue_status": {},

    # Conversation
    "get_recent_conversations": {},

    # Creative
    "get_creative_workspace_status": {},

    # Tool execution
    "build_tool_schemas": {},
    "build_tool_context": {"task": "test"},
    "get_tool_credit_cost": {"tool_name": "get_current_time"},
    "get_tool_credit_reward": {"tool_name": "get_current_time"},
    "get_step_tool_hint": {"step": "test"},
    "get_task_tool_examples": {"task": "test"},

    # Trading sim
    "sim_portfolio": {},
    "sim_price_check": {"token": "SOL"},

    # Trading bot bridge
    "trading_bot_status": {},
    "trading_signals": {},
    "trading_hot_tokens": {},
    "trading_performance": {},

    # Trading scan
    "trading_scan": {},

    # Scalp
    "scalp_status": {},
    "scalp_history": {},

    # Whale / KOL
    "whale_list_wallets": {},
    "whale_monitor_status": {},
    "kol_leaderboard": {},

    # DeFi
    "dexscreener_trending": {},

    # Social (read-only)
    "get_twitter_status": {},

    # Swarm (read-only)
    "get_swarm_overview": {},
    "list_agents": {},

    # Employee (read-only)
    "employee_roster": {},

    # Robot economy (read-only)
    "get_economy_status": {},

    # Maps (safe)
    "geocode_address": {"address": "New York, NY"},

    # Web search (safe)
    "real_web_search": {"query": "test"},

    # Filesystem (safe read)
    "list_dir": {"path": "."},
    "get_sandbox_status": {},

    # Code
    "get_code_context": {},

    # Video
    "video_project_status": {},

    # Grokipedia reset
    "reset_inspiration_index": {},
}

# Tools that should NOT be called even with test args (destructive / side effects)
SKIP_EXECUTION = {
    # Writes / mutations
    "write_file", "run_terminal_cmd", "search_replace",
    "brain_memory_save", "store_learning", "update_procedural",
    "modify_personality_trait", "evolve_personality_dimension",
    "update_behavioral_guidelines", "recreate_autonomous_personality",
    "add_personality_trait", "remove_personality_trait",
    "log_personality_evolution", "update_avatar",
    "create_chain_of_thought", "create_self_autonomous_chain",
    "advance_self_autonomous_chain", "update_chain_progress",
    "queue_chain_of_thought", "clear_cot_queue",
    "initiate_conversation", "export_conversation",
    "create_creative_file", "write_to_creative_file",
    "append_to_creative_file",
    "sim_buy", "sim_sell", "sim_faucet",
    "trading_bot_start", "trading_bot_stop",
    "log_trade_outcome",
    "scalp_force_buy", "scalp_force_sell", "scalp_set_param",
    "whale_add_wallet", "whale_remove_wallet",
    "kol_remove_underperformers", "kol_sync_wallets",
    "post_tweet", "tweet", "reply_to_twitter",
    "check_twitter_mentions",  # rate-limited
    "x_search_tweets", "twitter_search", "x_search_crypto",  # rate-limited
    "create_agent", "create_swarm", "add_agents_to_swarm",
    "retire_agent", "dissolve_swarm", "dispatch_task",
    "broadcast_task", "delegate_tasks", "start_discussion",
    "quick_research", "quick_brainstorm", "call_jarvis",
    "council_advise", "council_post_report",
    "assign_work", "rename_employee",
    "start_robot_economy", "stop_robot_economy",
    "submit_robot_workload", "allocate_robot_dao_funds",
    "create_robot_wallet", "recover_robot_wallet",
    "monitor_robot_economy",
    "speak", "listen", "capture_camera",
    "generate_image", "analyze_image",
    "google_maps_search", "get_directions", "find_nearby_places",
    "create_video_project", "write_screenplay", "create_shot_list",
    "generate_video_clip", "generate_all_clips", "generate_narration",
    "generate_music", "assemble_edit", "qa_review_video",
    "render_final", "generate_thumbnail", "auto_produce_video",
    "analyze_codebase", "check_syntax", "propose_code_change",
    "read_file",  # needs path
    "run_code_tests",  # runs tests
    "scrape_web_page", "extract_content_from_url",
    "call_knowledge_api_feeder",
    "google_web_search", "web_search_results_only",
    "solana_rpc_query",
    "dexscreener_token_search",
    "compute_zeta_function", "analyze_zeta_zeros",
    "symbolic_manipulation", "numerical_analysis",
    "statistical_analysis", "pattern_recognition",
    "access_mathematical_databases", "mathematical_visualization",
    "read_creative_file",
    "get_robot_blockchain_info", "get_robot_wallet_balance",
    "token_price_history", "trading_token_detail",
    "review_trade_journal",
    "get_conversation_summary", "search_conversations",
    "query_exploration_history", "get_chain_context",
    "check_work", "find_employee", "employee_status",
    "get_agent_info",
}

# ── Test runner ──────────────────────────────────────────────────────────────

results = {}  # tool_name → {status, category, callable, has_sig, exec_result, error}

categories = reg.categories()
total = 0
passed = 0
callable_ok = 0
exec_ok = 0
exec_skipped = 0
exec_fail = 0

for cat in sorted(categories.keys()):
    tools = sorted(categories[cat])
    for tool_name in tools:
        total += 1
        result = {
            "category": cat,
            "callable": False,
            "has_signature": False,
            "signature": "",
            "exec_status": "skipped",
            "exec_result": "",
            "error": "",
        }

        # 1. Check callable
        func = reg.get(tool_name)
        if func is None:
            result["error"] = "registry.get() returned None"
            result["status"] = "FAIL"
            results[tool_name] = result
            continue

        if callable(func):
            result["callable"] = True
            callable_ok += 1
        else:
            result["error"] = f"Not callable: {type(func)}"
            result["status"] = "FAIL"
            results[tool_name] = result
            continue

        # 2. Check signature
        try:
            sig = inspect.signature(func)
            result["has_signature"] = True
            result["signature"] = str(sig)
        except (ValueError, TypeError) as e:
            result["signature"] = f"<error: {e}>"

        # 3. Try execution (safe tools only)
        if tool_name in SKIP_EXECUTION:
            result["exec_status"] = "skipped_destructive"
            exec_skipped += 1
        elif tool_name in SAFE_CALL_ARGS:
            args = SAFE_CALL_ARGS[tool_name]
            try:
                ret = func(**args)
                # Truncate large returns
                ret_str = str(ret)
                if len(ret_str) > 200:
                    ret_str = ret_str[:200] + "..."
                result["exec_status"] = "OK"
                result["exec_result"] = ret_str
                exec_ok += 1
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if len(err) > 300:
                    err = err[:300] + "..."
                result["exec_status"] = "EXEC_ERROR"
                result["exec_result"] = err
                exec_fail += 1
        else:
            # Try calling with no args — if it fails with TypeError about args, 
            # that proves the function IS connected (just needs args)
            try:
                ret = func()
                ret_str = str(ret)
                if len(ret_str) > 200:
                    ret_str = ret_str[:200] + "..."
                result["exec_status"] = "OK"
                result["exec_result"] = ret_str
                exec_ok += 1
            except TypeError as e:
                err_str = str(e)
                if "argument" in err_str or "required" in err_str:
                    # Good — function exists, just needs args
                    result["exec_status"] = "OK_NEEDS_ARGS"
                    result["exec_result"] = err_str
                    exec_ok += 1
                else:
                    result["exec_status"] = "EXEC_ERROR"
                    result["exec_result"] = f"TypeError: {err_str}"
                    exec_fail += 1
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if len(err) > 300:
                    err = err[:300] + "..."
                result["exec_status"] = "EXEC_ERROR"
                result["exec_result"] = err
                exec_fail += 1

        # Overall status
        if result["callable"] and result["exec_status"] in ("OK", "OK_NEEDS_ARGS", "skipped_destructive"):
            result["status"] = "PASS"
            passed += 1
        else:
            result["status"] = "FAIL"

        results[tool_name] = result

        # Progress indicator
        icon = "✓" if result["status"] == "PASS" else "✗"
        print(f"  {icon} {tool_name:<45} {result['exec_status']}", flush=True)


# ── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
print(f"TOOL VERIFICATION SUMMARY")
print(f"{'='*60}")
print(f"  Total tools:       {total}")
print(f"  Callable:          {callable_ok}")
print(f"  Passed:            {passed}")
print(f"  Exec OK:           {exec_ok}")
print(f"  Exec skipped:      {exec_skipped}")
print(f"  Exec failed:       {exec_fail}")
print(f"  Overall:           {'ALL PASS' if passed == total else f'{total - passed} FAILURES'}")
print(f"{'='*60}")

if exec_fail > 0:
    print(f"\nFAILED TOOLS:")
    for name, r in sorted(results.items()):
        if r["status"] == "FAIL":
            print(f"  ✗ {name}: {r['exec_status']} — {r.get('exec_result', r.get('error', ''))[:120]}")

# ── Write JSON report ────────────────────────────────────────────────────────

report = {
    "timestamp": datetime.now().isoformat(),
    "total_tools": total,
    "callable": callable_ok,
    "passed": passed,
    "exec_ok": exec_ok,
    "exec_skipped": exec_skipped,
    "exec_failed": exec_fail,
    "tools": results,
}

report_path = Path(__file__).parent / "tool_verification_results.json"
report_path.write_text(json.dumps(report, indent=2, default=str))
print(f"\nJSON report: {report_path}")
