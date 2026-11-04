from functools import lru_cache


class SecretManager:
    """
    Manages secure retrieval of secrets from Azure Key Vault.
    Uses caching to minimize network calls to Key Vault.
    """

    def __init__(self, secret_client):
        """Initialize Key Vault client with managed identity"""
        self.secret_client = secret_client
        self._cache = {}

    @lru_cache(maxsize=128)
    def get_secret(self, secret_name: str) -> str:
        """
        Retrieve a secret from Key Vault with caching.

        Args:
            secret_name: Name of the secret to retrieve

        Returns:
            Secret value as string
        """
        try:
            # Check in-memory cache first
            if secret_name in self._cache:
                return self._cache[secret_name]

            # Retrieve from Key Vault
            secret = self.get_secret_without_caching(secret_name)
            self._cache[secret_name] = secret.value
            return secret.value

        except Exception as e:
            print(f"ERROR: Failed to retrieve secret '{secret_name}': {e}")
            raise

    def get_secret_without_caching(self, secret_name: str) -> str:
        """Retrieve secret without using cache (bypasses lru_cache)"""
        secret = self.secret_client.get_secret(secret_name)
        return secret

    def clear_cache(self):
        """Clear the secret cache"""
        self._cache.clear()
        self.get_secret.cache_clear()
