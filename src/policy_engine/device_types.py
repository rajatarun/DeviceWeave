"""
Canonical device-type vocabulary shared by the policy compiler/validator and
the policy evaluator.

Policies name a device *class* in ``scope.device_type`` (``light``, ``plug``,
``fan`` ...).  Devices in the registry carry the adapter's routing key
(``SmartBulb``, ``GoveePlug``, ``WyzeLock`` ...).  Enforcement must compare
the two in one vocabulary, so both sides are passed through
``canonical_device_type()`` before they are compared or used as a cache key.

Every adapter routing key in ``providers._REGISTRY`` must appear in
``ADAPTER_TYPE_TO_SCOPE`` (enforced by tests/test_policy_device_types.py).
"""

from typing import Dict, FrozenSet, Optional

# Class names a policy may use in scope.device_type.  The first five are the
# original vocabulary; the rest exist so every adapter type has a class.
POLICY_SCOPE_TYPES: FrozenSet[str] = frozenset({
    "fan", "light", "ac", "plug", "heater",
    "switch", "curtain", "lock", "camera", "doorbell",
    "garage_door", "sensor", "thermostat",
})

# Adapter routing key (registry device_type) -> policy scope class.
ADAPTER_TYPE_TO_SCOPE: Dict[str, str] = {
    # Kasa
    "SmartPlug": "plug",
    "SmartBulb": "light",
    # SwitchBot
    "SwitchBotBulb": "light",
    "SwitchBotPlug": "plug",
    "SwitchBotFan": "fan",
    "SwitchBotSwitch": "switch",
    "SwitchBotCurtain": "curtain",
    # Govee
    "GoveeBulb": "light",
    "GoveePlug": "plug",
    # Ring
    "RingDoorbell": "doorbell",
    "RingCamera": "camera",
    "RingLight": "light",
    # MyQ
    "MyQGarageDoor": "garage_door",
    "MyQLamp": "light",
    # Wyze
    "WyzeBulb": "light",
    "WyzePlug": "plug",
    "WyzeSwitch": "switch",
    "WyzeCamera": "camera",
    "WyzeLock": "lock",
    "WyzeMotionSensor": "sensor",
    "WyzeContactSensor": "sensor",
    "WyzeThermostat": "thermostat",
}

_LOWER_ADAPTER = {k.lower(): v for k, v in ADAPTER_TYPE_TO_SCOPE.items()}


def canonical_device_type(device_type: Optional[str]) -> str:
    """
    Map a policy scope name or an adapter routing key to its policy class.

    ``"light"`` -> ``"light"``, ``"SmartBulb"`` -> ``"light"``,
    ``"GoveePlug"`` -> ``"plug"``.  Matching is case-insensitive.  A value
    that is neither a scope class nor a known adapter type is returned
    unchanged, so it only ever matches itself.
    """
    if not isinstance(device_type, str):
        return ""
    if device_type in POLICY_SCOPE_TYPES:
        return device_type
    mapped = ADAPTER_TYPE_TO_SCOPE.get(device_type)
    if mapped is not None:
        return mapped
    key = device_type.strip().lower()
    if key in POLICY_SCOPE_TYPES:
        return key
    return _LOWER_ADAPTER.get(key, device_type)
