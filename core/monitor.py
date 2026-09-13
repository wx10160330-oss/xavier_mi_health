"""mi_health 主动关怀后台监测模块。

核心逻辑:
  - 定时轮询云端数据(默认 15 分钟)
  - 维护滚动 7 天基线
  - 用 z-score / 绝对阈值检测异常
  - 命中后调 LLM 生成关怀文案并主动发送到白名单会话

限制:
  - 云端数据只在用户打开小米健康 App 后才刷新, 无法真正实时
  - 用"数据陈旧保护"避免误报
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, date as _date, time as _time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger


# ============================================================
# 基线存储
# ============================================================
class BaselineStore:
    """滚动 7 天基线 + 冷却时间戳持久化。

    结构:
      {
        "history": [
          {"date": "YYYY-MM-DD", "max_hr": 120, "min_hr": 55, "avg_hr": 75,
           "sleep_score": 82, "spo2": 97, "steps": 5000},
          ...
        ],
        "cooldowns": {
          "heart_rate_high": 1700000000.0,
          "heart_rate_low":  0,
          "sleep":           0,
          "spo2":            0,
          "late_night":      0,
          "lazy_step":       0
        },
        "last_care_ts": 1700000000.0
      }
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"history": [], "cooldowns": {}, "last_care_ts": 0.0}
        self._lock = asyncio.Lock()

    async def load(self):
        try:
            if self.path.exists():
                text = self.path.read_text(encoding="utf-8")
                self._data = json.loads(text) if text.strip() else self._data
                self._data.setdefault("history", [])
                self._data.setdefault("cooldowns", {})
                self._data.setdefault("last_care_ts", 0.0)
        except Exception as e:
            logger.warning(f"[mi_health.monitor] 读取 baseline 失败(忽略): {e}")

    async def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[mi_health.monitor] 写入 baseline 失败: {e}")

    async def append_daily(self, day: dict, keep_days: int = 7):
        """追加/覆盖某天的指标, 保留最近 keep_days 天。"""
        async with self._lock:
            date_str = day.get("date")
            if not date_str:
                return
            history = self._data.setdefault("history", [])
            # 去掉同日期的旧记录再追加
            history[:] = [h for h in history if h.get("date") != date_str]
            history.append(day)
            # 按日期排序 + 截断
            history.sort(key=lambda h: h.get("date", ""))
            self._data["history"] = history[-keep_days:]
            await self._save()

    def history(self) -> list[dict]:
        return list(self._data.get("history", []))

    def days_collected(self) -> int:
        return len(self._data.get("history", []))

    def get_cooldown(self, key: str) -> float:
        return float(self._data.get("cooldowns", {}).get(key, 0.0))

    async def set_cooldown(self, key: str, ts: float):
        async with self._lock:
            self._data.setdefault("cooldowns", {})[key] = ts
            await self._save()

    async def touch_care_ts(self):
        async with self._lock:
            self._data["last_care_ts"] = time.time()
            await self._save()


