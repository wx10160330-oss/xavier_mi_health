"""生理期记录与预测。

数据结构 period.json:
{
    "periods": [
        {"start": "2026-09-09", "end": null, "notes": {}},
        {"start": "2026-08-12", "end": "2026-08-17", "notes": {"2026-08-13": "痛经"}}
    ],
    "settings": {"avg_cycle_days": 28, "avg_period_days": 5}
}
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional

from astrbot.api import logger


DEFAULT_CYCLE = 28
DEFAULT_LENGTH = 5


class PeriodManager:
    """生理期数据管理。"""

    def __init__(self, data_dir: Path, default_cycle: int = 28, default_length: int = 5):
        self.dir = data_dir / "period"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "period.json"
        self.default_cycle = default_cycle
        self.default_length = default_length
        self._data = self._load()

    # ---------- 读写 ----------
    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "periods": [],
                "settings": {
                    "avg_cycle_days": self.default_cycle,
                    "avg_period_days": self.default_length,
                },
            }
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.exception(f"[mi_health.period] 读取失败: {e}")
            return {
                "periods": [],
                "settings": {
                    "avg_cycle_days": self.default_cycle,
                    "avg_period_days": self.default_length,
                },
            }

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception(f"[mi_health.period] 保存失败: {e}")

    # ---------- 记录操作 ----------
    def mark_start(self, date_str: str) -> dict:
        """标记某天开始。若已有未结束的，先结束它。"""
        periods = self._data["periods"]
        # 若有未结束的，用今天作为其结束（避免覆盖）
        for p in periods:
            if p.get("end") is None and p.get("start") != date_str:
                # 若新 start 比旧 start 还早或相同，跳过
                pass
        # 去重
        for p in periods:
            if p.get("start") == date_str:
                return {"ok": True, "msg": "已存在", "period": p}
        new_p = {"start": date_str, "end": None, "notes": {}}
        periods.append(new_p)
        # 按 start 倒序
        periods.sort(key=lambda x: x.get("start", ""), reverse=True)
        self._recalc_settings()
        self._save()
        return {"ok": True, "msg": "已标记开始", "period": new_p}

    def mark_end(self, date_str: str) -> dict:
        """标记结束（结束最近一个未结束的周期）。"""
        periods = self._data["periods"]
        # 找最近未结束的
        for p in periods:
            if p.get("end") is None:
                p["end"] = date_str
                self._recalc_settings()
                self._save()
                return {"ok": True, "msg": "已标记结束", "period": p}
        return {"ok": False, "msg": "没有正在进行的周期"}

    def delete(self, start_date: str) -> dict:
        """删除某条记录（按 start 定位）。"""
        before = len(self._data["periods"])
        self._data["periods"] = [
            p for p in self._data["periods"] if p.get("start") != start_date
        ]
        after = len(self._data["periods"])
        if after < before:
            self._recalc_settings()
            self._save()
            return {"ok": True, "msg": "已删除"}
        return {"ok": False, "msg": "未找到"}

    def add_note(self, date_str: str, note: str) -> dict:
        """给某天加备注。"""
        # 找到该日期所在的周期
        for p in self._data["periods"]:
            start = p.get("start")
            end = p.get("end") or datetime.now().strftime("%Y-%m-%d")
            if start <= date_str <= end:
                notes = p.setdefault("notes", {})
                if note.strip():
                    notes[date_str] = note.strip()
                else:
                    notes.pop(date_str, None)
                self._save()
                return {"ok": True, "msg": "已保存备注"}
        return {"ok": False, "msg": "该日期不在任何周期内"}

    # ---------- 统计与预测 ----------
    def _recalc_settings(self):
        """根据最近 3 次周期重算平均值。"""
        periods = sorted(
            [p for p in self._data["periods"] if p.get("start")],
            key=lambda x: x["start"],
        )
        # 只取最近 4 条计算 3 段间隔
        recent = periods[-4:]
        cycle_diffs = []
        length_days = []
        for i in range(len(recent) - 1):
            try:
                s1 = datetime.strptime(recent[i]["start"], "%Y-%m-%d").date()
                s2 = datetime.strptime(recent[i + 1]["start"], "%Y-%m-%d").date()
                diff = (s2 - s1).days
                if 14 <= diff <= 60:  # 合理范围
                    cycle_diffs.append(diff)
            except Exception:
                pass
        for p in recent:
            if p.get("start") and p.get("end"):
                try:
                    s = datetime.strptime(p["start"], "%Y-%m-%d").date()
                    e = datetime.strptime(p["end"], "%Y-%m-%d").date()
                    d = (e - s).days + 1
                    if 1 <= d <= 14:
                        length_days.append(d)
                except Exception:
                    pass

        s = self._data.setdefault("settings", {})
        # 如果用户手动改过，就不再自动重算覆盖
        manual = s.get("manual_override", False)
        if manual:
            return
        if cycle_diffs:
            s["avg_cycle_days"] = round(sum(cycle_diffs) / len(cycle_diffs))
        else:
            s["avg_cycle_days"] = self.default_cycle
        if length_days:
            s["avg_period_days"] = round(sum(length_days) / len(length_days))
        else:
            s["avg_period_days"] = self.default_length

    def get_last_period(self) -> Optional[dict]:
        """最近一次记录。"""
        periods = sorted(
            [p for p in self._data["periods"] if p.get("start")],
            key=lambda x: x["start"],
            reverse=True,
        )
        return periods[0] if periods else None

    def predict_next(self) -> Optional[date]:
        """预测下次经期开始日。"""
        last = self.get_last_period()
        if not last:
            return None
        try:
            last_start = datetime.strptime(last["start"], "%Y-%m-%d").date()
            cycle = self._data["settings"].get("avg_cycle_days", self.default_cycle)
            return last_start + timedelta(days=cycle)
        except Exception:
            return None

    def predict_ovulation(self) -> Optional[tuple[date, date, date]]:
        """预测排卵日 & 排卵期(前5后4)。返回 (start, ovulation_day, end)。"""
        next_period = self.predict_next()
        if not next_period:
            return None
        ovu_day = next_period - timedelta(days=14)
        return (
            ovu_day - timedelta(days=5),
            ovu_day,
            ovu_day + timedelta(days=4),
        )

    def get_current_status(self) -> dict:
        """当前状态: 是否在经期/排卵期/预告期。"""
        today = datetime.now().date()
        last = self.get_last_period()
        result = {
            "today": today.isoformat(),
            "in_period": False,
            "period_day": 0,          # 经期第几天
            "in_period_predicted": False,
            "in_ovulation": False,
            "in_ovulation_day": False,
            "days_to_next": None,
            "next_period": None,
            "avg_cycle_days": self._data["settings"].get("avg_cycle_days", self.default_cycle),
            "avg_period_days": self._data["settings"].get("avg_period_days", self.default_length),
        }

        if last:
            try:
                last_start = datetime.strptime(last["start"], "%Y-%m-%d").date()
                last_end_raw = last.get("end")
                # 判断是否在当前经期
                if last_end_raw:
                    last_end = datetime.strptime(last_end_raw, "%Y-%m-%d").date()
                    if last_start <= today <= last_end:
                        result["in_period"] = True
                        result["period_day"] = (today - last_start).days + 1
                else:
                    # 未结束：如果今天在最近 start ~ start+14 内，视为经期
                    if last_start <= today <= last_start + timedelta(days=14):
                        result["in_period"] = True
                        result["period_day"] = (today - last_start).days + 1
            except Exception:
                pass

        # 预测数据
        nxt = self.predict_next()
        if nxt:
            result["next_period"] = nxt.isoformat()
            result["days_to_next"] = (nxt - today).days
            # 预测经期区间
            avg_len = result["avg_period_days"]
            if nxt <= today <= nxt + timedelta(days=avg_len - 1):
                if not result["in_period"]:  # 实际经期优先
                    result["in_period_predicted"] = True
                    result["period_day"] = (today - nxt).days + 1

        # 排卵期
        ovu = self.predict_ovulation()
        if ovu:
            start, day, end = ovu
            if start <= today <= end:
                result["in_ovulation"] = True
            if today == day:
                result["in_ovulation_day"] = True

        return result

    def get_calendar_data(self, year: int, month: int) -> dict:
        """获取某个月的日历标记数据。返回 {date_iso: type}。"""
        marks = {}
        # 遍历所有 period 记录
        for p in self._data["periods"]:
            start_raw = p.get("start")
            end_raw = p.get("end")
            if not start_raw:
                continue
            try:
                start = datetime.strptime(start_raw, "%Y-%m-%d").date()
                # 如果没有 end：用 start + avg_period_days - 1 或 今天(取较早)
                if end_raw:
                    end = datetime.strptime(end_raw, "%Y-%m-%d").date()
                else:
                    avg_len = self._data["settings"].get("avg_period_days", self.default_length)
                    end = min(
                        start + timedelta(days=avg_len - 1),
                        datetime.now().date(),
                    )
                d = start
                while d <= end:
                    marks[d.isoformat()] = "period"
                    d += timedelta(days=1)
            except Exception:
                continue

        # 预测经期
        nxt = self.predict_next()
        avg_len = self._data["settings"].get("avg_period_days", self.default_length)
        if nxt:
            for i in range(avg_len):
                d = nxt + timedelta(days=i)
                if d.isoformat() not in marks:
                    marks[d.isoformat()] = "period_predicted"

        # 排卵期
        ovu = self.predict_ovulation()
        if ovu:
            start, day, end = ovu
            d = start
            while d <= end:
                key = d.isoformat()
                if key not in marks:  # 已有经期标记优先
                    marks[key] = "ovulation"
                d += timedelta(days=1)
            # 排卵日单独标记
            if day.isoformat() not in [k for k, v in marks.items() if v == "period"]:
                marks[day.isoformat()] = "ovulation_day"

        return marks

    def get_all(self) -> dict:
        """返回全部数据（前端用）。"""
        return dict(self._data)
