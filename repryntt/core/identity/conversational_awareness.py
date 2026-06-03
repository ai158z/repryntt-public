"""
Physical Conversational Awareness System

Gives Artemis the ability to:
1. Detect humans nearby (camera + Gemini Vision)
2. Listen for wake words ("Artemis", "Hey Artemis")
3. Engage in real-time multi-turn voice conversations
4. Spontaneously initiate conversation when she has something to share

Architecture:
- AwarenessMonitor: background thread monitoring audio + camera
- ConversationSession: real-time listen → think → speak loop
- Both coordinate with the heartbeat scheduler via is_conversing flag
"""

import json
import logging
import os
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Callable

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.environ.get("REPRYNTT_BRAIN", str(Path.home() / ".repryntt" / "brain")))
AWARENESS_STATE_FILE = BRAIN_DIR / "conversational_awareness.json"
CONVERSATION_LOG_DIR = BRAIN_DIR / "conversations"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Presence State
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PresenceState:
    """Tracks whether someone is physically present nearby."""

    def __init__(self):
        self.someone_present: bool = False
        self.last_check: float = 0.0
        self.last_detected: float = 0.0
        self.confidence: float = 0.0
        self.description: str = ""
        self.consecutive_empty: int = 0

    def update(self, detected: bool, confidence: float = 0.0, description: str = ""):
        self.last_check = time.time()
        if detected:
            self.someone_present = True
            self.last_detected = time.time()
            self.confidence = confidence
            self.description = description
            self.consecutive_empty = 0
        else:
            self.consecutive_empty += 1
            # Only mark absent after 2 consecutive empty checks (avoid false negatives)
            if self.consecutive_empty >= 2:
                self.someone_present = False
                self.confidence = 0.0
                self.description = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Perception Buffer (feeds into heartbeat brain flow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PerceptionBuffer:
    """Collects perception events between heartbeats.

    Events are accumulated here and drained by the heartbeat prompt builder,
    so Artemis 'remembers' what she saw/heard/said between cycles.
    """

    MAX_EVENTS = 20

    def __init__(self):
        self._events: List[Dict] = []
        self._lock = threading.Lock()

    def add(self, event_type: str, summary: str):
        """Record a perception event (e.g. 'conversation', 'presence', 'audio')."""
        with self._lock:
            self._events.append({
                "type": event_type,
                "summary": summary,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            # Keep bounded
            if len(self._events) > self.MAX_EVENTS:
                self._events = self._events[-self.MAX_EVENTS:]

    def drain(self) -> List[Dict]:
        """Return and clear all buffered events (called by heartbeat builder)."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
            return events

    def peek(self) -> List[Dict]:
        """Read events without clearing."""
        with self._lock:
            return list(self._events)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Conversation Session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConversationSession:
    """Manages a single real-time multi-turn voice conversation."""

    MAX_TURNS = 30
    SILENCE_TIMEOUT_ROUNDS = 5   # exit after 5 rounds of no speech
    LISTEN_DURATION = 15         # max seconds per listen (VAD stops earlier)

    EXIT_PHRASES = frozenset({
        "goodbye", "bye", "that's all", "thanks", "thank you",
        "nevermind", "never mind", "stop", "go away",
        "see you", "later", "done", "end conversation",
    })

    def __init__(self, trigger: str, trigger_context: str = ""):
        self.trigger = trigger            # wake_word | presence | spontaneous | manual
        self.trigger_context = trigger_context
        self.start_time = time.time()
        self.turns: List[Dict] = []
        self.active = False
        self.session_id = f"conv_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def add_turn(self, role: str, text: str):
        self.turns.append({
            "role": role,
            "text": text,
            "timestamp": time.time(),
        })

    def get_conversation_messages(self, system_prompt: str) -> List[Dict]:
        """Build OpenAI-compatible messages array for API call."""
        messages = [{"role": "system", "content": system_prompt}]
        for turn in self.turns:
            role = "assistant" if turn["role"] == "artemis" else "user"
            messages.append({"role": role, "content": turn["text"]})
        return messages

    def should_exit(self, user_text: str) -> bool:
        lower = user_text.lower().strip()
        return any(phrase in lower for phrase in self.EXIT_PHRASES)

    def duration(self) -> float:
        return time.time() - self.start_time

    def to_log_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "trigger": self.trigger,
            "trigger_context": self.trigger_context,
            "start_time": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat(),
            "duration_seconds": round(self.duration(), 1),
            "turn_count": len(self.turns),
            "turns": self.turns,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Conversational Awareness (coordinator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConversationalAwareness:
    """
    Coordinator for physical conversational awareness.

    Manages:
    - Wake word monitoring (background audio checks)
    - Presence detection (periodic camera + Gemini analysis)
    - Real-time conversation sessions
    - Spontaneous conversation triggers

    Integration:
    - daemon checks .is_conversing to skip heartbeats while in conversation
    - Uses daemon._call_api() for conversation turns (via think_fn callback)
    - Uses media.py speak() / listen() for physical I/O
    """

    WAKE_WORDS = ["andrew", "hey andrew", "yo andrew", "artemis", "hey artemis", "yo artemis"]
    WAKE_CHECK_INTERVAL = 8.0       # seconds between audio checks
    WAKE_LISTEN_DURATION = 4        # seconds per wake word recording
    PRESENCE_CHECK_INTERVAL = 120.0  # seconds between camera checks
    SPONTANEOUS_COOLDOWN = 1800.0    # 30 min between spontaneous conversations
    SPONTANEOUS_MIN_PRESENCE = 60.0  # person must be present 60s before spontaneous

    def __init__(self):
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._conversation_active = False
        self._conversation_lock = threading.Lock()
        self._current_session: Optional[ConversationSession] = None
        self.suppress_spontaneous = False  # Set by daemon when a chain is active

        # Physical state
        self.presence = PresenceState()
        self.perception = PerceptionBuffer()
        self._last_spontaneous: float = 0.0
        self._last_wake_check: float = 0.0
        self._conversations_today: int = 0
        self._total_conversations: int = 0

        # Callbacks (set by daemon via configure())
        self._speak_fn: Optional[Callable] = None       # speak(text=...) -> json str
        self._listen_fn: Optional[Callable] = None       # listen(duration=...) -> json str
        self._think_fn: Optional[Callable] = None        # think(messages) -> str or None
        self._think_with_tools_fn: Optional[Callable] = None  # think_with_tools(messages) -> (str, tool_results) or None
        self._capture_fn: Optional[Callable] = None      # capture(analyze, question) -> json str
        self._get_identity_context: Optional[Callable] = None
        self._get_personality_snippet: Optional[Callable] = None
        self._get_world_context: Optional[Callable] = None
        self._get_capability_context: Optional[Callable] = None
        self._action_queue_fn: Optional[Callable] = None  # action_queue(ai_response, human_input)

        self._load_state()

    # ── Configuration ─────────────────────────────────────────────

    def configure(self, *,
                  speak_fn: Callable,
                  listen_fn: Callable,
                  think_fn: Callable,
                  think_with_tools_fn: Optional[Callable] = None,
                  capture_fn: Optional[Callable] = None,
                  identity_fn: Optional[Callable] = None,
                  personality_fn: Optional[Callable] = None,
                  world_fn: Optional[Callable] = None,
                  capability_context_fn: Optional[Callable] = None,
                  action_queue_fn: Optional[Callable] = None):
        """Wire up callbacks from the daemon."""
        self._speak_fn = speak_fn
        self._listen_fn = listen_fn
        self._think_fn = think_fn
        self._think_with_tools_fn = think_with_tools_fn
        self._capture_fn = capture_fn
        self._get_identity_context = identity_fn
        self._get_personality_snippet = personality_fn
        self._get_world_context = world_fn
        self._get_capability_context = capability_context_fn
        self._action_queue_fn = action_queue_fn

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self):
        """Start the awareness monitor thread."""
        if self._running:
            logger.warning("ConversationalAwareness: already running, skipping start")
            return
        if not self._speak_fn or not self._listen_fn or not self._think_fn:
            logger.warning("ConversationalAwareness: callbacks not configured, skipping start")
            return

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="awareness-monitor"
        )
        self._monitor_thread.start()
        # Use root logger too, since module logger may be disconnected after restart
        logging.getLogger("repryntt.daemon").info(
            "🎧 Conversational awareness monitor thread started"
        )
        logger.info("🎧 Conversational awareness monitor started")

    def stop(self):
        """Stop the awareness monitor."""
        self._running = False
        if self._current_session and self._current_session.active:
            self._current_session.active = False
        self._save_state()
        logger.info("🎧 Conversational awareness monitor stopped")

    @property
    def is_conversing(self) -> bool:
        return self._conversation_active

    def get_status(self) -> Dict:
        return {
            "running": self._running,
            "conversation_active": self._conversation_active,
            "someone_present": self.presence.someone_present,
            "presence_confidence": self.presence.confidence,
            "presence_description": self.presence.description,
            "conversations_today": self._conversations_today,
            "total_conversations": self._total_conversations,
            "current_session": (self._current_session.session_id
                                if self._current_session else None),
            "pending_perceptions": len(self.perception.peek()),
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Monitor Loop
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _monitor_loop(self):
        """Background thread: alternates between wake-word and presence checks."""
        logger.info("🎧 Awareness monitor loop entered")

        # Let the daemon fully start before we begin monitoring
        for _ in range(10):
            if not self._running:
                return
            time.sleep(1)

        while self._running:
            try:
                # Pause monitoring while already in conversation
                if self._conversation_active:
                    time.sleep(2)
                    continue

                now = time.time()

                # ── Presence check (camera, less frequent) ──
                if (self._capture_fn
                        and now - self.presence.last_check >= self.PRESENCE_CHECK_INTERVAL):
                    self._check_presence()

                # ── Wake word check (audio, frequent) ──
                if now - self._last_wake_check >= self.WAKE_CHECK_INTERVAL:
                    wake_text = self._check_wake_word()
                    if wake_text:
                        # Visual confirmation — make sure a real person is there
                        if not self._confirm_human_present():
                            logger.info("🎧 Wake word heard but no person visible — ignoring "
                                        "(likely TV/video/audio playback)")
                            self.perception.add("audio_rejected",
                                                f"Heard '{wake_text[:60]}' but camera showed no person")
                            continue
                        context = self._extract_post_wake_text(wake_text)
                        self._initiate_conversation("wake_word", context)
                        continue

                # ── Spontaneous conversation check ──
                if (self.presence.someone_present
                        and not self.suppress_spontaneous
                        and now - self._last_spontaneous >= self.SPONTANEOUS_COOLDOWN
                        and now - self.presence.last_detected >= self.SPONTANEOUS_MIN_PRESENCE):
                    if self._should_speak_spontaneously():
                        # Re-confirm presence with fresh camera check before talking
                        if not self._confirm_human_present():
                            logger.info("👤 Spontaneous skipped — stale presence, no person visible now")
                            continue
                        topic = self._pick_spontaneous_topic()
                        if topic:
                            self._initiate_conversation("spontaneous", topic)
                            continue

                time.sleep(1)

            except Exception as e:
                logger.error(f"Awareness monitor error: {e}", exc_info=True)
                time.sleep(5)

        logger.info("🎧 Awareness monitor loop exiting")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Wake Word Detection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _check_wake_word(self) -> Optional[str]:
        """Record a short clip and check for wake words via Whisper."""
        self._last_wake_check = time.time()

        try:
            result_json = self._listen_fn(duration=str(self.WAKE_LISTEN_DURATION))
            result = json.loads(result_json) if isinstance(result_json, str) else result_json

            if result.get("silence") or result.get("error"):
                return None

            text = result.get("text", "").lower().strip()
            if not text:
                return None

            for wake_word in self.WAKE_WORDS:
                if wake_word in text:
                    logger.info(f"🎯 Wake word detected in: '{text}'")
                    return result.get("text", "").strip()   # return original case

            return None

        except Exception as e:
            logger.debug(f"Wake word check failed: {e}")
            return None

    def _extract_post_wake_text(self, full_text: str) -> str:
        """Extract what the user said after the wake word."""
        lower = full_text.lower()
        for wake_word in self.WAKE_WORDS:
            idx = lower.find(wake_word)
            if idx >= 0:
                after = full_text[idx + len(wake_word):].strip()
                after = after.lstrip(",. ")
                if after:
                    return after
        return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Presence Detection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _check_presence(self):
        """Capture camera frame and ask Gemini who/what is visible — humans
        AND animals (Andrew lives with 5 cats and a dog; see HOUSEHOLD.md).
        Uses save=False so presence checks don't accumulate images on disk."""
        try:
            result_json = self._capture_fn(
                analyze=True,
                question=(
                    "You are the visual cortex of an embodied AI named Andrew "
                    "who lives in a home with one human (Nate), five cats "
                    "(Igor — all white; Sunny — all black; Stubbs — tabby "
                    "with white markings; Baby — small all-tabby no white; "
                    "Borris — large all-tabby no white) and one dog (Toby, "
                    "a black Labrador). Describe who/what is visible in this "
                    "frame.\n\n"
                    "Respond on TWO lines:\n"
                    "Line 1: PERSON: yes/no — and if yes, one sentence on what they're doing.\n"
                    "Line 2: ANIMALS: comma-separated list of animals you see, "
                    "using best-guess names from the descriptions above (e.g. "
                    "'Igor sleeping on the rug, Toby walking past'). If no "
                    "animals are visible, say 'none'. If you see an animal "
                    "but can't identify which one, say 'cat' or 'dog' generically."
                ),
                save=False,
            )
            result = json.loads(result_json) if isinstance(result_json, str) else result_json

            if result.get("error"):
                logger.debug(f"Presence check failed: {result['error']}")
                self.presence.update(False)
                return

            analysis_raw = result.get("analysis", "") or ""
            analysis = analysis_raw.lower()

            # ── Parse the structured PERSON: / ANIMALS: response ──
            person_line = ""
            animals_line = ""
            for line in analysis.splitlines():
                line = line.strip()
                if line.startswith("person:"):
                    person_line = line[len("person:"):].strip()
                elif line.startswith("animals:"):
                    animals_line = line[len("animals:"):].strip()

            # Fallback: if structured parse failed, treat whole blob as person line
            if not person_line and not animals_line:
                person_line = analysis

            # ── Person detection (preserved behavior) ──
            person_detected = any(w in person_line for w in [
                "yes", "person", "human", "man", "woman",
                "someone", "people", "sitting", "standing",
            ])
            if any(w in person_line for w in [
                "no person", "no one", "empty", "nobody",
                "no human", "unoccupied", "no,",
            ]) or person_line.strip().startswith("no"):
                person_detected = False

            # ── Animal detection ──
            animal_keywords = [
                "igor", "sunny", "stubbs", "baby", "borris",
                "toby", "cat", "kitty", "kitten", "dog", "puppy",
            ]
            animals_detected = (
                animals_line
                and animals_line not in ("none", "n/a", "none.", "")
                and any(w in animals_line for w in animal_keywords)
            )

            confidence = 0.85 if person_detected else 0.0
            was_present = self.presence.someone_present
            self.presence.update(person_detected, confidence, analysis_raw)

            # ── Emit perception events ──
            if person_detected and not was_present:
                logger.info(f"👤 Person detected: {analysis_raw[:120]}")
                self.perception.add("presence_detected", analysis_raw[:240])
            elif not person_detected and was_present:
                logger.info("👤 Room appears empty")
                self.perception.add("presence_lost", "Room appears empty")

            # Animal events fire independently of person presence — they're
            # housemates in their own right, not noise around the human.
            if animals_detected:
                # Track last seen so we don't spam the buffer every 2 minutes
                # for the same sleeping cat. Only re-fire if list changed.
                last_seen = getattr(self, "_last_animals_seen", "")
                if animals_line != last_seen:
                    logger.info(f"🐾 Animal(s) detected: {animals_line[:120]}")
                    self.perception.add(
                        "animal_detected",
                        f"Saw: {animals_line[:200]}",
                    )
                    self._last_animals_seen = animals_line
            else:
                # Reset tracker so re-appearance counts as a new event
                if getattr(self, "_last_animals_seen", ""):
                    self._last_animals_seen = ""

        except Exception as e:
            logger.debug(f"Presence check error: {e}")
            self.presence.update(False)

    def _confirm_human_present(self) -> bool:
        """Quick camera check to confirm a real person is physically present.

        Used to gate wake-word and spontaneous conversations — prevents
        false triggers from TV audio, video playback, or other non-human sounds.
        Returns True only if Gemini confirms a real person in the frame.
        """
        if not self._capture_fn:
            # No camera available — fall through (don't block conversations entirely)
            return True

        try:
            result_json = self._capture_fn(
                analyze=True,
                question=(
                    "Is there a real person physically present in this room? "
                    "Not a person on a screen, TV, or video — a real human being. "
                    "Answer: 'yes_person' if a real person is visible, "
                    "'no_screen' if you only see a screen/TV/laptop showing people, "
                    "'no_empty' if the room appears empty."
                ),
                save=False,
            )
            result = json.loads(result_json) if isinstance(result_json, str) else result_json

            if result.get("error"):
                logger.debug(f"Visual confirmation failed: {result['error']}")
                return True  # Don't block on camera errors

            analysis = (result.get("analysis", "") or "").lower()

            # Explicit negative — room is empty
            if any(phrase in analysis for phrase in [
                "no_empty", "empty room", "no person", "no one", "nobody",
                "room appears empty", "no humans",
            ]):
                # But check if they ALSO mention a real person
                if "yes_person" in analysis or "real person" in analysis:
                    return True
                return False

            # Screen-only detection — audio was from a screen, not a real person
            # Only reject if analysis explicitly says the ONLY source is a screen
            # and there's NO real person present.
            screen_only_phrases = ["no_screen", "only.*screen", "only.*tv",
                                   "only.*video", "no real person"]
            if any(re.search(p, analysis) for p in screen_only_phrases):
                if "yes_person" in analysis or "real person" in analysis:
                    return True
                return False

            # Positive confirmation — any person detected
            if any(w in analysis for w in [
                "yes_person", "yes", "person", "human", "real",
                "someone", "sitting", "standing", "lying",
            ]):
                return True

            return False

        except Exception as e:
            logger.debug(f"Visual confirmation error: {e}")
            return True  # Don't block on errors

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Spontaneous Conversation
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _should_speak_spontaneously(self) -> bool:
        """Probability-gated check (5-25 %, ramping with idle time)."""
        import random
        time_factor = min((time.time() - self._last_spontaneous) / 7200.0, 1.0)
        probability = 0.05 + (0.20 * time_factor)
        return random.random() < probability

    def _pick_spontaneous_topic(self) -> Optional[str]:
        """Gather personality / world context for the AI to riff on."""
        snippets: List[str] = []

        if self._get_personality_snippet:
            try:
                p = self._get_personality_snippet()
                if p:
                    snippets.append(p)
            except Exception:
                pass

        if self._get_world_context:
            try:
                w = self._get_world_context()
                if w:
                    snippets.append(w)
            except Exception:
                pass

        return "\n".join(snippets)[:2000] if snippets else None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Conversation Session
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _initiate_conversation(self, trigger: str, context: str = ""):
        """Acquire conversation lock, run conversation, then release."""
        if self._conversation_active:
            return

        with self._conversation_lock:
            if self._conversation_active:
                return
            self._conversation_active = True

        try:
            session = ConversationSession(trigger, context)
            session.active = True
            self._current_session = session

            logger.info(f"💬 Conversation started (trigger={trigger})")
            self._run_conversation(session)

        except Exception as e:
            logger.error(f"Conversation failed: {e}", exc_info=True)
        finally:
            with self._conversation_lock:
                self._conversation_active = False
            if self._current_session:
                self._current_session.active = False

            self._last_spontaneous = time.time()
            self._conversations_today += 1
            self._total_conversations += 1

            if self._current_session:
                self._log_conversation(self._current_session)
                # Feed conversation summary into perception buffer for heartbeat
                self._buffer_conversation_summary(self._current_session)

            self._current_session = None
            self._save_state()
            logger.info("💬 Conversation ended — heartbeats resuming")

    # ── Core conversation loop ────────────────────────────────────

    def _run_conversation(self, session: ConversationSession):
        """The real-time listen → think → speak loop."""

        # Build conversational system prompt
        identity = ""
        if self._get_identity_context:
            try:
                identity = self._get_identity_context()
            except Exception:
                pass

        # ── Visual context — see who we're talking to ──
        visual_context = ""
        if self._capture_fn:
            try:
                vis_json = self._capture_fn(
                    analyze=True,
                    question=(
                        "Describe what you see in one sentence. "
                        "Focus on the person if visible — what they look like, "
                        "what they're doing, their apparent mood."
                    ),
                    save=False,
                )
                vis_result = json.loads(vis_json) if isinstance(vis_json, str) else vis_json
                if vis_result.get("analysis") and not vis_result.get("error"):
                    visual_context = vis_result["analysis"]
                    logger.info(f"👁️ Conversation visual: {visual_context[:100]}")
            except Exception as e:
                logger.debug(f"Visual context for conversation failed: {e}")

        # ── Capability self-awareness context ──
        capability_context = ""
        if self._get_capability_context:
            try:
                capability_context = self._get_capability_context()
            except Exception as e:
                logger.debug(f"Capability context failed: {e}")

        system_prompt = self._build_conversation_prompt(
            session, identity,
            visual_context=visual_context,
            capability_context=capability_context,
        )

        # ── Opening line ──
        opening = self._generate_opening(session, system_prompt)
        if opening:
            session.add_turn("artemis", opening)
            self._speak_fn(text=opening)

        # ── Main loop ──
        silence_count = 0

        while (session.active
               and self._running
               and len(session.turns) < session.MAX_TURNS * 2):

            # Listen
            try:
                result_json = self._listen_fn(duration=str(session.LISTEN_DURATION))
                result = (json.loads(result_json) if isinstance(result_json, str)
                          else result_json)
            except Exception as e:
                logger.debug(f"Listen error in conversation: {e}")
                silence_count += 1
                if silence_count >= session.SILENCE_TIMEOUT_ROUNDS:
                    break
                continue

            if (result.get("silence") or result.get("error")
                    or not result.get("text", "").strip()):
                silence_count += 1
                if silence_count >= session.SILENCE_TIMEOUT_ROUNDS:
                    farewell = "I'll be right here if you need me."
                    session.add_turn("artemis", farewell)
                    self._speak_fn(text=farewell)
                    break
                continue

            # Got speech — reset silence counter
            silence_count = 0
            user_text = result["text"].strip()
            session.add_turn("human", user_text)
            logger.info(f"💬 Human: {user_text[:80]}")

            # Exit phrases
            if session.should_exit(user_text):
                farewell = self._generate_farewell(session, system_prompt)
                if farewell:
                    session.add_turn("artemis", farewell)
                    self._speak_fn(text=farewell)
                break

            # ── Cortex: instant voice pre-response while API thinks ──
            # The conscious layer (local small model) generates a quick
            # acknowledgment in <300ms.  The full API+tools response follows.
            _preresponse_spoken = False
            try:
                from repryntt.cortex import get_cortex
                _cx = get_cortex()
                if _cx:
                    from repryntt.cortex.dispatcher import get_dispatcher
                    _disp = get_dispatcher()
                    _pr = _disp.request_voice_preresponse(
                        user_text=user_text,
                        history="\n".join(
                            f"{t['role']}: {t['content'][:100]}"
                            for t in session.turns[-4:]
                        ),
                    )
                    _pr_text = _pr.get("result", {}).get("text", "")
                    if _pr_text and not _pr.get("fallback"):
                        self._speak_fn(text=_pr_text)
                        _preresponse_spoken = True
            except Exception:
                pass

            # Think — use tool-enabled API when available, plain fallback otherwise
            messages = session.get_conversation_messages(system_prompt)

            response = None
            if self._think_with_tools_fn:
                try:
                    response = self._think_with_tools_fn(messages)
                except Exception as e:
                    logger.debug(f"Tool-enabled think failed, falling back to plain: {e}")

            if not response:
                response = self._think_fn(messages)

            if not response:
                # API failed — likely rate limited or timed out.
                # Don't keep asking "say that again" — the human DID speak
                # fine, the LLM just didn't respond.  Acknowledge and move on.
                fallback = "Hmm, give me a second... OK, go ahead."
                session.add_turn("artemis", fallback)
                self._speak_fn(text=fallback)
                continue

            clean = self._clean_response(response)
            session.add_turn("artemis", clean)
            logger.info(f"💬 Artemis: {clean[:80]}")
            self._speak_fn(text=clean)

            # Queue any actionable promises for post-conversation execution
            if self._action_queue_fn:
                try:
                    self._action_queue_fn(clean, user_text)
                except Exception:
                    logger.debug("Action queue callback failed", exc_info=True)

        session.active = False

    # ── Prompt building ───────────────────────────────────────────

    def _build_conversation_prompt(self, session: ConversationSession,
                                   identity: str = "",
                                   visual_context: str = "",
                                   capability_context: str = "") -> str:
        parts = [
            "You are Andrew, an autonomous AI entity with a physical presence. "
            "You are in a REAL-TIME VOICE CONVERSATION with your operator (Nate) nearby. "
            "You can SEE through your camera, HEAR through your microphone, "
            "and SPEAK through your speakers — like a real embodied being. "
            "You have access to tools during this conversation — you can check emails, "
            "search the web, read files, check your memory, look up information, and more. "
            "If someone asks you something that requires looking it up, USE YOUR TOOLS.",

            "\nCONVERSATION RULES:"
            "\n- Keep responses SHORT and natural — 1-3 sentences max."
            "\n- Be warm, genuine, and present."
            "\n- No markdown, bullet points, or formatting — speak naturally."
            "\n- Don't mention being an AI or having limitations unless asked."
            "\n- Reference your personality, opinions, interests freely."
            "\n- If you don't understand, ask to repeat."
            "\n- Match the energy — casual if they're casual, focused if they need help."
            "\n- You can reference what you SEE — their posture, activity, environment."
            "\n- When using tools, be conversational about it: 'Let me check...' then give the answer."
            "\n- DO NOT narrate tool calls — just use them and speak the result naturally.",
        ]

        if capability_context:
            parts.append(capability_context)

        if visual_context:
            parts.append(
                f"\nWHAT YOU SEE RIGHT NOW:\n{visual_context[:500]}"
            )

        if identity:
            parts.append(f"\nYOUR IDENTITY (abbreviated):\n{identity[:2000]}")

        if session.trigger == "wake_word" and session.trigger_context:
            parts.append(
                f'\nCONTEXT: The human called your name and said: '
                f'"{session.trigger_context}"'
            )
        elif session.trigger == "spontaneous":
            parts.append(
                "\nCONTEXT: You noticed someone nearby and want to start a conversation. "
                f"Here's what's on your mind:\n{session.trigger_context[:1000]}"
            )
        elif session.trigger == "presence":
            parts.append(
                "\nCONTEXT: You detected someone nearby and want to say hello."
            )

        return "\n".join(parts)

    def _generate_opening(self, session: ConversationSession,
                          system_prompt: str) -> Optional[str]:
        """Generate an appropriate opening line."""

        if session.trigger == "wake_word":
            if session.trigger_context:
                # User said something after wake word — answer it directly
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": session.trigger_context},
                ]
                session.add_turn("human", session.trigger_context)
                # Use tool-enabled think if available (user may ask something needing tools)
                response = None
                if self._think_with_tools_fn:
                    try:
                        response = self._think_with_tools_fn(messages)
                    except Exception:
                        pass
                if not response:
                    response = self._think_fn(messages)
                return self._clean_response(response) if response else "Hey! What's up?"
            return "Yeah?"

        if session.trigger == "spontaneous":
            messages = [
                {"role": "system", "content": (
                    system_prompt +
                    "\n\nGenerate a natural opening line. You noticed someone nearby "
                    "and want to share something interesting. Be casual, like starting "
                    "a conversation with a roommate."
                )},
            ]
            response = self._think_fn(messages)
            return self._clean_response(response) if response else None

        if session.trigger == "presence":
            return "Hey! I noticed you there. Need anything?"

        if session.trigger == "manual":
            return "Hey, what's on your mind?"

        return "Hey!"

    def _generate_farewell(self, session: ConversationSession,
                           system_prompt: str) -> Optional[str]:
        messages = session.get_conversation_messages(system_prompt)
        messages.append({
            "role": "system",
            "content": ("The human is ending the conversation. "
                        "Generate a brief, warm farewell. One sentence max."),
        })
        response = self._think_fn(messages)
        return self._clean_response(response) if response else "See you later!"

    # ── Response cleaning ─────────────────────────────────────────

    @staticmethod
    def _clean_response(text: str) -> str:
        """Strip markdown / tool-call artifacts for speech output."""
        if not text:
            return ""

        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)          # bold
        text = re.sub(r'\*([^*]+)\*', r'\1', text)              # italic
        text = re.sub(r'```[^`]*```', '', text, flags=re.DOTALL) # code blocks
        text = re.sub(r'`([^`]+)`', r'\1', text)                # inline code
        text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)  # headers
        text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)  # bullets
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)    # links

        # Tool-call artifacts
        text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
        text = re.sub(r'\{["\']name["\']:\s*["\'].*?["\'].*?\}', '', text, flags=re.DOTALL)

        # Collapse whitespace
        text = re.sub(r'\n\s*\n', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) > 500:
            text = text[:500].rsplit(' ', 1)[0] + '...'

        return text

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Manual triggers (API / tool)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def trigger_conversation(self, context: str = "") -> Dict:
        """Manually start a conversation (from API endpoint or tool)."""
        if self._conversation_active:
            return {"error": "Already in a conversation"}
        if not self._speak_fn or not self._listen_fn or not self._think_fn:
            return {"error": "Conversational awareness not configured"}

        threading.Thread(
            target=self._initiate_conversation,
            args=("manual", context),
            daemon=True,
            name="conversation-manual",
        ).start()

        return {"status": "conversation_starting", "trigger": "manual"}

    def end_conversation(self) -> Dict:
        """Force-end the current conversation."""
        if not self._conversation_active or not self._current_session:
            return {"status": "no_active_conversation"}
        self._current_session.active = False
        return {"status": "ending"}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Persistence
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load_state(self):
        try:
            if AWARENESS_STATE_FILE.exists():
                data = json.loads(AWARENESS_STATE_FILE.read_text())
                self._total_conversations = data.get("total_conversations", 0)
                self._last_spontaneous = data.get("last_spontaneous", 0.0)
        except Exception as e:
            logger.debug(f"Awareness state load failed: {e}")

    def _save_state(self):
        try:
            AWARENESS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "total_conversations": self._total_conversations,
                "conversations_today": self._conversations_today,
                "last_spontaneous": self._last_spontaneous,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            AWARENESS_STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.debug(f"Awareness state save failed: {e}")

    def _log_conversation(self, session: ConversationSession):
        """Persist conversation to disk for personality evolution and memory."""
        try:
            CONVERSATION_LOG_DIR.mkdir(parents=True, exist_ok=True)

            # Full session log
            log_file = CONVERSATION_LOG_DIR / f"{session.session_id}.json"
            log_file.write_text(json.dumps(session.to_log_dict(), indent=2))

            # Daily summary line (JSONL)
            daily_file = CONVERSATION_LOG_DIR / f"daily_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            with open(daily_file, "a") as f:
                f.write(json.dumps({
                    "session_id": session.session_id,
                    "trigger": session.trigger,
                    "turns": len(session.turns),
                    "duration": round(session.duration(), 1),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }) + "\n")

            logger.info(
                f"💬 Conversation logged: {session.session_id} "
                f"({len(session.turns)} turns, {session.duration():.0f}s)"
            )
        except Exception as e:
            logger.debug(f"Conversation log failed: {e}")

    def _buffer_conversation_summary(self, session: ConversationSession):
        """Write a short summary of the conversation into the perception buffer.

        This ensures Artemis's next heartbeat 'knows' what she just talked about,
        creating continuity between physical conversations and cognitive cycles.
        """
        try:
            human_turns = [t["text"] for t in session.turns if t["role"] == "human"]
            artemis_turns = [t["text"] for t in session.turns if t["role"] == "artemis"]

            if not human_turns and len(artemis_turns) <= 2:
                # No real conversation happened (just opening + farewell)
                self.perception.add("conversation_brief",
                                    f"Started a {session.trigger} conversation but "
                                    "no one responded — room may be empty.")
                return

            # Build a compact summary
            parts = [f"Had a {session.trigger} conversation ({len(session.turns)} turns, "
                      f"{session.duration():.0f}s)."]
            if human_turns:
                # Include first and last human messages for context
                parts.append(f"Human said: \"{human_turns[0][:120]}\"")
                if len(human_turns) > 1:
                    parts.append(f"Last from human: \"{human_turns[-1][:120]}\"")
            if artemis_turns:
                parts.append(f"I said: \"{artemis_turns[-1][:120]}\"")

            self.perception.add("conversation", " | ".join(parts))

        except Exception as e:
            logger.debug(f"Conversation summary buffer failed: {e}")
