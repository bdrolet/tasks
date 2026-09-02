#!/usr/bin/env python
"""Dry run of gate 1 over historical mail. Creates nothing, comments nothing.

Reads the INBOX database (not this service's) for the corpus and replays every
email through services/screening.screen, then — for `relate` verdicts —
services/relating.match itself (see _relate() below, which fetches candidates
once via relating._candidates() and hands them to relating.match(event,
rows=...) so the verdict comes from production's own function, not a copy of
its control flow), writing a TSV for analysis. This only reads — the comment
is posted by handlers/task_create.py::_suppress, which is never called here.

Local-only: the deployed tasks service has no inbox DB access.

Why a dry run and not a shadow period: the inbox DB holds zero recorded
misses. Every human correction runs the other way (Ben downgrades false
positives; mail filed `ignore` is archived and never seen again). But the
baseline on that pile is zero promotions, so the screener cannot be worse than
today on recall — only on precision, which is exactly what this measures
without an answer key.

Usage:
    .venv/bin/python scripts/backtest_screening.py --out backtest.tsv
    .venv/bin/python scripts/backtest_screening.py --corpus recall_probe --limit 40
    .venv/bin/python scripts/backtest_screening.py --no-relate   # screener only, cheaper
    .venv/bin/python scripts/backtest_screening.py --ids probe.ids --out probe-out.tsv
        # tight iteration loop: replays only these message_ids (see --help for the
        # dedup/unmatched-id rules) instead of the whole corpus — seconds and cents,
        # not tens of minutes and dollars.

Needs BOTH databases: this repo's .env (tasks DB for the embedding corpus,
ANTHROPIC_API_KEY, INBOX_API_*) plus ~/src/inbox/.env for the corpus itself.
Run scripts/fetch-env.sh first.
"""

import argparse
import collections
import csv
import pathlib
import sys
from typing import TYPE_CHECKING

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

if TYPE_CHECKING:  # pragma: no cover — keeps the runtime import lazy (see _relate)
    from models.events import Match

_LLM_CTE = """
WITH llm AS (
  SELECT DISTINCT ON (message_id) message_id, category, importance
  FROM classifications WHERE source='llm'
  ORDER BY message_id, created_at DESC),
hum AS (
  SELECT DISTINCT ON (message_id) message_id, category
  FROM classifications WHERE source='human_correction'
  ORDER BY message_id, created_at DESC)
"""

_SELECT = """
SELECT m.id, m.external_id, m.sender, m.sender_display, m.subject,
       m.body, m.received_at, llm.category, llm.importance
"""

# expected: the verdict a correct screener reaches. "non_task" accepts either
# drop or relate — both are non-task outcomes and neither floods the list.
CORPORA: dict[str, tuple[str, str | None]] = {
    "negative": (
        _LLM_CTE
        + _SELECT
        + """
        FROM llm JOIN messages m ON m.id = llm.message_id
        WHERE llm.category IN ('ignore','reference')
          AND llm.message_id NOT IN (SELECT message_id FROM hum)
        ORDER BY m.received_at DESC""",
        "non_task",
    ),
    # Ben explicitly corrected these DOWN to ignore/reference. Sharpest rows in
    # the corpus — he actively ruled "not a task" on each. A hard negative
    # landing on `relate` is acceptable: he ruled "not a task", not
    # "not relevant".
    "hard_negative": (
        _LLM_CTE
        + _SELECT
        + """
        FROM hum JOIN llm USING (message_id) JOIN messages m ON m.id = hum.message_id
        WHERE hum.category IN ('ignore','reference')
        ORDER BY m.received_at DESC""",
        "non_task",
    ),
    "regression_reclass": (
        _LLM_CTE
        + _SELECT
        + """
        FROM hum JOIN llm USING (message_id) JOIN messages m ON m.id = hum.message_id
        WHERE hum.category IN ('urgent','review','respond')
        ORDER BY m.received_at DESC""",
        "task",
    ),
    # The population that currently becomes a task, replayed against the new
    # gate. Under the old gate all of these became tasks automatically (no
    # gate-1 at all); under the new gate the screener sees each fresh and may
    # say `drop` or `relate` instead. `expected` is "task" so the summary's
    # miss count reads directly as "emails that would stop becoming tasks" —
    # but a miss here is a BEHAVIOUR CHANGE TO REVIEW, not necessarily an
    # error: inbox's category is not ground truth (that's the entire premise
    # of this branch), so a `positive` miss may be the screener correctly
    # catching something inbox over-classified, same as it may be a real
    # regression. Read the misses; don't just count them.
    "positive": (
        _LLM_CTE
        + _SELECT
        + """
        FROM llm JOIN messages m ON m.id = llm.message_id
        WHERE llm.category IN ('urgent','review','respond')
          AND llm.message_id NOT IN (SELECT message_id FROM hum)
        ORDER BY m.received_at DESC""",
        "task",
    ),
    # Dana's seam: near-empty bodies where the signal is elsewhere.
    "recall_probe": (
        _LLM_CTE
        + _SELECT
        + """
        FROM llm JOIN messages m ON m.id = llm.message_id
        WHERE llm.category IN ('ignore','reference')
          AND length(coalesce(m.body,'')) < 200
        ORDER BY m.received_at DESC""",
        None,
    ),
}


