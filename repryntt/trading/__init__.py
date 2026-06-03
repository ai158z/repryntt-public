"""
repryntt.trading — Trading service.

Independently deployable trading engine for Solana memecoin operations:
    - Scalp executor: Automated micro-trades (TP/SL/timeout)
    - Whale monitor: Copy-trade from tracked Solana wallets via RPC
    - Signal scorer: Quality scoring for trading signals
    - Paper trading simulator: Practice with simulated capital + live prices
    - Gem hunter: Hourly research-driven long-hold strategy (Andrew-powered)
    - Micro-chain trader: Local LLM sequential trading pipeline
    - KOL scraper: Wallet discovery from kolscan.io leaderboard
    - Trading bot pipeline: Dashboard, token fetcher, monitor, trend agent

Migration source:
    - SAIGE/brain/scalp_executor.py (~500 lines)
    - SAIGE/brain/whale_monitor.py (~400 lines)
    - SAIGE/brain/signal_scorer.py (~300 lines)
    - SAIGE/brain/trading_simulator.py (~400 lines)
    - SAIGE/brain/jarvis_trading_engine.py (~500 lines)
    - SAIGE/brain/micro_chain_trader.py (~400 lines)
    - SAIGE/brain/andrew_gem_hunter.py (~500 lines)
    - SAIGE/brain/kolscan_scraper.py (~300 lines)
    - SAIGE/brain/trading_bot_bridge.py (~200 lines)
    - SAIGE/trading_bot/ (7 scripts, ~2K total)
"""
