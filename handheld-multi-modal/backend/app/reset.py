"""Per-camera history reset.

The producer owns its crop files, so Backend does not delete them directly. It
stops that camera's pipeline, waits for the exclusive lock to be released, and
then runs the producer's *own* CLI against the shared directory. The lock is
what makes this safe: if the producer is somehow still alive the CLI refuses and
the reset aborts rather than racing a live writer.

Scope is one camera. Clearing cam1 never touches cam2's evidence, pipeline,
rules or mission data.
"""
from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import time
from pathlib import Path

from . import pipelines
from .core import crop_store_busy

log = logging.getLogger("backend.reset")

# Step 4 of the handshake. This must outlast the *takeover* threshold, not
# merely match it: gvapython has no teardown hook, so stopping a pipeline
# does not release the crop lock -- the store only becomes free once its
# heartbeat has been stale for `takeover_seconds`. A wait equal to that
# threshold is a race the reset loses whenever the producer was alive at step 3,
# which is the normal case. Waiting takeover + this grace gives the staleness
# rule time to actually fire, plus room for one missed heartbeat tick.
LOCK_GRACE_SECONDS = 20.0
CLI_TIMEOUT_SECONDS = 60.0


class ResetError(RuntimeError):
    """Raised when the reset could not complete. History is reported unchanged."""


