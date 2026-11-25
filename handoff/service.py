"""The shift-handoff service: all business rules live here.

Vocabulary
==========
shift (班)
    Opened once with two frozen channels (unit + exact ``y = a*x + b``).
    Channel settings are frozen for the whole shift; reopening the same shift
    with identical settings is a no-op, with different settings it is refused.

device (设备替身)
    Loads ``[frame_no, temperature, displacement, temp_source, disp_source]``
    rows in order.  Frame numbers are unique inside a shift; raw values are
    integers or ``null``.  Only the *current* frame is served.  A broken row is
    refused and the device does not advance past it.

intake (采录)
    Each channel is committed separately and can be reopened.  Only the frame
    the device currently points at can be taken.  Raw values (including a
    deliberate ``null`` = missing) are persisted before the device may be
    released.  Intake never waits for archiving.

release (释放)
    A frame is released only after *both* channels were persisted.  A failure
    before the effect leaves the same frame current; a failure after the
    effect is recovered from the persisted acknowledgement.  Releasing never
    settles or releases the next frame.

archive (归档)
    Submissions are separate per shift / frame / channel.  The first
    successful submission mints the first-execution token; resubmitting the
    same item returns that token; the same key with different evidence is
    refused.

handoff (交班)
    Issued per shift / frame.  While one channel has no archived receipt the
    frame waits; once both are in, the handoff number is fixed.  Closing the
    source also closes the bridge and the archive; after reopening the
    archive, the handoff number yields both channels' raw value, exact
    conversion or missing state, unit, source and first-execution token.
    Unknown numbers report "not found".
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction

from . import db
from .errors import HandoffError
from .rational import (
    apply_calibration,
    calibration_formula,
    format_rational,
    parse_calibration,
    parse_int,
)

CHANNELS = ("T", "D")
CHANNEL_LABELS = {"T": "温度", "D": "位移"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _require(condition: bool, code: str, message: str, **details) -> None:
    if not condition:
        raise HandoffError(code, message, **details)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class Service:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = db.connect(path)
        with db.transaction(self.conn):
            db.ensure_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # ------------------------------------------------------------------ util
    def _state(self, shift_no) -> sqlite3.Row:
        _require(isinstance(shift_no, str) and shift_no.strip(),
                 "invalid_shift", "shift 必须是非空字符串")
        row = self.conn.execute(
            "SELECT * FROM shift_state WHERE shift_no = ?", (shift_no,)
        ).fetchone()
        if row is None:
            raise HandoffError("shift_not_open", f"班次 {shift_no} 尚未开班",
                               shift_no=shift_no)
        return row

    def _journal(self, operation, shift_no=None, frame_no=None, channel=None,
                 detail=None) -> None:
        self.conn.execute(
            "INSERT INTO journal(at, operation, shift_no, frame_no, channel,"
            " detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), operation, shift_no, frame_no, channel,
             _canonical(detail or {})),
        )

    def _frame_by_no(self, shift_no, frame_no):
        return self.conn.execute(
            "SELECT * FROM device_frames WHERE shift_no = ? AND frame_no = ?",
            (shift_no, frame_no),
        ).fetchone()

    def _current(self, shift_no):
        state = self._state(shift_no)
        if not state["pos"]:
            return None
        return self.conn.execute(
            "SELECT * FROM device_frames WHERE shift_no = ? AND seq = ?",
            (shift_no, state["pos"]),
        ).fetchone()

    def _next_seq_no(self, shift_no, seq):
        row = self.conn.execute(
            "SELECT frame_no FROM device_frames WHERE shift_no = ? AND seq > ?"
            " ORDER BY seq LIMIT 1",
            (shift_no, seq),
        ).fetchone()
        return row["frame_no"] if row else None

    def _intake_row(self, shift_no, frame_no, channel):
        return self.conn.execute(
            "SELECT * FROM intakes WHERE shift_no = ? AND frame_no = ?"
            " AND channel = ?",
            (shift_no, frame_no, channel),
        ).fetchone()

    def _release_row(self, shift_no, frame_no):
        return self.conn.execute(
            "SELECT * FROM releases WHERE shift_no = ? AND frame_no = ?",
            (shift_no, frame_no),
        ).fetchone()

    def _handoff_row(self, shift_no, frame_no):
        return self.conn.execute(
            "SELECT * FROM handoffs WHERE shift_no = ? AND frame_no = ?",
            (shift_no, frame_no),
        ).fetchone()

    def _describe_channels(self, shift_no) -> dict:
        described = {}
        for row in self.conn.execute(
            "SELECT * FROM channels WHERE shift_no = ? ORDER BY channel",
            (shift_no,),
        ):
            a = Fraction(row["cal_a"])
            b = Fraction(row["cal_b"])
            described[row["channel"]] = {
                "name": row["name"],
                "unit": row["unit"],
                "a": format_rational(a),
                "b": format_rational(b),
                "formula": calibration_formula(a, b),
            }
        return described

    # ----------------------------------------------------------------- shift
    def open_shift(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        _require(isinstance(shift_no, str) and shift_no.strip(),
                 "invalid_shift", "shift 必须是非空字符串")
        start_frame = parse_int(payload.get("start_frame", 1), "start_frame")
        raw_channels = payload.get("channels")
        _require(isinstance(raw_channels, dict), "invalid_channels",
                 "channels 必须是对象，包含 T 与 D 两路")

        frozen: dict[str, dict] = {}
        for channel in CHANNELS:
            spec = raw_channels.get(channel)
            _require(isinstance(spec, dict), "invalid_channels",
                     f"缺少通道 {channel} 的单位与定标", channel=channel)
            name = spec.get("name") or CHANNEL_LABELS[channel]
            _require(isinstance(name, str) and name.strip(), "invalid_channels",
                     f"通道 {channel} 的名称必须是非空字符串", channel=channel)
            unit = spec.get("unit")
            _require(isinstance(unit, str) and unit.strip(), "invalid_channels",
                     f"通道 {channel} 必须给出单位", channel=channel)
            _require("calibration" in spec, "invalid_channels",
                     f"通道 {channel} 必须给出有理定标", channel=channel)
            a, b = parse_calibration(spec["calibration"],
                                     f"channels.{channel}.calibration")
            frozen[channel] = {"name": name, "unit": unit, "a": a, "b": b}

        with db.transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM shifts WHERE shift_no = ?", (shift_no,)
            ).fetchone()
            if existing is not None:
                stored = {
                    row["channel"]: row
                    for row in self.conn.execute(
                        "SELECT * FROM channels WHERE shift_no = ?", (shift_no,)
                    )
                }
                for channel in CHANNELS:
                    row = stored.get(channel)
                    spec = frozen[channel]
                    same = (
                        row is not None
                        and row["name"] == spec["name"]
                        and row["unit"] == spec["unit"]
                        and Fraction(row["cal_a"]) == spec["a"]
                        and Fraction(row["cal_b"]) == spec["b"]
                    )
                    if not same:
                        raise HandoffError(
                            "calibration_conflict",
                            f"班次 {shift_no} 的通道 {channel} 已按其他口径冻结",
                            channel=channel,
                            frozen={
                                "name": row["name"],
                                "unit": row["unit"],
                                "formula": calibration_formula(
                                    Fraction(row["cal_a"]),
                                    Fraction(row["cal_b"]),
                                ),
                            } if row else None,
                            requested={
                                "name": spec["name"],
                                "unit": spec["unit"],
                                "formula": calibration_formula(spec["a"],
                                                               spec["b"]),
                            },
                        )
                return {
                    "ok": True,
                    "shift": shift_no,
                    "created": False,
                    "reused": True,
                    "frozen": self._describe_channels(shift_no),
                }

            self.conn.execute(
                "INSERT INTO shifts(shift_no, start_frame, opened_at)"
                " VALUES (?, ?, ?)",
                (shift_no, start_frame, now()),
            )
            for channel in CHANNELS:
                spec = frozen[channel]
                self.conn.execute(
                    "INSERT INTO channels(shift_no, channel, name, unit,"
                    " cal_a, cal_b, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (shift_no, channel, spec["name"], spec["unit"],
                     format_rational(spec["a"]), format_rational(spec["b"]),
                     now()),
                )
            self.conn.execute(
                "INSERT INTO shift_state(shift_no, loaded, pos, pending_ack,"
                " source_open, bridge_open, archive_open)"
                " VALUES (?, 0, 0, NULL, 1, 1, 1)",
                (shift_no,),
            )
            self._journal("open_shift", shift_no,
                          detail={"start_frame": start_frame})
            return {
                "ok": True,
                "shift": shift_no,
                "created": True,
                "reused": False,
                "frozen": self._describe_channels(shift_no),
            }

    # ---------------------------------------------------------------- device
    def load_frames(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        frames = payload.get("frames")
        _require(isinstance(frames, list), "invalid_frames",
                 "frames 必须是按序排列的数组", shift_no=shift_no)

        with db.transaction(self.conn):
            state = self._state(shift_no)
            _require(bool(state["source_open"]), "source_closed",
                     "源已关闭，设备不再接新帧", shift_no=shift_no)
            _require(bool(state["bridge_open"]), "bridge_closed",
                     "桥已停止，设备不再接新帧", shift_no=shift_no)

            accepted = []
            already = []
            rejected = []
            loaded = state["loaded"]
            pos = state["pos"]
            for index, row in enumerate(frames):
                try:
                    parsed = self._parse_device_row(row)
                except HandoffError as exc:
                    rejected.append({"index": index, "row": row,
                                     "error": exc.to_dict()})
                    break  # refuse the bad frame and do not move past it
                duplicate = self._frame_by_no(shift_no, parsed["frame_no"])
                if duplicate is not None:
                    same = all(
                        duplicate[field] == parsed[field]
                        for field in ("temp_raw", "disp_raw",
                                      "temp_source", "disp_source")
                    )
                    if same:
                        already.append(parsed["frame_no"])
                        continue
                    rejected.append({
                        "index": index,
                        "row": row,
                        "error": HandoffError(
                            "duplicate_frame",
                            f"帧 {parsed['frame_no']} 在班内已存在且内容不同",
                            frame_no=parsed["frame_no"],
                        ).to_dict(),
                    })
                    break
                loaded += 1
                self.conn.execute(
                    "INSERT INTO device_frames(shift_no, seq, frame_no,"
                    " temp_raw, disp_raw, temp_source, disp_source, loaded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (shift_no, loaded, parsed["frame_no"], parsed["temp_raw"],
                     parsed["disp_raw"], parsed["temp_source"],
                     parsed["disp_source"], now()),
                )
                if not pos:
                    pos = loaded
                accepted.append(parsed["frame_no"])

            self.conn.execute(
                "UPDATE shift_state SET loaded = ?, pos = ?"
                " WHERE shift_no = ?",
                (loaded, pos, shift_no),
            )
            self._journal("load_frames", shift_no,
                          detail={"accepted": accepted,
                                  "already_loaded": already,
                                  "rejected": len(rejected)})
            current = self._current(shift_no)
            result = {
                "ok": not rejected,
                "shift": shift_no,
                "accepted": accepted,
                "already_loaded": already,
                "rejected": rejected,
                "current_frame": current["frame_no"] if current else None,
            }
            return result

    @staticmethod
    def _parse_raw(value, field):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise HandoffError("invalid_raw", f"{field} 必须是整数或 null",
                               field=field, got=value)
        return value

    @staticmethod
    def _parse_source(value, field):
        _require(isinstance(value, str) and value.strip(), "invalid_source",
                 f"{field} 必须是非空字符串", field=field, got=value)
        return value

    def _parse_device_row(self, row) -> dict:
        _require(
            isinstance(row, list) and len(row) == 5,
            "invalid_frame",
            "设备帧格式为 [帧号, 温度, 位移, 温度出处, 位移出处]",
            row=row,
        )
        frame_no = parse_int(row[0], "frame_no")
        temp_raw = self._parse_raw(row[1], "温度")
        disp_raw = self._parse_raw(row[2], "位移")
        _require(temp_raw is not None or disp_raw is not None, "invalid_frame",
                 f"帧 {frame_no} 的温度与位移不能同时为 null",
                 frame_no=frame_no)
        return {
            "frame_no": frame_no,
            "temp_raw": temp_raw,
            "disp_raw": disp_raw,
            "temp_source": self._parse_source(row[3], "温度出处"),
            "disp_source": self._parse_source(row[4], "位移出处"),
        }

    # ---------------------------------------------------------------- intake
    def intake(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        channel = payload.get("channel")
        _require(channel in CHANNELS, "invalid_channel",
                 "channel 必须是 T（温度）或 D（位移）", channel=channel)

        with db.transaction(self.conn):
            state = self._state(shift_no)
            _require(bool(state["source_open"]), "source_closed",
                     "源已关闭，不能再采录", shift_no=shift_no)
            _require(bool(state["bridge_open"]), "bridge_closed",
                     "桥已停止，不能再采录", shift_no=shift_no)

            frame = None
            explicit = payload.get("frame_no") is not None
            if explicit:
                requested = parse_int(payload["frame_no"], "frame_no")
                frame = self._frame_by_no(shift_no, requested)
                _require(frame is not None, "frame_not_found",
                         f"班内没有帧 {requested}", frame_no=requested)
            else:
                frame = self._current(shift_no)
                _require(frame is not None, "no_current_frame",
                         "设备没有当前帧", shift_no=shift_no)

            frame_no = frame["frame_no"]
            existing = self._intake_row(shift_no, frame_no, channel)
            if existing is not None:
                # reopening a committed channel: return what was persisted,
                # never re-read a device value behind the operator's back
                if "raw" in payload:
                    decided = self._parse_raw(payload["raw"], "raw")
                    _require(decided == existing["raw"], "intake_conflict",
                             "该路已采录；重开时不能改变已持久化的原值",
                             frame_no=frame_no, channel=channel,
                             stored=existing["raw"], requested=decided)
                return self._intake_result("already_taken", shift_no, frame_no,
                                           channel, existing)

            _require(frame["seq"] == state["pos"], "frame_not_current",
                     f"设备当前帧是 {self._current_frame_no(shift_no)}，"
                     f"不是 {frame_no}，只供当前帧",
                     current=self._current_frame_no(shift_no),
                     requested=frame_no)
            _require(self._release_row(shift_no, frame_no) is None,
                     "frame_released", f"帧 {frame_no} 已释放，不能采录",
                     frame_no=frame_no)

            raw_field = "temp_raw" if channel == "T" else "disp_raw"
            source_field = "temp_source" if channel == "T" else "disp_source"
            raw = frame[raw_field]
            source = frame[source_field]
            if "raw" in payload:
                requested_raw = self._parse_raw(payload["raw"], "raw")
                _require(requested_raw == raw, "intake_conflict",
                         "采录只能落设备当前帧的原值，不能改写",
                         frame_no=frame_no, channel=channel,
                         device_raw=raw, requested=requested_raw)
            if payload.get("source") is not None:
                requested_source = self._parse_source(payload["source"],
                                                      "source")
                _require(requested_source == source, "intake_conflict",
                         "出处取自设备当前帧，不能改写",
                         frame_no=frame_no, channel=channel,
                         device_source=source,
                         requested=requested_source)

            persisted_at = now()
            self.conn.execute(
                "INSERT INTO intakes(shift_no, frame_no, channel, raw, source,"
                " persisted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (shift_no, frame_no, channel, raw, source, persisted_at),
            )
            self._journal("intake", shift_no, frame_no, channel,
                          detail={"raw": raw, "source": source})
            return {
                "ok": True,
                "shift": shift_no,
                "frame_no": frame_no,
                "channel": channel,
                "status": "taken",
                "raw": raw,
                "state": "missing" if raw is None else "measured",
                "source": source,
                "persisted_at": persisted_at,
                "current_frame": self._current_frame_no(shift_no),
            }

    def _current_frame_no(self, shift_no):
        current = self._current(shift_no)
        return current["frame_no"] if current else None

    def _intake_result(self, status, shift_no, frame_no, channel, row):
        return {
            "ok": True,
            "shift": shift_no,
            "frame_no": frame_no,
            "channel": channel,
            "status": status,
            "raw": row["raw"],
            "state": "missing" if row["raw"] is None else "measured",
            "source": row["source"],
            "persisted_at": row["persisted_at"],
            "current_frame": self._current_frame_no(shift_no),
        }

    # --------------------------------------------------------------- release
    def release(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        failure = payload.get("simulate_failure", "none")
        _require(failure in ("none", "before", "after"), "invalid_simulation",
                 "simulate_failure 只能是 none / before / after", got=failure)

        outcome = None
        pending = None
        with db.transaction(self.conn):
            state = self._state(shift_no)
            explicit = payload.get("frame_no") is not None
            if explicit:
                requested = parse_int(payload["frame_no"], "frame_no")
                frame = self._frame_by_no(shift_no, requested)
                _require(frame is not None, "frame_not_found",
                         f"班内没有帧 {requested}", frame_no=requested)
                _require(
                    state["pending_ack"] in (None, requested),
                    "release_pending_ack",
                    "上一帧释放已生效但回执未确认：先按原帧结清，"
                    "不得越过它释放下一帧",
                    pending_frame=state["pending_ack"],
                    requested=requested,
                )
            elif state["pending_ack"] is not None:
                # an earlier release took effect but its answer was lost:
                # finish that frame first, never the next one
                requested = state["pending_ack"]
                frame = self._frame_by_no(shift_no, requested)
            else:
                frame = self._current(shift_no)
                _require(frame is not None, "no_current_frame",
                         "设备没有当前帧", shift_no=shift_no)
                requested = frame["frame_no"]

            existing = self._release_row(shift_no, requested)
            if existing is not None:
                # resume of an already settled frame: return its recorded
                # acknowledgement and leave the next frame alone
                outcome = self._release_result("already_released", shift_no,
                                               requested, existing)
                self.conn.execute(
                    "UPDATE shift_state SET pending_ack = NULL"
                    " WHERE shift_no = ? AND pending_ack = ?",
                    (shift_no, requested),
                )
            else:
                _require(frame["seq"] == state["pos"], "frame_not_current",
                         "只能续办设备当前帧；不会顺带释放下一帧",
                         current=self._current_frame_no(shift_no),
                         requested=requested)
                _require(bool(state["source_open"]), "source_closed",
                         "源已关闭，设备不能释放", shift_no=shift_no)
                _require(bool(state["bridge_open"]), "bridge_closed",
                         "桥已停止，设备不能释放", shift_no=shift_no)

                missing = [
                    channel for channel in CHANNELS
                    if self._intake_row(shift_no, requested, channel) is None
                ]
                _require(not missing, "intake_incomplete",
                         "两路原据都持久化后才能释放",
                         frame_no=requested, missing=missing)

                ack = self._release_ack(shift_no, requested)
                if failure == "before":
                    # the fault happens before the effect: nothing is written,
                    # the same frame stays current and can be resumed
                    pending = HandoffError(
                        "release_not_applied",
                        "模拟释放生效前失答：释放未生效，两路原据已持久化，"
                        "可续办原帧",
                        frame_no=requested,
                        retry=True,
                    )
                else:
                    released_at = now()
                    self.conn.execute(
                        "INSERT INTO releases(shift_no, frame_no, released_at,"
                        " ack_json) VALUES (?, ?, ?, ?)",
                        (shift_no, requested, released_at, ack),
                    )
                    next_frame = self._next_seq_no(shift_no, frame["seq"])
                    # the cursor always moves past the settled frame; if the
                    # next frame has not been loaded yet the device simply has
                    # no current frame until it is
                    self.conn.execute(
                        "UPDATE shift_state SET pos = ?, pending_ack = NULL"
                        " WHERE shift_no = ?",
                        (frame["seq"] + 1, shift_no),
                    )
                    self._journal("release", shift_no, requested,
                                  detail={"simulate_failure": failure,
                                          "ack": ack})
                    if failure == "after":
                        # the release is durable, only the answer was lost:
                        # remember it so a bare retry settles this frame and
                        # does not slip onto the next one
                        self.conn.execute(
                            "UPDATE shift_state SET pending_ack = ?"
                            " WHERE shift_no = ?",
                            (requested, shift_no),
                        )
                        pending = HandoffError(
                            "release_ack_unknown",
                            "模拟释放生效后失答：释放已持久化，请按原帧查询回执，"
                            "续办不带帧号时只结清原帧",
                            frame_no=requested,
                            recover_by="release_status",
                            current=self._current_frame_no(shift_no),
                        )
                    else:
                        outcome = {
                            "ok": True,
                            "shift": shift_no,
                            "frame_no": requested,
                            "status": "released",
                            "released_at": released_at,
                            "ack": json.loads(ack),
                            "current_frame": self._current_frame_no(shift_no),
                            "next_frame": next_frame,
                        }

        if pending is not None:
            raise pending
        return outcome

    def _release_ack(self, shift_no, frame_no) -> str:
        rows = []
        for channel in CHANNELS:
            row = self._intake_row(shift_no, frame_no, channel)
            rows.append({
                "channel": channel,
                "raw": row["raw"] if row else None,
                "source": row["source"] if row else None,
                "persisted_at": row["persisted_at"] if row else None,
                "present": row is not None,
            })
        return _canonical(rows)

    def _release_result(self, status, shift_no, frame_no, row):
        recorded = row["ack_json"]
        expected = self._release_ack(shift_no, frame_no)
        _require(recorded == expected, "release_ack_conflict",
                 "已持久化的回执与库内原值不一致，拒绝静默覆盖",
                 frame_no=frame_no)
        return {
            "ok": True,
            "shift": shift_no,
            "frame_no": frame_no,
            "status": status,
            "released_at": row["released_at"],
            "ack": json.loads(recorded),
            "current_frame": self._current_frame_no(shift_no),
        }

    def release_status(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            self._state(shift_no)
            if payload.get("frame_no") is not None:
                frame_no = parse_int(payload["frame_no"], "frame_no")
            else:
                current = self._current(shift_no)
                _require(current is not None, "no_current_frame",
                         "设备没有当前帧", shift_no=shift_no)
                frame_no = current["frame_no"]
            frame = self._frame_by_no(shift_no, frame_no)
            _require(frame is not None, "frame_not_found",
                     f"班内没有帧 {frame_no}", frame_no=frame_no)
            row = self._release_row(shift_no, frame_no)
            intakes = {}
            for channel in CHANNELS:
                taken = self._intake_row(shift_no, frame_no, channel)
                if taken is not None:
                    intakes[channel] = {
                        "raw": taken["raw"],
                        "state": ("missing" if taken["raw"] is None
                                  else "measured"),
                        "source": taken["source"],
                        "persisted_at": taken["persisted_at"],
                    }
            ack = json.loads(row["ack_json"]) if row else None
            state = self._state(shift_no)
            if row is not None and state["pending_ack"] == frame_no:
                # the operator has now seen the durable acknowledgement
                self.conn.execute(
                    "UPDATE shift_state SET pending_ack = NULL"
                    " WHERE shift_no = ?", (shift_no,),
                )
            return {
                "ok": True,
                "shift": shift_no,
                "frame_no": frame_no,
                "released": row is not None,
                "released_at": row["released_at"] if row else None,
                "ack": ack,
                "ack_matches_intakes": (
                    row is not None
                    and row["ack_json"] == self._release_ack(shift_no, frame_no)
                ),
                "intakes": intakes,
                "current_frame": self._current_frame_no(shift_no),
                "pending_ack": (
                    None if (row is not None and state["pending_ack"] == frame_no)
                    else state["pending_ack"]
                ),
            }

    # --------------------------------------------------------------- archive
    @staticmethod
    def _strip_volatile(evidence):
        """Drop fields that change between calls, so the same item is stable."""

        if not isinstance(evidence, dict):
            return evidence
        volatile = {
            "first_execution_token", "first_execution", "token",
            "submitted_at", "status", "reclaimed", "claimed_at",
        }
        return {key: value for key, value in evidence.items()
                if key not in volatile}

    def _evidence_for(self, shift_no, frame_no, channel) -> dict:
        settings = self.conn.execute(
            "SELECT * FROM channels WHERE shift_no = ? AND channel = ?",
            (shift_no, channel),
        ).fetchone()
        taken = self._intake_row(shift_no, frame_no, channel)
        a = Fraction(settings["cal_a"])
        b = Fraction(settings["cal_b"])
        value = None
        display = None
        if taken["raw"] is not None:
            value = format_rational(apply_calibration(a, b, taken["raw"]))
            display = f"{value} {settings['unit']}"
        return {
            "shift": shift_no,
            "frame_no": frame_no,
            "channel": channel,
            "unit": settings["unit"],
            "calibration": {"a": format_rational(a), "b": format_rational(b)},
            "raw": taken["raw"],
            "value": value,
            "value_display": display,
            "state": "missing" if taken["raw"] is None else "measured",
            "source": taken["source"],
        }

    def prepare(self, payload: dict) -> dict:
        """Generate the per-channel archive items from the frozen settings."""

        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            self._state(shift_no)
            frame_no = self._resolve_frame(payload, shift_no)
            items = []
            missing_intake = []
            for channel in CHANNELS:
                if self._intake_row(shift_no, frame_no, channel) is None:
                    missing_intake.append(channel)
                    continue
                item = self._evidence_for(shift_no, frame_no, channel)
                archived = self.conn.execute(
                    "SELECT token FROM archive_items WHERE shift_no = ?"
                    " AND frame_no = ? AND channel = ?",
                    (shift_no, frame_no, channel),
                ).fetchone()
                item["first_execution_token"] = (
                    archived["token"] if archived else None
                )
                items.append(item)
            return {
                "ok": True,
                "shift": shift_no,
                "frame_no": frame_no,
                "items": items,
                "missing_intake": missing_intake,
            }

    def _resolve_frame(self, payload, shift_no) -> int:
        if payload.get("frame_no") is not None:
            frame_no = parse_int(payload["frame_no"], "frame_no")
            _require(self._frame_by_no(shift_no, frame_no) is not None,
                     "frame_not_found", f"班内没有帧 {frame_no}",
                     frame_no=frame_no)
            return frame_no
        current = self._current(shift_no)
        _require(current is not None, "no_current_frame",
                 "设备没有当前帧", shift_no=shift_no)
        return current["frame_no"]

    def archive_submit(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        _require(isinstance(shift_no, str) and shift_no.strip(),
                 "invalid_shift", "shift 必须是非空字符串")
        frame_no = parse_int(payload.get("frame_no"), "frame_no")
        channel = payload.get("channel")
        _require(channel in CHANNELS, "invalid_channel",
                 "channel 必须是 T 或 D", channel=channel)
        failure = payload.get("simulate_failure", False)
        if isinstance(failure, str) and failure.lower() in ("none", "false"):
            failure = False
        _require(isinstance(failure, bool), "invalid_simulation",
                 "simulate_failure 必须是布尔值", got=failure)

        with db.transaction(self.conn):
            state = self._state(shift_no)
            _require(bool(state["archive_open"]), "archive_closed",
                     "归档已关闭，不能投件", shift_no=shift_no)
            _require(self._frame_by_no(shift_no, frame_no) is not None,
                     "frame_not_found", f"班内没有帧 {frame_no}",
                     frame_no=frame_no)
            taken = self._intake_row(shift_no, frame_no, channel)
            _require(taken is not None, "intake_missing",
                     "该班帧路尚未采录，不能投件",
                     frame_no=frame_no, channel=channel)

            if "evidence" in payload:
                evidence = self._strip_volatile(payload["evidence"])
                _require(isinstance(evidence, dict), "invalid_evidence",
                         "evidence 必须是对象", frame_no=frame_no)
                _require(evidence.get("channel", channel) == channel,
                         "archive_conflict",
                         "evidence 的通道与提交通道不一致",
                         frame_no=frame_no, channel=channel,
                         evidence_channel=evidence.get("channel"))
                _require(evidence.get("frame_no", frame_no) == frame_no,
                         "archive_conflict",
                         "evidence 的帧号与提交帧号不一致",
                         frame_no=frame_no, evidence_frame=evidence.get("frame_no"))
            else:
                evidence = self._evidence_for(shift_no, frame_no, channel)
            canonical = _canonical(evidence)
            token = "x1-" + hashlib.sha256(
                f"{shift_no}\x00{frame_no}\x00{channel}\x00{canonical}".encode()
            ).hexdigest()[:32]

            existing = self.conn.execute(
                "SELECT * FROM archive_items WHERE shift_no = ? AND frame_no = ?"
                " AND channel = ?",
                (shift_no, frame_no, channel),
            ).fetchone()
            if existing is not None:
                _require(existing["evidence"] == canonical, "archive_conflict",
                         "同一班帧路已按不同凭据投件，拒绝覆盖",
                         frame_no=frame_no, channel=channel)
                self._journal("archive_reclaim", shift_no, frame_no, channel,
                              detail={"token": existing["token"]})
                return {
                    "ok": True,
                    "shift": shift_no,
                    "frame_no": frame_no,
                    "channel": channel,
                    "status": "reclaimed",
                    "token": existing["token"],
                    "first_execution": True,
                    "submitted_at": existing["submitted_at"],
                }

            if failure:
                raise HandoffError(
                    "archive_not_applied",
                    "模拟投件失败：本次未入档、未签发首执，可补交",
                    frame_no=frame_no, channel=channel,
                    retry=True,
                )

            submitted_at = now()
            self.conn.execute(
                "INSERT INTO archive_items(shift_no, frame_no, channel,"
                " evidence, token, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (shift_no, frame_no, channel, canonical, token, submitted_at),
            )
            self._journal("archive_submit", shift_no, frame_no, channel,
                          detail={"token": token})
            return {
                "ok": True,
                "shift": shift_no,
                "frame_no": frame_no,
                "channel": channel,
                "status": "archived",
                "token": token,
                "first_execution": True,
                "submitted_at": submitted_at,
            }

    def archive_status(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            state = self._state(shift_no)
            frames = []
            frame_rows = self.conn.execute(
                "SELECT frame_no FROM device_frames WHERE shift_no = ?"
                " ORDER BY seq", (shift_no,),
            ).fetchall()
            for frame in frame_rows:
                frame_no = frame["frame_no"]
                items = {}
                for row in self.conn.execute(
                    "SELECT * FROM archive_items WHERE shift_no = ?"
                    " AND frame_no = ?", (shift_no, frame_no),
                ):
                    items[row["channel"]] = {
                        "token": row["token"],
                        "submitted_at": row["submitted_at"],
                    }
                handoff = self._handoff_row(shift_no, frame_no)
                frames.append({
                    "frame_no": frame_no,
                    "channels": items,
                    "complete": set(items) == set(CHANNELS),
                    "handoff_no": handoff["handoff_no"] if handoff else None,
                })
            return {
                "ok": True,
                "shift": shift_no,
                "archive_open": bool(state["archive_open"]),
                "frames": frames,
            }

    # --------------------------------------------------------------- handoff
    def handoff(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            state = self._state(shift_no)
            frame_no = self._resolve_frame(payload, shift_no)
            existing = self._handoff_row(shift_no, frame_no)
            if existing is not None:
                return {
                    "ok": True,
                    "shift": shift_no,
                    "frame_no": frame_no,
                    "status": "issued",
                    "handoff_no": existing["handoff_no"],
                    "issued_at": existing["issued_at"],
                    "fixed": True,
                }

            _require(bool(state["archive_open"]), "archive_closed",
                     "归档已关闭，不能签发交班号", shift_no=shift_no)
            archived = {
                row["channel"]
                for row in self.conn.execute(
                    "SELECT channel FROM archive_items WHERE shift_no = ?"
                    " AND frame_no = ?", (shift_no, frame_no),
                )
            }
            missing = [channel for channel in CHANNELS
                       if channel not in archived]
            if missing:
                return {
                    "ok": True,
                    "shift": shift_no,
                    "frame_no": frame_no,
                    "status": "waiting",
                    "missing": missing,
                    "message": "缺一路实收，待齐后再签发交班号",
                }

            handoff_no = "H-" + hashlib.sha256(
                f"{shift_no}\x00{frame_no}".encode()
            ).hexdigest()[:16].upper()
            issued_at = now()
            self.conn.execute(
                "INSERT INTO handoffs(handoff_no, shift_no, frame_no, issued_at)"
                " VALUES (?, ?, ?, ?)",
                (handoff_no, shift_no, frame_no, issued_at),
            )
            self._journal("handoff", shift_no, frame_no,
                          detail={"handoff_no": handoff_no})
            return {
                "ok": True,
                "shift": shift_no,
                "frame_no": frame_no,
                "status": "issued",
                "handoff_no": handoff_no,
                "issued_at": issued_at,
                "fixed": True,
            }

    # ----------------------------------------------------------------- close
    def close_source(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            state = self._state(shift_no)
            _require(bool(state["source_open"]) or bool(state["bridge_open"])
                     or bool(state["archive_open"]), "already_closed",
                     "源、桥与归档均已关闭", shift_no=shift_no)
            self.conn.execute(
                "UPDATE shift_state SET source_open = 0, bridge_open = 0,"
                " archive_open = 0 WHERE shift_no = ?", (shift_no,),
            )
            self._journal("close_source", shift_no)
            return {
                "ok": True,
                "shift": shift_no,
                "source_open": False,
                "bridge_open": False,
                "archive_open": False,
            }

    def open_archive(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            self._state(shift_no)
            self.conn.execute(
                "UPDATE shift_state SET archive_open = 1 WHERE shift_no = ?",
                (shift_no,),
            )
            self._journal("open_archive", shift_no)
            return {"ok": True, "shift": shift_no, "archive_open": True}

    def close_archive(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            self._state(shift_no)
            self.conn.execute(
                "UPDATE shift_state SET archive_open = 0 WHERE shift_no = ?",
                (shift_no,),
            )
            self._journal("close_archive", shift_no)
            return {"ok": True, "shift": shift_no, "archive_open": False}

    # ----------------------------------------------------------------- claim
    def claim(self, payload: dict) -> dict:
        handoff_no = payload.get("handoff_no")
        _require(isinstance(handoff_no, str) and handoff_no.strip(),
                 "invalid_handoff_no", "handoff_no 必须是非空字符串")

        with db.transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM handoffs WHERE handoff_no = ?", (handoff_no,)
            ).fetchone()
            if row is None:
                raise HandoffError(
                    "handoff_not_found",
                    f"交班号 {handoff_no} 未找到",
                    handoff_no=handoff_no,
                )
            state = self._state(row["shift_no"])
            _require(bool(state["archive_open"]), "archive_closed",
                     "归档已关闭，不能凭交班号领件", handoff_no=handoff_no)
            channels = {}
            for channel in CHANNELS:
                existing_claim = self.conn.execute(
                    "SELECT * FROM claims WHERE handoff_no = ? AND channel = ?",
                    (handoff_no, channel),
                ).fetchone()
                token_row = self.conn.execute(
                    "SELECT token FROM archive_items WHERE shift_no = ?"
                    " AND frame_no = ? AND channel = ?",
                    (row["shift_no"], row["frame_no"], channel),
                ).fetchone()
                _require(token_row is not None, "archive_incomplete",
                         "该交班号的归档不齐，不能领件", handoff_no=handoff_no,
                         channel=channel)
                if existing_claim is None:
                    claimed_at = now()
                    self.conn.execute(
                        "INSERT INTO claims(handoff_no, channel, token,"
                        " claimed_at) VALUES (?, ?, ?, ?)",
                        (handoff_no, channel, token_row["token"], claimed_at),
                    )
                    first_claim = True
                    token = token_row["token"]
                    self._journal("claim", row["shift_no"], row["frame_no"],
                                  channel, detail={"handoff_no": handoff_no})
                else:
                    claimed_at = existing_claim["claimed_at"]
                    token = existing_claim["token"]
                    first_claim = False
                channels[channel] = self._deliver(
                    row["shift_no"], row["frame_no"], channel, token,
                    claimed_at, first_claim,
                )
            return {
                "ok": True,
                "status": "delivered",
                "handoff_no": handoff_no,
                "shift": row["shift_no"],
                "frame_no": row["frame_no"],
                "issued_at": row["issued_at"],
                "channels": channels,
            }

    def _deliver(self, shift_no, frame_no, channel, token, claimed_at,
                 first_claim):
        settings = self.conn.execute(
            "SELECT * FROM channels WHERE shift_no = ? AND channel = ?",
            (shift_no, channel),
        ).fetchone()
        taken = self._intake_row(shift_no, frame_no, channel)
        a = Fraction(settings["cal_a"])
        b = Fraction(settings["cal_b"])
        raw = taken["raw"]
        if raw is None:
            exact = None
            display = None
            delivered_state = "missing"
        else:
            exact = format_rational(apply_calibration(a, b, raw))
            display = f"{exact} {settings['unit']}"
            delivered_state = "measured"
        return {
            "channel": channel,
            "name": settings["name"],
            "unit": settings["unit"],
            "calibration": {
                "a": format_rational(a),
                "b": format_rational(b),
                "formula": calibration_formula(a, b),
            },
            "raw": raw,
            "state": delivered_state,
            "value": exact,
            "value_display": display,
            "source": taken["source"],
            "first_execution_token": token,
            "first_claim": first_claim,
            "claimed_at": claimed_at,
        }

    # ---------------------------------------------------------------- status
    def status(self, payload: dict) -> dict:
        shift_no = payload.get("shift")
        with db.transaction(self.conn):
            state = self._state(shift_no)
            loaded = []
            for row in self.conn.execute(
                "SELECT * FROM device_frames WHERE shift_no = ? ORDER BY seq",
                (shift_no,),
            ):
                loaded.append({
                    "seq": row["seq"],
                    "frame_no": row["frame_no"],
                    "temperature": row["temp_raw"],
                    "displacement": row["disp_raw"],
                    "temp_source": row["temp_source"],
                    "disp_source": row["disp_source"],
                })
            intakes = {}
            for row in self.conn.execute(
                "SELECT * FROM intakes WHERE shift_no = ?"
                " ORDER BY frame_no, channel", (shift_no,),
            ):
                intakes.setdefault(str(row["frame_no"]), {})[row["channel"]] = {
                    "raw": row["raw"],
                    "source": row["source"],
                }
            releases = {
                str(row["frame_no"]): row["released_at"]
                for row in self.conn.execute(
                    "SELECT * FROM releases WHERE shift_no = ?", (shift_no,)
                )
            }
            handoffs = {
                str(row["frame_no"]): row["handoff_no"]
                for row in self.conn.execute(
                    "SELECT * FROM handoffs WHERE shift_no = ?", (shift_no,)
                )
            }
            current = self._current(shift_no)
            return {
                "ok": True,
                "shift": shift_no,
                "frozen": self._describe_channels(shift_no),
                "source_open": bool(state["source_open"]),
                "bridge_open": bool(state["bridge_open"]),
                "archive_open": bool(state["archive_open"]),
                "current_frame": current["frame_no"] if current else None,
                "pending_ack": state["pending_ack"],
                "loaded": loaded,
                "intakes": intakes,
                "releases": releases,
                "handoffs": handoffs,
            }
