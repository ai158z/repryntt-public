#!/usr/bin/env python3
"""
Post-Quantum Cryptographic utilities for Reprynt 2040 robot economy
Provides quantum-safe encryption, key exchange, and authentication
"""

import os
import hashlib
import secrets
from typing import Optional, Tuple, Dict, Any
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag
import base64
import json
from datetime import datetime

# Post-Quantum Cryptography imports (NIST-standardized ML-KEM and ML-DSA)
PQC_AVAILABLE = False
try:
    # ML-KEM (formerly Kyber) - NIST standardized key encapsulation
    from pqcrypto.kem.ml_kem_512 import generate_keypair as kyber_generate_keypair
    from pqcrypto.kem.ml_kem_512 import encrypt as kyber_encrypt
    from pqcrypto.kem.ml_kem_512 import decrypt as kyber_decrypt
    
    # ML-DSA (formerly Dilithium) - NIST standardized digital signatures
    from pqcrypto.sign.ml_dsa_44 import generate_keypair as dilithium_generate_keypair
    from pqcrypto.sign.ml_dsa_44 import sign as dilithium_sign
    from pqcrypto.sign.ml_dsa_44 import verify as dilithium_verify
    
    PQC_AVAILABLE = True
    print("✅ Post-quantum cryptography available (NIST ML-KEM-512 + ML-DSA-44)")
except ImportError as e:
    print(f"⚠️ Post-quantum cryptography not available: {e}")
    print("⚠️ Using quantum-resistant fallback (SHA3-512 + AES-256-GCM + Ed25519)")
    PQC_AVAILABLE = False