def _write(ingest, conn, operation):
    """Apply a write through the single writer.

    The ingest thread commits roughly every second. A bulk DELETE issued on a
    second connection loses that race and fails with `database is locked`, so
    every mutation here is handed to the ingest connection instead. Without
    ingest (tests, startup recovery) this connection is the only writer anyway.
    """
    if ingest is not None:
        return ingest.write(operation)
    try:
        result = operation(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _set_state(ingest, conn, stream_id: str, state: str) -> None:
    _write(ingest, conn, lambda c: c.execute(
        "UPDATE streams SET clear_state = ? WHERE stream_id = ?", (state, stream_id)))


def _wait_for_lock_release(cfg: dict, stream_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not crop_store_busy(cfg["crop_dir"], stream_id, cfg["takeover_seconds"]):
            return
        time.sleep(0.5)
    raise ResetError(
        f"{stream_id}: the crop producer still holds its lock after {timeout:.0f}s; "
        "history was not cleared")


def _run_cleanup(cfg: dict, stream_id: str) -> None:
    directory = str(Path(cfg["crop_dir"]) / stream_id)
    script = cfg["crop_cleanup_script"]
    if not Path(script).is_file():
        raise ResetError(f"cleanup script {script} is not mounted; history was not cleared")
    # The producer's own CLI, run in this container against the shared volume.
    # Deliberately not `docker exec`: no socket mount, no exec privileges.
    #
    # Run it as the user that owns the store. Backend is root and the producer
    # is not, so a cleanup run as root recreates `.lock`, `generation` and
    # `status.json` root-owned and 0644 -- after which the producer can never
    # reopen its own lock and that camera stays dead through every restart.
    # Dropping to the directory's owner makes this invocation indistinguishable
    # from the producer's own and keeps root-owned files out of the shared store.
    credentials = {}
    if os.geteuid() == 0:
        info = os.stat(directory)
        if info.st_uid != 0:
            credentials = {"user": info.st_uid, "group": info.st_gid}
    result = subprocess.run(  # nosec B603 - fixed interpreter/script argv, no shell
        [sys.executable, script, "--clear-store", directory, "--confirm-clear"],
        capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS, **credentials)
    if result.returncode != 0:
        raise ResetError(
            f"{stream_id}: cleanup exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:300]}")
    _check_producer_can_write(directory, credentials)


def _check_producer_can_write(directory: str, credentials: dict) -> None:
    """Fail loudly if cleanup left files the producer cannot reopen.

    Self-check for the ownership trap above: a wrong-owner `.lock` does not fail
    the CLI, it fails the *next* pipeline start, which surfaces as an opaque
    gvapython "Can't create PyObject py_class" minutes later. Catch it here.
    """
    uid = credentials.get("user")
    if uid is None:
        return
    for name in (".lock", "generation", "status.json"):
        path = Path(directory) / name
        if path.exists() and path.stat().st_uid != uid:
            raise ResetError(
                f"cleanup left {name} owned by uid {path.stat().st_uid}, not {uid}; "
                "the crop producer would not be able to restart")


def _read_generation(cfg: dict, stream_id: str) -> str | None:
    try:
        return (Path(cfg["crop_dir"]) / stream_id / "generation").read_text().strip() or None
    except OSError:
        return None


def clear_camera(conn, cfg: dict, client, ingest, stream_id: str) -> dict:
    with pipelines.lifecycle_lock():
        return _clear_camera(conn, cfg, client, ingest, stream_id)


def _clear_camera(conn, cfg: dict, client, ingest, stream_id: str) -> dict:
    """Run the full handshake for one camera. Raises ResetError on any failure."""
    row = conn.execute("SELECT * FROM streams WHERE stream_id = ?", (stream_id,)).fetchone()
    if row is None:
        raise ResetError(f"unknown camera {stream_id}")

    log.warning("clearing history for camera %s", stream_id)
    # Step 2. Persisted before anything destructive, so a crash mid-reset is
    # recoverable rather than ambiguous.
    _set_state(ingest, conn, stream_id, "DRAINING")
    if ingest is not None:
        ingest.set_draining(stream_id, True)
    try:
        # Step 3: stop only this camera's pipeline.
        if row["dlsps_instance_id"]:
            try:
                pipelines.stop(client, cfg["services"]["dlsps"], row["dlsps_instance_id"])
            except Exception as exc:  # already gone is a fine outcome here
                log.warning("camera %s pipeline delete reported %s", stream_id, exc)
            _write(ingest, conn, lambda c: c.execute(
                "UPDATE streams SET dlsps_instance_id = NULL WHERE stream_id = ?",
                (stream_id,)))

        # Step 4: the lock is the interlock, not a convention.
        _wait_for_lock_release(
            cfg, stream_id, cfg["takeover_seconds"] + LOCK_GRACE_SECONDS)
        # Step 5: producer-owned cleanup, which also rotates `generation`.
        _run_cleanup(cfg, stream_id)

        # Steps 6 and 7 in one transaction, this camera only: the rows go and the
        # rotated generation is adopted together, so no window exists in which
        # history is deleted but stale-generation messages are still accepted.
        generation = _read_generation(cfg, stream_id)

        def purge(c):
            c.execute(
                "DELETE FROM rule_matches WHERE track_id IN "
                "(SELECT track_id FROM tracks WHERE stream_id = ?)", (stream_id,))
            c.execute(
                "DELETE FROM track_intervals WHERE track_id IN "
                "(SELECT track_id FROM tracks WHERE stream_id = ?)", (stream_id,))
            c.execute("DELETE FROM alert_incidents WHERE stream_id = ?", (stream_id,))
            c.execute("DELETE FROM candidates WHERE stream_id = ?", (stream_id,))
            c.execute("DELETE FROM tracks WHERE stream_id = ?", (stream_id,))
            c.execute("DELETE FROM feed_health WHERE stream_id = ?", (stream_id,))
            c.execute(
                "UPDATE streams SET history_generation = ?, tracking_session_id = NULL "
                "WHERE stream_id = ?", (generation, stream_id))

        _write(ingest, conn, purge)
        if ingest is not None:
            ingest.forget(stream_id, generation)
    except Exception:
        # Leave DRAINING set only if we never got to a consistent state; the
        # caller reports failure and startup recovery will retry the sequence.
        _set_state(ingest, conn, stream_id, "IDLE")
        if ingest is not None:
            ingest.set_draining(stream_id, False)
        raise
    _set_state(ingest, conn, stream_id, "IDLE")
    if ingest is not None:
        ingest.set_draining(stream_id, False)

    # Step 8: the same fixed POST used at startup, with identical parameters.
    # Clear any backoff first: this is an operator action on a camera we have
    # just repaired, so it must retry now rather than serve out a penalty
    # earned by the state we just cleared.
    pipelines.forget_backoff(stream_id)
    started = pipelines.reconcile(
        conn, cfg, client, writer=ingest.write if ingest is not None else None)
    log.warning("camera %s history cleared, generation=%s, restarted=%s",
                stream_id, generation, stream_id in started)
    return {"stream_id": stream_id, "cleared": True, "history_generation": generation,
            "pipeline_restarted": stream_id in started}


def resume_interrupted(conn, cfg: dict, client, ingest) -> list[str]:
    """Finish clears that a crash interrupted.

    Runs before the startup reconcile, so a camera caught mid-reset is not
    restarted with half its history still present.
    """
    rows = conn.execute(
        "SELECT stream_id FROM streams WHERE clear_state = 'DRAINING'").fetchall()
    resumed = []
    for row in rows:
        log.warning("resuming interrupted clear for camera %s", row["stream_id"])
        try:
            clear_camera(conn, cfg, client, ingest, row["stream_id"])
            resumed.append(row["stream_id"])
        except ResetError as exc:
            log.error("could not resume clear for %s: %s", row["stream_id"], exc)
    return resumed
