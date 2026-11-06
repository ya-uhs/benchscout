"""Questions from the agent to the human, answered in the inbox (design.md section 8, 17).

The agent asks through the agent channel (MCP tool `ask_user`) and the call waits, like an
approval, until the human picks an option or writes something. Only the control channel
(the human's panel) can answer.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable


@dataclass
class Question:
    id: str
    text: str
    options: list[str]
    status: str = "pending"  # pending | answered | withdrawn
    choice: str | None = None  # one of options, if picked
    reply: str | None = None  # free text, if written
    kind: str = "question"  # "question" | "devices" (the agent asks to use devices; see Broker.request_devices)
    data: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)


class QuestionBook:
    def __init__(self, on_event: Callable[[dict], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self._emit = on_event or (lambda e: None)
        self._clock = clock
        self._cond = threading.Condition()
        self._items: dict[str, Question] = {}
        self.on_answer: Callable[[Question], None] | None = None  # e.g. the broker acting on a devices request
        self._answering: set[str] = set()  # answered, being acted on: the asker is woken only after that

    def ask(self, text: str, options: list[str], kind: str = "question", data: dict | None = None) -> Question:
        q = Question(uuid.uuid4().hex[:6], text, [str(o) for o in options], kind=kind, data=data or {})
        with self._cond:
            self._items[q.id] = q
        self._emit({"event": "question_asked", "question": asdict(q)})
        return q

    def wait(self, question_id: str, timeout: float) -> Question | None:
        deadline = self._clock() + max(timeout, 0)
        with self._cond:
            while True:
                q = self._items.get(question_id)
                if q is None or q.status != "pending":
                    return q
                left = deadline - self._clock()
                if left <= 0:
                    return q
                self._cond.wait(min(left, 1.0))

    def answer(self, question_id: str, choice: str | None = None, reply: str | None = None) -> Question:
        """Control channel only."""
        with self._cond:
            q = self._items.get(question_id)
            if q is None:
                raise KeyError(f"no question {question_id!r}")
            if q.status != "pending" or question_id in self._answering:
                raise ValueError(f"{question_id} is already {'being answered' if q.status == 'pending' else q.status}")
            if choice is None and reply is not None and reply.strip() in q.options:
                choice, reply = reply.strip(), None  # a typed option counts as picking it
            if choice is not None and choice not in q.options:
                raise ValueError(f"{choice!r} is not one of the options: {', '.join(q.options)}")
            if choice is None and not (reply or "").strip():
                raise ValueError("pick an option or write a reply")
            q.choice, q.reply = choice, (reply or None)
            self._answering.add(question_id)
        try:
            if self.on_answer:  # e.g. select the devices first, so the agent's call returns the new selection
                self.on_answer(q)
        except Exception:
            with self._cond:
                self._answering.discard(question_id)
                q.choice, q.reply = None, None
                self._cond.notify_all()
            raise
        with self._cond:
            self._answering.discard(question_id)
            q.status = "answered"
            self._cond.notify_all()
        self._emit({"event": "question_answered", "question": asdict(q)})
        return q

    def pending(self) -> list[Question]:
        with self._cond:
            return [q for q in self._items.values() if q.status == "pending"]
