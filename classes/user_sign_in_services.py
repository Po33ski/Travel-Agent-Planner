from dataclasses import dataclass

import msal


@dataclass(frozen=True)
class SignedInUser:
    """The user who signed in to the concierge chat, from the claims of the ID token."""

    # oid: object ID of the account, created by Microsoft Entra ID with the account. It never changes and is
    # never given to another account, so the chat uses it as the user's key (partition key in Cosmos DB).
    user_id: str
    # tid: the tenant of the account (the same fixed value for every personal Microsoft account). The same person
    # has another oid in another tenant, so the tenant is stored next to the key.
    tenant_id: str
    # For the greeting only: names and usernames can change and are not unique
    name: str | None
    username: str | None


def sign_in_user(client_id: str, settings: dict) -> SignedInUser | None:
    """Sign the user in with Microsoft Entra ID in the browser and return who signed in.

    MSAL opens the sign-in page, receives the answer on http://localhost (the redirect URI of the app
    registration) and checks the ID token it got straight from Entra ID. Nothing is cached on disk, so every
    start of the chat asks again, and "select_account" lets another person choose their own account.

    The sign-in only says whose memory the chat uses. The chat still reaches Azure (Key Vault, Foundry, Cosmos DB)
    with the developer's az login (DefaultAzureCredential).

    Args:
        client_id: application (client) ID of the app registration (Key Vault secret ENTRA-APP-CLIENT-ID).
        settings: the "sign_in" section of config.yaml (authority, scopes, timeout_seconds).

    Returns:
        The signed-in user, or None when the sign-in failed or was cancelled.
    """
    app = msal.PublicClientApplication(client_id, authority=settings["authority"])
    print("[SIGN-IN] Sign in with your Microsoft account in the browser window that opens...")
    try:
        result = app.acquire_token_interactive(
            scopes=list(settings["scopes"]),
            prompt="select_account",
            timeout=settings["timeout_seconds"],
        )
    except Exception as error:
        print(f"[SIGN-IN] Sign-in failed: {error}")
        return None

    claims = result.get("id_token_claims") if isinstance(result, dict) else None
    if not claims or not claims.get("oid") or not claims.get("tid"):
        reason = result.get("error_description") or result.get("error") if isinstance(result, dict) else None
        print(f"[SIGN-IN] Sign-in failed: {reason or 'no ID token with an object ID'}")
        return None

    return SignedInUser(
        user_id=claims["oid"],
        tenant_id=claims["tid"],
        name=claims.get("name"),
        username=claims.get("preferred_username"),
    )
