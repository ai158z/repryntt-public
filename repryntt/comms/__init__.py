"""
repryntt.comms — Communications layer (OpenClaw integration point).

This is where OpenClaw gets consumed as a library dependency.
Handles all external I/O channels:
    - Channel gateway: Multi-platform routing (Telegram, Discord, SMS, email → core)
    - Webhooks: Inbound hook parsing, routing, rate limiting
    - Auth: Shared Flask auth/CORS/rate-limiting middleware

Migration source:
    - SAIGE/channel_gateway.py (~400 lines)
    - SAIGE/hooks/ (6 files, ~600 lines total)
    - SAIGE/saige_auth.py (~200 lines)
"""
