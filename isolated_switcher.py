"""
bnet-session-switcher - 核心会话隔离、整代快照事务与故障恢复引擎 (v1.2.2)
"""
import _winapi
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
import winreg
from datetime import datetime

import psutil

from config import (
    APP_VERSION,
    CURRENT_USER_SID,
    DATA_DIR,
    LOGS_DIR,
    REG_EXE,
    ROBOCOPY_EXE,
    DataDirectoryLock,
    ensure_dirs,
)

MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_TAIL_READ_BYTES = 256 * 1024
MIN_FREE_DISK_MARGIN = 64 * 1024 * 1024  # 64MB 安全余量
REG_CMD_TIMEOUT = 30
ROBOCOPY_CMD_TIMEOUT = 300

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED_WIN_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_EMAIL_REDACT_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_REDACT_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_LOGOUT_LOG_RE = re.compile(
    r"(?:OnAuthLogout|ClearAutoLogin|User\s+logged\s+out|LogoutRequest|AuthenticationState.*LoggedOut)",
    re.IGNORECASE,
)
_DWORD_VAL_RE = re.compile(r"^dword:[0-9a-fA-F]{8}$", re.IGNORECASE)
_HEX_VAL_RE = re.compile(r"^hex(?:\([0-9a-fA-F]+\))?:(?:[0-9a-fA-F]{2}(?:,\s*[0-9a-fA-F]{2})*)?\s*$", re.IGNORECASE)


def is_admin() -> bool:
    """检查当前进程是否具有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_safe_account_id(account_id) -> bool:
    """严格校验账号 ID 为安全的单段标识符，拒绝路径穿越与 Windows 保留设备名"""
    if not isinstance(account_id, str) or not account_id:
        return False
    if not _SAFE_ID_RE.match(account_id):
        return False
    if account_id.upper() in _RESERVED_WIN_NAMES:
        return False
    return True


def redact_sensitive(text: str) -> str:
    """对调试日志中的邮箱、手机号进行脱敏处理"""
    if not text:
        return ""

    def _mask_email_match(m):
        user, domain = m.group(1), m.group(2)
        if len(user) <= 1:
            u_mask = "*"
        elif len(user) == 2:
            u_mask = user[0] + "*"
        else:
            u_mask = user[0] + "***" + user[-1]
        return f"{u_mask}@{domain}"

    masked = _EMAIL_REDACT_RE.sub(_mask_email_match, str(text))
    masked = _PHONE_REDACT_RE.sub(r"\1****\3", masked)
    return masked


def _query_limited_process_exe(pid: int) -> str:
    """使用 PROCESS_QUERY_LIMITED_INFORMATION (0x1000) 查询高权限进程的完整路径"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL

        h_proc = k32.OpenProcess(0x1000, False, int(pid))
        if not h_proc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value or ""
        finally:
            k32.CloseHandle(h_proc)
    except Exception:
        pass
    return ""


