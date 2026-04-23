"""
Device resolver using deterministic TF-vector cosine similarity.

No external ML model is invoked. The embedding is a term-frequency vector
over a shared vocabulary built from the query and all device sample phrases.
This is a valid and fully transparent mock embedding sufficient for a POC
with a small, curated device catalog.
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Device catalog
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
            "turn fan on",
            "switch fan",
            "ventilation",
        ],
    },
]


# ---------------------------------------------------------------------------
# Stop-word list
# ---------------------------------------------------------------------------
# These tokens carry no device-identification signal and would widen the angle
# between the query vector and the device corpus vector unnecessarily.
# Covers common English function words and action verbs handled by the intent
# parser (so they are redundant in the device-resolution step).
_STOP_WORDS: frozenset[str] = frozenset({
    # English function words
    "a", "an", "the", "this", "that", "it", "its",
    "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "off", "at", "by", "with",
    "from", "as", "or", "and", "me", "my", "i", "do",
    "can", "could", "would", "should", "please", "hey",
    # Action verbs resolved by intent_parser — no device signal
    "turn", "switch", "power", "enable", "disable",
    "toggle", "flip", "set", "check", "activate",
    "deactivate", "get", "show", "tell", "make",
    "dim", "darken", "brighten",
    # Filler tokens common in voice-style commands
    "status", "state", "what", "how",
})


# ---------------------------------------------------------------------------
# Internal TF-vector helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercase alphabetic tokens, stop-words removed."""
    return [
        tok for tok in re.findall(r'\b[a-z]+\b', text.lower())
        if tok not in _STOP_WORDS
    ]


def _build_vocab(texts: List[str]) -> List[str]:
    """Sorted deduplicated vocabulary across all texts."""
    vocab: set[str] = set()
    for t in texts:
        vocab.update(_tokenize(t))
    return sorted(vocab)


def _tf_vector(text: str, vocab: List[str]) -> List[float]:
    """
    Term-frequency vector aligned to vocab.

    Each dimension holds the raw count of that token in text.
    """
    counts: Dict[str, int] = {}
    for token in _tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    return [float(counts.get(w, 0)) for w in vocab]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """
    Cosine similarity between two equal-length vectors.

    Returns 0.0 when either vector is the zero vector.
    """
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _device_corpus(device: Dict[str, Any]) -> str:
    """All sample phrases joined into one string for vectorisation."""
    return " ".join(device["sample_phrases"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_device(query: str) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Find the best-matching device from DEVICE_CATALOG for a natural-language query.

    Returns:
        (device_dict, confidence)  where confidence is cosine similarity [0, 1].
        Returns (None, 0.0) when the catalog is empty or the query is blank.
    """
    if not query.strip() or not DEVICE_CATALOG:
        return None, 0.0

    # Build a shared vocabulary from query + every device corpus so vectors
    # are aligned and comparable.
    corpora = [_device_corpus(d) for d in DEVICE_CATALOG]
    all_texts = corpora + [query]
    vocab = _build_vocab(all_texts)

    query_vec = _tf_vector(query, vocab)

    best_device: Optional[Dict[str, Any]] = None
    best_score = -1.0

    for device, corpus in zip(DEVICE_CATALOG, corpora):
        device_vec = _tf_vector(corpus, vocab)
        score = _cosine_similarity(query_vec, device_vec)
        if score > best_score:
            best_score = score
            best_device = device

    return best_device, round(best_score, 4)
