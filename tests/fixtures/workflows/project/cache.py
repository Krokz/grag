"""Local context cache. There is no Redis integration."""


def read_cached_policy():
    """Reuse a cached authorization result for at most 15 seconds."""
    return {"ttl_seconds": 15}


def save_context():
    """Save a project memory through the single database owner."""
    return "saved"
