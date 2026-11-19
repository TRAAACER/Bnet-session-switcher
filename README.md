# bnet-session-switcher（暴雪战网会话切换器）

一个面向 **暴雪战网国服（网易通行证）与国际服** 的多账号本地会话隔离与快速切换工具。通过独立封存每个账号的会话缓存目录与完整注册表认证状态，并在切走或重开前对已核验匹配的活跃账号同步最新滚动凭证，实现便捷的多账号免重复登录切换。

> 本项目基于 [54006/cnbxqhdl](https://github.com/54006/cnbxqhdl) 重构与深度加固，重点解决了国服滚动登录态失效（如离线页、`BLZBNTBNA00000A8E`）、同账号重开覆盖新凭证、异常中断半残数据及跨区服串号等问题。

---

## ✨ 核心特性

- **🔁 滚动登录凭证自动同步**：国服战网/网易登录态在正常使用期间可能滚动刷新本地凭证。每次切换到其他账号、重开当前归属账号或准备新账号登录前，工具会先综合记忆账号名、登录区服、账户 UID 与日志状态核验身份；确认一致后以整目录代次原子回写最新状态（并自动保留上一份良好代次），若回写受阻则中止切换以保护本机最新凭证。
- **🔐 完整会话三件套隔离与范围语法校验**：独立保存每个账号的 `%LOCALAPPDATA%\Battle.net`、`%APPDATA%\Battle.net` 与 `HKCU\Software\Blizzard Entertainment\Battle.net` 注册表；导入前严格校验注册表文件头、编码、值语法与子树范围，清空旧树确认无残留后再导入。
- **🧾 持久事务日志与中断恢复**：切换、快照、准备新登录与账号删除均记录持久事务状态（`transaction_journal.json`）并区分 `COMMITTED` / `ROLLED_BACK` / `RECOVERY_REQUIRED` 终态；遇到占用自动回滚，若进程意外中断可在下次启动（战网及 Agent 完全退出状态下）完成恢复。
- **🌏 国服 & 国际服独立认证域**：按 `国服 (CN)` 与 `国际服 (KR / US / EU)` 区分账号与本机登录域，同邮箱分别登录国服与国际服不会互相覆盖；支持右键切换区服标记。
- **🛡️ 安全运行边界**：
  - **默认当前用户权限运行**：不强制整进程 UAC 提权，避免标准用户提权切换到另一管理员账户导致误读写他人 `HKCU`；跨机器/跨用户迁移 `data/` 目录时自动隔离源机器未决事务；
  - **双层单实例互斥锁**：结合当前用户 SID 互斥量与 `data/` 目录文件排他锁，阻止多开并发踩踏；
  - **严格重解析点与路径防护**：目录清理在进入子目录前先行拦截 NTFS 联接（junction/symlink），仅摘除链接本身，绝不触碰外部目录；日志自动脱敏邮箱与手机号。

---

## 🚀 快速开始

### 方式一：运行预编译发行包（推荐）

1. 从仓库 Releases 页面下载 `bnet-session-switcher-v*-win64.zip`，完整解压到当前用户有读写权限的普通目录（例如 `D:\Tools\bnet-session-switcher\`，**请勿放在 `C:\Program Files` 下**）。
2. 双击运行 `bnet-session-switcher.exe`，程序会在同级目录自动创建 `data/` 数据文件夹。
   > **升级与旧数据迁移**：将新版 `bnet-session-switcher.exe` 放在原程序同目录（或将旧版的 `data/` 文件夹复制到新程序同级目录），即可直接兼容读取原有的 `isolated_accounts.json`、`switcher_state.json` 及全部账号快照。

### 方式二：从源码运行

环境要求：Windows 10/11 (64-bit) + Python 3.10+ (x64，推荐已验证基准环境 Python 3.12.10 AMD64)

```bash
git clone https://github.com/TRAAACER/Bnet-session-switcher.git
cd Bnet-session-switcher

pip install -r requirements.txt
python modern_gui.py
```

### 自行构建与生成纯净发行包

双击或命令行运行根目录的 `build.bat`：

```bat
build.bat
```

脚本使用隔离的 `.venv-build` 环境与锁定的依赖清单（[requirements-build.lock.txt](./requirements-build.lock.txt)）完成打包，并通过 [stage_release.py](./stage_release.py) 执行递归白名单审计：
- `dist\bnet-session-switcher.exe`：保留同级已有的 `dist\data` 本地测试数据，供本机直接验证；
- `release\`：严格清空并递归校验通过的纯净发行目录（包含可执行文件、完整许可证正文、对应源码包 `corresponding-source.zip`、`BUILD_INFO.txt`、`SHA256SUMS.txt` 及打包好的 `bnet-session-switcher-v*-win64.zip`；若清理失败或混入任何非白名单文件将直接报错中止构建）。

---

## 📖 使用指南

1. **添加新账号**：
   - 点击左侧 **【🔄 登录国服新账号】** 或 **【🌐 登录国际服新账号】**，工具会先保护当前已保存账号的最新状态，再重置干净的本机登录环境并启动战网。
   - 在战网客户端完成登录（**建议勾选“保持登录”**），等待主界面完全加载。
   - 回到本工具点击 **【💾 保存当前登录】**（国际服点击 **【🌐 保存当前登录 (国际服)】**），输入备注昵称保存。
2. **日常切换与启动**：
   - **切换到其他账号**：双击目标卡片或点击右侧 **【切换】**。
   - **启动当前归属账号**：带有 `⚡ 当前归属` 标记的卡片按钮为 **【启动】**，点击会先同步本机最新滚动凭证到备份，再拉起战网。
   - **右键菜单**：支持 **切换/启动**、**用当前战网状态更新备份**、**重命名**、**切换国服/国际服标记**、**删除账号**。

---

## ⚠️ 使用边界与故障排查

- **凭证自然过期与服务端失效**：本工具管理的是客户端本地缓存与注册表凭证。若某个账号长期（如超过约 30 天）未登录、修改过密码或在服务端下线，本地凭证会自然失效；此时切到该账号手动登录一次，再右键卡片点击 **【💾 用当前战网状态更新备份】** 即可。
- **无记忆账号名的国际服账号**：若未勾选记忆账号名（`Battle.net.config` 中 `SavedAccountNames` 为空），工具不会凭历史残留日志自动覆写正式备份，而是在切走前将完整候选副本保存至 `data/candidates/`（若候选保存失败则中止切换以保留本机状态）；需要更新正式备份时，请在确认已登录该账号后右键执行手动更新。
- **调试日志位置**（已做邮箱与手机号脱敏）：`data\logs\` 目录下的 `snapshot_debug.log`、`robocopy_debug.log`、`reg_debug.log`、`restore_debug.log`、`switch_debug.log`。

---

## 🙏 致谢、免责声明与开源协议

- **致谢**：本项目衍生自 [54006/cnbxqhdl](https://github.com/54006/cnbxqhdl)，感谢原作者的开源分享。
- **免责声明**：本工具仅在当前操作系统用户权限下备份与替换战网本地配置目录及 `HKCU` 注册表项，不读取明文密码、不联网、不注入或修改客户端进程内存。仅供个人学习与多账号日常管理使用，使用风险由使用者自行承担。
- **开源协议说明**：
  - 本仓库自有源码采用 **[MIT License](./LICENSE)** 授权；
  - 预编译二进制发行包（`bnet-session-switcher.exe`）因包含开源版 `PyQt5`（GPLv3），组合发行物整体遵循 **[GNU GPL v3.0](./COPYING.GPLv3)** 条款分发，各组件许可证正文与对应源码获取方式详见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。
