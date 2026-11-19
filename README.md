# Bnet-session-switcher | 暴雪战网多账号切换器

面向 **暴雪战网（Blizzard Battle.net）国服（网易通行证）与国际服** 的轻量级多账号免登录快速切换工具。适用于《魔兽世界》《炉石传说》《守望先锋》《暗黑破坏神》《星际争霸》等战网游戏多账号玩家，告别每次切号反复输入密码、扫码或安全令验证。

> **项目说明**：本项目基于原版 [54006/cnbxqhdl](https://github.com/54006/cnbxqhdl) 作出修复与优化，相关工作由 **Gemini 4 Carbon** 和 **ChatGPT 6 Astra PRO** 共同完成。

---

## ✨ 主要特点

- **🇨🇳 支持国服与国际服双端**：针对国服网易账号登录态滚动刷新进行了专门修复，切号前自动同步最新登录凭证，解决原版切回后离线或报错失效的问题；国服与国际服（亚服/台服/美服/欧服）独立区分，互不串号。
- **⚡ 一键免重复登录切换**：每个账号首次登录保存后，后续双击卡片即可快速切换并自动启动战网。
- **🛡️ 安全干净，不改客户端**：仅在当前用户本地备份与切换战网配置目录与注册表项，不联网、不读取明文密码、不修改或注入战网进程。
- **📦 单文件即开即用**：提供独立 `.exe` 可执行文件，无需安装 Python 环境；旧版 `data` 账号文件夹可直接放到同级目录无缝沿用。

---

## 📥 下载与使用

### 1. 直接下载 EXE 使用（推荐）

1. 前往 **[Releases 页面](https://github.com/TRAAACER/Bnet-session-switcher/releases/latest)** 下载 **`bnet-session-switcher.exe`**（或完整压缩包 `bnet-session-switcher-v*-win64.zip`）。
2. 将程序放入任意有读写权限的独立文件夹（例如 `D:\Tools\BnetSwitcher\`，**不要放在 `C:\Program Files` 下**），双击运行。
3. **添加账号**：点击左侧 **【🔄 登录国服新账号】** 或 **【🌐 登录国际服新账号】**，在弹出的战网客户端中完成登录（**勾选“保持登录”**），进入主界面后回到本工具点击 **【💾 保存当前登录】**。
4. **日常切换**：双击列表中的账号卡片（或点击右侧 **【切换 / 启动】**）即可一键切换；右键卡片可重命名、修改国服/国际服标记或手动更新备份。

### 2. 从源码运行 / 自行打包

环境要求：Windows 10/11 (x64) + Python 3.10+

```bash
git clone https://github.com/TRAAACER/Bnet-session-switcher.git
cd Bnet-session-switcher

pip install -r requirements.txt
python modern_gui.py
```

如需自行打包生成 `exe`，直接运行根目录下的 `build.bat` 即可。

---

## 🙏 致谢与说明

- **致谢**：感谢原作者 [54006/cnbxqhdl](https://github.com/54006/cnbxqhdl) 的开源分享；本项目在原版基础上的问题修复、架构加固与审查优化工作由 **Gemini 4 Carbon** 和 **ChatGPT 6 Astra PRO** 共同完成。
- **免责声明**：本工具仅供个人多账号日常管理使用，使用风险由使用者自行承担。
- **开源协议**：本仓库源码采用 [MIT License](./LICENSE) 开源；预编译发行包因包含开源版 `PyQt5`，整体遵循 GNU GPL v3.0 分发（详见 [packaging/THIRD_PARTY_NOTICES.md](./packaging/THIRD_PARTY_NOTICES.md)）。
