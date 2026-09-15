# Model provenance and ownership

Initial scaffold downloaded no weights. In authorized continuation, original
required Phi checkpoint was cached at revision
`2fe192450127e6a83f7441aef6e3ca586c338b77`; the owner subsequently directed
(2026-09-14) a replacement of the pinned LLM with `Intel/neural-chat-7b-v3-3`
(revision `7506dfc5fb325a8a8e0c4f9a6a001671833e5b8e`), resolved and approved
below. No model silently substituted in either case — both were explicit,
recorded decisions. Exact artifact pins in
`config/chatqna/models.lock.json`. Requested identities + completed
downloads are not inference passes.

## Detector

| Field | Finding |
|---|---|
| Role | Object detection |
| Requested identity | Ultralytics YOLO11s (`yolo11s.pt`), exported OpenVINO IR |
| Publisher/developer | Ultralytics |
| Organization/country evidence | Ultralytics, Inc., Delaware, United States, in publisher legal terms |
| License | AGPL-3.0 model card; commercial enterprise licensing offered separately |
| Sources | [Publisher model card](https://huggingface.co/Ultralytics/YOLO11), [legal terms](https://www.ultralytics.com/legal/terms-of-service), [OEP YOLO11s integration](https://github.com/open-edge-platform/edge-ai-libraries/blob/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/dlstreamer-pipeline-server/docs/user-guide/how-to-guides/download-and-run-yolo-models.md) |
| Provenance assessment | Evidence identifies the original Ultralytics family and US publisher; no prohibited parent model is identified in the inspected card. Exact checkpoint revision/lineage has not been frozen; execution approval remains unresolved. |
| Consumer/device | DLSPS, NPU default (deployed); CPU/GPU alternative variants (deployed) |
| Pin/compatibility status | Resolved. INT8 OpenVINO IR exported via `ultralytics`+`openvino==2025.0.0`+`nncf` PTQ (prior session). Frozen artifact hashes: `yolo11s.xml` sha256 `61e8501fa5a11fa012921d5f7c65f0ff05fa78f2d2bf0a37fc1fe7fdf8630d45`, `yolo11s.bin` sha256 `8bb1f5d1b46de8c09fe26d0379072cfddf847e4d627696ccd59374b4a623d51d`. Loaded and executed on NPU/GPU/CPU in `intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24-rc3`; NPU run confirmed real ~30fps detection+tracking via `gvafpscounter`. A cosmetic runtime-version-mismatch warning appears (exporter openvino 2025.0.0 vs. pipeline-server runtime 2026.2.0) with no observed functional effect. |

Publisher nationality alone doesn't certify every derivative's lineage. Owner
provenance policy requires resolving uncertainty before use. YOLO11s fixed —
no alternate detector selected without owner approval. Don't confuse generic
YOLO branding with evidence about a particular checkpoint, fine-tune, or
derivative.

## Other model roles

| Role / exact ID | Publisher / institutional provenance | License evidence | Consumer / required device | Assessment |
|---|---|---|---|---|
| LLM: `Intel/neural-chat-7b-v3-3` (replaces `microsoft/Phi-3.5-mini-instruct`, owner-directed 2026-09-14) | Intel (NeuralChat team, DCAI/AISE/AIPT), US organization; fine-tuned from `Intel/neural-chat-7b-v3-1`, itself fine-tuned from `mistralai/Mistral-7B-v0.1` (Mistral AI, France); DPO-aligned on `Intel/orca_dpo_pairs`; SFT on `meta-math/MetaMathQA` (meta-math, US) | Apache-2.0, explicit on model card | Chat Q&A / GPU (deployed and smoke-tested) | Approved: full lineage traced (Intel → Intel → Mistral AI base, US/France training data/teachers), no PRC-trained family or derivative identified; revision `7506dfc5fb325a8a8e0c4f9a6a001671833e5b8e` pinned; superseded original `microsoft/Phi-3.5-mini-instruct` pin, which had also passed provenance (MIT, Microsoft) but was replaced per explicit owner direction, not a compatibility failure. Redeployed 2026-09-14: stock backend downloaded+converted the model itself (no custom exporter), `GET /model` confirms `llm_model: "Intel/neural-chat-7b-v3-3"`, `LLM_DEVICE: GPU`. Mission RAG and general-chat smoke tests re-run and PASS against the new model — evidence: `docs/bootstrap/evidence/chatqna/smoke/rag.json`, `docs/bootstrap/evidence/chatqna/smoke/general.json` |
| STT: `openai/whisper-medium` | OpenAI, US organization; original multilingual Whisper checkpoint | Card metadata Apache-2.0; do not confuse with repository code licensing | Audio Analyzer / OpenVINO GPU | Deployed and smoke-tested on Lunar Lake GPU: real `optimum-cli export openvino` at container startup, `device=GPU` load confirmed, real transcription of a spoken-philosophy WAV clip returned recognizable non-empty text. Evidence: `docs/bootstrap/evidence/audio-analyzer/smoke.log` |
| Embedding stock default: `BAAI/bge-small-en-v1.5` | Beijing Academy of Artificial Intelligence, PRC | MIT model-card metadata | Chat Q&A / stock CPU; no approved final device | Rejected: prohibited PRC model family |
| Reranker stock default: `BAAI/bge-reranker-base` | Beijing Academy of Artificial Intelligence, PRC | MIT model-card metadata | Chat Q&A / stock CPU; no approved final device | Rejected: prohibited PRC model family |
| **Embedding, selected: `ibm-granite/granite-embedding-125m-english`** | IBM Research AI, US organization. Base "WatBERT" (RoBERTa-like) trained internally at IBM. Fine-tuned via knowledge distillation from `mistralai/Mistral-7B-Instruct-v0.2` (Mistral AI, France). Multilingual-variant synthetic query generation in the same technical report used `mistralai/Mixtral-8x22B-Instruct-v0.1` and `microsoft/Phi-3.5-MoE-instruct` (both non-PRC); no PRC-origin teacher/dataset identified for the English model. | Apache-2.0, explicit on model card | Chat Q&A / CPU (selected) | Approved: no PRC-trained family or derivative identified in primary technical report (arXiv:2502.20204v1); revision `4ab61ffd423be45cd932b21a7c696063d82bf45f` pinned |
| **Reranker, selected: `cross-encoder/ms-marco-electra-base`** | Base `google/electra-base-discriminator`; ELECTRA authors Kevin Clark, Christopher Manning (Stanford), Minh-Thang Luong, Quoc Le (Google Brain) — US institutions. Fine-tuned on MS MARCO by Sentence-Transformers/UKPLab (Nils Reimers). | Apache-2.0, explicit on model card | Chat Q&A / CPU (selected) | Approved: documented non-PRC institutional lineage; revision `8b9301757513f8a695920c334c3a9f365ebeb357` pinned |
| TTS deployed model: `microsoft/speecht5_tts` | Original paper identifies Microsoft Research Asia internship work and PRC university affiliations (institutional provenance flag) | MIT model card | OEP TTS / GPU (deployed) | **Owner-approved override (2026-09-10)**: owner reviewed this institutional-provenance finding directly and confirmed SpeechT5 is a Microsoft product, not PRC-origin, explicitly approving deployment despite the flag. Deployed and smoke-tested (real GPU synthesis, `docs/bootstrap/evidence/tts/smoke.log`). Chosen over Parler because Parler is blocked by a real packaging gap in the pinned image (see below), not because SpeechT5's provenance flag was independently resolved |
| SpeechT5 vocoder: `microsoft/speecht5_hifigan` | SpeechT5 dependency, same Microsoft-origin disposition | Not separately cleared | OEP TTS / deployed | Approved under the same owner override as the base model |
| SpeechT5 speaker vectors: `Matthijs/cmu-arctic-xvectors` | CMU Arctic vectors repackaged as stock named voices (Ryan/Miles/Aaron/Nora/Elena/Kabir/Angus), confirmed live via deployed `GET /v1/audio/voices` | Not separately cleared | OEP TTS / deployed | Approved under the same owner override as the base model |
| TTS alternative: `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | Qwen/Alibaba, PRC family | Not reviewed; excluded before use | OEP TTS / unselected | Prohibited |
| TTS provenance-approved but undeployed: `parler-tts/parler-tts-mini-v1` | Hugging Face Parler; reproduces Stability AI / University of Edinburgh work | Apache-2.0 card | Stock OEP TTS / GPU-accelerated with CPU codec stage | **Provenance approved**, but blocked from actual deployment by a packaging gap in the pinned `2026.2.0-rc3` image (missing `parler-tts` pip dependency, confirmed via failed deploy + direct import test). SpeechT5 was deployed instead per owner decision above |
| Parler text encoder: `google/flan-t5-large` | Google, US; T5/FLAN lineage | Apache-2.0 | TTS dependency / GPU stage | Approved: US-origin, no PRC involvement |
| Parler codec: `parler-tts/dac_44khZ_8kbps` | Wrapper around Descript's Descript Audio Codec (Descript, US company) | MIT license | TTS dependency / CPU stage | Approved: US-origin, no PRC involvement |
| Parler synthetic-annotation teacher: `mistralai/Mistral-7B-Instruct-v0.2` | Mistral AI, France; used by upstream `dataspeech` pipeline to generate the natural-language speaker/style descriptions in Parler's training datasets | Apache-2.0 | Training-time dependency only, not deployed | Approved: confirmed via `dataspeech` README's documented `run_prompt_creation.py --model_name_or_path` invocation; closes the last open Parler provenance question |

Primary evidence:
[neural-chat-7b-v3-3 card](https://huggingface.co/Intel/neural-chat-7b-v3-3/blob/main/README.md),
[Phi card (superseded pin)](https://huggingface.co/microsoft/Phi-3.5-mini-instruct/blob/main/README.md),
[Whisper card](https://huggingface.co/openai/whisper-medium/blob/main/README.md),
[BGE embedding card](https://huggingface.co/BAAI/bge-small-en-v1.5/blob/main/README.md),
[BGE reranker card](https://huggingface.co/BAAI/bge-reranker-base/blob/main/README.md),
[Granite embedding card](https://huggingface.co/ibm-granite/granite-embedding-125m-english/blob/main/README.md),
[Granite Embedding Models technical report](https://arxiv.org/abs/2502.20204),
[ELECTRA reranker card](https://huggingface.co/cross-encoder/ms-marco-electra-base/blob/main/README.md),
[ELECTRA paper](https://arxiv.org/html/2003.10555v1),
[SpeechT5 card](https://huggingface.co/microsoft/speecht5_tts/blob/main/README.md),
[SpeechT5 original paper v3](https://arxiv.org/html/2110.07205v3),
[Parler card](https://huggingface.co/parler-tts/parler-tts-mini-v1/blob/main/README.md),
[Parler dependency configuration](https://huggingface.co/parler-tts/parler-tts-mini-v1/blob/main/config.json),
[Descript DAC codec card](https://huggingface.co/parler-tts/dac_44khZ_8kbps/blob/main/README.md),
[dataspeech pipeline README](https://github.com/huggingface/dataspeech) (documents the
`mistralai/Mistral-7B-Instruct-v0.2` annotation-generation step).
These online model-card links are research evidence, not immutable weight
pins. See `docs/bootstrap/ai-contracts.md` for exact stock source paths.

**Embedding/reranker pair selected and pinned.**
`ibm-granite/granite-embedding-125m-english` and
`cross-encoder/ms-marco-electra-base` replace prohibited stock BAAI defaults
in `config/chatqna/config.yaml` and `config/chatqna/models.lock.json`.
Granite's own usage example already applies CLS pooling w/ L2 normalization,
matching stock `OpenVINOBgeEmbeddings` wrapper (`mean_pooling=False`,
`normalize_embeddings=True`); wrapper's added query-search instruction
prefix is a retrieval-quality consideration validated by smoke test, not a
pooling/license blocker. Owner accepts general conversation through stock
`/chat` with retrieval allowed; absence of separate retrieval-bypass
endpoint is not a current blocker. Research alternatives considered, not
selected: `sentence-transformers/multi-qa-distilbert-dot-v1` (undeclared
checkpoint license), `Snowflake/snowflake-arctic-embed-m` (intermediate
`e5-unsupervised-base` ancestor traced to Microsoft Research Asia
authorship), `mixedbread-ai/mxbai-embed-large-v1` (base checkpoint/teacher
lineage undocumented in primary sources).

## Rejected defaults and examples

Qwen, DeepSeek, GLM, Yi, InternLM, MiniCPM, incl. derivatives, prohibited by
owner policy. Official copied skills include generic examples referencing
prohibited families. Retained as upstream documentation, not selected,
downloaded, or executed models.

DL Streamer safety-compliance sample includes a Qwen VLM. Not adopted; its
crop implementation was inspected only to find a supported in-pipeline
extension point.

## Model ownership

Actual installed cache paths + model consumers stay unresolved until
compliant stock deployment selected. `null` in manifest = unknown, not a
path to create or setting to infer.

| Role | Model | Download owner | Cache/volume | Consumer |
|---|---|---|---|---|
| Detection | Required YOLO11s | OEP DLSPS tutorial uses an external Ultralytics export; not a DLSPS startup download | Tutorial mount `/home/pipeline-server/yolo_models/yolo11s`; repository export/cache not selected | DLSPS |
| LLM | Required `Intel/neural-chat-7b-v3-3` (owner-directed replacement for `microsoft/Phi-3.5-mini-instruct`) | Chat Q&A backend HF/Optimum export (`app/openvino_backend.py`) | Stock `/tmp/model_cache/Intel/neural-chat-7b-v3-3` and compile cache; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO GPU |
| Embedding | Selected `ibm-granite/granite-embedding-125m-english` | Chat Q&A backend HF/Optimum export | Stock `/tmp/model_cache/ibm-granite/granite-embedding-125m-english`; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO CPU |
| Reranker | Selected `cross-encoder/ms-marco-electra-base` | Chat Q&A backend HF/Optimum export | Stock `/tmp/model_cache/cross-encoder/ms-marco-electra-base`; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO CPU |
| STT | `openai/whisper-medium`, service name `whisper-medium` | Audio Analyzer `utils/ensure_model.py` | Stock `/app/audio_analyzer/models/openvino/whisper-medium`; HF cache `/app/audio_analyzer/.cache/huggingface` | Audio Analyzer OpenVINO GPU |
| TTS | Unresolved compliant stock selection | OEP TTS ensure/export helpers | Stock `/app/text-to-speech/models` and `.cache/huggingface`; concrete model export path depends on unselected model/runtime | OEP TTS GPU |
| Wake word | Not selected | None | None | None |

Official `model-download-user` skill documents separate Model Download
service. Its availability isn't evidence every component uses its cache.
No Hugging Face token belongs in this table, the manifest, a lockfile, or
logs.
