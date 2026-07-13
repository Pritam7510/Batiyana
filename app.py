from flask import Flask, render_template, redirect, url_for, request, session, jsonify
from functools import wraps
from datetime import datetime
import os
import re
import time
import hashlib
import logging
import difflib
import traceback
from typing import Dict, List, Optional, Tuple
import requests

from voice_commands import parse_command, get_command_response, CommandType

try:
    from dotenv import load_dotenv  # python-dotenv
    load_dotenv()
except ImportError:
    # python-dotenv isn't installed — GEMINI_API_KEY can still be set directly
    # in the environment; nothing else depends on dotenv.
    pass

import firebase_admin
from firebase_admin import credentials, firestore, auth

# ---------------------------------------------------------------------------
# Firebase (optional) — the whole app works fine without it, using in-memory
# storage instead. If you want real persistence/multi-device sync, drop a
# valid service-account JSON next to this file and set FIREBASE_CRED_PATH /
# FIREBASE_WEB_API_KEY as environment variables.
# ---------------------------------------------------------------------------

FIREBASE_CRED_PATH = os.environ.get(
    "FIREBASE_CRED_PATH", "batiyana-firebase-adminsdk-fbsvc-246d465292.json"
)
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "")

try:
    if not firebase_admin._apps and os.path.exists(FIREBASE_CRED_PATH):
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        FIREBASE_ENABLED = True
    else:
        db = None
        FIREBASE_ENABLED = False
except Exception as e:
    print(f"Firebase initialization warning: {e}")
    FIREBASE_ENABLED = False
    db = None

# ---------------------------------------------------------------------------
# Gemini natural-language voice-command translator (merged from what was
# gemini_command_translator.py). Sits BETWEEN speech-to-text and the existing
# rule-based parse_command() — it never replaces or modifies that parser:
#
#     Speech -> Speech-to-Text -> translate_to_command() -> parse_command() -> Execute
#
# - Never touches parse_command(), execute_voice_command(), or any other
#   existing logic in this file — it only produces a plain command string.
# - If Gemini is unavailable or fails (missing key, timeout, network error,
#   bad response) after one retry, a local heuristic fallback handles common
#   natural phrasings ("i want apple" -> "add apple"); if even that doesn't
#   match, the original text is returned unchanged so parse_command() keeps
#   behaving exactly as it always has.
# - Adds conversation memory (last-referenced item), spoken-number
#   conversion ("two" -> 2, "half dozen" -> 6, ...), synonym normalization,
#   and fuzzy item-name correction against known inventory.
# ---------------------------------------------------------------------------

voice_logger = logging.getLogger("gemini_command_translator")
if not voice_logger.handlers:
    _voice_log_handler = logging.StreamHandler()
    _voice_log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s: %(message)s"))
    voice_logger.addHandler(_voice_log_handler)
voice_logger.setLevel(logging.INFO)

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT: str = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT_SECONDS: float = 6.0
GEMINI_MAX_RETRIES: int = 1  # retry once before giving up

ALLOWED_COMMANDS_HELP = """\
add <qty> <item>
add <item>
remove <item>
remove <qty> <item>
increase <item> <qty>
decrease <item> <qty>
show cart
clear cart
checkout
bill
help"""

GEMINI_SYSTEM_PROMPT = f"""You translate ONE natural spoken sentence from a grocery-cart voice \
assistant into EXACTLY one command from this fixed grammar:

{ALLOWED_COMMANDS_HELP}

Rules:
- Output ONLY the command text. No markdown, no quotes, no explanation, no punctuation.
- Quantities must be plain digits (e.g. "2", not "two").
- Item names should be lowercase and singular where natural (e.g. "apple", not "Apples").
- If the sentence doesn't clearly match any allowed command, output exactly: help

Examples:
"I want apple" -> add apple
"I need two bananas" -> add 2 bananas
"Delete milk" -> remove milk
"Empty my cart" -> clear cart
"What is inside my cart?" -> show cart
"I want to checkout" -> checkout
"Increase apples by two" -> increase apple 2

Sentence: """


# --- Quantity word -> number conversion ---

_WORD_TO_NUMBER: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "half a dozen": 6, "half dozen": 6,
    "a dozen": 12, "dozen": 12,
    "a couple of": 2, "a couple": 2, "couple of": 2, "couple": 2,
    "a pair of": 2, "a pair": 2, "pair of": 2, "pair": 2,
}

# Longest phrases first, so "half a dozen" is matched before "dozen" alone, etc.
_WORD_NUMBER_PATTERN = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in _WORD_TO_NUMBER), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def convert_spoken_numbers(text: str) -> str:
    """Replace spelled-out quantities ('two', 'half dozen', 'pair', ...) with digits."""

    def _replace(match: "re.Match[str]") -> str:
        return str(_WORD_TO_NUMBER[match.group(1).lower()])

    return _WORD_NUMBER_PATTERN.sub(_replace, text)


# --- Synonym / alias normalization ---