# ============================================================
# 快照 → 每日指标 提取
# ============================================================
def extract_daily_metrics(snapshot: dict) -> Optional[dict]:
    """从 snapshot 提取用于基线的关键数值。所有字段可能为 None。"""
    if not snapshot:
        return None

    def _val(obj, keys):
        if obj is None:
            return None
        if isinstance(obj, list):
            if not obj:
                return None
            # 睡眠等可能返回多条，取 time 最大的一条
            def _t(x):
                if isinstance(x, dict):
                    return x.get("time") or 0
                return getattr(x, "time", 0) or 0
            try:
                obj = sorted([x for x in obj if x], key=_t, reverse=True)[0]
            except Exception:
                obj = obj[0]
        for k in keys:
            v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
            if v is not None:
                return v
        return None

    hr = snapshot.get("heart_rate")
    sleep = snapshot.get("sleep")
    spo2 = snapshot.get("spo2")
    steps = snapshot.get("steps")

    # 解析 spo2 值
    spo2_val = None
    if spo2 is not None:
        # 1. 直接是数字
        if isinstance(spo2, (int, float)):
            spo2_val = int(spo2)
        # 2. dict / 对象, 优先取 spo2 / value 属性
        else:
            raw = None
            for attr in ("spo2", "value", "avg", "latest"):
                v = getattr(spo2, attr, None) if not isinstance(spo2, dict) else spo2.get(attr)
                if v is not None:
                    raw = v
                    break
            if isinstance(raw, (int, float)):
                spo2_val = int(raw)
            elif raw is not None:
                # 从字符串里抠数字, 只接受合理范围 [70, 100]
                import re
                for m in re.finditer(r"\d{2,3}", str(raw)):
                    n = int(m.group())
                    if 70 <= n <= 100:
                        spo2_val = n
                        break
        # 最终校验: 血氧生理合理值 70~100, 超出直接丢弃
        if spo2_val is not None and not (70 <= spo2_val <= 100):
            spo2_val = None

    day = {
        "date": snapshot.get("date"),
        "max_hr": _val(hr, ["max_hr"]),
        "min_hr": _val(hr, ["min_hr"]),
        "avg_hr": _val(hr, ["avg_hr"]),
        "latest_hr": None,
        "sleep_score": _val(sleep, ["score", "sleep_score"]),
        "sleep_duration": _val(sleep, ["duration", "total_duration"]),
        "spo2": spo2_val,
        "steps": _val(steps, ["steps"]),
    }

    # 最新心率(用于熬夜判断)
    latest_obj = _val(hr, ["latest_hr"])
    if latest_obj is not None:
        day["latest_hr"] = getattr(latest_obj, "bpm", None) or (
            latest_obj.get("bpm") if isinstance(latest_obj, dict) else None
        )

    return day


def snapshot_timestamp(snapshot: dict) -> Optional[float]:
    """尝试从 snapshot 提取"最新数据时间戳"(用于陈旧判断)。

    优先看心率的 latest_hr 时间, 拿不到就返回 None(视为无效)。
    """
    if not snapshot:
        return None
    hr = snapshot.get("heart_rate")
    if isinstance(hr, list):
        hr = hr[0] if hr else None
    if hr is None:
        return None
    latest = getattr(hr, "latest_hr", None) or (hr.get("latest_hr") if isinstance(hr, dict) else None)
    if latest is None:
        return None
    for key in ("timestamp", "time", "ts", "datetime"):
        v = getattr(latest, key, None) or (latest.get(key) if isinstance(latest, dict) else None)
        if v is None:
            continue
        # datetime / int / str
        if isinstance(v, datetime):
            return v.timestamp()
        if isinstance(v, (int, float)):
            # 猜测毫秒
            return float(v) / 1000 if v > 1e12 else float(v)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v).timestamp()
            except Exception:
                continue
    return None


# ============================================================
# 静默时段判断
# ============================================================
def _parse_hm(s: str) -> Optional[_time]:
    try:
        h, m = str(s).strip().split(":")
        return _time(int(h), int(m))
    except Exception:
        return None


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """判断 now 是否在 [start, end) 静默时段内, 支持跨天(如 23:00-07:00)。"""
    s = _parse_hm(start)
    e = _parse_hm(end)
    if not s or not e:
        return False
    cur = now.time()
    if s <= e:
        return s <= cur < e
    # 跨天
    return cur >= s or cur < e


# ============================================================
# 统计工具
# ============================================================
def _mean_std(values: list[float]) -> tuple[float, float]:
    xs = [float(v) for v in values if v is not None]
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, var ** 0.5


def _zscore(x: float, mean: float, std: float) -> float:
    if std <= 0:
        return 0.0
    return (x - mean) / std


