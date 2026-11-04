import json
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from agent_framework import Message
from pydantic import BaseModel, Field

from utils.utils import add_todays_date

# Name under which the extraction appears in the console
MEMORY_EXTRACTOR_NAME = "memoryExtractor"
# The chat text given to the model is cut to this many characters (its end, where the plan is final)
MAX_CONVERSATION_CHARS = 60000


# Structured output of the extraction. Every field is required and has no default, as structured output asks
# for; an empty list or null means "nothing new". The field names must match PROFILE_LIST_FIELDS,
# PROFILE_TEXT_FIELDS and the trip items in classes/user_memory_services.py, and the instructions in
# concierge_memory.instructions in config.yaml.
class ProfileUpdates(BaseModel):
    """New or changed preferences of the user that hold for future trips."""

    interests: list[str] = Field(description="Interests the user stated, e.g. 'museums', 'food'.")
    hotel_preferences: list[str] = Field(description="Hotel wishes the user stated, e.g. 'quiet hotel'.")
    dietary_needs: list[str] = Field(description="Dietary needs the user stated, e.g. 'vegetarian'.")
    avoid: list[str] = Field(description="What the user does not want, e.g. 'long hikes'.")
    budget_level: Literal["low", "medium", "high"] | None = Field(description="Usual budget level.")
    travel_party: str | None = Field(description="Who usually travels, e.g. 'couple'.")
    pace: Literal["relaxed", "intense"] | None = Field(description="Preferred pace of a trip.")
    home_city: str | None = Field(description="The city the user lives in or travels from.")
    citizenship: str | None = Field(description="The user's citizenship (passport), e.g. 'Polish'.")
    language: str | None = Field(description="Language the user writes in, as an English name.")


class TripMemory(BaseModel):
    """One destination city that the conversation planned."""

    city: str
    country: str | None
    start_date: str | None = Field(description="YYYY-MM-DD, or null when unknown.")
    end_date: str | None = Field(description="YYYY-MM-DD, or null when unknown.")
    travel_party: str | None
    budget_level: Literal["low", "medium", "high"] | None
    interests: list[str]
    places_recommended: list[str] = Field(description="Places the concierge recommended and the user did not reject.")
    hotels_recommended: list[str] = Field(description="Hotels the concierge recommended or the user chose.")
    places_visited: list[str] = Field(description="Only places the user said they have already been to.")
    open_items: list[str] = Field(description="Next steps offered or left open, not done yet.")
    summary: str = Field(description="One or two sentences about the plan.")


class MemoryProposal(BaseModel):
    """What the extraction proposes to save; the user accepts or rejects all of it."""

    profile_updates: ProfileUpdates
    trips: list[TripMemory]

    def is_empty(self) -> bool:
        updates = self.profile_updates.model_dump()
        return not self.trips and not any(updates.values())


@dataclass
class ChatTurn:
    """One answered turn of the concierge chat."""

    user_message: str  # as the user wrote it, without the date line
    reply: str
    # Questions of the subagents in this turn (create_ask_user_tool): {"topic", "question", "answer"}
    specialist_questions: list[dict] = field(default_factory=list)


def conversation_text(turns: list[ChatTurn]) -> str:
    """The chat as plain text for the extraction, with the subagents' questions where they were asked."""
    lines = []
    for turn in turns:
        lines.append(f"USER: {turn.user_message}")
        for question in turn.specialist_questions:
            lines.append(f"QUESTION FROM THE {question['topic'].upper()} SPECIALIST: {question['question']}")
            lines.append(f"USER'S ANSWER: {question['answer'] or '(skipped)'}")
        lines.append(f"CONCIERGE: {turn.reply}")
        lines.append("")
    return "\n".join(lines).strip()


class MemoryExtractor:
    """Proposes what to remember from a finished concierge chat: profile updates and trips.

    A small model (gpt-5-mini, concierge_memory in config.yaml) reads the chat and the current profile and
    answers with structured output, as the scope guard does. It replaces the Azure AI Language summary and
    key phrases that the Magentic chat uses: these give free text, the concierge needs fields (interests,
    budget, citizenship, places recommended and places visited apart).
    """

    def __init__(self, chat_client, config: dict):
        """
        Args:
            chat_client: FoundryChatClient of the extraction's model deployment.
            config: content of config.yaml; the "concierge_memory" section is used.
        """
        settings = config["concierge_memory"]
        self.chat_client = chat_client
        self.instructions = settings["instructions"]
        self.reasoning_effort = settings["reasoning_effort"]

    async def extract(self, turns: list[ChatTurn], current_preferences: dict) -> MemoryProposal | None:
        """Return the proposal, or None when the call failed (the chat then saves no memory)."""
        conversation = conversation_text(turns)[-MAX_CONVERSATION_CHARS:]
        text = add_todays_date(
            f"<current_profile>\n{json.dumps(current_preferences or {}, ensure_ascii=False)}\n</current_profile>\n\n"
            f"<conversation>\n{conversation}\n</conversation>"
        )
        try:
            response = await self.chat_client.get_response(
                [Message("user", [text])],
                options={
                    "instructions": self.instructions,
                    "response_format": MemoryProposal,
                    "reasoning": {"effort": self.reasoning_effort},
                    # The service keeps no copy of the chat
                    "store": False,
                },
            )
            proposal = response.value
            if proposal is None:
                raise ValueError("the model returned no structured answer")
        except Exception as error:
            print(f"[LONG-TERM] The memory extraction failed, nothing is saved: {type(error).__name__}: {error}")
            return None
        return clean_proposal(proposal)


def clean_proposal(proposal: MemoryProposal) -> MemoryProposal:
    """Drop empty entries and dates that are not YYYY-MM-DD, and trips without a city."""
    updates = proposal.profile_updates
    for name in ("interests", "hotel_preferences", "dietary_needs", "avoid"):
        setattr(updates, name, _clean_list(getattr(updates, name)))
    for name in ("travel_party", "home_city", "citizenship", "language"):
        value = getattr(updates, name)
        setattr(updates, name, value.strip() if value and value.strip() else None)

    trips = []
    for trip in proposal.trips:
        if not trip.city or not trip.city.strip():
            continue
        trip.city = trip.city.strip()
        trip.start_date = _clean_date(trip.start_date)
        trip.end_date = _clean_date(trip.end_date)
        for name in ("interests", "places_recommended", "hotels_recommended", "places_visited", "open_items"):
            setattr(trip, name, _clean_list(getattr(trip, name)))
        trip.summary = trip.summary.strip()
        trips.append(trip)
    proposal.trips = trips
    return proposal


def _clean_list(values: list[str]) -> list[str]:
    """Strip the entries and drop empty ones and case-insensitive duplicates, keeping the order."""
    seen, cleaned = set(), []
    for value in values:
        value = (value or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            cleaned.append(value)
    return cleaned


def _clean_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None
