# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""On-demand flight summary reports, built from MQTT scene captions.

This deliberately does not call out to an LLM: the minimal VSS deployment
this dashboard talks to (docker-compose-vss.yml) explicitly excludes VSS's
own summarization path (VLM/LLM serving, audio, RabbitMQ -- see that file's
header comment), and the UAV stack's own captioner (InternVL2-1B etc, see
uav-vision-analytics) is a *per-frame* VLM, not a document summarizer. Instead
this is a classic extractive map-reduce over the caption stream already
flowing on MQTT (bridged into this process by caption_log.py):

  MAP (per camera, ``_map_camera``):
    * de-duplicate consecutive near-identical captions into "scenes" with a
      start/end time and an occurrence count (a static scene re-described
      every few seconds by the VLM should not become N near-identical bullet
      points);
    * tally keyword frequency (simple stopword-filtered tokenization) as a
      proxy for "what showed up".

  REDUCE (across cameras, ``build_summary``):
    * downsample each camera's own scene list to its share of the overall
      bullet budget, independently of the others;
    * concatenate the per-camera timelines **grouped by camera** (all of
      camera A's highlights in chronological order, then all of camera B's,
      etc.) rather than interleaving every camera's scenes into one global
      chronological feed -- with several cameras captioning concurrently, a
      single interleaved timeline reads as "cpu, gpu, npu, cpu, gpu, ..."
      which is hard to follow; grouping by camera makes each camera's story
      readable start-to-finish before moving to the next one. Cameras are
      ordered the way the caller supplied them (normally the order the
      dashboard currently shows the camera cards in);
    * merge keyword tallies into one ranked list for the overview paragraph;
    * fold in the current search context (query + matched clips), if the
      report was requested from the search panel, as an appendix -- this is
      the "summarize-from-search" entry point, tying a natural-language query
      back to the raw caption evidence around the same time window.

Routes:
  * ``POST /api/summary``      -- JSON report (used to render the panel).
  * ``GET  /api/summary.pdf``  -- same report as a downloadable PDF.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

import caption_log
from pdf_writer import PdfReport

logger = logging.getLogger("viz-backend.vss-summary")

router = APIRouter()

# Matches main.py's CAMERA_PATTERN -- cameras here are identified by their
# RTSP stream path (e.g. "uav-front-camera"), not by compute device: two
# cameras can both run captioning on "gpu" and still be reported separately.
CAMERA_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"
HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"

MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 24 * 60
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_MAX_HIGHLIGHTS = 20
MAX_HIGHLIGHTS_CAP = 100
TOP_KEYWORDS = 10

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "of", "in", "on", "at",
    "to", "for", "and", "or", "with", "it", "its", "this", "that", "there", "here", "over", "near",
    "as", "by", "from", "into", "onto", "than", "then", "also", "while", "which", "appears", "seems",
    "visible", "shows", "showing", "shown", "scene", "image", "frame", "video", "appear", "appearing",
}
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")


@dataclass
class Scene:
    camera: str
    text: str
    start_ts: float
    end_ts: float
    occurrences: int


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokens(text: str) -> list:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def _map_camera(camera: str, entries: list) -> tuple:
    """entries: list[caption_log.CaptionEntry], oldest first.

    Returns (scenes, keyword_counter) for this camera.
    """
    scenes: list = []
    counter: Counter = Counter()
    current: Optional[Scene] = None
    for entry in entries:
        counter.update(_tokens(entry.text))
        norm = _normalize(entry.text)
        if current is not None and _normalize(current.text) == norm:
            current.end_ts = entry.ts
            current.occurrences += 1
            continue
        if current is not None:
            scenes.append(current)
        current = Scene(camera=camera, text=entry.text.strip(), start_ts=entry.ts, end_ts=entry.ts, occurrences=1)
    if current is not None:
        scenes.append(current)
    return scenes, counter