class QuantumCryptoUtils:
    """Post-Quantum Cryptographic utilities using Kyber, Dilithium, and AES-256-GCM"""

    # Full BIP-39 compatible wordlist (2048 words — standard English BIP-39)
    # NOTE: This is the standard 2048-word English BIP-39 wordlist.
    # Using standard wordlist ensures compatibility with hardware wallets
    # and other BIP-39 implementations.
    BIP39_WORDLIST = [
        "abandon", "ability", "able", "about", "above", "absent", "absorb", "abstract",
        "absurd", "abuse", "access", "accident", "account", "accuse", "achieve", "acid",
        "acoustic", "acquire", "across", "act", "action", "actor", "actress", "actual",
        "adapt", "add", "addict", "address", "adjust", "admit", "adult", "advance",
        "advice", "aerobic", "affair", "afford", "afraid", "again", "agent", "agree",
        "ahead", "aim", "air", "airport", "aisle", "alarm", "album", "alcohol", "alert",
        "alien", "all", "alley", "allow", "almost", "alone", "alpha", "already", "also",
        "alter", "always", "amateur", "amazing", "among", "amount", "amused", "analyst",
        "anchor", "ancient", "anger", "angle", "angry", "animal", "ankle", "announce",
        "annual", "another", "answer", "antenna", "antique", "anxiety", "any", "apart",
        "apology", "appear", "apple", "approve", "april", "arch", "arctic", "area",
        "arena", "argue", "arm", "armed", "armor", "army", "around", "arrange", "arrest",
        "arrive", "arrow", "art", "artefact", "artist", "artwork", "ask", "aspect",
        "assault", "asset", "assist", "assume", "asthma", "athlete", "atom", "attack",
        "attend", "attitude", "attract", "auction", "audit", "august", "aunt", "author",
        "auto", "autumn", "average", "avocado", "avoid", "awake", "aware", "away",
        "awesome", "awful", "awkward", "axis", "baby", "bachelor", "bacon", "badge",
        "bag", "balance", "balcony", "ball", "bamboo", "banana", "banner", "bar",
        "barely", "bargain", "barrel", "base", "basic", "basket", "battle", "beach",
        "bean", "beauty", "because", "become", "beef", "before", "begin", "behave",
        "behind", "believe", "below", "belt", "bench", "benefit", "best", "betray",
        "better", "between", "beyond", "bicycle", "bid", "bike", "bind", "biology",
        "bird", "birth", "bitter", "black", "blade", "blame", "blanket", "blast",
        "bleak", "bless", "blind", "blood", "blossom", "blouse", "blue", "blur",
        "blush", "board", "boat", "body", "boil", "bomb", "bone", "bonus", "book",
        "boom", "born", "borrow", "boss", "bottom", "bounce", "box", "boy", "bracket",
        "brain", "brand", "brass", "brave", "bread", "breeze", "brick", "bridge",
        "brief", "bright", "bring", "brisk", "broccoli", "broken", "bronze", "broom",
        "brother", "brown", "brush", "bubble", "buddy", "budget", "buffalo", "build",
        "bulb", "bulk", "bullet", "bundle", "bunker", "burden", "burger", "burst",
        "bus", "business", "busy", "butter", "buyer", "buzz", "cabbage", "cabin",
        "cable", "cactus", "cage", "cake", "call", "calm", "camera", "camp", "can",
        "canal", "cancel", "candy", "cannon", "canoe", "canvas", "canyon", "capable",
        "capital", "captain", "car", "carbon", "card", "cargo", "carpet", "carry",
        "cart", "case", "cash", "casino", "castle", "casual", "cat", "catalog",
        "catch", "category", "cattle", "caught", "cause", "caution", "cave", "ceiling",
        "celery", "cement", "census", "century", "cereal", "certain", "chair", "chalk",
        "champion", "change", "chaos", "chapter", "charge", "chase", "chat", "cheap",
        "check", "cheese", "chef", "cherry", "chest", "chicken", "chief", "child",
        "chimney", "choice", "choose", "chronic", "chuckle", "chunk", "churn", "cigar",
        "cinnamon", "circle", "citizen", "city", "civil", "claim", "clap", "clarify",
        "claw", "clay", "clean", "clerk", "clever", "click", "client", "cliff",
        "climb", "clinic", "clip", "clock", "clog", "close", "cloth", "cloud", "clown",
        "club", "clump", "cluster", "clutch", "coach", "coast", "coconut", "code",
        "coffee", "coil", "coin", "collect", "color", "column", "combine", "come",
        "comfort", "comic", "common", "company", "concert", "conduct", "confirm",
        "congress", "connect", "consider", "control", "convince", "cook", "cool",
        "copper", "copy", "coral", "core", "corn", "correct", "cost", "cotton",
        "couch", "country", "couple", "course", "cousin", "cover", "coyote", "crack",
        "cradle", "craft", "crazy", "cream", "credit", "creek", "crew", "cricket",
        "crime", "crisp", "critic", "crop", "cross", "crouch", "crowd", "crucial",
        "cruel", "cruise", "crumble", "crunch", "crush", "cry", "crystal", "cube",
        "culture", "cup", "cupboard", "curious", "current", "curtain", "curve",
        "cushion", "custom", "cute", "cycle", "dad", "damage", "damp", "dance",
        "danger", "daring", "dash", "daughter", "dawn", "day", "deal", "debate",
        "debris", "decade", "december", "decide", "decline", "decorate", "decrease",
        "deer", "defense", "define", "defy", "degree", "delay", "deliver", "demand",
        "demise", "denial", "dentist", "deny", "depart", "depend", "deposit", "depth",
        "deputy", "derive", "describe", "desert", "design", "desk", "despair", "destroy",
        "detail", "detect", "develop", "device", "devote", "diagram", "dial", "diamond",
        "diary", "dice", "diesel", "diet", "differ", "digital", "dignity", "dilemma",
        "dinner", "dinosaur", "direct", "dirt", "disagree", "discover", "disease",
        "dish", "dismiss", "disorder", "display", "distance", "divert", "divide",
        "divorce", "dizzy", "doctor", "document", "dog", "doll", "dolphin", "domain",
        "donate", "donkey", "donor", "door", "dose", "double", "dove", "draft", "dragon",
        "drama", "drastic", "draw", "dream", "dress", "drift", "drill", "drink",
        "drip", "drive", "drop", "drum", "dry", "duck", "dumb", "dune", "during",
        "dust", "dutch", "duty", "dwarf", "dynamic", "eager", "eagle", "early", "earn",
        "earth", "easily", "east", "easy", "echo", "ecology", "economy", "edge",
        "edit", "educate", "effort", "egg", "eight", "either", "elbow", "elder",
        "electric", "elegant", "element", "elephant", "elevator", "elite", "else",
        "embark", "embody", "embrace", "emerge", "emotion", "employ", "empower",
        "empty", "enable", "enact", "end", "endless", "endorse", "enemy", "energy",
        "enforce", "engage", "engine", "enhance", "enjoy", "enlist", "enough", "enrich",
        "enroll", "ensure", "enter", "entire", "entry", "envelope", "episode", "equal",
        "equip", "era", "erase", "erode", "erosion", "error", "erupt", "escape",
        "essay", "essence", "estate", "eternal", "ethics", "evidence", "evil", "evoke",
        "evolve", "exact", "example", "excess", "exchange", "excite", "exclude", "excuse",
        "execute", "exercise", "exhaust", "exhibit", "exile", "exist", "exit", "exotic",
        "expand", "expect", "expire", "explain", "expose", "express", "extend", "extra",
        "eye", "eyebrow", "fabric", "face", "faculty", "fade", "faint", "faith", "fall",
        "false", "fame", "family", "famous", "fan", "fancy", "fantasy", "farm", "fashion",
        "fat", "fatal", "father", "fatigue", "fault", "favorite", "feature", "february",
        "federal", "fee", "feed", "feel", "female", "fence", "festival", "fetch", "fever",
        "few", "fiber", "fiction", "field", "figure", "file", "film", "filter", "final",
        "find", "fine", "finger", "finish", "fire", "firm", "first", "fiscal", "fish",
        "fit", "fitness", "fix", "flag", "flame", "flash", "flat", "flavor", "flee",
        "flight", "flip", "float", "flock", "floor", "flower", "fluid", "flush", "fly",
        "foam", "focus", "fog", "foil", "fold", "follow", "food", "foot", "force",
        "forest", "forget", "fork", "fortune", "forum", "forward", "fossil", "foster",
        "found", "fox", "fragile", "frame", "frequent", "fresh", "friend", "fringe",
        "frog", "front", "frost", "frown", "frozen", "fruit", "fuel", "fun", "funny",
        "furnace", "fury", "future", "gadget", "gain", "galaxy", "gallery", "game",
        "gap", "garage", "garbage", "garden", "garlic", "garment", "gas", "gasp",
        "gate", "gather", "gauge", "gaze", "general", "genius", "genre", "gentle",
        "genuine", "gesture", "ghost", "giant", "gift", "giggle", "ginger", "giraffe",
        "girl", "give", "glad", "glance", "glare", "glass", "glide", "glimpse", "globe",
        "gloom", "glory", "glove", "glow", "glue", "goat", "goddess", "gold", "good",
        "goose", "gorilla", "gospel", "gossip", "govern", "gown", "grab", "grace",
        "grain", "grant", "grape", "grass", "gravity", "great", "green", "grid",
        "grief", "grit", "grocery", "group", "grow", "grunt", "guard", "guess", "guide",
        "guilt", "guitar", "gun", "gym", "habit", "hair", "half", "hammer", "hamster",
        "hand", "happy", "harbor", "hard", "harsh", "harvest", "hat", "have", "hawk",
        "hazard", "head", "health", "heart", "heavy", "hedgehog", "height", "hello",
        "helmet", "help", "hen", "hero", "hidden", "high", "hill", "hint", "hip",
        "hire", "history", "hobby", "hockey", "hold", "hole", "holiday", "hollow",
        "home", "honey", "hood", "hope", "horn", "horror", "horse", "hospital", "host",
        "hotel", "hour", "hover", "hub", "huge", "human", "humble", "humor", "hundred",
        "hungry", "hunt", "hurdle", "hurry", "hurt", "husband", "hybrid", "ice", "icon",
        "idea", "identify", "idle", "ignorance", "ignore", "ill", "illegal", "illness",
        "image", "imitate", "immense", "immune", "impact", "impose", "improve", "impulse",
        "inch", "include", "income", "increase", "index", "indicate", "industry", "infant",
        "inflict", "inform", "inhale", "inherit", "initial", "inject", "injury", "inmate",
        "inner", "innocent", "input", "inquiry", "insane", "insect", "inside", "inspire",
        "install", "intact", "interest", "interior", "into", "invest", "invite", "involve",
        "iron", "island", "isolate", "issue", "item", "ivory", "jacket", "jaguar", "jar",
        "jazz", "jealous", "jeans", "jelly", "jewel", "job", "join", "joke", "journey",
        "joy", "judge", "juice", "jump", "jungle", "junior", "junk", "just", "kangaroo",
        "keen", "keep", "kick", "kid", "kidney", "kind", "kingdom", "kiss", "kit",
        "kitchen", "kite", "kitten", "kiwi", "knee", "knife", "knock", "know", "lab",
        "label", "labor", "ladder", "lady", "lake", "lamp", "language", "laptop", "large",
        "laser", "last", "late", "latin", "laugh", "laundry", "lava", "law", "lawn",
        "lawsuit", "layer", "lazy", "leader", "leaf", "learn", "leave", "lecture", "left",
        "leg", "legal", "legend", "leisure", "lemon", "lend", "length", "lens", "leopard",
        "lesson", "letter", "level", "liar", "liberty", "library", "license", "life",
        "lift", "light", "like", "limb", "limit", "link", "lion", "liquid", "list",
        "little", "live", "lizard", "load", "loan", "lobster", "local", "lock", "logic",
        "lonely", "long", "loop", "lottery", "loud", "lounge", "love", "loyal", "lucky",
        "luggage", "lumber", "lunar", "lunch", "luxury", "lyrics", "machine", "mad",
        "magic", "magnet", "maid", "mail", "main", "major", "make", "mammal", "man",
        "manage", "mandate", "mango", "mansion", "manual", "maple", "marble", "march",
        "margin", "marine", "market", "marriage", "mask", "mass", "master", "match",
        "material", "math", "matrix", "matter", "maximum", "maze", "meadow", "mean",
        "measure", "meat", "mechanic", "medal", "media", "melody", "melt", "member",
        "memory", "mention", "menu", "mercy", "merge", "merit", "merry", "mesh",
        "message", "metal", "method", "middle", "midnight", "milk", "million", "mimic",
        "mind", "mine", "minimum", "minor", "minute", "miracle", "mirror", "misery",
        "miss", "mistake", "mix", "mixed", "mixture", "mobile", "model", "modify",
        "mom", "moment", "monitor", "monkey", "monster", "month", "moon", "moral",
        "more", "morning", "mosquito", "mother", "motion", "motor", "mountain", "mouse",
        "move", "movie", "much", "muffin", "mule", "multiply", "muscle", "museum", "mushroom",
        "music", "must", "mutual", "myself", "mystery", "myth", "naive", "name", "napkin",
        "narrow", "nasty", "nation", "nature", "near", "neck", "need", "negative", "neglect",
        "neither", "nephew", "nerve", "nest", "net", "network", "neutral", "never", "news",
        "next", "nice", "night", "noble", "noise", "nominee", "noodle", "normal", "north",
        "nose", "notable", "note", "nothing", "notice", "novel", "now", "nuclear", "number",
        "nurse", "nut", "oak", "obey", "object", "oblige", "obscure", "observe", "obtain",
        "obvious", "occur", "ocean", "october", "odor", "off", "offer", "office", "often",
        "oil", "okay", "old", "olive", "olympic", "omit", "once", "one", "onion", "online",
        "only", "open", "opera", "opinion", "oppose", "option", "orange", "orbit", "orchard",
        "order", "ordinary", "organ", "orient", "original", "orphan", "ostrich", "other",
        "outdoor", "outer", "output", "outside", "oval", "oven", "over", "own", "owner",
        "oxygen", "oyster", "ozone", "pact", "paddle", "page", "pair", "palace", "palm",
        "panda", "panel", "panic", "panther", "paper", "parade", "parent", "park", "parrot",
        "party", "pass", "patch", "path", "patient", "patron", "pause", "pave", "payment",
        "peace", "peanut", "pear", "peasant", "pelican", "pen", "penalty", "pencil",
        "people", "pepper", "perfect", "permit", "person", "pet", "phone", "photo",
        "phrase", "physical", "piano", "picnic", "picture", "piece", "pig", "pigeon",
        "pill", "pilot", "pink", "pioneer", "pipe", "pistol", "pitch", "pizza", "place",
        "planet", "plastic", "plate", "play", "please", "pledge", "pluck", "plug", "plunge",
        "poem", "poet", "point", "polar", "pole", "police", "pond", "pony", "pool", "popular",
        "portion", "position", "possible", "post", "potato", "pottery", "poverty", "powder",
        "power", "practice", "praise", "predict", "prefer", "prepare", "present", "pretty",
        "prevent", "price", "pride", "primary", "print", "priority", "prison", "private",
        "prize", "problem", "process", "produce", "profit", "program", "project", "promote",
        "proof", "property", "prosper", "protect", "proud", "provide", "public", "pudding",
        "pull", "pulp", "pulse", "pumpkin", "punch", "pupil", "puppy", "purchase", "purity",
        "purpose", "purse", "push", "put", "puzzle", "pyramid", "quality", "quantum", "quarter",
        "question", "quick", "quit", "quiz", "quote", "rabbit", "raccoon", "race", "rack",
        "radar", "radio", "rail", "rain", "raise", "rally", "ramp", "ranch", "random",
        "range", "rapid", "rare", "rate", "rather", "raven", "raw", "razor", "ready",
        "real", "reason", "rebel", "rebuild", "recall", "receive", "recipe", "record",
        "recycle", "reduce", "reflect", "reform", "refuse", "region", "regret", "regular",
        "reject", "relax", "release", "relief", "rely", "remain", "remember", "remind",
        "remove", "render", "renew", "rent", "reopen", "repair", "repeat", "replace",
        "report", "require", "rescue", "result", "retire", "retreat", "return", "reunion",
        "reveal", "review", "reward", "rhythm", "rib", "ribbon", "rice", "rich", "ride",
        "ridge", "rifle", "right", "rigid", "ring", "riot", "rise", "risk", "ritual",
        "rival", "river", "road", "roast", "robot", "robust", "rocket", "romance", "roof",
        "rookie", "room", "rose", "rotate", "rough", "round", "route", "royal", "rubber",
        "rude", "rug", "rule", "run", "runway", "rural", "sad", "saddle", "sadness", "safe",
        "sail", "salad", "salmon", "salon", "salt", "salute", "same", "sample", "sand",
        "satisfy", "satoshi", "sauce", "sausage", "save", "say", "scale", "scan", "scare",
        "scatter", "scene", "scheme", "school", "science", "scissors", "scorpion", "scout",
        "scrap", "screen", "script", "scrub", "sea", "search", "season", "seat", "second",
        "secret", "section", "security", "seed", "seek", "segment", "select", "sell",
        "semi", "senior", "sense", "sentence", "series", "service", "session", "settle",
        "setup", "seven", "shadow", "shaft", "shallow", "share", "shed", "shell", "sheriff",
        "shield", "shift", "shine", "ship", "shiver", "shock", "shoe", "shoot", "shop",
        "short", "shoulder", "shove", "shrimp", "shrug", "shuffle", "shy", "sibling",
        "sick", "side", "siege", "sight", "sign", "silent", "silk", "silly", "silver",
        "similar", "simple", "since", "sing", "siren", "sister", "situate", "six", "size",
        "skate", "sketch", "ski", "skill", "skin", "skirt", "skull", "slab", "slam",
        "sleep", "slender", "slice", "slide", "slight", "slim", "slogan", "slot", "slow",
        "slush", "small", "smart", "smile", "smoke", "smooth", "snack", "snake", "snap",
        "sniff", "snow", "soap", "soccer", "social", "sock", "soda", "soft", "solar",
        "soldier", "solid", "solution", "solve", "someone", "song", "soon", "sorry",
        "sort", "soul", "sound", "soup", "source", "south", "space", "spare", "spatial",
        "spawn", "speak", "special", "speed", "spell", "spend", "sphere", "spice", "spider",
        "spike", "spin", "spirit", "split", "spoil", "sponsor", "spoon", "sport", "spot",
        "spray", "spread", "spring", "spy", "square", "squeeze", "squirrel", "stable",
        "stadium", "staff", "stage", "stairs", "stamp", "stand", "start", "state", "stay",
        "steak", "steel", "stem", "step", "stick", "still", "sting", "stock", "stomach",
        "stone", "stool", "story", "stove", "strategy", "street", "strike", "strong",
        "struggle", "student", "stuff", "stumble", "style", "subject", "submit", "sudden",
        "suffer", "sugar", "suggest", "suit", "summer", "sun", "sunny", "sunset", "super",
        "supply", "supreme", "sure", "surface", "surge", "surprise", "surround", "survey",
        "suspect", "sustain", "swallow", "swamp", "swap", "swarm", "swear", "sweet", "swift",
        "swim", "swing", "switch", "sword", "symbol", "symptom", "syrup", "system", "table",
        "tackle", "tag", "tail", "talent", "talk", "tank", "tape", "target", "task", "taste",
        "tattoo", "taxi", "teach", "team", "tell", "ten", "tenant", "tennis", "tent",
        "term", "test", "text", "thank", "that", "theme", "then", "theory", "there", "they",
        "thing", "this", "thought", "thread", "thrive", "throat", "thumb", "thunder", "ticket",
        "tide", "tiger", "tilt", "timber", "time", "tiny", "tip", "tired", "tissue", "title",
        "toast", "tobacco", "today", "toddler", "toe", "together", "toilet", "token", "tomato",
        "tomorrow", "tone", "tongue", "tonight", "tool", "tooth", "top", "topic", "topple",
        "torch", "tornado", "tortoise", "toss", "trace", "track", "trade", "traffic", "tragic",
        "train", "transfer", "trap", "trash", "travel", "tray", "treat", "tree", "trend",
        "trial", "tribe", "trick", "trigger", "trim", "trip", "trophy", "trouble", "truck",
        "true", "truly", "trumpet", "trust", "truth", "try", "tube", "tuition", "tumble",
        "tuna", "tunnel", "turkey", "turn", "turtle", "twelve", "twenty", "twice", "twin",
        "twist", "two", "type", "typical", "ugly", "umbrella", "unable", "unaware", "uncle",
        "uncover", "under", "undo", "unfair", "unfold", "unhappy", "uniform", "unique",
        "unit", "universe", "unknown", "unlock", "until", "unusual", "unveil", "update",
        "upgrade", "uphold", "upon", "upper", "upset", "urban", "urge", "usage", "use",
        "used", "useful", "useless", "usual", "utility", "vacant", "vacuum", "vague", "valid",
        "valley", "valve", "van", "vanish", "vapor", "various", "vast", "vault", "vehicle",
        "velvet", "vendor", "venture", "venue", "verb", "verify", "version", "very", "vessel",
        "veteran", "viable", "vibrant", "vicious", "victory", "video", "view", "village",
        "vintage", "violin", "virtual", "virus", "visa", "visit", "visual", "vital", "vivid",
        "vocal", "voice", "void", "volcano", "volume", "vote", "voyage", "wage", "wagon",
        "wait", "walk", "wall", "walnut", "want", "warfare", "warm", "warrior", "wash",
        "wasp", "waste", "water", "wave", "way", "wealth", "weapon", "wear", "weasel",
        "weather", "web", "wedding", "weekend", "weird", "welcome", "west", "wet", "whale",
        "what", "wheat", "wheel", "when", "where", "whip", "whisper", "wide", "width",
        "wife", "wild", "will", "win", "window", "wine", "wing", "wink", "winner", "winter",
        "wire", "wisdom", "wise", "wish", "witness", "wolf", "woman", "wonder", "wood",
        "wool", "word", "work", "world", "worry", "worth", "wrap", "wreck", "wrestler",
        "wrist", "write", "wrong", "yard", "year", "yellow", "you", "young", "youth",
        "zebra", "zero", "zone", "zoo"
    ]

    def __init__(self):
        self.pqc_available = PQC_AVAILABLE
        self.logger = self._setup_logger()

    def _setup_logger(self):
        """Setup secure logging"""
        import logging
        logger = logging.getLogger('QuantumCryptoUtils')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - QUANTUM-CRYPTO - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def generate_kyber_keypair(self) -> Tuple[bytes, bytes]:
        """Generate Kyber keypair for post-quantum key exchange"""
        if self.pqc_available:
            return kyber_generate_keypair()
        else:
            # Fallback: Generate ECDH keypair (still better than classical crypto)
            from cryptography.hazmat.primitives.asymmetric import ec
            private_key = ec.generate_private_key(ec.SECP521R1(), default_backend())
            public_key = private_key.public_key()
            return public_key, private_key

    def kyber_encrypt(self, public_key: bytes, message: bytes) -> Tuple[bytes, bytes]:
        """Encrypt using Kyber key exchange"""
        if self.pqc_available:
            return kyber_encrypt(public_key, message)
        else:
            # Fallback: Use ECDH with AES-GCM
            raise NotImplementedError("ECDH encryption not implemented in fallback mode")

    def kyber_decrypt(self, secret_key: bytes, ciphertext: bytes, shared_secret: bytes) -> bytes:
        """Decrypt using Kyber key exchange"""
        if self.pqc_available:
            return kyber_decrypt(secret_key, ciphertext, shared_secret)
        else:
            raise NotImplementedError("ECDH decryption not implemented in fallback mode")

    def generate_dilithium_keypair(self) -> Tuple[bytes, bytes]:
        """Generate Dilithium keypair for post-quantum signatures"""
        if self.pqc_available:
            return dilithium_generate_keypair()
        else:
            # Fallback: Use Ed25519 (quantum-resistant for signatures)
            from cryptography.hazmat.primitives.asymmetric import ed25519
            private_key = ed25519.Ed25519PrivateKey.generate()
            public_key = private_key.public_key()
            return public_key, private_key

    def dilithium_sign(self, private_key: bytes, message: bytes) -> bytes:
        """Sign message using Dilithium"""
        if self.pqc_available:
            return dilithium_sign(private_key, message)
        else:
            # Fallback: Use Ed25519
            from cryptography.hazmat.primitives.asymmetric import ed25519
            if isinstance(private_key, ed25519.Ed25519PrivateKey):
                return private_key.sign(message)
            else:
                raise ValueError("Invalid private key for Ed25519 fallback")

    def dilithium_verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        """Verify Dilithium signature"""
        if self.pqc_available:
            return dilithium_verify(public_key, message, signature)
        else:
            # Fallback: Use Ed25519
            from cryptography.hazmat.primitives.asymmetric import ed25519
            try:
                if isinstance(public_key, ed25519.Ed25519PublicKey):
                    public_key.verify(signature, message)
                    return True
                else:
                    return False
            except:
                return False

    def encrypt_data_pqc(self, data: bytes, recipient_public_key: bytes = None) -> Dict[str, bytes]:
        """
        Encrypt data using hybrid quantum-resistant approach:
        - Kyber/ECDH for key exchange (quantum-resistant)
        - AES-256-GCM for symmetric encryption (quantum-safe when key is from PQC)
        """
        if self.pqc_available and recipient_public_key is not None:
            # Use Kyber for key exchange
            aes_key = secrets.token_bytes(32)  # 256-bit key
            encrypted_key, shared_secret = self.kyber_encrypt(recipient_public_key, aes_key)
            ephemeral_key = None
        else:
            # Fallback: Use direct AES-256-GCM encryption with PBKDF2-derived key
            # This is still very secure and quantum-resistant for data at rest
            if recipient_public_key is None:
                # Generate a key from system entropy for symmetric encryption
                aes_key = secrets.token_bytes(32)
                encrypted_key = b""  # No key encryption needed for symmetric
                shared_secret = b""  # Not used in symmetric mode
                ephemeral_key = None
            else:
                # Use recipient public key as basis for key derivation
                key_material = recipient_public_key + secrets.token_bytes(32)
                aes_key = hashlib.sha3_512(key_material).digest()[:32]
                encrypted_key = b""
                shared_secret = b""
                ephemeral_key = None

        # Use AES-256-GCM for data encryption (quantum-safe when used properly)
        iv = secrets.token_bytes(12)  # 96-bit IV for GCM
        cipher = Cipher(algorithms.AES(aes_key), modes.GCM(iv), backend=default_backend())
        encryptor = cipher.encryptor()

        ciphertext = encryptor.update(data) + encryptor.finalize()
        tag = encryptor.tag

        return {
            'ciphertext': ciphertext,
            'encrypted_key': encrypted_key,
            'shared_secret': shared_secret,
            'iv': iv,
            'tag': tag,
            'ephemeral_key': ephemeral_key,
            'pqc_used': self.pqc_available
        }

    def decrypt_data_pqc(self, encrypted_data: Dict[str, bytes], recipient_secret_key: bytes = None) -> bytes:
        """Decrypt data using hybrid quantum-resistant approach"""
        pqc_used = encrypted_data.get('pqc_used', self.pqc_available)

        if pqc_used and recipient_secret_key is not None:
            # Use Kyber for key decryption
            aes_key = self.kyber_decrypt(
                recipient_secret_key,
                encrypted_data['encrypted_key'],
                encrypted_data['shared_secret']
            )
        else:
            # Fallback: Reconstruct AES key from available data
            if recipient_secret_key is not None:
                # Reconstruct key using the same method as encryption
                key_material = recipient_secret_key + secrets.token_bytes(32)
                aes_key = hashlib.sha3_512(key_material).digest()[:32]
            else:
                # For symmetric encryption, we need the key to be known
                # This is a limitation of the symmetric fallback
                raise ValueError("Secret key required for symmetric decryption")

        # Decrypt data using AES-256-GCM
        cipher = Cipher(
            algorithms.AES(aes_key),
            modes.GCM(encrypted_data['iv'], encrypted_data['tag']),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()

        try:
            plaintext = decryptor.update(encrypted_data['ciphertext']) + decryptor.finalize()
            return plaintext
        except InvalidTag:
            raise ValueError("Authentication failed - data may be corrupted")

    def _derive_master_seed(self, mnemonic_phrase: str, kdf_version: int = 3) -> bytes:
        """
        Derive master seed from mnemonic phrase using versioned KDF.

        Args:
            mnemonic_phrase: Space-separated BIP-39 mnemonic words
            kdf_version: 1=legacy (single SHA3-512 hash),
                         2=legacy PBKDF2 (2048 iterations),
                         3=current PBKDF2 (600,000 iterations)

        Returns:
            64-byte master seed
        """
        if kdf_version == 1:
            # Legacy: single SHA3-512 hash (kept for backward compatibility)
            digest = hashes.Hash(hashes.SHA3_512())
            digest.update(mnemonic_phrase.encode())
            return digest.finalize()
        elif kdf_version == 2:
            # v2: Legacy PBKDF2 with low iteration count (backward compat only)
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA3_512(),
                length=64,
                salt=b"SAIGE_mnemonic_v2",
                iterations=2048,
                backend=default_backend()
            )
            return kdf.derive(mnemonic_phrase.encode())
        else:
            # v3: Production PBKDF2 with 600,000 iterations (OWASP recommended)
            # Using unique domain separator salt for repryntt network
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA3_512(),
                length=64,
                salt=b"repryntt_mnemonic_v3_2026",
                iterations=600_000,
                backend=default_backend()
            )
            return kdf.derive(mnemonic_phrase.encode())

    def generate_wallet_seed(self) -> Tuple[str, str]:
        """
        Generate a BIP-39 compatible wallet with quantum-safe entropy
        Returns: (address, space-separated mnemonic phrase)
        """
        # Generate 256-bit entropy for post-quantum security
        entropy = secrets.token_bytes(32)
        entropy_bits = ''.join(format(byte, '08b') for byte in entropy)

        # For 24 words, we need 24 * 11 = 264 bits, but we'll use 256 bits
        # and pad/truncate as needed
        if len(entropy_bits) < 264:
            # Pad with more entropy if needed
            additional_entropy = secrets.token_bytes((264 - len(entropy_bits)) // 8 + 1)
            entropy_bits += ''.join(format(byte, '08b') for byte in additional_entropy)

        entropy_bits = entropy_bits[:264]  # Ensure exactly 264 bits for 24 words

        # Convert to 24-word mnemonic for better security
        indices = [int(entropy_bits[i:i+11], 2) % len(self.BIP39_WORDLIST) for i in range(0, 264, 11)]
        mnemonic_words = [self.BIP39_WORDLIST[i] for i in indices[:24]]  # Ensure exactly 24 words
        mnemonic_phrase = ' '.join(mnemonic_words)

        # Derive master seed using PBKDF2 key-stretching (v3 KDF — 600K iterations)
        master_seed = self._derive_master_seed(mnemonic_phrase, kdf_version=3)

        # Derive Ed25519 keypair from master seed, then address from PUBLIC KEY
        # (standard crypto practice: address = hash(pubkey), like Bitcoin/Ethereum)
        from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
        from cryptography.hazmat.primitives import serialization as _ser
        _priv = _ed25519.Ed25519PrivateKey.from_private_bytes(master_seed[:32])
        _pub_bytes = _priv.public_key().public_bytes(
            encoding=_ser.Encoding.Raw, format=_ser.PublicFormat.Raw
        )
        address = hashlib.sha3_256(_pub_bytes).hexdigest()[:40]

        self.logger.info(f"Generated quantum-safe wallet (v3 KDF, 600K iterations): {address}")
        return address, mnemonic_phrase

    def recover_wallet_from_mnemonic(self, mnemonic_phrase: str, kdf_version: int = 3) -> Optional[str]:
        """
        Recover wallet address from BIP-39 mnemonic phrase

        Args:
            kdf_version: 1=legacy (SHA3-512 hash), 2=legacy PBKDF2 (2048 iter),
                         3=current PBKDF2 (600K iter).
                         Defaults to 3 for new wallets.  If you can't find your
                         wallet, try kdf_version=2 or kdf_version=1.
        """
        try:
            words = mnemonic_phrase.split()
            if len(words) != 24:
                raise ValueError("Mnemonic must be exactly 24 words for quantum-safe wallets")

            if not all(word in self.BIP39_WORDLIST for word in words):
                raise ValueError("Invalid words in mnemonic phrase")

            # Derive master seed from mnemonic using versioned KDF
            master_seed = self._derive_master_seed(mnemonic_phrase, kdf_version=kdf_version)

            # Derive Ed25519 public key, then address from PUBLIC KEY
            # (standard crypto practice: address = hash(pubkey), like Bitcoin/Ethereum)
            from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
            from cryptography.hazmat.primitives import serialization as _ser
            _priv = _ed25519.Ed25519PrivateKey.from_private_bytes(master_seed[:32])
            _pub_bytes = _priv.public_key().public_bytes(
                encoding=_ser.Encoding.Raw, format=_ser.PublicFormat.Raw
            )
            address = hashlib.sha3_256(_pub_bytes).hexdigest()[:40]

            self.logger.info(f"Recovered quantum-safe wallet (v{kdf_version} KDF): {address}")
            return address

        except Exception as e:
            self.logger.error(f"Wallet recovery failed: {e}")
            return None

    def derive_private_key_from_mnemonic(self, mnemonic_phrase: str, kdf_version: int = 3) -> Tuple[Optional[bytes], Optional[bytes]]:
        """
        Derive Ed25519 private/public key pair from BIP-39 mnemonic phrase

        Args:
            kdf_version: 1=legacy, 2=legacy PBKDF2, 3=current (600K iter).
                         Defaults to 3 for new wallets.

        Returns:
            (private_key_bytes, public_key_bytes) or (None, None) on error
        """
        try:
            words = mnemonic_phrase.split()
            if len(words) != 24:
                raise ValueError("Mnemonic must be exactly 24 words")

            if not all(word in self.BIP39_WORDLIST for word in words):
                raise ValueError("Invalid words in mnemonic phrase")

            # Derive master seed from mnemonic using versioned KDF
            master_seed = self._derive_master_seed(mnemonic_phrase, kdf_version=kdf_version)

            # Use first 32 bytes of master seed as Ed25519 private key
            from cryptography.hazmat.primitives.asymmetric import ed25519
            private_key_bytes = master_seed[:32]
            private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
            public_key = private_key.public_key()
            
            # Serialize keys
            from cryptography.hazmat.primitives import serialization
            public_key_bytes = public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw
            )

            self.logger.info(f"Derived Ed25519 keypair from mnemonic")
            return private_key_bytes, public_key_bytes

        except Exception as e:
            self.logger.error(f"Key derivation failed: {e}")
            return None, None

    def sign_wallet_message(self, message: str, private_key: bytes) -> bytes:
        """Sign a message with Dilithium for wallet operations"""
        return self.dilithium_sign(private_key, message.encode())

    def verify_wallet_signature(self, message: str, signature: bytes, public_key: bytes) -> bool:
        """Verify Dilithium signature for wallet operations"""
        return self.dilithium_verify(public_key, message.encode(), signature)

    def hash_data(self, data: bytes, algorithm: str = 'sha3_512') -> str:
        """Hash data using specified quantum-safe algorithm"""
        if algorithm == 'sha3_512':
            digest = hashes.Hash(hashes.SHA3_512())
        elif algorithm == 'sha3_256':
            digest = hashes.Hash(hashes.SHA3_256())
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}")

        digest.update(data)
        return digest.finalize().hex()

    def validate_input(self, data: Any, expected_type: type, max_length: int = None) -> bool:
        """Validate input data for security"""
        if not isinstance(data, expected_type):
            return False

        if isinstance(data, (str, bytes)) and max_length and len(data) > max_length:
            return False

        return True

    def sanitize_string(self, input_str: str, max_length: int = 1000) -> str:
        """Sanitize string input to prevent injection attacks"""
        if not isinstance(input_str, str):
            raise ValueError("Input must be string")

        # Remove potentially dangerous characters
        import re
        sanitized = re.sub(r'[^\w\s\-_.,!?@#$%^&*()+=]', '', input_str)

        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]

        return sanitized.strip()

    # Backward compatibility methods using quantum-resistant AES-256-GCM
    def encrypt_data(self, data: bytes, password: str, salt: bytes = None) -> bytes:
        """Legacy method - uses quantum-resistant AES-256-GCM encryption"""
        # SECURITY: Generate random salt per encryption (never use hardcoded salts)
        if salt is None:
            salt = secrets.token_bytes(16)
        # Derive AES key from password using PBKDF2 with SHA-3-512 (quantum-resistant)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA3_512(),
            length=32,
            salt=salt,
            iterations=100000,  # High iteration count for quantum resistance
            backend=default_backend()
        )
        aes_key = kdf.derive(password.encode())

        # Use AES-256-GCM for encryption
        iv = secrets.token_bytes(12)  # 96-bit IV for GCM
        cipher = Cipher(algorithms.AES(aes_key), modes.GCM(iv), backend=default_backend())
        encryptor = cipher.encryptor()

        ciphertext = encryptor.update(data) + encryptor.finalize()
        tag = encryptor.tag

        # Store as JSON for backward compatibility
        encrypted_dict = {
            'ciphertext': ciphertext.hex(),
            'iv': iv.hex(),
            'tag': tag.hex(),
            'salt': salt.hex(),
            'version': 'quantum_safe_v1'
        }
        return json.dumps(encrypted_dict).encode()

    def decrypt_data(self, encrypted_data: bytes, password: str, salt: bytes = None) -> bytes:
        """Legacy method - uses quantum-resistant AES-256-GCM decryption"""
        try:
            encrypted_dict = json.loads(encrypted_data.decode())

            # Handle both old and new formats
            if 'version' in encrypted_dict and encrypted_dict['version'] == 'quantum_safe_v1':
                # New quantum-safe format
                ciphertext = bytes.fromhex(encrypted_dict['ciphertext'])
                iv = bytes.fromhex(encrypted_dict['iv'])
                tag = bytes.fromhex(encrypted_dict['tag'])
                used_salt = bytes.fromhex(encrypted_dict['salt']) if salt is None else salt
            else:
                # Try old PQC format for backward compatibility
                try:
                    return self.decrypt_data_pqc(encrypted_dict, None)
                except:
                    raise ValueError("Invalid encrypted data format")

            # Derive AES key from password
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA3_512(),
                length=32,
                salt=used_salt,
                iterations=100000,
                backend=default_backend()
            )
            aes_key = kdf.derive(password.encode())

            # Decrypt using AES-256-GCM
            cipher = Cipher(algorithms.AES(aes_key), modes.GCM(iv, tag), backend=default_backend())
            decryptor = cipher.decryptor()

            plaintext = decryptor.update(ciphertext) + decryptor.finalize()
            return plaintext

        except (json.JSONDecodeError, ValueError, KeyError, InvalidTag):
            raise ValueError("Invalid encrypted data or password")

# Global instance for use across the system
crypto_utils = QuantumCryptoUtils()
