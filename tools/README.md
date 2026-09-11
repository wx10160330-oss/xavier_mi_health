# mi_health · 工具目录

## 🔄 刷新 token (最常用)

**双击** `刷新token.bat` 即可,不用记命令。

流程:
1. 会自动使用 AstrBot 的 `.venv` 虚拟环境
2. 读取 `data/config/mi_health_config.json` 里配置的 `token_path`
3. 备份旧 token → `token.json.bak`
4. 终端里显示二维码
5. **用小号手机**的微信/系统相机扫码 (不要用小米运动健康 App 内扫码)
6. 二维码 5 分钟内有效, 超时重跑
7. 扫码成功后新 token 自动写到 `token_path`
8. 去 AstrBot 面板点一下 mi_health 插件「重新加载」即可

## 用命令行也行

```cmd
cd /d C:\path\to\AstrBot-master
.venv\Scripts\python.exe data\plugins\mi_health\tools\refresh_token.py
```

## 什么时候需要刷新?

- 对话里 `/health status` 显示 token 过期
- 任何一条 `/health` 指令报 401 / 未授权
- 插件日志里频繁出现 `AuthError` / `token expired`

## 常见问题

- **扫码失败**: 一定要用小号手机, 用微信/系统相机的扫一扫, 不能用小米运动健康 App
- **二维码超时**: 有效期 5 分钟, 磨蹭超时了就重跑
- **找不到 .venv**: 说明 AstrBot 不是用虚拟环境装的, 直接改用 `python refresh_token.py` 即可
