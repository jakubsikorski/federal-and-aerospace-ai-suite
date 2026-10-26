# Model provenance and ownership

Initial scaffold downloaded no weights. In authorized continuation, original
required Phi checkpoint was cached at revision
`2fe192450127e6a83f7441aef6e3ca586c338b77`; the pinned LLM was subsequently
replaced with `Intel/neural-chat-7b-v3-3`
(revision `7506dfc5fb325a8a8e0c4f9a6a001671833e5b8e`), resolved and approved
below. No model silently substituted in either case — both were explicit,
recorded decisions. Exact artifact pins in
`config/chatqna/models.lock.json`. Requested identities + completed
downloads are not inference passes.

## Detector

| Field | Finding |
|---|---|
| Role | Object detection |
| Requested identity | Ultralytics YOLO26s (`yolo26s`), exported OpenVINO IR INT8 |
| Publisher/developer | Ultralytics |
| Organization/country evidence | Ultralytics, Inc., Delaware, United States, in publisher legal terms (same publisher as the prior YOLO11s record) |
| Training data | COCO-80 classes; current export metadata describes training on Ultralytics `coco.yaml` and INT8 calibration request `coco8.yaml`. |
| License | AGPL-3.0 in generated Ultralytics metadata; commercial enterprise licensing offered separately |
| Sources | Installed export metadata (`data/dlsps/yolo_export/yolo26s_int8_openvino_model/metadata.yaml`), pinned request (`config/model-download/yolo26s.json`), [legal terms](https://www.ultralytics.com/legal/terms-of-service), stock Model Download/DL Streamer export script commit `4d22f42ffe1df79800dc2ef1749f7e37f3d47bf1` |
| Provenance assessment | Selected to replace YOLO11s. Evidence identifies the original Ultralytics family/authorship and COCO-80 class set; no prohibited parent model is identified in the inspected local metadata. This carries the same acceptance rationale previously applied to YOLO11s, with uncertainty bounded by the stock Ultralytics plugin not exposing an immutable checkpoint revision selector. Image/export-script pins, request path and generated artifact checksums are recorded separately. |
| Consumer/device | DLSPS, NPU default (current); CPU/GPU alternative configs exist but current YOLO26s CPU/GPU runtime passes are not claimed. |
| Pin/compatibility status | Current. INT8 OpenVINO IR exported through stock `intel/model-download:2026.2.0` Ultralytics plugin using Ultralytics 8.4.57/OpenVINO 2026.2.0/NNCF 2.19.0. Frozen artifact hashes from `config/dlsps/detector-profile.json`: `yolo26s.xml` sha256 `a1631d3d8467717acd7eda84c05932e867c5dec866f154593542b7315fb74f28`, `yolo26s.bin` sha256 `f29147d95ef439b1efdd9aa0952dfd24088980dd2e4dd558e8efb9dbf6eadbd1`. Installed receipt: `data/dlsps/yolo_export/yolo26s_int8_openvino_model/download.json`. NPU runtime/tracking was checked with a tracker confidence probe; older YOLO11s NPU/GPU/CPU runs are historical only and are not transferred to YOLO26s. |

The automated path uses the digest-pinned stock downloader and its
pinned DL Streamer export script, Ultralytics 8.4.57/OpenVINO 2026.2.0/NNCF
2.19.0, with the YOLO26s/coco8/INT8 selection. It preserves generated metadata
and artifact hashes. Only the reviewed YOLO26s hash pair above is accepted; a
self-generated receipt never authorizes a changed model. Repeat exports are not
claimed bit-identical unless their hashes match the pinned pair.

Historical record: YOLO11s was the prior approved detector and was
smoke-tested before the YOLO26s switch. Its old hashes (`61e8501f…`/`8bb1f5d1…`, and
a later fresh-demo pair `fe835c8f…`/`5ece73c…`) and execution evidence
remain historical YOLO11s evidence only. They do not establish current YOLO26s
execution or provenance.

Publisher nationality alone doesn't certify every derivative's lineage. The
provenance policy requires resolving uncertainty before use. YOLO26s is the
fixed detector; no alternate detector is selected without provenance
approval, and never substitute nano. Don't confuse generic YOLO branding with
evidence about a particular checkpoint, fine-tune, or derivative.

## Other model roles

| Role / exact ID | Publisher / institutional provenance | License evidence | Consumer / required device | Assessment |
|---|---|---|---|---|
| LLM: `Intel/neural-chat-7b-v3-3` (replaces `microsoft/Phi-3.5-mini-instruct`) | Intel (NeuralChat team, DCAI/AISE/AIPT), US organization; fine-tuned from `Intel/neural-chat-7b-v3-1`, itself fine-tuned from `mistralai/Mistral-7B-v0.1` (Mistral AI, France); DPO-aligned on `Intel/orca_dpo_pairs`; SFT on `meta-math/MetaMathQA` (meta-math, US) | Apache-2.0, explicit on model card | Chat Q&A / GPU (deployed and smoke-tested) | Approved: full lineage traced (Intel → Intel → Mistral AI base, US/France training data/teachers), no PRC-trained family or derivative identified; revision `7506dfc5fb325a8a8e0c4f9a6a001671833e5b8e` pinned; superseded original `microsoft/Phi-3.5-mini-instruct` pin, which had also passed provenance (MIT, Microsoft) but was replaced by explicit decision, not a compatibility failure. Redeployed: stock backend downloaded+converted the model itself (no custom exporter), `GET /model` confirms `llm_model: "Intel/neural-chat-7b-v3-3"`, `LLM_DEVICE: GPU`. Mission RAG and general-chat smoke tests re-run and pass against the new model |
| STT: `openai/whisper-medium` | OpenAI, US organization; original multilingual Whisper checkpoint | Card metadata Apache-2.0; do not confuse with repository code licensing | Audio Analyzer / OpenVINO GPU | Deployed and smoke-tested on an Intel Core Ultra (Series 2) GPU: real `optimum-cli export openvino` at container startup, `device=GPU` load confirmed, real transcription of a spoken-philosophy WAV clip returned recognizable non-empty text |
| Embedding stock default: `BAAI/bge-small-en-v1.5` | Beijing Academy of Artificial Intelligence, PRC | MIT model-card metadata | Chat Q&A / stock CPU; no approved final device | Rejected: prohibited PRC model family |
| Reranker stock default: `BAAI/bge-reranker-base` | Beijing Academy of Artificial Intelligence, PRC | MIT model-card metadata | Chat Q&A / stock CPU; no approved final device | Rejected: prohibited PRC model family |
| **Embedding, selected: `ibm-granite/granite-embedding-125m-english`** | IBM Research AI, US organization. Base "WatBERT" (RoBERTa-like) trained internally at IBM. Fine-tuned via knowledge distillation from `mistralai/Mistral-7B-Instruct-v0.2` (Mistral AI, France). Multilingual-variant synthetic query generation in the same technical report used `mistralai/Mixtral-8x22B-Instruct-v0.1` and `microsoft/Phi-3.5-MoE-instruct` (both non-PRC); no PRC-origin teacher/dataset identified for the English model. | Apache-2.0, explicit on model card | Chat Q&A / CPU (selected) | Approved: no PRC-trained family or derivative identified in primary technical report (arXiv:2502.20204v1); revision `4ab61ffd423be45cd932b21a7c696063d82bf45f` pinned |
| **Reranker, selected: `cross-encoder/ms-marco-electra-base`** | Base `google/electra-base-discriminator`; ELECTRA authors Kevin Clark, Christopher Manning (Stanford), Minh-Thang Luong, Quoc Le (Google Brain) — US institutions. Fine-tuned on MS MARCO by Sentence-Transformers/UKPLab (Nils Reimers). | Apache-2.0, explicit on model card | Chat Q&A / CPU (selected) | Approved: documented non-PRC institutional lineage; revision `8b9301757513f8a695920c334c3a9f365ebeb357` pinned |
| TTS deployed model: `microsoft/speecht5_tts` | Original paper identifies Microsoft Research Asia internship work and PRC university affiliations (institutional provenance flag) | MIT model card | OEP TTS / GPU (deployed) | **Approved override**: this institutional-provenance finding was reviewed directly, SpeechT5 was confirmed as a Microsoft product, not PRC-origin, and deployment was explicitly approved despite the flag. Deployed and smoke-tested (real GPU synthesis). Chosen over Parler because Parler is blocked by a real packaging gap in the pinned image (see below), not because SpeechT5's provenance flag was independently resolved |
| SpeechT5 vocoder: `microsoft/speecht5_hifigan` | SpeechT5 dependency, same Microsoft-origin disposition | Not separately cleared | OEP TTS / deployed | Approved under the same override as the base model |
| SpeechT5 speaker vectors: `Matthijs/cmu-arctic-xvectors` | CMU Arctic vectors repackaged as stock named voices (Ryan/Miles/Aaron/Nora/Elena/Kabir/Angus), confirmed live via deployed `GET /v1/audio/voices` | Not separately cleared | OEP TTS / deployed | Approved under the same override as the base model |
| TTS alternative: `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | Qwen/Alibaba, PRC family | Not reviewed; excluded before use | OEP TTS / unselected | Prohibited |
| TTS provenance-approved but undeployed: `parler-tts/parler-tts-mini-v1` | Hugging Face Parler; reproduces Stability AI / University of Edinburgh work | Apache-2.0 card | Stock OEP TTS / GPU-accelerated with CPU codec stage | **Provenance approved**, but blocked from actual deployment by a packaging gap in the pinned `2026.2.0` image (missing `parler-tts` pip dependency, confirmed via failed deploy + direct import test). SpeechT5 was deployed instead per the decision above |
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
pins.

**Embedding/reranker pair selected and pinned.**
`ibm-granite/granite-embedding-125m-english` and
`cross-encoder/ms-marco-electra-base` replace prohibited stock BAAI defaults
in `config/chatqna/config.yaml` and `config/chatqna/models.lock.json`.
Granite's own usage example already applies CLS pooling w/ L2 normalization,
matching stock `OpenVINOBgeEmbeddings` wrapper (`mean_pooling=False`,
`normalize_embeddings=True`); wrapper's added query-search instruction
prefix is a retrieval-quality consideration validated by smoke test, not a
pooling/license blocker. General conversation goes through stock
`/chat` with retrieval allowed; absence of separate retrieval-bypass
endpoint is not a current blocker. Research alternatives considered, not
selected: `sentence-transformers/multi-qa-distilbert-dot-v1` (undeclared
checkpoint license), `Snowflake/snowflake-arctic-embed-m` (intermediate
`e5-unsupervised-base` ancestor traced to Microsoft Research Asia
authorship), `mixedbread-ai/mxbai-embed-large-v1` (base checkpoint/teacher
lineage undocumented in primary sources).

## Rejected defaults and examples

Qwen, DeepSeek, GLM, Yi, InternLM, MiniCPM, incl. derivatives, prohibited by
provenance policy. Official copied skills include generic examples referencing
prohibited families. Retained as upstream documentation, not selected,
downloaded, or executed models.

DL Streamer safety-compliance sample includes a Qwen VLM. Not adopted; its
crop implementation was inspected only to find a supported in-pipeline
extension point.

## Model ownership

Installed cache paths and consumers are recorded below. Stock speech exporters
do not enforce a revision argument, so a later cold stock export is not
guaranteed to resolve the same upstream revision.

| Role | Model | Download owner | Cache/volume | Consumer |
|---|---|---|---|---|
| Detection | Required YOLO26s INT8/coco8 (replacement for YOLO11s) | Stock Model Download Ultralytics plugin, invoked by `make demo-prereq` | `ASSET_ROOT/data/model-download`; validated output installed under `data/dlsps/yolo_export/yolo26s_int8_openvino_model`, with `download.json` provenance/checksums | DLSPS |
| LLM | Required `Intel/neural-chat-7b-v3-3` (replacement for `microsoft/Phi-3.5-mini-instruct`) | Chat Q&A backend HF/Optimum export (`app/openvino_backend.py`) | Stock `/tmp/model_cache/Intel/neural-chat-7b-v3-3` and compile cache; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO GPU |
| Embedding | Selected `ibm-granite/granite-embedding-125m-english` | Chat Q&A backend HF/Optimum export | Stock `/tmp/model_cache/ibm-granite/granite-embedding-125m-english`; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO CPU |
| Reranker | Selected `cross-encoder/ms-marco-electra-base` | Chat Q&A backend HF/Optimum export | Stock `/tmp/model_cache/cross-encoder/ms-marco-electra-base`; host volume `data/chatqna/model_cache` | Chat Q&A OpenVINO CPU |
| STT | `openai/whisper-medium`, service name `whisper-medium` | Audio Analyzer `utils/ensure_model.py` | Stock `/app/audio_analyzer/models/openvino/whisper-medium`; HF cache `/app/audio_analyzer/.cache/huggingface` | Audio Analyzer OpenVINO GPU |
| TTS | Approved `microsoft/speecht5_tts` | Stock OEP TTS ensure/export helpers | `/app/text-to-speech/models/openvino/microsoft_speecht5_tts__fp16`; source cache `/app/text-to-speech/.cache/huggingface` | OEP TTS GPU |
| Wake word | Not selected | None | None | None |

Official `model-download-user` skill documents separate Model Download
service. Its availability isn't evidence every component uses its cache.
No Hugging Face token belongs in this table, a config file, a lockfile, or
logs.
