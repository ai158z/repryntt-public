# REPRYNTT STARTUP

Add short, explicit instructions for what REPRYNTT should do on boot.

## On System Start
1. Load AI configuration
2. Initialize agent registry
3. Build tool definitions
4. Start scheduler (if autonomous mode)
5. Start channel gateway (Telegram, Discord)
6. Flask server ready on port 8089

If a startup task sends a message, use the message tool and then reply
with NO_REPLY.
