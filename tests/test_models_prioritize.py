# tests/test_models_prioritize.py
from datetime import date

from models.prioritize import Enrichment, Overrides, ScoredSet, ScoredTask, Stats


def test_defaults_are_the_spec_defaults():
    e = Enrichment.DEFAULT
    assert e.story_points_suggested is None and e.points_confidence == "low"
    assert e.waiting_on is None and e.due_date_inferred is None
    assert (e.impact, e.energy, e.latest_comment_signal) == ("medium", "shallow", "none")
    assert e.unenriched is True
    assert Overrides.NONE.fields == {} and Overrides.NONE.pinned_rank is None
    assert Stats.NONE.times_deferred == 0


def test_scored_set_helpers():
    a = ScoredTask(gid="a", bucket="next", score=1.0, position=1, rank=1, components={})
    b = ScoredTask(gid="b", bucket="nudge", score=None, position=2, rank=None, components={})
    s = ScoredSet(today=date(2026, 9, 23), tasks=[a, b])
    assert [t.gid for t in s.next()] == ["a"]
    assert s.by_gid()["b"] is b
