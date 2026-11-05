import time
from dataclasses import dataclass
from typing import Any, Dict, Literal

from agent_framework import FunctionTool, Message, add_usage_details, tool
from pydantic import BaseModel, Field

from utils.utils import add_todays_date

# The conversation given to the model is cut to this many characters (its end)
MAX_CONVERSATION_CHARS = 60000


class SimulatedUserStep(BaseModel):
    """Structured output after a reply of the concierge: the next message or the end of the conversation."""

    status: Literal["continue", "goal_reached", "gave_up"] = Field(
        description="continue: send the message; goal_reached: the goal is met; gave_up: the assistant cannot help."
    )
    message: str = Field(description="The next message to the assistant when status is continue, otherwise empty.")
    reason: str = Field(description="One short sentence in English about the decision.")


class SimulatedAnswer(BaseModel):
    """Structured output for a question of a subagent (ask_user)."""

    answer: str = Field(description="The traveller's answer, as typed in a chat.")


@dataclass
class SimulatedConversation:
    """One test conversation of the concierge evaluation, as the simulator sees it.

    turns is the list that EvaluateService fills for the record ({"user", "scope_guard", "reply"} per turn).
    The ask_user tools add the subagents' questions to its last turn, the one whose concierge run is going on.
    """

    # The test case's "user" (goal and facts); None in a scripted case, where every question is skipped
    user: dict | None
    turns: list
    # Summed usage_details and number of the simulator's model calls
    usage: Any = None
    calls: int = 0
    # Time spent in the simulator's model calls: the user's time, not the assistant's
    seconds: float = 0.0
    questions_answered: int = 0


