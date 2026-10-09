"""The weekly review: a pure builder over stored rows (spec D11) and a
markdown renderer, plus the necessity half of calibrate (D10)."""

from datetime import date

from models.strategy import GoalState, Strategy


def _ref(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"gid": row["task_gid"], "name": row["name"], "permalink_url": row.get("permalink_url")}


def build(
    strategy: Strategy,
    states: dict[str, GoalState],
    scores: list[dict],
    suppressed: list[dict],
    today: date,
) -> dict:
    by_gid = {r["task_gid"]: r for r in scores}
    goals = []
    for g in strategy.goals:
        s = (states.get(g.id).state if states.get(g.id) else {}) or {}
        entry: dict = {
            "id": g.id,
            "kind": g.kind,
            "weight": g.weight,
            "next_step": _ref(by_gid.get(s.get("next_step"))),
            "stalled": s.get("next_step") is None,
        }
        if g.kind == "outcome":
            entry.update(
                {
                    "horizon": g.horizon.isoformat() if g.horizon else None,
                    "leads": s.get("leads", []),
                    "lag": s.get("lag"),
                    "tripwires": s.get("tripwires", []),
                    "diagnosis": s.get("diagnosis", "insufficient data"),
                }
            )
        else:
            entry.update(
                {
                    "below_the_line": bool(s.get("below_the_line")),
                    "muted_until": s.get("muted_until"),
                    "signals": [
                        sig
                        | {
                            "tasks": [
                                _ref(by_gid.get(t)) or {"gid": t, "name": t, "permalink_url": None}
                                for t in sig.get("tasks", [])
                            ]
                        }
                        for sig in s.get("signals", [])
                    ],
                }
            )
        goals.append(entry)
    active = [r for r in scores if r["bucket"] in ("next", "nudge", "stop_doing")]
    grooming = [
        _ref(r)
        | {
            "serves_suggested": r["components"].get("serves_suggested")
            or r["components"].get("serves")
            or [],
            "confidence": r["components"].get("necessity_confidence"),
            "reason": r["components"].get("reason"),
        }
        for r in active
        if r["components"].get("grooming")
    ]
    stop = [
        _ref(r) | {"reason": r["components"].get("reason")}
        for r in active
        if r["components"].get("confident_none")
    ]
    return {
        "reviewed_at": today.isoformat(),
        "strategy_last_reviewed": strategy.last_reviewed.isoformat()
        if strategy.last_reviewed
        else None,
        "findings": list(strategy.findings),
        "goals": goals,
        "grooming": grooming,
        "stop_doing": {
            "tasks": stop,
            "suppressed_emails": [
                {
                    "message_id": r["message_id"],
                    "subject": r.get("subject"),
                    "sender": r.get("sender"),
                    "web_link": r.get("web_link"),
                    "reason": r.get("reason"),
                    "created_at": r["created_at"].isoformat()
                    if hasattr(r.get("created_at"), "isoformat")
                    else r.get("created_at"),
                    "restored": r.get("restored_at") is not None,
                }
                for r in suppressed
            ],
        },
    }


def _link(ref: dict | None) -> str:
    if not ref:
        return "—"
    return f"[{ref['name']}]({ref['permalink_url']})" if ref.get("permalink_url") else ref["name"]


