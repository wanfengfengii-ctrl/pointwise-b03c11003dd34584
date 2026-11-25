"""Narrated walk-through: ``python3 -m handoff.demo``.

Runs the F7/F8 case, then closes the source (断源) and claims both channels by
handoff number from the reopened archive.
"""

from __future__ import annotations

import json
import sys

from .scenario import run_case


def show(title: str, value) -> None:
    print(f"\n== {title}")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def main() -> int:
    store, log = run_case()
    try:
        print("材料台架双路交班 —— F7/F8 演示")
        print(f"库文件: {store.path}")
        show("开班：两路单位与有理定标冻结", log["open"]["frozen"])
        show("装载设备替身（F7=0/null, F8=4/9）", log["load"])
        show("采录 F7 温度 0（出处 T7）", log["intake_f7_t"])
        show("采录 F7 位移 null（出处 D7，缺测也持久化）", log["intake_f7_d"])
        show("释放 F7，生效后失答", log["release_lost_ack"])
        show("按原帧查询回执：释放已持久化，F8 已到当前位", {
            "released": log["release_status"]["released"],
            "current_frame": log["release_status"]["current_frame"],
            "ack": log["release_status"]["ack"],
        })
        show("采录 F8 位移 9（出处 D8）——F7 两路未投档", log["intake_f8_d"])
        show("按冻结口径生成 F7 分路投件", log["prepare_f7"]["items"])
        show("归档 F7 温度成功，签发首执", log["archive_f7_t"])
        show("F7 缺位移实收，交班待齐", log["handoff_wait"])
        show("归档 F8 位移（另一班帧）", log["archive_f8_d"])
        show("F8 缺温度实收，同样待齐", log["handoff_f8_wait"])
        show("F7 位移投件失败，仍待齐", log["archive_f7_d_fail"])
        show("补交 F7 位移", log["archive_f7_d"])
        show("两路齐备，固定交班号", log["handoff_fixed"])

        # 断源领件
        from .scenario import claim_after_cutoff

        claim_after_cutoff(store, log)
        show("关源停桥，归档随之关闭；此时领件被拒", log["claim_while_shut"])
        show("重开归档", log["open_archive"])
        show("凭交班号领两路原值、换算、单位、出处与首执",
             _summary(log["claim"]))
        show("同件复领：原执/原值不变", _summary(log["claim_again"]))
        show("未知交班号", log["claim_unknown"])
        print("\n演示结束：断源后仍可凭交班号取回两路。")
        return 0
    finally:
        store.cleanup()


def _summary(claim: dict) -> dict:
    return {
        "handoff_no": claim["handoff_no"],
        "frame_no": claim["frame_no"],
        "channels": {
            channel: {
                "raw": data["raw"],
                "state": data["state"],
                "value": data["value"],
                "display": data["value_display"],
                "unit": data["unit"],
                "source": data["source"],
                "token": data["first_execution_token"],
                "first_claim": data["first_claim"],
            }
            for channel, data in claim["channels"].items()
        },
    }


if __name__ == "__main__":
    sys.exit(main())
