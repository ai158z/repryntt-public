"""
repryntt.search — Knowledge and data acquisition service.

Unified search and data feeder system:
    - Knowledge router: Single entry point replacing all search-engine scraping
    - Feeder coordinator: Aggregates data from multiple sources into stimulus
    - Individual feeders: DDG, Google, RSS, web research, sensors, conversations

Migration source:
    - SAIGE/knowledge_router.py (~400 lines — unified search)
    - SAIGE/feeders/feeder_coordinator.py (~400 lines — aggregation)
    - SAIGE/feeders/curiosity_feeder.py (~300 lines)
    - SAIGE/feeders/google_search_scraper.py (~300 lines)
    - SAIGE/feeders/web_search_feeder.py (~200 lines)
    - SAIGE/feeders/web_research_feeder.py (~200 lines)
    - SAIGE/feeders/news_feeder.py (~200 lines)
    - SAIGE/feeders/conversation_feeder.py (~200 lines)
    - SAIGE/feeders/knowledge_api_feeder.py (~200 lines)
    - SAIGE/feeders/performance_feeder.py (~200 lines)
    - SAIGE/feeders/sensor_feeder.py (~200 lines)
    - SAIGE/feeders/start_all_feeders.py (~100 lines)
"""
