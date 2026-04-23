"""
Device resolver using deterministic TF-vector cosine similarity.

Resolution accuracy improves continuously through the learning store:
on each call, learned phrases are merged into the device corpus before
similarity is computed. Learned phrases are cached in module-level memory
for the lifetime of the Lambda container (typically several hours) to
avoid a DynamoDB read on every request.

No external ML model is invoked. The embedding is a term-frequency vector
over a shared vocabulary built from the query and all device corpora.
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Device catalog — single source of truth for device identity and capabilities
# ---------------------------------------------------------------------------

DEVICE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "office_light",
        "name": "Office Light",
        "ip": "192.168.1.101",
        "device_type": "SmartBulb",
        "capabilities": ["turn_on", "turn_off", "get_status", "toggle", "set_brightness"],
        "sample_phrases": [
            "office light",
            "desk light",
            "room light",
            "ceiling light",
            "light bulb",
            "lamp office",
            "switch light",
            "light in the office",
            "overhead light",
            "brightness light",
            "dim light",
            "brightness control light",
            "adjust brightness light",
        ],
    },
    {
        "id": "office_fan",
        "name": "Office Fan",
        "ip": "192.168.1.102",
        "device_type": "SmartPlug",
        "capabilities": ["turn_on", "turn_off", "get_status", "toggle"],
        "sample_phrases": [
            "office fan",
            "desk fan",
            "room fan",
            "cooling fan",
            "electric fan",
            "fan in office",
            "table fan",
            "switch fan",
            "ventilation",
        ],
    },
]


# ---------------------------------------------------------------------------
# Stop-word list
# ---------------------------------------------------------------------------
# Tokens that carry no device-identification signal are removed before
# vectorisation. Action verbs are included because they have already been
# captured by intent_parser and would only dilute the cosine angle.

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
    # Additional conjugations and contraction fragments
    "am", "im", "re", "ve", "ll", "don", "won", "can",
})


# ---------------------------------------------------------------------------
# In-memory learned-phrase cache
# ---------------------------------------------------------------------------
# Populated lazily on the first resolve_device call and retained for the
# lifetime of the Lambda container. Explicitly invalidated by
# invalidate_learned_phrases_cache() after a POST /learn write.

_learned_phrases_cache: Optional[Dict[str, List[str]]] = None


def invalidate_learned_phrases_cache() -> None:
    """Force a fresh DynamoDB read on the next resolve_device call."""
    global _learned_phrases_cache
    _learned_phrases_cache = None


def _get_learned_phrases() -> Dict[str, List[str]]:
    global _learned_phrases_cache
    if _learned_phrases_cache is None:
        from learning_store import load_all_learned_phrases
        _learned_phrases_cache = load_all_learned_phrases()
    return _learned_phrases_cache


# ---------------------------------------------------------------------------
# TF-vector helpers
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


def _build_device_corpus(device: Dict[str, Any], learned: Dict[str, List[str]]) -> str:
    """
    Combine catalog sample_phrases with any learned phrases for this device.

    The resulting corpus string is tokenised and vectorised during resolution.
    """
    base = list(device["sample_phrases"])
    extra = learned.get(device["id"], [])
    return " ".join(base + extra)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_device(query: str) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Find the best-matching device for a natural-language query.

    Merges catalog phrases with learned phrases from DynamoDB (cached)
    before computing cosine similarity so resolution accuracy improves
    automatically as the system learns from successful executions.

    Returns:
        (device_dict, confidence)  where confidence ∈ [0, 1].
        Returns (None, 0.0) when the catalog is empty or query is blank.
    """
    if not query.strip() or not DEVICE_CATALOG:
        return None, 0.0

    learned = _get_learned_phrases()
    corpora = [_build_device_corpus(d, learned) for d in DEVICE_CATALOG]
    vocab = _build_vocab(corpora + [query])
    query_vec = _tf_vector(query, vocab)

    best_device: Optional[Dict[str, Any]] = None
    best_score = -1.0

    for device, corpus in zip(DEVICE_CATALOG, corpora):
        score = _cosine_similarity(query_vec, _tf_vector(corpus, vocab))
        if score > best_score:
            best_score = score
            best_device = device

    return best_device, round(best_score, 4)


def device_public_view(device: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal fields (IP) before returning a device to API callers."""
    return {
        "id": device["id"],
        "name": device["name"],
        "device_type": device["device_type"],
        "capabilities": device["capabilities"],
    }
