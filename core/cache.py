"""内存+磁盘双层缓存。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

try:
    import aiofiles  # type: ignore
    HAS_AIOFILES = True
except Exception:
    HAS_AIOFILES = False


class SnapshotCache:
    """健康数据快照缓存。"""

    def __init__(self, disk_path: Path, ttl: int = 600):
        self.disk_path = Path(disk_path)
        self.ttl = ttl
        self._mem: Optional[dict[str, Any]] = None
        self._mem_ts: float = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self, ts: float) -> bool:
        return (time.time() - ts) < self.ttl

    async def get(self) -> Optional[dict[str, Any]]:
        # 先看内存
        if self._mem and self._fresh(self._mem_ts):
            return self._mem
        # 再看磁盘
        if not self.disk_path.exists():
            return None
        try:
            if HAS_AIOFILES:
                async with aiofiles.open(self.disk_path, "r", encoding="utf-8") as f:
                    raw = await f.read()
            else:
                raw = self.disk_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            ts = float(data.get("_ts", 0))
            if self._fresh(ts):
                self._mem = data
                self._mem_ts = ts
                return data
        except Exception:
            return None
        return None

    async def set(self, data: dict[str, Any]) -> None:
        async with self._lock:
            data = dict(data)
            data["_ts"] = time.time()
            self._mem = data
            self._mem_ts = data["_ts"]
            try:
                from .history_store import _to_jsonable
                self.disk_path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(_to_jsonable(data), ensure_ascii=False, indent=2)
                if HAS_AIOFILES:
                    async with aiofiles.open(self.disk_path, "w", encoding="utf-8") as f:
                        await f.write(payload)
                else:
                    self.disk_path.write_text(payload, encoding="utf-8")
            except Exception:
                # 磁盘写失败不影响内存缓存
                pass

    async def clear(self) -> None:
        async with self._lock:
            self._mem = None
            self._mem_ts = 0.0
            try:
                if self.disk_path.exists():
                    self.disk_path.unlink()
            except Exception:
                pass

    async def stale(self) -> Optional[dict[str, Any]]:
        """无视 TTL,拿最后一次的数据(接口失败降级用)。"""
        if self._mem:
            return self._mem
        if not self.disk_path.exists():
            return None
        try:
            if HAS_AIOFILES:
                async with aiofiles.open(self.disk_path, "r", encoding="utf-8") as f:
                    raw = await f.read()
            else:
                raw = self.disk_path.read_text(encoding="utf-8")
            return json.loads(raw)
        except Exception:
            return None