def find_battlenet_exe() -> str:
    """自动探测本机 Battle.net Launcher.exe 安装路径"""
    default_path = r"C:\Program Files (x86)\Battle.net\Battle.net Launcher.exe"
    candidates = [
        default_path,
        r"C:\Program Files\Battle.net\Battle.net Launcher.exe",
        r"D:\Program Files (x86)\Battle.net\Battle.net Launcher.exe",
        r"D:\Battle.net\Battle.net Launcher.exe",
        r"E:\Program Files (x86)\Battle.net\Battle.net Launcher.exe",
        r"E:\Battle.net\Battle.net Launcher.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    uninstall_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Battle.net"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Battle.net"),
    ]
    for root, subkey in uninstall_keys:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
                if install_dir:
                    exe_candidate = os.path.join(str(install_dir).strip('" '), "Battle.net Launcher.exe")
                    if os.path.isfile(exe_candidate):
                        return exe_candidate
        except Exception:
            pass

    reg_icon_keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Blizzard Entertainment\Battle.net\Capabilities", "ApplicationIcon"),
        (winreg.HKEY_CLASSES_ROOT, r"battlenet\shell\open\command", ""),
    ]
    for root, subkey, value_name in reg_icon_keys:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                raw_val, _ = winreg.QueryValueEx(key, value_name)
                if raw_val:
                    match = re.search(r'"([^"]+\.exe)"', str(raw_val), flags=re.IGNORECASE)
                    exe_ref = match.group(1) if match else str(raw_val).split(",")[0].strip('" ')
                    if exe_ref:
                        launcher = os.path.join(os.path.dirname(exe_ref), "Battle.net Launcher.exe")
                        if os.path.isfile(launcher):
                            return launcher
                        if os.path.isfile(exe_ref):
                            return exe_ref
        except Exception:
            pass

    try:
        for proc in psutil.process_iter(["name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if "battle.net" in name and exe and os.path.isfile(exe):
                launcher = os.path.join(os.path.dirname(exe), "Battle.net Launcher.exe")
                return launcher if os.path.isfile(launcher) else exe
    except Exception:
        pass

    return default_path


class IsolatedSwitcher:
    """独立数据目录会话切换器（具备整代事务提交、中断恢复、防串号与严格联接边界）"""

    BATTLENET_LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Battle.net")
    BATTLENET_ROAMING = os.path.join(os.environ.get("APPDATA", ""), "Battle.net")
    BATTLENET_REG_FULL_PATH = r"HKCU\Software\Blizzard Entertainment\Battle.net"
    BATTLENET_REG_SUBKEY = r"Software\Blizzard Entertainment\Battle.net"
    ALLOWED_REG_ROOT_LOWER = r"hkey_current_user\software\blizzard entertainment\battle.net"

    def __init__(self):
        self.dir_writable, self.writable_error = ensure_dirs(DATA_DIR, LOGS_DIR)
        self.BATTLENET_EXE = find_battlenet_exe()
        self.global_region = "KR"
        self.last_error = ""
        self.index_corrupted = False
        self.recovery_error = ""
        self.sid_mismatch_warning = ""
        self.invalid_entries = {}

        self.accounts_dir = os.path.join(DATA_DIR, "isolated_accounts")
        self.prev_gens_dir = os.path.join(DATA_DIR, "previous_generations")
        self.candidates_dir = os.path.join(DATA_DIR, "candidates")
        self.accounts_file = os.path.join(DATA_DIR, "isolated_accounts.json")
        self.state_file = os.path.join(DATA_DIR, "switcher_state.json")
        self.journal_file = os.path.join(DATA_DIR, "transaction_journal.json")

        self._own_data_lock = None
        if self.dir_writable:
            os.makedirs(self.accounts_dir, exist_ok=True)

        self.accounts = self._load_accounts()
        self.current_account_id = self._load_current_account()

        # RR04：必须先确认不存在跨 Windows 用户 SID 冲突，才允许执行本机 live 事务恢复与垃圾清理
        if self.dir_writable and not self.index_corrupted:
            if self.sid_mismatch_warning:
                self._quarantine_foreign_journal_if_needed()
            else:
                self._recover_pending_transactions()
                if not self.recovery_error and not os.path.exists(self.journal_file):
                    self._cleanup_managed_temp_artifacts()

    # ------------------------------------------------------------------
    # 日志、JSON 原子读写与受信任系统命令执行
    # ------------------------------------------------------------------

    def _write_debug_log(self, file_name: str, message: str):
        """写入自动脱敏与大小轮转的调试日志"""
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            log_path = os.path.join(LOGS_DIR, file_name)
            if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_BYTES:
                os.replace(log_path, log_path + ".1")
            safe_msg = redact_sensitive(message)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] [v{APP_VERSION}] {safe_msg}\n")
        except Exception:
            pass

    @staticmethod
    def _atomic_write_json(path: str, data: dict, encoding: str = "utf-8", indent: int = 2):
        """通过同目录临时文件 + fsync + os.replace 原子写入 JSON"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp_path = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(tmp_path, "w", encoding=encoding) as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _run_external_cmd(self, args: list, timeout: int) -> tuple:
        """执行带超时与绝对路径校验的系统命令。返回 (returncode, stdout, stderr, timed_out)"""
        exe_path = args[0]
        if not os.path.isfile(exe_path):
            msg = f"未找到受信任的系统工具: {exe_path}"
            self._write_debug_log("cmd_debug.log", msg)
            return -1, "", msg, False
        try:
            res = subprocess.run(
                args,
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return res.returncode, res.stdout or "", res.stderr or "", False
        except subprocess.TimeoutExpired as te:
            self._write_debug_log("cmd_debug.log", f"command timeout ({timeout}s): {exe_path}")
            return -1, "", f"命令执行超时 ({timeout}s): {te}", True
        except Exception as e:
            self._write_debug_log("cmd_debug.log", f"command error: {exe_path}, error={e}")
            return -1, "", str(e), False

    # ------------------------------------------------------------------
    # 状态加载、Schema 规范化、SID 归属检查与操作门禁（修复 RR04, RR06, O03）
    # ------------------------------------------------------------------

    def _check_operation_allowed(self) -> tuple:
        """破坏性写操作统一前置门禁：同步检查内存状态与磁盘未决事务日志"""
        if not self.dir_writable:
            return False, self.writable_error or "数据目录不可写"
        if self.index_corrupted:
            return (
                False,
                f"账号索引文件损坏，已启用只读保护以防丢失已有备份。\n请检查 {self.accounts_file} 及同目录下的 .corrupt 备份文件。",
            )
        if self.recovery_error:
            return (
                False,
                f"检测到未完成的会话切换/快照恢复受阻，已暂停写入以保护备份材料：\n{self.recovery_error}",
            )
        if os.path.exists(self.journal_file):
            try:
                with open(self.journal_file, "r", encoding="utf-8-sig") as jf:
                    j_peek = json.load(jf)
                if isinstance(j_peek, dict) and j_peek.get("state") in ("COMMITTED", "ROLLED_BACK"):
                    self._cleanup_journal_artifacts(j_peek)
            except Exception:
                pass
        if os.path.exists(self.journal_file):
            return (
                False,
                f"检测到未决事务记录（{self.journal_file}），已阻止新写操作覆盖旧恢复材料。请先关闭战网后重启本工具完成自动恢复。",
            )
        if self.sid_mismatch_warning:
            return (
                False,
                f"{self.sid_mismatch_warning}\n请先确认将数据目录绑定到当前 Windows 用户后再执行写入或切换操作。",
            )
        return True, ""

    def _load_accounts(self) -> dict:
        """加载并逐条校验 isolated_accounts.json（修复 O03：规范化 battletag 等全部字段类型）"""
        if not os.path.exists(self.accounts_file):
            self.index_corrupted = False
            return {}
        try:
            with open(self.accounts_file, "r", encoding="utf-8-sig") as f:
                raw_data = json.load(f)
            if not isinstance(raw_data, dict):
                raise ValueError("顶层 JSON 结构不是对象字典")
        except Exception as e:
            self.index_corrupted = True
            self._write_debug_log("config_debug.log", f"load isolated_accounts.json failed: {e}")
            try:
                backup = f"{self.accounts_file}.corrupt.{int(time.time())}"
                if not os.path.exists(backup):
                    shutil.copy2(self.accounts_file, backup)
            except Exception:
                pass
            return {}

        valid_accounts = {}
        self.invalid_entries = {}
        for acc_id, raw_info in raw_data.items():
            if not is_safe_account_id(acc_id) or not isinstance(raw_info, dict):
                self.invalid_entries[str(acc_id)] = raw_info
                self._write_debug_log("config_debug.log", f"quarantined invalid account record key={acc_id}")
                continue

            entry = dict(raw_info)
            nickname = entry.get("nickname")
            entry["nickname"] = str(nickname).strip() if isinstance(nickname, str) and nickname.strip() else str(acc_id)

            email = entry.get("email")
            entry["email"] = str(email).strip() if isinstance(email, str) else ""

            btag = entry.get("battletag")
            entry["battletag"] = str(btag).strip() if isinstance(btag, str) and btag.strip() else None

            version = entry.get("version")
            entry["version"] = version if version in ("cn", "global") else "cn"
            entry["logged_in"] = bool(entry.get("logged_in", False))

            last_login = entry.get("last_login")
            entry["last_login"] = str(last_login) if isinstance(last_login, str) else ""
            valid_accounts[acc_id] = entry

        if self.invalid_entries and self.dir_writable:
            try:
                quarantine_file = os.path.join(DATA_DIR, "quarantined_accounts_records.json")
                self._atomic_write_json(quarantine_file, self.invalid_entries)
            except Exception:
                pass

        self.index_corrupted = False
        return valid_accounts

    def _save_accounts(self):
        if self.index_corrupted:
            raise RuntimeError("账号索引处于损坏保护状态，禁止写回覆盖原文件")
        self._atomic_write_json(self.accounts_file, self.accounts)

    def _load_current_account(self):
        """加载 switcher_state.json，校验类型与 Windows 用户 SID 归属"""
        if not os.path.exists(self.state_file):
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8-sig") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return None

            recorded_sid = state.get("owner_sid")
            if isinstance(recorded_sid, str) and recorded_sid and recorded_sid != CURRENT_USER_SID:
                self.sid_mismatch_warning = (
                    f"数据目录原归属 Windows 用户 ({recorded_sid}) 与当前运行用户 ({CURRENT_USER_SID}) 不一致。"
                    "为防止跨用户/跨机器混淆本机注册表与缓存，已暂停本机反向回写与旧本机事务回放。"
                )
                self._write_debug_log("config_debug.log", self.sid_mismatch_warning)

            cid = state.get("current_account_id")
            if cid is None:
                return None
            if isinstance(cid, str) and is_safe_account_id(cid):
                return cid
        except Exception as e:
            self._write_debug_log("config_debug.log", f"load switcher_state failed: {e}")
        return None

    def _save_current_account(self, account_id, bind_current_sid: bool = True):
        """先持久化 switcher_state.json 成功，再更新内存 self.current_account_id"""
        if account_id is not None and not is_safe_account_id(account_id):
            raise ValueError(f"非法账号标识符: {account_id}")

        payload = {
            "current_account_id": account_id,
            "owner_sid": CURRENT_USER_SID if bind_current_sid else None,
            "updated_at": datetime.now().isoformat(),
        }
        self._atomic_write_json(self.state_file, payload)
        self.current_account_id = account_id
        if bind_current_sid:
            self.sid_mismatch_warning = ""

    def _quarantine_foreign_journal_if_needed(self):
        """
        跨用户/跨机迁移 data 目录时（RR04）：
        绝不把源机器未完成的 live switch/prepare 事务（及源用户注册表）回放进当前用户的 HKCU，而是将其隔离归档。
        """
        if not os.path.exists(self.journal_file):
            return
        try:
            with open(self.journal_file, "r", encoding="utf-8-sig") as f:
                journal = json.load(f)
            if not isinstance(journal, dict):
                return
            op = journal.get("op")
            txid = journal.get("txid", int(time.time()))
            if op in ("switch", "prepare"):
                archive_path = f"{self.journal_file}.foreign_{txid}.json"
                os.replace(self.journal_file, archive_path)
                self._write_debug_log(
                    "restore_debug.log",
                    f"quarantined foreign live journal ({op}, tx={txid}) without touching current HKCU: {archive_path}",
                )
        except Exception as e:
            self.recovery_error = f"隔离跨用户未决事务日志失败：{e}"

    def rebind_current_user_sid(self) -> tuple:
        """用户显式确认在迁移机器/用户后重新绑定当前 Windows 用户 SID（不回放源机器旧 live 事务）"""
        try:
            self._quarantine_foreign_journal_if_needed()
            if os.path.exists(self.journal_file):
                # 若剩余的是纯 data/isolated_accounts 内部的 snapshot/delete 事务，可在绑定前安全自愈
                self._recover_pending_transactions()
            self._save_current_account(None, bind_current_sid=True)
            return True, "已将当前数据目录安全绑定到当前 Windows 用户（未改动当前战网本机登录态）"
        except Exception as e:
            return False, f"绑定失败：{e}"

    # ------------------------------------------------------------------
    # 严格祖先重解析点校验与递归 scandir 删除（修复 RR02）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_junction(path: str) -> bool:
        """使用 Win32 FILE_ATTRIBUTE_REPARSE_POINT (0x400) 检测任何 NTFS junction 或 symlink"""
        try:
            if not os.path.lexists(path):
                return False
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == -1:
                return False
            return bool(attrs & 0x400)
        except Exception:
            return False

    @classmethod
    def _has_reparse_between(cls, base_root: str, target_path: str) -> bool:
        """
        检查从受管根目录 base_root（不含其父级系统挂载，但含 base_root 自身及以下每一级祖先）
        到 target_path 父目录之间是否存在任何重解析点（防止祖先联接绕过 commonpath 词法检查，修复 RR02 D02）。
        """
        abs_root = os.path.abspath(base_root)
        abs_target = os.path.abspath(target_path)
        try:
            if os.path.commonpath([abs_root, abs_target]) != abs_root:
                return True
        except ValueError:
            return True

        if os.path.lexists(abs_root) and cls._is_junction(abs_root):
            return True

        rel = os.path.relpath(abs_target, abs_root)
        if rel in (".", ""):
            return False

        parts = rel.split(os.sep)
        # 检查所有中间祖先目录（不含最终叶子本身，叶子由调用方按联接或目录分别处理）
        curr = abs_root
        for seg in parts[:-1]:
            if seg in ("", "."):
                continue
            if seg == "..":
                return True
            curr = os.path.join(curr, seg)
            if os.path.lexists(curr) and cls._is_junction(curr):
                return True
        return False

    def _resolve_account_path(self, account_id: str, *subparts: str) -> str:
        """严格校验并解析位于 accounts_dir 内部的子路径，拒绝越界与任何层级重解析点"""
        if not is_safe_account_id(account_id):
            raise ValueError(f"非法账号 ID: {account_id!r}")
        root_abs = os.path.abspath(self.accounts_dir)
        if os.path.lexists(root_abs) and self._is_junction(root_abs):
            raise ValueError(f"拒绝访问作为重解析点的账号根目录: {root_abs}")

        acc_root = os.path.abspath(os.path.join(root_abs, account_id))
        if os.path.commonpath([root_abs, acc_root]) != root_abs or acc_root == root_abs:
            raise ValueError(f"账号路径越出受管根目录: {account_id!r}")
        if os.path.lexists(acc_root) and self._is_junction(acc_root):
            raise ValueError(f"拒绝访问作为重解析点的账号目录: {acc_root}")

        full = os.path.abspath(os.path.join(acc_root, *subparts)) if subparts else acc_root
        if os.path.commonpath([acc_root, full]) != acc_root:
            raise ValueError(f"账号子路径越界: {full}")
        if self._has_reparse_between(acc_root, full):
            raise ValueError(f"账号子路径包含非法的祖先重解析点: {full}")
        return full

    def get_account_dir(self, account_id: str) -> str:
        return self._resolve_account_path(account_id)

    def get_account_local_dir(self, account_id: str) -> str:
        return self._resolve_account_path(account_id, "LocalAppData")

    def get_account_roaming_dir(self, account_id: str) -> str:
        return self._resolve_account_path(account_id, "Roaming")

    def get_account_full_reg_file(self, account_id: str) -> str:
        return self._resolve_account_path(account_id, "battlenet_full_reg.reg")

    @staticmethod
    def _read_junction_target(path: str):
        try:
            target = os.readlink(path)
            if target.startswith("\\\\?\\"):
                target = target[4:]
            return target
        except Exception:
            return None

    @staticmethod
    def _create_junction_win32(target_dir: str, link_path: str) -> bool:
        try:
            _winapi.CreateJunction(os.path.abspath(target_dir), os.path.abspath(link_path))
            return os.path.lexists(link_path)
        except Exception:
            return False

    @staticmethod
    def _unlink_junction_only(path: str) -> bool:
        """仅移除目录联接或符号链接本身，绝不进入目标目录"""
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
            os.rmdir(path)
            return not os.path.lexists(path)
        except Exception:
            try:
                os.unlink(path)
                return not os.path.lexists(path)
            except Exception:
                try:
                    res = ctypes.windll.kernel32.RemoveDirectoryW(str(path))
                    return bool(res) or not os.path.lexists(path)
                except Exception:
                    return not os.path.lexists(path)

    def _is_within_allowed_delete_roots(self, path: str) -> bool:
        """白名单根检查 + 祖先重解析点拦截（修复 RR02 D02）"""
        abs_p = os.path.abspath(path)
        _drive, tail = os.path.splitdrive(abs_p)
        if not tail or tail in ("\\", "/"):
            return False

        abs_data = os.path.abspath(DATA_DIR)
        try:
            if os.path.commonpath([abs_data, abs_p]) == abs_data and abs_p != abs_data:
                if self._has_reparse_between(abs_data, abs_p):
                    return False
                return True
        except ValueError:
            pass

        for live_base in (self.BATTLENET_LOCAL, self.BATTLENET_ROAMING):
            if not live_base:
                continue
            abs_live = os.path.abspath(live_base)
            live_parent = os.path.dirname(abs_live)
            live_name = os.path.basename(abs_live)
            if os.path.lexists(live_parent) and self._is_junction(live_parent):
                return False
            if abs_p == abs_live:
                return True
            if os.path.dirname(abs_p) == live_parent and os.path.basename(abs_p).startswith(live_name + "."):
                return True
        return False

    def _rmtree_scandir_no_reparse(self, dir_path: str):
        """
        基于 os.scandir 的先验重解析点拦截递归删除（修复 RR02 D01）：
        在进入任何子目录之前先用 Win32 GetFileAttributesW 检查重解析属性，遇到嵌套 junction 只摘除链接本身，
        绝不信任 CPython os.walk(topdown=False) 对 Windows junction 的遍历行为。
        """
        if self._is_junction(dir_path) or os.path.islink(dir_path):
            self._unlink_junction_only(dir_path)
            return

        with os.scandir(dir_path) as it:
            entries = list(it)

        for entry in entries:
            entry_path = entry.path
            if self._is_junction(entry_path) or entry.is_symlink():
                self._unlink_junction_only(entry_path)
            elif entry.is_dir(follow_symlinks=False):
                self._rmtree_scandir_no_reparse(entry_path)
            else:
                try:
                    os.chmod(entry_path, stat.S_IWRITE)
                except Exception:
                    pass
                os.unlink(entry_path)

        try:
            os.chmod(dir_path, stat.S_IWRITE)
        except Exception:
            pass
        os.rmdir(dir_path)

    def _remove_dir_or_junction(self, path: str) -> bool:
        """纯 Python/Win32 安全删除目录或联接（零 cmd.exe 调用，防嵌套与祖先 junction 逃逸）"""
        if not os.path.lexists(path):
            return True
        if not self._is_within_allowed_delete_roots(path):
            self._write_debug_log("remove_debug.log", f"blocked out-of-bounds or reparse-ancestor delete: {path}")
            return False

        try:
            if self._is_junction(path) or os.path.islink(path):
                return self._unlink_junction_only(path)

            if not os.path.isdir(path):
                try:
                    os.chmod(path, stat.S_IWRITE)
                except Exception:
                    pass
                os.unlink(path)
                return not os.path.lexists(path)

            self._rmtree_scandir_no_reparse(path)
            return not os.path.lexists(path)
        except Exception as e:
            self._write_debug_log("remove_debug.log", f"remove path failed: {path}, error={e}")
            return not os.path.lexists(path)

    def _cleanup_invalid_junctions(self):
        for path in (self.BATTLENET_LOCAL, self.BATTLENET_ROAMING):
            if os.path.lexists(path) and self._is_junction(path):
                try:
                    os.listdir(path)
                except OSError:
                    self._unlink_junction_only(path)

    @staticmethod
    def _directory_has_data(path: str) -> bool:
        try:
            if not os.path.isdir(path):
                return False
            with os.scandir(path) as it:
                return any(True for _ in it)
        except Exception:
            return False

    @staticmethod
    def _estimate_dir_size(path: str) -> int:
        total = 0
        try:
            for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
                dirs[:] = [d for d in dirs if not IsolatedSwitcher._is_junction(os.path.join(root, d))]
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
        except Exception:
            pass
        return total

    def _check_disk_space(self, sources: list, target_dir: str) -> tuple:
        try:
            probe_dir = os.path.abspath(target_dir)
            while not os.path.exists(probe_dir):
                parent = os.path.dirname(probe_dir)
                if parent == probe_dir:
                    break
                probe_dir = parent
            free_bytes = shutil.disk_usage(probe_dir).free
            needed = sum(self._estimate_dir_size(s) for s in sources if s and os.path.exists(s)) + MIN_FREE_DISK_MARGIN
            if free_bytes < needed:
                free_mb = free_bytes // (1024 * 1024)
                need_mb = needed // (1024 * 1024)
                return False, f"目标磁盘可用空间不足（需要约 {need_mb} MB，当前仅剩 {free_mb} MB）"
            return True, ""
        except Exception:
            return True, ""

    @staticmethod
    def _robocopy_succeeded(return_code: int) -> bool:
        return 0 <= int(return_code) <= 7

    def _run_robocopy(self, source: str, target: str, mirror: bool = False, exclude_dirs: list = None) -> tuple:
        if not os.path.exists(source):
            return False, f"源目录不存在: {source}", None
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        mode = "/MIR" if mirror else "/E"
        args = [
            ROBOCOPY_EXE, source, target, mode,
            "/XJ", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS",
        ]
        if exclude_dirs:
            args.extend(["/XD"] + exclude_dirs)

        rc, stdout, stderr, timed_out = self._run_external_cmd(args, timeout=ROBOCOPY_CMD_TIMEOUT)
        if not timed_out and self._robocopy_succeeded(rc):
            return True, "ok", rc
        msg = f"robocopy 失败: {source} -> {target}, rc={rc}, timeout={timed_out}, stdout={stdout.strip()}, stderr={stderr.strip()}"
        self._write_debug_log("robocopy_debug.log", msg)
        if timed_out:
            return False, "复制目录超时，请检查磁盘状态或杀毒软件拦截", rc
        return False, f"复制目录失败（robocopy 返回码 {rc}）", rc

    # ------------------------------------------------------------------
    # 注册表状态核验、.reg 键域与值级语法校验（修复 CR09, CR25, O01）
    # ------------------------------------------------------------------

    def _get_registry_state(self) -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.BATTLENET_REG_SUBKEY, 0, winreg.KEY_READ):
                return "EXISTS"
        except FileNotFoundError:
            return "ABSENT"
        except OSError as e:
            if getattr(e, "winerror", None) == 2:
                return "ABSENT"
            self._write_debug_log("reg_debug.log", f"registry state unreadable: {e}")
            return "ERROR"

    @staticmethod
    def _ends_with_unescaped_quote(s: str) -> bool:
        if len(s) < 2 or not s.startswith('"') or not s.endswith('"'):
            return False
        backslashes = 0
        idx = len(s) - 2
        while idx >= 1 and s[idx] == "\\":
            backslashes += 1
            idx -= 1
        return (backslashes % 2) == 0

    @classmethod
    def _validate_reg_file(cls, reg_file: str) -> tuple:
        """
        严格校验 .reg 备份文件：
        1. 校验 UTF-16 / UTF-8 编码与注册表版本头；
        2. 校验无悬空反斜杠续行（D09）；
        3. 校验全部节严格位于 HKCU\\Software\\Blizzard Entertainment\\Battle.net 子树，拒绝删除节 [-...]；
        4. 校验字符串引号闭合（D08）、DWORD 8位十六进制（D10）、hex 字节序列语法及拒绝值删除（=-）。
        """
        if not os.path.isfile(reg_file):
            return False, "注册表备份文件不存在"
        try:
            with open(reg_file, "rb") as f:
                raw = f.read()
        except Exception as e:
            return False, f"无法读取注册表备份文件: {e}"

        if len(raw) < 32:
            return False, "注册表备份文件过小或已截断"

        try:
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                text = raw.decode("utf-16")
            else:
                text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False, "注册表备份文件编码损坏"

        if "\x00" in text:
            return False, "注册表备份文件包含非法空字节"

        raw_lines = text.splitlines()
        logical_lines = []
        continuing = ""
        for raw_ln in raw_lines:
            stripped = raw_ln.strip()
            if not continuing:
                if not stripped or stripped.startswith(";"):
                    continue
                if stripped.endswith("\\") and not stripped.startswith('"') and "=" in stripped:
                    continuing = stripped[:-1].strip()
                elif stripped.endswith("\\") and stripped.startswith('"') and re.search(r"=\s*hex", stripped, re.IGNORECASE):
                    continuing = stripped[:-1].strip()
                else:
                    logical_lines.append(stripped)
            else:
                if stripped.endswith("\\"):
                    continuing += stripped[:-1].strip()
                else:
                    continuing += stripped
                    logical_lines.append(continuing)
                    continuing = ""

        if continuing:
            return False, "注册表备份文件在多行续行处意外截断"

        if not logical_lines:
            return False, "注册表备份文件内容为空"

        header = logical_lines[0]
        if header not in ("Windows Registry Editor Version 5.00", "REGEDIT4"):
            return False, f"注册表文件头无效: {header[:40]}"

        section_count = 0
        in_section = False
        allowed_root = cls.ALLOWED_REG_ROOT_LOWER

        for ln in logical_lines[1:]:
            if ln.startswith("["):
                if not ln.endswith("]"):
                    return False, "注册表备份文件存在未闭合的节头"
                inner = ln[1:-1].strip()
                if inner.startswith("-"):
                    return False, "拒绝导入包含删除指令 [-...] 的注册表文件"
                lower_key = inner.lower()
                if lower_key != allowed_root and not lower_key.startswith(allowed_root + "\\"):
                    return False, f"拒绝导入越出 Battle.net 范围的注册表项: {inner}"
                section_count += 1
                in_section = True
                continue

            if not in_section:
                return False, "注册表赋值项出现在任何有效节之前"

            # 解析键值赋值行: @=... 或 "Name"=...
            if ln.startswith("@="):
                rhs = ln[2:].strip()
            elif ln.startswith('"'):
                # 查找未转义闭合引号后的 '='
                i = 1
                escaped = False
                end_quote = -1
                while i < len(ln):
                    ch = ln[i]
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        end_quote = i
                        break
                    i += 1
                if end_quote == -1:
                    return False, "注册表键名字符串未闭合"
                rest = ln[end_quote + 1:].lstrip()
                if not rest.startswith("="):
                    return False, "注册表赋值行缺少等号"
                rhs = rest[1:].strip()
            else:
                return False, f"无法识别的注册表语法行: {ln[:40]}"

            if rhs == "-":
                return False, "拒绝导入包含键值删除指令 (=-) 的注册表文件"
            if rhs.startswith('"'):
                if not cls._ends_with_unescaped_quote(rhs):
                    return False, "注册表字符串值未闭合（可能已截断）"
            elif rhs.lower().startswith("dword:"):
                if not _DWORD_VAL_RE.match(rhs):
                    return False, f"非法的注册表 DWORD 值格式: {rhs[:30]}"
            elif rhs.lower().startswith("hex"):
                if not _HEX_VAL_RE.match(rhs):
                    return False, f"非法的注册表 HEX 值格式: {rhs[:40]}"
            else:
                return False, f"不支持的注册表值类型: {rhs[:30]}"

        if section_count == 0:
            return False, "注册表备份文件未包含任何有效注册表节"
        return True, "ok"

    def _export_full_registry_to_file(self, reg_file: str) -> bool:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(reg_file)), exist_ok=True)
            rc, _out, err, timed_out = self._run_external_cmd(
                [REG_EXE, "export", self.BATTLENET_REG_FULL_PATH, reg_file, "/y"],
                timeout=REG_CMD_TIMEOUT,
            )
            if timed_out or rc != 0:
                self._write_debug_log("reg_debug.log", f"export failed rc={rc}, timeout={timed_out}, err={err.strip()}")
                return False
            valid, reason = self._validate_reg_file(reg_file)
            if not valid:
                self._write_debug_log("reg_debug.log", f"exported reg invalid ({reg_file}): {reason}")
                return False
            return True
        except Exception as e:
            self._write_debug_log("reg_debug.log", f"export exception: {e}")
            return False

    def _clear_live_registry(self) -> bool:
        state = self._get_registry_state()
        if state == "ABSENT":
            return True
        if state == "ERROR":
            return False

        rc, _out, err, timed_out = self._run_external_cmd(
            [REG_EXE, "delete", self.BATTLENET_REG_FULL_PATH, "/f"],
            timeout=REG_CMD_TIMEOUT,
        )
        post_state = self._get_registry_state()
        ok = (not timed_out) and (rc == 0) and (post_state == "ABSENT")
        if not ok:
            self._write_debug_log(
                "restore_debug.log",
                f"clear registry failed: rc={rc}, timeout={timed_out}, post_state={post_state}, err={err.strip()}",
            )
        return ok

    def _restore_registry_file(self, reg_file: str) -> bool:
        valid, reason = self._validate_reg_file(reg_file)
        if not valid:
            self._write_debug_log("restore_debug.log", f"reg file validation rejected ({reg_file}): {reason}")
            return False

        if not self._clear_live_registry():
            return False

        rc, _out, err, timed_out = self._run_external_cmd(
            [REG_EXE, "import", reg_file],
            timeout=REG_CMD_TIMEOUT,
        )
        ok = (not timed_out) and (rc == 0) and (self._get_registry_state() == "EXISTS")
        self._write_debug_log(
            "restore_debug.log",
            f"import registry {reg_file}: rc={rc}, timeout={timed_out}, ok={ok}, err={err.strip()}",
        )
        return ok

    def backup_full_registry(self, account_id: str) -> bool:
        return self._export_full_registry_to_file(self.get_account_full_reg_file(account_id))

    def restore_full_registry(self, account_id: str) -> bool:
        return self._restore_registry_file(self.get_account_full_reg_file(account_id))

    # ------------------------------------------------------------------
    # 持久事务日志（含 ROLLED_BACK 终态、归属校验、崩溃自愈与删除恢复：修复 RR03, RR04, RR06, RR07, RR08）
    # ------------------------------------------------------------------

    def _write_journal(self, journal_data: dict):
        payload = dict(journal_data)
        payload.setdefault("owner_sid", CURRENT_USER_SID)
        payload.setdefault("live_local_root", os.path.abspath(self.BATTLENET_LOCAL))
        payload.setdefault("live_roaming_root", os.path.abspath(self.BATTLENET_ROAMING))
        self._atomic_write_json(self.journal_file, payload)

    def _clear_journal(self):
        if os.path.exists(self.journal_file):
            os.remove(self.journal_file)

    def _mark_recovery_required(self, journal: dict, reason: str):
        self.recovery_error = reason
        if isinstance(journal, dict):
            journal["state"] = "RECOVERY_REQUIRED"
            journal["last_error"] = reason
            try:
                self._write_journal(journal)
            except Exception:
                pass
        self._write_debug_log("restore_debug.log", f"RECOVERY_REQUIRED: {reason}")

    def _capture_path_spec(self, path: str, old_backup_path: str) -> dict:
        if not os.path.lexists(path):
            return {"kind": "missing", "target": path, "old_path": None, "junction_target": None}
        if self._is_junction(path):
            return {
                "kind": "junction",
                "target": path,
                "old_path": None,
                "junction_target": self._read_junction_target(path),
            }
        return {"kind": "dir", "target": path, "old_path": old_backup_path, "junction_target": None}

    def _move_live_aside(self, spec: dict):
        target = spec["target"]
        kind = spec["kind"]
        if kind == "missing":
            return
        if kind == "junction":
            if not self._unlink_junction_only(target):
                raise RuntimeError(f"无法移除旧目录联接: {target}")
            return
        old_path = spec["old_path"]
        if os.path.lexists(old_path):
            if not self._remove_dir_or_junction(old_path):
                raise RuntimeError(f"无法清理残留备份路径: {old_path}")
        os.rename(target, old_path)

    def _restore_single_live_spec(self, spec: dict, txid: str) -> bool:
        target = spec["target"]
        kind = spec["kind"]
        failed_aside = f"{target}.__rollback_aside_{txid}"

        try:
            if os.path.lexists(target):
                if self._is_junction(target):
                    if not self._unlink_junction_only(target):
                        return False
                else:
                    if kind == "dir" and spec.get("old_path") and not os.path.lexists(spec["old_path"]):
                        return True
                    self._remove_dir_or_junction(failed_aside)
                    os.rename(target, failed_aside)

            if kind == "missing":
                ok = not os.path.lexists(target)
            elif kind == "junction":
                j_target = spec.get("junction_target")
                ok = self._create_junction_win32(j_target, target) if j_target else True
            elif kind == "dir":
                old_path = spec.get("old_path")
                if old_path and os.path.lexists(old_path):
                    os.rename(old_path, target)
                ok = os.path.isdir(target)
            else:
                ok = True

            if ok and os.path.lexists(failed_aside):
                self._remove_dir_or_junction(failed_aside)
            return ok
        except Exception as e:
            self._write_debug_log("restore_debug.log", f"restore live spec failed ({target}): {e}")
            return False

    def _archive_prev_generation_safely(self, account_id: str, prev_dir: str) -> bool:
        """
        将提交后的上一代备份目录原子归档至 previous_generations/<id>（修复 N01 / FG03）：
        若目标位置清理或移动失败，完整保留原 prev_dir 不删除，待下次幂等收尾重试。
        """
        if not prev_dir or not os.path.lexists(prev_dir):
            return True
        if not is_safe_account_id(account_id):
            return False
        try:
            os.makedirs(self.prev_gens_dir, exist_ok=True)
            last_good = os.path.join(self.prev_gens_dir, account_id)
            old_good_aside = os.path.join(self.prev_gens_dir, f".old_{account_id}_{uuid.uuid4().hex[:6]}")
            moved_old_good = False
            if os.path.lexists(last_good):
                self._remove_dir_or_junction(old_good_aside)
                os.rename(last_good, old_good_aside)
                moved_old_good = True
            try:
                os.rename(prev_dir, last_good)
            except Exception:
                if moved_old_good and os.path.lexists(old_good_aside) and not os.path.lexists(last_good):
                    try:
                        os.rename(old_good_aside, last_good)
                    except Exception:
                        pass
                return False
            if moved_old_good and os.path.lexists(old_good_aside):
                self._remove_dir_or_junction(old_good_aside)
            return not os.path.lexists(prev_dir)
        except Exception as e:
            self._write_debug_log("snapshot_debug.log", f"archive prev generation deferred ({account_id}): {e}")
            return False

    def _cleanup_journal_artifacts(self, journal: dict):
        """对已确认到达终态（COMMITTED / ROLLED_BACK）的事务执行幂等收尾清理"""
        op = journal.get("op")
        state = journal.get("state")
        acc_id = journal.get("account_id")
        prev_dir = journal.get("prev_dir")

        # 若是已 COMMITTED 的快照，优先将 prev_dir 归档为 previous_generations/<id> 而非直接擦除
        if op == "snapshot" and state == "COMMITTED" and prev_dir and os.path.lexists(prev_dir):
            if not self._archive_prev_generation_safely(acc_id, prev_dir):
                # 归档暂未成功时保留 prev_dir 与 COMMITTED 日志，留待下次重试
                return

        if op == "delete" and state == "COMMITTED" and is_safe_account_id(acc_id):
            prev_gen = os.path.join(self.prev_gens_dir, acc_id)
            if os.path.lexists(prev_gen):
                self._remove_dir_or_junction(prev_gen)

        trash_dir = journal.get("trash_dir")
        allow_trash_delete = (
            op != "delete"
            or state == "COMMITTED"
            or (is_safe_account_id(acc_id) and os.path.isdir(self.get_account_dir(acc_id)))
        )

        for p in (
            prev_dir,
            journal.get("stage_dir"),
            trash_dir if allow_trash_delete else None,
            journal.get("tmp_local"),
            journal.get("tmp_roaming"),
            (journal.get("local_spec") or {}).get("old_path"),
            (journal.get("roaming_spec") or {}).get("old_path"),
        ):
            if p and os.path.lexists(p):
                self._remove_dir_or_junction(p)
        pre_reg = journal.get("pre_reg_file")
        if pre_reg and os.path.exists(pre_reg):
            try:
                os.remove(pre_reg)
            except Exception:
                pass
        try:
            self._clear_journal()
        except Exception as e:
            self._write_debug_log("restore_debug.log", f"deferred journal file unlink: {e}")

    def _rollback_uncommitted_delete(self, journal: dict) -> tuple:
        """
        统一幂等回退未提交的删除事务（修复 FG04 S3T18 / S3T19 / S3T20）：
        只要未写入 COMMITTED 终态，一律恢复原目录、原磁盘索引与原 current_account_id，绝不凭“无 trash”猜测已删除。
        """
        acc_id = journal.get("account_id")
        trash_dir = journal.get("trash_dir")
        removed_rec = journal.get("removed_record")
        prev_cid = journal.get("prev_current_account_id")
        had_acc_dir = bool(journal.get("had_acc_dir", True))

        if not is_safe_account_id(acc_id):
            err = f"未决删除事务账号 ID 非法: {acc_id}"
            self._mark_recovery_required(journal, err)
            return False, err

        acc_dir = self.get_account_dir(acc_id)
        errors = []

        # 1. 恢复正式备份目录（兼容“尚未移动到 trash”、“在 trash 中待恢复”及“首次恢复后中断再恢复”）
        try:
            if os.path.isdir(acc_dir):
                pass
            elif trash_dir and os.path.isdir(trash_dir):
                os.rename(trash_dir, acc_dir)
            elif had_acc_dir:
                errors.append(f"账号正式目录与回收暂存目录均缺失: {acc_dir}")
        except Exception as de:
            errors.append(f"恢复账号目录失败: {de}")

        # 2. 恢复内存与磁盘索引
        if isinstance(removed_rec, dict):
            self.accounts[acc_id] = dict(removed_rec)
            try:
                self._save_accounts()
            except Exception as ae:
                errors.append(f"恢复磁盘账号索引失败: {ae}")

        # 3. 恢复原 current_account_id（防止误清活跃归属导致后续切换漏保护）
        try:
            self._save_current_account(prev_cid)
        except Exception as ce:
            errors.append(f"恢复原活跃账号归属失败: {ce}")

        if errors:
            err_txt = "；".join(errors)
            self._mark_recovery_required(journal, err_txt)
            return False, err_txt

        try:
            journal["state"] = "ROLLED_BACK"
            self._write_journal(journal)
        except Exception as je:
            err_txt = f"写入删除回退终态失败: {je}"
            self._mark_recovery_required(journal, err_txt)
            return False, err_txt

        if trash_dir and os.path.lexists(trash_dir) and os.path.isdir(acc_dir):
            self._remove_dir_or_junction(trash_dir)
        try:
            self._clear_journal()
        except Exception as ue:
            self._write_debug_log("restore_debug.log", f"deferred delete journal unlink: {ue}")
        return True, "已完整恢复删除前状态"

    def _rollback_live_transaction(self, journal: dict) -> tuple:
        """
        全量回滚未提交的 switch / prepare 事务：
        关键改进（RR07）：核验回滚全部成功后，必须先将 ROLLED_BACK 终态持久化写入 journal，再删除任何恢复材料！
        """
        txid = journal.get("txid", "unknown")
        errors = []

        local_spec = journal.get("local_spec")
        if local_spec and not self._restore_single_live_spec(local_spec, txid):
            errors.append("恢复 LocalAppData\\Battle.net 原目录失败")

        roaming_spec = journal.get("roaming_spec")
        if roaming_spec and not self._restore_single_live_spec(roaming_spec, txid):
            errors.append("恢复 Roaming\\Battle.net 原目录失败")

        pre_reg_state = journal.get("pre_reg_state")
        pre_reg_file = journal.get("pre_reg_file")
        if pre_reg_state == "EXISTS":
            if not pre_reg_file or not self._restore_registry_file(pre_reg_file):
                errors.append("回滚恢复原 Battle.net 注册表失败")
        elif pre_reg_state == "ABSENT":
            if not self._clear_live_registry():
                errors.append("回滚清理新增 Battle.net 注册表失败")

        prev_cid = journal.get("prev_current_account_id")
        try:
            self._save_current_account(prev_cid)
        except Exception as e:
            errors.append(f"回滚 current_account_id 失败: {e}")

        if errors:
            err_summary = "；".join(errors)
            self._mark_recovery_required(journal, err_summary)
            return False, err_summary

        # 先持久记录 ROLLED_BACK 终态，确保随后清理期间就算断电重启也不会二次回放已删的 pre_reg
        try:
            journal["state"] = "ROLLED_BACK"
            self._write_journal(journal)
        except Exception as je:
            err_summary = f"无法持久化写入 ROLLED_BACK 终态记录：{je}"
            self._mark_recovery_required(journal, err_summary)
            return False, err_summary

        self._cleanup_journal_artifacts(journal)
        return True, "已完整回滚到切换前状态"

    def _recover_pending_transactions(self):
        """启动时检测并恢复未决事务（覆盖 snapshot / switch / prepare / delete）"""
        if not os.path.exists(self.journal_file):
            return
        try:
            with open(self.journal_file, "r", encoding="utf-8-sig") as f:
                journal = json.load(f)
            if not isinstance(journal, dict):
                raise ValueError("事务日志内容不是 JSON 对象")
        except Exception as e:
            self.recovery_error = f"无法解析未决事务日志 {self.journal_file}: {e}"
            return

        txid = journal.get("txid", "unknown")
        op = journal.get("op")
        state = journal.get("state")

        # 终态（COMMITTED / ROLLED_BACK）只做幂等收尾清理，绝不重新回放数据（修复 RR07）
        if state in ("COMMITTED", "ROLLED_BACK"):
            self._cleanup_journal_artifacts(journal)
            self._write_debug_log("restore_debug.log", f"finished terminal cleanup ({state}) for tx={txid}")
            return

        # RR04：对涉及本机 live / HKCU 的事务核验创建者 SID 与本机路径归属
        if op in ("switch", "prepare"):
            j_sid = journal.get("owner_sid")
            j_local = journal.get("live_local_root")
            if (j_sid and j_sid != CURRENT_USER_SID) or (
                j_local and os.path.normcase(os.path.abspath(j_local)) != os.path.normcase(os.path.abspath(self.BATTLENET_LOCAL))
            ):
                self._quarantine_foreign_journal_if_needed()
                return

        targets, has_unreadable_agent = self._scan_battlenet_processes()
        if targets or has_unreadable_agent:
            self.recovery_error = "检测到上次操作异常中断，且当前战网或相关 Agent 正在运行。请先完全退出战网后重启本工具完成自动恢复。"
            return

        if op == "snapshot":
            acc_id = journal.get("account_id")
            if not is_safe_account_id(acc_id):
                self._mark_recovery_required(journal, f"未决快照事务包含非法账号 ID: {acc_id}")
                return
            acc_dir = self.get_account_dir(acc_id)
            prev_dir = journal.get("prev_dir")
            stage_dir = journal.get("stage_dir")
            try:
                if prev_dir and os.path.isdir(prev_dir):
                    if os.path.lexists(acc_dir):
                        aside = os.path.join(self.accounts_dir, f".aborted_{acc_id}_{txid}")
                        self._remove_dir_or_junction(aside)
                        os.rename(acc_dir, aside)
                        self._remove_dir_or_junction(aside)
                    os.rename(prev_dir, acc_dir)
                if not os.path.isdir(acc_dir):
                    raise RuntimeError(f"账号目录仍缺失: {acc_dir}")
                journal["state"] = "ROLLED_BACK"
                self._write_journal(journal)
                if stage_dir and os.path.lexists(stage_dir):
                    self._remove_dir_or_junction(stage_dir)
                self._clear_journal()
                self._write_debug_log("restore_debug.log", f"recovered interrupted snapshot tx={txid} for {acc_id}")
            except Exception as e:
                self._mark_recovery_required(journal, f"恢复未完成快照失败（账号 {acc_id}）：{e}")
            return

        if op == "delete":
            ok_del_rb, err_del_rb = self._rollback_uncommitted_delete(journal)
            if ok_del_rb:
                self._write_debug_log("restore_debug.log", f"rolled back uncommitted delete tx={txid}")
            else:
                self.recovery_error = f"恢复未完成删除事务失败：{err_del_rb}"
            return

        if op in ("switch", "prepare"):
            started_ts = float(journal.get("started_epoch") or 0)
            cfg_path = os.path.join(self.BATTLENET_ROAMING, "Battle.net.config")
            if started_ts > 0 and os.path.exists(cfg_path):
                try:
                    if os.path.getmtime(cfg_path) > started_ts + 10:
                        # RR03 (S2T11)：崩溃后本机产生了新会话，必须确认候选留底完整成功后才允许回滚清理新 live！
                        cand_ok, cand_res = self._save_candidate_snapshot(f"post_crash_{txid}")
                        if not cand_ok:
                            self._mark_recovery_required(
                                journal,
                                f"崩溃后本机产生了新战网会话，但保护性候选备份失败（{cand_res}），已停止自动回滚以保留本机最新会话",
                            )
                            return
                except Exception as ce:
                    self._mark_recovery_required(journal, f"检查崩溃后新增会话失败：{ce}")
                    return

            ok, err = self._rollback_live_transaction(journal)
            if not ok:
                self.recovery_error = f"自动回滚未决切换事务失败：{err}"
            else:
                self._write_debug_log("restore_debug.log", f"recovered interrupted {op} tx={txid}")

    def _cleanup_managed_temp_artifacts(self):
        """仅在无任何未决 journal 时，清理已无索引引用的历史遗留临时目录"""
        if os.path.exists(self.journal_file):
            return
        try:
            for item in os.listdir(self.accounts_dir):
                full_path = os.path.join(self.accounts_dir, item)
                if not os.path.isdir(full_path):
                    continue
                if item.startswith("temp_") and item not in self.accounts:
                    self._remove_dir_or_junction(full_path)
                elif item.startswith(".stage_"):
                    self._remove_dir_or_junction(full_path)
                elif item.startswith(".trash_"):
                    # RR08：仅当对应账号确实已不在索引中时才擦除 .trash_<acc_id>_<txid>
                    parts = item[len(".trash_"):].rsplit("_", 1)
                    ref_id = parts[0] if len(parts) == 2 else ""
                    if ref_id and ref_id not in self.accounts:
                        self._remove_dir_or_junction(full_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 战网进程管理：QueryFullProcessImageNameW 辅助识别、全部关联进程停写强复查（修复 RR10）
    # ------------------------------------------------------------------

    @staticmethod
    def _current_username_lower() -> str:
        return (os.environ.get("USERNAME") or "").strip().lower()

    def _scan_battlenet_processes(self) -> tuple:
        """
        扫描当前用户会话关联的 Battle.net 及 Blizzard Agent 进程。
        返回: (target_procs, has_unreadable_agent)
        """
        cur_user = self._current_username_lower()
        targets = []
        has_unreadable_agent = False
        launcher_names = {
            "battle.net.exe",
            "battle.net launcher.exe",
            "battle.net helper.exe",
            "blizzarderror.exe",
        }
        service_accounts = {"system", "local service", "network service"}

        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "username"]):
            try:
                info = proc.info
                name_lower = (info.get("name") or "").lower()
                if not name_lower:
                    continue

                proc_user = (info.get("username") or "").strip().lower()
                proc_short = proc_user.split("\\")[-1] if proc_user else ""
                if proc_short and cur_user and proc_short != cur_user and proc_short not in service_accounts:
                    continue

                if name_lower in launcher_names:
                    targets.append(proc)
                    continue

                if name_lower in ("agent.exe", "agent"):
                    exe = info.get("exe") or ""
                    cmdline = info.get("cmdline") or []
                    if not exe and hasattr(proc, "pid"):
                        exe = _query_limited_process_exe(proc.pid)
                    if not exe and not cmdline:
                        # 非系统服务账号下的同名不可读 Agent 视为潜在会话写入冲突（修复 RR10 D13）
                        if proc_short not in service_accounts:
                            has_unreadable_agent = True
                        continue
                    details = " ".join([str(exe)] + [str(x) for x in cmdline]).lower()
                    if any(k in details for k in ("battle.net", "blizzard", "battlenet")):
                        targets.append(proc)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except Exception:
                pass

        return targets, has_unreadable_agent

    def is_battlenet_running(self) -> bool:
        targets, _ = self._scan_battlenet_processes()
        return len(targets) > 0

    def is_battlenet_client_open(self) -> bool:
        """检查战网主客户端窗口进程是否在运行（用于保存登录前的提示，不把后台残留 Agent 误判为已打开主窗口）"""
        targets, _ = self._scan_battlenet_processes()
        for p in targets:
            try:
                nm = (p.info.get("name") or "").lower()
                if nm in ("battle.net.exe", "battle.net launcher.exe"):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _post_wm_close_to_pids(pids: set):
        if not pids:
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            WM_CLOSE = 0x0010

            def _cb(hwnd, _lparam):
                pid = wt.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True

            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            pass

    def close_battlenet_checked(self) -> tuple:
        """
        停止全部已确认的 Battle.net 与暴雪 Agent 进程，并在所有阶段结束后严格复查（修复 RR10 D12/D13）。
        返回: (all_stopped: bool, message: str, used_force_kill: bool)
        """
        targets, has_unreadable_agent = self._scan_battlenet_processes()
        if not targets and not has_unreadable_agent:
            return True, "战网未在运行", False

        if targets:
            launcher_pids = {
                p.pid for p in targets
                if (p.info.get("name") or "").lower() in ("battle.net.exe", "battle.net launcher.exe")
            }
            self._post_wm_close_to_pids(launcher_pids)

            try:
                _, alive = psutil.wait_procs(targets, timeout=3.0)
            except Exception:
                alive = targets

            used_force = False
            if alive:
                used_force = True
                for proc in alive:
                    try:
                        proc.kill()
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        pass
                    except Exception as e:
                        self._write_debug_log("switch_debug.log", f"kill pid={proc.pid} failed: {e}")
                try:
                    psutil.wait_procs(alive, timeout=5.0)
                except Exception:
                    pass
            time.sleep(0.6)
        else:
            used_force = False

        # 最终复查：包含暴雪 Agent.exe 在内的全部关联进程及不可读同名冲突均不得残留！
        remaining, still_unreadable_agent = self._scan_battlenet_processes()
        if remaining:
            # 再给最后一次停止与等待机会（应对客户端退出瞬间拉起的短期进程）
            used_force = True
            for proc in remaining:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                psutil.wait_procs(remaining, timeout=3.0)
            except Exception:
                pass
            remaining, still_unreadable_agent = self._scan_battlenet_processes()

        if remaining:
            pids_str = ", ".join(f"{p.info.get('name')}({p.pid})" for p in remaining)
            msg = f"无法停止战网关联进程 [{pids_str}]，可能权限不足（请手动退出战网或以管理员权限运行）"
            self._write_debug_log("switch_debug.log", msg)
            return False, msg, used_force

        if still_unreadable_agent:
            msg = "检测到权限受限的同名 Agent.exe 进程无法核实归属与停写状态，为保护会话安全已中止操作"
            self._write_debug_log("switch_debug.log", msg)
            return False, msg, used_force

        return True, "战网已关闭", used_force

    def close_battlenet(self) -> bool:
        targets, has_unreadable = self._scan_battlenet_processes()
        if not targets and not has_unreadable:
            return False
        ok, _msg, _forced = self.close_battlenet_checked()
        return ok

    def start_battlenet(self, region: str = None) -> bool:
        exe_path = self.BATTLENET_EXE
        if not exe_path or not os.path.isfile(exe_path):
            exe_path = find_battlenet_exe()
            self.BATTLENET_EXE = exe_path

        if os.path.isfile(exe_path):
            try:
                args = [exe_path]
                if region:
                    args.append(f"--setregion={region}")
                subprocess.Popen(args)
                return True
            except Exception as e:
                self._write_debug_log("switch_debug.log", f"start_battlenet failed: {exe_path}, error={e}")
        return False

    # ------------------------------------------------------------------
    # 认证域、注销信号与冲突优先的三态身份核验（修复 RR01, RR09）
    # ------------------------------------------------------------------

    def detect_current_version(self) -> str:
        signals = self._inspect_config_auth_signals(self.BATTLENET_ROAMING)
        return signals["domain"] if signals["domain"] in ("cn", "global") else "cn"

    def get_paths_for_version(self, _version: str = "cn") -> tuple:
        return self.BATTLENET_EXE, self.BATTLENET_LOCAL, self.BATTLENET_ROAMING

    @staticmethod
    def _normalize_account_name(value: str) -> str:
        return str(value or "").strip().lower()

    def _inspect_config_auth_signals(self, roaming_path: str) -> dict:
        """
        从 Battle.net.config 提取结构化会话信号：
        - email               : SavedAccountNames 首项
        - auto_login_disabled : Client.AutoLogin 是否显式为 'false'（注销后常见特征）
        - domain              : 根据 Services.LastLoginRegion / LastLoginAddress 识别 'cn' | 'global' | None
        """
        signals = {"email": "", "auto_login_disabled": False, "domain": None}
        config_path = os.path.join(roaming_path, "Battle.net.config")
        if not os.path.exists(config_path):
            return signals
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                return signals

            client = config.get("Client")
            if isinstance(client, dict):
                saved_names = client.get("SavedAccountNames", "")
                if isinstance(saved_names, str) and saved_names:
                    emails = [e.strip() for e in saved_names.split(",") if e.strip()]
                    if emails:
                        signals["email"] = emails[0]
                auto_login = str(client.get("AutoLogin", "")).strip().lower()
                if auto_login == "false":
                    signals["auto_login_disabled"] = True

            for _k, sec in config.items():
                if not isinstance(sec, dict):
                    continue
                services = sec.get("Services")
                if not isinstance(services, dict):
                    continue
                region = str(services.get("LastLoginRegion") or "").strip().upper()
                addr = str(services.get("LastLoginAddress") or "").strip().lower()
                tassadar = str(services.get("LastLoginTassadar") or "").strip().lower()
                if region == "CN" or "battlenet.com.cn" in addr or "battlenet.com.cn" in tassadar:
                    signals["domain"] = "cn"
                    break
                if region in ("KR", "US", "EU", "TW", "SG") or (
                    (".battle.net" in addr or ".battle.net" in tassadar)
                    and "battlenet.com.cn" not in addr
                    and "battlenet.com.cn" not in tassadar
                ):
                    signals["domain"] = "global"
                    break
        except Exception as e:
            self._write_debug_log("config_debug.log", f"inspect Battle.net.config failed ({config_path}): {e}")
        return signals

    def _get_current_email_from_config(self, roaming_path: str):
        email = self._inspect_config_auth_signals(roaming_path)["email"]
        return email or None

    def _set_first_email_in_config(self, roaming_path: str, email: str) -> bool:
        if not email or not str(email).strip():
            return False
        email = str(email).strip()
        config_path = os.path.join(roaming_path, "Battle.net.config")
        if not os.path.exists(config_path):
            return False
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                return False
            client = config.setdefault("Client", {})
            saved_names = client.get("SavedAccountNames", "")
            emails = [e.strip() for e in str(saved_names).split(",") if e.strip()]

            normalized_target = email.lower()
            remaining = []
            seen = {normalized_target}
            for item in emails:
                key = item.lower()
                if key not in seen:
                    remaining.append(item)
                    seen.add(key)
            client["SavedAccountNames"] = ",".join([email] + remaining)

            self._atomic_write_json(config_path, config, encoding="utf-8-sig", indent=4)
            return True
        except Exception as e:
            self._write_debug_log("config_debug.log", f"set first email failed: {e}")
            return False

    @staticmethod
    def _get_account_uid_folders(local_path: str) -> set:
        acc_root = os.path.join(local_path, "Account")
        if not os.path.isdir(acc_root):
            return set()
        try:
            return {
                name for name in os.listdir(acc_root)
                if name.isdigit() and os.path.isdir(os.path.join(acc_root, name))
            }
        except Exception:
            return set()

    @staticmethod
    def _extract_latest_auth_log_signals(local_path: str) -> tuple:
        """
        按时间倒序读取最近最多 5 份日志文件尾部（256KB），修复跨文件注销信号遗漏（FG05）：
        - 若较新的无 BattleTag 日志中已出现注销事件，保留该注销信号传递到后续日志判断；
        - 若同一文件内新 BattleTag 登录事件出现在旧注销事件之后，则以最新登录为准。
        返回: (latest_battletag, has_unredeemed_logout)
        """
        logs_dir = os.path.join(local_path, "Logs")
        if not os.path.isdir(logs_dir):
            return None, False
        seen_newer_logout = False
        try:
            candidates = []
            for fn in os.listdir(logs_dir):
                if fn.endswith(".log"):
                    fp = os.path.join(logs_dir, fn)
                    try:
                        candidates.append((os.path.getmtime(fp), fp))
                    except OSError:
                        pass
            candidates.sort(key=lambda x: x[0], reverse=True)
            for _mt, log_path in candidates[:5]:
                try:
                    size = os.path.getsize(log_path)
                    with open(log_path, "rb") as bf:
                        if size > LOG_TAIL_READ_BYTES:
                            bf.seek(size - LOG_TAIL_READ_BYTES)
                        tail_text = bf.read().decode("utf-8", errors="ignore")
                    btag_matches = list(re.finditer(r"BattleTag[:\s]+([^\s,]+#\d+)", tail_text))
                    logout_matches = list(_LOGOUT_LOG_RE.finditer(tail_text))
                    if btag_matches:
                        last_btag = btag_matches[-1]
                        same_file_logout_after = bool(
                            logout_matches and logout_matches[-1].start() > last_btag.end()
                        )
                        return last_btag.group(1), (seen_newer_logout or same_file_logout_after)
                    if logout_matches:
                        seen_newer_logout = True
                except Exception:
                    pass
        except Exception:
            pass
        return None, seen_newer_logout

    def get_current_logged_account(self) -> dict:
        result = {"account_name": None, "email": None, "battletag": None}
        self._cleanup_invalid_junctions()
        try:
            email = self._get_current_email_from_config(self.BATTLENET_ROAMING)
            if email:
                result["email"] = email
                result["account_name"] = email.split("@")[0] if "@" in email else email
            btag, _logged_out = self._extract_latest_auth_log_signals(self.BATTLENET_LOCAL)
            result["battletag"] = btag
        except Exception as e:
            self._write_debug_log("config_debug.log", f"get_current_logged_account error: {e}")
        return result

    def has_recent_logout_signal(self) -> bool:
        """检查本机最近日志尾部是否存在未解除的显式注销事件（用于手动保存/更新前的风险提示，修复 N02）"""
        try:
            _btag, has_logout = self._extract_latest_auth_log_signals(self.BATTLENET_LOCAL)
            return bool(has_logout)
        except Exception:
            return False

    def _ensure_account_metadata(self, account_id: str) -> tuple:
        info = self.accounts.get(account_id, {})
        changed = False
        roaming_dir = self.get_account_roaming_dir(account_id)
        saved_email = self._get_current_email_from_config(roaming_dir) or ""
        email = info.get("email") or saved_email
        if info.get("email") != email:
            info["email"] = email
            changed = True
        version = info.get("version")
        if version not in ("cn", "global"):
            version = "cn"
            info["version"] = version
            changed = True
        if changed and not self.index_corrupted:
            self.accounts[account_id] = info
            self._save_accounts()
        return version, email

    def _live_has_session_artifacts(self) -> bool:
        if not self._directory_has_data(self.BATTLENET_LOCAL) or not self._directory_has_data(self.BATTLENET_ROAMING):
            return False
        if self._get_registry_state() != "EXISTS":
            return False
        browser_caches = os.path.join(self.BATTLENET_LOCAL, "BrowserCaches")
        account_dir = os.path.join(self.BATTLENET_LOCAL, "Account")
        return self._directory_has_data(browser_caches) or self._directory_has_data(account_dir)

    def _evaluate_live_identity(self, account_id: str) -> tuple:
        """
        冲突优先的三态身份核验（修复 RR01 & RR09）：
        1. 先核查所有已知冲突（认证域 cn/global 冲突、新增不同数字 UID 目录、BattleTag 冲突、邮箱不同） -> MISMATCH；
        2. 再核查注销/未知状态（AutoLogin=false、日志尾部出现注销事件、原无记忆邮箱的空名账号） -> UNKNOWN；
        3. 仅当具备明确匹配的记忆邮箱且无任何域/UID/BattleTag/注销冲突时 -> MATCH。
        """
        if not self._live_has_session_artifacts():
            return "UNKNOWN", "当前本机战网缺少有效的会话缓存或认证注册表"

        info = self.accounts.get(account_id, {})
        nickname = info.get("nickname", account_id)
        target_ver = info.get("version") if info.get("version") in ("cn", "global") else "cn"
        backup_roaming = self.get_account_roaming_dir(account_id)
        backup_local = self.get_account_local_dir(account_id)

        backup_signals = self._inspect_config_auth_signals(backup_roaming)
        live_signals = self._inspect_config_auth_signals(self.BATTLENET_ROAMING)

        backup_cfg_email = backup_signals["email"]
        recorded_raw = (info.get("email") or "").strip() if isinstance(info.get("email"), str) else ""
        is_recorded_btag = bool(recorded_raw and ("#" in recorded_raw and "@" not in recorded_raw))
        expected_email = backup_cfg_email or ("" if is_recorded_btag else recorded_raw)

        raw_btag = info.get("battletag")
        recorded_btag = (str(raw_btag).strip() if isinstance(raw_btag, str) and raw_btag.strip() else "") or (
            recorded_raw if is_recorded_btag else ""
        )

        live_email = live_signals["email"]
        live_domain = live_signals["domain"]
        backup_uids = self._get_account_uid_folders(backup_local)
        live_uids = self._get_account_uid_folders(self.BATTLENET_LOCAL)
        live_btag, has_post_logout = self._extract_latest_auth_log_signals(self.BATTLENET_LOCAL)

        # 1. 已知冲突优先拦截（修复 RR01 I02 同邮箱跨国服/国际服覆盖）
        if live_domain in ("cn", "global") and live_domain != target_ver:
            live_ver_cn = "国际服" if live_domain == "global" else "国服"
            target_ver_cn = "国际服" if target_ver == "global" else "国服"
            return (
                "MISMATCH",
                f"当前本机战网登录区服为【{live_ver_cn}】，与目标备份【{nickname}】（{target_ver_cn}）不一致",
            )

        if backup_uids and live_uids and (live_uids - backup_uids):
            return (
                "MISMATCH",
                f"当前本机战网出现了新的账户 UID 目录 {sorted(live_uids - backup_uids)}，与目标备份【{nickname}】不一致",
            )

        if expected_email and live_email:
            if self._normalize_account_name(expected_email) != self._normalize_account_name(live_email):
                return (
                    "MISMATCH",
                    f"当前战网配置显示账号为【{live_email}】，与目标备份【{nickname}】（{expected_email}）不一致",
                )

        # 若 live 出现了新邮箱而原备份为空名：检查该邮箱是否已属于同区服下的另一个已保存账号
        if not expected_email and live_email:
            norm_live = self._normalize_account_name(live_email)
            for other_id, other_info in self.accounts.items():
                if other_id == account_id or not isinstance(other_info, dict):
                    continue
                if other_info.get("version", "cn") == target_ver:
                    other_email = self._normalize_account_name(other_info.get("email") or "")
                    if other_email and other_email == norm_live:
                        return (
                            "MISMATCH",
                            f"当前战网登录的【{live_email}】属于另一已保存账号【{other_info.get('nickname', other_id)}】",
                        )
            return "UNKNOWN", f"账号【{nickname}】原无记忆邮箱，当前检测到邮箱【{live_email}】，需手动确认更新"

        # FG06：同一账号合法修改 BattleTag 时（域、UID、邮箱均无冲突，仅日志 BattleTag 不同），归为 UNKNOWN 而非硬冲突 MISMATCH：
        # 既保护正式备份不被静默自动改写，又允许用户通过右键【手动更新备份】完成改名后的快照更新！
        if recorded_btag and live_btag:
            if self._normalize_account_name(recorded_btag) != self._normalize_account_name(live_btag):
                return (
                    "UNKNOWN",
                    f"检测到日志最新 BattleTag 为【{live_btag}】（原记录：{recorded_btag}），若已修改战网昵称请右键手动确认更新",
                )

        # 2. 注销或未保存账号名状态拦截（修复 RR01 I01 & FG05 I312）
        if live_signals["auto_login_disabled"] or has_post_logout:
            return "UNKNOWN", f"检测到当前战网处于退出登录或关闭自动登录状态，保留【{nickname}】原备份"

        if not expected_email:
            return "UNKNOWN", f"账号【{nickname}】无记忆账号名标识，跳过自动覆盖并保存候选留底"

        if not live_email:
            return "UNKNOWN", f"当前战网 SavedAccountNames 为空（可能已退出登录），保留【{nickname}】原备份"

        return "MATCH", live_email

    def _save_candidate_snapshot(self, label: str, source_account_id: str = None) -> tuple:
        """
        将未核验的 live 会话完整保存为候选副本，并严格校验每一步结果（修复 RR03 & FG02）：
        - 任一组件失败均返回 (False, error_message)，调用方必须停止破坏性覆盖并保留当前 live；
        - 已提交 COMPLETE.json 的候选记录明确来源（source_key），且不执行无替代确认的自动淘汰，
          确保任何账号（含频繁切换的空名账号与崩溃后会话）唯一最新的本地会话绝不会被其他操作自动擦除。
        """
        safe_label = re.sub(r"[^A-Za-z0-9_-]", "_", str(label or "cand"))[:40]
        space_ok, space_msg = self._check_disk_space(
            [self.BATTLENET_LOCAL, self.BATTLENET_ROAMING],
            DATA_DIR,
        )
        if not space_ok:
            return False, space_msg

        reg_state = self._get_registry_state()
        if reg_state == "ERROR":
            return False, "无法读取当前 HKCU Battle.net 注册表状态"

        txid = uuid.uuid4().hex[:8]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(self.candidates_dir, exist_ok=True)
        tmp_dir = os.path.join(self.candidates_dir, f".tmp_{safe_label}_{txid}")
        final_dir = os.path.join(self.candidates_dir, f"{safe_label}_{ts}_{txid}")
        source_key = f"account:{source_account_id}" if (source_account_id and is_safe_account_id(source_account_id)) else None

        try:
            os.makedirs(tmp_dir, exist_ok=True)
            ok_l, msg_l, _ = self._run_robocopy(self.BATTLENET_LOCAL, os.path.join(tmp_dir, "LocalAppData"), mirror=True)
            if not ok_l:
                raise RuntimeError(f"候选副本 LocalAppData 复制失败：{msg_l}")

            ok_r, msg_r, _ = self._run_robocopy(self.BATTLENET_ROAMING, os.path.join(tmp_dir, "Roaming"), mirror=True)
            if not ok_r:
                raise RuntimeError(f"候选副本 Roaming 复制失败：{msg_r}")

            if reg_state == "EXISTS":
                if not self._export_full_registry_to_file(os.path.join(tmp_dir, "battlenet_full_reg.reg")):
                    raise RuntimeError("候选副本注册表导出失败")

            self._atomic_write_json(
                os.path.join(tmp_dir, "COMPLETE.json"),
                {
                    "label": safe_label,
                    "source_key": source_key,
                    "saved_at": datetime.now().isoformat(),
                    "reg_state": reg_state,
                },
            )
            os.rename(tmp_dir, final_dir)
            self._write_debug_log("snapshot_debug.log", f"candidate snapshot committed: {final_dir}")
            return True, final_dir
        except Exception as e:
            self._write_debug_log("snapshot_debug.log", f"candidate snapshot failed ({safe_label}): {e}")
            if os.path.lexists(tmp_dir):
                self._remove_dir_or_junction(tmp_dir)
            return False, str(e)

    # ------------------------------------------------------------------
    # 整代快照提交（保留上一良好代次 + 双失败锁写门禁：修复 RR01, RR06）
    # ------------------------------------------------------------------

    def _snapshot_live_state_to_account(self, account_id: str, reason: str = "manual") -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            return False, block_msg
        if account_id not in self.accounts:
            return False, "账号不存在，无法刷新当前登录态"

        status, detail = self._evaluate_live_identity(account_id)
        info = self.accounts[account_id]
        nickname = info.get("nickname", account_id)

        if status == "MISMATCH":
            msg = f"{detail}。为避免串号覆盖，已中止操作。"
            self._write_debug_log("snapshot_debug.log", f"identity mismatch ({reason}): {msg}")
            return False, msg

        if status == "UNKNOWN":
            if reason != "manual_update":
                self._write_debug_log("snapshot_debug.log", f"identity unknown ({reason}): {detail}")
                return False, detail
            if not self._live_has_session_artifacts():
                return False, "当前战网未检测到有效登录缓存或认证注册表，请确认已在战网完成登录"
            live_signals = self._inspect_config_auth_signals(self.BATTLENET_ROAMING)
            backup_cfg_email = (self._get_current_email_from_config(self.get_account_roaming_dir(account_id)) or "").strip()
            if backup_cfg_email and not live_signals["email"]:
                return False, f"当前战网未检测到登录账号（SavedAccountNames 为空），拒绝覆盖【{nickname}】"

        space_ok, space_msg = self._check_disk_space(
            [self.BATTLENET_LOCAL, self.BATTLENET_ROAMING],
            self.accounts_dir,
        )
        if not space_ok:
            return False, space_msg

        txid = uuid.uuid4().hex[:10]
        acc_dir = self.get_account_dir(account_id)
        stage_dir = os.path.join(self.accounts_dir, f".stage_{account_id}_{txid}")
        prev_dir = os.path.join(self.accounts_dir, f".prev_{account_id}_{txid}")

        stage_local = os.path.join(stage_dir, "LocalAppData")
        stage_roaming = os.path.join(stage_dir, "Roaming")
        stage_reg = os.path.join(stage_dir, "battlenet_full_reg.reg")

        live_email = (self._get_current_email_from_config(self.BATTLENET_ROAMING) or "").strip()
        live_btag, _ = self._extract_latest_auth_log_signals(self.BATTLENET_LOCAL)
        version = info.get("version") if info.get("version") in ("cn", "global") else "cn"
        recorded_email = (info.get("email") or "").strip()
        recorded_is_btag = bool(recorded_email and ("#" in recorded_email and "@" not in recorded_email))
        current_email = live_email or (live_btag if (recorded_is_btag and live_btag) else recorded_email)
        old_info_copy = dict(info)

        journal = None
        moved_old_to_prev = False

        try:
            os.makedirs(stage_dir, exist_ok=True)
            ok, msg, _ = self._run_robocopy(self.BATTLENET_LOCAL, stage_local, mirror=True)
            if not ok:
                raise RuntimeError(f"刷新 LocalAppData 失败：{msg}")
            ok, msg, _ = self._run_robocopy(self.BATTLENET_ROAMING, stage_roaming, mirror=True)
            if not ok:
                raise RuntimeError(f"刷新 Roaming 失败：{msg}")

            if live_email:
                cfg_path = os.path.join(stage_roaming, "Battle.net.config")
                if os.path.exists(cfg_path) and not self._set_first_email_in_config(stage_roaming, live_email):
                    raise RuntimeError("更新暂存配置中的账号顺序失败")

            if not self._export_full_registry_to_file(stage_reg):
                raise RuntimeError("导出或校验完整 Battle.net 注册表失败")

            if os.path.isdir(acc_dir):
                for extra in os.listdir(acc_dir):
                    if extra not in ("LocalAppData", "Roaming", "battlenet_full_reg.reg"):
                        src_extra = os.path.join(acc_dir, extra)
                        dst_extra = os.path.join(stage_dir, extra)
                        if os.path.isfile(src_extra):
                            shutil.copy2(src_extra, dst_extra)

            journal = {
                "txid": txid,
                "op": "snapshot",
                "state": "SWAPPING",
                "account_id": account_id,
                "stage_dir": stage_dir,
                "prev_dir": prev_dir,
                "started_epoch": time.time(),
            }
            self._write_journal(journal)

            if os.path.lexists(acc_dir):
                os.rename(acc_dir, prev_dir)
                moved_old_to_prev = True

            os.rename(stage_dir, acc_dir)

            now = datetime.now().isoformat()
            self.accounts[account_id].update({
                "version": version,
                "email": current_email,
                "battletag": live_btag or info.get("battletag"),
                "last_login": now,
                "last_snapshot": now,
                "snapshot_reason": reason,
                "logged_in": True,
            })
            self._save_accounts()
            self._save_current_account(account_id)

            journal["state"] = "COMMITTED"
            self._write_journal(journal)
        except Exception as e:
            self.accounts[account_id] = old_info_copy
            self._write_debug_log("snapshot_debug.log", f"snapshot failed: account={account_id}, reason={reason}, error={e}")

            # RR06 (S2T07)：只要旧目录曾被移至 prev_dir，无论 stage->acc 是否成功都必须恢复并验证；恢复失败则强制锁写！
            if moved_old_to_prev:
                try:
                    if os.path.lexists(acc_dir):
                        os.rename(acc_dir, stage_dir)
                    if os.path.lexists(prev_dir):
                        os.rename(prev_dir, acc_dir)
                except Exception as rb_err:
                    self._mark_recovery_required(
                        journal or {"txid": txid, "op": "snapshot", "account_id": account_id, "prev_dir": prev_dir, "stage_dir": stage_dir},
                        f"回滚快照代次失败（账号 {account_id}）：{rb_err}",
                    )
                    return False, f"{e}（且回滚旧代次失败，已锁定写入保护恢复副本：{rb_err}）"

                if not os.path.isdir(acc_dir) or os.path.lexists(prev_dir):
                    self._mark_recovery_required(
                        journal or {"txid": txid, "op": "snapshot", "account_id": account_id, "prev_dir": prev_dir, "stage_dir": stage_dir},
                        f"快照代次回滚不完整（账号 {account_id}）",
                    )
                    return False, f"{e}（回滚未完成，已锁定写入）"

            if journal is not None:
                try:
                    journal["state"] = "ROLLED_BACK"
                    self._write_journal(journal)
                    if os.path.lexists(stage_dir):
                        self._remove_dir_or_junction(stage_dir)
                    self._clear_journal()
                except Exception as je:
                    self._mark_recovery_required(journal, f"写入快照回滚终态失败：{je}")
            elif os.path.lexists(stage_dir):
                self._remove_dir_or_junction(stage_dir)

            return False, str(e)

        # FG03 + N01：COMMITTED 成功落盘后，归档上一良好代次与删除 journal 移出数据回滚 try 块！
        # 即使日志删除或归档遇到临时占用，正式新代次与索引也保持完整可用，绝不再回退移动正式目录。
        self._cleanup_journal_artifacts(journal)
        self._write_debug_log(
            "snapshot_debug.log",
            f"snapshot committed: account={account_id}, reason={reason}, version={version}",
        )
        return True, f"已同步账号【{nickname}】最新免登录状态"

    def _auto_refresh_active_account_before_overwrite(self, target_account_id: str = None) -> tuple:
        if self.sid_mismatch_warning:
            return True, "跨 Windows 用户保护生效，跳过自动回写", None

        active_id = self.current_account_id
        if not active_id or str(active_id).startswith("temp_"):
            return True, "无活跃账号需自动刷新", None
        if target_account_id and active_id == target_account_id:
            return True, "目标即当前活跃账号", None
        if active_id not in self.accounts:
            return True, "原活跃账号已不存在，跳过自动刷新", None
        if not self._live_has_session_artifacts():
            return True, "当前本机战网会话不完整，保留原备份", None

        status, detail = self._evaluate_live_identity(active_id)
        if status == "MISMATCH":
            return False, f"自动刷新当前账号备份中止：{detail}", active_id

        if status == "UNKNOWN":
            # FG01 (I311)：只要被跟踪活跃账号的 UNKNOWN live 即将被覆盖，无条件先完成候选三件套留底，移除原邮箱豁免！
            cand_ok, cand_res = self._save_candidate_snapshot(f"unknown_{active_id}", source_account_id=active_id)
            if not cand_ok:
                return (
                    False,
                    f"当前账号处于未核验状态，尝试保存候选留底副本失败（{cand_res}）。为防丢失本机最新凭证，已中止切换。",
                    active_id,
                )
            self._write_debug_log("snapshot_debug.log", f"auto_before_switch candidate saved ({active_id}): {detail}")
            return True, detail, None

        ok, msg = self._snapshot_live_state_to_account(active_id, reason="auto_before_switch")
        if ok:
            return True, msg, active_id
        return False, f"自动刷新当前账号备份失败：{msg}", active_id

    # ------------------------------------------------------------------
    # 新登录准备、创建、更新、切换与带事务记录的删除（修复 RR08, O04）
    # ------------------------------------------------------------------

    def has_unsaved_temp_login(self) -> tuple:
        if not (self.current_account_id and str(self.current_account_id).startswith("temp_")):
            return False, ""
        info = self.get_current_logged_account()
        ident = info.get("email") or info.get("battletag") or ""
        if ident or self._live_has_session_artifacts():
            return True, ident or "未命名临时登录"
        return False, ""

    def prepare_for_new_login(self) -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            self.last_error = block_msg
            return False, block_msg

        stopped, stop_msg, _ = self.close_battlenet_checked()
        if not stopped:
            self.last_error = stop_msg
            return False, stop_msg

        ok, msg, _ = self._auto_refresh_active_account_before_overwrite(target_account_id=None)
        if not ok:
            self.last_error = msg
            return False, msg

        reg_state = self._get_registry_state()
        if reg_state == "ERROR":
            err = "无法读取当前 HKCU Battle.net 注册表状态，已中止准备新登录"
            self.last_error = err
            return False, err

        txid = uuid.uuid4().hex[:10]
        pre_reg_file = os.path.join(DATA_DIR, f".tx_pre_reg_{txid}.reg")
        if reg_state == "EXISTS":
            if not self._export_full_registry_to_file(pre_reg_file):
                err = "备份当前 Battle.net 注册表失败，为保护现有会话已中止操作"
                self.last_error = err
                return False, err

        tmp_local = f"{self.BATTLENET_LOCAL}.__tmp_{txid}"
        tmp_roaming = f"{self.BATTLENET_ROAMING}.__tmp_{txid}"
        old_local = f"{self.BATTLENET_LOCAL}.__old_{txid}"
        old_roaming = f"{self.BATTLENET_ROAMING}.__old_{txid}"

        local_spec = self._capture_path_spec(self.BATTLENET_LOCAL, old_local)
        roaming_spec = self._capture_path_spec(self.BATTLENET_ROAMING, old_roaming)
        prev_cid = self.current_account_id

        journal = {
            "txid": txid,
            "op": "prepare",
            "state": "SWAPPING",
            "local_spec": local_spec,
            "roaming_spec": roaming_spec,
            "tmp_local": tmp_local,
            "tmp_roaming": tmp_roaming,
            "pre_reg_state": reg_state,
            "pre_reg_file": pre_reg_file if reg_state == "EXISTS" else None,
            "prev_current_account_id": prev_cid,
            "started_epoch": time.time(),
        }

        try:
            os.makedirs(tmp_local, exist_ok=True)
            os.makedirs(tmp_roaming, exist_ok=True)
            self._write_journal(journal)

            self._move_live_aside(local_spec)
            self._move_live_aside(roaming_spec)
            os.rename(tmp_local, self.BATTLENET_LOCAL)
            os.rename(tmp_roaming, self.BATTLENET_ROAMING)

            if not self._clear_live_registry():
                raise RuntimeError("清空当前 Battle.net 认证注册表失败")

            temp_id = "temp_" + uuid.uuid4().hex[:8]
            self._save_current_account(temp_id)

            journal["state"] = "COMMITTED"
            self._write_journal(journal)

            self._cleanup_journal_artifacts(journal)
            self._cleanup_managed_temp_artifacts()
            self.last_error = ""
            return True, temp_id
        except Exception as e:
            rb_ok, rb_msg = self._rollback_live_transaction(journal)
            suffix = "" if rb_ok else f"（且回滚未完全成功，已保留恢复副本：{rb_msg}）"
            err = f"准备新登录环境失败：{e}{suffix}"
            self.last_error = err
            return False, err

    def create_account_from_current(
        self,
        nickname: str,
        force_version: str = None,
        expected_email: str = None,
        expected_battletag: str = None,
    ) -> str:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            self.last_error = block_msg
            return None

        clean_nickname = (nickname or "").strip()
        if not clean_nickname:
            self.last_error = "账号昵称不能为空"
            return None

        stopped, stop_msg, _ = self.close_battlenet_checked()
        if not stopped:
            self.last_error = stop_msg
            return None

        self._cleanup_invalid_junctions()
        if not self._live_has_session_artifacts():
            self.last_error = "未检测到完整的战网登录目录或认证注册表，请先完成登录"
            return None

        version = force_version if force_version in ("cn", "global") else "cn"
        frozen_info = self.get_current_logged_account()
        frozen_cfg_email = (frozen_info.get("email") or "").strip()
        frozen_btag = (frozen_info.get("battletag") or "").strip()

        exp_email_clean = (expected_email or "").strip()
        exp_is_btag = bool(exp_email_clean and ("#" in exp_email_clean and "@" not in exp_email_clean))
        if exp_email_clean and not exp_is_btag:
            if not frozen_cfg_email or self._normalize_account_name(exp_email_clean) != self._normalize_account_name(frozen_cfg_email):
                self.last_error = (
                    f"保存期间检测到战网登录账号已变更（弹窗前：{exp_email_clean}，关闭战网后：{frozen_cfg_email or '空'}），"
                    "已中止保存以防串号"
                )
                return None

        # O04 (I07)：无论冻结后邮箱是否为空，只要弹窗前与冻结后均有明确 BattleTag 且不一致，立即中止！
        exp_btag_clean = (expected_battletag or "").strip()
        if exp_btag_clean and frozen_btag:
            if self._normalize_account_name(exp_btag_clean) != self._normalize_account_name(frozen_btag):
                self.last_error = f"保存期间检测到战网 BattleTag 已变更（{exp_btag_clean} -> {frozen_btag}），已中止保存"
                return None

        final_email = frozen_cfg_email or (frozen_btag if version == "global" else "")
        final_btag = frozen_btag or exp_btag_clean or None

        space_ok, space_msg = self._check_disk_space(
            [self.BATTLENET_LOCAL, self.BATTLENET_ROAMING],
            self.accounts_dir,
        )
        if not space_ok:
            self.last_error = space_msg
            return None

        account_id = uuid.uuid4().hex[:8]
        while account_id in self.accounts:
            account_id = uuid.uuid4().hex[:8]

        acc_dir = self.get_account_dir(account_id)
        local_dir = self.get_account_local_dir(account_id)
        roaming_dir = self.get_account_roaming_dir(account_id)
        index_committed = False

        try:
            os.makedirs(local_dir, exist_ok=True)
            os.makedirs(roaming_dir, exist_ok=True)

            ok, msg, _ = self._run_robocopy(self.BATTLENET_LOCAL, local_dir, mirror=False)
            if not ok:
                raise RuntimeError(msg)
            ok, msg, _ = self._run_robocopy(self.BATTLENET_ROAMING, roaming_dir, mirror=False)
            if not ok:
                raise RuntimeError(msg)

            if frozen_cfg_email:
                self._set_first_email_in_config(roaming_dir, frozen_cfg_email)

            if not self.backup_full_registry(account_id):
                raise RuntimeError("导出或验证 Battle.net 注册表失败，请确认当前战网账号已正常登录")

            now = datetime.now().isoformat()
            self.accounts[account_id] = {
                "nickname": clean_nickname,
                "created_time": now,
                "last_login": now,
                "last_snapshot": now,
                "snapshot_reason": "initial_save",
                "logged_in": True,
                "version": version,
                "email": final_email,
                "battletag": final_btag,
            }
            try:
                self._save_accounts()
                index_committed = True
            except Exception:
                self.accounts.pop(account_id, None)
                raise

            try:
                self._save_current_account(account_id)
            except Exception as state_err:
                self._write_debug_log("save_debug.log", f"state save warning after account commit ({account_id}): {state_err}")

            self._cleanup_managed_temp_artifacts()
            self.last_error = ""
            return account_id
        except Exception as e:
            self.last_error = str(e)
            self._write_debug_log("save_debug.log", f"create_account_from_current failed ({account_id}): {e}")
            if not index_committed and os.path.lexists(acc_dir):
                self._remove_dir_or_junction(acc_dir)
            return None

    def update_account_data(self, account_id: str) -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            self.last_error = block_msg
            return False, block_msg
        if account_id not in self.accounts:
            return False, "目标账号不存在"

        stopped, stop_msg, _ = self.close_battlenet_checked()
        if not stopped:
            self.last_error = stop_msg
            return False, stop_msg

        self._cleanup_invalid_junctions()
        ok, msg = self._snapshot_live_state_to_account(account_id, reason="manual_update")
        self.last_error = "" if ok else msg
        return ok, msg

    def _get_launch_region(self, version: str) -> str:
        if version == "global":
            return self.global_region if self.global_region in ("KR", "US", "EU") else "KR"
        return "CN"

    def switch_to_account(self, account_id: str) -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            return False, block_msg
        if not is_safe_account_id(account_id) or account_id not in self.accounts:
            return False, "账号不存在或标识符非法"

        version, email = self._ensure_account_metadata(account_id)
        _, local_path, roaming_path = self.get_paths_for_version(version)
        local_dir = self.get_account_local_dir(account_id)
        roaming_dir = self.get_account_roaming_dir(account_id)
        reg_file = self.get_account_full_reg_file(account_id)
        nickname = self.accounts[account_id].get("nickname", account_id)
        version_name = "国际服" if version == "global" else "国服"
        region = self._get_launch_region(version)

        if not self._directory_has_data(local_dir):
            return False, "账号 LocalAppData 备份不存在或为空，请重新登录并保存该账号"
        if not self._directory_has_data(roaming_dir):
            return False, "账号 Roaming 备份不存在或为空，请重新登录并保存该账号"
        reg_valid, reg_reason = self._validate_reg_file(reg_file)
        if not reg_valid:
            return False, f"账号注册表备份校验未通过（{reg_reason}），请重新登录并保存该账号"

        stopped, stop_msg, _ = self.close_battlenet_checked()
        if not stopped:
            return False, stop_msg

        # 分支 1：目标即当前活跃账号
        if self.current_account_id == account_id and not self.sid_mismatch_warning:
            if self._live_has_session_artifacts():
                id_status, id_detail = self._evaluate_live_identity(account_id)
                if id_status == "MISMATCH":
                    return (
                        False,
                        f"{id_detail}。若需保留当前本机账号，请先点击【保存当前登录】。",
                    )
                if id_status == "MATCH":
                    ok, snap_msg = self._snapshot_live_state_to_account(account_id, reason="relaunch_active")
                    if not ok:
                        return False, f"同步当前活跃账号最新状态失败，已中止启动以保护本机最新凭证：{snap_msg}"
                    live_email = self._get_current_email_from_config(roaming_path) or ""
                    if live_email:
                        self._set_first_email_in_config(roaming_path, live_email)
                    started = self.start_battlenet(region=region)
                    if started:
                        return True, f"{snap_msg}；已启动{version_name}账号【{nickname}】"
                    return True, f"{snap_msg}；未能自动启动战网，请检查设置中的战网路径"

                # FG01 (I310)：同号处于 UNKNOWN 时：
                # - 若原备份无记忆账号名，或当前 live 仍保留账号名（例如仅关闭了 AutoLogin 或刚改了 BattleTag），直接以现有 live 启动，绝不用旧备份倒灌覆盖；
                # - 仅当原备份有记忆邮箱而当前 live SavedAccountNames 已变空（需从备份恢复免登）时，也必须先完成候选三件套留底再进入恢复！
                backup_cfg_email = (self._get_current_email_from_config(roaming_dir) or "").strip()
                live_cfg_email = (self._get_current_email_from_config(roaming_path) or "").strip()
                if (not backup_cfg_email) or bool(live_cfg_email):
                    started = self.start_battlenet(region=region)
                    if started:
                        return True, f"已直接启动{version_name}账号【{nickname}】（保留本机当前状态与既有验证备份）"
                    return True, f"已就绪{version_name}账号【{nickname}】，未能自动启动战网，请手动启动"

                cand_ok, cand_res = self._save_candidate_snapshot(f"unknown_{account_id}", source_account_id=account_id)
                if not cand_ok:
                    return (
                        False,
                        f"当前账号本机状态未核验，尝试保存候选留底副本失败（{cand_res}）。为保护本机现有数据，已中止恢复旧备份。",
                    )

        # 分支 2：切换到另一账号（或当前账号已明确注销/本地目录缺失，需从备份恢复）
        ok, refresh_msg, refreshed_id = self._auto_refresh_active_account_before_overwrite(target_account_id=account_id)
        if not ok:
            return False, refresh_msg

        space_ok, space_msg = self._check_disk_space([local_dir, roaming_dir], os.path.dirname(local_path))
        if not space_ok:
            return False, space_msg

        reg_state = self._get_registry_state()
        if reg_state == "ERROR":
            return False, "无法读取当前 HKCU Battle.net 注册表状态，已中止切换"

        txid = uuid.uuid4().hex[:10]
        pre_reg_file = os.path.join(DATA_DIR, f".tx_pre_reg_{txid}.reg")
        if reg_state == "EXISTS":
            if not self._export_full_registry_to_file(pre_reg_file):
                return False, "切号前备份当前 Battle.net 注册表失败，已中止切换以保护现有会话"

        tmp_local = f"{local_path}.__tmp_{txid}"
        tmp_roaming = f"{roaming_path}.__tmp_{txid}"
        old_local = f"{local_path}.__old_{txid}"
        old_roaming = f"{roaming_path}.__old_{txid}"

        ok, msg, _ = self._run_robocopy(local_dir, tmp_local, mirror=True)
        if not ok:
            self._remove_dir_or_junction(tmp_local)
            if os.path.exists(pre_reg_file):
                os.remove(pre_reg_file)
            return False, f"预复制账号 LocalAppData 失败：{msg}"

        ok, msg, _ = self._run_robocopy(roaming_dir, tmp_roaming, mirror=True)
        if not ok:
            self._remove_dir_or_junction(tmp_local)
            self._remove_dir_or_junction(tmp_roaming)
            if os.path.exists(pre_reg_file):
                os.remove(pre_reg_file)
            return False, f"预复制账号 Roaming 失败：{msg}"

        backup_saved_email = self._get_current_email_from_config(tmp_roaming) or ""
        target_first_email = backup_saved_email or (
            email if email and not ("#" in email and "@" not in email) else ""
        )
        if target_first_email and not self._set_first_email_in_config(tmp_roaming, target_first_email):
            self._remove_dir_or_junction(tmp_local)
            self._remove_dir_or_junction(tmp_roaming)
            if os.path.exists(pre_reg_file):
                os.remove(pre_reg_file)
            return False, "更新暂存 Battle.net.config 账号顺序失败，请重新保存该账号"

        local_spec = self._capture_path_spec(local_path, old_local)
        roaming_spec = self._capture_path_spec(roaming_path, old_roaming)
        prev_cid = self.current_account_id

        journal = {
            "txid": txid,
            "op": "switch",
            "state": "SWAPPING",
            "target_account_id": account_id,
            "local_spec": local_spec,
            "roaming_spec": roaming_spec,
            "tmp_local": tmp_local,
            "tmp_roaming": tmp_roaming,
            "pre_reg_state": reg_state,
            "pre_reg_file": pre_reg_file if reg_state == "EXISTS" else None,
            "prev_current_account_id": prev_cid,
            "started_epoch": time.time(),
        }

        try:
            self._write_journal(journal)

            self._move_live_aside(local_spec)
            self._move_live_aside(roaming_spec)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            os.makedirs(os.path.dirname(roaming_path), exist_ok=True)
            os.rename(tmp_local, local_path)
            os.rename(tmp_roaming, roaming_path)

            if not self.restore_full_registry(account_id):
                raise RuntimeError("恢复目标账号 Battle.net 认证注册表失败")

            self._save_current_account(account_id)

            journal["state"] = "COMMITTED"
            self._write_journal(journal)
        except Exception as e:
            rb_ok, rb_msg = self._rollback_live_transaction(journal)
            suffix = "" if rb_ok else f"（警告：自动回滚未完全成功，已保留恢复副本：{rb_msg}）"
            self._write_debug_log("switch_debug.log", f"switch_to_account failed ({account_id}): {e} {suffix}")
            return False, f"切换失败：{e}{suffix}"

        self._cleanup_journal_artifacts(journal)

        try:
            self.accounts[account_id]["last_login"] = datetime.now().isoformat()
            self.accounts[account_id]["version"] = version
            self.accounts[account_id]["email"] = email
            self._save_accounts()
        except Exception as meta_err:
            self._write_debug_log("config_debug.log", f"non-critical metadata update warning ({account_id}): {meta_err}")

        started = self.start_battlenet(region=region)
        prefix = f"{refresh_msg}；" if refreshed_id else ""
        if started:
            return True, f"{prefix}已恢复{version_name}账号【{nickname}】本地会话并启动战网"
        return True, f"{prefix}已恢复{version_name}账号【{nickname}】本地会话，但未能自动启动战网，请手动启动"

    def rename_account_checked(self, account_id: str, new_nickname: str) -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            return False, block_msg
        if account_id not in self.accounts:
            return False, "账号不存在"
        clean = (new_nickname or "").strip()
        if not clean:
            return False, "新昵称不能为空"

        old_name = self.accounts[account_id].get("nickname")
        self.accounts[account_id]["nickname"] = clean
        try:
            self._save_accounts()
            return True, clean
        except Exception as e:
            self.accounts[account_id]["nickname"] = old_name
            return False, f"保存重命名失败：{e}"

    def toggle_version_checked(self, account_id: str) -> tuple:
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            return False, block_msg
        if account_id not in self.accounts:
            return False, "账号不存在"

        old_ver = self.accounts[account_id].get("version", "cn")
        new_ver = "global" if old_ver == "cn" else "cn"
        self.accounts[account_id]["version"] = new_ver
        try:
            self._save_accounts()
            return True, new_ver
        except Exception as e:
            self.accounts[account_id]["version"] = old_ver
            return False, f"保存区服标记失败：{e}"

    def delete_account_checked(self, account_id: str) -> tuple:
        """
        带持久事务记录的可恢复删除（修复 RR08 S2T09 / S2T10）：
        1. 先写 delete journal，再将目录重命名至 .trash_<id>_<txid>；
        2. 同步持久化 isolated_accounts.json 与 switcher_state.json；
        3. 若 switcher_state.json 写入失败，连同磁盘 isolated_accounts.json、内存与目录一并恢复（失败则锁写）；
        4. 提交 COMMITTED 终态后再擦除 .trash 目录并移除上一代副本。
        """
        allowed, block_msg = self._check_operation_allowed()
        if not allowed:
            return False, block_msg
        if not is_safe_account_id(account_id):
            return False, f"拒绝删除非法账号标识符: {account_id}"
        if account_id not in self.accounts:
            return False, "账号不存在"

        txid = uuid.uuid4().hex[:8]
        acc_dir = self.get_account_dir(account_id)
        trash_dir = os.path.join(self.accounts_dir, f".trash_{account_id}_{txid}")
        removed_record = dict(self.accounts[account_id])
        prev_cid = self.current_account_id
        had_acc_dir = os.path.lexists(acc_dir)

        journal = {
            "txid": txid,
            "op": "delete",
            "state": "DELETING",
            "account_id": account_id,
            "trash_dir": trash_dir,
            "had_acc_dir": had_acc_dir,
            "removed_record": removed_record,
            "prev_current_account_id": prev_cid,
            "started_epoch": time.time(),
        }

        moved_to_trash = False
        try:
            self._write_journal(journal)
            if had_acc_dir:
                os.rename(acc_dir, trash_dir)
                moved_to_trash = True

            self.accounts.pop(account_id, None)
            self._save_accounts()

            if prev_cid == account_id:
                self._save_current_account(None)

            journal["state"] = "COMMITTED"
            self._write_journal(journal)
        except Exception as e:
            rb_ok, rb_msg = self._rollback_uncommitted_delete(journal)
            if not rb_ok:
                return False, f"删除失败且回退不完整（已锁定写入保护数据）：{e}；{rb_msg}"
            return False, f"删除账号失败，已完整恢复原状态：{e}"

        # 清理关联的 previous_generations/<id> 与 trash
        prev_gen = os.path.join(self.prev_gens_dir, account_id)
        if os.path.lexists(prev_gen):
            self._remove_dir_or_junction(prev_gen)

        trash_cleaned = True
        if moved_to_trash and os.path.lexists(trash_dir):
            trash_cleaned = self._remove_dir_or_junction(trash_dir)

        try:
            self._clear_journal()
        except Exception:
            pass

        if not trash_cleaned:
            return True, "账号已移除，部分只读或占用缓存将在下次启动时自动擦除"
        return True, "账号及本地隔离备份已彻底删除"

    def delete_account(self, account_id: str) -> bool:
        ok, _ = self.delete_account_checked(account_id)
        return ok

    def get_all_accounts(self) -> list:
        result = []
        for account_id, info in self.accounts.items():
            if not is_safe_account_id(account_id) or not isinstance(info, dict):
                continue
            try:
                local_dir = self.get_account_local_dir(account_id)
                roaming_dir = self.get_account_roaming_dir(account_id)
                reg_file = self.get_account_full_reg_file(account_id)
                reg_ok, _ = self._validate_reg_file(reg_file)
                has_data = (
                    self._directory_has_data(local_dir)
                    and self._directory_has_data(roaming_dir)
                    and reg_ok
                )
            except Exception:
                has_data = False

            result.append({
                "id": account_id,
                "nickname": info.get("nickname", "未知"),
                "logged_in": bool(info.get("logged_in", False)),
                "last_login": info.get("last_login", ""),
                "has_data": has_data,
                "is_current": account_id == self.current_account_id,
            })
        return result


if __name__ == "__main__":
    # 命令行只读诊断输出（不抢锁执行自动写恢复，修复 O02）
    print(f"管理员权限: {is_admin()}")
    print(f"检测到的战网路径: {find_battlenet_exe()}")
