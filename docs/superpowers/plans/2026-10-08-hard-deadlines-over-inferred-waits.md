# Hard Deadlines Over Inferred Waits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A task with a hard `due_on` reaches `/next` early enough to do the work, and a wait the model guessed can never hide it.

**Architecture:** All scoring changes land in the pure scorer `services/prioritize.py` (no I/O), fed by one new enrichment field (`waiting_confidence`) and one renamed config knob (`hard_due_slack_days`). A wait now carries a *source* (tag / override / model / none); only tag and override waits inherit to subtasks; a model wait on a hard-dated task is released back into `next` when raw slack ≤ N; must-dos fire on EDF `effective_slack ≤ N` instead of calendar days. The API, CLI and agent docs surface the new flag.

**Tech Stack:** Python 3.13, pydantic, stdlib `tomllib`, pytest. Run tests with `.venv/bin/pytest tests/ -q` from the repo root. No new dependencies, no DB migration (`components` and `task_enrichment.raw` are JSONB).

**Spec:** `docs/superpowers/specs/2026-10-08-hard-deadlines-over-inferred-waits-design.md`

## Global Constraints

- `services/prioritize.py` stays pure: no I/O, no clock, every constant from `Config` (original spec D4).
- Layer rules (CLAUDE.md): `services/` has no HTTP; `handlers/` and `api/routers/` orchestrate; `models/` imports nothing from other layers.
- `hard_due_slack_days = 5` replaces `hard_due_window_days`; it is a rename, not an alias — a config still naming the old key fails to load.
- Enrichment hash is versioned: `sha256("v2\n" + name + "\n" + notes + "\n" + comments)`.
- A stored enrichment row without `waiting_confidence` reads as `medium`.
- `PUT /tasks/{gid}/overrides {"waiting_on": ""}` means *not waiting*; `null` still clears the override.
- CLI: `task-next override <ref> waiting_on=-` sends `""`; `waiting_on=` (empty) still sends `null`.
- Commit messages end with the attribution lines the session supplies (Co-Authored-By + Claude-Session).

## Review Focus

1. A `waiting:` tag whose value is empty (`waiting:`) must be ignored, not treated as a wait — `_tag_values` already drops empty values; Task 3 pins it.
2. A pinned task with a model wait and a near hard date must not be double-handled: `_bucket` already returns `next` with `pinned_despite="waiting"`, so the release step must only act on bucket `nudge` — Task 5 pins it.
3. A released task whose hard date is *inherited* (`due_from` set) must release exactly like one with its own `due_on` — Task 5 pins it.
4. `effective_slack` is `None` for tasks without a date; `_is_must` must treat `None` as "not a must-do" rather than raising — Task 6 pins it.
5. The CLI's `due in Nd` flag must not crash on a row with no `effective_due` or a payload with no `today` — Task 8 pins it.

---

### Task 1: Rename the must-do knob to `hard_due_slack_days`

**Files:**
- Modify: `config/prioritize.toml:57-61`
- Modify: `services/prioritize_config.py:40,80`
- Modify: `services/prioritize.py:439-444` (reads the old name; interim rename only — Task 6 changes the rule)
- Test: `tests/test_prioritize_config.py:33`

**Interfaces:**
- Produces: `Config.hard_due_slack_days: int` (value 5 in the repo config). Tasks 5 and 6 read it.

- [ ] **Step 1: Change the config assertion to the new name and value**

In `tests/test_prioritize_config.py` replace the line

```python
    assert cfg.hard_due_window_days == 1
```

with

```python
    assert cfg.hard_due_slack_days == 5
```

and append this test at the end of the file:

```python
def test_old_window_key_no_longer_loads(tmp_path: Path):
    p = tmp_path / "p.toml"
    p.write_text(
        pc.DEFAULT_PATH.read_text().replace("hard_due_slack_days = 5", "hard_due_window_days = 1")
    )
    with pytest.raises(KeyError):
        pc.load(str(p))
```

- [ ] **Step 2: Run the config tests to see them fail**

Run: `.venv/bin/pytest tests/test_prioritize_config.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'hard_due_slack_days'` and the new test fails because the replace finds nothing to replace.

- [ ] **Step 3: Rename in the TOML**

In `config/prioritize.toml` replace

```toml
hard_due_window_days = 1      # hard due_on <= today + N is selected first, beyond n and capacity
```

with

```toml
hard_due_slack_days = 5       # hard due_on with effective_slack <= N is a must-do (beyond n and capacity);
                              # the same N releases a model-inferred wait on a hard-dated task back into next
```

- [ ] **Step 4: Rename in the loader**

In `services/prioritize_config.py` change the dataclass field

```python
    hard_due_window_days: int
```

to

```python
    hard_due_slack_days: int
```

and in `load()` change

```python
        hard_due_window_days=int(sel["hard_due_window_days"]),
```

to

```python
        hard_due_slack_days=int(sel["hard_due_slack_days"]),
```

- [ ] **Step 5: Rename the read in the scorer (rule unchanged for now)**

In `services/prioritize.py::_is_must` change

```python
        c.get("due_source") == "hard" and days is not None and days <= config.hard_due_window_days
```

to

```python
        c.get("due_source") == "hard" and days is not None and days <= config.hard_due_slack_days
```

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: config tests PASS. Three must-do tests in `tests/test_prioritize.py` now FAIL because the window grew from 1 to 5 calendar days: `test_hard_due_today_is_selected_first_beyond_n_and_consumes_capacity` (the four `[P0]` tasks due in 3 days are now must-dos), `test_every_hard_must_do_is_placed_even_past_n_and_capacity` (`o` due in 5 days is now a must-do). Fix them by moving the non-must-do fixtures out of the window:

In `test_hard_due_today_is_selected_first_beyond_n_and_consumes_capacity` change

```python
        facts(f"h{i}", name="[P0] big", points=1, due_on=TODAY + timedelta(days=3))
```

to

```python
        facts(f"h{i}", name="[P0] big", points=1, due_on=TODAY + timedelta(days=10))
```

In `test_every_hard_must_do_is_placed_even_past_n_and_capacity` change

```python
    other = facts("o", name="[P0] y", points=1, due_on=TODAY + timedelta(days=5))
```

