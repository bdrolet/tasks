import json

import clients.claude as claude
from services import task_bullets as tb

NOTES = (
    "<body>Renew before the trip.\n"
    "<strong>Key points:</strong><ul><li>Expires 2026-10-01</li><li>Agency needs DS-82</li><li>Photos too</li></ul>"
    '<strong>Links:</strong><ul><li><a href="https://drive/ds82">DS-82 (filled)</a></li>'
    '<li><a href="https://drive/ds82">dup</a></li><li><a href="https://travel.state.gov/x">State Dept</a></li></ul>'
    '<strong>Actions</strong><ul><li><a href="https://hook/label?id=1">Confirmed review</a></li></ul>'
    '<strong>Source:</strong> Email<ul><li><a href="https://outlook/msg">Open in Outlook</a></li></ul></body>'
)


def test_parse_links_only_links_block_deduped():
    assert tb.parse_links(NOTES) == [
        ("https://drive/ds82", "DS-82 (filled)"),
        ("https://travel.state.gov/x", "State Dept"),
    ]


def test_parse_links_caps_at_five():
    html = (
        "<strong>Links:</strong><ul>"
        + "".join(f'<li><a href="https://d/{i}">{i}</a></li>' for i in range(8))
        + "</ul>"
    )
    assert len(tb.parse_links(html)) == 5


def test_parse_links_empty_or_bad_html():
    assert tb.parse_links("") == []
    assert tb.parse_links("<a href='https://x'>outside links</a>") == []


def test_description_text_drops_actions_and_source():
    text = tb.description_text(NOTES)
    assert text.startswith(
        "Renew before the trip. Key points: Expires 2026-10-01 Agency needs DS-82"
    )
    assert "Confirmed review" not in text
    assert "Outlook" not in text
    assert "DS-82 (filled)" in text


def test_fallback_points_prefers_key_points():
    assert tb.fallback_points(NOTES) == ["Expires 2026-10-01", "Agency needs DS-82"]


def test_fallback_points_uses_lead_context_when_no_key_points():
    html = "<body>" + "word " * 60 + "<strong>Source:</strong> Created manually</body>"
    points = tb.fallback_points(html)
    assert len(points) == 1 and points[0].endswith("…") and len(points[0]) <= 141


def test_fallback_points_empty_when_nothing():
    assert tb.fallback_points("<body><strong>Source:</strong> Created manually</body>") == []


def test_content_hash_changes_with_name_or_notes():
    a = tb.content_hash("n", "x")
    assert a == tb.content_hash("n", "x")
    assert a != tb.content_hash("n2", "x") and a != tb.content_hash("n", "y")


class MemCache:
    def __init__(self):
        self.store = {}

    def get(self, gid, content_hash):
        entry = self.store.get(gid)
        return entry[1] if entry and entry[0] == content_hash else None

    def put(self, gid, content_hash, points):
        self.store[gid] = (content_hash, points)


def test_points_for_cache_hit_makes_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "summarize", lambda p: calls.append(p) or "{}")
    cache = MemCache()
    cache.put("g1", tb.content_hash("n", NOTES), ["cached point"])
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=tb.Budget())
    assert (points, result) == (["cached point"], "cached")
    assert calls == []


def test_points_for_miss_calls_once_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        claude,
        "summarize",
        lambda p: calls.append(p) or json.dumps({"points": ["a", "b", "c", "d"]}),
    )
    cache = MemCache()
    budget = tb.Budget()
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=budget)
    assert (points, result) == (["a", "b", "c"], "ok")
    assert cache.get("g1", tb.content_hash("n", NOTES)) == ["a", "b", "c"]
    assert budget.remaining == tb.DIGEST_BULLET_CALLS_MAX - 1
    assert "Renew before the trip." in calls[0] and "Confirmed review" not in calls[0]


def test_points_for_failure_falls_back_and_does_not_cache(monkeypatch):
    def boom(p):
        raise RuntimeError("api down")

    monkeypatch.setattr(claude, "summarize", boom)
    cache = MemCache()
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=tb.Budget())
    assert result == "fallback"
    assert points == ["Expires 2026-10-01", "Agency needs DS-82"]
    assert cache.store == {}


def test_points_for_unparseable_json_falls_back(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda p: "not json")
    points, result = tb.points_for("g1", "n", NOTES, cache=MemCache(), budget=tb.Budget())
    assert result == "fallback" and points


def test_points_for_capped_uses_fallback_without_calling(monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "summarize", lambda p: calls.append(p) or "{}")
    points, result = tb.points_for("g1", "n", NOTES, cache=MemCache(), budget=tb.Budget(limit=0))
    assert result == "capped" and calls == []
    assert points == ["Expires 2026-10-01", "Agency needs DS-82"]
