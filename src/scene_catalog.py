"""
Scene catalog — contextual, multi-device execution plans.

A scene maps a situational phrase ("I'm starting work", "it's too hot")
to an ordered list of device actions. Scene resolution uses the same
TF-vector cosine similarity approach as device resolution so natural
language variations are handled without exhaustive pattern lists.

Adding a new scene: append an entry to SCENE_CATALOG. No other code
changes required.
"""

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_SCENE_TABLE_NAME: str = os.environ.get("SCENE_TABLE_NAME", "")
_deleted_ids_cache: Optional[Set[str]] = None


# ---------------------------------------------------------------------------
# Scene catalog
# ---------------------------------------------------------------------------

SceneAction = Dict[str, Any]   # {"device_id": str, "action": str, "params": dict}
Scene = Dict[str, Any]

SCENE_CATALOG: List[Scene] = [
    {
        "id": "work_mode",
        "name": "Work Mode",
        "description": "Optimise the environment for focused work — lights on, fan on.",
        "sample_phrases": [
            "i'm starting work",
            "starting work",
            "work mode",
            "office mode",
            "focus mode",
            "begin work session",
            "i am working now",
            "time to work",
            "work from home",
            "let's get to work",
        ],
        "actions": [
            {"device_id": "office_light", "action": "turn_on",      "params": {}},
            {"device_id": "office_fan",   "action": "turn_on",      "params": {}},
        ],
    },
    {
        "id": "cooling_mode",
        "name": "Cooling Mode",
        "description": "Activate the fan to cool the room.",
        "sample_phrases": [
            "it's too hot",
            "too hot here",
            "cooling mode",
            "i'm hot",
            "it is hot",
            "cool the room",
            "make it cooler",
            "warm in here",
            "feeling warm",
            "need cooling",
        ],
        "actions": [
            {"device_id": "office_fan", "action": "turn_on", "params": {}},
        ],
    },
    {
        "id": "all_off",
        "name": "All Off",
        "description": "Power off all devices in the environment.",
        "sample_phrases": [
            "turn off everything",
            "all off",
            "everything off",
            "shutdown everything",
            "leaving",
            "i'm leaving",
            "leaving now",
            "heading out",
            "going home",
            "goodnight",
            "good night",
            "i'm done",
            "done for today",
            "end work session",
            "power off everything",
            "shut it all down",
            "bye",
            "see you later",
        ],
        "actions": [
            {"device_id": "office_light", "action": "turn_off", "params": {}},
            {"device_id": "office_fan",   "action": "turn_off", "params": {}},
        ],
    },
    {
        "id": "evening_mode",
        "name": "Evening Mode",
        "description": "Dim lights to 30 % and switch off the fan for winding down.",
        "sample_phrases": [
            "evening mode",
            "relax mode",
            "chill mode",
            "wind down",
            "relaxing now",
            "softer lighting",
            "dim the room",
            "cozy mode",
            "night mode",
            "i'm relaxing",
        ],
        "actions": [
            {"device_id": "office_light", "action": "set_brightness", "params": {"brightness": 30}},
            {"device_id": "office_fan",   "action": "turn_off",       "params": {}},
        ],
    },
    {
        "id": "presentation_mode",
        "name": "Presentation Mode",
        "description": "Full brightness, fan off — quiet and bright for presenting.",
        "sample_phrases": [
            "presentation mode",
            "meeting mode",
            "presenting now",
            "starting a meeting",
            "on a call",
            "video call",
            "conference mode",
            "demo mode",
        ],
        "actions": [
            {"device_id": "office_light", "action": "set_brightness", "params": {"brightness": 100}},
            {"device_id": "office_fan",   "action": "turn_off",       "params": {}},
        ],
    },
]


# ---------------------------------------------------------------------------
# Stop-words — same set as device_resolver for consistent tokenisation
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "it", "its",
    "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "off", "at", "by", "with",
    "from", "as", "or", "and", "me", "my", "i", "do",
    "can", "could", "would", "should", "please", "hey",
    "turn", "switch", "power", "enable", "disable",
    "toggle", "flip", "set", "check", "activate",
    "deactivate", "get", "show", "tell", "make",
    "status", "state", "what", "how", "dim", "darken", "brighten",
    "let", "now", "here", "all",
    # Additional conjugations and contraction fragments
    "am", "im", "re", "ve", "ll", "don", "won", "can",
})


