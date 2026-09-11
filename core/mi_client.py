"""封装 mi-fitness-python SDK 调用。"""
from __future__ import annotations

import asyncio
from datetime import date as _date
from typing import Any, Optional

from astrbot.api import logger

# 静默 mi_fitness SDK 内部的 loguru 日志。
# 该 SDK 使用 `from loguru import logger` 直接打日志, 而 AstrBot 的 loguru
# 全局 format 依赖 {extra[plugin_tag]} 字段, SDK 打的日志缺该字段会导致
# 后台刷 KeyError: 'plugin_tag'。这里通过 loguru.disable 屏蔽掉 SDK 的
# 日志输出——它自己那些"加载 token"之类的信息我们已在插件层重新记录。
try:
    from loguru import logger as _loguru_logger  # noqa: E402
    _loguru_logger.disable("mi_fitness")
except Exception:  # noqa: BLE001
    pass


class MiHealthClient:
    def __init__(self, token_path: str, target_uid: str | int, timeout: float = 15.0):
        self.token_path = str(token_path)
        self.target_uid = int(target_uid)
        self.timeout = timeout

    async def _with_client(self, fn):
        try:
            from mi_fitness import MiHealthClient as _SDKClient
        except Exception as e:
            logger.exception(f"[mi_health] mi-fitness 未安装: {e}")
            raise RuntimeError("mi-fitness 未安装") from e

        try:
            async with _SDKClient.from_token(self.token_path) as client:
                return await asyncio.wait_for(fn(client), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("[mi_health] SDK 调用超时")
            raise
        except Exception as e:
            logger.exception(f"[mi_health] SDK 调用失败: {e}")
            raise

    async def get_steps(self, d: Optional[_date] = None) -> Any:
        query_d = d or _date.today()

        async def _call(client):
            return await client.get_steps(self.target_uid, query_d)

        return await self._with_client(_call)

    async def get_heart_rate(self, d: Optional[_date] = None) -> Any:
        query_d = d or _date.today()

        async def _call(client):
            return await client.get_heart_rate(self.target_uid, query_d)

        return await self._with_client(_call)

    async def get_sleep(self, d: Optional[_date] = None, days: int = 1) -> Any:
        query_d = d or _date.today()

        async def _call(client):
            return await client.get_sleep(self.target_uid, query_d, days=days)

        return await self._with_client(_call)

    async def get_spo2(self) -> Any:
        async def _call(client):
            return await client.get_spo2(self.target_uid)

        try:
            return await self._with_client(_call)
        except Exception:
            return None

    async def get_vitality(self) -> Any:
        """获取活力指标(中高强度活动时长,小米健康里的"活力")。"""
        async def _call(client):
            return await client.get_intensity(self.target_uid)

        try:
            return await self._with_client(_call)
        except Exception:
            return None

    async def get_snapshot(self, include_vitality: bool = False) -> dict[str, Any]:
        today = _date.today()
        today_str = today.strftime("%Y-%m-%d")

        async def _safe(coro):
            try:
                return await coro
            except Exception as e:
                logger.warning(f"[mi_health] 子项拉取异常: {e}")
                return None

        tasks = [
            _safe(self.get_steps(today)),
            _safe(self.get_heart_rate(today)),
            _safe(self.get_sleep(today, days=2)),
            _safe(self.get_spo2()),
        ]
        if include_vitality:
            tasks.append(_safe(self.get_vitality()))

        results = await asyncio.gather(*tasks)
        steps, hr, sleep, spo2 = results[:4]
        vitality = results[4] if include_vitality else None

        return {
            "date": today_str,
            "steps": steps,
            "heart_rate": hr,
            "sleep": sleep,
            "spo2": spo2,
            "vitality": vitality,
        }

    async def ping(self) -> bool:
        try:
            await self.get_steps()
            return True
        except Exception:
            return False