def _downsample(scenes: list, budget: int) -> list:
    """Pick up to `budget` scenes from a single camera's chronologically
    sorted scene list. Two passes:

      1. "Notable" scenes -- top-scoring by (duration * occurrences), i.e.
         ones the VLM kept re-describing or that held for a while. These are
         worth surfacing regardless of where they fall in time.
      2. Fill the remaining budget with an evenly-spaced chronological sample
         of what's left, so a long, uneventful flight still gets a
         representative timeline instead of only its chattiest moments (e.g.
         40 single-shot scenes with identical "interest" scores should spread
         across the whole window, not bunch up at the start).

    Always keeps the first and last scene so the report's timeline always
    spans the full window.
    """
    if len(scenes) <= budget or budget <= 0:
        return list(scenes)
    if budget == 1:
        return [max(scenes, key=lambda s: (s.end_ts - s.start_ts, s.occurrences))]

    keep_ids = {id(scenes[0]), id(scenes[-1])}
    notable_budget = max(0, budget - 2)
    scored = sorted(scenes, key=lambda s: (s.end_ts - s.start_ts) * max(s.occurrences, 1), reverse=True)
    top_score = scored[0] and (scored[0].end_ts - scored[0].start_ts) * max(scored[0].occurrences, 1)
    if top_score and top_score > 0:
        for s in scored:
            if len(keep_ids) - 2 >= notable_budget:
                break
            score = (s.end_ts - s.start_ts) * max(s.occurrences, 1)
            if score <= 0:
                break
            keep_ids.add(id(s))

    remaining_slots = budget - len(keep_ids)
    if remaining_slots > 0:
        leftover = [s for s in scenes if id(s) not in keep_ids]
        if leftover:
            step = max(1, len(leftover) // remaining_slots)
            for i in range(0, len(leftover), step):
                if len(keep_ids) >= budget:
                    break
                keep_ids.add(id(leftover[i]))

    return [s for s in scenes if id(s) in keep_ids]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class SearchContextClip(BaseModel):
    camera: Optional[str] = None
    captured_at: Optional[str] = None
    score: Optional[float] = Field(None, ge=0, le=100)
    filename: Optional[str] = None


class SearchContext(BaseModel):
    query: Optional[str] = Field(None, max_length=500)
    clips: list[SearchContextClip] = Field(default_factory=list, max_length=50)


class SummaryRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=253)
    cameras: list[str] = Field(..., min_length=1, max_length=16)
    window_minutes: int = Field(DEFAULT_WINDOW_MINUTES, ge=MIN_WINDOW_MINUTES, le=MAX_WINDOW_MINUTES)
    max_highlights: int = Field(DEFAULT_MAX_HIGHLIGHTS, ge=1, le=MAX_HIGHLIGHTS_CAP)
    search_context: Optional[SearchContext] = None

    def validate_cameras(self) -> list:
        bad = [c for c in self.cameras if not re.fullmatch(CAMERA_PATTERN, c)]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid camera(s): {bad}")
        return self.cameras


