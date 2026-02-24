# Contributing to mcp-recorder

Thanks for your interest in contributing! This guide covers everything you need
to get started.

## Getting Started

1. Fork the repo and clone your fork:

   ```bash
   git clone https://github.com/<your-user>/mcp-recorder.git
   cd mcp-recorder
   ```

2. Install dependencies (requires [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync --group dev
   ```

3. (Optional) Install pre-commit hooks:

   ```bash
   uv run pre-commit install
   ```

## Development Workflow

1. Create a feature branch from `main`:

   ```bash
   git checkout -b feat/my-change
   ```

2. Make your changes and ensure all checks pass (see below).

3. Push your branch and open a Pull Request against `main`.

4. CI must be green before a maintainer will review.

5. A maintainer merges the PR once approved.

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and
formatting, and [mypy](https://mypy-lang.org/) in strict mode for type
checking.

```bash
uv run ruff format src/ tests/   # auto-format
uv run ruff check  src/ tests/   # lint
uv run mypy src/                  # type check
```

If you installed the pre-commit hooks, these run automatically on each commit.

## Testing

Tests use [pytest](https://docs.pytest.org/) with async support via
pytest-asyncio:

```bash
uv run pytest tests/unit tests/integration -v
```

New features and bug fixes should include tests. Aim to cover both the happy
path and relevant edge cases.

## Commit Messages

Use short, descriptive messages. Conventional prefixes are encouraged:

- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `test:` adding or updating tests
- `refactor:` code change that neither fixes a bug nor adds a feature
- `ci:` CI/CD changes

## PR Checklist

Before requesting review, make sure:

- [ ] `ruff format --check` and `ruff check` pass
- [ ] `mypy src/` passes with no errors
- [ ] All tests pass
- [ ] New/changed behavior has test coverage
- [ ] Docs are updated if applicable
