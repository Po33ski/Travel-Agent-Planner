import uuid
from datetime import date, datetime, timezone
from typing import Any

from agent_framework import ContextProvider, Message
from azure.core import MatchConditions
from azure.cosmos import exceptions

# The id of the profile item; every user has one, in their own partition
PROFILE_ITEM_ID = "profile"
# Version of the item layout, so a later change can tell old items apart
SCHEMA_VERSION = 1
# Preference fields of the profile. Must match ProfileUpdates in classes/memory_extraction_services.py.
PROFILE_LIST_FIELDS = ("interests", "hotel_preferences", "dietary_needs", "avoid")
PROFILE_TEXT_FIELDS = ("budget_level", "travel_party", "pace", "home_city", "citizenship", "language")
# Labels of the note and of /memory, in the order in which they are shown
PROFILE_LABELS = {
    "interests": "interests",
    "hotel_preferences": "hotel wishes",
    "budget_level": "budget level",
    "travel_party": "travels as",
    "pace": "pace",
    "dietary_needs": "dietary needs",
    "avoid": "avoid",
    "home_city": "home city",
    "citizenship": "citizenship",
    "language": "language",
}
# First line of the note. The concierge's system prompt (section "Travel history") describes it and the labels
# below: "past trip", "upcoming or ongoing trip", "dates unknown", "Recommended then", "Visited (said by the
# user)", "Open then".
NOTE_FIRST_LINE = "Travel history of this user:"
# Attempts of a profile write when another session changed the profile in between (ETag check)
PROFILE_WRITE_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserMemoryStore:
    """Long-term memory of the concierge chat in Azure Cosmos DB, one partition per user (/userId = oid).

    Memory container:
    - one "profile" item per user: the sign-in data (for the greeting only) and the preferences;
    - one "trip" item per saved trip: destination, dates, places recommended and places visited apart,
      open items and a short summary.
    Fine-tuning container: one item per chat, in the chat fine-tuning format. Another purpose and another TTL,
    and never read by the agents.

    Every item has its own "ttl" (cosmos in config.yaml); a write restarts it. A trip is added as a new item,
    so writes stay small and two sessions never overwrite each other's trips. The profile is changed with an
    ETag check: when another session changed it in between, the change is applied again to the new version.
    """

    def __init__(self, memory_container, fine_tuning_container, config: dict):
        self.memory_container = memory_container
        self.fine_tuning_container = fine_tuning_container
        self.memory_ttl = config["cosmos"]["memory_ttl_seconds"]
        self.fine_tuning_ttl = config["cosmos"]["fine_tuning_ttl_seconds"]
        self.max_trips_in_note = config["concierge_memory"]["max_trips_in_note"]
        self.max_items_per_list = config["concierge_memory"]["max_items_per_list"]

    # ------------------------------------------------------------------ profile

    def sign_in(self, user, user_role: str | None = None) -> tuple[dict, bool]:
        """Load or create the profile of the signed-in user and record the visit.

        The visit is a write, so the profile's TTL starts again: the memory of a user who keeps using the chat
        does not expire. Returns the profile and whether the user is new.
        """
        created = False

        def record_visit(profile: dict) -> None:
            nonlocal created
            created = "created_at" not in profile
            profile.setdefault("created_at", _now())
            profile["tenantId"] = user.tenant_id
            profile["displayName"] = user.name
            profile["username"] = user.username
            if user_role:
                profile["userRole"] = user_role
            profile["last_seen"] = _now()

        profile = self._modify_profile(user.user_id, record_visit)
        return profile, created

    def get_preferences(self, user_id: str) -> dict:
        """The user's preferences; an empty dict when there is no profile."""
        profile = self._read_profile(user_id)
        return (profile or {}).get("preferences", {})

    def update_preferences(self, user_id: str, updates: dict) -> None:
        """Merge preference updates (ProfileUpdates as a dict) into the profile.

        Text fields are replaced when the update has a value. A list keeps its entries and gets the new ones
        at the end (an entry already stored, case-insensitive, moves to the end); only the newest
        concierge_memory.max_items_per_list entries are kept.
        """

        def merge(profile: dict) -> None:
            preferences = profile.setdefault("preferences", {})
            for name in PROFILE_TEXT_FIELDS:
                if updates.get(name):
                    preferences[name] = updates[name]
            for name in PROFILE_LIST_FIELDS:
                new_values = updates.get(name) or []
                if not new_values:
                    continue
                new_keys = {value.lower() for value in new_values}
                kept = [value for value in preferences.get(name, []) if value.lower() not in new_keys]
                preferences[name] = (kept + list(new_values))[-self.max_items_per_list:]

        self._modify_profile(user_id, merge)

    def clear_preferences(self, user_id: str) -> None:
        """Delete all preferences; the profile itself (sign-in data) stays."""

        def clear(profile: dict) -> None:
            profile["preferences"] = {}

        self._modify_profile(user_id, clear)

    def _read_profile(self, user_id: str) -> dict | None:
        try:
            return self.memory_container.read_item(item=PROFILE_ITEM_ID, partition_key=user_id)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def _modify_profile(self, user_id: str, change) -> dict:
        """Read the profile, apply change(profile) and write it back, with an ETag check.

        A new profile is created with create_item, which fails when another session created it first; an
        existing one is replaced only if its ETag is unchanged. In both cases the change is applied again to
        the version that is stored now.
        """
        for attempt in range(PROFILE_WRITE_ATTEMPTS):
            profile = self._read_profile(user_id)
            is_new = profile is None
            if is_new:
                profile = {
                    "id": PROFILE_ITEM_ID,
                    "userId": user_id,
                    "type": "profile",
                    "schema_version": SCHEMA_VERSION,
                    "preferences": {},
                }
            change(profile)
            profile["updated_at"] = _now()
            profile["ttl"] = self.memory_ttl
            try:
                if is_new:
                    return self.memory_container.create_item(body=profile)
                return self.memory_container.replace_item(
                    item=PROFILE_ITEM_ID,
                    body=profile,
                    etag=profile["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
            except (exceptions.CosmosResourceExistsError, exceptions.CosmosAccessConditionFailedError):
                if attempt == PROFILE_WRITE_ATTEMPTS - 1:
                    raise
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------ trips

    def list_trips(self, user_id: str) -> list[dict]:
        """All saved trips of the user, newest first (a query inside the user's partition)."""
        trips = list(
            self.memory_container.query_items(
                query="SELECT * FROM c WHERE c.type = 'trip'",
                partition_key=user_id,
            )
        )
        return sorted(trips, key=lambda trip: trip.get("created_at", ""), reverse=True)

    def add_trips(self, user_id: str, trips: list[dict], conversation_id: str | None = None) -> int:
        """Save every trip (TripMemory as a dict) as a new item; returns how many were saved."""
        for trip in trips:
            item = {
                "id": str(uuid.uuid4()),
                "userId": user_id,
                "type": "trip",
                "schema_version": SCHEMA_VERSION,
                **trip,
                "created_at": _now(),
                # Where the trip comes from: the concierge chat and its service-side conversation
                "source": "concierge_chat",
                "conversation_id": conversation_id,
                "ttl": self.memory_ttl,
            }
            self.memory_container.create_item(body=item)
        return len(trips)

    def delete_trip(self, user_id: str, trip_id: str) -> None:
        self.memory_container.delete_item(item=trip_id, partition_key=user_id)

    # ------------------------------------------------------------------ fine-tuning data

    def add_fine_tuning_record(self, user_id: str, messages: list[dict], variant: str = "concierge") -> None:
        """Save one chat in the chat fine-tuning format (system, then user and assistant messages in turn)."""
        self.fine_tuning_container.create_item(
            body={
                "id": str(uuid.uuid4()),
                "userId": user_id,
                "type": "chat",
                "variant": variant,
                "schema_version": SCHEMA_VERSION,
                "created_at": _now(),
                "messages": messages,
                "ttl": self.fine_tuning_ttl,
            }
        )

    def count_fine_tuning_records(self, user_id: str) -> int:
        result = list(
            self.fine_tuning_container.query_items(query="SELECT VALUE COUNT(1) FROM c", partition_key=user_id)
        )
        return result[0] if result else 0

    # ------------------------------------------------------------------ forget

    def forget_all(self, user_id: str) -> int:
        """Delete everything stored about the user in both containers; returns the number of deleted items.

        The items of one partition are few, so they are deleted one by one. Delete-by-partition-key would need
        a preview feature of the account.
        """
        deleted = 0
        for container in (self.memory_container, self.fine_tuning_container):
            ids = list(container.query_items(query="SELECT VALUE c.id FROM c", partition_key=user_id))
            for item_id in ids:
                try:
                    container.delete_item(item=item_id, partition_key=user_id)
                    deleted += 1
                except exceptions.CosmosResourceNotFoundError:
                    pass  # already gone (TTL or another session)
        return deleted

    # ------------------------------------------------------------------ what the concierge and the user see

    def build_travel_history_note(self, user_id: str, today: date | None = None) -> str | None:
        """The note for the concierge: profile preferences and the newest trips; None when there is nothing."""
        today = today or date.today()
        lines = []
        profile_line = format_preferences(self.get_preferences(user_id))
        if profile_line:
            lines.append(f"Profile: {profile_line}")
        trips = self.list_trips(user_id)[: self.max_trips_in_note]
        if trips:
            lines.append("Saved trips, newest first:")
            for trip in trips:
                lines.extend(format_trip(trip, today))
        if not lines:
            return None
        return NOTE_FIRST_LINE + "\n" + "\n".join(lines)

    def describe_for_user(self, user_id: str, today: date | None = None) -> str:
        """Everything stored about the user, for the /memory command (trips numbered for /forget)."""
        today = today or date.today()
        lines = []
        profile_line = format_preferences(self.get_preferences(user_id))
        lines.append(f"Profile: {profile_line or '(no preferences saved)'}")
        trips = self.list_trips(user_id)
        if trips:
            lines.append(f"Saved trips, newest first ({len(trips)}; the concierge gets the newest {self.max_trips_in_note}):")
            for number, trip in enumerate(trips, start=1):
                trip_lines = format_trip(trip, today)
                lines.append(f"{number}. {trip_lines[0][2:]}")
                lines.extend(trip_lines[1:])
        else:
            lines.append("Saved trips: (none)")
        lines.append(f"Chats saved for fine-tuning: {self.count_fine_tuning_records(user_id)}")
        return "\n".join(lines)


def format_preferences(preferences: dict) -> str:
    """One line with the non-empty preferences, e.g. "interests: museums, food; budget level: medium"."""
    parts = []
    for name, label in PROFILE_LABELS.items():
        value = preferences.get(name)
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def trip_status(trip: dict, today: date) -> str:
    """past trip / upcoming or ongoing trip / dates unknown, from the trip's dates and today's date."""
    last_day = trip.get("end_date") or trip.get("start_date")
    if not last_day:
        return "dates unknown"
    try:
        return "past trip" if date.fromisoformat(last_day) < today else "upcoming or ongoing trip"
    except ValueError:
        return "dates unknown"


def format_trip(trip: dict, today: date) -> list[str]:
    """The lines of one trip in the note: a header line starting with "- ", then indented details."""
    place = ", ".join(part for part in (trip.get("city"), trip.get("country")) if part)
    dates = " to ".join(part for part in (trip.get("start_date"), trip.get("end_date")) if part)
    saved = (trip.get("created_at") or "")[:10]
    header = " · ".join(part for part in (place, dates, trip_status(trip, today)) if part)
    lines = [f"- {header}" + (f" (saved {saved})" if saved else "")]
    details = [
        ("Summary", trip.get("summary")),
        ("Travels as", trip.get("travel_party")),
        ("Budget level", trip.get("budget_level")),
        ("Interests", trip.get("interests")),
        ("Recommended then", trip.get("places_recommended")),
        ("Hotels recommended then", trip.get("hotels_recommended")),
        ("Visited (said by the user)", trip.get("places_visited")),
        ("Open then", trip.get("open_items")),
    ]
    for label, value in details:
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            lines.append(f"  {label}: {value}")
    return lines


class UserMemoryProvider(ContextProvider):
    """Context provider that gives the concierge the user's long-term memory once per chat.

    - The note (UserMemoryStore.build_travel_history_note) goes as a user message with the first message of
      the chat and then stays in the conversation that the service stores, so it is not repeated every turn.
    - It is marked as sent only after a turn succeeded (after_run): a first turn that fails is not stored by
      the service, so the note goes again with the next message.
    - A user message, not instructions: the system prompt belongs to the server-side agent version, and
      FoundryAgent drops system messages (as for TravelHistoryProvider of the Magentic chat).
    - Only the concierge gets it. It passes what matters to the subagents in their tasks (for example the
      citizenship to webDataSubagent), so the subagents do not need to ask the user for it.
    """

    def __init__(self, store: UserMemoryStore, user_id: str):
        super().__init__("user_memory")
        self.store = store
        self.user_id = user_id
        self.note: str | None = None
        self.refresh()

    def refresh(self) -> None:
        """Read the memory again and rebuild the note (after a save or a /forget)."""
        self.note = self.store.build_travel_history_note(self.user_id)
        if self.note:
            lines = self.note.splitlines()
            has_profile = any(line.startswith("Profile: ") for line in lines)
            trips = sum(1 for line in lines if line.startswith("- "))
            print(f"[LONG-TERM] Memory for the concierge: {'preferences and ' if has_profile else ''}{trips} trip(s)")

    def note_was_sent(self, session) -> bool:
        """Whether the concierge already has the note in this session's conversation."""
        return bool(session.state.get(self.source_id, {}).get("note_sent"))

    async def before_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        if not self.note or state.get("note_sent"):
            return
        context.extend_messages(self, [Message("user", [self.note])])
        state["note_pending"] = True

    async def after_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        if state.pop("note_pending", False):
            state["note_sent"] = True
