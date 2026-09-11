"""历史健康数据归档存储。

每次拉取 snapshot 时调用 append()，会写入当天的 JSON 文件。
前端通过 query() 读取指定天数范围的历史数据。
"""
from __future__ import annotations

import json
import dataclasses
from pathlib import Path
from datetime import datetime, timedelta, date as _date
from typing import Any

from astrbot.api import logger


def _to_jsonable(obj: Any) -> Any:
    """把 SDK 返回的对象（StepData / dataclass / pydantic / 带 __dict__ 的对象）
    递归转为 JSON 原生类型。遇到无法序列化的对象就转字符串兜底。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, _date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    # dataclass
    if dataclasses.is_dataclass(obj):
        try:
            return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
        except Exception:
            pass
    # pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return _to_jsonable(obj.model_dump())
        except Exception:
            pass
    # pydantic v1
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        try:
            return _to_jsonable(obj.dict())
        except Exception:
            pass
    # 普通对象带 __dict__
    if hasattr(obj, "__dict__"):
        try:
            return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            pass
    # 兜底
    return str(obj)


class HistoryStore:
    """健康数据历史归档（按日期存）。"""

    def __init__(self, data_dir: Path, keep_days: int = 90):
        self.history_dir = data_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days

    def append(self, snapshot: dict) -> bool:
        """追加今日快照到归档。"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            path = self.history_dir / f"{today}.json"
            
            # 读取已有数据（如有）
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict) or "snapshots" not in data:
                        raise ValueError("bad structure")
                except Exception as e:
                    # 旧文件可能是上一版写坏的半截 JSON，备份后重建
                    logger.warning(f"[mi_health.history] {path.name} 内容损坏({e})，备份后重建")
                    try:
                        backup = path.with_suffix(".json.bak")
                        path.replace(backup)
                    except Exception:
                        pass
                    data = {"date": today, "snapshots": []}
            else:
                data = {"date": today, "snapshots": []}
            
            # 追加新快照（带时间戳，先转成 JSON 安全类型）
            snapshot_copy = _to_jsonable(snapshot)
            if not isinstance(snapshot_copy, dict):
                snapshot_copy = {"data": snapshot_copy}
            snapshot_copy["_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data["snapshots"].append(snapshot_copy)
            
            # 写回
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.exception(f"[mi_health.history] 归档失败: {e}")
            return False

    def query(self, days: int = 7) -> list[dict]:
        """查询最近 N 天的归档数据（每天取最后一次）。"""
        result = []
        today = datetime.now().date()
        for i in range(days):
            date = today - timedelta(days=i)
            path = self.history_dir / f"{date}.json"
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                snapshots = data.get("snapshots", [])
                if snapshots:
                    # 取当天最后一次
                    last = snapshots[-1]
                    result.append({
                        "date": data.get("date", str(date)),
                        "snapshot": last,
                    })
            except Exception as e:
                logger.warning(f"[mi_health.history] 读取 {date} 失败: {e}")
        return list(reversed(result))  # 时间从旧到新

    def cleanup(self) -> int:
        """清理超过 keep_days 的旧归档。"""
        count = 0
        cutoff = datetime.now().date() - timedelta(days=self.keep_days)
        for p in self.history_dir.glob("*.json"):
            try:
                date_str = p.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if file_date < cutoff:
                    p.unlink()
                    count += 1
            except Exception:
                pass
        return count
