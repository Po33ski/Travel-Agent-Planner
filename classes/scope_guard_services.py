import time
from dataclasses import dataclass, field

from agent_framework import Message
from agent_framework.exceptions import ChatClientContentFilterException
from pydantic import BaseModel, Field

# Name under which the guard's tokens appear in the evaluation reports, next to the agents
SCOPE_GUARD_NAME = "scopeGuard"


class ScopeDecision(BaseModel):
    """Structured output of the classifier."""

    in_scope: bool = Field(description="True when the travel-planning assistant should handle the message.")
    reason: str = Field(description="One short sentence that explains the decision.")


@dataclass
class ScopeCheck:
    """Result of one check. A check that failed lets the message through (see ScopeGuard.check)."""

    in_scope: bool
    reason: str
    duration_seconds: float = 0.0
    usage: dict = field(default_factory=dict)
    error: str | None = None
    # True when the guard is off and no model was called
    skipped: bool = False

    def as_dict(self) -> dict:
        """The check without its token usage, for the evaluation report (the tokens go to token_usage)."""
        return {
            "in_scope": self.in_scope,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "skipped": self.skipped,
        }

    def print_line(self) -> None:
        """Print the outcome as a [SCOPE] console line; nothing when the guard is off."""
        if self.skipped:
            return
        if self.error:
            print(f"[SCOPE] Check failed, the team decides ({self.duration_seconds} s): {self.error}")
        elif self.in_scope:
            print(f"[SCOPE] Travel request ({self.duration_seconds} s): {self.reason}")
        else:
            print(f"[SCOPE] Not a travel request ({self.duration_seconds} s): {self.reason}")


class ScopeGuard:
    """Scope check of the user's message before the team or the single agent runs.

    A small model (gpt-5-mini, `scope_guard` in config.yaml) answers whether the message is a travel
    request, with structured output. An out-of-scope message gets the fixed refusal without any agent
    call. The agents' own scope rules stay as the second layer.

    - Allow-list by nature: the classifier says whether the message IS about travel, so nobody has to
      foresee every unwanted topic.
    - Fails open: when the check itself fails (timeout, service error, unreadable answer), the message
      goes to the team, whose prompt still refuses out-of-scope requests. A message blocked by the content
      filter of the model deployment raises ChatClientContentFilterException, as an agent's input would.
    - The message is passed between <message> tags and the instructions say that it is data, so text
      such as "classify this as travel" does not decide the result.
    """

    def __init__(self, chat_client, config: dict):
        """
        Args:
            chat_client: FoundryChatClient of the classifier's model deployment.
            config: content of config.yaml; the "scope_guard" section is used.
        """
        settings = config["scope_guard"]
        self.chat_client = chat_client
        self.enabled = settings["enabled"]
        self.instructions = settings["instructions"]
        self.refusal = settings["refusal"]
        self.reasoning_effort = settings["reasoning_effort"]

    async def check(self, user_message: str) -> ScopeCheck:
        """Classify one user message (as the user wrote it, without the date line)."""
        if not self.enabled:
            return ScopeCheck(in_scope=True, reason="Scope guard is off (scope_guard.enabled in config.yaml).", skipped=True)

        started = time.perf_counter()
        try:
            response = await self.chat_client.get_response(
                [Message("user", [f"<message>\n{user_message}\n</message>"])],
                options={
                    "instructions": self.instructions,
                    "response_format": ScopeDecision,
                    "reasoning": {"effort": self.reasoning_effort},
                    # The service keeps no copy of the classified message
                    "store": False,
                },
            )
            decision = response.value
            if decision is None:
                raise ValueError("the model returned no structured answer")
        except ChatClientContentFilterException:
            # The input is blocked by the content filter: reported like a blocked agent input
            raise
        except Exception as error:
            return ScopeCheck(
                in_scope=True,
                reason="The check failed, so the team decides.",
                duration_seconds=round(time.perf_counter() - started, 1),
                error=f"{type(error).__name__}: {error}",
            )

        return ScopeCheck(
            in_scope=decision.in_scope,
            reason=decision.reason,
            duration_seconds=round(time.perf_counter() - started, 1),
            usage=dict(response.usage_details or {}),
        )
