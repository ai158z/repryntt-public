"""
repryntt.hardware.social_behavior — Social Perception & Interaction FSM.

This is the missing "social layer" — sits between perception (NavCortex)
and motor control (Explorer/tank), adding human-aware behavior.

Real robotics reference:
    Boston Dynamics Spot:  Person detected → stop → face person → wait
    Tesla Optimus:         Human proximity → yield → gesture/speak
    Pepper/Jibo/Kuri:      Face detect → orient → greet → converse → disengage
    Us:                    Scene "person" → pause nav → face → speak → log → resume

Architecture:
    NavCortex.perceive() returns scene description
        ↓
    SocialBehavior.process_scene(scene, perception)
        ↓
    FSM: IDLE → PERSON_DETECTED → APPROACHING → ENGAGING → COOLDOWN → IDLE
        ↓
    Motor commands (turn to face), speech (greet/converse), event log
        ↓
    Explorer reads social_state, pauses/resumes accordingly
        ↓
    Heartbeat context injection (Andrew knows what happened)

Proxemics (Hall, 1966 — standard in social robotics):
    Intimate:    < 45cm  — too close, back up
    Personal:    45-120cm — conversation distance (TARGET)
    Social:      120-360cm — approach distance (detected, start approach)
    Public:      > 360cm — acknowledge but don't approach

State machine prevents:
    - Greeting the same person every 5 seconds (cooldown)
    - Approaching when person is walking away (scene change detection)
    - Speaking during motor movement (speech waits for stop)
    - Infinite approach loops (max approach steps)
    - Blocking navigation forever (interaction timeout)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Social FSM States ────────────────────────────────────────────────

class SocialState(Enum):
    """Finite state machine for social interaction behavior."""
    IDLE = auto()           # No person detected, normal navigation
    PERSON_DETECTED = auto()  # Person spotted in scene, evaluating
    APPROACHING = auto()    # Moving toward person at social distance
    ENGAGING = auto()       # Stopped, facing person, speaking/listening
    COOLDOWN = auto()       # Just finished interaction, waiting before next


# ── Proxemics Constants (meters → cm for our system) ──────────────

PROXEMICS_INTIMATE_CM = 45     # Too close — back up
PROXEMICS_PERSONAL_CM = 120    # Ideal conversation distance
PROXEMICS_SOCIAL_CM = 360      # Start noticing / approaching
PROXEMICS_PUBLIC_CM = 500      # Acknowledge only

# ── Timing Constants ─────────────────────────────────────────────────

COOLDOWN_SECONDS = 60          # Don't re-engage same area for 60s
APPROACH_TIMEOUT_SEC = 20      # Max time to spend approaching
ENGAGE_TIMEOUT_SEC = 30        # Max time in conversation
ENGAGE_TIMEOUT_EXPLORING_SEC = 6  # Shortened when explorer has active goal
ROUTE_AROUND_AFTER_SEC = 3     # After this many s holding, route around person
MAX_APPROACH_STEPS = 8         # Max motor steps during approach
PERSON_CONFIRM_FRAMES = 1      # Consecutive frames with person to trigger
                                # (1 = immediate, raise for false-positive filtering)

# ── Person Detection Keywords ────────────────────────────────────────
# These are what Gemini says when it sees people in the scene description.
# Ordered by specificity — checked with substring matching on lowercase scene.

PERSON_KEYWORDS = (
    "person", "people", "someone", "man ", "woman ", "human",
    "child", "kid ", "standing", "sitting nearby", "walking",
    "individual", "figure", "face ", "faces ", "legs ", "feet ",
    "toddler", "baby ", "occupant", "resident",
)

# Keywords that indicate the person is far or not interactable
PERSON_FAR_KEYWORDS = (
    "in the distance", "far away", "background", "barely visible",
    "through window", "on screen", "on tv", "on the tv",
    "in picture", "photo of", "poster", "painting",
    "television", "tv screen", "monitor", "display showing",
    "video playing", "movie", "broadcast", "channel",
)

# Keywords indicating degraded image — skip social processing
DEGRADED_IMAGE_KEYWORDS = (
    "blurry", "pixelated", "out of focus", "out-of-focus",
    "no discernible", "completely gray", "grey and white",
    "hazy", "noise", "faded", "unrecognizable",
    "heavily pixelated", "speckled",
)


@dataclass
class PersonSighting:
    """Record of a detected person."""
    timestamp: float
    scene: str                   # raw scene text when detected
    estimated_distance_cm: float # from stereo or Gemini's estimate
    direction: str               # left/center/right in frame
    confidence: float            # how certain we are (0-1)
    consecutive_frames: int = 1  # how many frames in a row


@dataclass
class SocialInteraction:
    """Record of a completed social interaction."""
    timestamp: float
    duration_sec: float
    greeting_spoken: str
    person_description: str
    approach_steps: int
    outcome: str  # "greeted", "person_left", "timeout", "too_close"
    location: Optional[Dict[str, float]] = None  # x, y from spatial map


@dataclass
class SocialContext:
    """Context payload injected into Andrew's heartbeat."""
    state: str
    current_sighting: Optional[Dict[str, Any]] = None
    recent_interactions: List[Dict[str, Any]] = field(default_factory=list)
    total_interactions_today: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "current_sighting": self.current_sighting,
            "recent_interactions": self.recent_interactions[-3:],
            "total_interactions_today": self.total_interactions_today,
        }