class UserSimulator:
    """Simulated traveller of the concierge evaluation (concierge_agent_and_eval.py).

    The concierge leads a conversation and its subagents ask the user questions (ask_user), so one fixed
    message per test case cannot show how it talks. In a test case with a "user" field (goal and facts),
    a small model (gpt-5-mini, `user_simulator` in config.yaml) plays the traveller, with structured output
    as the scope guard:
    - after every reply of the concierge, next_step() returns the next message, or ends the conversation
      when the goal is reached or the assistant cannot help;
    - the ask_user tools of the subagents (create_ask_user_tool) answer their questions with the facts.
    In a scripted test case the ask_user tools skip every question, as when the user presses Enter.

    The simulator is part of the test, not of the assistant: its tokens and time are reported apart
    (user_simulation in the record), and its verdict "goal reached" is a rule check, not a judge's score.
    Agent Framework has no user simulator, and the Simulator of azure-ai-evaluation always plays to its
    turn limit and cannot answer questions asked inside a concierge run.
    """

    def __init__(self, chat_client, config: dict):
        """
        Args:
            chat_client: FoundryChatClient of the simulator's model deployment.
            config: content of config.yaml; the "user_simulator" section is used.
        """
        settings = config["user_simulator"]
        self.chat_client = chat_client
        self.instructions = settings["instructions"]
        self.question_instructions = settings["question_instructions"]
        self.reasoning_effort = settings["reasoning_effort"]
        # User messages of one conversation, the first one included (max_turns of a test case overrides it)
        self.max_turns = settings["max_turns"]
        self._conversation: SimulatedConversation | None = None

    def start(self, user: dict | None, turns: list) -> SimulatedConversation:
        """Start a test conversation: user = the test case's "user" (None for a scripted case), turns = record["turns"]."""
        self._conversation = SimulatedConversation(user=user, turns=turns)
        return self._conversation

    def finish(self) -> None:
        """End the test conversation; questions asked outside one are skipped."""
        self._conversation = None

    async def next_step(self) -> SimulatedUserStep:
        """After the concierge's last reply: the simulated user's next message, or the end of the conversation.

        Raises on a failed call; the caller ends the conversation and records the error.
        """
        conversation = self._conversation
        if conversation is None or conversation.user is None:
            raise ValueError("next_step needs a simulated test conversation (start() with a user)")
        return await self._ask(conversation, self.instructions, self._input_text(conversation), SimulatedUserStep)

    def create_ask_user_tool(self, topic: str) -> FunctionTool:
        """Return the ask_user tool of one concierge subagent for the evaluation.

        Same name and argument as the chat's create_ask_user_tool (classes/human_review_services.py), which
        the subagents' server-side definitions declare. Every question is logged in the current turn
        ({"topic", "question", "answer"}; answer None when skipped).

        Args:
            topic: What the subagent is responsible for, as in the chat (e.g. "hotel").
        """
        simulator = self

        @tool(name="ask_user")
        async def ask_user(question: str) -> Dict[str, Any]:
            """
            Tool function that gives one question of a subagent to the simulated user.

            Args:
                question: The question in the user's language.

            Returns:
                {"answer": "..."}, or {"skipped": True} in a scripted test case or when the simulator failed.
            """
            return await simulator._answer_question(topic, question)

        return ask_user

    async def _answer_question(self, topic: str, question: str) -> Dict[str, Any]:
        conversation = self._conversation
        if conversation is None or not conversation.turns:
            return {"skipped": True}

        # Built before the question is logged, so the question appears only between <question> tags
        text = self._input_text(conversation, question=(topic, question)) if conversation.user else None
        entry = {"topic": topic, "question": question, "answer": None}
        conversation.turns[-1].setdefault("questions", []).append(entry)

        if text is None:
            print(f"[EVAL] Question from the {topic} specialist skipped: {question}")
            return {"skipped": True}

        try:
            result = await self._ask(conversation, self.question_instructions, text, SimulatedAnswer)
            answer = result.answer.strip()
        except Exception as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            print(f"[SIMULATED USER] Answer failed, the question is skipped: {entry['error']}")
            return {"skipped": True}

        if not answer:
            print(f"[SIMULATED USER] Question from the {topic} specialist skipped: {question}")
            return {"skipped": True}
        entry["answer"] = answer
        conversation.questions_answered += 1
        print(f"[SIMULATED USER] Question from the {topic} specialist: {question}")
        print(f"[SIMULATED USER] Answer: {answer}")
        return {"answer": answer}

    async def _ask(self, conversation: SimulatedConversation, instructions: str, text: str, response_format):
        """One structured call of the simulator's model; usage and time are added to the conversation."""
        started = time.perf_counter()
        try:
            response = await self.chat_client.get_response(
                [Message("user", [text])],
                options={
                    "instructions": instructions,
                    "response_format": response_format,
                    "reasoning": {"effort": self.reasoning_effort},
                    # The service keeps no copy of the test conversation
                    "store": False,
                },
            )
        finally:
            conversation.seconds += time.perf_counter() - started
        conversation.calls += 1
        conversation.usage = add_usage_details(conversation.usage, response.usage_details)
        if response.value is None:
            raise ValueError("the model returned no structured answer")
        return response.value

    @staticmethod
    def _input_text(conversation: SimulatedConversation, question: tuple[str, str] | None = None) -> str:
        """The simulator's input: today's date, goal, facts, the conversation so far and an optional question."""
        lines = []
        for turn in conversation.turns:
            lines.append(f"YOU: {turn['user']}")
            for asked in turn.get("questions", []):
                lines.append(f"QUESTION FROM THE {asked['topic'].upper()} SPECIALIST: {asked['question']}")
                lines.append(f"YOUR ANSWER: {asked['answer'] or '(no answer)'}")
            if "reply" in turn:
                lines.append(f"ASSISTANT: {turn['reply']}")
            lines.append("")
        history = "\n".join(lines).strip()[-MAX_CONVERSATION_CHARS:]

        user = conversation.user
        text = (
            f"<goal>\n{user['goal']}\n</goal>\n\n"
            f"<facts>\n{user.get('facts') or 'none'}\n</facts>\n\n"
            f"<conversation>\n{history}\n</conversation>"
        )
        if question is not None:
            topic, question_text = question
            text += f"\n\n<question>\nThe {topic} specialist asks: {question_text}\n</question>"
        return add_todays_date(text)
