# Chat Q&A: resolved model configuration

`config/chatqna/config.yaml`: approved runtime YAML, mounted read-only
at `/tmp/model_config/config.yaml` (`MODEL_CONFIG_PATH`). Replaces
prohibited stock BAAI defaults w/ provenance-approved pair:

- LLM: `Intel/neural-chat-7b-v3-3` (required; owner-directed replacement
  for `microsoft/Phi-3.5-mini-instruct`, 2026-09-14 — fine-tuned from
  `mistralai/Mistral-7B-v0.1`, non-PRC lineage, see `MODEL_PROVENANCE.md`), GPU.
- Embedding: `ibm-granite/granite-embedding-125m-english` (IBM Research AI,
  US; distilled from Mistral-7B-Instruct-v0.2), CPU.
- Reranker: `cross-encoder/ms-marco-electra-base` (base Google ELECTRA,
  Stanford/Google Brain authors), CPU.

Pinned revisions/licenses: `config/chatqna/models.lock.json` and
`MODEL_PROVENANCE.md`. Stock backend (`app/openvino_backend.py`) owns
model download/conversion into `MODEL_CACHE_PATH` via its own HF/Optimum
export path; no custom exporter or config rewriting used.

Owner accepts stock `/chat` for general and document questions.
Retrieval-enabled chat, not clean retrieval bypass: retrieval still runs
against ingested documents. Keep documents intact during general chat.
FAISS corpus state in memory; retain source documents externally for
re-ingestion on restart. Don't fork service or run different LLM.

`PROMPT_TEMPLATE` (nested under `model_settings:` — `app/config.py`'s
YAML loader only reads `model_settings`/`device_settings` keys, a
top-level `PROMPT_TEMPLATE:` key would be silently ignored) overrides the
stock default from `app/prompt.py`, whose system instruction says
"do not make up an answer if there is no context" — this made the model
refuse or hedge on plain general questions unrelated to the ingested
mission documents, since retrieval always runs and always supplies some
(possibly irrelevant) context. `neural-chat-7b-v3-3` (Intel; Mistral-7B
base) has no `<|system|>`/`<|end|>`-style control tokens like Phi-3.5 did;
its documented chat format is a plain
`### System:\n...\n### User:\n...\n### Assistant:\n` block (see model
card "How To Use"), with context folded into the System section since the
model has no dedicated context slot. The override keeps the
required `{context}`/`{question}` placeholders (`app/config.py` rejects a
template missing either), changing only the instruction text: use context
when relevant, otherwise answer from general knowledge without mentioning
the context/documents/mission at all. Verified via
`scripts/smoke/09_chat_general.sh` (general question answered from model
knowledge, mission corpus left intact) alongside
`scripts/smoke/08_chatqna_rag.sh` (document-grounded answers unaffected).

Deployment: `compose/handheld-multi-modal.yaml` service `chatqna`
(`intel/chatqna:core_gpu_2026.2.0`, pinned by digest). `handheld-multi-modal.yaml`
is the single entry point: a build agent (or any fresh environment with no
existing container) runs `docker compose -f compose/handheld-multi-modal.yaml
up -d chatqna` and gets a clean, reproducible deployment — no other setup
step is required. `HF_ACCESS_TOKEN` is read from the caller's environment,
never hardcoded/committed.

Adoption note for this dev environment specifically: this container was
originally started by hand, by name, before it was declared in Compose. A
same-named container already existing causes `docker compose up -d chatqna`
to report a name conflict rather than deploying cleanly, because Compose
won't silently take over/replace a container it didn't create. Model
cache/config are host bind mounts (`../data/chatqna/model_cache`,
`../config/chatqna/config.yaml`), not container-internal state, so nothing
is lost by removing the manually-started container and letting Compose
recreate it under the same name:

```sh
docker rm -f handheld-multi-modal-chatqna-core-gpu   # manually-started container only
docker compose -f compose/handheld-multi-modal.yaml up -d chatqna
```

Corpus caveat: FAISS document state lives in the running process's memory,
not on any bind mount — recreating the container drops ingested documents
and requires re-running the document-upload step (`07_chatqna_ingest.sh`)
against the fresh container.

Upload limits: stock `/documents` accepts PDF, TXT and DOCX with no size
limit and ingests synchronously; it returns HTTP 500 ("No text data from the
document") for image-only files. The Backend enforces its own limits before
forwarding (`mission_max_upload_mb`, `mission_max_pdf_pages`,
`mission_max_docx_uncompressed_mb` in `config/backend/backend.yaml`; out-of-range
values stop Backend startup) and rejects documents with no readable text.
When raising the limits, also raise `mission_ingest_timeout_seconds`.

Known limitation: the stock backend downloads model revision `main` and does
not read `models.lock.json` to pin/enforce a revision — the lock file records
the approved revision for audit only, it is not consumed at runtime. No
patch to backend code or external exporter is added to force enforcement.

Source contracts: `docs/application/ai-contracts.md`.

