# Roadmap

Where Loom is and where it's going. This is a statement of direction, not a
commitment or a schedule; priorities shift with usage. Current release: **v0.1.0**.
Day-to-day changes are in [CHANGELOG.md](CHANGELOG.md).

## Shipped

- **Gateway**: OpenAI-compatible chat completions and Anthropic Messages API
  (streaming, tool use), Ollama-compatible endpoints, Gemini/OpenAI/Anthropic/Ollama providers
- **Routing**: EQRT cost-aware model selection, provider registry, per-source policies,
  cloud rerouting, programmatic (zero-inference) search
- **Compression**: four tiers, content-aware compressors, in-place tool-result
  compression, optional LLM prose compression, optional variant store
- **Data protection**: regex detection with redact / mask / pseudonymize / encrypt actions
- **Observability**: audit trail, cost and savings metrics, sessions, stable v1 API
- **Storage**: SQLite (default) and PostgreSQL behind one interface
- **Dashboard**: Overview, Audit, Sessions, Costs, Metrics, Models, Routing,
  Rate Limits, Governor, Data Protection, Settings

## In progress / next

- **Dashboard-first configuration**: every config domain editable at runtime through the
  API and dashboard (several pages are marked *In Planning* in the UI)
- **Governor enforcement**: budget and limit enforcement (tracking exists today)
- **Original retrieval**: expose resolution of compressed-content pointers over HTTP
  (originals are stored by the variant store but not yet retrievable via the API)
- **Compression page** in the dashboard: visualize savings by tier and content type

## Under consideration

- Plugin / hook extensibility for routing, compression, the variant store and scanning,
  so behavior can be extended without forking
- Per-turn model routing within an interactive session
  ([design notes](docs/design/per-turn-routing.md))
- Richer task classification for routing decisions

## Not planned

- Telemetry, analytics or any phone-home behavior
- Hosted or managed Loom service
- Storing provider credentials in the gateway (callers pass their own)

## Influencing the roadmap

Open a [feature request](../../issues/new?template=feature_request.md) describing the
problem you're solving. See [CONTRIBUTING.md](CONTRIBUTING.md).