def load_ids(path: pathlib.Path) -> list[str]:
    """One message_id per line; blank lines and full-line `#` comments are
    ignored. Order is preserved, duplicates collapse (a dict, not a set, so
    the first occurrence's position is what survives — not that it matters
    for the IN-clause this feeds)."""
    ids: dict[str, None] = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids[line] = None
    return list(ids)


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def connect(env: dict[str, str]):
    from google.cloud.sql.connector import Connector

    return Connector().connect(
        env["CLOUD_SQL_CONNECTION_NAME"],
        "pg8000",
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        db=env.get("POSTGRES_DB", "inbox"),
    )


def to_event(row: tuple) -> dict:
    """inbox `messages` row → the EmailClassifiedEvent shape screen() reads.

    has_attachments is set True unconditionally: messages.raw stores the Graph
    *webhook notification*, not the message, so the DB cannot answer it.
    attachment_lines() then asks inbox-api, which answers authoritatively and
    returns [] when there are none."""
    mid, graph_id, sender, sender_display, subject, body, received_at, category, importance = row
    return {
        "event": "email_classified",
        "message_id": str(mid),
        "graph_message_id": graph_id,
        "has_attachments": True,
        "category": category,
        "importance": importance or "P3",
        "confidence": 0.0,
        "subject": subject or "",
        "sender": sender or "",
        "sender_display": sender_display or sender or "",
        "to": [],
        "cc": [],
        "received_at": str(received_at),
        "tags": [],
        "reasoning": "",
        "body": body or "",
        "body_html": None,
        "web_link": None,
    }


def _relate(event) -> tuple[float, str, "Match"]:
    """Top-3 neighbours, the best cosine score, and the match (a
    models.events.Match; the return annotation is a string forward reference,
    resolved only under TYPE_CHECKING at the top of this file, so the runtime
    import of models.events stays lazy) — all from ONE candidate lookup.

    relating.match(event) calls relating._candidates(event) itself, which
    does a Vertex embedding plus a pgvector query; clients/vertex.py has no
    caching, so calling both _neighbours-style diagnostics and match()
    separately paid for that lookup twice on every relate row. This fetches
    candidates once and passes them to relating.match(event, rows=rows),
    which then skips its own fetch and runs its real floor-then-confirm
    logic against the rows already fetched — the verdict comes from
    production's own function, not a hand-copy of it. services/relating.py's
    only other call site, handlers/task_create.py, always calls
    relating.match(event) with no rows and is unaffected."""
    from services import relating

    try:
        rows = relating._candidates(event)
    except Exception as exc:  # noqa: BLE001 — diagnostics only; matches match()'s own fail-open
        rows = []
        neighbours = f"error: {exc}"
    else:
        neighbours = (
            " | ".join(
                f"{r['task_gid']}:{r['score']:.2f}:{(r.get('title') or '')[:40]}" for r in rows
            )
            if rows
            else ""
        )

    best_score = rows[0]["score"] if rows else 0.0
    result = relating.match(event, rows=rows)
    return best_score, neighbours, result


