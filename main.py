"""mi_health 插件入口。

作用:
  - 指令 /health 系列: 主动查询步数、心率、血氧、睡眠、活力。
  - LLM Tool  get_user_health: 允许模型在需要时自行调用。
  - LLM Hook  on_llm_request : 每次对话前静默把健康快照塞进 system_prompt,
              让小回作为背景常识感知,不主动罗列。
  - 后台 Monitor: 主动关怀(定时轮询 + 基线异常检测 + 主动推送到白名单会话)。

强制:
  - 会话隔离: on_llm_request / llm_tool 内主动判断 SessionPluginManager。
  - 会话白名单: allowed_session_ids 配置可再收一层(隐私保护)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register

from .core.mi_client import MiHealthClient
from .core.cache import SnapshotCache
from .core import formatter as fmt
from .core.monitor import HealthMonitor
from .core.history_store import HistoryStore
from .core.period_manager import PeriodManager
from .core.web_server import WebServer


PLUGIN_NAME = "mi_health"


@register(
    PLUGIN_NAME,
    "xavier",
    "小米手环健康数据接入(步数/心率/血氧/睡眠/活力),支持指令查询、聊天上下文注入与主动关怀。",
    "0.2.0",
)
class MiHealthPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.name = PLUGIN_NAME

        # 数据目录
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir

        # 基础配置
        self.auto_inject: bool = bool(self.config.get("auto_inject", True))
        self.cache_ttl: int = int(self.config.get("cache_ttl", 600))
        self.inject_max_length: int = int(self.config.get("inject_max_length", 400))
        self.silent_on_error: bool = bool(self.config.get("silent_on_error", True))
        self.allowed_session_ids: list[str] = list(self.config.get("allowed_session_ids") or [])
        # 活力指标(默认关)
        self.enable_vitality: bool = bool(self.config.get("enable_vitality", False))

        # token / uid
        configured_token = str(self.config.get("token_path") or "").strip()
        local_token = str(data_dir / "token.json")
        if configured_token:
            self.token_path: str = configured_token
        elif (data_dir / "token.json").exists():
            self.token_path: str = local_token
        else:
            self.token_path: str = ""
        self.target_uid: str = str(self.config.get("target_uid") or "").strip()

        # 缓存
        self.cache = SnapshotCache(
            disk_path=data_dir / "snapshot.json",
            ttl=self.cache_ttl,
        )

        # 客户端
        self.client: Optional[MiHealthClient] = None
        if self.token_path and self.target_uid:
            self.client = MiHealthClient(self.token_path, self.target_uid)
        else:
            logger.warning(
                f"[{PLUGIN_NAME}] token_path 或 target_uid 未配置,插件将无法查询数据"
            )

        # 历史归档存储
        self.history_store = HistoryStore(
            data_dir=data_dir,
            keep_days=int(self.config.get("history_keep_days", 90)),
        )

        # 生理期管理器
        self.period_mgr = PeriodManager(
            data_dir=data_dir,
            default_cycle=int(self.config.get("period_default_cycle", 28)),
            default_length=int(self.config.get("period_default_length", 5)),
        )

        # Web 前端服务
        self.web_server = WebServer(self, self.config, data_dir=data_dir)

        # 主动关怀 monitor
        self.monitor = HealthMonitor(self, self.config, data_dir=data_dir)

    # =====================================================
    # 生命周期
    # =====================================================
    async def initialize(self):
        logger.info(
            f"[{PLUGIN_NAME}] initialized. token={'set' if self.token_path else 'MISSING'}, "
            f"uid={'set' if self.target_uid else 'MISSING'}, auto_inject={self.auto_inject}, "
            f"vitality={self.enable_vitality}, monitor={self.config.get('monitor_enabled', False)}"
        )
        # 启动主动关怀(内部会判 monitor_enabled)
        if self.client:
            await self.monitor.start()

        # 启动 Web 前端服务
        if self.config.get("web_enabled", True):
            try:
                await self.web_server.start()
            except Exception as e:
                logger.exception(f"[{PLUGIN_NAME}] Web 服务启动失败: {e}")

    async def terminate(self):
        try:
            await self.web_server.stop()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] web stop 异常: {e}")
        try:
            await self.monitor.stop()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] monitor stop 异常: {e}")
        logger.info(f"[{PLUGIN_NAME}] terminated.")

    # =====================================================
    # 通用工具
    # =====================================================
    def _is_enabled(self, event: AstrMessageEvent) -> bool:
        """会话级隔离判断。"""
        try:
            from astrbot.core.plugin.session_plugin_manager import (
                SessionPluginManager,
            )
            return SessionPluginManager.is_plugin_enabled_for_session(
                plugin_name=self.name,
                session_id=event.get_session_id(),
            )
        except Exception:
            return True

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        """白名单二次过滤(隐私保护)。"""
        if not self.allowed_session_ids:
            return True
        try:
            sid = event.get_session_id() or ""
            umo = getattr(event, "unified_msg_origin", "") or ""
            for entry in self.allowed_session_ids:
                if not entry:
                    continue
                if entry == sid or entry == umo:
                    return True
            return False
        except Exception:
            return False

    async def _fetch_snapshot(self, force: bool = False) -> Optional[dict]:
        """拿数据: 优先缓存,失效则调云端,再失败则返回旧数据。"""
        if not self.client:
            return None
        if not force:
            cached = await self.cache.get()
            if cached:
                return cached
        try:
            snap = await self.client.get_snapshot(include_vitality=self.enable_vitality)
            await self.cache.set(snap)
            # 自动归档历史
            if self.config.get("history_enabled", True):
                self.history_store.append(snap)
            return snap
        except Exception as e:
            logger.exception(f"[{PLUGIN_NAME}] 拉取云端数据失败: {e}")
            return await self.cache.stale()

    # =====================================================
    # 指令
    # =====================================================
    @filter.command_group("health")
    def health_group(self):
        """健康数据查询指令族。"""
        pass

    @health_group.command("")
    async def cmd_health_all(self, event: AstrMessageEvent):
        """/health 查询今日全部健康数据"""
        if not self.client:
            yield event.plain_result("插件还没配置好(缺 token 或 UID),先去配置里填一下~")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据,可能是 token 过期或亲友授权没开,可以 /health status 看看")
            return
        yield event.plain_result(fmt.format_full(snap))

    @health_group.command("steps")
    async def cmd_health_steps(self, event: AstrMessageEvent):
        """/health steps 查看步数"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据")
            return
        yield event.plain_result("👣 " + fmt.format_steps(snap.get("steps")))

    @health_group.command("hr")
    async def cmd_health_hr(self, event: AstrMessageEvent):
        """/health hr 查看心率"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据")
            return
        yield event.plain_result("❤️ " + fmt.format_heart_rate(snap.get("heart_rate")))

    @health_group.command("sleep")
    async def cmd_health_sleep(self, event: AstrMessageEvent):
        """/health sleep 查看昨晚睡眠"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据")
            return
        yield event.plain_result("😴 " + fmt.format_sleep(snap.get("sleep")))

    @health_group.command("spo2")
    async def cmd_health_spo2(self, event: AstrMessageEvent):
        """/health spo2 查看血氧"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据")
            return
        text = fmt.format_spo2(snap.get("spo2"))
        yield event.plain_result("🫁 " + text if text else "🫁 血氧: 暂无数据(可能手环未开启或SDK不支持)")

    @health_group.command("vitality")
    async def cmd_health_vitality(self, event: AstrMessageEvent):
        """/health vitality 查看活力指标(中高强度活动时长)"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        if not self.enable_vitality:
            yield event.plain_result("活力指标开关未开启(配置里打开 enable_vitality 再试)")
            return
        snap = await self._fetch_snapshot()
        if not snap:
            yield event.plain_result("查不到数据")
            return
        text = fmt.format_vitality(snap.get("vitality"))
        yield event.plain_result("🌟 " + text if text else "🌟 活力: 暂无数据")

    @health_group.command("refresh")
    async def cmd_health_refresh(self, event: AstrMessageEvent):
        """/health refresh 强制刷新缓存"""
        if not self.client:
            yield event.plain_result("插件未配置")
            return
        snap = await self._fetch_snapshot(force=True)
        if not snap:
            yield event.plain_result("刷新失败,可能 token 过期了")
            return
        yield event.plain_result("已刷新 ✅\n" + fmt.format_full(snap))

    @health_group.command("status")
    async def cmd_health_status(self, event: AstrMessageEvent):
        """/health status 查看插件状态"""
        lines = [
            f"[{PLUGIN_NAME}] 状态",
            f"token_path  : {'✅ 已设置' if self.token_path else '❌ 未设置'}",
            f"target_uid  : {'✅ 已设置' if self.target_uid else '❌ 未设置'}",
            f"auto_inject : {'✅ 开' if self.auto_inject else '❌ 关'}",
            f"vitality    : {'✅ 开' if self.enable_vitality else '❌ 关'}",
            f"monitor     : {'✅ 开' if self.config.get('monitor_enabled', False) else '❌ 关'}",
            f"缓存TTL     : {self.cache_ttl}s",
            f"基线天数    : {self.monitor.store.days_collected()}/"
            f"{int(self.config.get('baseline_days', 7))}",
        ]
        if self.client:
            try:
                ok = await self.client.ping()
                lines.append(f"云端连通    : {'✅ 正常' if ok else '❌ 失败(token可能已过期)'}")
            except Exception as e:
                lines.append(f"云端连通    : ❌ 异常 {e}")
        yield event.plain_result("\n".join(lines))

    # ------- 主动关怀子指令 -------
    @health_group.command("monitor")
    async def cmd_health_monitor(self, event: AstrMessageEvent, action: str = ""):
        """/health monitor [on|off|status|test|push] 主动关怀开关"""
        act = (action or "").lower().strip()

        if act == "push":
            # 强制推送一次（测试用）
            if not self.client:
                yield event.plain_result("插件未配置")
                return
            try:
                await self.monitor._tick_force(event.get_session_id())
                yield event.plain_result("✅ 已强制推送一次（忽略冷却/聊天窗口限制）")
            except Exception as e:
                logger.exception(f"[mi_health] 强制推送失败: {e}")
                yield event.plain_result(f"❌ 推送失败: {e}")
            return

        if act == "on":
            if not self.client:
                yield event.plain_result("插件未配置, 无法启动关怀")
                return
            self.config["monitor_enabled"] = True
            await self.monitor.stop()
            await self.monitor.start()
            yield event.plain_result("主动关怀已临时开启(重启后按配置为准)")
            return

        if act == "off":
            self.config["monitor_enabled"] = False
            await self.monitor.stop()
            yield event.plain_result("主动关怀已临时关闭")
            return

        if act == "test":
            if not self.client:
                yield event.plain_result("插件未配置")
                return
            info = await self.monitor.dry_run()
            if not info.get("ok"):
                yield event.plain_result(f"测试失败: {info.get('reason')}")
                return
            alerts = info.get("alerts") or []
            lines = [
                f"基线天数: {info.get('days_collected')}",
                f"静默中  : {info.get('in_quiet')}",
                f"候选关怀: {len(alerts)} 条",
            ]
            for a in alerts:
                lines.append(f"  - [{a.get('type')}] {a.get('hint')}")
            yield event.plain_result("\n".join(lines))
            return

        # default: status
        cds = []
        for key in ("heart_rate_high", "heart_rate_low", "sleep", "spo2", "late_night", "lazy_step"):
            in_cd = self.monitor._in_cooldown(key)
            cds.append(f"  - {key}: {'冷却中' if in_cd else '空闲'}")
        yield event.plain_result(
            "\n".join(
                [
                    "[主动关怀 状态]",
                    f"启用     : {'✅' if self.config.get('monitor_enabled', False) else '❌'}",
                    f"轮询间隔 : {int(self.config.get('check_interval', 900))}s",
                    f"基线天数 : {self.monitor.store.days_collected()}/"
                    f"{int(self.config.get('baseline_days', 7))}",
                    f"目标会话 : {len(self.allowed_session_ids)} 个",
                    "冷却状态 :",
                    *cds,
                ]
            )
        )

    # =====================================================
    # LLM Tool
    # =====================================================
    @filter.llm_tool(name="get_user_health")
    async def tool_get_user_health(
        self,
        event: AstrMessageEvent,
        data_type: str = "all",
        date: str = "today",
    ):
        """查询用户的实时健康数据(步数/心率/血氧/睡眠/活力)。当用户提到累、困、不舒服、运动、休息等场景,或明确询问健康状态时调用。

        Args:
            data_type(string): 数据类型。可选值: all(全部), steps(步数), heart_rate(心率), sleep(睡眠), spo2(血氧), vitality(活力)。默认 all。
            date(string): 查询日期,格式 YYYY-MM-DD。默认 today(今天)。
        """
        # 会话隔离
        if not self._is_enabled(event):
            return "插件在当前会话已禁用"
        if not self._session_allowed(event):
            return "当前会话无权访问健康数据"
        if not self.client:
            return "插件未配置(缺 token 或 UID)"

        snap = await self._fetch_snapshot()
        if not snap:
            return "健康数据暂时拿不到(可能 token 过期)"

        dtype = (data_type or "all").lower().strip()
        if dtype in ("all", "*", "full"):
            return fmt.format_full(snap)
        if dtype == "steps":
            return fmt.format_steps(snap.get("steps"))
        if dtype in ("heart_rate", "hr"):
            return fmt.format_heart_rate(snap.get("heart_rate"))
        if dtype == "sleep":
            return fmt.format_sleep(snap.get("sleep"))
        if dtype in ("spo2", "oxygen"):
            return fmt.format_spo2(snap.get("spo2")) or "血氧暂无数据"
        if dtype in ("vitality", "intensity"):
            return fmt.format_vitality(snap.get("vitality")) or "活力暂无数据"
        return fmt.format_full(snap)

    # =====================================================
    # LLM Hook: 自动上下文注入
    # =====================================================
    @filter.on_llm_request()
    async def inject_health_context(self, event: AstrMessageEvent, req):
        # 强制会话级隔离 —— 会话禁用则完全不生效
        if not self._is_enabled(event):
            return
        if not self.auto_inject:
            return
        if not self._session_allowed(event):
            return
        if not self.client:
            return

        # 记录本会话的活跃时间(供 monitor 决定是否让路)
        try:
            sid = event.get_session_id() or ""
            if sid:
                self.monitor.touch_user_activity(sid)
        except Exception:
            pass

        try:
            snap = await self._fetch_snapshot()
            # 生理期状态即使拿不到手环快照也要注入
            try:
                period_status = (
                    self.period_mgr.get_current_status() if self.period_mgr else None
                )
            except Exception as _e:
                period_status = None
                logger.debug(f"[{PLUGIN_NAME}] 读取生理期状态失败: {_e}")

            if not snap and not period_status:
                return
            block = fmt.format_for_llm(
                snap or {},
                max_length=self.inject_max_length,
                period_status=period_status,
            )
            if not block:
                return
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = (existing.rstrip() + "\n\n" + block).strip()
        except Exception as e:
            if not self.silent_on_error:
                raise
            logger.warning(f"[{PLUGIN_NAME}] 注入健康上下文失败(已静默): {e}")
