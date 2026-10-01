"""What a tool did (ToolResult), and a level-3 action waiting for the owner's confirmation (Plan)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent import Script


@dataclass
class ToolResult:
    tool: str
    ok: bool
    say: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    """A destructive or outward action (a deletion, a message, a call, a move) resolved to one exact
    item, awaiting confirmation."""

    tool: str
    item_id: int
    what: str  # e.g. the note's text or the event's title and time
    hi: bool
    path: str = ""  # files.trash: the exact file shown in the confirmation
    script: Script | None = None  # mac.do: the generated script, shown in full in the confirmation
    ask: str = ""  # the spoken question, when it isn't "Delete …?" (an email's read-back)
    detail: str = ""  # shown in full in the confirmation card (the email)
    ref: Any = None  # email.send: the Gmail draft; message.send, call.start, files.move …: what to do
    cancel: str = ""  # spoken when the owner says no (default: "Okay, I won't delete it.")