to

```python
    other = facts("o", name="[P0] y", points=1, due_on=TODAY + timedelta(days=10))
```

In `test_must_do_ignores_soft_dates_inside_the_window` change

```python
    top = facts("t", name="[P0] y", points=1, due_on=TODAY + timedelta(days=5))
```

to

```python
    top = facts("t", name="[P0] y", points=1, due_on=TODAY + timedelta(days=10))
```

(so the test still proves the inferred date was ignored rather than passing because `t` became a must-do.)

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add config/prioritize.toml services/prioritize_config.py services/prioritize.py tests/test_prioritize_config.py tests/test_prioritize.py
git commit -m "config(prioritize): rename hard_due_window_days to hard_due_slack_days (5)"
```

---

### Task 2: Enrichment returns `waiting_confidence`; the hash is versioned

**Files:**
- Modify: `services/enrichment.py:22-50` (SCHEMA), `:52-68` (SYSTEM_PROMPT), `:71-80` (`_Out`), `:105-107` (`content_hash`), `:143-159` (`parse`)
- Modify: `models/prioritize.py:36-62` (`Enrichment`, `DEFAULT`)
- Modify: `handlers/prioritize.py:298-304` (`_enrichment_from_raw`)
- Test: `tests/test_enrichment.py`, `tests/test_prioritize_handler.py:55-65`, `tests/test_prioritize.py` (the `enr()` helper needs no change — it copies `DEFAULT.__dict__`)

**Interfaces:**
- Produces: `Enrichment.waiting_confidence: str` (`"low" | "medium" | "high"`; `DEFAULT` is `"low"`). `services.enrichment.HASH_VERSION = "v2"`. Task 3 reads `waiting_confidence`.

- [ ] **Step 1: Write the failing enrichment tests**

In `tests/test_enrichment.py` add `"waiting_confidence": "high",` to `GOOD` directly after the `"waiting_on": "the lawyer",` line. Then append:

```python
def test_parse_requires_waiting_confidence():
    without = {k: v for k, v in GOOD.items() if k != "waiting_confidence"}
    with pytest.raises(ValueError):
        en.parse(json.dumps(without))
    assert en.parse(json.dumps(GOOD)).waiting_confidence == "high"


def test_hash_is_versioned():
    import hashlib

    unversioned = hashlib.sha256("\n".join(["n", "notes"]).encode()).hexdigest()
    assert en.content_hash("n", "notes", []) != unversioned
    assert en.HASH_VERSION == "v2"


def test_prompt_defines_waiting_confidence():
    assert "waiting_confidence" in en.SYSTEM_PROMPT
    assert "is not waiting" in en.SYSTEM_PROMPT
```

- [ ] **Step 2: Write the failing handler test**

In `tests/test_prioritize_handler.py` add `"waiting_confidence": "medium",` to its `GOOD` dict directly after `"waiting_on": None,`. Then append at the end of the file:

```python
def test_stored_enrichment_without_waiting_confidence_reads_as_medium():
    from handlers.prioritize import _enrichment_from_raw

    old_row = {k: v for k, v in GOOD.items() if k != "waiting_confidence"}
    assert _enrichment_from_raw(old_row).waiting_confidence == "medium"
    assert _enrichment_from_raw(GOOD).waiting_confidence == "medium"
    assert _enrichment_from_raw({**GOOD, "waiting_confidence": "low"}).waiting_confidence == "low"
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_enrichment.py tests/test_prioritize_handler.py -q`
Expected: FAIL — `Enrichment.__init__() got an unexpected keyword argument 'waiting_confidence'` from `parse`, `AttributeError: module ... has no attribute 'HASH_VERSION'`, and the prompt assertion.

- [ ] **Step 4: Add the field to the model**

In `models/prioritize.py` change the `Enrichment` dataclass to

```python
@dataclass(frozen=True)
class Enrichment:
    story_points_suggested: int | None
    points_confidence: str
    waiting_on: str | None
    waiting_confidence: str  # low | medium | high — how sure the model is about waiting_on
    due_date_inferred: date | None
    due_date_inferred_confidence: str
    impact: str
    energy: str
    latest_comment_signal: str
    reason: str | None
    unenriched: bool

    DEFAULT: ClassVar["Enrichment"]
```

and in `Enrichment.DEFAULT = Enrichment(...)` add `waiting_confidence="low",` directly after `waiting_on=None,`.

- [ ] **Step 5: Schema, prompt, parse and hash in `services/enrichment.py`**

Add the constant after `COMMENTS_CAP = 3000`:

```python
# Bump when SCHEMA or SYSTEM_PROMPT changes: every stored hash goes stale and
# the next daily heal re-enriches every task (spec D6).
HASH_VERSION = "v2"
```

In `SCHEMA["properties"]` add, directly after the `"waiting_on"` entry:

```python
        "waiting_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
```

and in `SCHEMA["required"]` add `"waiting_confidence",` directly after `"waiting_on",`.

Replace the `waiting_on` paragraph of `SYSTEM_PROMPT` (the one beginning `waiting_on — the external party`) with:

```
waiting_on — the external party (a person, company or process) who must act before Ben can do anything more, or null. Waiting means Ben has done his part. A task Ben has not started is not waiting. A task whose newest comment asks Ben to send, sign, provide or decide something is not waiting — that is his action. waiting_confidence is high only when a comment says the wait outright ("sent to X, waiting on their reply"); medium when the wait is implied by the task's state; low when you are reading between the lines.
```

In `_Out` add `waiting_confidence: Literal["low", "medium", "high"]` directly after `waiting_on: str | None`.

In `parse()` add `waiting_confidence=data.waiting_confidence,` directly after `waiting_on=(data.waiting_on or None),`.

Change `content_hash` to:

```python
def content_hash(name: str, notes: str, comments: list[dict]) -> str:
    body = "\n".join([HASH_VERSION, name or "", notes or "", *_comment_lines(comments)])
    return hashlib.sha256(body.encode()).hexdigest()
