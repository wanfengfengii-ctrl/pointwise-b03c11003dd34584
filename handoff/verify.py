"""Self-check: ``python3 -m handoff.verify``.

Runs the frozen-settings, device, intake, release, archive, handoff and claim
rules end to end against throw-away SQLite stores.  Any failed check is
reported and the process exits non-zero.
"""

from __future__ import annotations

import sys
import traceback

from .scenario import (
    F7,
    F8,
    TempStore,
    archive,
    expect_error,
    handoff,
    intake,
    load,
    open_shift,
)

CHECKS = []


def check(function):
    CHECKS.append(function)
    return function


def _frozen(service, shift="S1"):
    return open_shift(service, shift)


@check
def shift_freezes_and_conflicts():
    store = TempStore()
    try:
        service = store.service()
        first = _frozen(service)
        assert first["created"] is True
        assert first["frozen"]["T"]["unit"] == "℃"
        assert first["frozen"]["T"]["formula"] == "y=x+1/2"
        assert first["frozen"]["D"]["unit"] == "mm"
        assert first["frozen"]["D"]["formula"] == "y=x"

        reused = service.open_shift({
            "shift": "S1",
            "channels": {
                "T": {"unit": "℃", "calibration": {"a": 1, "b": "2/4"}},
                "D": {"unit": "mm", "calibration": "y=x"},
            },
        })
        assert reused["created"] is False and reused["reused"] is True
        assert reused["frozen"]["T"]["b"] == "1/2"

        expect_error(
            lambda: service.open_shift({
                "shift": "S1",
                "channels": {
                    "T": {"unit": "℃", "calibration": "y=x+1/3"},
                    "D": {"unit": "mm", "calibration": "y=x"},
                },
            }),
            "calibration_conflict",
        )
        expect_error(
            lambda: service.open_shift({
                "shift": "S1",
                "channels": {
                    "T": {"unit": "K", "calibration": "y=x+1/2"},
                    "D": {"unit": "mm", "calibration": "y=x"},
                },
            }),
            "calibration_conflict",
        )
    finally:
        store.cleanup()


@check
def device_only_serves_current_frame_and_refuses_bad_rows():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        bad = service.load_frames({
            "shift": "S1",
            "frames": [F7, [8, "4", 9, "T8", "D8"], F8],
        })
        assert bad["ok"] is False
        assert bad["accepted"] == [7]
        assert bad["rejected"][0]["error"]["code"] == "invalid_raw"
        assert bad["current_frame"] == 7
        # the rows after the broken one were not loaded
        status = service.status({"shift": "S1"})
        assert [frame["frame_no"] for frame in status["loaded"]] == [7]

        # a valid retry of the same feed leaves F7 alone and adds F8
        good = service.load_frames({"shift": "S1", "frames": [F7, F8]})
        assert good["accepted"] == [8]
        assert good["already_loaded"] == [7]

        # duplicate frame numbers are refused
        conflict = service.load_frames({
            "shift": "S1",
            "frames": [[8, 5, 5, "TX", "DX"]],
        })
        assert conflict["ok"] is False
        assert conflict["rejected"][0]["error"]["code"] == "duplicate_frame"

        # non-current frame may not be taken
        expect_error(
            lambda: intake(service, "T", frame_no=8),
            "frame_not_current",
        )
        # only the current frame exists for intake
        taken = intake(service, "T")
        assert taken["frame_no"] == 7 and taken["raw"] == 0
        assert taken["source"] == "T7"
    finally:
        store.cleanup()


@check
def intake_persists_missing_and_can_be_reopened():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7, F8])
        intake(service, "T")
        missing = intake(service, "D")
        assert missing["raw"] is None and missing["state"] == "missing"
        assert missing["source"] == "D7"
        again = intake(service, "D")
        assert again["status"] == "already_taken"
        assert again["raw"] is None
        expect_error(
            lambda: intake(service, "D", raw=5),
            "intake_conflict",
        )
        expect_error(
            lambda: intake(service, "T", raw=99),
            "intake_conflict",
        )
        status = service.status({"shift": "S1"})
        assert status["intakes"]["7"]["T"]["raw"] == 0
        assert status["intakes"]["7"]["D"]["raw"] is None
    finally:
        store.cleanup()


