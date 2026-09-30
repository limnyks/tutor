"""What the log says about each topic: evidence and when to review. Facts, never scores.

A topic counts as known only through answers to questions (lessons, tests, practice).
Studying it or covering it in a summary is recorded, but is not evidence of knowing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .catalog import Course

# Days until the next review after 1, 2, 3, 4, 5+ correct answers in a row.
REVIEW_INTERVALS = [1, 3, 7, 16, 35]


@dataclass
class Answer:
    ts: str
    correct: bool
    source: str
    error: str = ""


@dataclass
class TopicState:
    course: str
    topic: str
    title: str
    answers: list[Answer] = field(default_factory=list)
    studied: list[str] = field(default_factory=list)      # self-study reports (dates)
    stuck_on: list[str] = field(default_factory=list)     # what the reports said was unclear
    covered: list[str] = field(default_factory=list)      # lesson summaries mentioning it

    @property
    def streak(self) -> int:
        """Correct answers in a row, counting back from the latest."""
        n = 0
        for a in reversed(self.answers):
            if not a.correct:
                break
            n += 1
        return n

    @property
    def last_answer(self) -> Answer | None:
        return self.answers[-1] if self.answers else None

    def next_review(self) -> date | None:
        last = self.last_answer
        if last is None:
            return None
        answered = _day(last.ts)
        if not last.correct:
            return answered  # wrong last time: review right away
        return answered + timedelta(days=REVIEW_INTERVALS[min(self.streak, len(REVIEW_INTERVALS)) - 1])

    def review_due(self, today: date) -> bool:
        nxt = self.next_review()
        return nxt is not None and nxt <= today

    @property
    def unchecked_study(self) -> bool:
        """Studied or covered after the last answer (or never answered): needs a quick check."""
        latest_seen = max(self.studied + self.covered, default="")
        return bool(latest_seen) and (self.last_answer is None or latest_seen > self.last_answer.ts)

    def facts(self) -> str:
        """One line of evidence, in plain facts."""
        parts = []
        if self.answers:
            right = sum(a.correct for a in self.answers)
            last = self.last_answer
            parts.append(f"{len(self.answers)} answers, {right} right; last {'right' if last.correct else 'wrong'} "
                         f"on {_day(last.ts)}" + (f" ({last.error})" if not last.correct and last.error else ""))
            parts.append(f"next review {self.next_review()}")
        else:
            parts.append("no answers yet")
        if self.studied:
            parts.append(f"studied {_day(self.studied[-1])}")
        if self.stuck_on:
            parts.append(f"stuck on: {self.stuck_on[-1]}")
        return "; ".join(parts)


def _day(ts: str) -> date:
    return datetime.fromisoformat(ts).date()


def build(course: Course, events: list[dict]) -> dict[str, TopicState]:
    """Topic id -> state, from the memory events of one course."""
    states = {t.id: TopicState(course.slug, t.id, t.title) for t in course.topics}
    for ev in events:
        if ev.get("course") != course.slug:
            continue
        data = ev.get("data", {})
        if ev["type"] == "answer" and data.get("topic") in states:
            states[data["topic"]].answers.append(
                Answer(ev["ts"], bool(data.get("correct")), data.get("source", ""), data.get("error", "")))
        elif ev["type"] == "study":
            for topic in data.get("topics", []):
                if topic in states:
                    states[topic].studied.append(ev["ts"])
                    if data.get("stuck_on"):
                        states[topic].stuck_on.append(data["stuck_on"])
        elif ev["type"] == "summary":
            for topic in data.get("topics", []):
                if topic in states:
                    states[topic].covered.append(ev["ts"])
    return states
