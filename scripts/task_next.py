#!/usr/bin/env python3
"""What should I work on next — the CLI over tasks-api's prioritizer.

    task-next                                  # today's list + side lists
    task-next --energy deep --n 3 --explain
    task-next ranking [--all] [--explain]      # the full order (GET /ranking)
    task-next start <ref|gid>                  # Started at = today
    task-next points <ref|gid> <n>
    task-next pin <ref|gid> <position> | unpin <ref|gid>
    task-next snooze <ref|gid> <YYYY-MM-DD> | unsnooze <ref|gid>
    task-next override <ref|gid> field=value ... (field= clears)
    task-next calibrate

Refs are scripts/task_ref.py refs over the ranking; a write resolves a ref
against GET /ranking?bucket=next and every other bucket before sending the
GID. Stdlib only — this runs from PATH under whatever python3 is first.
`scripts/link-skills.sh` symlinks this onto PATH as `task-next`."""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_ref  # noqa: E402

API_BASE = "https://tasks-api.drolet.cloud"
OVERRIDE_FIELDS = (
    "waiting_on",
    "impact",
    "energy",
    "due_date_inferred",
    "story_points",
    "pinned_rank",
    "snooze_until",
)


def _token() -> str:
    try:
        return subprocess.run(
            ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"task-next: gcloud auth print-identity-token failed: {e}")


def _api(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    url = API_BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"task-next: {method} {path} -> {e.code}: {e.read().decode(errors='replace')}"
        )


def _refs(tasks: list[dict]) -> dict[str, str]:
    """gid -> ref, collision-resolved across the whole set (task_ref.assign)."""
    return task_ref.assign([t["task_gid"] for t in tasks])


def _line(t: dict, refs: dict[str, str], explain: bool) -> str:
    due = t.get("effective_due") or "—"
    soft = "~" if t.get("soft") else ""
    flags = "".join(
        s
        for s, on in (
            ("!", t.get("overcommitted")),
            ("z", t.get("stale")),
            ("📌", (t.get("override") or {}).get("pinned_rank")),
        )
        if on
    )
    row = [
        refs[t["task_gid"]],
        t["task_gid"],
        f"{soft}{due}",
        f"{t.get('points') or '?'}p",
        t["name"],
        t.get("project") or "—",
        flags,
    ]
    if explain and t.get("components"):
        c = t["components"]
        row.append(
            f"score={t.get('score'):.2f} P={c.get('P', 0):.2f} U={c.get('U', 0):.2f} I={c.get('I', 0):.2f} "
            f"B={c.get('B', 0):.2f} A={c.get('A', 0):.2f} C={c.get('C', 0):.2f} slack={c.get('effective_slack')} "
            f"{'unenriched' if c.get('unenriched') else ''} {t.get('reason') or ''}".strip()
        )
    return "\t".join(str(x) for x in row)


def render_lists(payload: dict, explain: bool = False) -> str:
    all_tasks = [
        t for key in ("next", "overcommitted", "stale", "nudge") for t in payload.get(key, [])
    ]
    refs = _refs(all_tasks)
    out = [f"# {payload.get('today')} — ref\tgid\tdue\tpts\tname\tproject\tflags"]
    for key, title in (
        ("next", "Next"),
        ("overcommitted", "Overcommitted"),
        ("stale", "Stale / re-scope"),
        ("nudge", "Nudge"),
    ):
        out.append(f"## {title}")
        rows = payload.get(key) or []
        out += [_line(t, refs, explain) for t in rows] or ["—"]
    if payload.get("unenriched"):
        out.append(f"({payload['unenriched']} task(s) scored with defaults — enrichment pending)")
    return "\n".join(out)


def render_ranking(payload: dict, explain: bool = False) -> str:
    tasks = payload.get("tasks") or []
    refs = _refs(tasks)
    out = [
        f"# {payload.get('today')} — {payload.get('total')} tasks — ref\tgid\tdue\tpts\tname\tproject\tflags"
    ]
    out += [f"{t['position']}\t" + _line(t, refs, explain) for t in tasks]
    return "\n".join(out)