# ---------------------------------------------------------------------------
# TF-vector cosine similarity (duplicated intentionally — scene_catalog must
# be importable without importing device_resolver to avoid circular imports)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return [
        tok for tok in re.findall(r'\b[a-z]+\b', text.lower())
        if tok not in _STOP_WORDS and len(tok) > 1
    ]


def _build_vocab(texts: List[str]) -> List[str]:
    vocab: set[str] = set()
    for t in texts:
        vocab.update(_tokenize(t))
    return sorted(vocab)


def _tf_vector(text: str, vocab: List[str]) -> List[float]:
    counts: Dict[str, int] = {}
    for tok in _tokenize(text):
        counts[tok] = counts.get(tok, 0) + 1
    return [float(counts.get(w, 0)) for w in vocab]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Scene persistence — deleted scene IDs stored in DynamoDB SceneTable
# ---------------------------------------------------------------------------

def _load_deleted_ids() -> Set[str]:
    if not _SCENE_TABLE_NAME:
        return set()
    import boto3
    from boto3.dynamodb.conditions import Attr
    try:
        table = boto3.resource("dynamodb").Table(_SCENE_TABLE_NAME)
        resp = table.scan(FilterExpression=Attr("status").eq("deleted"))
        return {item["scene_id"] for item in resp.get("Items", [])}
    except Exception as exc:
        logger.warning("Could not load deleted scene IDs: %s", exc)
        return set()


def _get_deleted_ids() -> Set[str]:
    global _deleted_ids_cache
    if _deleted_ids_cache is None:
        _deleted_ids_cache = _load_deleted_ids()
    return _deleted_ids_cache


def invalidate_scene_cache() -> None:
    global _deleted_ids_cache
    _deleted_ids_cache = None


def get_active_scenes() -> List[Scene]:
    """Return SCENE_CATALOG minus any scenes deleted via delete_scene()."""
    deleted = _get_deleted_ids()
    return [s for s in SCENE_CATALOG if s["id"] not in deleted]


def delete_scene(scene_id: str) -> bool:
    """
    Persist a scene deletion to DynamoDB.
    Returns True if the scene_id exists in the catalog, False otherwise.
    """
    if not any(s["id"] == scene_id for s in SCENE_CATALOG):
        return False
    if _SCENE_TABLE_NAME:
        import boto3
        from datetime import datetime, timezone
        try:
            table = boto3.resource("dynamodb").Table(_SCENE_TABLE_NAME)
            table.put_item(Item={
                "scene_id": scene_id,
                "status": "deleted",
                "deleted_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.error("Failed to persist scene deletion: %s", exc)
            raise
    invalidate_scene_cache()
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_scene(query: str) -> Tuple[Optional[Scene], float]:
    """
    Find the best-matching scene using nearest-neighbour phrase similarity.

    For each scene, compute cosine similarity between the query and EACH
    individual sample phrase, then take the maximum.  A query of "leaving"
    scores 1.0 against the exact phrase "leaving" — unlike a corpus-based
    approach where the rich scene vocabulary would dilute a sparse query.

    Choosing max over all phrases per scene is equivalent to a 1-nearest-
    neighbour classifier over the scene's example set, which is semantically
    correct: "how similar is this query to the closest known trigger?"

    Returns:
        (scene, confidence)  where confidence ∈ [0, 1].
        Returns (None, 0.0) if the catalog is empty or the query is blank.
    """
    query_tokens = _tokenize(query)
    active = get_active_scenes()
    if not query_tokens or not active:
        return None, 0.0

    best_scene: Optional[Scene] = None
    best_score = -1.0

    for scene in active:
        scene_max = 0.0
        for phrase in scene["sample_phrases"]:
            # Build vocab only from this query↔phrase pair.
            # Extra dimensions added by a wider vocab would be zero in both
            # vectors and therefore do not affect cosine similarity — building
            # per-pair keeps the comparison focused and is mathematically
            # equivalent.
            vocab = _build_vocab([query, phrase])
            if not vocab:
                continue
            q_vec = _tf_vector(query, vocab)
            p_vec = _tf_vector(phrase, vocab)
            sim = _cosine_similarity(q_vec, p_vec)
            if sim > scene_max:
                scene_max = sim

        if scene_max > best_score:
            best_score = scene_max
            best_scene = scene

    return best_scene, round(best_score, 4)


def scene_public_view(scene: Scene) -> Dict[str, Any]:
    """Strip internal fields before returning a scene to API callers."""
    return {
        "id": scene["id"],
        "name": scene["name"],
        "description": scene["description"],
        "action_count": len(scene["actions"]),
        "sample_phrases": scene["sample_phrases"],
    }
