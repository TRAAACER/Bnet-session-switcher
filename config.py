"""
bnet-session-switcher - 配置、路径、受信任系统命令与单实例锁模块
"""
import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import sys
import uuid

APP_NAME = "bnet-session-switcher"
APP_TITLE = "战网会话切换器"
APP_VERSION = "1.2.2"
PROJECT_REPO = "https://github.com/TRAAACER/Bnet-session-switcher"
UPSTREAM_REPO = "https://github.com/54006/cnbxqhdl"


def get_app_dir() -> str:
    """获取应用根目录（兼容源码运行与 PyInstaller 打包后的 EXE）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_system_exe(exe_name: str) -> str:
    """解析受信任的 Windows System32 系统命令绝对路径，绝不回退到当前工作目录裸命令"""
    sys_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.abspath(os.path.join(sys_root, "System32", exe_name))


def get_current_user_sid() -> str:
    """使用 64 位安全的 Win32 API 获取当前进程所属用户的 SID 字符串"""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wt.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.ConvertSidToStringSidW.restype = wt.BOOL

        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return os.environ.get("USERNAME", "unknown")

        try:
            needed = wt.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                return os.environ.get("USERNAME", "unknown")
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, 1, buf, needed.value, ctypes.byref(needed)):
                return os.environ.get("USERNAME", "unknown")

            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_ptr = ctypes.c_void_p()
            if advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)) and str_ptr.value:
                try:
                    return ctypes.wstring_at(str_ptr)
                finally:
                    kernel32.LocalFree(str_ptr)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        pass
    return os.environ.get("USERNAME", "unknown")


APP_DIR = get_app_dir()
DATA_DIR = os.path.join(APP_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

CURRENT_USER_SID = get_current_user_sid()
REG_EXE = get_system_exe("reg.exe")
ROBOCOPY_EXE = get_system_exe("robocopy.exe")


def ensure_dirs(target_data_dir: str = None, target_logs_dir: str = None) -> tuple:
    """确保必要的数据和日志目录存在且可写。返回 (ok, error_message)"""
    d_dir = target_data_dir or DATA_DIR
    l_dir = target_logs_dir or LOGS_DIR
    try:
        os.makedirs(d_dir, exist_ok=True)
        os.makedirs(l_dir, exist_ok=True)
        probe = os.path.join(d_dir, f".write_probe_{uuid.uuid4().hex[:6]}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True, ""
    except Exception as e:
        return False, f"数据目录不可写（{d_dir}）：{e}\n请将程序移动到当前用户有读写权限的文件夹（不要放在 C:\\Program Files 下）。"


class DataDirectoryLock:
    """针对特定 DATA_DIR 的排他文件锁，防止不同入口或不同用户并发写同一份 data 目录"""

    def __init__(self, data_dir: str):
        self.lock_file = os.path.join(data_dir, ".data_dir.lock")
        self._fh = None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.lock_file)), exist_ok=True)
            fh = open(self.lock_file, "a+b")
            fh.seek(0)
            if os.path.getsize(self.lock_file) == 0:
                fh.write(b"1")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._fh = fh
            return True
        except OSError:
            return False

    def release(self):
        if self._fh is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


class SingleInstanceLock:
    """
    基于当前 Windows 用户 SID + DATA_DIR 的跨目录、跨会话单实例互斥锁。
    防止多开实例同时读写同一用户的 Battle.net 目录、HKCU 注册表或同一份账号索引。
    """

    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED = 5

    def __init__(self, sid: str = None, data_dir: str = None):
        self.sid = sid or CURRENT_USER_SID
        safe_sid = "".join(c if c.isalnum() or c == "-" else "_" for c in self.sid)
        self.mutex_name = f"Global\\BNetSessionSwitcher_Mutex_{safe_sid}"
        self.local_mutex_name = f"Local\\BNetSessionSwitcher_Mutex_{safe_sid}"
        local_appdata = os.environ.get("LOCALAPPDATA") or APP_DIR
        self.lock_file_path = os.path.join(local_appdata, f"bnet-session-switcher-{safe_sid}.lock")
        self._data_lock = DataDirectoryLock(data_dir or DATA_DIR)
        self._mutex_handle = None
        self._lock_fh = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        kernel32.CreateMutexW.restype = wt.HANDLE

        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            handle = kernel32.CreateMutexW(None, False, self.local_mutex_name)
            err = ctypes.get_last_error()

        if err in (self.ERROR_ALREADY_EXISTS, self.ERROR_ACCESS_DENIED):
            if handle:
                kernel32.CloseHandle(handle)
            return False

        try:
            os.makedirs(os.path.dirname(self.lock_file_path), exist_ok=True)
            fh = open(self.lock_file_path, "a+b")
            fh.seek(0)
            if os.path.getsize(self.lock_file_path) == 0:
                fh.write(b"1")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._lock_fh = fh
        except OSError:
            if handle:
                kernel32.CloseHandle(handle)
            return False

        # 若 DATA_DIR 可写，同步锁住 DATA_DIR 防止跨用户共享同一 data 目录同时写入
        if os.path.isdir(os.path.dirname(self._data_lock.lock_file)) or os.access(APP_DIR, os.W_OK):
            if not self._data_lock.acquire():
                try:
                    self._lock_fh.seek(0)
                    msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
                    self._lock_fh.close()
                except Exception:
                    pass
                self._lock_fh = None
                if handle:
                    kernel32.CloseHandle(handle)
                return False

        self._mutex_handle = handle
        self._acquired = True
        return True

    def release(self):
        self._data_lock.release()
        if self._lock_fh is not None:
            try:
                self._lock_fh.seek(0)
                msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            try:
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None

        if self._mutex_handle:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wt.HANDLE]
                kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None

        self._acquired = False