```

- [ ] **Step 6: Default a missing confidence to medium when reading stored rows**

In `handlers/prioritize.py::_enrichment_from_raw` change the body to:

```python
def _enrichment_from_raw(raw: dict) -> Enrichment:
    d = dict(Enrichment.DEFAULT.__dict__)
    d.update({k: v for k, v in raw.items() if k in d})
    if "waiting_confidence" not in raw:
        # Rows written before the field existed: keep today's behaviour
        # (a stored wait counts) until the heal re-enriches them.
        d["waiting_confidence"] = "medium"
    if isinstance(d.get("due_date_inferred"), str):
        d["due_date_inferred"] = date.fromisoformat(d["due_date_inferred"])
    d["unenriched"] = False
    return Enrichment(**d)
```

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS. (`test_extract_uses_injected_call_and_model` still holds because the field was added to both `properties` and `required`.)

- [ ] **Step 8: Commit**

```bash
git add services/enrichment.py models/prioritize.py handlers/prioritize.py tests/test_enrichment.py tests/test_prioritize_handler.py
git commit -m "feat(prioritize): enrichment returns waiting_confidence; versioned content hash"
```

---

### Task 3: `Effective` carries `waiting_source`; low-confidence model waits and the `""` override

**Files:**
- Modify: `services/prioritize.py:27-36` (`Effective`), `:49-97` (`effective`), `:285-311` (`components`)
- Test: `tests/test_prioritize.py`

**Interfaces:**
- Consumes: `Enrichment.waiting_confidence` (Task 2).
- Produces: `Effective.waiting_source: str` — one of `"tag" | "override" | "model" | "none"`; `components["waiting_source"]` and `components["waiting_confidence"]`. Tasks 4 and 5 read `waiting_source`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prioritize.py`:

```python
def test_waiting_source_follows_precedence():
    a = facts("a", points=1)
    model = enr(waiting_on="the model", waiting_confidence="high")
    assert pz.effective(a, model, Overrides.NONE, CFG).waiting_source == "model"
    o = Overrides(fields={"waiting_on": "vendor"})
    e = pz.effective(a, model, o, CFG)
    assert (e.waiting_on, e.waiting_source) == ("vendor", "override")
    tagged = facts("t", points=1, tags=("waiting:the bank",))
    e = pz.effective(tagged, model, o, CFG)
    assert (e.waiting_on, e.waiting_source) == ("the bank", "tag")
    assert pz.effective(a, enr(), Overrides.NONE, CFG).waiting_source == "none"


def test_low_confidence_model_wait_is_not_a_wait():
    a = facts("a", points=1)
    for conf, bucket in (("low", "next"), ("medium", "nudge"), ("high", "nudge")):
        s = run([a], {"a": enr(waiting_on="someone", waiting_confidence=conf)}).by_gid()["a"]
        assert s.bucket == bucket, conf
        assert s.components["waiting_confidence"] == conf
    low = run([a], {"a": enr(waiting_on="someone", waiting_confidence="low")}).by_gid()["a"]
    assert low.components["waiting_on"] is None and low.components["waiting_source"] == "none"


def test_tag_and_override_waits_ignore_confidence():
    o = Overrides(fields={"waiting_on": "vendor"})
    a = facts("a", points=1)
    assert run([a], {"a": enr(waiting_confidence="low")}, {"a": o}).by_gid()["a"].bucket == "nudge"
    t = facts("t", points=1, tags=("waiting:bank",))
    assert run([t], {"t": enr(waiting_confidence="low")}).by_gid()["t"].bucket == "nudge"


def test_empty_override_means_not_waiting():
    a = facts("a", points=1)
    model = enr(waiting_on="Michael", waiting_confidence="high")
    s = run([a], {"a": model}, {"a": Overrides(fields={"waiting_on": ""})}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["waiting_on"] is None
    assert s.components["waiting_source"] == "override"
    # a tag still beats the empty override
    t = facts("t", points=1, tags=("waiting:bank",))
    s = run([t], {"t": model}, {"t": Overrides(fields={"waiting_on": ""})}).by_gid()["t"]
    assert s.bucket == "nudge" and s.components["waiting_on"] == "bank"


def test_empty_waiting_tag_is_ignored():
    t = facts("t", points=1, tags=("waiting:",))
    s = run([t]).by_gid()["t"]
    assert s.bucket == "next" and s.components["waiting_source"] == "none"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize.py -q -k "waiting_source or low_confidence_model or ignore_confidence or empty_override or empty_waiting_tag"`
Expected: FAIL — `AttributeError: 'Effective' object has no attribute 'waiting_source'` and KeyErrors on the new components.

- [ ] **Step 3: Add `waiting_source` to `Effective` and compute it**

In `services/prioritize.py` change the `Effective` dataclass to:

```python
@dataclass(frozen=True)
class Effective:
    points: int
    points_source: str  # field | estimate | default
    points_confidence: str
    waiting_on: str | None
    waiting_source: str  # tag | override | model | none
    due_date_inferred: date | None
    due_date_inferred_confidence: str
    impact: str
    energy: str
```

In `effective()`, replace the single line `waiting_on=pick("waiting", "waiting_on", enrichment.waiting_on),` with a block computed *before* the `return Effective(...)`, and pass both values. The function becomes:

```python
def effective(
    facts: TaskFacts, enrichment: Enrichment, overrides: Overrides, config: Config
) -> Effective:
    """tag > override > model > default, per field. A wait also records its
    source: a model wait at low confidence is no wait at all, and an override
    of "" is an explicit "not waiting" that only a tag outranks."""
    tags = _tag_values(facts.tags)
    o = overrides.fields

    def pick(tag_key: str, field: str, model_value, valid=None):
        for value in (tags.get(tag_key), o.get(field)):
            if value is not None and (valid is None or value in valid):
                return value
        return model_value

    if facts.story_points is not None:
        points, source, conf = facts.story_points, "field", "high"
    elif o.get("story_points") is not None:
        points, source, conf = int(o["story_points"]), "field", "high"
    elif facts.points_estimated is not None:
        points, source, conf = facts.points_estimated, "estimate", enrichment.points_confidence
    elif enrichment.story_points_suggested is not None:
        points, source, conf = (
            enrichment.story_points_suggested,
            "estimate",
            enrichment.points_confidence,
        )
    else:
        points, source, conf = config.default_points, "default", "low"

    inferred = o.get("due_date_inferred", enrichment.due_date_inferred)
    if isinstance(inferred, str):
        try:
            inferred = date.fromisoformat(inferred)
        except ValueError:
            inferred = None
    inferred_conf = (
        "high"
        if "due_date_inferred" in o and o["due_date_inferred"]
        else enrichment.due_date_inferred_confidence
    )

    if tags.get("waiting"):
        waiting_on, waiting_source = tags["waiting"], "tag"
    elif o.get("waiting_on") is not None:
        waiting_on, waiting_source = (o["waiting_on"] or None), "override"
    elif enrichment.waiting_on and enrichment.waiting_confidence != "low":
        waiting_on, waiting_source = enrichment.waiting_on, "model"
    else:
        waiting_on, waiting_source = None, "none"

    return Effective(
        points=points,
        points_source=source,
        points_confidence=conf,
        waiting_on=waiting_on,
        waiting_source=waiting_source,
        due_date_inferred=inferred,
        due_date_inferred_confidence=inferred_conf,
        impact=pick("impact", "impact", enrichment.impact, IMPACTS),
        energy=pick("energy", "energy", enrichment.energy, ENERGIES),
    )
```

- [ ] **Step 4: Record both in `components`**

In `score_set`, in the `components={...}` dict, directly after `"waiting_on": eff.waiting_on,` add:

```python
                "waiting_source": eff.waiting_source,
                "waiting_confidence": e.waiting_confidence,
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS. (`test_tag_beats_override_beats_model` still passes: the override `"vendor"` wins over a `None` model wait.)

- [ ] **Step 6: Commit**

```bash
git add services/prioritize.py tests/test_prioritize.py
git commit -m "feat(prioritize): waits carry a source; low-confidence model waits and the empty override"
```

---

### Task 4: Only tag/override waits inherit

**Files:**
- Modify: `services/prioritize.py:131-140` (`_State`), `:142-148` (`_Bucketed`), `:150-159` (`_own_state`), `:176-216` (`_bucket`), `:258-264` (the `score_set` loop where `eff` is replaced)
- Test: `tests/test_prioritize.py:553-562` (`test_child_of_waiting_parent_is_a_nudge_under_the_parents_person`)

**Interfaces:**
- Consumes: `Effective.waiting_source` (Task 3).
- Produces: `_State.waiting_source`, `_Bucketed.waiting_source` (the effective source after inheritance). Task 5 relies on `eff.waiting_source` in `score_set` being `"model"` only for a task's *own* model wait.

- [ ] **Step 1: Rewrite the inheritance test and add the model-wait case**

Replace `test_child_of_waiting_parent_is_a_nudge_under_the_parents_person` in `tests/test_prioritize.py` with:

```python
def test_child_of_tag_waiting_parent_is_a_nudge_under_the_parents_person():
    parent = facts("p", points=1, tags=("waiting:the consulate",))
    child = facts("c", points=1, parent_gid="p")
    scored = run([parent, child])
    s = scored.by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "the consulate"
    assert s.components["waiting_source"] == "tag"
    assert s.components["inherited"] == {"state": "waiting", "from": "p"}
    assert "c" in {t.gid for t in pz.side_lists(scored)["nudge"]}


def test_child_of_override_waiting_parent_is_a_nudge():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], overrides={"p": Overrides(fields={"waiting_on": "FTB"})}).by_gid()["c"]
    assert s.bucket == "nudge" and s.components["waiting_on"] == "FTB"
    assert s.components["waiting_source"] == "override"
    assert s.components["inherited"] == {"state": "waiting", "from": "p"}


def test_child_of_model_waiting_parent_is_not_waiting():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"p": enr(waiting_on="FTB", waiting_confidence="high")}).by_gid()
    assert s["p"].bucket == "nudge"  # the parent's own wait still holds on the parent
    assert s["c"].bucket == "next"
    assert s["c"].components["waiting_on"] is None
    assert s["c"].components["inherited"] is None
```

Also change `test_own_state_wins_over_inherited_and_is_not_marked_inherited` at `:564`: it uses model waits on both parent and child, and would still pass (the child's own model wait buckets it) without testing that inheritance was *beaten*. Replace its body with:

```python
def test_own_state_wins_over_inherited_and_is_not_marked_inherited():
    parent = facts("p", points=1, tags=("waiting:A",))
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"c": enr(waiting_on="B", waiting_confidence="high")}).by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "B"
    assert s.components["waiting_source"] == "model"
    assert s.components["inherited"] is None
```

The other inheritance tests (`test_grandchild_inherits_through_two_levels`, `test_pinned_child_of_blocked_parent_is_next_flagged`, `test_pinned_child_of_snoozed_parent_stays_snoozed`) use blocked and snoozed, not waits, and need no change.

- [ ] **Step 2: Run to verify the model case fails**

Run: `.venv/bin/pytest tests/test_prioritize.py -q -k "waiting_parent or own_state_wins or grandchild_inherits"`
Expected: `test_child_of_model_waiting_parent_is_not_waiting` FAILS (child is `nudge`); the others PASS or FAIL on `waiting_source` KeyError.

- [ ] **Step 3: Carry the source through `_State` and `_Bucketed`**

In `services/prioritize.py` change `_State` and `_Bucketed` to:

```python
@dataclass(frozen=True)
class _State:
    """A task's OWN inheritable state (D16)."""

    snoozed: bool
    blocked: bool
    waiting_on: str | None
    waiting_source: str  # tag | override | model | none
    completed: bool
    due_on: date | None


@dataclass(frozen=True)
class _Bucketed:
    bucket: str
    pinned_despite: str | None
    inherited: dict | None  # {"state": ..., "from": ancestor gid} when inheritance decided
    waiting_on: str | None  # effective, after inheritance
    waiting_source: str  # effective, after inheritance