def _render_comment(event: dict, reason: str, resolves: bool) -> str:
    """Mirrors handlers/task_create.py::_suppress's comment text — the exact
    string that function would post to Asana on a match, reproduced here
    (not imported) so this read-only harness never touches handler code.
    This is a MIRROR, not a shared implementation: the lead-sentence choice
    and the trailing `.rstrip(" —")` must be kept identical to _suppress by
    hand, so a future change there is visibly a change that should be
    ported here too.

    The harness's synthetic events (see to_event()) always set web_link to
    None, so the rendered string ends after the reason with no trailing
    link — that's expected and correct for a dry run, not a bug to fix by
    fabricating a link."""
    lead = "Looks resolved — close this task if you agree." if resolves else "Related email:"
    return (f"{lead} {event['subject']} — {reason} — {event.get('web_link') or ''}").rstrip(" —")


def run(args) -> int:
    from services import screening

    env = load_env(pathlib.Path(args.inbox_env).expanduser())
    conn = connect(env)
    cur = conn.cursor()

    ids: list[str] | None = None
    if args.ids:
        ids = load_ids(pathlib.Path(args.ids).expanduser())
        if not ids:
            # Falling through here would silently replay every corpus whole —
            # the exact 65-minute, ~$3 run --ids exists to avoid. Fail loud.
            print(
                f"--ids {args.ids} contained no usable ids after stripping blank "
                f"lines and '#' comments — refusing to fall back to a full corpus "
                f"run. Nothing was replayed.",
                file=sys.stderr,
            )
            conn.close()
            return 1
    unmatched = set(ids) if ids else None

    rows_out: list[dict] = []
    skipped = 0
    for name in args.corpus or list(CORPORA):
        sql, expected = CORPORA[name]
        params: list[str] | None = None
        if ids:
            # Restrict this corpus's own query to the requested ids rather than
            # replaying it whole. An id can satisfy more than one corpus's WHERE
            # clause (e.g. `negative` and `recall_probe` overlap) — dedup below
            # keeps whichever corpus is searched first (args.corpus order, or
            # CORPORA's definition order when --corpus is omitted).
            placeholders = ", ".join(["%s"] * len(ids))
            sql = f"SELECT * FROM ({sql}) sub WHERE sub.id::text IN ({placeholders})"
            params = list(ids)
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        batch = cur.fetchall()
        total = len(batch)
        for i, row in enumerate(batch, start=1):
            if i == 1 or i % 25 == 0 or i == total:
                print(f"[{name}] {i}/{total}", file=sys.stderr)
            if ids:
                mid = str(row[0])
                if mid not in unmatched:
                    continue  # already emitted for an earlier corpus — first match wins
                unmatched.discard(mid)
            event = to_event(row)
            verdict = screening.screen(event)
            if verdict.outcome == "fail_open":
                skipped += 1
                print(f"  fail_open (not scored): {event['message_id']}", file=sys.stderr)
                continue

            best_score, neighbours, matched_gid, resolves = 0.0, "", "", ""
            match_reason, comment = "", ""
            if verdict.verdict == "relate" and not args.no_relate:
                best_score, neighbours, found = _relate(event)
                matched_gid = found.task_gid or ""
                resolves = str(found.resolves)
                if found.task_gid:
                    match_reason = found.reason or ""
                    comment = _render_comment(event, found.reason, found.resolves)

            rows_out.append(
                {
                    "corpus": name,
                    "message_id": event["message_id"],
                    "expected": expected or "",
                    "verdict": verdict.verdict,
                    "priority": verdict.priority,
                    "outcome": verdict.outcome,
                    "matched_gid": matched_gid,
                    "resolves": resolves,
                    "best_score": f"{best_score:.3f}" if best_score else "",
                    "neighbours": neighbours,
                    "sender": event["sender"],
                    "subject": event["subject"][:80],
                    "reason": verdict.reason,
                    "match_reason": match_reason,
                    "comment": comment,
                }
            )
    conn.close()

    if ids and unmatched:
        print(
            f"\n{len(unmatched)} id(s) in {args.ids} matched no corpus row "
            f"(wrong id, or excluded by --corpus): {sorted(unmatched)}",
            file=sys.stderr,
        )

    if not rows_out:
        print(f"\nno rows scored ({skipped} fail_open, not scored) — nothing written to {args.out}")
        return 0

    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows_out[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows_out)

    summarise(rows_out, skipped, args.out)
    return 0


