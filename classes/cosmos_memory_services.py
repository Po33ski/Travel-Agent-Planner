import uuid
from datetime import datetime

from azure.cosmos import exceptions


class CosmosMemory:
    """
    Long-term user memory using Azure Cosmos DB.
    One document per user: name, role, preferences and conversation_history
    (fine-tuning examples) across sessions.
    """

    def __init__(
        self,
        cosmos_client,
        database_client,
        container_client,
        config,
    ):
        self.cosmos_client = cosmos_client
        self.database_client = database_client
        self.container_client = container_client
        self.ttl_seconds = config["cosmos"]["profile_ttl_seconds"]

    def get_or_create_user(self, user_name, user_role):
        """Return the id of the user with this name; on first use generate an id and create the profile.

        Demo only: the name identifies the user. In production the id would be the object ID (oid claim)
        of the user signed in with Microsoft Entra ID.
        """
        query = "SELECT c.id FROM c WHERE StringEquals(c.userName, @userName, true)"
        items = list(
            self.container_client.query_items(
                query=query,
                parameters=[{"name": "@userName", "value": user_name}],
                enable_cross_partition_query=True,  # the user id (partition key) is not known yet
            )
        )
        if items:
            user_id = items[0]["id"]
            print(f"[LONG-TERM] Welcome back, {user_name} (user id: {user_id})")
            return user_id

        user_id = str(uuid.uuid4())
        profile = self._new_profile(user_id)
        profile["userName"] = user_name
        profile["userRole"] = user_role
        self._upsert_profile(profile)
        print(f"[LONG-TERM] Created profile for new user: {user_name} (user id: {user_id})")
        return user_id

    def get_user_or_default_profile(self, user_id):
        user_prefs = self.get_user_profile(user_id)
        if user_prefs is None:
            user_prefs = self._get_default_preferences()
            self.save_user_profile(user_id, user_prefs)
        return user_prefs

    def get_user_profile(self, user_id):
        """Retrieve user preferences from Cosmos DB."""
        # self.debug_container_contents()  # Debugging line to check container contents

        try:
            item = self.container_client.read_item(item=user_id, partition_key=user_id)
            print(f"[LONG-TERM] Loaded profile for user: {user_id}")
            return item["preferences"]
        except exceptions.CosmosResourceNotFoundError:
            print(f"[LONG-TERM] No existing profile found for user: {user_id}")
            return None
        except Exception as error:
            print(f"[LONG-TERM] Error reading profile: {error}")
            return None

    def save_user_profile(self, user_id, preferences):
        """Save or update user preferences in Cosmos DB; the other profile fields stay unchanged."""
        try:
            profile = self._read_or_new_profile(user_id)
            profile["preferences"] = preferences
            self._upsert_profile(profile)
            print(f"[LONG-TERM] Saved profile for user: {user_id}")
            return True
        except Exception as error:
            print(f"[LONG-TERM] Error saving profile: {error}")
            return False

    def update_preference(self, user_id, key, value):
        """Update a single preference field."""
        current = self.get_user_profile(user_id) or {}
        current[key] = value
        return self.save_user_profile(user_id, current)

    def add_conversation(self, user_id, record, preference_updates=None):
        """Append one fine-tuning example (system, user and assistant message) to conversation_history.

        preference_updates (e.g. {"language": "Polish"}) are merged into the preferences in the same write.
        """
        try:
            profile = self._read_or_new_profile(user_id)
            profile.setdefault("conversation_history", []).append(record)
            if preference_updates:
                profile.setdefault("preferences", {}).update(preference_updates)
                print(f"[LONG-TERM] Updated preferences: {preference_updates}")
            self._upsert_profile(profile)
            print(f"[LONG-TERM] Saved example {len(profile['conversation_history'])} for user: {user_id}")
            return True
        except Exception as error:
            print(f"[LONG-TERM] Error saving conversation: {error}")
            return False

    def _new_profile(self, user_id):
        """Return a new profile document; the partition key (/userId) equals the document id."""
        return {
            "id": user_id,
            "userId": user_id,
            "preferences": self._get_default_preferences(),
            "conversation_history": [],
        }

    def _read_or_new_profile(self, user_id):
        """Return the whole profile document, or a new one if the user has none."""
        try:
            return self.container_client.read_item(item=user_id, partition_key=user_id)
        except exceptions.CosmosResourceNotFoundError:
            return self._new_profile(user_id)

    def _upsert_profile(self, profile):
        """Write the whole profile; every write restarts the TTL countdown."""
        profile["last_updated"] = datetime.now().isoformat()
        profile["ttl"] = self.ttl_seconds
        self.container_client.upsert_item(profile)

    def _get_default_preferences(self):
        """Return a default preferences dictionary."""
        return {
            "language": "English",  # language name, as saved by AzureNLPService.extract_preferences
            "language_preference": "professional",
            "email_address": None,
        }

    def debug_container_contents(self):
        """Query all items in container to see what actually exists"""
        print("=== CONTAINER DEBUG ===")

        # 1. Check container partition key definition
        container_props = self.container_client.read()
        print(f"Partition key path: {container_props['partitionKey']['paths']}")

        # 2. Query ALL items (this WILL find your profile if it exists)
        query = "SELECT * FROM c"
        try:
            items = list(
                self.container_client.query_items(
                    query=query,
                    enable_cross_partition_query=True,  # Critical for this to work
                )
            )
            print(f"Total items in container: {len(items)}")
            for item in items:
                print(
                    f"  - id: '{item['id']}', userId: '{item['userId']}', partition key value: '{item['userId']}'"
                )
        except Exception as e:
            print(f"Query failed: {e}")

        # 3. Try to find your specific profile
        profile_id = "luke.ginn"  # Replace with actual user_id
        query2 = "SELECT * FROM c WHERE c.id = @id"
        items2 = list(
            self.container_client.query_items(
                query=query2,
                parameters=[{"name": "@id", "value": profile_id}],
                enable_cross_partition_query=True,
            )
        )
        print(f"Query for id '{profile_id}': {len(items2)} items found")

        return items
