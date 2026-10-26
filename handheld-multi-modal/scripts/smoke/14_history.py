#!/usr/bin/env python3
"""Typed-history contract and live typed-query smoke."""

import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import test_backend  # noqa: E402


EVIDENCE = Path(os.environ.get(
    "HISTORY_EVIDENCE", ROOT / "docs/application/evidence/history"))
URL = os.environ.get("HISTORY_URL", "https://127.0.0.1:8443/api/ask")
TLS_CERT = Path(os.environ.get("ASSET_ROOT", ROOT)) / "config/tls/server.crt"
ATTEMPTS = 1  # first-submission reliability; never retry the smoke into a pass
SAFE_FAILURE = "I could not turn that into a query I can answer."


def write_json(name, value):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + "\n")


def post(payload):
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    if not URL.startswith(("http://", "https://")):
        raise ValueError(f"HISTORY_URL must be http(s): {URL}")
    context = ssl.create_default_context(cafile=TLS_CERT) if URL.startswith("https://") else None
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=180, context=context) as response:  # nosec B310 - scheme checked above
        result = json.load(response)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def verify_live(body):
    data = body.get("data", {})
    if data.get("intent") != "video_history":
        return False
    query = data.get("query", {})
    if query.get("filters", {}).get("streams") != ["cam1"]:
        raise AssertionError("live query escaped the selected cam1 scope")
    if query.get("filters", {}).get("classes") != ["person"]:
        raise AssertionError("live question lost the requested person class")
    started = datetime.fromisoformat(query["window"]["started_utc"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(query["window"]["ended_utc"].replace("Z", "+00:00"))
    if not 599 <= (ended - started).total_seconds() <= 601:
        raise AssertionError("live query did not preserve the ten-minute window")
    if query["total"] < len(query["tracks"]):
        raise AssertionError("full aggregate is smaller than the visible page")
    if sum(query["class_counts"].values()) != query["total"]:
        raise AssertionError("class aggregates do not cover the full selection")
    for track in query["tracks"]:
        if track["stream_id"] != "cam1" or not track["intervals"]:
            raise AssertionError("track is outside scope or lacks matching intervals")
    return True


def main():
    focused = [
        test_backend.test_history_plan_validation_retries_then_fails_safely,
        test_backend.test_history_interval_query_aggregates_completeness_and_safe_grounding,
        test_backend.test_history_readonly_budget_and_stable_paging,
        test_backend.test_handheld_search_fast_path_and_recovery,
    ]
    for test in focused:
        test()

    write_json("plan_validation_cases.json", {
        "generated_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "cases": [
            "unknown route and plan keys rejected",
            "unknown intent rejected",
            "unknown detector class rejected without near-match mapping",
            "unknown camera rejected",
            "model cannot widen the operator-selected camera scope",
            "relative windows and page sizes are bounded",
            "one invalid-plan retry then explicit safe failure",
            "read-only connection rejects writes",
            "queries are interrupted at 500 ms",
        ],
    })
    paging_evidence = {
        "result": "PASS",
        "fixed_cutoff": True,
        "track_id_ceiling": True,
        "stream_session_generation_frozen": True,
        "newer_insert_excluded_from_later_page": True,
        "signed_token_tamper_rejected": True,
        "live": {"status": "not_exercised_insufficient_rows"},
    }

    # Missing or uncheckable details are answered with a stated assumption, not refused.
    probes = []
    for question, stream in (("What was seen?", "cam1"),
                             ("Were there any weapons in the last ten minutes?", "cam1"),
                             ("What did camera 2 see in the last ten minutes?", "cam2")):
        body = post({"text": question, "selected_stream": "cam1"})
        data = body["data"]
        assert data["intent"] == "video_history" and data.get("assumption"), body
        assert data["query"]["filters"]["streams"] == [stream], body
        probes.append({"question": question, "intent": data["intent"], "streams": [stream],
                       "assumption": data["assumption"],
                       "elapsed_seconds": body["elapsed_seconds"]})
    success = None
    for attempt in range(1, ATTEMPTS + 1):
        body = post({
            "text": "Which person tracks were recorded in the last ten minutes?",
            "selected_stream": "cam1",
        })
        data = body.get("data", {})
        probes.append({
            "question": "ten_minute_person_tracks",
            "attempt": attempt,
            "request_id": body.get("request_id"),
            "elapsed_seconds": body["elapsed_seconds"],
            "intent": data.get("intent"),
            "answer_text": data.get("answer_text"),
            "error_codes": [item.get("code") for item in data.get("errors", [])],
        })
        if verify_live(body):
            success = body
            break
        if data.get("answer_text") != SAFE_FAILURE:
            raise AssertionError("malformed stock-model output did not fail safely")

    if success is None:
        write_json("route_probes.json", {"result": "FAIL", "first_submission_queries": probes})
        raise AssertionError(
            f"typed query produced no valid strict plan in {ATTEMPTS} independent requests")
    write_json("ten_minute_query.json", success)

    hour_success = False
    for attempt in range(1, ATTEMPTS + 1):
        body = post({
            "text": "What was detected in the last 60 minutes?",
            "selected_stream": "cam1",
        })
        data = body.get("data", {})
        query = data.get("query")
        probes.append({
            "question": "paged_hour_history",
            "attempt": attempt,
            "request_id": body.get("request_id"),
            "elapsed_seconds": body["elapsed_seconds"],
            "intent": data.get("intent"),
            "answer_text": data.get("answer_text"),
            "error_codes": [item.get("code") for item in data.get("errors", [])],
        })
        if query:
            start = datetime.fromisoformat(query["window"]["started_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(query["window"]["ended_utc"].replace("Z", "+00:00"))
            assert (end-start).total_seconds() == 3600 and query["filters"]["classes"] is None
            hour_success = True
            token = query.get("next_page_token")
            if token:
                next_query = post({"page_token": token})["data"]["query"]
                first_ids = {track["track_id"] for track in query["tracks"]}
                next_ids = {track["track_id"] for track in next_query["tracks"]}
                if query["query_cutoff_utc"] != next_query["query_cutoff_utc"]:
                    raise AssertionError("live paging changed the fixed cutoff")
                if query["total"] != next_query["total"] or first_ids & next_ids:
                    raise AssertionError("live paging changed totals or duplicated rows")
                paging_evidence["live"] = {
                    "status": "PASS",
                    "query_cutoff_utc": query["query_cutoff_utc"],
                    "total": query["total"],
                    "page_0_track_ids": sorted(first_ids),
                    "page_1_track_ids": sorted(next_ids),
                    "duplicate_track_ids": sorted(first_ids & next_ids),
                }
            break
        if data.get("answer_text") != SAFE_FAILURE:
            raise AssertionError("invalid paging response did not fail safely")

    write_json("route_probes.json", {
        "generated_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "first_submission_queries": probes,
        "safe_failure": SAFE_FAILURE,
    })
    if not hour_success:
        raise AssertionError("hour-history question failed on its first submission")
    write_json("paging_stability.json", paging_evidence)
    print("PASS: strict plans, interval truth, paging, and live typed history")


if __name__ == "__main__":
    main()