@check
def release_requires_both_channels_and_never_advances():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7, F8])
        intake(service, "T")
        expect_error(
            lambda: service.release({"shift": "S1"}),
            "intake_incomplete",
        )
        assert service.status({"shift": "S1"})["current_frame"] == 7
        intake(service, "D")

        before = expect_error(
            lambda: service.release({"shift": "S1",
                                     "simulate_failure": "before"}),
            "release_not_applied",
        )
        assert before.details["retry"] is True
        status = service.status({"shift": "S1"})
        assert status["current_frame"] == 7
        assert status["releases"] == {}

        after = expect_error(
            lambda: service.release({"shift": "S1",
                                     "simulate_failure": "after"}),
            "release_ack_unknown",
        )
        assert after.details["recover_by"] == "release_status"
        # the lost answer is remembered: F8 cannot be settled past it
        blocked = expect_error(
            lambda: service.release({"shift": "S1", "frame_no": 8}),
            "release_pending_ack",
        )
        assert blocked.details["pending_frame"] == 7
        # intake on F8 stays possible while the answer is pending
        assert intake(service, "D")["frame_no"] == 8
        recovered = service.release_status({"shift": "S1", "frame_no": 7})
        assert recovered["released"] is True
        assert recovered["ack_matches_intakes"] is True
        assert recovered["pending_ack"] is None
        assert service.status({"shift": "S1"})["current_frame"] == 8

        # the next frame is not released by settling the previous one
        assert service.release_status({"shift": "S1", "frame_no": 8})["released"] is False
        # resettling the same frame returns the original acknowledgement
        again = service.release({"shift": "S1", "frame_no": 7})
        assert again["status"] == "already_released"
        assert again["ack"] == recovered["ack"]
    finally:
        store.cleanup()


@check
def release_only_settles_the_original_frame():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7, F8])
        intake(service, "T")
        intake(service, "D")
        service.release({"shift": "S1"})
        intake(service, "D")  # F8 displacement, as in the walk-through
        # F7 is no longer current: reopening it only reads what was persisted
        assert intake(service, "T", frame_no=7)["frame_no"] == 7
        status = service.status({"shift": "S1"})
        assert status["current_frame"] == 8
        assert status["releases"].keys() == {"7"}
        # F8 is current and still open for its temperature channel
        assert intake(service, "T")["frame_no"] == 8
        # resettling F7 must not touch F8's release state
        service.release({"shift": "S1", "frame_no": 7})
        assert service.status({"shift": "S1"})["releases"].keys() == {"7"}
    finally:
        store.cleanup()


@check
def archive_tokens_are_first_execution_and_conflict_safe():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7, F8])
        intake(service, "T")
        intake(service, "D")
        first = archive(service, 7, "T")
        assert first["status"] == "archived"
        assert first["first_execution"] is True
        again = archive(service, 7, "T")
        assert again["status"] == "reclaimed"
        assert again["token"] == first["token"]
        expect_error(
            lambda: archive(service, 7, "T", evidence={"raw": 99}),
            "archive_conflict",
        )
        failed = expect_error(
            lambda: archive(service, 7, "D", simulate_failure=True),
            "archive_not_applied",
        )
        assert failed.details["retry"] is True
        status = service.archive_status({"shift": "S1"})
        frame7 = status["frames"][0]
        assert frame7["channels"].keys() == {"T"}
        assert frame7["complete"] is False
    finally:
        store.cleanup()


