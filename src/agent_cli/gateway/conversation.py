"""Root-prompt conversation memory: recent turns, so follow-ups like "do that" resolve.

In memory for one shell session and never written to canonical task state. Every consumer gets
a compact view: the last few turns, each truncated, so routing stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_cli.gateway.intent import RootIntent


@dataclass(frozen=True, slots=True)
class Turn:
    user: str
    assistant: str
    intent: RootIntent


@dataclass(frozen=True, slots=True)
class View:
    """How much of the conversation one consumer sees."""

    turns: int
    user_chars: int
    assistant_chars: int


ROUTER_VIEW = View(turns=4, user_chars=400, assistant_chars=600)
"""Enough to resolve references; small enough for a low-cost model."""
ANSWER_VIEW = View(turns=6, user_chars=1000, assistant_chars=2000)
HANDOFF_VIEW = View(turns=4, user_chars=1000, assistant_chars=3000)
"""What the orchestrator sees of the conversation a request refers to."""
KEPT_TURNS = 12


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(slots=True)
class Conversation:
    turns: list[Turn] = field(default_factory=list)

    def add(self, user: str, assistant: str, intent: RootIntent) -> None:
        self.turns.append(Turn(user, assistant, intent))
        del self.turns[:-KEPT_TURNS]

    def recent(self, view: View) -> tuple[dict[str, str], ...]:
        """The last ``view.turns`` turns, oldest first, as compact ``user``/``assistant`` pairs."""
        return tuple(
            {
                "user": clip(turn.user, view.user_chars),
                "assistant": clip(turn.assistant, view.assistant_chars),
            }
            for turn in self.turns[-view.turns :]
        )

    def transcript(self, view: View) -> str:
        """The same view as plain text, for context recorded with delegated work."""
        return "\n\n".join(
            f"User: {turn['user']}\nAssistant: {turn['assistant']}" for turn in self.recent(view)
        )
