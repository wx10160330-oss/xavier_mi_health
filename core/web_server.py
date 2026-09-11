"""内置 HTTP Web 服务 (基于 aiohttp.web)。

提供:
  - GET  /                     首页: 今日健康卡片 + 入口
  - GET  /calendar             生理期日历 (参考设计)
  - GET  /history              历史数据折线图
  - GET  /api/today            今日健康快照
  - GET  /api/refresh          强制重拉小米云端
  - GET  /api/history          历史归档数据 (?days=7|30)
  - GET  /api/period           生理期全部数据 + 预测 + 当前状态
  - GET  /api/period/calendar  某月标记 (?year=2026&month=9)
  - POST /api/period/mark      标记今天/指定日来了 {"date": "YYYY-MM-DD"}
  - POST /api/period/end       标记结束 {"date": "YYYY-MM-DD"}
  - POST /api/period/delete    删除某条 {"start": "YYYY-MM-DD"}
  - POST /api/period/note      添加备注 {"date": "...", "note": "..."}

安全:
  - web_token_enabled=True 时校验 ?k=xxx 或 Cookie: mi_health_token=xxx
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from datetime import datetime
from aiohttp import web

from astrbot.api import logger
from .history_store import _to_jsonable


class WebServer:
    def __init__(self, plugin, config: dict, data_dir: Path):
        self.plugin = plugin
        self.config = config
        self.data_dir = data_dir
        self.web_dir = Path(__file__).parent.parent / "web"
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.runner = None
        self.site = None

        # 安全配置
        self.token_enabled = bool(config.get("web_token_enabled", True))
        self.token = str(config.get("web_token") or "").strip()
        if self.token_enabled and not self.token:
            # 自动生成 20 位随机 token 并写回配置
            self.token = secrets.token_hex(10)
            self._save_token_to_config(self.token)

        self._setup_routes()

    def _save_token_to_config(self, token: str):
        """把自动生成的 token 写回配置文件。"""
        try:
            cfg_path = Path(__file__).parent.parent.parent.parent / "config" / "mi_health_config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                cfg["web_token"] = token
                with open(cfg_path, "w", encoding="utf-8-sig") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                logger.info(f"[mi_health.web] 已自动生成 web_token 并保存: {token}")
        except Exception as e:
            logger.warning(f"[mi_health.web] 写回 token 失败: {e}")

    # ---------- 中间件: 全局异常兜底 + Token 鉴权 ----------
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # 全局异常兜底: API 请求出错时返回 JSON 而不是 aiohttp 默认的纯文本
        async def _safe_handle(req):
            try:
                return await handler(req)
            except web.HTTPException:
                raise
            except Exception as e:
                logger.exception(f"[mi_health.web] 未捕获异常 {req.path}: {e}")
                if req.path.startswith("/api/"):
                    return web.json_response(
                        {"ok": False, "msg": f"服务端异常: {e}"},
                        status=500,
                    )
                raise

        if not self.token_enabled or not self.token:
            return await _safe_handle(request)

        # 静态资源可放行(如果有单独静态路由)
        # 鉴权: 优先 query ?k=xxx, 其次 Cookie mi_health_token, 其次 Header X-Token
        token = (
            request.query.get("k")
            or request.cookies.get("mi_health_token")
            or request.headers.get("X-Token")
            or ""
        )

        if token != self.token:
            # 区分 API 和页面
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "msg": "未授权: token 错误"}, status=401)
            # 页面给个友好的登录提示
            return web.Response(
                text=(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
                    "<title>访问受限</title>"
                    "<style>body{font-family:sans-serif;text-align:center;padding:50px;background:#fdf6f7;color:#555}"
                    "h2{color:#d65d7a}input{padding:8px 12px;border:1px solid #ddd;border-radius:6px;margin:8px}"
                    "button{padding:8px 18px;background:#d65d7a;color:#fff;border:none;border-radius:6px;cursor:pointer}</style>"
                    "</head><body><h2>🔒 需要访问凭证</h2>"
                    "<p>请在链接后加上 <code>?k=你的Token</code>，或在下方输入：</p>"
                    "<form method='GET' onsubmit=\"location.href='/?k='+document.getElementById('t').value;return false;\">"
                    "<input id='t' type='text' placeholder='请输入 Token'/><br>"
                    "<button type='submit'>进入</button></form></body></html>"
                ),
                content_type="text/html",
                status=401,
            )

        # 鉴权通过：如果是从 query 进来的，顺便写 Cookie 方便下次免输
        resp = await _safe_handle(request)
        if request.query.get("k") == self.token:
            resp.set_cookie("mi_health_token", self.token, max_age=86400 * 365, httponly=True)
        return resp

    # ---------- 路由注册 ----------
    def _setup_routes(self):
        # 静态 HTML 页面
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/calendar", self.handle_calendar)
        self.app.router.add_get("/history", self.handle_history)

        # 静态资源 (CSS / JS)
        self.app.router.add_static("/static", str(self.web_dir), show_index=False)

        # API
        self.app.router.add_get("/api/today", self.api_today)
        self.app.router.add_get("/api/refresh", self.api_refresh)
        self.app.router.add_get("/api/history", self.api_history)
        self.app.router.add_get("/api/period", self.api_period)
        self.app.router.add_get("/api/period/calendar", self.api_period_calendar)
        self.app.router.add_post("/api/period/mark", self.api_period_mark)
        self.app.router.add_post("/api/period/end", self.api_period_end)
        self.app.router.add_post("/api/period/delete", self.api_period_delete)
        self.app.router.add_post("/api/period/note", self.api_period_note)
        self.app.router.add_post("/api/period/settings", self.api_period_settings)

    # ---------- 页面 Handlers ----------
    def _no_cache_headers(self) -> dict:
        return {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }

    async def handle_index(self, request: web.Request) -> web.Response:
        p = self.web_dir / "index.html"
        return web.FileResponse(p, headers=self._no_cache_headers())

    async def handle_calendar(self, request: web.Request) -> web.Response:
        p = self.web_dir / "calendar.html"
        return web.FileResponse(p, headers=self._no_cache_headers())

    async def handle_history(self, request: web.Request) -> web.Response:
        p = self.web_dir / "history.html"
        return web.FileResponse(p, headers=self._no_cache_headers())

    # ---------- API Handlers ----------
    async def api_today(self, request: web.Request) -> web.Response:
        force = request.query.get("force") in ("1", "true", "yes")
        try:
            snap = await self.plugin._fetch_snapshot(force=force)
        except Exception as e:
            logger.exception(f"[mi_health.web] api_today 拉数据失败: {e}")
            return web.json_response({
                "ok": False,
                "msg": f"拉取健康数据失败: {e}",
                "snapshot": None,
                "period_status": self.plugin.period_mgr.get_current_status(),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        try:
            status = self.plugin.period_mgr.get_current_status()
        except Exception as e:
            logger.exception(f"[mi_health.web] api_today 生理期状态失败: {e}")
            status = {}
        resp = web.json_response(_to_jsonable({
            "ok": True,
            "snapshot": snap,
            "period_status": status,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    async def api_refresh(self, request: web.Request) -> web.Response:
        snap = await self.plugin._fetch_snapshot(force=True)
        if not snap:
            return web.json_response({"ok": False, "msg": "刷新失败"}, status=500)
        # 刷新成功顺便归档
        self.plugin.history_store.append(snap)
        resp = web.json_response(_to_jsonable({
            "ok": True,
            "snapshot": snap,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    async def api_history(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", 7))
        except Exception:
            days = 7
        data = self.plugin.history_store.query(days=days)
        return web.json_response({"ok": True, "days": days, "data": data})

    async def api_period(self, request: web.Request) -> web.Response:
        all_data = self.plugin.period_mgr.get_all()
        status = self.plugin.period_mgr.get_current_status()
        return web.json_response({
            "ok": True,
            "data": all_data,
            "status": status,
        })

    async def api_period_calendar(self, request: web.Request) -> web.Response:
        now = datetime.now()
        try:
            year = int(request.query.get("year", now.year))
            month = int(request.query.get("month", now.month))
        except Exception:
            year, month = now.year, now.month
        marks = self.plugin.period_mgr.get_calendar_data(year, month)
        return web.json_response({
            "ok": True,
            "year": year,
            "month": month,
            "marks": marks,
        })

    async def api_period_mark(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            d = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        except Exception:
            d = datetime.now().strftime("%Y-%m-%d")
        res = self.plugin.period_mgr.mark_start(d)
        return web.json_response(res)

    async def api_period_end(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            d = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        except Exception:
            d = datetime.now().strftime("%Y-%m-%d")
        res = self.plugin.period_mgr.mark_end(d)
        return web.json_response(res)

    async def api_period_delete(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            start = body.get("start")
        except Exception:
            start = None
        if not start:
            return web.json_response({"ok": False, "msg": "缺少 start 参数"}, status=400)
        res = self.plugin.period_mgr.delete(start)
        return web.json_response(res)

    async def api_period_note(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            d = body.get("date")
            note = body.get("note", "")
        except Exception:
            return web.json_response({"ok": False, "msg": "参数错误"}, status=400)
        if not d:
            return web.json_response({"ok": False, "msg": "缺少 date"}, status=400)
        res = self.plugin.period_mgr.add_note(d, note)
        return web.json_response(res)

    async def api_period_settings(self, request: web.Request) -> web.Response:
        """更新生理期设置（周期天数 & 经期天数）"""
        try:
            body = await request.json()
            cycle = body.get("avg_cycle_days")
            length = body.get("avg_period_days")
        except Exception:
            return web.json_response({"ok": False, "msg": "参数错误"}, status=400)
        
        if cycle is not None:
            try:
                cycle = int(cycle)
                if not (14 <= cycle <= 60):
                    return web.json_response({"ok": False, "msg": "周期天数需在 14~60 天之间"}, status=400)
            except ValueError:
                return web.json_response({"ok": False, "msg": "周期天数格式错误"}, status=400)
        
        if length is not None:
            try:
                length = int(length)
                if not (1 <= length <= 14):
                    return web.json_response({"ok": False, "msg": "经期天数需在 1~14 天之间"}, status=400)
            except ValueError:
                return web.json_response({"ok": False, "msg": "经期天数格式错误"}, status=400)
        
        # 更新到 period_mgr 的数据里
        settings = self.plugin.period_mgr._data.setdefault("settings", {})
        if cycle is not None:
            settings["avg_cycle_days"] = cycle
        if length is not None:
            settings["avg_period_days"] = length
        # 标记为手动覆盖，避免后续自动重算覆盖用户设置
        settings["manual_override"] = True
        self.plugin.period_mgr._save()
        
        return web.json_response({"ok": True, "msg": "设置已更新"})

    # ---------- 生命周期 ----------
    async def start(self):
        host = str(self.config.get("web_host", "0.0.0.0"))
        port = int(self.config.get("web_port", 4847))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        logger.info(
            f"[mi_health.web] HTTP 服务已启动: http://{host}:{port} "
            f"(token_enabled={self.token_enabled}, token={self.token[:4]}****)"
        )

    async def stop(self):
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("[mi_health.web] HTTP 服务已停止")