```

In `_own_state` add `waiting_source=eff.waiting_source,` directly after `waiting_on=eff.waiting_on,`.

- [ ] **Step 4: Inherit only hand-set waits in `_bucket`**

Replace `_bucket` with:

```python
def _bucket(
    facts: TaskFacts,
    own: _State,
    ov: Overrides,
    ancestors: list[tuple[str, _State]],
    config: Config,
) -> _Bucketed:
    """Order: completed, snoozed (own, then inherited), excluded project,
    blocked (own, then inherited), parent, waiting (own, then inherited —
    but only a tag or override wait inherits; a model's guess about the
    parent says nothing about the work under it). A pin overrides only the
    last three, own or inherited (D15, D16)."""
    waiting_on, waiting_source = own.waiting_on, own.waiting_source

    def first(attr: str) -> str | None:
        return next((gid for gid, st in ancestors if getattr(st, attr)), None)

    def first_hand_set_wait() -> str | None:
        return next(
            (
                gid
                for gid, st in ancestors
                if st.waiting_on and st.waiting_source in ("tag", "override")
            ),
            None,
        )

    if facts.completed:
        return _Bucketed("excluded:completed", None, None, waiting_on, waiting_source)
    if own.snoozed:
        return _Bucketed("snoozed", None, None, waiting_on, waiting_source)
    if src := first("snoozed"):
        return _Bucketed(
            "snoozed", None, {"state": "snoozed", "from": src}, waiting_on, waiting_source
        )
    if facts.project_name in config.excluded_projects:
        return _Bucketed("excluded:project", None, None, waiting_on, waiting_source)
    reason, inherited = None, None
    if own.blocked:
        reason = "blocked"
    elif src := first("blocked"):
        reason, inherited = "blocked", {"state": "blocked", "from": src}
    elif facts.num_open_subtasks > 0:
        reason = "parent"
    elif own.waiting_on:
        reason = "waiting"
    elif src := first_hand_set_wait():
        reason, inherited = "waiting", {"state": "waiting", "from": src}
        waiting_on = dict(ancestors)[src].waiting_on
        waiting_source = dict(ancestors)[src].waiting_source
    if reason is None:
        return _Bucketed("next", None, None, waiting_on, waiting_source)
    if ov.pinned_rank is not None:
        return _Bucketed("next", reason, inherited, waiting_on, waiting_source)
    bucket = "nudge" if reason == "waiting" else f"excluded:{reason}"
    return _Bucketed(bucket, None, inherited, waiting_on, waiting_source)
```

- [ ] **Step 5: Propagate the inherited source into `eff`**

In `score_set`, replace

```python
        if bk.waiting_on != eff.waiting_on:
            eff = replace(eff, waiting_on=bk.waiting_on)
```

with

```python
        if (bk.waiting_on, bk.waiting_source) != (eff.waiting_on, eff.waiting_source):
            eff = replace(eff, waiting_on=bk.waiting_on, waiting_source=bk.waiting_source)
```

- [ ] **Step 6: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add services/prioritize.py tests/test_prioritize.py
git commit -m "feat(prioritize): only tag/override waits inherit to subtasks"
```

---

### Task 5: A hard deadline releases a model wait

**Files:**
- Modify: `services/prioritize.py` — `score_set`, between the effort computation and the `ScoredTask(...)` construction (currently `:264-279`), plus the `components` dict
- Test: `tests/test_prioritize.py`

**Interfaces:**
- Consumes: `Config.hard_due_slack_days` (Task 1), `eff.waiting_source` (Tasks 3–4).
- Produces: `components["wait_released"]: {"waiting_on": str, "slack": float} | None`. Task 7 exposes it; Task 8 renders it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prioritize.py`:

```python
def _model_wait(who="Michael"):
    return enr(waiting_on=who, waiting_confidence="high")


def test_model_wait_is_released_near_a_hard_deadline():
    # 5 points = 1.0 effort day; due in 6 days → raw slack 5.0 == N → released
    a = facts("a", points=5, due_on=TODAY + timedelta(days=6))
    s = run([a], {"a": _model_wait()}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["waiting_on"] == "Michael"  # kept, for the waiting? flag
    assert s.components["wait_released"] == {"waiting_on": "Michael", "slack": 5.0}
    assert s.components["due_source"] == "hard"


def test_model_wait_holds_outside_the_slack_window():
    a = facts("a", points=5, due_on=TODAY + timedelta(days=7))  # raw slack 6.0 > N
    s = run([a], {"a": _model_wait()}).by_gid()["a"]
    assert s.bucket == "nudge" and s.components["wait_released"] is None


def test_override_wait_is_never_released():
    a = facts("a", points=5, due_on=TODAY)  # slack -1
    s = run([a], {"a": enr()}, {"a": Overrides(fields={"waiting_on": "FTB"})}).by_gid()["a"]
    assert s.bucket == "nudge" and s.components["wait_released"] is None
    t = facts("t", points=5, due_on=TODAY, tags=("waiting:FTB",))
    assert run([t]).by_gid()["t"].bucket == "nudge"


def test_soft_dates_never_release_a_wait():
    inferred = facts("i", points=1)
    e = enr(
        waiting_on="X",
        waiting_confidence="high",
        due_date_inferred=TODAY,
        due_date_inferred_confidence="high",
    )
    assert run([inferred], {"i": e}).by_gid()["i"].bucket == "nudge"
    horizon = facts("h", name="[P0] x", points=1, created_at=TS)  # P0 horizon long past
    assert run([horizon], {"h": _model_wait()}).by_gid()["h"].bucket == "nudge"


def test_inherited_hard_date_releases_a_childs_model_wait():
    parent = facts("p", points=1, due_on=TODAY + timedelta(days=2), num_open_subtasks=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"c": _model_wait()}).by_gid()["c"]
    assert s.bucket == "next"
    assert s.components["due_from"] == "p"
    assert s.components["wait_released"]["waiting_on"] == "Michael"


