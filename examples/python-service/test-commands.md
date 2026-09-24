# Test commands

- Unit tests (fast, no services): `.venv/bin/pytest -q tests/unit`
- Integration tests (need Postgres on :5432 — run preflight first): `.venv/bin/pytest -q tests/integration`
- Lint: `.venv/bin/ruff check .`
