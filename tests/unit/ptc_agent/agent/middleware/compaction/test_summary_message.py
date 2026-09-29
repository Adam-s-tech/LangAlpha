"""Round-trip tests for ``build_summary_message`` / ``parse_summary_message``.

``parse_summary_message`` recovers the raw summary text a checkpoint stores so
the projector can re-emit the ``context_window`` event on replay. It slices by
the stamped ``summary_length`` rather than string-splitting on the note, so a
summary that itself contains the note text survives the round-trip.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from ptc_agent.agent.middleware.compaction.types import CONTEXT_SUMMARY_PREFIX
from ptc_agent.agent.middleware.compaction.utils import (
    _LEGACY_FILE_NOTE,
    build_summary_message,
    parse_summary_message,
)
from ptc_agent.agent.transcript import TranscriptTarget
from ptc_agent.agent.transcript.pointer import summary_resumes_at, transcript_note

TRANSCRIPT = TranscriptTarget("abcd1234-0000-0000-0000-000000000000")


def test_round_trip_with_transcript():
    summary = "We analyzed three tickers and charted the spread."
    msg = build_summary_message(summary, TRANSCRIPT, resumes_at=(4, False))
    assert parse_summary_message(msg) == summary
    assert msg.content.endswith(transcript_note(TRANSCRIPT, (4, False)))


def test_round_trip_without_transcript():
    summary = "Quick factual answer, no sandbox to hold a transcript."
    msg = build_summary_message(summary, None)
    assert parse_summary_message(msg) == summary
    assert msg.content == f"{CONTEXT_SUMMARY_PREFIX}{summary}"


def test_summary_containing_the_note_survives():
    summary = f"Earlier the run said: {transcript_note(TRANSCRIPT)} and moved on."
    msg = build_summary_message(summary, TRANSCRIPT)
    assert parse_summary_message(msg) == summary


def test_legacy_message_without_length_stamp_falls_back():
    # Pre-stamp checkpoints have no summarize_complete metadata → rsplit path.
    summary = "Legacy summary text."
    content = f"{CONTEXT_SUMMARY_PREFIX}{summary}{_LEGACY_FILE_NOTE}work/history.md`."
    legacy = HumanMessage(content=content)  # no additional_kwargs stamp
    assert parse_summary_message(legacy) == summary


def test_task_note_names_the_task_runs():
    task = TranscriptTarget.for_agent(
        "abcd1234-0000-0000-0000-000000000000", "task:t/1|model:x"
    )
    assert task.directory == ".agents/transcripts/abcd1234/tasks/t_1"
    note = transcript_note(task, (3, False))
    assert f"`{task.directory}/`" in note and "`run-NNNN.jsonl` per run" in note
    assert note.endswith("Runs before 3 did not fit in this summary and are only there.")
    assert transcript_note(task, (1, True)).endswith(
        "This summary starts partway through run 1; everything before that is only there."
    )


def _turns(n):
    out = []
    for i in range(1, n + 1):
        out += [HumanMessage(content=f"q{i}", id=f"h{i}"), AIMessage(content="a", id=f"a{i}")]
    return out


def test_resumes_at_names_first_turn_trimming_kept():
    raw = _turns(5)
    assert summary_resumes_at(raw, raw[:8], raw[:8]) is None
    assert summary_resumes_at(raw, raw[:8], raw[4:8]) == (3, False)
    # A prior summary heads the summarized list; it is not in raw, so the
    # first raw message that survived trimming names the turn.
    prior = HumanMessage(content="old summary", id="s0")
    assert summary_resumes_at(raw, [prior, *raw[:8]], raw[2:8]) == (2, False)


def test_resumes_at_flags_a_message_cut_partway():
    # One huge message dominates: trimming keeps its tail under the same id.
    raw = _turns(3)
    tail = raw[2].model_copy(update={"content": "q2 (tail)"})
    assert summary_resumes_at(raw, raw[:4], [tail, raw[3]]) == (2, True)
    head_cut = raw[0].model_copy(update={"content": "1 (tail)"})
    assert summary_resumes_at(raw, raw[:4], [head_cut, *raw[1:4]]) == (1, True)
