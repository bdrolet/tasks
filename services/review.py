"""The weekly review: a pure builder over stored rows (spec D11) and a
markdown renderer, plus the necessity half of calibrate (D10)."""

from datetime import date

from models.strategy import GoalState, Strategy


def _ref(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"gid": row["task_gid"], "name": row["name"], "permalink_url": row.get("permalink_url")}


def build(strategy: Strategy, states: dict[str, GoalState], scores: list[dict], suppressed: list[dict], today: date) -> dict:
    by_gid = {r["task_gid"]: r for r in scores}
    goals = []
    for g in strategy.goals:
        s = (states.get(g.id).state if states.get(g.id) else {}) or {}
        entry: dict = {"id": g.id, "kind": g.kind, "weight": g.weight,
                       "next_step": _ref(by_gid.get(s.get("next_step"))), "stalled": s.get("next_step") is None}
        if g.kind == "outcome":
            entry.update({"horizon": g.horizon.isoformat() if g.horizon else None,
                          "leads": s.get("leads", []), "lag": s.get("lag"), "tripwires": s.get("tripwires", []),
                          "diagnosis": s.get("diagnosis", "insufficient data")})
        else:
            entry.update({"below_the_line": bool(s.get("below_the_line")), "muted_until": s.get("muted_until"),
                          "signals": [sig | {"tasks": [_ref(by_gid.get(t)) or {"gid": t, "name": t, "permalink_url": None}
                                                       for t in sig.get("tasks", [])]}
                                      for sig in s.get("signals", [])]})
        goals.append(entry)
    active = [r for r in scores if r["bucket"] in ("next", "nudge", "stop_doing")]
    grooming = [
        _ref(r) | {"serves_suggested": r["components"].get("serves_suggested") or r["components"].get("serves") or [],
                   "confidence": r["components"].get("necessity_confidence"), "reason": r["components"].get("reason")}
        for r in active if r["components"].get("grooming")
    ]
    stop = [_ref(r) | {"reason": r["components"].get("reason")} for r in active if r["components"].get("confident_none")]
    return {
        "reviewed_at": today.isoformat(),
        "strategy_last_reviewed": strategy.last_reviewed.isoformat() if strategy.last_reviewed else None,
        "findings": list(strategy.findings),
        "goals": goals,
        "grooming": grooming,
        "stop_doing": {
            "tasks": stop,
            "suppressed_emails": [
                {"message_id": r["message_id"], "subject": r.get("subject"), "sender": r.get("sender"),
                 "web_link": r.get("web_link"), "reason": r.get("reason"),
                 "created_at": r["created_at"].isoformat() if hasattr(r.get("created_at"), "isoformat") else r.get("created_at"),
                 "restored": r.get("restored_at") is not None}
                for r in suppressed
            ],
        },
    }


def _link(ref: dict | None) -> str:
    if not ref:
        return "—"
    return f"[{ref['name']}]({ref['permalink_url']})" if ref.get("permalink_url") else ref["name"]


def render(review: dict) -> str:
    out = [f"# Weekly strategy review — {review['reviewed_at']}", ""]
    if review["findings"]:
        out += ["**Findings**"] + [f"- {f}" for f in review["findings"]] + [""]
    for g in review["goals"]:
        out.append(f"## {g['id']} ({g['kind']})")
        out.append(f"- next step: {_link(g['next_step'])}" + (" — **stalled**" if g["stalled"] else ""))
        if g["kind"] == "outcome":
            for lead in g["leads"]:
                out.append(f"- lead {lead['tag']}: {lead['value']} / {lead['threshold']:g} per {lead['window']} — {'met' if lead['met'] else 'not met'}")
            lag = g["lag"]
            out.append(f"- lag: {lag['value']:g} / {lag['threshold']:g} — {'met' if lag['met'] else 'not met'}" if lag else "- lag: not reported")
            for t in g["tripwires"]:
                out.append(f"- tripwire {t['text']} → {t['action']}: {'**FIRED**' if t['fired'] else ('watching' if t['evaluated'] else 'not yet due')}")
            out.append(f"- diagnosis: **{g['diagnosis']}**")
        else:
            out.append(f"- below the line: {'**yes**' if g['below_the_line'] else 'no'}" + (f" (muted until {g['muted_until']})" if g["muted_until"] else ""))
            for s in g["signals"]:
                tasks = ", ".join(_link(t) for t in s["tasks"]) or "—"
                out.append(f"  - {s['signal']} [{s['class']}]: {s['state']} ({s['consecutive_days']}d) — {tasks}")
        out.append("")
    out.append("## Grooming")
    out += [f"- {_link(t)} — suggested {', '.join(t['serves_suggested']) or 'nothing'} ({t['confidence']}): {t['reason']}" for t in review["grooming"]] or ["- nothing"]
    out += ["", "## Stop doing"]
    out += [f"- {_link(t)} — {t['reason']} (remove, or attach with a serves: tag)" for t in review["stop_doing"]["tasks"]] or ["- nothing"]
    for e in review["stop_doing"]["suppressed_emails"]:
        flag = " — restored" if e["restored"] else ""
        out.append(f"- email [{e['subject']}]({e['web_link']}) from {e['sender']} — {e['reason']}{flag}")
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
    for bucket in (by_conf, by_source, by_strategy):
        for b in bucket.values():
            b["rate"] = b["agreed"] / b["judged"] if b["judged"] else None
    sup = {}
    for r in restore_rates:
        decided = int(r["restored"]) + int(r["settled"])
        sup[r["band"]] = {"restored": int(r["restored"]), "settled": int(r["settled"]), "pending": int(r["pending"]),
                          "restore_rate": (int(r["restored"]) / decided) if decided else None}
    return {"by_confidence": by_conf, "by_source": by_source, "by_strategy": by_strategy, "grooming": grooming, "suppressions": sup}
