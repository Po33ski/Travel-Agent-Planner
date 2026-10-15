from typing import Any

from agent_framework import ContextProvider, Message


class TravelHistoryProvider(ContextProvider):
    """Context provider that gives the manager a short note on the user's earlier trips.

    The note holds only the trip summaries and travel keywords the user agreed to save
    (CosmosMemory.add_trip_memory), never the e-mail address or the conversation history.

    - It is attached to the manager only: the manager passes what matters to the specialists
      in its instructions, so the note does not cost tokens in every agent call.
    - It is a user message, not instructions: the system prompt belongs to the server-side agent
      version. FoundryAgent drops system messages, and provider instructions would go out as an
      extra "instructions" field next to the agent reference, which Foundry does not document.
    - It cannot go on the workflow agent: Magentic accepts exactly one task message.
    - Magentic calls the manager several times per task, so the note is read from Cosmos DB
      once and only read again by refresh() after the user saved a new trip.
    """

    def __init__(self, cosmos_memory, user_id: str):
        super().__init__("travel_history")
        self.cosmos_memory = cosmos_memory
        self.user_id = user_id
        self.note: str | None = None
        self.refresh()

    def refresh(self) -> None:
        """Read the saved trips and keywords again and rebuild the note."""
        trips, keywords = self.cosmos_memory.get_trip_memory(self.user_id)
        lines = [f"- {trip['date']}: {trip['summary']}" for trip in trips]
        if keywords:
            lines.append(f"Travel keywords: {', '.join(keywords)}")
        # The manager's system prompt (section "Travel history") describes this first line
        self.note = "Travel history of this user:\n" + "\n".join(lines) if lines else None
        if self.note:
            print(f"[LONG-TERM] Travel history for the manager: {len(trips)} trip(s), {len(keywords)} keyword(s)")

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        if self.note:
            context.extend_messages(self, [Message("user", [self.note])])
