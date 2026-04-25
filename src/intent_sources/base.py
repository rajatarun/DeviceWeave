"""
Abstract intent source interface.

Intent is the contract between the outside world and the DeviceWeave
execution pipeline. Today intent comes from typed text. Tomorrow it
can come from a camera observing a room, an occupancy sensor, a
calendar event, or any other ambient signal — without changing anything
downstream (resolution, policy, execution, learning).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class IntentPayload:
    """
    Normalised intent regardless of how it entered the system.

    The pipeline (scene resolver, device resolver, LLM fallback, policy
    engine) operates on raw_text and never needs to know the source.
    source and metadata are passed through to the API response for
    observability and debugging.
    """
    raw_text: str               # command text fed into the resolution pipeline
    source: str                 # "text" | "vision" | "sensor"
    confidence: float = 1.0    # 1.0 for explicit text; model confidence for inferred sources
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseIntentSource(ABC):

    @abstractmethod
    def extract(self, body: Dict[str, Any]) -> Optional[IntentPayload]:
        """
        Extract a normalised IntentPayload from a raw request body.
        Returns None when the body does not match this source's expected shape.
        """