VOICE_SYNONYMS: Dict[str, str] = {
    "soft drinks": "coke",
    "soft drink": "coke",
    "coke": "coca cola",
    "biscuits": "biscuit",
    "chips packet": "chips",
    "potatoes": "potato",
    "tomatoes": "tomato",
}

_SYNONYM_PATTERN = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in VOICE_SYNONYMS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def apply_synonyms(text: str) -> str:
    """Normalize known synonyms/aliases to their canonical product name."""

    def _replace(match: "re.Match[str]") -> str:
        return VOICE_SYNONYMS[match.group(1).lower()]

    return _SYNONYM_PATTERN.sub(_replace, text)


# --- Fuzzy item-name correction against known inventory ---

def fuzzy_correct_item(item: str, known_items: Optional[List[str]]) -> str:
    """Correct a misheard/misspelled item name against a list of known items."""
    if not item or not known_items:
        return item

    matches = difflib.get_close_matches(
        item.lower(), [k.lower() for k in known_items], n=1, cutoff=0.72
    )
    return matches[0] if matches else item


# --- Conversation memory: remembers the last item referenced per session,
# so "add two more" / "remove one" work without repeating the item name. ---

class _ConversationMemory:
    def __init__(self) -> None:
        self._last_item_by_session: Dict[str, str] = {}

    def remember(self, session_id: str, item: str) -> None:
        if item:
            self._last_item_by_session[session_id] = item

    def recall(self, session_id: str) -> Optional[str]:
        return self._last_item_by_session.get(session_id)


_voice_memory = _ConversationMemory()

# Matches sentences that reference an action + optional quantity but no item,
# e.g. "add two more", "remove one", "increase by two", "add more".
_IMPLICIT_ITEM_PATTERN = re.compile(
    r"^\s*(add|remove|increase|decrease)\s+(?:by\s+)?(\d+)?\s*"
    r"(more|less|of (?:it|that|them|those))?\s*$",
    re.IGNORECASE,
)


def _resolve_implicit_item(text: str, session_id: str) -> str:
    """If the sentence has an action/quantity but no item, fill in the last-referenced item."""
    match = _IMPLICIT_ITEM_PATTERN.match(text.strip())
    if not match:
        return text

    last_item = _voice_memory.recall(session_id)
    if not last_item:
        return text  # nothing to recall — let it fail naturally downstream

    action, qty, _filler = match.groups()
    action = action.lower()
    qty = qty or "1"

    if action in ("add", "remove"):
        return f"{action} {qty} {last_item}"
    return f"{action} {last_item} {qty}"  # increase/decrease grammar: <item> <qty>


# --- Parsing helpers shared by memory-update + fuzzy correction. Handles
# both command grammars: "add/remove [qty] <item>" and "increase/decrease <item> <qty>" ---

_ADD_REMOVE_PATTERN = re.compile(r"^(add|remove)\s+(?:(\d+)\s+)?(.+)$", re.IGNORECASE)
_INC_DEC_PATTERN = re.compile(r"^(increase|decrease)\s+(.+?)\s+(\d+)$", re.IGNORECASE)


