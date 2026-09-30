"""The tutor's memory: an append-only event log in the tutor-memory repo, and what's derived from it.

Nothing here edits past events. Knowledge, reviews and the briefing are recomputed from
the log every time, so they can't drift from what actually happened.
"""
