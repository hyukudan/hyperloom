"""Shared physical-device policy for inventory and machine views."""
from .config import DEFAULT_DEVICE_POLICIES


def resolve(card, declared, *, placement=False):
    """Resolve physical identity first. Ambiguous configuration is always excluded."""
    default = {} if placement else next((p for p in DEFAULT_DEVICE_POLICIES
                    if p["name_contains"].casefold() in (card.get("name") or "").casefold()), {})
    observed = {str(card[key]).casefold() for key in ("uuid", "pci", "id", "device") if card.get(key)}
    matches = [d for d in declared or () if isinstance(d, dict) and any(
        d.get(key) and str(d[key]).casefold() in observed for key in ("uuid", "pci", "id"))]
    if not matches and not placement and not default.get("require_identity"):
        matches = [d for d in declared or () if isinstance(d, dict) and d.get("name") and
                   d["name"] == card.get("name")]
    if len(matches) > 1:
        return {"excluded": True, "policy_reason": "ambiguous configured device identity"}
    policy = matches[0] if matches else {}
    if placement:
        excluded = not matches or policy.get("excluded", False)
        return {"excluded": excluded, "policy_reason": policy.get("reason") if matches else "device is not configured"}
    excluded = policy.get("excluded", default.get("excluded", False))
    reason = policy.get("reason") or (default.get("reason", "RTX 4090 is excluded by policy") if excluded else None)
    return {"excluded": excluded, "policy_reason": reason}
