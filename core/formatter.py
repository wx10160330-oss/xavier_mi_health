"""把 SDK 原始数据格式化成人话/上下文注入片段。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _fmt_minutes(m: Optional[int]) -> str:
    if not m or not isinstance(m, (int, float)):
        return "-"
    m = int(m)
    h = m // 60
    mm = m % 60
    if h and mm:
        return f"{h}小时{mm}分"
    if h:
        return f"{h}小时"
    return f"{mm}分"


def _extract_data_time(data: Any) -> Optional[str]:
    """从数据对象里提取"数据更新时间"，格式化为 HH:MM。

    优先级:
      1. latest_hr.time (最新心率采样时间戳，最能反映"新鲜度")
      2. 对象.at 属性 (datetime)
      3. 对象.time 字段 (unix 时间戳)
    返回 None 表示没拿到。
    """
    if data is None:
        return None
    # list -> 取第一条
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]

    def _get(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    # 优先看 latest_hr.time 或 latest_hr.at (心率数据独有)
    latest = _get(data, "latest_hr")
    if latest is not None:
        at = _get(latest, "at")
        if isinstance(at, datetime):
            return at.astimezone().strftime("%H:%M")
        ts = _get(latest, "time")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
            except Exception:
                pass

    # 次选: 对象自身的 at / time
    at = _get(data, "at")
    if isinstance(at, datetime):
        return at.astimezone().strftime("%H:%M")

    ts = _get(data, "time")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            dt = datetime.fromtimestamp(int(ts))
            # time 常常是"当天0点"的时间戳，那样意义不大，只在非0点时才返回
            if dt.hour != 0 or dt.minute != 0:
                return dt.strftime("%H:%M")
        except Exception:
            pass
    return None


def format_steps(data: Any) -> str:
    if not data:
        return "步数: 暂无数据"
    # data 可能是 list[StepData] 或单个对象
    if isinstance(data, list):
        if not data:
            return "步数: 暂无数据"
        data = data[0]

    steps = getattr(data, "steps", None) or (data.get("steps") if isinstance(data, dict) else None)
    dist = getattr(data, "distance", None) or (data.get("distance") if isinstance(data, dict) else None)
    cal = getattr(data, "calories", None) or (data.get("calories") if isinstance(data, dict) else None)

    parts = [f"步数 {steps}" if steps is not None else "步数 -"]
    if dist is not None:
        try:
            d = float(dist)
            if d > 100:
                parts.append(f"{d/1000:.2f}km")
            else:
                parts.append(f"{d:.2f}km")
        except Exception:
            parts.append(f"{dist}")
    if cal is not None:
        parts.append(f"{cal}kcal")
    return "今日 " + " · ".join(parts)


def format_heart_rate(data: Any) -> str:
    if not data:
        return "心率: 暂无数据"
    if isinstance(data, list):
        if not data:
            return "心率: 暂无数据"
        data = data[0]

    latest_obj = getattr(data, "latest_hr", None) or (data.get("latest_hr") if isinstance(data, dict) else None)
    latest = getattr(latest_obj, "bpm", None) if latest_obj else None
    avg = getattr(data, "avg_hr", None) or (data.get("avg_hr") if isinstance(data, dict) else None)
    resting = getattr(data, "avg_rhr", None) or (data.get("avg_rhr") if isinstance(data, dict) else None)
    hi = getattr(data, "max_hr", None) or (data.get("max_hr") if isinstance(data, dict) else None)
    lo = getattr(data, "min_hr", None) or (data.get("min_hr") if isinstance(data, dict) else None)

    parts = []
    if latest is not None:
        parts.append(f"最新{latest}bpm")
    if avg is not None:
        parts.append(f"平均{avg}")
    if resting is not None:
        parts.append(f"静息{resting}")
    if hi is not None:
        parts.append(f"最高{hi}")
    if lo is not None:
        parts.append(f"最低{lo}")
    if not parts:
        return "心率: 暂无数据"
    return "心率 " + " / ".join(parts)


def format_sleep(data: Any) -> str:
    if not data:
        return "睡眠: 暂无数据"
    if isinstance(data, list):
        if not data:
            return "睡眠: 暂无数据"
        # 取最新一条（time 最大），避免选中白天小憩
        def _get_time(x):
            return getattr(x, "time", None) if not isinstance(x, dict) else x.get("time", 0)
        try:
            data = sorted([x for x in data if x], key=lambda x: _get_time(x) or 0, reverse=True)[0]
        except Exception:
            data = data[0]

    def _g(obj, *keys):
        for k in keys:
            v = None
            if isinstance(obj, dict):
                v = obj.get(k)
            else:
                v = getattr(obj, k, None)
            if v is not None:
                return v
        return None

    dur = _g(data, "duration", "total_duration")
    score = _g(data, "score", "sleep_score")
    deep = _g(data, "deep_sleep_duration", "sleep_deep_duration", "deep_sleep")
    light = _g(data, "light_sleep_duration", "sleep_light_duration", "light_sleep")
    segments = _g(data, "segment_details")

    parts = []
    
    # 尝试提取入睡/醒来时间(从 segment_details 最新一条)
    bedtime_str, wakeup_str = None, None
    if segments and isinstance(segments, list) and len(segments) > 0:
        # 取最新一段(通常就一条主睡眠)
        seg = segments[0] if not isinstance(segments[0], dict) else segments[0]
        bedtime_ts = _g(seg, "bedtime")
        wakeup_ts = _g(seg, "wake_up_time")
        try:
            from datetime import datetime
            if bedtime_ts and isinstance(bedtime_ts, (int, float)) and bedtime_ts > 0:
                bedtime_str = datetime.fromtimestamp(int(bedtime_ts)).strftime("%H:%M")
            if wakeup_ts and isinstance(wakeup_ts, (int, float)) and wakeup_ts > 0:
                wakeup_str = datetime.fromtimestamp(int(wakeup_ts)).strftime("%H:%M")
        except Exception:
            pass

    # 组装: 入睡→醒来 时长
    if bedtime_str and wakeup_str:
        parts.append(f"{bedtime_str}→{wakeup_str}")
    if dur is not None:
        parts.append(_fmt_minutes(dur))
    if score is not None:
        parts.append(f"评分{score}")
    if deep is not None:
        parts.append(f"深睡{_fmt_minutes(deep)}")
    if light is not None:
        parts.append(f"浅睡{_fmt_minutes(light)}")

    if not parts:
        return "睡眠: 暂无数据"
    return "睡眠 " + " · ".join(parts)


def format_spo2(data: Any) -> str:
    if not data:
        return ""
    # Spo2(98%)
    val = getattr(data, "spo2", None) or str(data)
    if "Spo2" in str(val):
        return f"血氧 {str(val).replace('Spo2(', '').replace(')', '')}"
    return f"血氧 {val}"


def format_vitality(data: Any) -> str:
    """活力指标(中高强度活动时长)。"""
    if not data:
        return ""
    # 尝试从对象或 dict 里取字段
    minutes = None
    for key in ("minutes", "duration", "value", "intensity"):
        v = getattr(data, key, None) if not isinstance(data, dict) else data.get(key)
        if v is not None:
            minutes = v
            break
    if minutes is None:
        return f"活力 {data}"
    try:
        m = int(minutes)
        return f"活力 中高强度{_fmt_minutes(m)}"
    except Exception:
        return f"活力 {minutes}"


def format_full(snapshot: dict) -> str:
    date = snapshot.get("date", "")
    lines = [f"📊 今日健康快照 · {date}"]
    lines.append("👣 " + format_steps(snapshot.get("steps")))
    lines.append("❤️ " + format_heart_rate(snapshot.get("heart_rate")))
    lines.append("😴 " + format_sleep(snapshot.get("sleep")))
    spo2 = format_spo2(snapshot.get("spo2"))
    if spo2:
        lines.append("🫁 " + spo2)
    vit = format_vitality(snapshot.get("vitality"))
    if vit:
        lines.append("🌟 " + vit)
    return "\n".join(lines)


def format_period_status(period_status):
    """把 PeriodManager.get_current_status() 结果格式化为一段简洁描述。
    只在有值得告知 LLM 的信息时返回字符串, 否则返回 None。
    """
    if not period_status or not isinstance(period_status, dict):
        return None

    in_period = period_status.get("in_period")
    in_period_pred = period_status.get("in_period_predicted")
    period_day = period_status.get("period_day") or 0
    avg_period_days = period_status.get("avg_period_days") or 0
    days_to_next = period_status.get("days_to_next")
    in_ovu = period_status.get("in_ovulation")
    in_ovu_day = period_status.get("in_ovulation_day")

    # 实际经期 > 预测经期 > 排卵日 > 排卵期 > 即将来临
    if in_period and period_day > 0:
        base = f"🩸 【当前处于生理期第 {period_day} 天】"
        if avg_period_days:
            base += f"(平时约{avg_period_days}天)"
        base += "。严禁吃冰/喝冷饮/生冷辛辣！她如果说想喝冰的或不舒服，必须立刻温柔阻止并提醒她还在经期，多关心照顾她。"
        return base

    if in_period_pred and period_day > 0:
        return (
            f"🩸 根据周期推算, 今天可能是经期第 {period_day} 天 "
            "(她还未手动记录, 请在合适时机温柔确认一下)。"
        )

    if in_ovu_day:
        return "🌸 今日为预测排卵日, 身体可能略敏感。"

    if in_ovu:
        return "🌸 处于预测排卵期, 关注身体变化。"

    if isinstance(days_to_next, int) and 0 <= days_to_next <= 3:
        if days_to_next == 0:
            return "🩸 预测生理期今天就会来, 可以提前提醒她备好卫生用品。"
        return f"🩸 预测距下次生理期还有 {days_to_next} 天, 请留意她的身体状态。"

    return None


def format_for_llm(snapshot: dict, max_length: int = 400, period_status: Optional[dict] = None) -> str:
    date = (snapshot or {}).get("date", "")
    now_str = datetime.now().strftime("%H:%M")
    lines = [f"[用户当前健康快照 · {date} · 当前时间{now_str}]"]

    def _with_time(line: str, data: Any) -> str:
        t = _extract_data_time(data)
        return f"{line} (数据更新于{t})" if t else line

    # 生理期状态: 极其重要，放在最前面，防止被截断
    period_line = format_period_status(period_status)
    if period_line:
        lines.insert(1, period_line)

    if snapshot:
        steps_data = snapshot.get("steps")
        steps_line = format_steps(steps_data)
        if "暂无" not in steps_line:
            lines.append(_with_time(steps_line, steps_data))

        hr_data = snapshot.get("heart_rate")
        hr_line = format_heart_rate(hr_data)
        if "暂无" not in hr_line:
            lines.append(_with_time(hr_line, hr_data))

        sleep_data = snapshot.get("sleep")
        sleep_line = format_sleep(sleep_data)
        if "暂无" not in sleep_line:
            lines.append(_with_time(sleep_line, sleep_data))

        spo2_data = snapshot.get("spo2")
        spo2 = format_spo2(spo2_data)
        if spo2:
            lines.append(_with_time(spo2, spo2_data))

        vit_data = snapshot.get("vitality")
        vit = format_vitality(vit_data)
        if vit:
            lines.append(_with_time(vit, vit_data))

    lines.append("(以上为背景常识，不要刻意机械汇报，在日常闲聊、关心或对应场景下自然带出。)")
    text = "\n".join(lines)
    if len(text) > max_length:
        text = text[: max_length - 3] + "..."
    return text