def render(review: dict) -> str:
    out = [f"# Weekly strategy review — {review.get('reviewed_at', '')}", ""]
    if review.get("findings"):
        out += ["**Findings**"] + [f"- {f}" for f in review["findings"]] + [""]
    for g in review.get("goals", []):
        out.append(f"## {g.get('id')} ({g.get('kind')})")
        out.append(
            f"- next step: {_link(g.get('next_step'))}"
            + (" — **stalled**" if g.get("stalled") else "")
        )
        if g.get("kind") == "outcome":
            for lead in g.get("leads", []):
                out.append(
                    f"- lead {lead.get('tag')}: {lead.get('value')} / {lead.get('threshold', 0):g} per {lead.get('window')} — {'met' if lead.get('met') else 'not met'}"
                )
            lag = g.get("lag")
            out.append(
                f"- lag: {lag.get('value', 0):g} / {lag.get('threshold', 0):g} — {'met' if lag.get('met') else 'not met'}"
                if lag
                else "- lag: not reported"
            )
            for t in g.get("tripwires", []):
                status = (
                    "**FIRED**"
                    if t.get("fired")
                    else ("watching" if t.get("evaluated") else "not yet due")
                )
                out.append(f"- tripwire {t.get('text')} → {t.get('action')}: {status}")
            out.append(f"- diagnosis: **{g.get('diagnosis', 'insufficient data')}**")
        else:
            out.append(
                f"- below the line: {'**yes**' if g.get('below_the_line') else 'no'}"
                + (f" (muted until {g['muted_until']})" if g.get("muted_until") else "")
            )
            for s in g.get("signals", []):
                tasks = ", ".join(_link(t) for t in s.get("tasks", [])) or "—"
                out.append(
                    f"  - {s.get('signal')} [{s.get('class', 'evidence')}]: {s.get('state', '?')} ({s.get('consecutive_days', 0)}d) — {tasks}"
                )
        out.append("")
    out.append("## Grooming")
    out += [
        f"- {_link(t)} — suggested {', '.join(t.get('serves_suggested') or []) or 'nothing'} ({t.get('confidence')}): {t.get('reason')}"
        for t in review.get("grooming", [])
    ] or ["- nothing"]
    out += ["", "## Stop doing"]
    stop = review.get("stop_doing", {})
    out += [
        f"- {_link(t)} — {t.get('reason')} (remove, or attach with a serves: tag)"
        for t in stop.get("tasks", [])
    ] or ["- nothing"]
    for e in stop.get("suppressed_emails", []):
        flag = " — restored" if e.get("restored") else ""
        subject = e.get("subject") or "(no subject)"
        label = f"[{subject}]({e['web_link']})" if e.get("web_link") else subject
        out.append(f"- email {label} from {e.get('sender')} — {e.get('reason')}{flag}")
    return "\n".join(out) + "\n"


def _effective(row: dict) -> tuple[list[str], str | None]:
    tags = row.get("tags") or []
    serves = [t.partition(":")[2] for t in tags if str(t).casefold().startswith("serves:")]
    role = next((t.partition(":")[2] for t in tags if str(t).casefold().startswith("role:")), None)
    if not serves and row.get("overrides"):
        serves = list((row["overrides"] or {}).get("serves") or [])
        role = (row["overrides"] or {}).get("role") or role
    return sorted(serves), role


def necessity_calibration(rows: list[dict], restore_rates: list[dict]) -> dict:
    by_conf: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    by_strategy: dict[str, dict] = {}
    grooming = {"attached": 0, "unresolved": 0}

    def bump(bucket: dict, key: str, agreed: bool) -> None:
        b = bucket.setdefault(key, {"judged": 0, "agreed": 0})
        b["judged"] += 1
        b["agreed"] += int(agreed)

    for r in rows:
        est = r.get("serves_estimated") or {}
        serves, role = _effective(r)
        if est.get("grooming"):
            grooming["attached" if serves else "unresolved"] += 1
            continue
        if est.get("none"):
            agreed = not serves
            conf = est.get("confidence", "unknown")
        else:
            agreed = sorted(est.get("serves") or []) == serves and est.get("role") == role
            conf = est.get("confidence", "unknown")
        bump(by_conf, conf, agreed)
        bump(by_source, r.get("source") or "enrichment", agreed)
        if r.get("strategy_hash"):
            bump(by_strategy, r["strategy_hash"], agreed)
    gate2: dict[str, dict] = {}
    enrich: dict[str, dict] = {}
    for r in rows:
        est = r.get("serves_estimated") or {}
        if est.get("grooming"):
            continue
        origin = r.get("source") or "enrichment"
        if origin == "gate2":
            gate2[r["task_gid"]] = est
        elif origin == "enrichment":
            enrich[r["task_gid"]] = est
    g2 = {"judged": 0, "agreed": 0}
    for gid in gate2.keys() & enrich.keys():
        a, b = gate2[gid], enrich[gid]
        g2["judged"] += 1
        same_serves = sorted(a.get("serves") or []) == sorted(b.get("serves") or [])
        same_role = (a.get("none") and b.get("none")) or a.get("role") == b.get("role")
        g2["agreed"] += int(bool(same_serves and same_role))
    g2["rate"] = g2["agreed"] / g2["judged"] if g2["judged"] else None
    for bucket in (by_conf, by_source, by_strategy):
        for b in bucket.values():
            b["rate"] = b["agreed"] / b["judged"] if b["judged"] else None
    sup = {}
    for r in restore_rates:
        decided = int(r["restored"]) + int(r["settled"])
        sup[r["band"]] = {
            "restored": int(r["restored"]),
            "settled": int(r["settled"]),
            "pending": int(r["pending"]),
            "restore_rate": (int(r["restored"]) / decided) if decided else None,
        }
    return {
        "by_confidence": by_conf,
        "by_source": by_source,
        "by_strategy": by_strategy,
        "grooming": grooming,
        "suppressions": sup,
        "gate2_vs_enrichment": g2,
    }
