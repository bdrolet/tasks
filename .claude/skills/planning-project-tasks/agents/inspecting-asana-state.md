---
name: inspecting-asana-state
description: Fetch Asana projects/sections/tags and dedup-search candidate task names; return a compact digest.
model: fast
---

# Inspecting Asana State

## Inputs
- A list of candidate task names (the parent's decomposition of the brief).

## Steps
1. Resolve auth:
   ```bash
   TOKEN=$(grep 'tasks_api_token' ~/src/tasks/terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
   BASE=https://tasks-api.drolet.cloud
   ```
2. `curl -s "$BASE/projects" -H "Authorization: Bearer $TOKEN"` — projects + sections.
3. `curl -s "$BASE/tags" -H "Authorization: Bearer $TOKEN"` — tag vocabulary.
4. For each candidate name, dedup-search (open + completed):
   ```bash
   curl -s -XPOST "$BASE/search" -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"query":"<name>","completed":null}'
   ```

## Output
A compact digest ONLY (no raw JSON dumps):
- **Projects**: each project name → its section names.
- **Tags**: the existing tag names.
- **Dedup**: per candidate, any close-match existing tasks as `name (gid) — project`,
  or "no matches". Do not decide skips — just report matches.