# ============================================================
# 主 Monitor
# ============================================================
class HealthMonitor:
    """后台监测 + 主动关怀。"""

    def __init__(self, plugin, config: dict, data_dir: Path):
        self.plugin = plugin  # 回引 MiHealthPlugin
        self.config = config or {}
        self.data_dir = Path(data_dir)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.store = BaselineStore(self.data_dir / "baseline.json")
        self._last_user_msg_ts: dict[str, float] = {}

    # ---------- 生命周期 ----------
    async def start(self):
        if not self.config.get("monitor_enabled", False):
            logger.info("[mi_health.monitor] 主动关怀总开关关闭, 不启动")
            return
        await self.store.load()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="mi_health.monitor")
        logger.info(
            f"[mi_health.monitor] 已启动 (interval={self.config.get('check_interval', 900)}s, "
            f"days_collected={self.store.days_collected()})"
        )

    async def stop(self):
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                logger.warning(f"[mi_health.monitor] 停止时异常: {e}")
        self._task = None
        logger.info("[mi_health.monitor] 已停止")

    # ---------- 记录最近对话时间 ----------
    def touch_user_activity(self, session_id: str):
        if session_id:
            self._last_user_msg_ts[session_id] = time.time()

    def _in_chat_window(self, session_id: str, gap_minutes: int) -> bool:
        ts = self._last_user_msg_ts.get(session_id)
        if not ts:
            return False
        return (time.time() - ts) < gap_minutes * 60

    # ---------- 主循环 ----------
    async def _run(self):
        interval = int(self.config.get("check_interval", 900))
        # 启动后延后 30 秒执行首次, 避免和插件其他初始化打架
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[mi_health.monitor] tick 异常: {e}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    async def _tick(self):
        # 1. 目标会话
        sessions = list(self.plugin.allowed_session_ids or [])
        if not sessions:
            return

        # 2. 静默时段
        now = datetime.now()
        if self.config.get("quiet_hours_enabled", True):
            if in_quiet_hours(
                now,
                self.config.get("quiet_hours_start", "23:00"),
                self.config.get("quiet_hours_end", "07:00"),
            ):
                return

        # 3. 拉数据 (强制刷新, 但会走 SDK 缓存/云端限速)
        snap = await self.plugin._fetch_snapshot()
        if not snap:
            return

        # 4. 数据陈旧保护
        ts = snapshot_timestamp(snap)
        stale_hours = int(self.config.get("stale_threshold_hours", 2))
        if ts is not None:
            if (time.time() - ts) > stale_hours * 3600:
                logger.debug("[mi_health.monitor] 数据过期, 跳过")
                return

        # 5. 更新基线
        day = extract_daily_metrics(snap)
        if day and day.get("date"):
            await self.store.append_daily(
                day, keep_days=int(self.config.get("baseline_days", 7))
            )

        # 6. 基线建立期（仅影响需要基线的 z-score 检测；绝对阈值检测下面单独处理）
        required_days = int(self.config.get("baseline_days", 7))
        baseline_ready = self.store.days_collected() >= required_days
        if not baseline_ready:
            logger.debug(
                f"[mi_health.monitor] 基线建立期 "
                f"({self.store.days_collected()}/{required_days}), 仅做绝对阈值检测"
            )

        # 7. 异常判定
        alerts = self._detect(day, baseline_ready=baseline_ready)
        
        # 7.1 生理期关怀检测 (不需要等待 7 天基线)
        if getattr(self.plugin, "period_mgr", None) and self.config.get("period_enabled", True):
            try:
                from .period_care import detect_period_alerts
                period_alerts = detect_period_alerts(
                    self.plugin.period_mgr, self.config, self.store
                )
                alerts.extend(period_alerts)
            except Exception as e:
                logger.warning(f"[mi_health.monitor] 生理期关怀检测失败: {e}")

        if not alerts:
            return

        # 8. 冷却过滤
        alerts = [a for a in alerts if not self._in_cooldown(a["key"])]
        if not alerts:
            return

        # 9. 对每个会话发送(会话隔离 + 聊天空窗)
        chat_gap = int(self.config.get("recent_chat_gap_minutes", 10))
        for sid in sessions:
            # 会话级插件启用判断
            try:
                from astrbot.core.plugin.session_plugin_manager import (
                    SessionPluginManager,
                )
                if not SessionPluginManager.is_plugin_enabled_for_session(
                    plugin_name=self.plugin.name, session_id=sid
                ):
                    continue
            except Exception:
                pass

            if self._in_chat_window(sid, chat_gap):
                logger.debug(f"[mi_health.monitor] 会话 {sid} 在聊天窗口, 跳过")
                continue

            # 只发第一个未冷却的关怀(避免刷屏)
            alert = alerts[0]
            ok = await self._dispatch_care(sid, alert)
            if ok:
                await self.store.set_cooldown(alert["key"], time.time())
                await self.store.touch_care_ts()
                logger.info(
                    f"[mi_health.monitor] 关怀已发送 session={sid} key={alert['key']}"
                )

    # ---------- 检测 ----------
    def _detect(self, today: dict, baseline_ready: bool = True) -> list[dict]:
        history = [h for h in self.store.history() if h.get("date") != today.get("date")]
        alerts: list[dict] = []

        # 心率 (需要基线)
        if baseline_ready and self.config.get("heart_rate_care_enabled", True):
            max_hrs = [h["max_hr"] for h in history if h.get("max_hr") is not None]
            if today.get("max_hr") is not None and len(max_hrs) >= 3:
                mean, std = _mean_std(max_hrs)
                z = _zscore(today["max_hr"], mean, std)
                th = float(self.config.get("heart_rate_zscore", 2.0))
                if z >= th:
                    alerts.append({
                        "key": "heart_rate_high",
                        "type": "心率偏高",
                        "hint": f"她今天最高心率 {today['max_hr']} bpm, 比平时基线偏高",
                    })
                elif z <= -th:
                    alerts.append({
                        "key": "heart_rate_low",
                        "type": "心率偏低",
                        "hint": f"她今天最高心率 {today['max_hr']} bpm, 比平时基线偏低",
                    })

        # 睡眠 (需要基线)
        if baseline_ready and self.config.get("sleep_care_enabled", True):
            scores = [h["sleep_score"] for h in history if h.get("sleep_score") is not None]
            if today.get("sleep_score") is not None and len(scores) >= 3:
                mean, std = _mean_std(scores)
                z = _zscore(today["sleep_score"], mean, std)
                th = float(self.config.get("sleep_zscore", 1.5))
                if z <= -th:
                    alerts.append({
                        "key": "sleep",
                        "type": "睡眠偏差",
                        "hint": "宝宝昨晚睡得不太好, 评分比平时低",
                    })

        # 血氧
        if self.config.get("spo2_care_enabled", True):
            abs_th = int(self.config.get("spo2_abs_threshold", 93))
            spo2 = today.get("spo2")
            if spo2 is not None and spo2 < abs_th:
                alerts.append({
                    "key": "spo2",
                    "type": "血氧偏低",
                    "hint": f"血氧只有 {spo2}% 有点低",
                })

        # 熬夜提醒
        if self.config.get("late_night_care_enabled", False):
            now = datetime.now()
            late_h, late_m = self._parse_hm_tuple(self.config.get("late_night_hour", "23:30"))
            end_h, end_m = self._parse_hm_tuple(self.config.get("late_night_end_hour", "05:00"))
            # 熬夜时段: 从 late_night_hour 到次日 late_night_end_hour(跨天)
            after_start = (now.hour > late_h) or (now.hour == late_h and now.minute >= late_m)
            before_end = (now.hour < end_h) or (now.hour == end_h and now.minute < end_m)
            in_late_night = after_start or before_end
            if in_late_night:
                latest_hr = today.get("latest_hr")
                avg_hrs = [h["avg_hr"] for h in history if h.get("avg_hr") is not None]
                mean, _ = _mean_std(avg_hrs)
                if latest_hr and mean > 0 and latest_hr > mean:
                    alerts.append({
                        "key": "late_night",
                        "type": "熬夜警告",
                        "hint": "这么晚心跳还这么活跃, 该睡啦",
                    })

        # 步数偷懒
        if self.config.get("lazy_step_enabled", False):
            now = datetime.now()
            check_h, check_m = self._parse_hm_tuple(self.config.get("lazy_step_check_hour", "20:00"))
            if (now.hour, now.minute) >= (check_h, check_m):
                th = int(self.config.get("lazy_step_threshold", 2000))
                steps = today.get("steps")
                if steps is not None and steps < th:
                    alerts.append({
                        "key": "lazy_step",
                        "type": "步数偏少",
                        "hint": f"到现在才 {steps} 步, 一天都没怎么动",
                    })

        return alerts

    @staticmethod
    def _parse_hm_tuple(s: str) -> tuple[int, int]:
        try:
            h, m = str(s).strip().split(":")
            return int(h), int(m)
        except Exception:
            return 23, 30

    def _in_cooldown(self, key: str) -> bool:
        hours_map = {
            "heart_rate_high": int(self.config.get("heart_rate_cooldown_hours", 4)),
            "heart_rate_low": int(self.config.get("heart_rate_cooldown_hours", 4)),
            "sleep": int(self.config.get("sleep_cooldown_hours", 8)),
            "spo2": int(self.config.get("spo2_cooldown_hours", 4)),
            "late_night": int(self.config.get("late_night_cooldown_hours", 6)),
            "lazy_step": 20,  # 步数偷懒: 20 小时 ≈ 每天最多一次
        }
        hours = hours_map.get(key, 4)
        last = self.store.get_cooldown(key)
        return (time.time() - last) < hours * 3600

    # ---------- 兜底文案 ----------
    def _fallback_text(self, alert: dict) -> str:
        key = alert.get("key", "")
        # 用户自定义兜底文案(留空则用默认)
        custom_map = {
            "heart_rate_high": self.config.get("fallback_hr_high"),
            "heart_rate_low": self.config.get("fallback_hr_low"),
            "sleep": self.config.get("fallback_sleep"),
            "spo2": self.config.get("fallback_spo2"),
            "late_night": self.config.get("fallback_late_night"),
            "lazy_step": self.config.get("fallback_lazy_step"),
            "period_incoming": self.config.get("fallback_period_incoming"),
            "period_care": self.config.get("fallback_period_care"),
            "period_ovulation": self.config.get("fallback_period_ovulation"),
        }
        custom = (custom_map.get(key) or "").strip()
        if custom:
            return custom

        # 默认兜底文案
        table = {
            "heart_rate_high": "宝宝, 心跳有点乱, 是遇到什么事了吗?",
            "heart_rate_low": "宝宝, 心跳有点慢, 感觉还好吗?",
            "sleep": "看你昨晚没睡好, 今天悠着点, 少喝一杯咖啡~",
            "spo2": "深呼吸下宝宝, 血氧有点低",
            "late_night": "这么晚啦, 该躺下休息啦~",
            "lazy_step": "今天一点没动喔, 起来溜达一圈嘛?",
            "period_incoming": "宝宝, 好像快到日子啦, 包里带片吧~",
            "period_care": "宝宝, 今天肚子还疼不, 想喝点热的吗?",
            "period_ovulation": "宝宝, 今天多喝温水, 别太累啦~",
        }
        return table.get(key, "宝宝, 感觉最近状态怎么样呀?")

    # ---------- 分派与发送 ----------
    def _build_care_prompt(self, alert: dict) -> str:
        """为注入方案构造一条伪造的用户消息 prompt。
        注入后交由 AstrBot 的 pipeline 用当前会话人设处理并自然回复。
        """
        hint = alert.get("hint", "")
        key = alert.get("key", "")
        template = self.config.get("care_prompt_template") or (
            "你刚扫到她的身体数据，注意到这件事：\n"
            "「{hint}」\n"
            "请结合你们的对话上下文和你当前的状态，用你自己的口吻自然地关心她，不要生硬复读上面的内容，也不要暴露「系统消息」这几个字。"
        )
        try:
            return template.format(
                hint=hint,
                key=key,
                time=datetime.now().strftime("%H:%M"),
                date=datetime.now().strftime("%Y-%m-%d"),
            )
        except Exception:
            return (
                f"你刚扫到她的身体数据，注意到这件事：\n"
                f"「{hint}」\n"
                f"请结合你们的对话上下文和你当前的状态，用你自己的口吻自然地关心她，不要生硬复读上面的内容，也不要暴露「系统消息」这几个字。"
            )

    async def _dispatch_care(self, session_id: str, alert: dict) -> bool:
        """分派关怀：
        1. 优先走 OneBot 伪造消息注入（抄自 wakeup/reminder，走完整 pipeline，当前人格自然回复并存入上下文）
        2. 注入失败再回退到静态兜底文案直发
        """
        prompt = self._build_care_prompt(alert)
        try:
            ok = await self._inject_as_user(session_id, prompt)
            if ok:
                logger.info(
                    f"[mi_health.monitor] 伪造关怀消息已成功注入 pipeline session={session_id}"
                )
                return True
            logger.warning(
                f"[mi_health.monitor] 注入失败, 回退到静态兜底文案直发"
            )
        except Exception as e:
            logger.warning(
                f"[mi_health.monitor] 注入异常, 回退到静态兜底文案直发: {e}"
            )

        # 方案 B: 回退 —— 走静态兜底文案直发
        text = self._fallback_text(alert)
        if not text:
            return False
        return await self._send_direct(session_id, text)

    async def _send_direct(self, session_id: str, text: str) -> bool:
        """直接以 Bot 身份把文案发给用户（旧方式，回退用）。"""
        try:
            from astrbot.api.event import MessageChain
            chain = MessageChain().message(text)
            ok = await self.plugin.context.send_message(session_id, chain)
            return bool(ok)
        except Exception as e:
            logger.exception(f"[mi_health.monitor] 直发失败 session={session_id}: {e}")
            return False

    async def _send(self, session_id: str, text: str) -> bool:
        """向后兼容的直发入口"""
        return await self._send_direct(session_id, text)

    async def _inject_as_user(self, session_id: str, text: str) -> bool:
        """把关怀 prompt 伪造成一条"用户发来的消息"注入到 CQHttp 事件流，
        直接使用 wakeup / reminder 的成熟做法：
        CQEvent.from_payload + cqhttp_bot.handle_event。

        让 AstrBot 走完整 pipeline（当前会话人格 + 上下文 + 所有 hook）。

        Args:
            session_id: unified_msg_origin, 形如 `xavier:FriendMessage:2652497429`
            text: 已经渲染好的关怀文案（作为伪造用户消息的正文）

        Returns:
            True  = 已成功注入到事件队列
            False = 注入失败
        """
        try:
            from aiocqhttp import Event as CQEvent
        except ImportError:
            logger.warning("[mi_health.monitor] 未安装 aiocqhttp，无法注入")
            return False

        # 1. 拆 umo，无论平台前缀叫什么（比如 xavier / aiocqhttp / onebot）
        parts = session_id.rsplit(":", 2)
        if len(parts) < 3:
            logger.warning(f"[mi_health.monitor] 无法解析 session_id: {session_id}")
            return False
        platform_id, msg_type_str, raw_sid = parts[0], parts[1], parts[2]
        is_group = "Group" in msg_type_str

        # 2. 获取 cqhttp_bot 实例（遍历 context 的所有 platform manager 寻找带 send_private_msg 的 bot）
        cqhttp_bot = None
        try:
            ctx = self.plugin.context
            for mgr_name in [
                "platform_manager", "_platform_manager", "platform_mgr", "_platform_mgr"
            ]:
                mgr = getattr(ctx, mgr_name, None)
                if mgr is None:
                    continue
                for list_name in [
                    "platforms", "platform_insts", "_platforms", "adapters"
                ]:
                    plist = getattr(mgr, list_name, None)
                    if not plist or not hasattr(plist, "__iter__"):
                        continue
                    for p in plist:
                        bot = getattr(p, "bot", None)
                        if bot and hasattr(bot, "send_private_msg"):
                            cqhttp_bot = bot
                            break
                    if cqhttp_bot:
                        break
                if cqhttp_bot:
                    break
        except Exception as e:
            logger.debug(f"[mi_health.monitor] 搜索 CQHttp 实例失败: {e}")

        if cqhttp_bot is None:
            logger.warning("[mi_health.monitor] 未搜索到 CQHttp 实例，无法注入")
            return False

        # 3. 获取机器人 QQ 号 (优先从 bot 实例拿，拿不到尝试看配置)
        bot_qq_id = ""
        try:
            info = await cqhttp_bot.get_login_info()
            qq = str(info.get("user_id", ""))
            if qq and qq != "0":
                bot_qq_id = qq
        except Exception as e:
            logger.debug(f"[mi_health.monitor] get_login_info 失败: {e}")

        if not bot_qq_id:
            bot_qq_id = str(self.config.get("bot_qq_id") or "").strip()

        if not bot_qq_id:
            logger.warning("[mi_health.monitor] 未检测到机器人 QQ 号（可在配置中配置 bot_qq_id）")
            return False

        # 4. 构造 CQEvent payload
        if is_group:
            if "_" in raw_sid:
                uid, gid = raw_sid.rsplit("_", 1)
            else:
                gid = raw_sid
                uid = bot_qq_id or raw_sid
            payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": int(time.time()) % 2147483647,
                "group_id": int(gid),
                "user_id": int(uid),
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
                "font": 0,
                "sender": {
                    "user_id": int(uid),
                    "nickname": "mi_health",
                    "card": "",
                },
                "time": int(time.time()),
                "self_id": int(bot_qq_id),
            }
        else:
            payload = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": int(time.time()) % 2147483647,
                "user_id": int(raw_sid),
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
                "font": 0,
                "sender": {
                    "user_id": int(raw_sid),
                    "nickname": "mi_health",
                    "sex": "unknown",
                    "age": 0,
                },
                "time": int(time.time()),
                "self_id": int(bot_qq_id),
            }

        fake_event = CQEvent.from_payload(payload)
        if fake_event is None:
            logger.warning("[mi_health.monitor] CQEvent.from_payload 返回 None")
            return False

        # 5. 调用 handler 注入 pipeline
        handler = getattr(cqhttp_bot, "_handle_event", None)
        if handler is None:
            handler = getattr(cqhttp_bot, "handle_event", None)
        if handler is None:
            logger.warning("[mi_health.monitor] CQHttp 无可用 handle_event 方法")
            return False

        await handler(fake_event)
        logger.info(f"[mi_health.monitor] 📨 OneBot 伪造消息已成功注入 pipeline | session={session_id}")
        return True

    # ---------- 手动测试 ----------
    async def dry_run(self) -> dict:
        """手动触发一次判定, 只返回诊断信息, 不真发消息。"""
        snap = await self.plugin._fetch_snapshot()
        if not snap:
            return {"ok": False, "reason": "no snapshot"}
        day = extract_daily_metrics(snap)
        baseline_ready = self.store.days_collected() >= int(self.config.get("baseline_days", 7))
        alerts = self._detect(day, baseline_ready=baseline_ready) if day else []
        return {
            "ok": True,
            "days_collected": self.store.days_collected(),
            "baseline_ready": baseline_ready,
            "today": day,
            "alerts": alerts,
            "in_quiet": in_quiet_hours(
                datetime.now(),
                self.config.get("quiet_hours_start", "23:00"),
                self.config.get("quiet_hours_end", "07:00"),
            ),
        }

    async def _tick_force(self, session_id: str | None = None) -> None:
        """强制走一次推送流程 (忽略冷却/聊天窗口/静默时段, 用于测试)。"""
        # 1. 目标会话
        sessions = list(self.plugin.allowed_session_ids or [])
        if session_id and session_id not in sessions:
            sessions.append(session_id)
        if not sessions:
            raise RuntimeError("no target session")

        # 2. 拉数据
        snap = await self.plugin._fetch_snapshot()
        if not snap:
            raise RuntimeError("no snapshot")

        # 3. 更新基线 (顺便)
        day = extract_daily_metrics(snap)
        if day and day.get("date"):
            await self.store.append_daily(
                day, keep_days=int(self.config.get("baseline_days", 7))
            )

        # 4. 检测 (强制视为基线就绪, 让 z-score 也参与; 若历史不足 z-score 内部自己会跳过)
        baseline_ready = self.store.days_collected() >= int(self.config.get("baseline_days", 7))
        alerts = self._detect(day, baseline_ready=True) if day else []
        if not alerts:
            raise RuntimeError("no alerts detected (今天各项都正常，没啥好推的)")

        # 5. 直接发第一个 alert (跳过冷却)
        alert = alerts[0]
        for sid in sessions:
            ok = await self._dispatch_care(sid, alert)
            logger.info(f"[mi_health.monitor] 强制推送 session={sid} key={alert['key']} ok={ok}")
