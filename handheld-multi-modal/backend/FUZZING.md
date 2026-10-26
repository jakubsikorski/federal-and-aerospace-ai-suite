# Frontend API fuzzing (SDL504)

From the repository root, using the existing `handheld/backend:dev` image:

```sh
make test-fuzz
make test-fuzz FUZZ_SEED=505 FUZZ_MUTATIONS=256
```

Each run writes a timestamped directory under
`docs/application/evidence/SDL504-api-fuzz/`. The summary lists every OpenAPI
operation and its coverage; the JSONL stores exact request bytes (base64),
parameters, response excerpts and case IDs. Findings make the command fail.
Use the same seed and mutation count to reproduce the campaign; fixture-generated
rule IDs and timestamps vary. No dependencies are installed.

The fuzzer uses the actual FastAPI handlers, temporary SQLite/crop/mission
storage and existing camera fixtures. It mutates frontend JSON request shapes,
encoding, nesting, field types, query/path values, uploads and size boundaries.
It detects unexpected server errors, exceptions, timeouts, traceback disclosure
and successful acceptance of explicitly invalid payloads. It also checks SQLite
integrity and verifies that each included operation was reached.

The container has no network, devices or Docker socket, read-only source/root,
and bounded memory/CPU/processes. All external HTTP services are stubbed;
background pipeline, ingest and inference workers are not started. This does not
touch the running application's database, cameras or mission documents.

Coverage is the frontend-facing application HTTP API, not upstream image
internals. The SSE stream is explicitly excluded. Camera/rule state is populated;
real track/incident/mission corpora, successful upstream inference, concurrent
requests, browser JavaScript, TLS/proxy and HTTP wire parsing need separate tests.
Stub-related 502/503/504 responses are recorded, not counted as successful behavior.

This is a seeded mutation fuzzer, not RESTler. Its bounded results support SDL504
review; they do not prove exhaustive coverage or authorize closing the task.
