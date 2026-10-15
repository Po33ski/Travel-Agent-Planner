from typing import Any, Dict
from agent_framework import tool
from agent_framework.orchestrations import MagenticPlanReviewRequest, MagenticPlanReviewResponse

# Human in the loop for the interactive chat: the user approves the manager's plan and the
# hotels that hotelAgent chose. Enter alone approves.
APPROVE_ANSWERS = ("", "y", "yes", "t", "tak")
REJECT_ANSWERS = ("n", "no", "nie")


def review_plan(request: MagenticPlanReviewRequest) -> MagenticPlanReviewResponse:
    """Show the manager's plan in the console and return the user's approval or corrections.

    Magentic asks for a review after the first plan and after every replan (also when the team
    is stuck). Corrections go to the manager as a user message, and it plans again.
    """
    print("\n" + "=" * 50)
    print("NEW PLAN (the team made no progress):" if request.is_stalled else "PLAN:")
    print("=" * 50)
    print(request.plan.text)
    print("=" * 50)
    while True:
        try:
            answer = input("Start with this plan? [Y/n], or type what to change: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.lower() in APPROVE_ANSWERS:
            return request.approve()
        # Magentic cannot reject a plan, only revise it, so a bare "no" needs a correction
        if answer.lower() in REJECT_ANSWERS:
            print("Type what the plan should change.")
            continue
        return request.revise(answer)


@tool
def ask_user_about_hotels(hotels: str) -> Dict[str, Any]:
    """
    Tool function that shows the hotels hotelAgent chose and asks the user in the console whether they are OK.
    Agent Framework runs a sync tool in a worker thread, so input() does not block the event loop.

    Args:
        hotels: The chosen hotels, one line per hotel, in the user's language.

    Returns:
        {"approved": True}, or {"approved": False, "comment": "..."} with what the user wants changed
        ("" when the user only said no).
    """
    print("\n" + "=" * 50)
    print("HOTEL REVIEW:")
    print("=" * 50)
    print(hotels)
    print("=" * 50)
    try:
        answer = input("Are these hotels OK? [Y/n], or type what to change: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer.lower() in APPROVE_ANSWERS:
        return {"approved": True}
    if answer.lower() in REJECT_ANSWERS:
        return {"approved": False, "comment": ""}
    return {"approved": False, "comment": answer}


# hotelAgent's server-side definition has the review tool, so the evaluation must also run one.
# Nobody answers there: every choice is approved and the agent replies as without the review.
@tool(name="ask_user_about_hotels")
def approve_hotels_automatically(hotels: str) -> Dict[str, Any]:
    """Stand-in for ask_user_about_hotels in the evaluation: approves every choice without asking."""
    return {"approved": True}
