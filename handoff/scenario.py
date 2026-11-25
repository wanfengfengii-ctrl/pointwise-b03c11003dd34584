"""Reproducible F7/F8 scenario shared by ``verify`` and ``demo``.

The bench: temperature channel ``T`` in ``℃`` with ``y = x + 1/2``, displacement
channel ``D`` in ``mm`` with ``y = x``.  Frame 7 is ``[0, null, T7, D7]`` and
frame 8 is ``[4, 9, T8, D8]``.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from .errors import HandoffError
from .service import Service

CHANNELS = {
    "T": {"name": "温度", "unit": "℃", "calibration": "y=x+1/2"},
    "D": {"name": "位移", "unit": "mm", "calibration": "y=x"},
}

F7 = [7, 0, None, "T7", "D7"]
F8 = [8, 4, 9, "T8", "D8"]


def open_shift(service: Service, shift="S1") -> dict:
    return service.open_shift({
        "shift": shift,
        "channels": {name: dict(spec) for name, spec in CHANNELS.items()},
    })


def load(service: Service, frames, shift="S1") -> dict:
    return service.load_frames({"shift": shift, "frames": frames})


def intake(service: Service, channel, shift="S1", **extra) -> dict:
    payload = {"shift": shift, "channel": channel}
    payload.update(extra)
    return service.intake(payload)


def archive(service: Service, frame_no, channel, shift="S1", **extra) -> dict:
    payload = {"shift": shift, "frame_no": frame_no, "channel": channel}
    payload.update(extra)
    return service.archive_submit(payload)


def handoff(service: Service, frame_no=None, shift="S1") -> dict:
    payload = {"shift": shift}
    if frame_no is not None:
        payload["frame_no"] = frame_no
    return service.handoff(payload)


class TempStore:
    """A throw-away SQLite store in its own directory."""

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="handoff-")
        self.path = os.path.join(self.directory, "handoff.sqlite3")

    def service(self) -> Service:
        return Service(self.path)

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def expect_error(callable_, code):
    try:
        callable_()
    except HandoffError as exc:
        if exc.code != code:
            raise AssertionError(
                f"expected error {code!r}, got {exc.code!r}: {exc.message}"
            ) from None
        return exc
    raise AssertionError(f"expected error {code!r}, but the call succeeded")


def run_case(shift="S1"):
    """Run the textbook case and return the store plus a log of answers.

    Sequence:
      1. open the shift (frozen settings)
      2. load F7 (0/null) and F8 (4/9)
      3. take F7 temperature and F7 displacement
      4. release F7 with a lost acknowledgement *after* the effect
      5. the device now sits on F8; take F8 displacement
      6. archive F7 temperature and F8 displacement; both frames wait
      7. the F7 displacement archive submission fails; F7 still waits
      8. resubmit F7 displacement; F7 is complete and its number is fixed
    """

    store = TempStore()
    service = store.service()
    log = {}

    log["open"] = open_shift(service, shift)
    log["load"] = load(service, [F7, F8], shift)
    log["intake_f7_t"] = intake(service, "T", shift)
    log["intake_f7_d"] = intake(service, "D", shift)
    log["release_lost_ack"] = expect_error(
        lambda: service.release({"shift": shift, "simulate_failure": "after"}),
        "release_ack_unknown",
    ).to_dict()
    log["release_status"] = service.release_status(
        {"shift": shift, "frame_no": 7}
    )
    log["intake_f8_d"] = intake(service, "D", shift)
    log["prepare_f7"] = service.prepare({"shift": shift, "frame_no": 7})
    log["archive_f7_t"] = archive(service, 7, "T", shift)
    log["handoff_wait"] = handoff(service, 7, shift)
    log["archive_f8_d"] = archive(service, 8, "D", shift)
    log["handoff_f8_wait"] = handoff(service, 8, shift)
    log["archive_f7_d_fail"] = expect_error(
        lambda: archive(service, 7, "D", shift, simulate_failure=True),
        "archive_not_applied",
    ).to_dict()
    log["handoff_still_wait"] = handoff(service, 7, shift)
    log["archive_f7_d"] = archive(service, 7, "D", shift)
    log["handoff_fixed"] = handoff(service, 7, shift)
    return store, log


def claim_after_cutoff(store, log):
    """Close the source, reopen the archive and claim by handoff number."""

    service = store.service()
    try:
        log["close_source"] = service.close_source({"shift": "S1"})
        log["claim_while_shut"] = expect_error(
            lambda: service.claim({"handoff_no": log["handoff_fixed"]["handoff_no"]}),
            "archive_closed",
        ).to_dict()
        log["open_archive"] = service.open_archive({"shift": "S1"})
        log["claim"] = service.claim({
            "handoff_no": log["handoff_fixed"]["handoff_no"],
        })
        log["claim_again"] = service.claim({
            "handoff_no": log["handoff_fixed"]["handoff_no"],
        })
        log["claim_unknown"] = expect_error(
            lambda: service.claim({"handoff_no": "H-UNKNOWN"}), "handoff_not_found"
        ).to_dict()
    finally:
        service.close()
    return log