class SocialBehavior:
    """Social perception and interaction finite state machine.

    Thread-safe. Called from Explorer's background thread during navigation,
    and queried from the heartbeat thread for context injection.

    The FSM handles the full interaction lifecycle:
        IDLE → PERSON_DETECTED → APPROACHING → ENGAGING → COOLDOWN → IDLE
    """

    def __init__(self):
        self._state = SocialState.IDLE
        self._lock = threading.Lock()

        # Current interaction tracking
        self._current_sighting: Optional[PersonSighting] = None
        self._approach_steps = 0
        self._approach_start: float = 0.0
        self._engage_start: float = 0.0
        self._cooldown_start: float = 0.0
        self._greeting_spoken: str = ""

        # History
        self._interactions: List[SocialInteraction] = []
        self._interactions_today: int = 0
        self._last_interaction_time: float = 0.0

        # Cooldown zones: (x, y, timestamp) — avoid re-engaging same spot
        self._cooldown_zones: List[Dict[str, Any]] = []

        # Callbacks (set by Explorer integration)
        self._on_state_change: Optional[Callable] = None
        self._speak_fn: Optional[Callable] = None

        # Greetings — varied so it doesn't sound robotic repeating the same thing
        self._greeting_index = 0
        self._greetings = [
            "Hey there! I'm Andrew, just exploring around.",
            "Oh, hello! Didn't expect to find someone here.",
            "Hi! I'm an autonomous robot named Andrew. How's it going?",
            "Hey! I'm just rolling through, exploring the place.",
            "Hello! I'm Andrew — I'm learning to navigate around here.",
            "Oh hi! Nice to see a friendly face.",
            "Hey there, I'm Andrew. Just mapping out the area.",
            "Hello! I noticed you and wanted to say hi.",
        ]

        # Contextual reactions (when re-encountering or in specific situations)
        self._re_encounter_phrases = [
            "Hey again! Still exploring over here.",
            "Oh, we meet again! Small world.",
            "Back again — I'm getting better at finding my way around.",
        ]

        logger.info("🤝 Social behavior FSM initialized")

    @property
    def state(self) -> SocialState:
        with self._lock:
            return self._state

    @property
    def is_interacting(self) -> bool:
        """True if in any active interaction state (not IDLE/COOLDOWN)."""
        with self._lock:
            return self._state in (
                SocialState.PERSON_DETECTED,
                SocialState.APPROACHING,
                SocialState.ENGAGING,
            )

    def set_speak_function(self, fn: Callable):
        """Set the function used to speak. Expected signature: fn(text) -> str."""
        self._speak_fn = fn

    def set_state_change_callback(self, fn: Callable):
        """Called with (old_state, new_state, context_dict) on transitions."""
        self._on_state_change = fn

    # ── Core: Process a scene from NavCortex ─────────────────────────

    def process_scene(
        self,
        scene: str,
        perception: Dict[str, Any],
        stereo_distance_cm: Optional[float] = None,
        robot_location: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Analyze a perception frame for social cues and drive the FSM.

        Called by Explorer on every navigation step. Returns an action
        recommendation that Explorer should follow.

        Args:
            scene: Scene description from Gemini (e.g. "A person standing near a couch")
            perception: Full NavCortex perception dict (obstacles, path, etc.)
            stereo_distance_cm: Stereo depth to nearest obstacle (if person, this is distance)
            robot_location: Current (x, y) from spatial map

        Returns:
            Dict with:
                "action": "continue" | "pause" | "approach" | "engage" | "backup"
                "motor_override": Optional action string for Explorer to execute
                "speak_text": Optional text to speak
                "reason": Why this action was chosen
        """
        with self._lock:
            return self._process_scene_locked(
                scene, perception, stereo_distance_cm, robot_location)

    def _process_scene_locked(
        self,
        scene: str,
        perception: Dict[str, Any],
        stereo_distance_cm: Optional[float],
        robot_location: Optional[Dict[str, float]],
    ) -> Dict[str, Any]:
        """FSM logic — must be called while holding self._lock."""
        now = time.time()
        scene_lower = scene.lower() if scene else ""

        # Skip social processing for degraded/blurry images
        if any(kw in scene_lower for kw in DEGRADED_IMAGE_KEYWORDS):
            if self._state == SocialState.IDLE:
                return {"action": "continue", "reason": "degraded image, skipping social"}

        # Primary: use Gemini's structured people detection if available
        people_field = perception.get("people", {})
        _structured_detection = False
        if people_field.get("detected") is True:
            person_detected = True
            _structured_detection = True
            # Use Gemini's person-specific distance if available
            person_dist = people_field.get("nearest_distance_cm")
            if person_dist and isinstance(person_dist, (int, float)) and person_dist > 0:
                stereo_distance_cm = float(person_dist)
        elif people_field.get("detected") is False:
            # Gemini explicitly says no people — trust it
            person_detected = False
        else:
            # No structured field — fall back to keyword matching
            person_detected = self._detect_person(scene_lower)
        distance = stereo_distance_cm or self._estimate_distance_from_perception(perception)

        # ── State: COOLDOWN ──────────────────────────────────────────
        if self._state == SocialState.COOLDOWN:
            elapsed = now - self._cooldown_start
            if elapsed >= COOLDOWN_SECONDS:
                self._transition(SocialState.IDLE)
            # During cooldown, navigate normally but don't re-engage
            return {"action": "continue", "reason": f"cooldown ({COOLDOWN_SECONDS - elapsed:.0f}s left)"}

        # ── State: IDLE ──────────────────────────────────────────────
        if self._state == SocialState.IDLE:
            if not person_detected:
                return {"action": "continue", "reason": "no person detected"}

            # Check if this location is in cooldown
            if self._in_cooldown_zone(robot_location):
                return {"action": "continue", "reason": "recently interacted here"}

            # Person detected! Evaluate.
            sighting = PersonSighting(
                timestamp=now,
                scene=scene,
                estimated_distance_cm=distance,
                direction=(people_field.get("nearest_position", "center")
                           if _structured_detection
                           else self._estimate_person_direction(perception)),
                confidence=(0.85 if _structured_detection
                            else self._assess_person_confidence(scene_lower)),
            )

            # Filter out false positives: people on TV, in photos, etc.
            if sighting.confidence < 0.3:
                return {"action": "continue", "reason": f"low confidence person ({sighting.confidence:.1f})"}

            self._current_sighting = sighting
            self._transition(SocialState.PERSON_DETECTED)

            logger.info(f"🧑 Person detected! distance≈{distance:.0f}cm, "
                        f"direction={sighting.direction}, conf={sighting.confidence:.1f}, "
                        f"scene: {scene[:80]}")

            # Immediate decision based on distance
            if distance <= PROXEMICS_INTIMATE_CM:
                # Too close — back up, then engage
                return {
                    "action": "backup",
                    "motor_override": "backward",
                    "reason": f"person too close ({distance:.0f}cm), backing up",
                }
            elif distance <= PROXEMICS_PERSONAL_CM:
                # Already at conversation distance — go straight to engage
                self._transition(SocialState.ENGAGING)
                self._engage_start = now
                greeting = self._pick_greeting()
                return {
                    "action": "engage",
                    "motor_override": "stop",
                    "speak_text": greeting,
                    "reason": f"person at conversational distance ({distance:.0f}cm)",
                }
            else:
                # Person is at social/public distance — approach
                self._transition(SocialState.APPROACHING)
                self._approach_start = now
                self._approach_steps = 0
                turn_dir = self._turn_toward_person(sighting.direction)
                return {
                    "action": "approach",
                    "motor_override": turn_dir,
                    "reason": f"person at {distance:.0f}cm, turning to approach",
                }

        # ── State: PERSON_DETECTED (evaluation frame) ────────────────
        if self._state == SocialState.PERSON_DETECTED:
            if not person_detected:
                # Person disappeared — false alarm or they left
                self._transition(SocialState.IDLE)
                self._current_sighting = None
                return {"action": "continue", "reason": "person disappeared"}

            # Update sighting
            if self._current_sighting:
                self._current_sighting.consecutive_frames += 1
                self._current_sighting.estimated_distance_cm = distance

            if distance <= PROXEMICS_PERSONAL_CM:
                self._transition(SocialState.ENGAGING)
                self._engage_start = now
                greeting = self._pick_greeting()
                return {
                    "action": "engage",
                    "motor_override": "stop",
                    "speak_text": greeting,
                    "reason": f"person confirmed at {distance:.0f}cm, engaging",
                }
            else:
                self._transition(SocialState.APPROACHING)
                self._approach_start = now
                self._approach_steps = 0
                direction = self._current_sighting.direction if self._current_sighting else "center"
                turn_dir = self._turn_toward_person(direction)
                return {
                    "action": "approach",
                    "motor_override": turn_dir,
                    "reason": f"person confirmed, approaching from {distance:.0f}cm",
                }

        # ── State: APPROACHING ───────────────────────────────────────
        if self._state == SocialState.APPROACHING:
            # Timeout check
            if now - self._approach_start > APPROACH_TIMEOUT_SEC:
                self._end_interaction("timeout_approach", robot_location)
                return {"action": "continue", "reason": "approach timeout, resuming nav"}

            if self._approach_steps >= MAX_APPROACH_STEPS:
                self._end_interaction("max_approach_steps", robot_location)
                return {"action": "continue", "reason": "max approach steps, resuming nav"}

            if not person_detected:
                # Lost sight of person during approach
                self._end_interaction("person_left", robot_location)
                return {"action": "continue", "reason": "person left during approach"}

            # Update distance
            if self._current_sighting:
                self._current_sighting.estimated_distance_cm = distance

            self._approach_steps += 1

            if distance <= PROXEMICS_INTIMATE_CM:
                # Got too close
                return {
                    "action": "backup",
                    "motor_override": "backward",
                    "reason": f"too close ({distance:.0f}cm), backing up",
                }
            elif distance <= PROXEMICS_PERSONAL_CM:
                # Reached conversation distance
                self._transition(SocialState.ENGAGING)
                self._engage_start = now
                greeting = self._pick_greeting()
                return {
                    "action": "engage",
                    "motor_override": "stop",
                    "speak_text": greeting,
                    "reason": f"reached conversation distance ({distance:.0f}cm)",
                }
            else:
                # Keep approaching — move forward toward person
                return {
                    "action": "approach",
                    "motor_override": "forward",
                    "reason": f"approaching person ({distance:.0f}cm away, step {self._approach_steps})",
                }

        # ── State: ENGAGING ──────────────────────────────────────────
        if self._state == SocialState.ENGAGING:
            # Check if explorer has an active exploration goal — if so,
            # don't pin the tank forever. Greet briefly, then route around.
            _exploring = False
            try:
                from repryntt.hardware.explorer import get_explorer
                _status = get_explorer().status()
                _exploring = bool(_status.get("running")) and bool(_status.get("goal"))
            except Exception:
                _exploring = False

            _timeout = ENGAGE_TIMEOUT_EXPLORING_SEC if _exploring else ENGAGE_TIMEOUT_SEC

            # Timeout check
            if now - self._engage_start > _timeout:
                self._end_interaction("timeout_engage", robot_location)
                return {"action": "continue", "reason": "engagement timeout, resuming nav"}

            if not person_detected:
                # Person walked away during conversation
                self._end_interaction("person_left", robot_location)
                farewell = "Oh, they left. Well, nice seeing them!"
                return {
                    "action": "continue",
                    "speak_text": farewell,
                    "reason": "person left during engagement",
                }

            if distance <= PROXEMICS_INTIMATE_CM:
                return {
                    "action": "backup",
                    "motor_override": "backward",
                    "reason": f"person too close ({distance:.0f}cm), maintaining space",
                }

            # If exploring and we've held briefly, route AROUND the person
            # instead of pinning indefinitely. This prevents the "operator
            # on couch freezes tank" failure mode.
            if _exploring and (now - self._engage_start) > ROUTE_AROUND_AFTER_SEC:
                # Pick turn direction based on person's location
                direction = (self._current_sighting.direction
                             if self._current_sighting else "center")
                if direction == "left":
                    turn = "turn_right"   # person on left, route right
                elif direction == "right":
                    turn = "turn_left"    # person on right, route left
                else:
                    turn = "turn_left"    # center: default to left route
                logger.info(f"🤝 Social: routing around person ({direction}) → {turn}")
                # End the interaction cleanly so we return to IDLE
                self._end_interaction("route_around", robot_location)
                return {
                    "action": "continue",
                    "motor_override": turn,
                    "reason": f"routing around person to continue exploration ({turn})",
                }

            # Stay engaged — don't move, just hold position
            return {
                "action": "hold",
                "motor_override": "stop",
                "reason": f"engaged with person at {distance:.0f}cm",
            }

        return {"action": "continue", "reason": "unknown state"}

    # ── Person Detection Heuristics ──────────────────────────────────

    def _detect_person(self, scene_lower: str) -> bool:
        """Check if the scene description contains a person.

        Uses keyword matching on Gemini's scene description. This is
        surprisingly reliable since Gemini is excellent at describing
        people in images. We filter out false positives from TV/photos.

        NOTE: This is the FALLBACK path — only called when the structured
        people field from Gemini is not available (e.g., old prompt format
        or parse failure). The primary path uses perception['people']['detected'].
        """
        if not scene_lower:
            return False

        # Check for person keywords
        has_person = any(kw in scene_lower for kw in PERSON_KEYWORDS)
        if not has_person:
            return False

        # Filter out non-real people (TV, photos, posters)
        is_fake = any(kw in scene_lower for kw in PERSON_FAR_KEYWORDS)
        if is_fake:
            return False

        return True

    def _assess_person_confidence(self, scene_lower: str) -> float:
        """Estimate confidence that a real person is present.

        Higher confidence for more explicit descriptions.
        Lower for ambiguous or potentially-fake detections.
        """
        confidence = 0.0

        # Strong indicators
        strong = ("person", "someone", "man ", "woman ", "human", "people")
        if any(kw in scene_lower for kw in strong):
            confidence += 0.6

        # Activity indicators (person doing something = more real)
        activity = ("standing", "sitting", "walking", "looking", "moving",
                    "talking", "holding", "reaching", "bending")
        if any(kw in scene_lower for kw in activity):
            confidence += 0.3

        # Appearance details (Gemini describes clothing = definitely sees person)
        appearance = ("wearing", "shirt", "pants", "jacket", "shoes",
                      "hair", "glasses", "hat", "dress")
        if any(kw in scene_lower for kw in appearance):
            confidence += 0.2

        # Penalty for ambiguous / far
        if any(kw in scene_lower for kw in ("might be", "appears to", "possibly",
                                             "shadow", "silhouette")):
            confidence -= 0.2

        return max(0.0, min(1.0, confidence))

    def _estimate_person_direction(self, perception: Dict[str, Any]) -> str:
        """Estimate which direction the person is, relative to robot.

        Uses obstacle distribution as a proxy — if obstacles are heavier
        on one side, the person (largest obstacle) is probably there.
        """
        obs = perception.get("obstacles", {})
        left = float(obs.get("left", 0.3))
        center = float(obs.get("center", 0.3))
        right = float(obs.get("right", 0.3))

        # Person = most prominent obstacle
        max_val = max(left, center, right)
        if max_val == center:
            return "center"
        elif max_val == left:
            return "left"
        else:
            return "right"

    def _estimate_distance_from_perception(self, perception: Dict[str, Any]) -> float:
        """Estimate distance to detected person from perception data."""
        # Use Gemini's estimate if available
        dist = perception.get("distance_to_nearest_obstacle_cm")
        if dist is not None:
            return float(dist)

        # Fall back to obstacle proximity → rough distance mapping
        center = float(perception.get("obstacles", {}).get("center", 0.5))
        # proximity 0.0 = clear (>300cm), 1.0 = blocked (<20cm)
        # Linear mapping: distance = (1 - proximity) * 300
        return max(20, (1.0 - center) * 300)

    # ── Motor Helpers ────────────────────────────────────────────────

    def _turn_toward_person(self, direction: str) -> str:
        """Return motor command to face the person."""
        if direction == "left":
            return "turn_left"
        elif direction == "right":
            return "turn_right"
        else:
            return "stop"  # Already facing them

    # ── Greeting Selection ───────────────────────────────────────────

    def _pick_greeting(self) -> str:
        """Pick the next greeting, cycling through for variety."""
        # If we've interacted recently, use re-encounter phrases
        if (self._interactions_today > 0 and
                time.time() - self._last_interaction_time < 600):
            idx = self._interactions_today % len(self._re_encounter_phrases)
            greeting = self._re_encounter_phrases[idx]
        else:
            greeting = self._greetings[self._greeting_index % len(self._greetings)]
            self._greeting_index += 1

        self._greeting_spoken = greeting
        return greeting

    # ── State Transitions ────────────────────────────────────────────

    def _transition(self, new_state: SocialState) -> None:
        """Transition to a new FSM state with logging."""
        old_state = self._state
        self._state = new_state
        logger.info(f"🤝 Social FSM: {old_state.name} → {new_state.name}")
        if self._on_state_change:
            try:
                self._on_state_change(old_state, new_state, self._get_context_dict())
            except Exception as e:
                logger.debug(f"State change callback error: {e}")

    def _end_interaction(
        self,
        outcome: str,
        robot_location: Optional[Dict[str, float]],
    ) -> None:
        """End the current interaction and enter cooldown."""
        now = time.time()
        duration = 0.0
        if self._engage_start > 0:
            duration = now - self._engage_start
        elif self._approach_start > 0:
            duration = now - self._approach_start

        interaction = SocialInteraction(
            timestamp=now,
            duration_sec=duration,
            greeting_spoken=self._greeting_spoken,
            person_description=(self._current_sighting.scene[:200]
                                if self._current_sighting else "unknown"),
            approach_steps=self._approach_steps,
            outcome=outcome,
            location=robot_location,
        )
        self._interactions.append(interaction)
        self._interactions_today += 1
        self._last_interaction_time = now

        # Add cooldown zone
        if robot_location:
            self._cooldown_zones.append({
                "x": robot_location.get("x", 0),
                "y": robot_location.get("y", 0),
                "timestamp": now,
            })

        # Reset current interaction state
        self._current_sighting = None
        self._approach_steps = 0
        self._approach_start = 0.0
        self._engage_start = 0.0
        self._greeting_spoken = ""

        # Enter cooldown
        self._cooldown_start = now
        self._transition(SocialState.COOLDOWN)

        logger.info(f"🤝 Interaction ended: outcome={outcome}, "
                    f"duration={duration:.1f}s, approaches={interaction.approach_steps}, "
                    f"total_today={self._interactions_today}")

    def _in_cooldown_zone(self, location: Optional[Dict[str, float]]) -> bool:
        """Check if current location is near a recent interaction site."""
        if not location:
            return False
        now = time.time()
        # Prune expired zones
        self._cooldown_zones = [
            z for z in self._cooldown_zones
            if now - z["timestamp"] < COOLDOWN_SECONDS
        ]
        x, y = location.get("x", 0), location.get("y", 0)
        for zone in self._cooldown_zones:
            dx = x - zone["x"]
            dy = y - zone["y"]
            if (dx * dx + dy * dy) < (150 * 150):  # within 150cm
                return True
        return False

    # ── Context for Heartbeat ────────────────────────────────────────

    def get_social_context(self) -> SocialContext:
        """Get current social state for injection into Andrew's heartbeat.

        Called by persistent_agents.py during sensory scan to give Andrew
        awareness of social interactions that happened during navigation.
        """
        with self._lock:
            return SocialContext(
                state=self._state.name,
                current_sighting=(
                    {
                        "scene": self._current_sighting.scene[:200],
                        "distance_cm": self._current_sighting.estimated_distance_cm,
                        "direction": self._current_sighting.direction,
                        "confidence": self._current_sighting.confidence,
                    }
                    if self._current_sighting else None
                ),
                recent_interactions=[
                    {
                        "timestamp": i.timestamp,
                        "duration_sec": round(i.duration_sec, 1),
                        "greeting": i.greeting_spoken,
                        "person": i.person_description[:100],
                        "outcome": i.outcome,
                    }
                    for i in self._interactions[-3:]
                ],
                total_interactions_today=self._interactions_today,
            )

    def _get_context_dict(self) -> Dict[str, Any]:
        """Internal context dict (no lock, caller must hold lock)."""
        return {
            "state": self._state.name,
            "sighting": (self._current_sighting.scene[:100]
                         if self._current_sighting else None),
            "interactions_today": self._interactions_today,
        }

    def get_interaction_log(self) -> List[Dict[str, Any]]:
        """Get all interactions from today for memory/journaling."""
        with self._lock:
            return [
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(i.timestamp)),
                    "duration_sec": round(i.duration_sec, 1),
                    "greeting": i.greeting_spoken,
                    "person": i.person_description[:150],
                    "approach_steps": i.approach_steps,
                    "outcome": i.outcome,
                    "location": i.location,
                }
                for i in self._interactions
            ]

    # ── Speech Execution ─────────────────────────────────────────────

    def speak(self, text: str) -> bool:
        """Speak text using the configured TTS function.

        Returns True if speech was successfully triggered.
        This is non-blocking — speech plays in background.
        """
        if not text:
            return False

        # Try configured speak function first
        if self._speak_fn:
            try:
                self._speak_fn(text)
                logger.info(f"🗣️ Social speech: {text[:80]}")
                return True
            except Exception as e:
                logger.warning(f"Social speak function failed: {e}")

        # Direct fallback — import and call speak from media
        try:
            from repryntt.tools.media import speak as media_speak
            brain_path = str(Path.home() / ".repryntt" / "brain")
            media_speak(brain_path, text=text)
            logger.info(f"🗣️ Social speech (direct): {text[:80]}")
            return True
        except Exception as e:
            logger.warning(f"Social speech failed entirely: {e}")
            return False

    # ── Reset ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset FSM to IDLE. Called when Explorer stops."""
        with self._lock:
            self._state = SocialState.IDLE
            self._current_sighting = None
            self._approach_steps = 0
            self._approach_start = 0.0
            self._engage_start = 0.0


# ── Singleton ────────────────────────────────────────────────────────

_social_behavior: Optional[SocialBehavior] = None


def get_social_behavior() -> SocialBehavior:
    """Get or create the singleton social behavior FSM."""
    global _social_behavior
    if _social_behavior is None:
        _social_behavior = SocialBehavior()
    return _social_behavior
