from typing import Any

from agent_framework import ContextProvider, Message


class TravelHistoryProvider(ContextProvider):
    """Context provider that gives the Magentic manager or the concierge a short note on the user's earlier trips.

    The note holds only the trip summaries and travel keywords the user agreed to save
    (CosmosMemory.add_trip_memory), never the e-mail address or the conversation history.

    - It is attached to the manager (or the concierge) only: it passes what matters to the
      specialists in its instructions, so the note does not cost tokens in every agent call.
    - It is a user message, not instructions: the system prompt belongs to the server-side agent
      version. FoundryAgent drops system messages, and provider instructions would go out as an
      extra "instructions" field next to the agent reference, which Foundry does not document.
    - It cannot go on the workflow agent: Magentic accepts exactly one task message.
    - Magentic calls the manager several times per task, so the note is read from Cosmos DB
      once and only read again by refresh() after the user saved a new trip.
    - once_per_session=True (concierge chat): the concierge keeps the whole conversation in one
      session, whose history the service stores, so the note is sent with the first message only
      and stays in that history instead of being repeated in every turn.
    """

    def __init__(self, cosmos_memory, user_id: str, once_per_session: bool = False):
        super().__init__("travel_history")
        self.cosmos_memory = cosmos_memory
        self.user_id = user_id
        self.once_per_session = once_per_session
        self.note: str | None = None
        self.refresh()

    def refresh(self) -> None:
        """Read the saved trips and keywords again and rebuild the note."""
        trips, keywords = self.cosmos_memory.get_trip_memory(self.user_id)
        lines = [f"- {trip['date']}: {trip['summary']}" for trip in trips]
        if keywords:
            lines.append(f"Travel keywords: {', '.join(keywords)}")
        # The manager's and the concierge's system prompts (section "Travel history") describe this first line
        self.note = "Travel history of this user:\n" + "\n".join(lines) if lines else None
        if self.note:
            print(f"[LONG-TERM] Travel history for the agent: {len(trips)} trip(s), {len(keywords)} keyword(s)")

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        if not self.note:
            return
        if self.once_per_session:
            # state is kept per session (AgentSession.state) for as long as the session lives
            if state.get("note_sent"):
                return
            state["note_sent"] = True
        context.extend_messages(self, [Message("user", [self.note])])