def build_summary(host: str, cameras: list, window_minutes: int,
                   max_highlights: int = DEFAULT_MAX_HIGHLIGHTS,
                   search_context: Optional[SearchContext] = None,
                   now: Optional[float] = None) -> dict:
    """Pure (apart from reading caption_log's buffers) report builder --
    the part of this module worth unit testing directly.

    Highlights are grouped by camera (not interleaved chronologically across
    cameras): each camera gets a fair share of ``max_highlights`` -- divided
    evenly, with any remainder going to the cameras that actually produced
    the most captions -- and its own highlights stay in their own
    chronological run, in the order ``cameras`` was given.
    """
    now = time.time() if now is None else now
    since = now - window_minutes * 60

    per_camera_scenes: dict = {}
    keywords: Counter = Counter()
    per_camera_counts: dict = {}
    for camera in cameras:
        entries = caption_log.get_captions(host, camera, since)
        scenes, counter = _map_camera(camera, entries)
        per_camera_scenes[camera] = scenes
        keywords.update(counter)
        per_camera_counts[camera] = {"captions": len(entries), "scenes": len(scenes)}

    total_scenes = sum(len(s) for s in per_camera_scenes.values())

    # Split the highlight budget across cameras: an even base share per
    # camera, with leftover slots (from integer division, or from cameras
    # that have fewer scenes than their base share) redistributed to the
    # busiest remaining cameras so a quiet camera doesn't waste slots a
    # chattier one could use.
    base_share = max(1, max_highlights // max(1, len(cameras)))
    highlights_by_camera: dict = {}
    cameras_by_volume = sorted(cameras, key=lambda c: len(per_camera_scenes.get(c, [])), reverse=True)
    remaining_budget = max_highlights
    shares = {c: min(base_share, len(per_camera_scenes.get(c, []))) for c in cameras}
    remaining_budget -= sum(shares.values())
    if remaining_budget > 0:
        for c in cameras_by_volume:
            if remaining_budget <= 0:
                break
            extra_capacity = len(per_camera_scenes.get(c, [])) - shares[c]
            if extra_capacity <= 0:
                continue
            grant = min(extra_capacity, remaining_budget)
            shares[c] += grant
            remaining_budget -= grant

    highlights: list = []
    for camera in cameras:
        scenes = sorted(per_camera_scenes.get(camera, []), key=lambda s: s.start_ts)
        picked = _downsample(scenes, shares.get(camera, 0))
        picked.sort(key=lambda s: s.start_ts)
        highlights_by_camera[camera] = picked
        highlights.extend(picked)

    total_captions = sum(v["captions"] for v in per_camera_counts.values())
    top_keywords = [w for w, _ in keywords.most_common(TOP_KEYWORDS)]

    if total_captions == 0:
        overview = (
            f"No captions were received from {', '.join(cameras)} in the last "
            f"{window_minutes} minute(s). Start the pipeline with captioning enabled, and confirm "
            f"this ground station can reach {host}'s MQTT broker."
        )
    else:
        span_desc = f"{window_minutes} minute" + ("s" if window_minutes != 1 else "")
        camera_desc = cameras[0] if len(cameras) == 1 else f"{len(cameras)} cameras ({', '.join(cameras)})"
        overview = (
            f"Over the last {span_desc}, {camera_desc} produced {total_captions} scene caption(s), "
            f"condensed into {total_scenes} distinct scene(s)."
        )
        if top_keywords:
            overview += f" Recurring subjects: {', '.join(top_keywords)}."

    return {
        "generated_at": _iso(now),
        "host": host,
        "cameras": cameras,
        "window_minutes": window_minutes,
        "since": _iso(since),
        "total_captions": total_captions,
        "camera_breakdown": per_camera_counts,
        "keywords": top_keywords,
        "overview": overview,
        # Grouped-by-camera: each camera's own highlights, in order, one
        # group per camera (in the order `cameras` was supplied), rather
        # than one globally time-interleaved list.
        "highlights_by_camera": [
            {
                "camera": camera,
                "highlights": [
                    {
                        "camera": s.camera,
                        "start": _iso(s.start_ts),
                        "end": _iso(s.end_ts),
                        "occurrences": s.occurrences,
                        "text": s.text,
                    }
                    for s in highlights_by_camera.get(camera, [])
                ],
            }
            for camera in cameras
        ],
        # Flat view kept for callers/older UI code that just wants "all the
        # highlights" without caring about grouping -- still ordered
        # camera-by-camera (not time-interleaved), matching the grouped view.
        "highlights": [
            {
                "camera": s.camera,
                "start": _iso(s.start_ts),
                "end": _iso(s.end_ts),
                "occurrences": s.occurrences,
                "text": s.text,
            }
            for camera in cameras
            for s in highlights_by_camera.get(camera, [])
        ],
        "highlights_total_scenes": total_scenes,
        "search_context": search_context.model_dump() if search_context else None,
    }


def _render_pdf(report: dict) -> bytes:
    subtitle = (
        f"Host {report['host']} \u00b7 camera(s): {', '.join(report['cameras'])} \u00b7 "
        f"window: last {report['window_minutes']} min \u00b7 generated {report['generated_at']}"
    )
    doc = PdfReport(title="UAV Flight Summary Report", subtitle=subtitle)
    doc.heading("Overview")
    doc.paragraph(report["overview"])

    ctx = report.get("search_context")
    if ctx and ctx.get("query"):
        doc.spacer()
        doc.heading("Search context")
        doc.paragraph(f'This report was generated from a search for: "{ctx["query"]}"')
        for clip in ctx.get("clips") or []:
            parts = []
            if clip.get("camera"):
                parts.append(f"camera {clip['camera']}")
            if clip.get("captured_at"):
                parts.append(clip["captured_at"])
            if clip.get("score") is not None:
                parts.append(f"{clip['score']:.0f}% relevance")
            if clip.get("filename"):
                parts.append(clip["filename"])
            doc.bullet(" \u00b7 ".join(parts) or "matched clip")

    doc.spacer()
    doc.heading(f"Timeline highlights ({len(report['highlights'])} of {report['highlights_total_scenes']} scenes)")
    # Grouped by camera: a subheading per camera, its own chronological run
    # of bullets underneath -- NOT one globally time-interleaved list, so a
    # reader can follow one camera's story start-to-finish before the next.
    groups = report.get("highlights_by_camera") or []
    if not any(g["highlights"] for g in groups):
        doc.paragraph("No scene captions were available for this window.")
    for group in groups:
        if not group["highlights"]:
            continue
        doc.spacer()
        doc.heading(f"Camera: {group['camera']}")
        for h in group["highlights"]:
            rep = f" (x{h['occurrences']})" if h["occurrences"] > 1 else ""
            doc.bullet(f"{h['start']}{rep} - {h['text']}")

    if report["keywords"]:
        doc.spacer()
        doc.heading("Recurring subjects")
        doc.paragraph(", ".join(report["keywords"]))

    doc.spacer()
    doc.heading("Per-camera caption counts")
    for camera, counts in report["camera_breakdown"].items():
        doc.bullet(f"{camera}: {counts['captions']} caption(s), {counts['scenes']} distinct scene(s)")

    return doc.render()


@router.post("/api/summary")
async def summary(req: SummaryRequest):
    cameras = req.validate_cameras()
    return build_summary(req.host, cameras, req.window_minutes, req.max_highlights, req.search_context)


@router.get("/api/summary.pdf")
async def summary_pdf(
    host: str = Query(..., min_length=1, max_length=253),
    cameras: str = Query(..., description="comma-separated camera names, e.g. uav-front-camera,uav-rear-camera"),
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=MIN_WINDOW_MINUTES, le=MAX_WINDOW_MINUTES),
    max_highlights: int = Query(DEFAULT_MAX_HIGHLIGHTS, ge=1, le=MAX_HIGHLIGHTS_CAP),
    query: Optional[str] = Query(None, max_length=500),
):
    camera_list = [c.strip() for c in cameras.split(",") if c.strip()]
    if not camera_list or any(not re.fullmatch(CAMERA_PATTERN, c) for c in camera_list):
        raise HTTPException(status_code=400, detail="cameras must be a comma-separated list of camera names")
    ctx = SearchContext(query=query) if query else None
    report = build_summary(host, camera_list, window_minutes, max_highlights, ctx)
    try:
        pdf_bytes = _render_pdf(report)
    except Exception:
        logger.exception("summary.pdf: render failed")
        raise HTTPException(status_code=500, detail="Failed to render PDF report")
    safe_cameras = "-".join(re.sub(r"[^A-Za-z0-9_-]", "_", c) for c in camera_list)
    filename = f"uav-summary-{host}-{safe_cameras}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
