### Task 1: Scaffold repo and tooling

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.pre-commit-config.yaml`, `conftest.py`
- Create: `clients/__init__.py`, `handlers/__init__.py`, `services/__init__.py`, `models/__init__.py`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Produces: importable packages `clients`, `handlers`, `services`, `models`; a `.venv` with dev deps; ruff/mypy/pytest configs every later task relies on.

- [ ] **Step 1: Init repo and directories**

```bash
cd /Users/ben/src/tasks
git init -b main
git commit --allow-empty -m "chore: initial commit"
git checkout -b tasks-service
mkdir -p clients repo handlers services models tests scripts terraform docs .claude/skills .github/workflows
touch clients/__init__.py repo/__init__.py handlers/__init__.py services/__init__.py models/__init__.py conftest.py
```

All implementation commits (Tasks 1–15) land on the `tasks-service` branch; `main` keeps only the empty initial commit until the PR merges (Task 20). This keeps the auto-deploy workflow off `main` until the infra is applied and verified.

(The plan file itself lives in `docs/superpowers/plans/` — it gets committed with this task.)

- [ ] **Step 2: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
venv/
*.egg-info/
dist/
build/

# Terraform
.terraform/
*.tfstate
*.tfstate.*
terraform.tfvars
*.tfvars.backup
crash.log
override.tf
override.tf.json
*_override.tf
*_override.tf.json

# Secrets / tokens
.env
.env.*
!.env.example
*token*.json

# macOS
.DS_Store
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[tool.ruff]
target-version = "py313"
line-length = 100
exclude = [".claude", ".venv"]

[tool.ruff.lint]
select = ["E", "F", "I"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"main.py" = ["E402"]
"scripts/*.py" = ["E402", "I001"]

[tool.mypy]
python_version = "3.13"
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Write `requirements.txt`**

```
# Asana REST
httpx>=0.27

# Enrichment LLM calls (summary, deadline extraction)
anthropic>=0.25

# Cloud Functions
functions-framework>=3.0

# Database — connector uses pg8000 (does NOT support psycopg); psycopg3 for local direct
psycopg>=3.1
pg8000>=1.31
cloud-sql-python-connector>=1.7.0

# Observability
opentelemetry-sdk>=1.24
opentelemetry-exporter-otlp-proto-http>=1.24

# Environment (local dev / scripts)
python-dotenv>=1.0.0
```

- [ ] **Step 5: Write `requirements-dev.txt`**

```
ruff>=0.4
mypy>=1.10
pytest>=8.0
pre-commit>=3.7
# Runtime deps the test suite imports (CI installs only this file):
httpx>=0.27
anthropic>=0.25
functions-framework>=3.0
psycopg>=3.1
pg8000>=1.31
cloud-sql-python-connector>=1.7.0
opentelemetry-sdk>=1.24
opentelemetry-exporter-otlp-proto-http>=1.24
python-dotenv>=1.0.0
```

- [ ] **Step 6: Write `.pre-commit-config.yaml`** (verbatim from inbox)

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.10
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.10.0
    hooks:
      - id: mypy
        args: [--ignore-missing-imports]
        additional_dependencies: []
```

- [ ] **Step 7: Write the failing test** — `tests/test_scaffold.py`

```python
import importlib


def test_packages_importable():
    for pkg in ("clients", "handlers", "services", "models"):
        importlib.import_module(pkg)
```

- [ ] **Step 8: Create venv, install, run test**

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/test_scaffold.py -v
```

Expected: PASS (packages exist from Step 1 — this test guards the scaffold, no implementation step needed).

- [ ] **Step 9: Lint + commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format .
git add -A
git commit -m "chore: scaffold tasks repo — packages, tooling config, dev deps"
```

---

