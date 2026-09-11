"""生理期关怀触发器。

判定 3 种触发:
  1. period_incoming   - 距下次经期 <= N 天,提前预告
  2. period_care        - 处于经期第 X~Y 天,每隔 N 天关心一次
  3. period_ovulation   - 排卵日/排卵期(默认关闭)
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger


def _in_time_window(now: datetime, start_hm: str, end_hm: str) -> bool:
    """判断当前时间是否在 [start_hm, end_hm] 之间。"""
    try:
        sh, sm = [int(x) for x in start_hm.split(":")]
        eh, em = [int(x) for x in end_hm.split(":")]
        cur_min = now.hour * 60 + now.minute
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if start_min <= end_min:
            return start_min <= cur_min <= end_min
        # 跨天(极少用)
        return cur_min >= start_min or cur_min <= end_min
    except Exception:
        return True


def detect_period_alerts(period_manager, config: dict, cooldown_store) -> list[dict]:
    """基于当前状态检测生理期关怀。返回 alerts 列表(可能空)。

    alert 结构:
      {"key": str, "hint": str, "type": "period"}
    """
    alerts = []
    if not period_manager:
        return alerts

    status = period_manager.get_current_status()
    now = datetime.now()

    # -------- 1. 经期预告 --------
    if config.get("period_incoming_care_enabled", True):
        days_ahead = int(config.get("period_incoming_days_ahead", 2))
        cooldown_h = int(config.get("period_incoming_cooldown_hours", 24))
        time_start = str(config.get("period_incoming_time_start", "12:00"))
        time_end = str(config.get("period_incoming_time_end", "22:00"))
        days_to_next = status.get("days_to_next")

        # 只在未开始 & 距离很近 & 时间段允许时触发
        if (
            days_to_next is not None
            and 0 < days_to_next <= days_ahead
            and not status.get("in_period")
            and _in_time_window(now, time_start, time_end)
            and not _in_cooldown(cooldown_store, "period_incoming", cooldown_h)
        ):
            alerts.append({
                "key": "period_incoming",
                "hint": f"她是我女朋友, 好像快来月经了, 大概还有 {days_to_next} 天",
                "type": "period",
            })

    # -------- 2. 经期中关怀 --------
    if config.get("period_care_enabled", True) and status.get("in_period"):
        gap_days = int(config.get("period_care_gap_days", 2))
        cooldown_h = int(config.get("period_care_cooldown_hours", 36))
        time_start = str(config.get("period_care_time_start", "13:00"))
        time_end = str(config.get("period_care_time_end", "22:00"))
        days_range = str(config.get("period_care_days_range", "1-5"))
        period_day = status.get("period_day", 0)

        try:
            lo, hi = [int(x) for x in days_range.split("-")]
        except Exception:
            lo, hi = 1, 5

        # 判断: 在经期 day 范围内, 且不在冷却, 且时段允许
        # gap_days 通过 cooldown = gap_days * 24 实现（冷却更宽松）
        effective_cooldown = max(cooldown_h, gap_days * 24 - 2)
        if (
            lo <= period_day <= hi
            and _in_time_window(now, time_start, time_end)
            and not _in_cooldown(cooldown_store, "period_care", effective_cooldown)
        ):
            alerts.append({
                "key": "period_care",
                "hint": f"她是我女朋友, 今天是月经期第 {period_day} 天, 想自然地关心她一句",
                "type": "period",
            })

    # -------- 3. 排卵日/期(默认关) --------
    if config.get("period_ovulation_care_enabled", False):
        cooldown_h = int(config.get("period_ovulation_cooldown_hours", 24))
        time_start = str(config.get("period_ovulation_time_start", "13:00"))
        time_end = str(config.get("period_ovulation_time_end", "22:00"))
        if (
            status.get("in_ovulation_day")
            and _in_time_window(now, time_start, time_end)
            and not _in_cooldown(cooldown_store, "period_ovulation", cooldown_h)
        ):
            alerts.append({
                "key": "period_ovulation",
                "hint": "她是我女朋友, 今天是她的排卵日, 想自然地关心她一句",
                "type": "period",
            })

    return alerts


def _in_cooldown(store, key: str, hours: int) -> bool:
    """检查是否在冷却期内。store 需实现 get_cooldown(key)->timestamp。"""
    if not store or hours <= 0:
        return False
    try:
        last = store.get_cooldown(key)
        return (time.time() - last) < hours * 3600
    except Exception:
        return False