def _parse_action_qty_item(command: str) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Returns (action, qty_or_None, item, grammar) or None if not an item command."""
    stripped = command.strip()

    match = _ADD_REMOVE_PATTERN.match(stripped)
    if match:
        action, qty, item = match.groups()
        return action.lower(), qty, item.strip(), "add_remove"

    match = _INC_DEC_PATTERN.match(stripped)
    if match:
        action, item, qty = match.groups()
        return action.lower(), qty, item.strip(), "inc_dec"

    return None


def _extract_item(command: str) -> Optional[str]:
    parsed = _parse_action_qty_item(command)
    return parsed[2] if parsed else None


def _apply_fuzzy_correction(command: str, known_items: Optional[List[str]]) -> str:
    parsed = _parse_action_qty_item(command)
    if not parsed:
        return command

    action, qty, item, grammar = parsed
    corrected_item = fuzzy_correct_item(item, known_items)

    if grammar == "add_remove":
        qty_part = f"{qty} " if qty else ""
        return f"{action} {qty_part}{corrected_item}"
    return f"{action} {corrected_item} {qty}"


# --- Local heuristic fallback: handles common natural phrasings WITHOUT
# Gemini, so the feature still works reasonably even if GEMINI_API_KEY isn't
# set (or the request fails). This never overrides a successful Gemini
# result; it only kicks in when Gemini returns nothing. ---

_CLEAR_CART_RE = re.compile(
    r"^\s*(?:empty|clear)\s+(?:my |the )?(?:cart|list)\s*[.!]?\s*$", re.IGNORECASE
)
_CHECKOUT_RE = re.compile(
    r"^\s*(?:i want to checkout|checkout|let'?s checkout|proceed to checkout|"
    r"place (?:my )?order)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SHOW_CART_RE = re.compile(
    r"^\s*(?:what'?s in my cart|what is in my cart|what'?s inside my cart|"
    r"show (?:me )?(?:my )?cart|show (?:me )?(?:my )?(?:grocery )?list|"
    r"view (?:my )?cart)\s*[?.]?\s*$",
    re.IGNORECASE,
)
_INCREASE_BY_RE = re.compile(r"^\s*increase\s+(.+?)\s+by\s+(\d+)\s*$", re.IGNORECASE)
_DECREASE_BY_RE = re.compile(r"^\s*decrease\s+(.+?)\s+by\s+(\d+)\s*$", re.IGNORECASE)

# "i want apple", "i need 2 bananas", "get me some milk", "please add bread", ...
_ADD_INTENT_RE = re.compile(
    r"^\s*(?:i want|i need|i would like|i'd like|get me|can you get me|"
    r"could you get me|please add|could you add|add me)\s+"
    r"(?:to (?:buy|get) )?(?:a |an |some )?(\d+)?\s*(.+?)\s*$",
    re.IGNORECASE,
)
# "delete milk", "remove all apples", "i don't need bananas", ...
_REMOVE_INTENT_RE = re.compile(
    r"^\s*(?:delete|remove|get rid of|i don'?t need|i don'?t want|"
    r"i no longer need|take off)\s+(?:all |the |any )?(\d+)?\s*(.+?)"
    r"(?:\s+from (?:my )?(?:cart|list))?\s*$",
    re.IGNORECASE,
)


def _local_heuristic_translate(text: str) -> Optional[str]:
    """Best-effort natural-language -> grammar mapping without calling Gemini."""
    stripped = text.strip()
    if not stripped:
        return None

    if _CLEAR_CART_RE.match(stripped):
        return "clear cart"

    if _CHECKOUT_RE.match(stripped):
        return "checkout"

    if _SHOW_CART_RE.match(stripped):
        return "show cart"

    match = _INCREASE_BY_RE.match(stripped)
    if match:
        item, qty = match.groups()
        return f"increase {item.strip()} {qty}"

    match = _DECREASE_BY_RE.match(stripped)
    if match:
        item, qty = match.groups()
        return f"decrease {item.strip()} {qty}"

    match = _ADD_INTENT_RE.match(stripped)
    if match:
        qty, item = match.groups()
        qty_part = f"{qty} " if qty else ""
        return f"add {qty_part}{item.strip()}"

    match = _REMOVE_INTENT_RE.match(stripped)
    if match:
        qty, item = match.groups()
        qty_part = f"{qty} " if qty else ""
        return f"remove {qty_part}{item.strip()}"

    return None


# --- Gemini call, with one retry ---

def _call_gemini(text: str) -> Optional[str]:
    """Call Gemini once, retry once on failure. Returns the raw command text or None."""
    if not GEMINI_API_KEY:
        return None

    payload = {
        "contents": [{"role": "user", "parts": [{"text": GEMINI_SYSTEM_PROMPT + text}]}],
        "generationConfig": {"temperature": 0},
    }

    last_error: Optional[Exception] = None

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            response = requests.post(
                GEMINI_ENDPOINT,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                last_error = RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:200]}")
                continue

            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                last_error = RuntimeError("Gemini returned no candidates")
                continue

            parts = candidates[0].get("content", {}).get("parts", [])
            command_text = "".join(p.get("text", "") for p in parts).strip()

            # Defensive cleanup in case the model adds quotes/fences/punctuation anyway.
            command_text = command_text.strip("`").strip('"').strip("'").rstrip(".").strip()
            command_text = command_text.splitlines()[0].strip() if command_text else ""

            if not command_text:
                last_error = RuntimeError("Gemini returned empty text")
                continue

            return command_text

        except Exception as e:  # network error, timeout, JSON error, etc.
            last_error = e
            if attempt < GEMINI_MAX_RETRIES:
                time.sleep(0.3)  # brief pause before the retry

    voice_logger.warning("Gemini translation failed after retries: %s", last_error)
    return None


def translate_to_command(
    text: str,
    session_id: str = "default",
    known_items: Optional[List[str]] = None,
) -> str:
    """
    Translate a natural-language sentence into one command in the fixed
    grammar that parse_command() understands (e.g. "add 2 apple",
    "remove milk", "show cart").

    Args:
        text: the raw speech-to-text transcript.
        session_id: identifies the user/session, so conversation memory
            ("add two more") is tracked per user rather than globally.
        known_items: optional list of item names currently known/in the
            cart, used for fuzzy-correcting misheard item names.

    On ANY failure (missing API key, network error, bad response after the
    retry), a local heuristic handles common phrasings; failing that, the
    original text is returned unchanged so parse_command() keeps behaving
    exactly as it always has.
    """
    if not text or not text.strip():
        return text

    original_text = text

    # Local, Gemini-independent normalization first, so even the fallback
    # path (Gemini down) still benefits from these.
    normalized = convert_spoken_numbers(text)
    normalized = apply_synonyms(normalized)
    normalized = _resolve_implicit_item(normalized, session_id)

    gemini_result = _call_gemini(normalized)

    if gemini_result is not None:
        final_command = gemini_result
    else:
        voice_logger.info("Gemini unavailable — trying local heuristic fallback.")
        local_result = _local_heuristic_translate(normalized)
        final_command = local_result if local_result is not None else normalized

    # Fuzzy-correct the item name against known inventory, if provided.
    final_command = _apply_fuzzy_correction(final_command, known_items)

    # Remember the item referenced this turn, for next turn's "add two more".
    item = _extract_item(final_command)
    if item:
        _voice_memory.remember(session_id, item)

    voice_logger.info(
        "voice command | original=%r | normalized=%r | gemini=%r | final=%r",
        original_text, normalized, gemini_result, final_command,
    )

    return final_command


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "batiyana-dev-secret-key-change-in-production")

# ---------------------------------------------------------------------------
# Default / seed data. When Firebase is disabled (the default), everything
# is kept in these in-memory dicts, keyed by user_id, so multiple demo
# accounts don't clobber each other.
# ---------------------------------------------------------------------------

DEFAULT_USER_DATA = {
    "name": "Pritam Deshmukh",
    "email": "pritam.deshmukh@example.com",
    "initials": "PD",
    "language": "English",
    "voice_recognition": True,
    "dark_mode": False,
    "notifications": True,
    "ai_suggestions": True,
}

VALID_USERS = {"pritam": "batiyana123", "demo": "demo"}

DEFAULT_GROCERY_LIST = [
    {"id": 1, "category": "Dairy & Eggs", "name": "Organic Eggs", "meta": "12 pack • Large", "qty": 1, "done": True},
    {"id": 2, "category": "Dairy & Eggs", "name": "Whole Milk", "meta": "1 Gallon", "qty": 2, "done": False},
    {"id": 3, "category": "Fruits", "name": "Honeycrisp Apples", "meta": "Approx. 2 lbs", "qty": 5, "done": False},
    {"id": 4, "category": "Fruits", "name": "Bananas", "meta": "1 Bunch (6-8 ct)", "qty": 1, "done": False},
    {"id": 5, "category": "Bakery", "name": "Sourdough Loaf", "meta": "Fresh baked", "qty": 1, "done": True},
    {"id": 6, "category": "Pantry", "name": "Extra Virgin Olive Oil", "meta": "500ml", "qty": 1, "done": True},
    {"id": 7, "category": "Pantry", "name": "Basmati Rice", "meta": "5 kg bag", "qty": 1, "done": True},
    {"id": 8, "category": "Household", "name": "Laundry Detergent", "meta": "2L bottle", "qty": 1, "done": True},
    {"id": 9, "category": "Household", "name": "Paper Towels", "meta": "6 rolls", "qty": 1, "done": True},
    {"id": 10, "category": "Beverages", "name": "Cold-Pressed Kale Juice", "meta": "350ml bottle", "qty": 1, "done": True},
    {"id": 11, "category": "Beverages", "name": "Coffee Filters", "meta": "Pack of 100", "qty": 1, "done": True},
    {"id": 12, "category": "Snacks", "name": "Blueberry Granola", "meta": "400g pack", "qty": 1, "done": True},
]

DEFAULT_VOICE_HISTORY = [
    {"text": "What's my schedule today?", "when": "2 minutes ago", "status": "Answered"},
    {"text": "Remind me to call Mom at 6pm", "when": "1 hour ago", "status": "Reminder Set"},
    {"text": "Add five bananas and laundry detergent to the cart", "when": "Today, 9:41 AM", "status": "Added to list"},
    {"text": "Remind me to buy coffee filters next time", "when": "Yesterday, 6:15 PM", "status": "Noted"},
]

# Per-user in-memory stores (fallback when Firebase is disabled)
USER_PROFILES = {"demo": DEFAULT_USER_DATA.copy(), "pritam": DEFAULT_USER_DATA.copy()}
USER_GROCERY_LISTS = {
    "demo": [item.copy() for item in DEFAULT_GROCERY_LIST],
    "pritam": [item.copy() for item in DEFAULT_GROCERY_LIST],
}
USER_VOICE_HISTORY = {
    "demo": [entry.copy() for entry in DEFAULT_VOICE_HISTORY],
    "pritam": [entry.copy() for entry in DEFAULT_VOICE_HISTORY],
}

# "Current request" working copies — refreshed per-request in load_user_state()
USER = DEFAULT_USER_DATA.copy()
GROCERY_LIST = [item.copy() for item in DEFAULT_GROCERY_LIST]
VOICE_HISTORY = [entry.copy() for entry in DEFAULT_VOICE_HISTORY]

RECOMMENDATIONS = [
    {"name": "Paneer Cubes", "category": "Dairy", "price": 220.00, "img": "bakery"},
    {"name": "Whole Milk", "category": "Dairy", "price": 65.00, "img": "fruit"},
    {"name": "Brown Bread", "category": "Bakery", "price": 45.00, "img": "granola", "rating": 4.7},
    {"name": "Organic Eggs", "category": "Dairy", "price": 160.00, "img": "kale", "rating": 4.9},
    {"name": "Basmati Rice", "category": "Pantry", "price": 420.00, "img": "fruit"},
    {"name": "Ghee Jar", "category": "Pantry", "price": 320.00, "img": "bakery"},
]

FLASH_DEALS = [
    {"name": "Premium Atta", "meta": "5 kg Pack", "price": 249.00, "old_price": 299.00, "discount": 17},
    {"name": "Fresh Strawberries", "meta": "250g tray", "price": 149.00, "old_price": 189.00, "discount": 21},
    {"name": "Cold Brew Coffee", "meta": "500ml bottle", "price": 179.00, "old_price": 229.00, "discount": 22},
]

HISTORY_STATS = {
    "total_items": 1284,
    "items_growth": 12,
    "total_savings": 432.50,
    "spend_growth": 8,
    "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
    "values": [58, 72, 66, 80, 74, 92],
}

RECENT_TRIPS = [
    {"name": "Weekly Groceries", "date": "June 24, 2026", "items": 24, "total": 142.30, "saved": 12.50, "icon": "basket"},
    {"name": "Household Supplies", "date": "June 18, 2026", "items": 8, "total": 64.15, "saved": 4.20, "icon": "case"},
    {"name": "Dinner Party Prep", "date": "June 12, 2026", "items": 15, "total": 89.00, "saved": 18.40, "icon": "fork"},
]


# ---------------------------------------------------------------------------
# Firebase / in-memory storage helpers
# ---------------------------------------------------------------------------

def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def current_user_id():
    return session.get("username", "demo")


def get_user_data(user_id="demo"):
    """Get user data from Firebase or in-memory storage."""
    if FIREBASE_ENABLED:
        try:
            doc = db.collection("users").document(user_id).get()
            if doc.exists:
                data = doc.to_dict()
                merged = DEFAULT_USER_DATA.copy()
                for key in merged:
                    if key in data:
                        merged[key] = data[key]
                return merged
        except Exception as e:
            print(f"Firebase read error: {e}")
    return USER_PROFILES.get(user_id, DEFAULT_USER_DATA.copy())


def save_user_data(user_id, data):
    """Save user data to Firebase or in-memory storage."""
    global USER
    if FIREBASE_ENABLED:
        try:
            db.collection("users").document(user_id).set(data, merge=True)
        except Exception as e:
            print(f"Firebase write error: {e}")
    USER_PROFILES[user_id] = data.copy()
    USER = data


def get_grocery_list(user_id="demo"):
    """Get grocery list from Firebase or in-memory storage."""
    if FIREBASE_ENABLED:
        try:
            docs = db.collection("users").document(user_id).collection("grocery_list").stream()
            items = [doc.to_dict() for doc in docs]
            if items:
                return items
        except Exception as e:
            print(f"Firebase read error: {e}")
    return [item.copy() for item in USER_GROCERY_LISTS.get(user_id, USER_GROCERY_LISTS.get("demo", []))]


def save_grocery_list(user_id, items):
    """Save grocery list to Firebase or in-memory storage."""
    global GROCERY_LIST
    if FIREBASE_ENABLED:
        try:
            batch = db.batch()
            docs = db.collection("users").document(user_id).collection("grocery_list").stream()
            for doc in docs:
                batch.delete(doc.reference)
            for item in items:
                batch.set(
                    db.collection("users").document(user_id).collection("grocery_list").document(str(item["id"])),
                    item,
                )
            batch.commit()
        except Exception as e:
            print(f"Firebase write error: {e}")
    USER_GROCERY_LISTS[user_id] = [item.copy() for item in items]
    GROCERY_LIST = items


def get_voice_history(user_id="demo"):
    """Get voice history from Firebase or in-memory storage."""
    if FIREBASE_ENABLED:
        try:
            docs = (
                db.collection("users")
                .document(user_id)
                .collection("voice_history")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(10)
                .stream()
            )
            history = []
            for doc in docs:
                data = doc.to_dict()
                timestamp = data.get("timestamp")
                when = data.get("when") or (timestamp.strftime("%b %d, %I:%M %p") if timestamp else "Just now")
                history.append(
                    {
                        "text": data.get("text", ""),
                        "when": when,
                        "status": data.get("status", "Processed"),
                    }
                )
            if history:
                return history
        except Exception as e:
            print(f"Firebase read error: {e}")
    return [entry.copy() for entry in USER_VOICE_HISTORY.get(user_id, USER_VOICE_HISTORY.get("demo", []))]


def save_voice_history(user_id, history):
    """Save voice history to Firebase or in-memory storage."""
    global VOICE_HISTORY
    if FIREBASE_ENABLED:
        try:
            batch = db.batch()
            docs = db.collection("users").document(user_id).collection("voice_history").stream()
            for doc in docs:
                batch.delete(doc.reference)
            for entry in history:
                batch.set(
                    db.collection("users").document(user_id).collection("voice_history").document(),
                    {
                        "text": entry.get("text"),
                        "timestamp": datetime.now(),
                        "when": entry.get("when"),
                        "status": entry.get("status"),
                    },
                )
            batch.commit()
        except Exception as e:
            print(f"Firebase write error: {e}")
    USER_VOICE_HISTORY[user_id] = [entry.copy() for entry in history]
    VOICE_HISTORY = [entry.copy() for entry in history]


def log_voice_command(user_id, transcript, status="Processed"):
    history = get_voice_history(user_id)
    entry = {"text": transcript, "when": "Just now", "status": status}
    history.insert(0, entry)
    history = history[:10]
    save_voice_history(user_id, history)


def clear_user_voice_history(user_id):
    save_voice_history(user_id, [])


def load_user_state():
    if not session.get("logged_in"):
        return
    user_id = current_user_id()

    global USER, GROCERY_LIST, VOICE_HISTORY
    USER = get_user_data(user_id) or DEFAULT_USER_DATA.copy()
    GROCERY_LIST = get_grocery_list(user_id) or []
    VOICE_HISTORY = get_voice_history(user_id) or []


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def load_current_user():
    # Skip static assets — no need to touch storage for CSS/JS/images.
    if request.endpoint == "static":
        return
    load_user_state()


@app.context_processor
def inject_user():
    return {"user": USER, "now": datetime.now(), "currency_symbol": "₹"}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        username = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password", "")

        authenticated = False

        # Try Firebase email/password auth first, if it's actually configured.
        if FIREBASE_ENABLED and FIREBASE_WEB_API_KEY and "@" in username:
            try:
                url = (
                    "https://identitytoolkit.googleapis.com/v1/accounts:"
                    f"signInWithPassword?key={FIREBASE_WEB_API_KEY}"
                )
                payload = {"email": username, "password": password, "returnSecureToken": True}
                response = requests.post(url, json=payload, timeout=5)

                if response.status_code == 200:
                    data = response.json()
                    uid = data["localId"]
                    profile = db.collection("users").document(uid).get()

                    session["logged_in"] = True
                    session["uid"] = uid
                    session["username"] = profile.to_dict().get("username", uid) if profile.exists else uid
                    authenticated = True
                else:
                    error = "Invalid Email or Password"
            except Exception as e:
                print(f"Firebase auth error: {e}")
                error = None  # fall through to local fallback below

        # Local/demo fallback — always available, so the app works without any
        # Firebase setup at all.
        if not authenticated:
            if VALID_USERS.get(username.lower()) == password:
                session["logged_in"] = True
                session["username"] = username.lower()
                authenticated = True
                error = None
            elif error is None:
                error = "That username or password isn't right. Try again."

        if authenticated:
            return redirect(request.args.get("next") or url_for("home"))

    return render_template("login.html", error=error)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not FIREBASE_ENABLED:
        error = "Sign-up requires Firebase to be configured on the server. Use the demo login instead."
        if request.method == "POST":
            return render_template("register.html", error=error)
        return render_template("register.html", error=None, firebase_disabled=True)

    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        name = request.form["name"]

        try:
            user = auth.create_user(email=email, password=password, display_name=name)

            db.collection("users").document(user.uid).set(
                {
                    "uid": user.uid,
                    "username": username,
                    "email": email,
                    "name": name,
                    "initials": (name[:2] or "US").upper(),
                }
            )

            session["logged_in"] = True
            session["uid"] = user.uid
            session["username"] = username

            return redirect(url_for("home"))
        except Exception as e:
            return render_template("register.html", error=str(e))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def home():
    total = len(GROCERY_LIST)
    return render_template(
        "home.html",
        list_preview=GROCERY_LIST[:2],
        total_items=total,
        recommendations=RECOMMENDATIONS[:3],
        voice_notes=VOICE_HISTORY[2:4] or VOICE_HISTORY[:2],
        active="home",
    )


@app.route("/voice")
@login_required
def voice():
    return render_template("voice.html", history=VOICE_HISTORY[:2], active="voice")


@app.route("/voice/submit", methods=["POST"])
@login_required
def voice_submit():
    try:
        payload = request.get_json(silent=True) or {}
        transcript = payload.get("transcript", "").strip()
        confirmed = payload.get("confirmed", False)

        if not transcript:
            return jsonify({"ok": False, "message": "No transcript received."}), 400

        translated = translate_to_command(
            transcript,
            session_id=current_user_id(),
            known_items=[item["name"] for item in GROCERY_LIST],
        )
        command = parse_command(translated)
        if command is None:
            return jsonify({"ok": False, "message": "Unable to understand command."})

        cmd_type = command.get("type")

        try:
            log_voice_command(current_user_id(), transcript, "Processed")
        except Exception as e:
            print("Voice history error:", e)

        if cmd_type == CommandType.UNKNOWN:
            return jsonify(
                {"ok": False, "message": get_command_response(command), "command_type": "unknown"}
            )

        if command.get("needs_confirmation") and not confirmed:
            return jsonify(
                {
                    "ok": False,
                    "needs_confirmation": True,
                    "message": get_command_response(command),
                    "command_type": str(cmd_type.value),
                }
            )

        try:
            action_result = execute_voice_command(command)
        except Exception as e:
            print("Execute command error:", e)
            return jsonify({"ok": False, "message": str(e)}), 500

        response = get_command_response(command, action_result)

        return jsonify(
            {
                "ok": action_result.get("ok", True),
                "message": response,
                "command_type": str(cmd_type.value),
                "action_result": action_result,
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "message": str(e), "trace": traceback.format_exc()}), 500


@app.route("/voice/history/clear", methods=["POST"])
@login_required
def clear_voice_history():
    clear_user_voice_history(current_user_id())
    return jsonify({"ok": True})


def execute_voice_command(command):
    """Dispatch the parsed voice command to its executor."""
    cmd_type = command.get("type")

    if cmd_type == CommandType.ADD_PRODUCT:
        return execute_add_product(command)
    elif cmd_type == CommandType.REMOVE_PRODUCT:
        return execute_remove_product(command)
    elif cmd_type == CommandType.UPDATE_QUANTITY:
        return execute_update_quantity(command)
    elif cmd_type == CommandType.INCREASE_QUANTITY:
        return execute_increase_quantity(command)
    elif cmd_type == CommandType.DECREASE_QUANTITY:
        return execute_decrease_quantity(command)
    elif cmd_type == CommandType.CART_ACTION:
        return execute_cart_action(command)
    elif cmd_type == CommandType.SEARCH:
        return execute_search(command)
    elif cmd_type == CommandType.NAVIGATE:
        return execute_navigation(command)
    elif cmd_type == CommandType.EDIT:
        return execute_edit(command)
    elif cmd_type == CommandType.UTILITY:
        return execute_utility(command)

    return {"ok": False, "message": "Command execution failed."}


def find_item_fuzzy(product_name):
    """Find an item in the grocery list using fuzzy (substring) matching."""
    if not product_name:
        return None

    product_lower = product_name.lower()

    for item in GROCERY_LIST:
        if item["name"].lower() == product_lower:
            return item

    for item in GROCERY_LIST:
        if product_lower in item["name"].lower() or item["name"].lower() in product_lower:
            return item

    return None


def execute_add_product(command):
    product_name = command.get("product")
    qty = command.get("quantity", 1)

    if not product_name:
        return {"ok": False, "message": "Product name not specified."}

    existing_item = find_item_fuzzy(product_name)

    if existing_item:
        existing_item["qty"] += qty
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": existing_item, "navigate": "/list"}
    else:
        new_id = max((item["id"] for item in GROCERY_LIST), default=0) + 1
        new_item = {
            "id": new_id,
            "category": "Pantry",
            "name": product_name.title(),
            "meta": "Added by voice",
            "qty": qty,
            "done": False,
        }
        GROCERY_LIST.append(new_item)
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": new_item, "navigate": "/list"}


def execute_remove_product(command):
    product_name = command.get("product")

    if not product_name:
        return {"ok": False, "message": "Product name not specified."}

    item = find_item_fuzzy(product_name)

    if item:
        GROCERY_LIST.remove(item)
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": item, "navigate": "/list"}
    else:
        return {"ok": False, "message": f"Product '{product_name}' not found in list."}


def execute_update_quantity(command):
    product_name = command.get("product")
    qty = command.get("quantity", 1)

    if not product_name:
        return {"ok": False, "message": "Product name not specified."}

    item = find_item_fuzzy(product_name)

    if item:
        item["qty"] = qty
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": item, "navigate": "/list"}
    else:
        return {"ok": False, "message": f"Product '{product_name}' not found in list."}


def execute_increase_quantity(command):
    """Execute increase quantity command."""
    product_name = command.get("product")
    qty = command.get("quantity", 1)

    if not product_name:
        return {"ok": False, "message": "Product name not specified."}

    item = find_item_fuzzy(product_name)

    if item:
        item["qty"] += qty
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": item, "navigate": "/list"}
    else:
        # BUG FIX: original code was missing this `else` — a not-found item
        # used to fall through and return None, crashing the caller.
        return {"ok": False, "message": f"Product '{product_name}' not found in list."}


def execute_decrease_quantity(command):
    """Execute decrease quantity command."""
    product_name = command.get("product")
    qty = command.get("quantity", 1)

    if not product_name:
        return {"ok": False, "message": "Product name not specified."}

    item = find_item_fuzzy(product_name)

    if item:
        item["qty"] = max(1, item["qty"] - qty)  # Prevent negative quantities
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "item": item, "navigate": "/list"}
    else:
        return {"ok": False, "message": f"Product '{product_name}' not found in list."}


def execute_cart_action(command):
    """Execute cart action command."""
    action = command.get("action", "show")

    if action == "clear":
        GROCERY_LIST.clear()
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "action": "clear"}

    elif action == "show":
        total = len(GROCERY_LIST)
        return {"ok": True, "action": "show", "total_items": total}

    elif action == "checkout":
        GROCERY_LIST.clear()
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "action": "checkout"}

    elif action == "save":
        save_grocery_list(current_user_id(), GROCERY_LIST)
        return {"ok": True, "action": "save"}

    elif action == "cancel":
        return {"ok": True, "action": "cancel"}

    return {"ok": True, "action": action}


def execute_search(command):
    """Execute search command."""
    query = command.get("query", "").lower()

    if not query:
        return {"ok": False, "message": "Search query not specified."}

    results = [
        item for item in GROCERY_LIST
        if query in item["name"].lower() or query in item["category"].lower()
    ]

    return {"ok": True, "query": query, "results": len(results)}


def execute_navigation(command):
    """Execute navigation command."""
    target = command.get("target", "home")
    return {"ok": True, "target": target, "navigate": f"/{target if target != 'back' else 'home'}"}


def execute_edit(command):
    """Execute edit command."""
    action = command.get("action", "edit")

    if action == "rename":
        old_name = command.get("old_name")
        new_name = command.get("new_name")

        item = find_item_fuzzy(old_name)
        if item:
            item["name"] = new_name
            save_grocery_list(current_user_id(), GROCERY_LIST)
            return {"ok": True, "action": "rename", "item": item}
        else:
            return {"ok": False, "message": f"Product '{old_name}' not found."}

    else:
        product = command.get("product")
        item = find_item_fuzzy(product)
        if item:
            return {"ok": True, "action": "edit", "item": item}
        else:
            return {"ok": False, "message": f"Product '{product}' not found."}


def execute_utility(command):
    """Execute utility command."""
    cmd = command.get("command", "help")

    if cmd == "help":
        return {"ok": True, "command": "help"}
    elif cmd == "logout":
        return {"ok": True, "command": "logout", "action": "logout"}
    elif cmd == "refresh":
        return {"ok": True, "command": "refresh", "action": "refresh"}
    elif cmd in ("start", "stop"):
        return {"ok": True, "command": cmd}

    return {"ok": True, "command": cmd}


@app.route("/list")
@login_required
def grocery_list():
    categories = {}
    for item in GROCERY_LIST:
        categories.setdefault(item["category"], []).append(item)
    done_count = sum(1 for i in GROCERY_LIST if i["done"])
    total_count = len(GROCERY_LIST)
    progress = round((done_count / total_count) * 100) if total_count else 0
    return render_template(
        "list.html",
        categories=categories,
        done_count=done_count,
        total_count=total_count,
        progress=progress,
        active="list",
    )


@app.route("/list/toggle/<int:item_id>", methods=["POST"])
@login_required
def toggle_item(item_id):
    for item in GROCERY_LIST:
        if item["id"] == item_id:
            item["done"] = not item["done"]
            save_grocery_list(current_user_id(), GROCERY_LIST)
            break
    return jsonify({"ok": True})


@app.route("/list/qty/<int:item_id>/<direction>", methods=["POST"])
@login_required
def change_qty(item_id, direction):
    for item in GROCERY_LIST:
        if item["id"] == item_id:
            if direction == "up":
                item["qty"] += 1
            elif direction == "down" and item["qty"] > 1:
                item["qty"] -= 1
            save_grocery_list(current_user_id(), GROCERY_LIST)
            return jsonify({"ok": True, "qty": item["qty"]})
    return jsonify({"ok": False}), 404


@app.route("/suggestions")
@login_required
def suggestions():
    return render_template(
        "suggestions.html",
        recommendations=RECOMMENDATIONS,
        deals=FLASH_DEALS,
        active="suggestions",
    )


@app.route("/search")
@login_required
def search():
    query = request.args.get("q", "").strip()
    results = []
    if query:
        query_lower = query.lower()
        results = [
            item for item in GROCERY_LIST
            if query_lower in item["name"].lower() or query_lower in item["category"].lower()
        ]
    return render_template("search.html", query=query, results=results, active="home")


@app.route("/profile")
@login_required
def profile():
    return render_template("settings.html", active="profile")


@app.route("/profile/update", methods=["POST"])
@login_required
def profile_update():
    field = request.form.get("field")
    if field in USER and isinstance(USER[field], bool):
        USER[field] = not USER[field]
        save_user_data(current_user_id(), USER)
    return jsonify({"ok": True, "value": USER.get(field)})


@app.route("/history")
@login_required
def history():
    return render_template(
        "history.html",
        stats=HISTORY_STATS,
        trips=RECENT_TRIPS,
        frequent=RECOMMENDATIONS[:3],
        active="profile",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)