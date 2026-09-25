"""Audio arbiter (design section 16.4): the only producer of TTS requests.

One utterance at a time. Backend decides what is "current"; the UI only plays
whatever the arbiter reports and stops when that changes, so two clips can never
be scheduled to overlap. An utterance is "spoken" only when a browser reports
that playback ended (`ack`). Without that report the slot ends after the WAV's
own duration plus a margin and is recorded as "unconfirmed", never as heard.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
import uuid
import wave
from collections import OrderedDict, deque
from collections.abc import Callable

log = logging.getLogger("backend.speech")

ALERT, ANSWER, NOTICE = 0, 1, 2
KINDS = {"answer": ANSWER, "alert": ALERT, "notice": NOTICE}
# Measured on the pinned SpeechT5 service: ~0.6 s per word up to ~35 words,
# while ~60-word inputs never hit the stop criterion and hold the service's
# single inference lock for minutes, starving every utterance behind them.
# The full answer text is always displayed; only the spoken part is bounded.
WORD_LIMITS = {ANSWER: 30, ALERT: 30, NOTICE: 30}
# The browser starts playback up to one SSE tick after the arbiter does, so the
# slot is held a little past the audio. A "started" ack restarts the window, so
# a late start is not cut off; "ended" frees the slot at once.
PLAYBACK_MARGIN_SECONDS = 1.5
PLAYBACK_EVENTS = ("started", "ended", "error")
ANSWER_CHUNK_WORDS = 12
MIN_TAIL_WORDS = 4
_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "e.g.", "i.e.", "etc.", "vs.", "u.s.", "u.k.",
}


def bound_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    kept = words[:limit]
    # Prefer ending on a whole sentence when that keeps at least half the words.
    for end in range(limit - 1, limit // 2 - 1, -1):
        if kept[end][-1] in ".!?":
            return " ".join(kept[:end + 1])
    return " ".join(kept) + "…"


def _sentence_boundary(token: str) -> bool:
    token = token.strip('"\')]}')
    lower = token.lower()
    if token.endswith(("!", "?")):
        return True
    if not token.endswith("."):
        return False
    base = token.rstrip(".")
    return not (
        lower in _ABBREVIATIONS
        or re.fullmatch(r"\d+", base)
        or re.fullmatch(r"\d+\.\d+", base)
        or re.fullmatch(r"[A-Z]", base)
    )


def _answer_sentences(text: str) -> list[str]:
    sentences, words = [], []
    for word in text.split():
        words.append(word)
        if _sentence_boundary(word):
            sentences.append(" ".join(words))
            words = []
    if words:
        sentences.append(" ".join(words))
    return sentences


def complete_answer_sentences(text: str) -> tuple[list[str], str]:
    sentences, start = [], 0
    for match in re.finditer(r"(\S+)(\s+)", text):
        if _sentence_boundary(match.group(1)):
            sentences.append(text[start:match.end()].strip())
            start = match.end()
    tail = text[start:]
    if tail and _sentence_boundary(tail.split()[-1]):
        sentences.append(tail.strip())
        tail = ""
    return sentences, tail


def _append_chunk(chunks: list[str], words: list[str]) -> None:
    if not words:
        return
    if len(words) < MIN_TAIL_WORDS and chunks:
        previous = chunks[-1].split()
        if len(previous) + len(words) <= ANSWER_CHUNK_WORDS:
            chunks[-1] = " ".join(previous + words)
            return
        move = MIN_TAIL_WORDS - len(words)
        if len(previous) - move >= MIN_TAIL_WORDS:
            chunks[-1] = " ".join(previous[:-move])
            words = previous[-move:] + words
    chunks.append(" ".join(words))


def answer_chunks(text: str) -> list[str]:
    # Keep every word, preferring sentences and then clause boundaries. Never
    # send SpeechT5 the long inputs known to stall its single inference lock.
    chunks = []
    for sentence in _answer_sentences(text.strip()):
        sentence_chunks = []
        words = sentence.split()
        while len(words) > ANSWER_CHUNK_WORDS:
            end = ANSWER_CHUNK_WORDS
            if len(words) > ANSWER_CHUNK_WORDS:
                for i in range(11, 3, -1):
                    if words[i].endswith((',', ';', ':')) and len(words) - (i + 1) >= MIN_TAIL_WORDS:
                        end = i + 1
                        break
            sentence_chunks.append(" ".join(words[:end]))
            words = words[end:]
        _append_chunk(sentence_chunks, words)
        chunks.extend(sentence_chunks)
    return chunks


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as clip:
        return clip.getnframes() / float(clip.getframerate() or 1)


class AudioCache:
    """Tiny byte-bounded LRU for successful TTS WAVs."""

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_entries: int = 256):
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(0, int(max_entries))
        self.bytes = 0
        self.items: OrderedDict[tuple[str, str], bytes] = OrderedDict()

    def get(self, key: tuple[str, str]) -> bytes | None:
        data = self.items.get(key)
        if data is not None:
            self.items.move_to_end(key)
        return data

    def put(self, key: tuple[str, str], data: bytes) -> None:
        if not self.max_bytes or not self.max_entries or len(data) > self.max_bytes:
            return
        old = self.items.pop(key, None)
        if old is not None:
            self.bytes -= len(old)
        self.items[key] = data
        self.bytes += len(data)
        while self.bytes > self.max_bytes or len(self.items) > self.max_entries:
            _, evicted = self.items.popitem(last=False)
            self.bytes -= len(evicted)


class Arbiter:
    def __init__(self, synthesize, *, alert_floor: float = 3.0, queue_limit: int = 8,
                 trace_path: str | None = None, clock=time.monotonic,
                 cache_bytes: int = 32 * 1024 * 1024, cache_entries: int = 256,
                 cache_identity: str | Callable[[], str] | None = None):
        self.synthesize = synthesize
        self.alert_floor = alert_floor
        self.queue_limit = queue_limit
        self.trace_path = trace_path
        self.clock = clock
        self.cond = threading.Condition()
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.talk_held = False
        self.talk_deadline = 0.0
        self.answer_generation = 0
        self.seq = 0
        self.version = 0
        self.last_alert_end = float("-inf")
        self.rule_spoken: dict[str, float] = {}
        # ref (e.g. match_id) -> latest event, so the alert feed can say what happened.
        self.outcomes: OrderedDict[str, str] = OrderedDict()
        self.trace: deque = deque(maxlen=500)
        self.tts_ok: bool | None = None
        self.last: dict | None = None
        self.preparing: dict | None = None
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        self.audio_cache = AudioCache(max_bytes=cache_bytes, max_entries=cache_entries)
        self.cache_identity = cache_identity if cache_identity is not None else getattr(
            synthesize, "cache_identity", "unknown")

    # -- producers ---------------------------------------------------------
    def say(self, kind: str, text: str, *, ref: str | None = None,
            rule_id: str | None = None, cooldown: float = 0.0, group: str | None = None,
            bypass_alert_floor: bool = False) -> dict:
        priority = KINDS[kind]
        text = bound_words(" ".join(str(text).split()), WORD_LIMITS[priority])
        with self.cond:
            self.seq += 1
            item = {"id": uuid.uuid4().hex[:12], "seq": self.seq, "kind": kind,
                    "priority": priority, "text": text, "ref": ref, "rule_id": rule_id,
                    "group": group, "bypass_alert_floor": bool(bypass_alert_floor)}
            now = self.clock()
            if rule_id is not None:
                last = self.rule_spoken.get(rule_id)
                if last is not None and now - last < cooldown:
                    self._finish(item, "rate_limited")
                    return item
                self.rule_spoken[rule_id] = now
            self.queue.append(item)
            self._event("queued", item)
            if len(self.queue) > self.queue_limit:
                # Oldest item of the least urgent class goes first.
                victim = max(self.queue, key=lambda i: (i["priority"], -i["seq"]))
                self.queue.remove(victim)
                self._finish(victim, "dropped_queue_full")
            self.cond.notify_all()
            return item

    def say_answer(self, text: str, generation: int) -> dict | None:
        chunks = answer_chunks(text)
        with self.cond:
            if not chunks or generation != self.answer_generation or self.talk_held:
                return None
            existing = self._append_answer_chunks(chunks, generation)
            if existing is not None:
                return existing
            item = self.say("answer", chunks[0], group="answer")
            item["answer_generation"] = generation
            item["remaining"] = chunks[1:]
            return item

    def stop_speaking(self) -> None:
        with self.cond:
            self.answer_generation += 1
            for item in self.queue:
                self._finish(item, "cancelled")
            self.queue.clear()
            for item in (self.current, self.preparing):
                if item is not None:
                    item["cancelled"] = "cancelled"
            self.cond.notify_all()

    def cancel(self, *, rule_id: str | None = None, group: str | None = None,
               reason: str = "cancelled") -> int:
        """Withdraw speech for a rule or incident the operator silenced.

        Queued items are dropped, an item being synthesized is discarded when
        synthesis returns, and the current one stops (the UI stops on change).
        """
        def hit(item):
            return item is not None and (
                (rule_id is not None and item.get("rule_id") == rule_id)
                or (group is not None and item.get("group") == group))
        if rule_id is None and group is None:
            return 0
        with self.cond:
            dropped = [i for i in self.queue if hit(i)]
            for item in dropped:
                self.queue.remove(item)
                self._finish(item, reason)
            for item in (self.preparing, self.current):
                if hit(item):
                    item["cancelled"] = reason
                    dropped.append(item)
            if dropped:
                self.cond.notify_all()
            return len(dropped)

    def ack(self, speech_id: str, event: str) -> bool:
        """Browser playback report for the current utterance."""
        if event not in PLAYBACK_EVENTS:
            raise ValueError("event must be one of " + ", ".join(PLAYBACK_EVENTS))
        with self.cond:
            if self.current is None or self.current["id"] != speech_id:
                return False
            self.current.setdefault("acks", []).append(event)
            self._event("playback_" + event, self.current)
            self.cond.notify_all()
            return True

    def set_talk(self, held: bool) -> None:
        with self.cond:
            self.talk_deadline = self.clock() + 25 if held else 0
            if held:
                self.answer_generation += 1
                self.cancel(group="answer")
            if self.talk_held != held:
                self.talk_held = bool(held)
                self._event("talk_held" if held else "talk_released", None)
                self.cond.notify_all()

    # -- consumers ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self.cond:
            current = self.current
            return {
                "version": self.version,
                "talk_held": self.talk_held,
                "tts_ok": self.tts_ok,
                "last": self.last,
                "preparing": None if self.preparing is None else self.preparing["text"],
                "preparing_kind": self.preparing["kind"] if self.preparing else None,
                "preparing_cancelled": bool(self.preparing and self.preparing.get("cancelled")),
                "current": None if current is None else {
                    key: current.get(key) for key in
                    ("id", "kind", "priority", "text", "ref", "mode", "duration_s", "cancelled")
                } | {"cached": current.get("cached", False),
                     "audio_base64": (current.get("audio_base64")
                                      if current.get("mode") == "audio" else None)},
                "queued": [{"id": i["id"], "kind": i["kind"], "text": i["text"]}
                           for i in sorted(self.queue, key=lambda i: (i["priority"], i["seq"]))],
            }


    def outcome(self, ref: str) -> str | None:
        with self.cond:
            return self.outcomes.get(ref)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.worker = threading.Thread(target=self._run, name="audio-arbiter", daemon=True)
        self.worker.start()

    def close(self) -> None:
        self.stopping.set()
        with self.cond:
            self.cond.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=5.0)

    # -- internals (caller holds self.cond) ---------------------------------
    def _event(self, event: str, item: dict | None, **extra) -> None:
        self.version += 1
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                 "mono": round(self.clock(), 3), "event": event, **extra}
        if item is not None:
            entry.update({k: item.get(k) for k in ("id", "kind", "priority", "ref", "rule_id")})
            entry["text"] = item.get("text")
            if item.get("ref"):
                self.outcomes[item["ref"]] = event
                self.outcomes.move_to_end(item["ref"])
                while len(self.outcomes) > 500:
                    self.outcomes.popitem(last=False)
        self.trace.append(entry)
        if self.trace_path:
            try:
                with open(self.trace_path, "a") as handle:
                    handle.write(json.dumps(entry) + "\n")
            except OSError:
                log.warning("arbiter trace not writable at %s", self.trace_path)

    def _finish(self, item: dict, outcome: str) -> None:
        self.last = {"id": item["id"], "kind": item["kind"], "text": item["text"],
                     "outcome": outcome, "error": item.get("error"),
                     "cached": item.get("cached", False)}
        self._event(outcome, item)

    def _next(self) -> dict | None:
        if self.talk_held and self.clock() >= self.talk_deadline:
            self.talk_held = False
            self._event("talk_expired", None)
        if self.talk_held or not self.queue:
            return None
        for item in sorted(self.queue, key=lambda i: (i["priority"], i["seq"])):
            if (item["priority"] == ALERT and not item.get("bypass_alert_floor")
                    and self.clock() - self.last_alert_end < self.alert_floor):
                continue
            return item
        return None

    def _cache_key(self, text: str) -> tuple[str, str]:
        identity = self.cache_identity() if callable(self.cache_identity) else self.cache_identity
        return (text, str(identity or "unknown"))

    def _new_answer_item(self, text: str, remaining: list[str], generation: int | None = None) -> dict:
        self.seq += 1
        return {"id": uuid.uuid4().hex[:12], "seq": self.seq, "kind": "answer",
                "priority": ANSWER, "text": text, "ref": None, "rule_id": None,
                "group": "answer", "bypass_alert_floor": False, "remaining": remaining,
                "answer_generation": generation}

    def _append_answer_chunks(self, chunks: list[str], generation: int) -> dict | None:
        for item in (self.current, self.preparing, *sorted(self.queue, key=lambda i: i["seq"])):
            if (item is not None and item.get("kind") == "answer"
                    and item.get("answer_generation") == generation
                    and not item.get("cancelled")):
                tail = item
                while tail.get("prefetched_next") is not None:
                    tail = tail["prefetched_next"]
                tail.setdefault("remaining", []).extend(chunks)
                self._event("answer_extended", tail)
                self.cond.notify_all()
                return item
        return None

    def _prepare_audio(self, item: dict) -> None:
        if "wav" in item:
            return
        cached = self.audio_cache.get(self._cache_key(item["text"]))
        if cached is not None:
            item["wav"] = cached
            item["cached"] = True
            item["duration_s"] = round(wav_seconds(cached), 2)
            self.tts_ok = True
            self._event("cache_hit", item, cached=True)
            return
        self._event("synthesizing", item, cached=False)
        self.cond.release()
        try:
            try:
                item["wav"] = self.synthesize(item["text"])
                item["duration_s"] = round(wav_seconds(item["wav"]), 2)
                self.audio_cache.put(self._cache_key(item["text"]), item["wav"])
                self.tts_ok = True
            except Exception as exc:
                self.tts_ok = False
                log.warning("TTS failed, speaking as text only: %s", type(exc).__name__)
                item["wav"] = None
                item["error"] = type(exc).__name__
        finally:
            self.cond.acquire()

    def _wait_for_next(self) -> dict | None:
        while not self.stopping.is_set():
            item = self._next()
            if item is not None:
                self.queue.remove(item)
                return item
            timeout = max(0.05, self.talk_deadline - self.clock()) if self.talk_held else None
            if self.queue and not self.talk_held:
                timeout = max(0.05, self.alert_floor - (self.clock() - self.last_alert_end))
            self.cond.wait(timeout)
        return None

    def _run(self) -> None:
        while not self.stopping.is_set():
            with self.cond:
                item = self._wait_for_next()
                if item is None:
                    return
                self.preparing = item
                self._prepare_audio(item)
            with self.cond:
                self.preparing = None
                if item.get("cancelled"):
                    self._finish(item, item["cancelled"])
                    continue
                self._play(item)

    def _prefetch_next(self, item: dict) -> None:
        if item["kind"] != "answer" or not item.get("remaining"):
            return
        if item.get("cancelled") or self.talk_held or any(i["priority"] <= ALERT for i in self.queue):
            return
        next_item = self._new_answer_item(
            item["remaining"][0], item["remaining"][1:], item.get("answer_generation"))
        next_item["prefetch_done"] = False
        item["prefetched_next"] = next_item
        def run():
            with self.cond:
                self.preparing = next_item
                self._event("prefetching", next_item)
                self._prepare_audio(next_item)
                self.preparing = None
                next_item["prefetch_done"] = True
                if item.get("cancelled") and not next_item.get("cancelled"):
                    next_item["cancelled"] = item["cancelled"]
                if next_item.get("cancelled"):
                    self._finish(next_item, next_item["cancelled"])
                self.cond.notify_all()
        threading.Thread(target=run, name="audio-prefetch", daemon=True).start()

    def _wait_prefetch(self, item: dict, *, cancel: str | None = None) -> dict | None:
        next_item = item.get("prefetched_next")
        if next_item is None:
            return None
        if cancel and not next_item.get("cancelled"):
            next_item["cancelled"] = cancel
        while not next_item.get("prefetch_done") and not self.stopping.is_set():
            self.cond.wait(0.05)
        return next_item

    def _play(self, item: dict) -> None:
        # Anything more urgent that arrived during synthesis wins; this item was
        # preempted before it started and is dropped, not re-queued.
        if any(i["priority"] < item["priority"] for i in self.queue):
            self._finish(item, "preempted")
            return
        if self.talk_held:
            if item["priority"] == ALERT:
                self.queue.append(item)
                self._event("suppressed_talk_held", item)
            else:
                self._finish(item, "dropped_talk_held")
            return
        if item["wav"] is None:
            item["mode"], item["duration_s"] = "text", 0.0
            self._finish(item, "text_only")
            if item["priority"] == ALERT:
                self.last_alert_end = self.clock()
            return
        item["mode"] = "audio"
        item.setdefault("audio_base64", base64.b64encode(item["wav"]).decode("ascii"))
        self.current = item
        self._event("playing", item, duration_s=item["duration_s"], cached=item.get("cached", False))
        self._prefetch_next(item)
        window = item["duration_s"] + PLAYBACK_MARGIN_SECONDS
        deadline = self.clock() + window
        outcome, seen = "unconfirmed", 0
        while not self.stopping.is_set():
            acks = item.get("acks", [])
            for event in acks[seen:]:
                if event == "started":
                    deadline = self.clock() + window
                    outcome = "unconfirmed"
                elif event == "ended":
                    outcome = "spoken"
                elif event == "error" and outcome != "spoken":
                    outcome = "playback_failed"
            seen = len(acks)
            if outcome in ("spoken", "playback_failed"):
                break
            if item.get("cancelled"):
                outcome = item["cancelled"]
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            if any(i["priority"] < item["priority"] for i in self.queue):
                outcome = "preempted"
                break
            if self.talk_held:
                outcome = "suppressed_talk_held" if item["priority"] == ALERT else "dropped_talk_held"
                break
            self.cond.wait(remaining)
        self.current = None
        if item["priority"] == ALERT:
            self.last_alert_end = self.clock()
        if outcome == "suppressed_talk_held":
            # Never spoken over the microphone, but not lost: replayed on release.
            self.queue.append(item)
            self._event(outcome, item)
        else:
            self._finish(item, outcome)
            if outcome == "spoken" and not item.get("cancelled") and item.get("remaining"):
                next_item = self._wait_prefetch(item) or self._new_answer_item(
                    item["remaining"][0], item["remaining"][1:], item.get("answer_generation"))
                if not next_item.get("cancelled"):
                    self.queue.append(next_item)
                    self._event("queued", next_item)
                    self.cond.notify_all()
            elif item.get("prefetched_next"):
                self._wait_prefetch(item, cancel=outcome)