def _wrong(row: dict) -> bool:
    """A scoring miss. `non_task` accepts drop OR relate."""
    if not row["expected"]:
        return False
    if row["expected"] == "non_task":
        return row["verdict"] == "task"
    return row["verdict"] != row["expected"]


def summarise(rows: list[dict], skipped: int, out: str) -> None:
    print(f"\nwrote {len(rows)} rows to {out} ({skipped} fail_open, not scored)\n")
    print(f"{'corpus':22} {'n':>5} {'task':>6} {'relate':>7} {'drop':>6} {'task rate':>10}  misses")
    for name in dict.fromkeys(r["corpus"] for r in rows):
        group = [r for r in rows if r["corpus"] == name]
        counts = collections.Counter(r["verdict"] for r in group)
        rate = counts["task"] / len(group) if group else 0.0
        print(
            f"{name:22} {len(group):5} {counts['task']:6} {counts['relate']:7} "
            f"{counts['drop']:6} {rate:9.1%}  {sum(1 for r in group if _wrong(r))}"
        )

    relates = [r for r in rows if r["verdict"] == "relate" and r["best_score"]]
    if relates:
        matched = [r for r in relates if r["matched_gid"]]
        print(
            f"\nrelate: {len(matched)}/{len(relates)} matched an open task "
            f"({len(matched) / len(relates):.0%}); "
            f"{sum(1 for r in matched if r['resolves'] == 'True')} claim resolves"
        )
        print("  check the floor and the prompt, but the open-task pool is small and")
        print("  live — it bounds what can ever match (see spec caveat 3)")

    domains = collections.Counter(
        r["sender"].rsplit("@", 1)[-1]
        for r in rows
        if r["corpus"] == "negative" and r["verdict"] == "task"
    )
    if domains:
        print(
            "\ntop `task`-promoted sender domains in `negative` "
            "(a pile of one vendor means the prompt is too loose):"
        )
        for domain, count in domains.most_common(12):
            print(f"  {count:4}  {domain}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backtest.tsv")
    parser.add_argument(
        "--corpus",
        action="append",
        choices=list(CORPORA),
        help="restrict to this corpus. With --ids, also restricts which "
        "corpora's queries are searched for the requested ids "
        "(default: search all of them).",
    )
    parser.add_argument(
        "--ids",
        help="replay only the message_ids listed in this file (one per "
        "line; blank lines and '#' comments ignored) instead of a "
        "whole corpus — a fast, cheap loop for iterating on a handful "
        "of known rows. Each id keeps the corpus label and `expected` "
        "value of whichever corpus's WHERE clause it satisfies; an id "
        "matching more than one corpus is emitted once, under whichever "
        "one is searched first (the order given to --corpus, or "
        "CORPORA's definition order when --corpus is omitted). An id "
        "that matches none of the searched corpora is reported on "
        "stderr, never silently dropped.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--no-relate",
        action="store_true",
        help="skip the relating stage (screener verdicts only, cheaper)",
    )
    parser.add_argument("--inbox-env", default="~/src/inbox/.env")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