@check
def handoff_waits_then_fixes_and_claims_exactly():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7, F8])
        intake(service, "T")
        intake(service, "D")
        service.release({"shift": "S1"})
        intake(service, "D")  # F8

        archive(service, 7, "T")
        waiting = handoff(service, 7)
        assert waiting["status"] == "waiting"
        assert waiting["missing"] == ["D"]
        assert "handoff_no" not in waiting

        expect_error(
            lambda: archive(service, 7, "D", simulate_failure=True),
            "archive_not_applied",
        )
        assert handoff(service, 7)["status"] == "waiting"

        archive(service, 7, "D")
        issued = handoff(service, 7)
        assert issued["status"] == "issued"
        assert issued["handoff_no"].startswith("H-")
        assert handoff(service, 7)["handoff_no"] == issued["handoff_no"]

        # closing the source also closes the archive; claims wait for reopen
        service.close_source({"shift": "S1"})
        expect_error(
            lambda: service.claim({"handoff_no": issued["handoff_no"]}),
            "archive_closed",
        )
        service.open_archive({"shift": "S1"})
        delivered = service.claim({"handoff_no": issued["handoff_no"]})
        assert delivered["status"] == "delivered"
        temp = delivered["channels"]["T"]
        disp = delivered["channels"]["D"]
        assert temp["unit"] == "℃" and temp["value"] == "1/2"
        assert temp["value_display"] == "1/2 ℃"
        assert temp["source"] == "T7"
        assert disp["unit"] == "mm" and disp["state"] == "missing"
        assert disp["value"] is None
        assert disp["source"] == "D7"
        assert temp["first_execution_token"] == archive(service, 7, "T")["token"]
        assert disp["first_execution_token"] == archive(service, 7, "D")["token"]

        repeat = service.claim({"handoff_no": issued["handoff_no"]})
        for channel in ("T", "D"):
            assert repeat["channels"][channel]["first_claim"] is False
            assert (repeat["channels"][channel]["claimed_at"]
                    == delivered["channels"][channel]["claimed_at"])
        expect_error(
            lambda: service.claim({"handoff_no": "H-UNKNOWN"}),
            "handoff_not_found",
        )
    finally:
        store.cleanup()


@check
def closing_source_stops_the_device():
    store = TempStore()
    try:
        service = store.service()
        _frozen(service)
        load(service, [F7])
        intake(service, "T")
        intake(service, "D")
        service.release({"shift": "S1"})
        service.close_source({"shift": "S1"})
        expect_error(
            lambda: service.load_frames({"shift": "S1", "frames": [F8]}),
            "source_closed",
        )
        expect_error(
            lambda: service.intake({"shift": "S1", "channel": "T"}),
            "source_closed",
        )
    finally:
        store.cleanup()


@check
def f7_f8_walkthrough():
    from .scenario import run_case, claim_after_cutoff

    store, log = run_case()
    try:
        assert log["load"]["accepted"] == [7, 8]
        assert log["intake_f7_t"]["raw"] == 0
        assert log["intake_f7_d"]["raw"] is None
        assert log["intake_f7_d"]["source"] == "D7"
        assert log["release_lost_ack"]["code"] == "release_ack_unknown"
        assert log["release_status"]["released"] is True
        assert log["intake_f8_d"]["raw"] == 9
        assert log["intake_f8_d"]["source"] == "D8"
        assert log["prepare_f7"]["items"][0]["value"] == "1/2"
        assert log["handoff_wait"]["status"] == "waiting"
        assert log["archive_f7_d_fail"]["code"] == "archive_not_applied"
        assert log["handoff_still_wait"]["status"] == "waiting"
        assert log["handoff_fixed"]["status"] == "issued"
        claim_after_cutoff(store, log)
        delivered = log["claim"]
        assert delivered["channels"]["T"]["value"] == "1/2"
        assert delivered["channels"]["T"]["source"] == "T7"
        assert delivered["channels"]["D"]["state"] == "missing"
        assert delivered["channels"]["D"]["source"] == "D7"
        assert log["claim_unknown"]["code"] == "handoff_not_found"
    finally:
        store.cleanup()


def main() -> int:
    failures = 0
    for function in CHECKS:
        try:
            function()
        except Exception:
            failures += 1
            print(f"FAIL {function.__name__}")
            traceback.print_exc()
        else:
            print(f"ok   {function.__name__}")
    if failures:
        print(f"\n{failures} check(s) failed", file=sys.stderr)
        return 1
    print(f"\nall {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