def test_pinned_waiting_task_is_not_double_handled_by_release():
    a = facts("a", points=5, due_on=TODAY)
    s = run([a], {"a": _model_wait()}, {"a": Overrides(pinned_rank=1)}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["pinned_despite"] == "waiting"
    assert s.components["wait_released"] is None


def test_released_task_joins_feasibility_and_can_be_a_must_do():
    a = facts("a", points=5, due_on=TODAY + timedelta(days=3))  # slack 2
    scored = run([a], {"a": _model_wait()})
    s = scored.by_gid()["a"]
    assert s.components["effective_slack"] == 2.0 and s.components["simulated_start"] == 0.0
    assert [t.gid for t in pz.select(scored.next(), CFG, n=1)] == ["a"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize.py -q -k "release"`
Expected: FAIL — buckets are `nudge` and `wait_released` is a KeyError.

- [ ] **Step 3: Add the release step to `score_set`**

In `score_set`, directly after

```python
        effort = eff.points / config.points_per_day
        if eff.points_source != "field" and eff.points_confidence == "low":
            effort *= config.low_confidence_multiplier
```

insert:

```python
        # A model's guess may not hide a hard deadline: inside the slack
        # window the task comes back to `next`, wait kept and flagged. A tag
        # or override wait is an instruction and holds. Raw slack here — the
        # task was outside the EDF queue when it was bucketed.
        released = None
        if bucket == "nudge" and eff.waiting_source == "model" and source == "hard" and due:
            raw_slack = (due - today).days - effort
            if raw_slack <= config.hard_due_slack_days:
                bucket = "next"
                released = {"waiting_on": eff.waiting_on, "slack": raw_slack}
```

and in the `components={...}` dict add, directly after `"inherited": bk.inherited,`:

```python
                "wait_released": released,
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS. (`test_released_task_joins_feasibility_and_can_be_a_must_do` passes under Task 1's interim calendar rule too — due in 3 days ≤ 5 — and keeps passing under Task 6's slack rule.)

- [ ] **Step 5: Commit**

```bash
git add services/prioritize.py tests/test_prioritize.py
git commit -m "feat(prioritize): a hard deadline releases a model-inferred wait"
```

---

### Task 6: Must-dos fire on `effective_slack`

**Files:**
- Modify: `services/prioritize.py:398-444` (`select` docstring and `_is_must`)
- Test: `tests/test_prioritize.py`

**Interfaces:**
- Consumes: `Config.hard_due_slack_days`; `components["effective_slack"]` (already stored, so `api/routers/next.py` re-selects unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prioritize.py`:

```python
def test_must_do_is_decided_by_effective_slack():
    # 5 points = 1 effort day. EDF queue: near is first (due in 6 → slack 5 →
    # must-do); far is second (due in 8, minus its own day and near's → slack 6 → not).
    near = facts("n", name="[P3] file it", points=5, due_on=TODAY + timedelta(days=6))
    far = facts("f", name="[P3] file it", points=5, due_on=TODAY + timedelta(days=8))
    quick = facts("q", name="[P0] hot", points=1, due_on=TODAY + timedelta(days=20))
    scored = run([near, far, quick])
    picked = [t.gid for t in pz.select(scored.next(), CFG, n=1)]
    assert picked[0] == "n" and "f" not in picked


def test_edf_queue_makes_the_second_of_two_same_day_tasks_a_must_do_first():
    # both due in 7 days with 1 effort day: alone each has slack 6 (not a
    # must-do); queued, the second has slack 5 and is one.
    a = facts("a", name="[P1] x", points=5, due_on=TODAY + timedelta(days=7))
    b = facts("b", name="[P1] y", points=5, due_on=TODAY + timedelta(days=7))
    scored = run([a, b])
    slacks = sorted(t.components["effective_slack"] for t in scored.next())
    assert slacks == [5.0, 6.0]
    musts = [t.gid for t in pz.select(scored.next(), CFG, n=1)]
    assert len(musts) == 1 and scored.by_gid()[musts[0]].components["effective_slack"] == 5.0


def test_overdue_hard_task_is_a_must_do():
    late = facts("l", name="[P3] late", points=1, due_on=TODAY - timedelta(days=3))
    quick = facts("q", name="[P0] hot", points=1, due_on=TODAY + timedelta(days=20))
    assert pz.select(run([late, quick]).next(), CFG, n=1)[0].gid == "l"


def test_undated_task_is_never_a_must_do():
    from models.prioritize import ScoredTask

    t = ScoredTask(
        gid="u", bucket="next", score=1.0, position=1, rank=None,
        components={"due_source": "none", "effective_slack": None},
    )
    assert pz._is_must(t, CFG) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize.py -q -k "must_do or edf_queue or overdue_hard or undated_task_is_never"`
Expected: `test_must_do_is_decided_by_effective_slack` and `test_edf_queue_...` FAIL (calendar rule: 6 and 7 days > 5); the overdue and undated tests PASS already.

- [ ] **Step 3: Change the rule**

In `services/prioritize.py` replace `_is_must` with:

```python
def _is_must(t: ScoredTask, config: Config) -> bool:
    """Hard-dated and, after the EDF pass, within hard_due_slack_days of
    being too late — so a day of work gets a week's notice and an hour's
    gets a few days'. Overdue is negative slack."""
    c = t.components
    slack = c.get("effective_slack")
    return (
        c.get("due_source") == "hard"
        and slack is not None
        and slack <= config.hard_due_slack_days
    )
```

and in the `select` docstring change

```
    Must-dos are hard-dated tasks due within hard_due_window_days (overdue
    included), by score: placed whatever n, capacity, energy or diversity
```

to

```
    Must-dos are hard-dated tasks whose effective_slack is within
    hard_due_slack_days (overdue included), by score: placed whatever n,
    capacity, energy or diversity
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS. `tests/test_api_next.py` still passes because `select` reads `effective_slack` from the stored `components` the fixtures already carry (if a fixture row lacks `effective_slack`, `_is_must` returns False — add `"effective_slack": 10.0,` to the `row()` components in `tests/test_api_next.py:35-47` only if a must-do assertion there starts failing).

- [ ] **Step 5: Commit**

```bash
git add services/prioritize.py tests/test_prioritize.py
git commit -m "feat(prioritize): must-dos decided by effective slack, not calendar days"
```

---

### Task 7: API — `wait_released` on rows; `""` accepted for `waiting_on`

**Files:**
- Modify: `api/routers/next.py:30-53` (`RankedTask`), `:97-121` (`_to_ranked`)
- Test: `tests/test_api_next.py`

**Interfaces:**
- Produces: `RankedTask.wait_released: bool` on every `/ranking` and `/next` row. Task 8 reads it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_next.py` (reuse the file's `client`, `AUTH`, `row`, `repo` and `monkeypatch` conventions as in `test_overrides_merge_and_reject_unknown`):

```python
def test_empty_waiting_on_override_is_stored_not_cleared(monkeypatch):
    saved = []

    def merge(conn, gid, patch):
        saved.append(patch)
        return type("O", (), {"fields": patch, "pinned_rank": None, "snooze_until": None})()

    monkeypatch.setattr(repo, "merge_overrides", merge)
    assert client.put("/tasks/t1/overrides", headers=AUTH, json={"waiting_on": ""}).status_code == 200
    assert saved == [{"waiting_on": ""}]
    client.put("/tasks/t1/overrides", headers=AUTH, json={"waiting_on": None})
    assert saved[-1] == {"waiting_on": None}


def test_rows_carry_wait_released(monkeypatch):
    released = row(
        "r",
        6,
        score=0.4,
        components={
            **row("r", 6)["components"],
            "waiting_on": "Michael",
            "wait_released": {"waiting_on": "Michael", "slack": 5.0},
        },
    )
    monkeypatch.setattr(repo, "list_scores", lambda conn: [*ROWS, released])
    tasks = client.get("/ranking?limit=100", headers=AUTH).json()["tasks"]
    by = {t["task_gid"]: t for t in tasks}
    assert by["r"]["wait_released"] is True and by["r"]["waiting_on"] == "Michael"
    assert by["a"]["wait_released"] is False
```

(The file's autouse `db` fixture already monkeypatches `repo.list_scores`; the second test re-patches it for its own rows.)

- [ ] **Step 2: Run to verify the second test fails**

Run: `.venv/bin/pytest tests/test_api_next.py -q -k "wait_released or empty_waiting"`
Expected: `test_rows_carry_wait_released` FAILS with `KeyError: 'wait_released'`; the override test may already PASS (pydantic accepts `""` and `merge_overrides` stores non-None values) — keep it as the pin.

- [ ] **Step 3: Expose the flag**

In `api/routers/next.py::RankedTask` add, directly after `waiting_on: str | None = None`:

```python
    wait_released: bool = False  # a model wait set aside because a hard deadline is near
```

In `_to_ranked` add, directly after `waiting_on=c.get("waiting_on"),`:

```python
        wait_released=bool(c.get("wait_released")),
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add api/routers/next.py tests/test_api_next.py
git commit -m "feat(api): wait_released on ranking rows; empty waiting_on override"
```

---

### Task 8: CLI — `waiting_on=-`, the `waiting?` flag and the due flag

**Files:**
- Modify: `scripts/task_next.py:1-20` (docstring), `:79-108` (`_line`), `:115-135` (`render_lists`), `:160-168` (`render_ranking`), `:280-291` (`override` parsing)
- Test: `tests/test_task_next.py`

**Interfaces:**
- Consumes: `wait_released`, `waiting_on`, `effective_due`, `soft` on rows; `today` on the payload.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task_next.py`:

```python
def test_override_dash_means_not_waiting(api):
    gid = "1218118170820306"
    tn.main(["override", gid, "waiting_on=-"])
    tn.main(["override", gid, "waiting_on="])
    writes = [c for c in api if c[0] == "PUT"]
    assert writes[-2][2] == {"waiting_on": ""}
    assert writes[-1][2] == {"waiting_on": None}


def test_released_row_shows_waiting_flag_and_due_flag():
    payload = {
        "today": "2026-09-23",
        "next": [
            T("r", wait_released=True, waiting_on="Michael", effective_due="2026-09-30"),
            T("t", effective_due="2026-09-23"),
            T("m", effective_due="2026-09-24"),
            T("o", effective_due="2026-09-20"),
            T("s", effective_due="2026-09-30", soft=True),
        ],
        "overcommitted": [],
        "stale": [],
        "nudge": [],
    }
    lines = tn.render_lists(payload).splitlines()
    flags = {line.split("\t")[1]: line.split("\t")[6] for line in lines if "\t" in line and not line.startswith("#")}
    assert flags["r"] == "waiting?Michael due in 7d"
    assert flags["t"] == "due today"
    assert flags["m"] == "due tomorrow"
    assert flags["o"] == "overdue"
    assert flags["s"] == ""


def test_due_flag_tolerates_missing_dates():
    assert tn._due_flag({"effective_due": None, "soft": False}, "2026-09-23") == ""
    assert tn._due_flag({"effective_due": "2026-09-30", "soft": False}, None) == ""
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_task_next.py -q -k "dash_means or released_row or due_flag"`
Expected: FAIL — `waiting_on=-` sends `"-"`; the flags column lacks the new words; `_due_flag` does not exist.

- [ ] **Step 3: Add the due flag helper and the new flags to `_line`**

In `scripts/task_next.py` add after `_refs`:

```python
def _due_flag(t: dict, today: str | None) -> str:
    """'due today' / 'due tomorrow' / 'overdue' / 'due in Nd' for a hard-dated
    row; '' for soft or undated rows. Hard dates inside the must-do window
    are what select() forces to the top, so every hard date in Next shows
    its distance."""
    due = t.get("effective_due")
    if not today or not due or t.get("soft"):
        return ""
    days = (date.fromisoformat(due) - date.fromisoformat(today)).days
    if days < 0:
        return "overdue"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due in {days}d"
```

Change `_line`'s signature and flags to:

```python
def _line(t: dict, refs: dict[str, str], explain: bool, today: str | None = None) -> str:
    due = t.get("effective_due") or "—"
    soft = "~" if t.get("soft") else ""
    marks = "".join(
        s
        for s, on in (
            ("!", t.get("overcommitted")),
            ("z", t.get("stale")),
            ("📌", (t.get("override") or {}).get("pinned_rank")),
        )
        if on
    )
    words = [
        f"waiting?{t.get('waiting_on') or '?'}" if t.get("wait_released") else "",
        _due_flag(t, today),
    ]
    flags = " ".join(p for p in (marks, *words) if p)
    row = [
        refs[t["task_gid"]],
        t["task_gid"],
        f"{soft}{due}",
        f"{t.get('points') or '?'}p",
        t["name"],
        t.get("project") or "—",
        flags,
    ]
```

(the rest of `_line` — the `explain` block and the `return` — is unchanged).

In `render_lists`, pass today only for the Next list: change

```python
            out += [_line(t, refs, explain) for t in rows] or ["—"]
```

to

```python
            today = payload.get("today") if key == "next" else None
            out += [_line(t, refs, explain, today) for t in rows] or ["—"]
```

- [ ] **Step 4: Parse `waiting_on=-`**

In the `override` branch of `main`, replace

```python
            patch[key] = (
                (int(value) if key in ("story_points", "pinned_rank") else value) if value else None
            )
```

with

```python
            if key == "waiting_on" and value == "-":
                patch[key] = ""  # explicit "not waiting" (spec D2); `waiting_on=` still clears
            else:
                patch[key] = (
                    (int(value) if key in ("story_points", "pinned_rank") else value)
                    if value
                    else None
                )
```

and in the module docstring change

```
    task-next override <ref|gid> field=value ... (field= clears)
```

to

```
    task-next override <ref|gid> field=value ... (field= clears; waiting_on=- means not waiting)
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/task_next.py tests/test_task_next.py
git commit -m "feat(task-next): waiting_on=- for not waiting; waiting? and due flags in Next"
```

---

### Task 9: Agent, skill and audit docs

**Files:**
- Modify: `.claude/agents/task-next.md:44-55,66-69,91-92`
- Modify: `.claude/skills/prioritizing-tasks/SKILL.md:44-69`
- Test: none (prose); verify by reading each diff against spec D2–D5. (`docs/prioritize-audit.md` does not enumerate `components` keys, so it needs no change.)

- [ ] **Step 1: Agent — how the selection is built**

In `.claude/agents/task-next.md` replace the **Must-dos first** bullet with:

```
- **Must-dos first.** A hard due date (`due_on`, `due_source: hard`) whose
  slack — days until due, minus the work it needs, minus hard-dated work
  queued ahead of it — is 5 days or less is placed at the top of **Next**
  whatever its score, even past `n` and the 5-point capacity, and uses up
  capacity. A day of work surfaces about a week out; an hour's a few days out.
```

Replace the **Subtasks inherit** bullet with:

```
- **Subtasks inherit.** Snoozing or blocking a parent covers its subtasks
  (up to 3 levels), and so does a wait Ben set on the parent (a `waiting:`
  tag or an override). A wait the *model* inferred on a parent stays on the
  parent. `components.inherited` names the ancestor. A pin on a subtask
  overrides an inherited block or wait, never an inherited snooze.
```

Add a new bullet after it:

```
- **A guessed wait never hides a deadline.** `waiting_on` the model inferred
  (`components.waiting_source: model`) only counts at medium or high
  `waiting_confidence`, and on a hard-dated task it is set aside once slack
  is within 5 days: the task is back in **Next** with `wait_released: true`.
  Render it as `waiting? <who>` and, when asked, say the model thought it was
  waiting and the deadline overruled that. Ben resolves it with "X isn't
  waiting on anyone" or "X really is waiting on Y".
```

- [ ] **Step 2: Agent — writes and rendering**

In the **Writes** section change the `waiting_on` bullet to:

```
- "X is waiting on the lawyer / X is high impact / X is deep work" →
  `PUT /tasks/{gid}/overrides {"waiting_on": "..."}` etc.
- "X isn't waiting on anyone / stop treating X as waiting" →
  `PUT /tasks/{gid}/overrides {"waiting_on": ""}` — the empty string is an
  explicit "not waiting" that only a `waiting:` tag outranks; `null` would
  just clear the override and let the model's guess back.
```

In the rendering section change

```
In **Next**, mark a must-do with `due today` / `due tomorrow` / `overdue` in
its flags.
```

to

```
In **Next**, mark every hard-dated row with `due today` / `due tomorrow` /
`overdue` / `due in Nd` in its flags, and a released wait with
`waiting? <who>` (from `wait_released` + `waiting_on`).
```

- [ ] **Step 3: Skill**

In `.claude/skills/prioritizing-tasks/SKILL.md` replace the **Must-dos first** sub-bullet with:

```
  - Must-dos first: a hard `due_on` whose effective slack (days until due
    minus the work it needs minus hard work queued ahead) is ≤ 5 leads the
    list whatever its score, beyond `n` and capacity, and consumes capacity.
```

Replace the **Subtasks inherit** sub-bullet with:

```
  - Subtasks inherit: snoozing or blocking a parent, or a wait Ben set on
    it (`waiting:` tag or override), covers its subtasks
    (`components.inherited` names the ancestor); a model-inferred wait on a
    parent does not. A pin overrides an inherited block or wait, never an
    inherited snooze.
  - A model-inferred wait counts only at medium/high `waiting_confidence`,
    and is set aside on a hard-dated task once slack ≤ 5 days
    (`wait_released: true`, shown as `waiting? <who>`).
```

In the `GET /ranking` bullet's `components` list add `waiting_source (tag | override | model | none), waiting_confidence, wait_released` after `starvation_boost`.

In the **Overrides** bullet change `(PUT /tasks/{gid}/overrides, null clears)` to `(PUT /tasks/{gid}/overrides, null clears; waiting_on: "" means not waiting — CLI waiting_on=-)`.

- [ ] **Step 4: Full suite and commit**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

```bash
git add .claude/agents/task-next.md .claude/skills/prioritizing-tasks/SKILL.md
git commit -m "docs(prioritize): waiting? flag, not-waiting override, slack-based must-dos"
```

---

## Rollout (after merge, from the spec)

1. Merging to `main` deploys `tasks-prioritize` and `tasks-api` (auto-deploy watches `main`).
2. The next `day_changed` tick (05:45 ET) finds every stored hash stale (the `v2` prefix) and re-enriches ~200 tasks with the new prompt — a one-off Opus burst, a few dollars. Before that, stored waits read as `medium` and Tasks 4–6 already apply on the first rescore after deploy; `scripts/backfill_prioritize.py` forces one.
3. Check `asana_prioritize_enrich_total{result}` for the burst and `task-next ranking` / `GET /ranking?list=nudge` the following morning: the nudge list should be shorter, and no hard-dated task with slack ≤ 5 should be in it.
