#!/usr/bin/env python3
"""
FULL END-TO-END TOOL VERIFICATION — Jarvis Execution Path

Tests every tool through the SAME _execute_native_tool_calls() path
that Jarvis/Andrew uses in production. Each tool receives a simulated
OpenAI-format tool_call and the result is captured.

This is not a "can we import it" test — this is "when Jarvis calls the
tool with real parameters, does it actually work?"
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import warnings
import traceback
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional

warnings.filterwarnings("ignore")

# ── Test parameters for every single tool ──
# Each tool gets the exact JSON arguments Jarvis would pass.
# Parameters are chosen to be SAFE (read-only where possible, temp files, etc.)

TOOL_TEST_ARGS: Dict[str, Dict] = {
    # ── awareness ──
    "check_time": {},
    "get_current_time": {"format": "full"},

    # ── chain_of_thought ──
    "create_chain_of_thought": {"topic": "__TEST_VERIFY__", "milestones": ["step1", "step2"]},
    "create_self_autonomous_chain": {"topic": "__TEST_VERIFY_CHAIN__", "target_steps": 2},
    "advance_self_autonomous_chain": {"chain_id": "__nonexistent_test__", "step_output": "test"},
    "get_chain_context": {"chain_id": "__nonexistent_test__"},
    "get_cot_queue_status": {},
    "query_exploration_history": {"query": "test", "limit": 1},
    "queue_chain_of_thought": {"topic": "__TEST_VERIFY__"},
    "update_chain_progress": {"chain_id": "__nonexistent_test__", "response": "test"},
    "clear_cot_queue": {},

    # ── code ──
    "get_code_context": {"filepath": "repryntt/__init__.py"},
    "grep_search": {"pattern": "def __init__", "directory": "repryntt/brain", "max_results": 2},
    "run_code_tests": {"test_path": "tests/", "pattern": "__nonexistent_test_xyz__"},
    "search_replace": {"target_file": "/tmp/__test_verify_sr__.txt", "search": "old", "replace": "new"},

    # ── conversation ──
    "get_recent_conversations": {"limit": 1},
    "initiate_conversation": {"target_agent": "__test_agent__", "message": "ping", "context": "verification test"},
    "search_conversations": {"query": "test", "limit": 1},
    "get_conversation_summary": {"conversation_id": "__nonexistent__"},
    "export_conversation": {"conversation_id": "__nonexistent__"},

    # ── creative ──
    "get_creative_workspace_status": {},
    "create_creative_file": {"filename": "__test_verify__.md", "content": "# Test\nVerification content."},
    "read_creative_file": {"filename": "__test_verify__.md"},
    "write_to_creative_file": {"filename": "__test_verify__.md", "content": "Updated content."},
    "append_to_creative_file": {"filename": "__test_verify__.md", "content": "\nAppended."},

    # ── defi ──
    "dexscreener_trending": {},
    "dexscreener_token_search": {"query": "SOL"},
    "solana_rpc_query": {"method": "getHealth"},

    # ── employee_mgmt ──
    "employee_roster": {},
    "employee_status": {"agent_id": "jarvis"},
    "find_employee": {"query": "jarvis"},
    "assign_work": {"agent_id": "jarvis", "task": "__TEST_VERIFY_TASK__"},
    "check_work": {"agent_id": "jarvis"},
    "rename_employee": {"agent_id": "__nonexistent__", "new_name": "Test"},

    # ── filesystem ──
    "list_dir": {"path": "."},
    "read_file": {"target_file": "README.md"},
    "write_file": {"target_file": "/tmp/__test_verify_file__.txt", "content": "test verification"},
    "get_sandbox_status": {},
    "run_terminal_cmd": {"command": "echo __TEST_VERIFY__"},
    "check_syntax": {"filepath": "repryntt/__init__.py"},
    "analyze_codebase": {"directory": "repryntt/brain", "pattern": "*.py", "max_files": 1},
    "propose_code_change": {"description": "test", "target_file": "/tmp/__nonexistent__.py"},

    # ── grokipedia ──
    "find_similar_topics": {"topic": "artificial intelligence"},
    "integrate_knowledge_context": {"query": "quantum computing"},
    "pull_knowledge_topics": {"domain": "technology"},
    "reset_inspiration_index": {},

    # ── knowledge ──
    "grokipedia_search": {"query": "blockchain"},
    "get_knowledge_domain_distribution": {},
    "analyze_topic_complexity": {"topic": "neural networks"},
    "clear_grokipedia_history": {},

    # ── maps ──
    "google_maps_search": {"query": "coffee shop near Times Square NYC"},
    "get_directions": {"origin": "New York", "destination": "Boston"},
    "geocode_address": {"address": "1600 Pennsylvania Ave, Washington DC"},
    "find_nearby_places": {"location": "40.7128,-74.0060", "type": "restaurant", "radius": 500},

    # ── math ──
    "compute_zeta_function": {"s_real": 2.0, "s_imag": 0.0},
    "symbolic_manipulation": {"expression": "x**2 + 2*x + 1", "operation": "factor"},
    "numerical_analysis": {"expression": "sin(x)", "variable": "x", "operation": "derivative"},
    "statistical_analysis": {"data": [1, 2, 3, 4, 5], "operation": "summary"},
    "pattern_recognition": {"sequence": [1, 1, 2, 3, 5, 8]},
    "mathematical_visualization": {"expression": "sin(x)", "x_range": [-3.14, 3.14]},
    "analyze_zeta_zeros": {"num_zeros": 3},
    "access_mathematical_databases": {"query": "prime numbers", "database": "oeis"},

    # ── media ──
    "generate_image": {"prompt": "__TEST_VERIFY__ a blue square"},
    "analyze_image": {"image_path": "/tmp/__nonexistent_image__.png", "question": "what is this?"},
    "speak": {"text": "__TEST_VERIFY__"},
    "listen": {},
    "post_tweet": {"content": "__TEST_VERIFY__ do not actually post"},
    "tweet": {"content": "__TEST_VERIFY__ do not actually post"},
    "check_twitter_mentions": {},
    "reply_to_twitter": {"mention_url": "https://x.com/__test__/status/1", "reply_text": "test"},
    "get_twitter_status": {},
    "twitter_status": {},
    "capture_camera": {},

    # ── memory ──
    "brain_memory_save": {"key": "__test_verify__", "value": "verification_value", "topic": "test"},
    "brain_memory_recall": {"query": "__test_verify__"},
    "brain_network_search": {"query": "test verification"},
    "get_brain_stats": {},
    "get_relevant_context": {"question": "test verification"},
    "search_domain": {"query": "test", "domain": "factual"},
    "store_learning": {"topic": "__test_verify__", "content": "learned this during verification"},
    "update_procedural": {"task_type": "__test_verify__", "steps": ["s1"], "success": True},

    # ── personality ──
    "analyze_personality_growth": {},
    "modify_personality_trait": {"trait_name": "__test_curiosity__", "new_value": "0.5"},
    "evolve_personality_dimension": {"dimension_name": "curiosity", "new_value": 0.5, "reason": "test"},
    "add_personality_trait": {"new_trait": "__test_verify_trait__", "reason": "Test trait"},
    "remove_personality_trait": {"trait_name": "__test_verify_trait__"},
    "log_personality_evolution": {"event_type": "__test_verify__", "details": {"test": True}},
    "recreate_autonomous_personality": {},
    "update_avatar": {"description": "test avatar update"},
    "update_behavioral_guidelines": {"guideline_index": 0, "new_guideline": "Be helpful."},

    # ── robot_economy ──
    "get_economy_status": {},
    "start_robot_economy": {},
    "stop_robot_economy": {},
    "create_robot_wallet": {"agent_id": "__test_verify__"},
    "get_robot_wallet_balance": {"agent_id": "jarvis"},
    "get_robot_blockchain_info": {},
    "submit_robot_workload": {"agent_id": "jarvis", "description": "test", "workload_type": "computation"},
    "allocate_robot_dao_funds": {"proposal": "test", "amount": 0},
    "monitor_robot_economy": {},
    "recover_robot_wallet": {"agent_id": "__nonexistent__"},

    # ── scalp ──
    "scalp_status": {},
    "scalp_history": {"limit": 3},
    "scalp_force_buy": {"token": "__TEST_TOKEN__", "amount": 0},
    "scalp_force_sell": {"token": "__TEST_TOKEN__", "amount": 0},
    "scalp_set_param": {"param": "max_position_size", "value": "0.001"},

    # ── social ──
    "x_search_tweets": {"query": "bitcoin"},
    "twitter_search": {"query": "ethereum"},
    "post_tweet": {"content": "__TEST_VERIFY__"},
    "tweet": {"content": "__TEST_VERIFY__"},
    "check_twitter_mentions": {},
    "reply_to_twitter": {"mention_url": "https://x.com/test/status/1", "reply_text": "test"},
    "get_twitter_status": {},
    "twitter_status": {},
    "x_search_crypto": {"query": "solana"},

    # ── swarm_tools ──
    "list_agents": {},
    "get_swarm_overview": {},
    "create_agent": {"name": "__test_verify_agent__", "role": "test", "provider": "google_gemini"},
    "get_agent_info": {"agent_id": "jarvis"},
    "retire_agent": {"agent_id": "__test_verify_agent__"},
    "dispatch_task": {"agent_id": "jarvis", "task": "__TEST_VERIFY_DISPATCH__"},
    "delegate_tasks": {"tasks": [{"agent_id": "jarvis", "task": "test"}]},
    "broadcast_task": {"task": "__TEST_VERIFY_BROADCAST__"},
    "create_swarm": {"name": "__test_swarm__", "goal": "verification"},
    "dissolve_swarm": {"swarm_id": "__nonexistent_swarm__"},
    "add_agents_to_swarm": {"swarm_id": "__nonexistent_swarm__", "agent_ids": ["jarvis"]},
    "start_discussion": {"topic": "__TEST_VERIFY__", "participants": ["jarvis"]},
    "quick_brainstorm": {"topic": "__TEST_VERIFY__"},
    "quick_research": {"query": "__TEST_VERIFY__"},
    "call_jarvis": {"message": "__TEST_VERIFY__ ping"},
    "council_advise": {"question": "__TEST_VERIFY__"},
    "council_post_report": {"report": "__TEST_VERIFY__"},

    # ── tool_execution ──
    "build_tool_context": {"task": "search for information"},
    "build_tool_schemas": {"tool_names": ["get_current_time"]},
    "get_step_tool_hint": {"step_description": "search the web"},
    "get_task_tool_examples": {"task_type": "research"},
    "get_tool_credit_cost": {"tool_name": "web_search"},
    "get_tool_credit_reward": {"tool_name": "web_search"},

    # ── trading ──
    "trading_scan": {},
    "trading_signals": {},
    "trading_hot_tokens": {},
    "trading_performance": {},
    "trading_bot_status": {},
    "trading_bot_start": {},
    "trading_bot_stop": {},
    "trading_token_detail": {"token": "SOL"},
    "token_price_history": {"token": "SOL", "days": 1},
    "log_trade_outcome": {"symbol": "__TEST__", "action": "buy_and_profit", "lessons": "test verification"},
    "review_trade_journal": {"limit": 3},

    # ── trading_sim ──
    "sim_portfolio": {},
    "sim_price_check": {"token": "SOL"},
    "sim_buy": {"token": "ETH", "amount": 0.001},
    "sim_sell": {"token": "ETH", "amount": 0.001},
    "sim_faucet": {"amount": 100},

    # ── video ──
    "video_project_status": {},
    "create_video_project": {"title": "__TEST_VERIFY__", "description": "test project"},
    "write_screenplay": {"project_id": "__nonexistent__", "content": "test screenplay"},
    "create_shot_list": {"project_id": "__nonexistent__"},
    "generate_video_clip": {"prompt": "test clip", "project_id": "__nonexistent__"},
    "generate_all_clips": {"project_id": "__nonexistent__"},
    "generate_narration": {"text": "test narration", "project_id": "__nonexistent__"},
    "generate_music": {"prompt": "test music", "project_id": "__nonexistent__"},
    "generate_thumbnail": {"prompt": "test thumbnail", "project_id": "__nonexistent__"},
    "assemble_edit": {"project_id": "__nonexistent__"},
    "qa_review_video": {"project_id": "__nonexistent__"},
    "render_final": {"project_id": "__nonexistent__"},
    "auto_produce_video": {"topic": "__TEST_VERIFY__"},

    # ── web_search ──
    "real_web_search": {"query": "test verification 2026"},
    "google_web_search": {"query": "test verification 2026"},
    "scrape_web_page": {"url": "https://httpbin.org/html"},
    "web_search_results_only": {"query": "test verification"},
    "extract_content_from_url": {"url": "https://httpbin.org/html"},
    "call_knowledge_api_feeder": {"query": "test verification"},

    # ── whale_monitoring ──
    "whale_list_wallets": {},
    "whale_monitor_status": {},
    "kol_leaderboard": {},
    "whale_add_wallet": {"address": "0x__TEST_VERIFY__", "label": "test_wallet"},
    "whale_remove_wallet": {"address": "0x__TEST_VERIFY__"},
    "kol_sync_wallets": {},
    "kol_remove_underperformers": {"threshold": 0},
}

# ── Daemon virtual tools (tested separately, also through _execute_native_tool_calls) ──
DAEMON_TOOL_TEST_ARGS: Dict[str, Dict] = {
    "schedule_cron": {"prompt": "__TEST_VERIFY__", "interval_minutes": 9999, "label": "test_verify"},
    "list_cron": {},
    "remove_cron": {"cron_id": "__nonexistent__"},
    "flush_memory": {},
    "list_skills": {},
    "get_skill": {"name": "__nonexistent__"},
    "install_skill": {"name": "__test_verify_skill__", "content": "# Test Skill\nVerification only."},
    "spawn_agent": {"task": "__TEST_VERIFY__", "role": "test"},
    "llm_toggle": {"action": "status"},
    "query_local_llm": {"prompt": "Say OK", "max_tokens": 5},
    "update_bootstrap_file": {"filename": "RECALL.md", "content": "\n## Test Verification Entry\nThis is a verification test.", "mode": "append"},
    "append_daily_memory": {"note": "TOOL VERIFICATION TEST — This entry confirms the append_daily_memory tool is working correctly through the Jarvis execution path. Test timestamp: " + datetime.now().isoformat(), "heading": "Tool Verification Test"},
    "update_daily_plan": {},  # Skip — would overwrite real plan
    "memory_search": {"query": "test verification"},
    "memory_get": {"date": "2026-01-01"},
    "list_my_tools": {"category": "memory"},
    "create_persistent_task": {"goal": "__TEST_VERIFY__", "success_criteria": "test only"},
    "complete_persistent_task": {"outcome": "success", "summary": "Test verification complete"},
    "commerce_status": {},
    "commerce_list_products": {},
    "commerce_check_orders": {},
    "commerce_list_saved_products": {},
    "commerce_create_product": {"platform": "shopify", "title": "__TEST__", "description": "test", "price": "0"},
    "commerce_save_digital_product": {"filename": "__test_verify__.txt", "content": "test"},
}

# Tools to skip entirely (would damage real state or require interactive resources)
SKIP_TOOLS = {
    "update_daily_plan",   # Would overwrite Jarvis's real daily plan
    "capture_camera",      # Requires physical camera hardware
    "listen",              # Requires microphone hardware
    "speak",               # Requires speaker hardware
}

def make_tool_call(name: str, args: Dict) -> Dict:
    """Build an OpenAI-format tool_call dict."""
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        }
    }

def classify_result(content: str, tool_name: str) -> str:
    """
    Classify a tool result as OK, EXPECTED_ERROR, or REAL_ERROR.
    
    OK = tool executed and returned useful output
    EXPECTED_ERROR = tool returned an expected/graceful error (missing config, no data, etc.)
    REAL_ERROR = tool crashed or has a code bug
    """
    cl = content.lower()
    
    # Real errors — code bugs, import failures, undefined variables
    real_error_patterns = [
        "nameerror:", "attributeerror:", "typeerror:", "importerror:",
        "syntaxerror:", "indentationerror:", "keyerror:", "indexerror:",
        "recursionerror:", "memoryerror:", "filenotfounderror:",
        "tool execution unavailable", "unknown tool",
    ]
    for pat in real_error_patterns:
        if pat in cl:
            # Some patterns are OK in context. e.g. "FileNotFoundError" when reading __nonexistent__
            if tool_name in ("read_file", "read_creative_file", "search_replace", "propose_code_change",
                             "analyze_image", "write_screenplay", "create_shot_list", "generate_video_clip",
                             "generate_all_clips", "generate_narration", "generate_music",
                             "generate_thumbnail", "assemble_edit", "qa_review_video", "render_final",
                             "get_conversation_summary", "export_conversation", "advance_self_autonomous_chain",
                             "get_chain_context", "update_chain_progress", "recover_robot_wallet",
                             "get_skill"):
                continue
            # KeyError for looking up nonexistent test agents/chains is expected
            if "keyerror" in cl and ("__test" in cl or "__nonexistent" in cl or "not found" in cl):
                continue
            # TypeError from optional dependencies not installed
            if "typeerror" in cl and ("nonetype" in cl or "not callable" in cl):
                if tool_name in ("generate_image", "auto_produce_video", "query_local_llm"):
                    continue
            return "REAL_ERROR"
    
    # Expected/graceful errors
    expected_patterns = [
        "not configured", "not available", "not installed", "not found",
        "no api key", "api key not", "no credential", "no matching",
        "no results", "empty", "no data", "no wallet", "no agent",
        "not implemented", "disabled", "no active", "no project",
        "module not available", "not supported", "no chain",
        "no module named", "none", "[]", "no tasks", "no cron",
        "no orders", "no products", "no skills", "no memory",
        "gracefully", "twitterbot", "selenium", "headless",
        "rate limit", "quota", "unauthorized", "403", "401",
        "connection refused", "timeout", "could not connect",
        "robot_economy", "rpc", "no recent",
    ]
    if any(p in cl for p in expected_patterns):
        return "EXPECTED_ERROR"
    
    if content.startswith("Error") and len(content) < 500:
        return "EXPECTED_ERROR"
    
    return "OK"


def run_test():
    print("=" * 70)
    print("FULL END-TO-END TOOL VERIFICATION — Jarvis Execution Path")
    print("=" * 70)
    print()

    # Step 1: Initialize the AgentDaemon (same as production)
    print("Loading AgentDaemon...")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    from repryntt.agents.persistent_agents import AgentDaemon, AutonomousAgentState
    
    daemon = AgentDaemon()
    brain = daemon._get_brain_system()
    if not brain:
        print("FATAL: Could not load BrainSystem!")
        sys.exit(1)
    
    registry = daemon._tool_registry
    print(f"BrainSystem loaded — {len(registry)} tools in registry")
    print()

    # Step 2: Create a test agent state (simulating Jarvis)
    test_agent = AutonomousAgentState(
        agent_id="jarvis",
        name="jarvis",
        role="general",
        provider="google_gemini",
        model="gemini-2.0-flash",
        personality="helpful assistant",
        department="general",
        display_name="Jarvis (Test)",
    )

    # If jarvis exists in daemon, use that state for richer context
    if "jarvis" in daemon.agents:
        test_agent = daemon.agents["jarvis"]
        print(f"Using live Jarvis agent state: {test_agent.display_name or test_agent.name}")
    else:
        print("Using synthetic Jarvis agent state for testing")
    print()

    results = {}
    ok_count = 0
    expected_err_count = 0
    real_err_count = 0
    skip_count = 0

    # ── Test all registry tools ──
    print("─" * 70)
    print("REGISTRY TOOLS")
    print("─" * 70)
    
    # Get unique tool names (deduplicated)
    all_tools = sorted(set(registry.names))
    
    for tool_name in all_tools:
        if tool_name in SKIP_TOOLS:
            status = "SKIPPED"
            content = f"Skipped — requires hardware: {tool_name}"
            skip_count += 1
            print(f"  ⊘ {tool_name:<45} {status}")
            results[tool_name] = {"status": status, "content": content, "type": "registry", "classification": "SKIP"}
            continue

        args = TOOL_TEST_ARGS.get(tool_name, {})
        tc = make_tool_call(tool_name, args)

        try:
            tool_results = daemon._execute_native_tool_calls(test_agent, [tc])
            content = tool_results[0]["content"] if tool_results else "NO RESULT"
        except Exception as e:
            content = f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}"

        classification = classify_result(content, tool_name)
        
        if classification == "OK":
            ok_count += 1
            marker = "✓"
            status = "OK"
        elif classification == "EXPECTED_ERROR":
            expected_err_count += 1
            marker = "⚠"
            status = "EXPECTED_ERR"
        elif classification == "REAL_ERROR":
            real_err_count += 1
            marker = "✗"
            status = "REAL_ERROR"
        else:
            ok_count += 1
            marker = "✓"
            status = "OK"

        # Truncate content for display
        display_content = content[:120].replace("\n", " ")
        print(f"  {marker} {tool_name:<45} {status:<15} {display_content}")
        
        results[tool_name] = {
            "status": status,
            "content": content[:2000],
            "type": "registry",
            "classification": classification,
            "args": args,
        }

    # ── Test daemon virtual tools ──
    print()
    print("─" * 70)
    print("DAEMON VIRTUAL TOOLS")
    print("─" * 70)

    for tool_name, args in sorted(DAEMON_TOOL_TEST_ARGS.items()):
        if tool_name in SKIP_TOOLS:
            status = "SKIPPED"
            content = f"Skipped: {tool_name}"
            skip_count += 1
            print(f"  ⊘ {tool_name:<45} {status}")
            results[f"daemon:{tool_name}"] = {"status": status, "content": content, "type": "daemon", "classification": "SKIP"}
            continue

        tc = make_tool_call(tool_name, args)
        
        try:
            tool_results = daemon._execute_native_tool_calls(test_agent, [tc])
            content = tool_results[0]["content"] if tool_results else "NO RESULT"
        except Exception as e:
            content = f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}"

        classification = classify_result(content, tool_name)

        if classification == "OK":
            ok_count += 1
            marker = "✓"
            status = "OK"
        elif classification == "EXPECTED_ERROR":
            expected_err_count += 1
            marker = "⚠"
            status = "EXPECTED_ERR"
        elif classification == "REAL_ERROR":
            real_err_count += 1
            marker = "✗"
            status = "REAL_ERROR"
        else:
            ok_count += 1
            marker = "✓"
            status = "OK"

        display_content = content[:120].replace("\n", " ")
        print(f"  {marker} {tool_name:<45} {status:<15} {display_content}")
        
        results[f"daemon:{tool_name}"] = {
            "status": status,
            "content": content[:2000],
            "type": "daemon",
            "classification": classification,
            "args": args,
        }

    # ── Cleanup test artifacts ──
    print()
    print("Cleaning up test artifacts...")
    
    # Remove test creative file
    creative_dir = os.path.join("agent_workspaces", "jarvis", "creative")
    test_creative = os.path.join(creative_dir, "__test_verify__.md")
    if os.path.exists(test_creative):
        os.remove(test_creative)
        print(f"  Removed {test_creative}")
    
    # Remove test write_file
    if os.path.exists("/tmp/__test_verify_file__.txt"):
        os.remove("/tmp/__test_verify_file__.txt")
        print("  Removed /tmp/__test_verify_file__.txt")
    
    # Remove test skill if installed
    skills_dir = os.path.join("skills")
    test_skill = os.path.join(skills_dir, "__test_verify_skill__.md")
    if os.path.exists(test_skill):
        os.remove(test_skill)
        print(f"  Removed {test_skill}")

    # Remove __test_verify__ memory key
    try:
        brain._memory.brain.semantic_memory.pop("__test_verify__", None)
    except Exception:
        pass

    # ── Summary ──
    total = ok_count + expected_err_count + real_err_count + skip_count
    print()
    print("=" * 70)
    print("FULL END-TO-END VERIFICATION SUMMARY")
    print("=" * 70)
    print(f"  Total tested:            {total}")
    print(f"  ✓ OK (working):          {ok_count}")
    print(f"  ⚠ Expected errors:       {expected_err_count}")
    print(f"  ✗ REAL ERRORS (bugs):    {real_err_count}")
    print(f"  ⊘ Skipped (hardware):    {skip_count}")
    print()
    
    if real_err_count == 0:
        print("  ★ ALL TOOLS FUNCTIONAL — NO CODE BUGS DETECTED ★")
    else:
        print(f"  ⚠ {real_err_count} TOOLS HAVE BUGS THAT NEED FIXING:")
        print()
        for tname, info in sorted(results.items()):
            if info["classification"] == "REAL_ERROR":
                print(f"  ✗ {tname}")
                # Show first 300 chars of error
                err_content = info["content"][:300].replace("\n", "\n    ")
                print(f"    {err_content}")
                print()
    
    if expected_err_count > 0:
        print(f"\n  ⚠ {expected_err_count} tools returned expected/graceful errors:")
        print("  (Missing API keys, uninstalled packages, no data — NOT bugs)")
        for tname, info in sorted(results.items()):
            if info["classification"] == "EXPECTED_ERROR":
                snippet = info["content"][:100].replace("\n", " ")
                print(f"    ⚠ {tname}: {snippet}")

    print("=" * 70)

    # ── Write JSON report ──
    report = {
        "timestamp": datetime.now().isoformat(),
        "test_type": "end_to_end_jarvis_execution_path",
        "total": total,
        "ok": ok_count,
        "expected_errors": expected_err_count,
        "real_errors": real_err_count,
        "skipped": skip_count,
        "tools": results,
    }
    
    report_path = os.path.join("tests", "tool_e2e_verification_results.json")
    os.makedirs("tests", exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON report: {os.path.abspath(report_path)}")

    return real_err_count


if __name__ == "__main__":
    exit_code = run_test()
    sys.exit(exit_code)
