import os

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token


def build_verifier() -> StaticTokenVerifier | None:
    """Build a bearer-token verifier from LENS_AUTH_TOKENS (format: "user:token,user:token").

    Returns None (auth disabled) if the variable is unset — the right default for a
    purely local, single-user self-hosted instance with no public exposure.
    """
    raw = os.environ.get("LENS_AUTH_TOKENS", "")
    if not raw.strip():
        return None

    tokens: dict[str, dict] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        client_id, _, token = pair.partition(":")
        if not client_id or not token:
            raise ValueError(f"Invalid LENS_AUTH_TOKENS entry: {pair!r} (expected user:token)")
        tokens[token] = {"client_id": client_id, "scopes": []}

    return StaticTokenVerifier(tokens=tokens)


def current_owner() -> str | None:
    """Return the authenticated client_id for this request, or None if auth is disabled."""
    token = get_access_token()
    return token.client_id if token else None
