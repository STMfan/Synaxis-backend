# Synaxis backend 1.0.0

This standalone source package contains backend/, main.py and the backend CI workflow. The student client is distributed separately.

Start with [backend/README.md](backend/README.md). Setup is documented in [deployment](backend/docs/deployment.md). Project code uses [MIT](LICENSE); third-party resources keep their own terms as described in [backend/NOTICE](backend/NOTICE).

## Core checks

```sh
cd backend
uv venv .venv --python 3.12
uv sync --frozen
uv run --frozen ruff check api auth core domain llm runtime storage tools tests ../main.py
uv run --frozen pytest -q
uv run --frozen python -m runtime.openapi_contract --check
uv run --frozen python -m runtime.publication --check
uv run --frozen python ../main.py --check
```

Unit checks do not require production credentials, runtime data, curriculum assets or models. Runtime dependencies and optional integrations are installed separately.
