"""
Device resolver using deterministic TF-vector cosine similarity.

Devices are loaded from the DynamoDB device registry (DEVICE_REGISTRY_TABLE)
at cold start and cached for the container lifetime.  The static DEVICE_CATALOG
below is used only when the env var is unset (local dev without DynamoDB).

Phrases for each device come from the DynamoDB learning table — the Bedrock-
generated phrases written during ingestion form the primary corpus.  Learned
phrases from successful executions are merged in on top.
"""

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REGISTRY_TABLE: str = os.environ.get("DEVICE_REGISTRY_TABLE", "")


# ---------------------------------------------------------------------------
# Static fallback catalog — used only when DEVICE_REGISTRY_TABLE is not set
# ---------------------------------------------------------------------------

DEVICE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "office_light",
        "name": "Office Light",
        "ip": "192.168.1.101",
        "device_type": "SmartBulb",
        "capabilities": ["turn_on", "turn_off", "get_status", "toggle", "set_brightness"],
        "sample_phrases": [
            "office light", "desk light", "room light", "ceiling light",
            "light bulb", "lamp office", "switch light", "light in the office",
            "overhead light", "brightness light", "dim light",
            "brightness control light", "adjust brightness light",
        ],
    },
    {
        "id": "office_fan",
        "name": "Office Fan",
        "ip": "192.168.1.102",
        "device_type": "SmartPlug",
        "capabilities": ["turn_on", "turn_off", "get_status", "toggle"],
        "sample_phrases": [
            "office fan", "desk fan", "room fan", "cooling fan",
            "electric fan", "fan in office", "table fan", "switch fan", "ventilation",
        ],
    },
]


# ---------------------------------------------------------------------------
# Dynamic device registry cache (DynamoDB)
# ---------------------------------------------------------------------------

_device_registry_cache: Optional[List[Dict[str, Any]]] = None


def invalidate_device_registry_cache() -> None:
    global _device_registry_cache
    _device_registry_cache = None


def _load_device_registry() -> List[Dict[str, Any]]:
    """Scan active devices from DynamoDB and normalise to catalog format."""
    import boto3
    from boto3.dynamodb.conditions import Attr

    try:
        table = boto3.resource("dynamodb").Table(_REGISTRY_TABLE)
        resp = table.scan(
            FilterExpression=Attr("status").eq("active"),
            ProjectionExpression="device_id, #n, device_type, capabilities, ip",
            ExpressionAttributeNames={"#n": "name"},
        )
        items = resp.get("Items", [])
        logger.info("Loaded %d active device(s) from registry.", len(items))
        return [
            {
                "id": item["device_id"],
                "name": item.get("name", item["device_id"]),
                "ip": item.get("ip", ""),
                "device_type": item.get("device_type", "SmartPlug"),
                "capabilities": item.get("capabilities", []),
                "sample_phrases": [],  # phrases come from learning table
            }
            for item in items
        ]
    except Exception as exc:
        logger.warning("Failed to load device registry: %s — falling back to static catalog.", exc)
        return []


def _get_active_catalog() -> List[Dict[str, Any]]:
    """Return the live registry (cached) or the static catalog for local dev."""
    global _device_registry_cache
    if not _REGISTRY_TABLE:
        return DEVICE_CATALOG
    if _device_registry_cache is None:
        _device_registry_cache = _load_device_registry() or DEVICE_CATALOG
    return _device_registry_cache


# ---------------------------------------------------------------------------
# Stop-word list
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
    "am", "im", "re", "ve", "ll", "don", "won", "can",
})


# ---------------------------------------------------------------------------
# In-memory learned-phrase cache
# ---------------------------------------------------------------------------

_learned_phrases_cache: Optional[Dict[str, List[str]]] = None


def invalidate_learned_phrases_cache() -> None:
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
    Combine static sample_phrases (if any) with learned/generated phrases.
    Registry devices have no sample_phrases — their corpus comes entirely
    from the learning table (Bedrock-generated + user-learned phrases).
    """
    base = list(device.get("sample_phrases") or [device["name"]])
    extra = learned.get(device["id"], [])
    return " ".join(base + extra)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_device(query: str) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Find the best-matching device for a natural-language query.

    Loads devices from the DynamoDB registry (cached per container) and
    merges with learned/generated phrases from the learning table before
    computing cosine similarity.

    Returns:
        (device_dict, confidence)  where confidence ∈ [0, 1].
        Returns (None, 0.0) when no devices are available or query is blank.
    """
    catalog = _get_active_catalog()
    if not query.strip() or not catalog:
        return None, 0.0

    learned = _get_learned_phrases()
    corpora = [_build_device_corpus(d, learned) for d in catalog]
    vocab = _build_vocab(corpora + [query])
    query_vec = _tf_vector(query, vocab)

    best_device: Optional[Dict[str, Any]] = None
    best_score = -1.0

    for device, corpus in zip(catalog, corpora):
        score = _cosine_similarity(query_vec, _tf_vector(corpus, vocab))
        if score > best_score:
            best_score = score
            best_device = device

    logger.debug(
        "resolve_device(%r) → %s (%.4f)",
        query, best_device["id"] if best_device else None, best_score,
    )
    return best_device, round(best_score, 4)


def device_public_view(device: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal fields (IP) before returning a device to API callers."""
    return {
        "id": device["id"],
        "name": device["name"],
        "device_type": device["device_type"],
        "capabilities": device["capabilities"],
    }