def _all_tasks() -> list[dict]:
    tasks: list[dict] = []
    for bucket in ("next", "nudge", "snoozed", "excluded"):
        tasks += _api("GET", "/ranking", params={"bucket": bucket, "limit": 500}).get("tasks", [])
    return tasks


def resolve(ref_or_gid: str, tasks: list[dict]) -> str:
    if ref_or_gid.isdigit() and len(ref_or_gid) > 3:
        return ref_or_gid
    refs = _refs(tasks)
    for gid, ref in refs.items():
        if ref == ref_or_gid:
            return gid
    raise SystemExit(f"task-next: no task with ref {ref_or_gid!r} in the current ranking")


def _overrides(gid: str, patch: dict) -> None:
    _api("PUT", f"/tasks/{gid}/overrides", patch)
    print(f"updated {gid}: {json.dumps(patch)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-next")
    sub = parser.add_subparsers(dest="cmd")
    parser.add_argument("--energy", choices=("deep", "shallow"))
    parser.add_argument("--n", type=int)
    parser.add_argument("--explain", action="store_true")
    r = sub.add_parser("ranking")
    r.add_argument("--all", action="store_true")
    r.add_argument("--explain", action="store_true")
    for name in ("start", "unpin", "unsnooze"):
        sub.add_parser(name).add_argument("task")
    p = sub.add_parser("points")
    p.add_argument("task")
    p.add_argument("n", type=int)
    p = sub.add_parser("pin")
    p.add_argument("task")
    p.add_argument("position", type=int)
    p = sub.add_parser("snooze")
    p.add_argument("task")
    p.add_argument("until")
    p = sub.add_parser("override")
    p.add_argument("task")
    p.add_argument("pairs", nargs="+")
    sub.add_parser("calibrate")
    args = parser.parse_args(argv)

    if args.cmd is None:
        body: dict = {"explain": args.explain}
        if args.energy:
            body["energy"] = args.energy
        if args.n:
            body["n"] = args.n
        print(render_lists(_api("POST", "/next", body), args.explain))
        return 0
    if args.cmd == "ranking":
        if args.all:
            tasks = _all_tasks()
            payload = {"today": None, "total": len(tasks), "tasks": tasks}
        else:
            payload = _api(
                "GET", "/ranking", params={"limit": 500, "explain": str(args.explain).lower()}
            )
        print(render_ranking(payload, args.explain))
        return 0
    if args.cmd == "calibrate":
        data = _api("GET", "/calibrate")
        print("project\tcompleted\tcycle d/pt (mean)\t(median)\tset/estimated\tdeferred")
        for p in data["projects"] + [{"project": "OVERALL", **data["overall"]}]:
            f = lambda v: "—" if v is None else f"{v:.2f}"  # noqa: E731
            print(
                f"{p['project']}\t{p['completed']}\t{f(p['mean_cycle_days_per_point'])}\t{f(p['median_cycle_days_per_point'])}\t{f(p['mean_points_ratio'])}\t{json.dumps(p['deferred_histogram'])}"
            )
        return 0

    gid = resolve(
        args.task, _all_tasks() if not (args.task.isdigit() and len(args.task) > 3) else []
    )
    if args.cmd == "start":
        _api("PATCH", f"/tasks/{gid}", {"started_at": date.today().isoformat()})
        print(f"started {gid} today")
    elif args.cmd == "points":
        _api("PATCH", f"/tasks/{gid}", {"story_points": args.n})
        print(f"{gid}: {args.n} points")
    elif args.cmd == "pin":
        _overrides(gid, {"pinned_rank": args.position})
    elif args.cmd == "unpin":
        _overrides(gid, {"pinned_rank": None})
    elif args.cmd == "snooze":
        _overrides(gid, {"snooze_until": args.until})
    elif args.cmd == "unsnooze":
        _overrides(gid, {"snooze_until": None})
    elif args.cmd == "override":
        patch = {}
        for pair in args.pairs:
            key, _, value = pair.partition("=")
            if key not in OVERRIDE_FIELDS:
                raise SystemExit(
                    f"task-next: unknown field {key!r}; one of {', '.join(OVERRIDE_FIELDS)}"
                )
            patch[key] = (
                (int(value) if key in ("story_points", "pinned_rank") else value) if value else None
            )
        _overrides(gid, patch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
