"""Courses and their topics, read from <memory>/courses/*.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Topic:
    id: str
    title: str
    week: int | None = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class Course:
    slug: str
    code: str
    name: str
    moodle_id: int | None
    credits: int
    mode: str  # "tutor": lessons and knowledge tracking; "deadlines": tracked, not tutored
    ai_policy: str = ""
    # True only when the course's AI policy allows AI help on graded homework (tutoring, not writing it).
    homework_help: bool = False
    assessment: list[str] = field(default_factory=list)
    notes: str = ""
    topics: list[Topic] = field(default_factory=list)

    def topic(self, query: str) -> Topic:
        """A topic by id, or by a unique match on its title or keywords."""
        q = _norm(query)
        for t in self.topics:
            if _norm(t.id) == q:
                return t
        matches = [t for t in self.topics
                   if q in _norm(t.title) or any(q == _norm(k) or q in _norm(k) for k in t.keywords)]
        if len(matches) == 1:
            return matches[0]
        options = ", ".join(f"{t.id} ({t.title})" for t in (matches or self.topics))
        raise ValueError(f"Topic '{query}' in {self.code} is {'ambiguous' if matches else 'unknown'}. "
                         f"Use one of: {options}")


def _norm(text: str) -> str:
    return re.sub(r"[\s_\-]+", " ", text.lower()).strip()


_TOPIC_FIELDS = set(Topic.__dataclass_fields__)
_COURSE_FIELDS = set(Course.__dataclass_fields__) - {"topics"}


def load_catalog(memory_dir: Path) -> dict[str, Course]:
    courses = {}
    for path in sorted((memory_dir / "courses").glob("*.json")):
        raw = json.loads(path.read_text())
        # Unknown keys are ignored, so a newer course file never breaks an older connector.
        topics = [Topic(**{k: v for k, v in t.items() if k in _TOPIC_FIELDS}) for t in raw.pop("topics", [])]
        course = Course(**{k: v for k, v in raw.items() if k in _COURSE_FIELDS}, topics=topics)
        courses[course.slug] = course
    return courses


def find_course(catalog: dict[str, Course], query: str) -> Course:
    """A course by slug, code (e.g. MATH252), Moodle id or a unique part of its name."""
    q = _norm(str(query))
    for c in catalog.values():
        if q in (_norm(c.slug), _norm(c.code), str(c.moodle_id)):
            return c
    matches = [c for c in catalog.values() if q in _norm(c.name)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"Course '{query}' is {'ambiguous' if matches else 'unknown'}. "
                     f"Use one of: {', '.join(c.code for c in catalog.values())}")
