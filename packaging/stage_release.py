"""
bnet-session-switcher - 纯净发行目录构建与白名单递归校验脚本
确保发行产物完整包含可执行程序、许可证及对应源码，且绝不混入任何本地 data 会话数据。
"""
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import APP_NAME, APP_VERSION

RELEASE_STATIC_FILES = [
    ("dist/bnet-session-switcher.exe", "bnet-session-switcher.exe"),
    ("README.md", "README.md"),
    ("LICENSE", "LICENSE"),
    ("packaging/COPYING.GPLv3", "COPYING.GPLv3"),
    ("packaging/LICENSE.Python.txt", "LICENSE.Python.txt"),
    ("packaging/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
]

CORRESPONDING_SOURCE_FILES = [
    "config.py",
    "isolated_switcher.py",
    "modern_gui.py",
    "build.bat",
    "requirements.txt",
    ".gitignore",
    "README.md",
    "LICENSE",
    "packaging/stage_release.py",
    "packaging/requirements-build.lock.txt",
    "packaging/COPYING.GPLv3",
    "packaging/LICENSE.Python.txt",
    "packaging/THIRD_PARTY_NOTICES.md",
]


def _clear_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def clean_dir_strict(target_dir: Path):
    """严格清空旧目录；若存在被锁文件或残留内容未能删除，立即抛错中止构建"""
    if target_dir.exists() or target_dir.is_symlink():
        shutil.rmtree(target_dir, onerror=_clear_readonly)
    if target_dir.exists() or target_dir.is_symlink():
        raise RuntimeError(f"旧发行目录清理失败，仍存在残留内容: {target_dir}")


def stage_clean_release(root_dir: Path = None, exe_override: Path = None) -> Path:
    root = (root_dir or REPO_ROOT).resolve()

    interp_license = Path(sys.base_prefix) / "LICENSE.txt"
    repo_py_license = root / "packaging" / "LICENSE.Python.txt"
    if not repo_py_license.is_file() and interp_license.is_file():
        repo_py_license.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(interp_license, repo_py_license)

    static_files = list(RELEASE_STATIC_FILES)
    if exe_override is not None:
        static_files[0] = (str(exe_override), "bnet-session-switcher.exe")

    for rel_src, _dst_name in static_files:
        src_path = Path(rel_src) if Path(rel_src).is_absolute() else (root / rel_src)
        if not src_path.is_file():
            raise FileNotFoundError(f"缺少必需的发行输入文件: {src_path}")

    for src_rel in CORRESPONDING_SOURCE_FILES:
        src_path = root / src_rel
        if not src_path.is_file():
            raise FileNotFoundError(f"缺少必需的对应源码/许可文件: {src_path}")

    rel_dir = root / "release"
    clean_dir_strict(rel_dir)
    rel_dir.mkdir(parents=True, exist_ok=False)

    expected_files = set()
    for rel_src, dst_name in static_files:
        src_path = Path(rel_src) if Path(rel_src).is_absolute() else (root / rel_src)
        shutil.copy2(src_path, rel_dir / dst_name)
        expected_files.add(dst_name)

    src_zip_name = "corresponding-source.zip"
    with zipfile.ZipFile(rel_dir / src_zip_name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src_rel in CORRESPONDING_SOURCE_FILES:
            zf.write(root / src_rel, arcname=src_rel)
    expected_files.add(src_zip_name)

    pkgs = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    build_info = (
        f"{APP_NAME} v{APP_VERSION}\n"
        f"Python: {sys.version}\n"
        f"Platform: {platform.platform()}\n\n"
        f"Installed Build Environment Packages:\n{pkgs}"
    )
    (rel_dir / "BUILD_INFO.txt").write_text(build_info, encoding="utf-8")
    expected_files.add("BUILD_INFO.txt")

    sums = []
    for fname in sorted(expected_files):
        digest = hashlib.sha256((rel_dir / fname).read_bytes()).hexdigest()
        sums.append(f"{digest}  {fname}")
    (rel_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    expected_files.add("SHA256SUMS.txt")

    for walk_root, subdirs, files in os.walk(rel_dir):
        if subdirs:
            raise RuntimeError(f"发行目录包含非预期的子目录: {subdirs}")
        if Path(walk_root).resolve() == rel_dir.resolve():
            actual_set = set(files)
            if actual_set != expected_files:
                raise RuntimeError(f"发行目录文件集合不匹配: 实际={sorted(actual_set)}, 预期={sorted(expected_files)}")

    bundle_name = f"{APP_NAME}-v{APP_VERSION}-win64.zip"
    bundle_path = rel_dir / bundle_name
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bzf:
        for fname in sorted(expected_files):
            bzf.write(rel_dir / fname, arcname=fname)

    print(f"Release staged and verified successfully: {rel_dir}")
    print(f"Release archive ready: {bundle_path}")
    return bundle_path


if __name__ == "__main__":
    override = Path(sys.argv[2]).resolve() if len(sys.argv) >= 3 and sys.argv[1] == "--exe-path" else None
    stage_clean_release(exe_override=override)
