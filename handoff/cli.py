"""Command-line front end: ``python3 -m handoff <store> <command>``.

Every command exchanges one JSON object on stdin/``--in`` and answers with one
JSON object on stdout.  Business rejections print ``{"ok": false, "error":
{...}}`` and exit with status 1; malformed invocations exit with status 2.

Examples
--------
    $ python3 -m handoff shift.db open-shift --in shift.json
    $ python3 -m handoff shift.db load --json \
        '{"shift": "S1", "frames": [[7, 0, null, "T7", "D7"]]}'
    $ echo '{"shift": "S1", "channel": "T"}' | python3 -m handoff shift.db intake
    $ python3 -m handoff shift.db release --shift S1
    $ python3 -m handoff shift.db claim --handoff-no H-XXXX
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .errors import HandoffError
from .service import Service

COMMANDS = {
    "open-shift": "open_shift",
    "开班": "open_shift",
    "load": "load_frames",
    "装载": "load_frames",
    "current": "current_frame",
    "当前帧": "current_frame",
    "intake": "intake",
    "采录": "intake",
    "release": "release",
    "释放": "release",
    "release-status": "release_status",
    "释放回执": "release_status",
    "prepare": "prepare",
    "投件": "prepare",
    "archive-submit": "archive_submit",
    "归档": "archive_submit",
    "archive-status": "archive_status",
    "归档状态": "archive_status",
    "handoff": "handoff",
    "交班": "handoff",
    "close-source": "close_source",
    "关源": "close_source",
    "open-archive": "open_archive",
    "开归档": "open_archive",
    "close-archive": "close_archive",
    "关归档": "close_archive",
    "claim": "claim",
    "领件": "claim",
    "status": "status",
    "状态": "status",
}

FLAGS = {
    "shift": "shift",
    "frame": "frame_no",
    "channel": "channel",
    "raw": "raw",
    "source": "source",
    "handoff_no": "handoff_no",
    "start_frame": "start_frame",
    "simulate_failure": "simulate_failure",
}

INT_FIELDS = {"frame_no", "start_frame"}
CHANNELS = {"T": "T", "D": "D", "温度": "T", "位移": "D"}


def resolve_store(store: str) -> str:
    if store in (":memory:", "-"):
        return store
    if os.path.isdir(store):
        return os.path.join(store, "handoff.sqlite3")
    if not os.path.splitext(store)[1]:
        return os.path.join(store, "handoff.sqlite3")
    return store


def read_payload(args) -> dict:
    payload: dict = {}
    if args.inline is not None:
        source = args.inline
        if source.startswith("@"):
            with open(source[1:], "r", encoding="utf-8") as handle:
                source = handle.read()
        payload.update(_parse_json_object(source, "--json"))
    elif args.input is not None:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as handle:
                text = handle.read()
        if text.strip():
            payload.update(_parse_json_object(text, "--in"))
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            payload.update(_parse_json_object(text, "stdin"))

    for flag, field in FLAGS.items():
        value = getattr(args, flag)
        if value is None:
            continue
        if field == "channel":
            _require_channel(value)
            value = CHANNELS[value]
        elif field in INT_FIELDS:
            value = _to_int(value, flag)
        elif field == "raw":
            value = _to_raw(value)
        elif field == "simulate_failure":
            value = _to_simulation(value)
        payload[field] = value
    return payload


def _to_raw(value):
    lowered = value.strip().lower()
    if lowered in ("null", "none", "缺测"):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _to_simulation(value):
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "1"):
        return True
    if lowered in ("false", "no", "0"):
        return False
    return lowered  # "none" for release, or an unrecognised value


def _parse_json_object(text: str, origin: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{origin} 不是合法 JSON: {exc}") from None
    if not isinstance(value, dict):
        raise UsageError(f"{origin} 必须是 JSON 对象")
    return value


def _require_channel(value: str) -> None:
    if value not in CHANNELS:
        raise UsageError("--channel 只能是 T/D、温度/位移")


def _to_int(value, flag: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise UsageError(f"--{flag} 必须是整数") from None


class UsageError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m handoff",
        description="Material bench dual-channel shift handoff",
    )
    parser.add_argument("store", help="SQLite file or a directory holding one")
    parser.add_argument("command", help=", ".join(sorted(COMMANDS)))
    parser.add_argument("--in", dest="input", metavar="FILE",
                        help="JSON payload file ('-' = stdin, default)")
    parser.add_argument("--json", dest="inline", metavar="TEXT",
                        help="inline JSON payload (or @file)")
    for flag in FLAGS:
        parser.add_argument(f"--{flag.replace('_', '-')}", dest=""
                            + flag, default=None)
    parser.add_argument("--pretty", action="store_true",
                        help="indent the JSON answer")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        method_name = COMMANDS.get(args.command)
        if method_name is None:
            raise UsageError(
                f"未知操作 {args.command}；可用: {', '.join(sorted(COMMANDS))}"
            )
        payload = read_payload(args)
    except UsageError as exc:
        json.dump({"ok": False,
                   "error": {"code": "usage", "message": str(exc)}},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2

    store = resolve_store(args.store)
    try:
        with Service(store) as service:
            if method_name == "current_frame":
                method = service.status
            else:
                method = getattr(service, method_name)
            result = method(payload)
    except HandoffError as exc:
        answer = {"ok": False, "error": exc.to_dict()}
        _emit(answer, args.pretty)
        return 1
    except Exception as exc:  # defensive: never print a traceback to the wire
        answer = {"ok": False,
                  "error": {"code": "internal_error", "message": str(exc)}}
        _emit(answer, args.pretty)
        return 1

    if method_name == "current_frame":
        result = {
            "ok": True,
            "shift": result["shift"],
            "current_frame": result["current_frame"],
            "frozen": result["frozen"],
        }
    _emit(result, args.pretty)
    return 0 if result.get("ok", True) else 1


def _emit(answer: dict, pretty: bool) -> None:
    json.dump(answer, sys.stdout, ensure_ascii=False,
              indent=2 if pretty else None)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
