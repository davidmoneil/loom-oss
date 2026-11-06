# Contributing to Loom

Thanks for your interest. Loom is a small, young project; bug reports, questions
and focused pull requests are welcome.

## Licensing note

Loom is source-available under the [PolyForm Internal Use License 1.0.0](LICENSE),
not an OSI-approved open source license. By submitting a contribution you agree
that it may be distributed as part of Loom under that license, and that the
maintainer may relicense the project (including your contribution) in the future.
If that doesn't work for you, please open an issue to discuss before investing time.

## Before you start

- **Bugs**: search existing issues, then open one using the bug template.
- **Features**: open a feature request first and wait for a response. Check the
  [ROADMAP](ROADMAP.md) — some areas are already planned or deliberately out of scope.
- **Security problems**: do not open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/davidmoneil/loom-oss.git && cd loom-oss
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ,postgres to test the Postgres backend
cp loom.example.yaml loom.yaml
loom serve
```

The dashboard source lives in `dashboard/` (React); the gateway in `src/loom/`.

## Checks to run

CI runs the following; please run them locally before opening a PR:

```bash
ruff check --select E9,F63,F7,F82 src tests
pytest -q
```

Add or update tests for any behavior change.

## Pull requests

- One purpose per PR, linked to an issue (`Closes #N`).
- Use [conventional commit](https://www.conventionalcommits.org/) subjects
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Add a line to `CHANGELOG.md` under **Unreleased** for user-visible changes.
- Keep config-driven behavior in `loom.yaml`; don't hardcode values that belong
  there. Never add telemetry or any outbound traffic other than the LLM calls
  the user configures.
- Never commit secrets, `.env` files, or local `loom*.yaml` configs.

## Versioning

Loom follows [Semantic Versioning](https://semver.org/). While the version is
`0.x`, minor releases (`0.N.0`) may include breaking changes to configuration or
internal APIs; patch releases (`0.N.P`) are fixes only. Breaking changes are
called out in `CHANGELOG.md`.

The observability API (`/api/costs`, `/api/audit`, `/api/sessions`, `/health`)
follows a stable v1 contract ([docs/observability-api.md](docs/observability-api.md));
changes to it are additive within v1.

## Conduct

Be respectful and constructive. Harassment or hostile behavior will get you
removed from the project's spaces.
