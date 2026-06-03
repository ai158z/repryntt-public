"""
repryntt.web — Web services and API endpoints.

Multiple web interfaces:
    - Chat server: 24/7 human-AI communication (port 4001)
    - External API: Credit-based authenticated access (port 8081)
    - Unified interface: Voice + system control UI (port 3000)
    - Tool API: Programmatic tool access (port 8083)
    - Nexus: Agent social network web frontend (port 8089)
    - Commerce: E-commerce platform integration

Migration source:
    - SAIGE/scripts/chat_server_simple.py (~500 lines)
    - SAIGE/scripts/saige_external_api.py (~500 lines)
    - SAIGE/unified_saige_interface.py (~500 lines)
    - SAIGE/start_tool_api_server.py (~300 lines)
    - SAIGE/ai_social_network/app.py (~2,500 lines)
    - SAIGE/saige_web/server.py (~500 lines)
    - SAIGE/brain/commerce_integration.py (~500 lines)
"""
