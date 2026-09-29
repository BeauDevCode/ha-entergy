# Contributing

Use Python 3.14 and repository-pinned `uv==0.12.19`. Sync the current environment with `uv sync --frozen`; the minimum Home Assistant 2026.9.3 environment comes from `requirements/ha-2026.9.3.txt` in a separate Python 3.14 virtual environment. The current lock targets 2026.9.4. Do not resolve or upgrade dependencies merely to run tests.

Before proposing a change, run `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy --explicit-package-bases custom_components/entergy_mobile`, and `uv run pytest -q --cov=custom_components.entergy_mobile --cov-branch --cov-report=json:coverage.json --cov-fail-under=95`, followed by `uv run python scripts/check_coverage.py coverage.json`. Repeat pytest and the coverage checker in the minimum environment. CI also runs HACS, hassfest, `uv run pip-audit`, CodeQL, and a secret scan.

Every fixture must be synthetic. Do not use live utility credentials or calls. Account data, addresses, tokens, captures, databases, Home Assistant runtime state, and homelab material must never enter commits, issues, tests, or CI artifacts. Review is required for runtime dependencies, schema/migration changes, Recorder changes, network endpoints, action pins, and security-affecting behavior.

A contribution does not imply acceptance, release, support, or an Entergy relationship. See [security reporting](SECURITY.md) for vulnerabilities.
