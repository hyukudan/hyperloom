"""Shared physical-device policy for inventory and machine views."""


def resolve(card, declared):
    """Resolve physical identity first; retain the legacy class fallback only for devices other than a 4090."""
    observed = {str(card[key]).casefold() for key in ("uuid", "pci", "id", "device") if card.get(key)}
    matches = [d for d in declared or () if isinstance(d, dict) and any(
        d.get(key) and str(d[key]).casefold() in observed for key in ("uuid", "pci", "id"))]
    if not matches and "4090" not in (card.get("name") or ""):
        matches = [d for d in declared or () if isinstance(d, dict) and d.get("name") and
                   d["name"] == card.get("name")]
    policy = matches[0] if len(matches) == 1 else {}
    excluded = policy.get("excluded", "4090" in (card.get("name") or ""))
    reason = policy.get("reason") or ("RTX 4090 is excluded by policy" if excluded else None)
    return {"excluded": excluded, "policy_reason": reason}
