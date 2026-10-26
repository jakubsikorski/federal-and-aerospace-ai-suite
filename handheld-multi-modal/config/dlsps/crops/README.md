# Crop producer configuration (D13, D53)

One template, `template.json`, serves every camera. Backend passes the camera's
`stream_id` with each pipeline request (`crop-config` parameter), and the
producer stores that camera's crops in `<output_dir>/<stream_id>`. The producer
takes an exclusive `flock` on that directory's `.lock`, so **two cameras never
share a store** — the second pipeline would fail to start with `Resource
temporarily unavailable`. That lock is also what makes the clear-history
handshake safe (design 9.2, D16). A camera added in the console at runtime
therefore needs no new file here.

`stream_id` is what the callback injects into every observation, so Backend
demultiplexes one shared MQTT topic back to the right camera. It is the
MediaMTX path name (`camN`) and must be 1–64 characters of `[A-Za-z0-9_-]`.

Budgets are **per camera**, and the store is a **rolling window, not an archive**
(D23). Once `store_candidates` (1500) or `store_bytes` (256 MiB) is reached, the
producer evicts the oldest candidates to make room for each new one. Eviction
order comes from an in-memory creation-ordered index, so rotation is a pop and
not a directory scan; `maintain()` rebuilds that index on the walk it already
does every 5 s, which is also how the order is recovered after a restart or a
takeover. Total disk is bounded at 256 MiB x camera count regardless of how long
the demo runs.

An undelivered `.pending` backlog is evicted like anything else if the broker
stays down long enough to fill the store. That is deliberate: keeping stale
undeliverable crops while discarding live evidence is the worse trade.

`retention_seconds` (1 h) expires candidates independently of the quota. It is
deliberately short: crops are evidence for objects seen this sortie, not a day
of archive. The previous 24 h default meant nothing was ever expired during a
demo, so every pipeline restart layered another generation of crops onto the
last — 3985 files and 105 MiB on one camera, measured 2026-09-14.

Regenerate the template from the producer's own defaults rather than editing the
numbers by hand, so they cannot drift from the code:

```sh
python3 scripts/dlsps/crop_candidates.py --print-defaults
```

The template has no `stream_id`; `output_dir` is the store root. Unknown keys
are rejected at startup, never silently ignored.

This template replaces the earlier per-camera `camN.json` files and the single `data/dlsps/crop-config.json`, which is kept only
as the reference defaults.
