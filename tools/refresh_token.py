"""mi_health · token.json 刷新工具

用途:
    小米亲友共享 token 过期后, 双击 refresh_token.bat 或在终端跑本脚本,
    通过扫码重新登录, 自动覆盖到插件配置里的 token_path 位置。

流程:
    1. 读取 AstrBot 主配置目录下的 mi_health_config.json, 找到 token_path
       (读不到就退回默认路径 data/plugins/mi_health/data/token.json)
    2. 备份旧 token.json → token.json.bak
    3. 调用 mi-fitness SDK 打印二维码, 等你扫码
    4. 扫码成功后把新 token.json 写到 token_path

注意:
    - 请用【小号】那部手机扫码, 且必须用「微信/系统相机/浏览器」的扫一扫,
      不能用小米运动健康 App 内的扫一扫 (官方限制)
    - 二维码 5 分钟内有效, 超时重跑本脚本
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

# ---- 路径推断 --------------------------------------------------------------
# 本文件位于  <AstrBot 根>/data/plugins/mi_health/tools/refresh_token.py
# 往上两级到 <AstrBot 根>/data
HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
DATA_ROOT = PLUGIN_DIR.parent.parent  # <AstrBot 根>/data
CONFIG_FILE = DATA_ROOT / "config" / "mi_health_config.json"
DEFAULT_TOKEN_PATH = PLUGIN_DIR / "data" / "token.json"


def _resolve_token_path() -> Path:
    """从 mi_health_config.json 里读 token_path, 失败则用默认路径。"""
    try:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            tp = cfg.get("token_path")
            if tp:
                return Path(tp).expanduser().resolve()
    except Exception as e:
        print(f"⚠️  读取配置失败, 使用默认路径: {e}")
    return DEFAULT_TOKEN_PATH.resolve()


def _backup_old_token(token_path: Path) -> None:
    if not token_path.exists():
        return
    bak = token_path.with_suffix(token_path.suffix + ".bak")
    try:
        shutil.copy2(token_path, bak)
        print(f"📦 旧 token 已备份 → {bak.name}")
    except Exception as e:
        print(f"⚠️  备份旧 token 失败 (忽略, 继续): {e}")


async def _qr_login(token_path: Path) -> None:
    try:
        from mi_fitness.auth import XiaomiAuth
        from mi_fitness.exceptions import AuthError
        from mi_fitness.cli import _print_qr_to_terminal
    except ImportError as e:
        print("❌ 未安装 mi-fitness SDK, 请先在插件目录执行:")
        print("   pip install -r requirements.txt")
        raise SystemExit(1) from e

    async def show_qr(qr_image_url: str, login_url: str) -> None:
        print("\n📱 请用【小号手机】的微信/系统相机扫描二维码 (不要用小米运动健康 App 内扫码)\n")
        if login_url:
            _print_qr_to_terminal(login_url)
        print(f"\n   二维码图片: {qr_image_url}")
        if login_url:
            print(f"   浏览器直接打开: {login_url}")
        print("\n⏳ 等待扫码 (5 分钟内有效)...\n")

    async with XiaomiAuth() as auth:
        try:
            await auth.login_qr(qr_callback=show_qr)
        except AuthError as e:
            print(f"❌ 扫码登录失败: {e}")
            raise SystemExit(2) from e

        token_path.parent.mkdir(parents=True, exist_ok=True)
        auth.save_token(token_path)
        print("\n✅ 扫码登录成功!")
        print(f"   user_id  = {auth.token.user_id}")
        print(f"   保存位置 = {token_path}")
        print("\n👉 现在去 AstrBot 面板点一下 mi_health 插件的「重新加载」, 即可生效。")


def main() -> None:
    print("=" * 60)
    print("  mi_health · token 刷新工具")
    print("=" * 60)

    token_path = _resolve_token_path()
    print(f"目标 token 路径: {token_path}")

    _backup_old_token(token_path)

    try:
        asyncio.run(_qr_login(token_path))
    except KeyboardInterrupt:
        print("\n👋 已取消。")
        sys.exit(130)


if __name__ == "__main__":
    main()
