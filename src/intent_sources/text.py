"""
Text intent source — the default entry point.

Accepts {"command": "..."} and wraps it in an IntentPayload with
confidence=1.0 (the user explicitly stated their intent).
"""

from typing import Any, Dict, Optional

from intent_sources.base import BaseIntentSource, IntentPayload


class TextIntentSource(BaseIntentSource):

    def extract(self, body: Dict[str, Any]) -> Optional[IntentPayload]:
        command = (body.get("command") or "").strip()
        if not command:
            return None
        return IntentPayload(
            raw_text=command,
            source="text",
            confidence=1.0,
        )
