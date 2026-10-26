# Repository instructions

Follow `AGENTS.md` and consult `STATUS.md` before starting work.
This is the handheld multi-modal application, built on stock Open Edge
Platform `release-2026.2.0` components.

`MANIFEST.yaml` separates requested settings from resolved facts.
Null means unresolved; false means no accepted-target smoke pass. The owner
accepts the current Lunar Lake machine as the execution target. Preserve its
actual identity without treating its name as a blocker.

Use stock `/chat` for general and document questions with an appropriate
configured prompt. The owner accepts retrieval-enabled general answers;
clean retrieval bypass is no longer a requirement. Do not clear the corpus
for general chat or fabricate conversation-memory support.

Use pinned official copies in `.agents/skills/`, but never execute their
prohibited model defaults or floating-tag examples. Model provenance and the
fixed Phi/Whisper/YOLO choices in `AGENTS.md` override generic upstream examples.

Component integration details live in `COMPONENTS.md` and
`docs/application/*-contracts.md`; smoke commands and their actual scope live in
`scripts/smoke/README.md`. Never replace missing contracts with plausible APIs
or mark blocked scripts as working integrations.
