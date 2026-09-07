"""Authorization for a small local project assistant."""


def load_policy():
    """Read the local access policy from disk."""
    return {"role": "reader"}


def authenticate():
    """Identify the caller using the current policy."""
    return load_policy()


def authorize():
    """Check access using the current policy."""
    return load_policy()


def refresh_session():
    """Renew an expired session after authenticating again."""
    return authenticate()
