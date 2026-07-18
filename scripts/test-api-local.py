#!/usr/bin/env python3
"""Smoke-test a running tasks-api (local uvicorn or deployed Cloud Run).

Read-only by default: health → search → fetch first hit. --write also
creates a task named "[smoke] tasks-api" in the default project, comments
on it, completes it (REAL Asana writes).

Usage:
  .venv/bin/uvicorn api.main:app --port 8080   # terminal 1, with .env loaded
  .venv/bin/python scripts/test-api-local.py [--base http://localhost:8080] [--write]
"""

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("TASKS_API_URL", "http://localhost:8080"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    headers = {}
    token = os.environ.get("TASKS_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = httpx.Client(base_url=args.base, headers=headers, timeout=60)

    health = client.get("/health")
    health.raise_for_status()
    print("health:", health.json())

    resp = client.post("/search", json={"query": "", "limit": 3})
    resp.raise_for_status()
    results = resp.json()["results"]
    print(f"search: {len(results)} results")
    for r in results:
        print(f"  {r['task_gid']}  {r['name']}  due={r['due_on']}  project={r['project']}")

    if results:
        detail = client.get(f"/tasks/{results[0]['task_gid']}")
        detail.raise_for_status()
        d = detail.json()
        print(f"fetch: {d['name']} — {len(d['comments'])} comments")

    if args.write:
        created = client.post("/tasks", json={"name": "[smoke] tasks-api"})
        created.raise_for_status()
        gid = created.json()["task_gid"]
        print("created:", created.json())
        comment = client.post(f"/tasks/{gid}/comments", json={"text": "smoke"})
        comment.raise_for_status()
        print("comment:", comment.json())
        complete = client.patch(f"/tasks/{gid}", json={"completed": True})
        complete.raise_for_status()
        print("complete:", complete.json())

    return 0


if __name__ == "__main__":
    sys.exit(main())
