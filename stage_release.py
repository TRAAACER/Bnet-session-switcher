"""
bnet-session-switcher - 纯净发行目录构建与白名单递归校验脚本
确保发行产物完整包含 GPLv3/LGPLv3/MIT/BSD/PSF 许可证及对应源码，且绝不混入任何本地 data 会话数据。
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

from config import APP_NAME, APP_VERSION

RELEASE_STATIC_FILES = [
    ("dist/bnet-session-switcher.exe", "bnet-session-switcher.exe"),
    ("README.md", "README.md"),
    ("LICENSE", "LICENSE"),
    ("COPYING.GPLv3", "COPYING.GPLv3"),
    ("LICENSE.Python.txt", "LICENSE.Python.txt"),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
]

CORRESPONDING_SOURCE_FILES = [
    "config.py",
    "isolated_switcher.py",
    "modern_gui.py",
    "stage_release.py",
    "build.bat",
    "requirements.txt",
    "requirements-build.lock.txt",
    ".gitignore",
    "README.md",
    "LICENSE",
    "COPYING.GPLv3",
    "LICENSE.Python.txt",
    "THIRD_PARTY_NOTICES.md",
]


def _clear_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def clean_dir_strict(target_dir: Path):
    """严格清空旧目录；若存在被锁文件或残留内容未能删除，立即抛错中止构建（防止旧 release/data 混入发行包）"""
    if target_dir.exists() or target_dir.is_symlink():
        shutil.rmtree(target_dir, onerror=_clear_readonly)
    if target_dir.exists() or target_dir.is_symlink():
        raise RuntimeError(f"旧发行目录清理失败，仍存在残留内容: {target_dir}")


def stage_clean_release(root_dir: Path = None) -> Path:
    root = (root_dir or Path.cwd()).resolve()

    # 若实际构建解释器自带 LICENSE.txt 且根目录尚未同步，优先校验或同步解释器许可正文
    interp_license = Path(sys.base_prefix) / "LICENSE.txt"
    repo_py_license = root / "LICENSE.Python.txt"
    if not repo_py_license.is_file() and interp_license.is_file():
        shutil.copy2(interp_license, repo_py_license)

    # 1. 严格检查所有必需二进制、许可证和对应源码文件存在（绝不静默跳过缺失文件）
    for rel_src, _dst_name in RELEASE_STATIC_FILES:
        src_path = root / rel_src
        if not src_path.is_file():
            raise FileNotFoundError(f"缺少必需的发行输入文件: {src_path}")

    for src_rel in CORRESPONDING_SOURCE_FILES:
        src_path = root / src_rel
        if not src_path.is_file():
            raise FileNotFoundError(f"缺少必需的对应源码/许可文件: {src_path}")

    rel_dir = root / "release"
    clean_dir_strict(rel_dir)
    rel_dir.mkdir(parents=True, exist_ok=False)

    # 2. 复制白名单静态文件
    expected_files = set()
    for rel_src, dst_name in RELEASE_STATIC_FILES:
        shutil.copy2(root / rel_src, rel_dir / dst_name)
        expected_files.add(dst_name)

    # 3. 打包精确匹配的完整项目对应源码包 corresponding-source.zip
    src_zip_name = "corresponding-source.zip"
    with zipfile.ZipFile(rel_dir / src_zip_name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src_rel in CORRESPONDING_SOURCE_FILES:
            zf.write(root / src_rel, arcname=src_rel)
    expected_files.add(src_zip_name)

    # 4. 生成环境与锁定版本构建元数据 BUILD_INFO.txt
    pkgs = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    build_info = (
        f"{APP_NAME} v{APP_VERSION}\n"
        f"Python: {sys.version}\n"
        f"Platform: {platform.platform()}\n\n"
        f"Installed Build Environment Packages:\n{pkgs}"
    )
    (rel_dir / "BUILD_INFO.txt").write_text(build_info, encoding="utf-8")
    expected_files.add("BUILD_INFO.txt")

    # 5. 生成 SHA256 校验清单
    sums = []
    for fname in sorted(expected_files):
        digest = hashlib.sha256((rel_dir / fname).read_bytes()).hexdigest()
        sums.append(f"{digest}  {fname}")
    (rel_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    expected_files.add("SHA256SUMS.txt")

    # 6. 递归白名单审计：严禁存在任何子目录（如 data/）或未知文件
    for walk_root, subdirs, files in os.walk(rel_dir):
        if subdirs:
            raise RuntimeError(f"发行目录包含非预期的子目录: {subdirs}")
        if Path(walk_root).resolve() == rel_dir.resolve():
            actual_set = set(files)
            if actual_set != expected_files:
                raise RuntimeError(f"发行目录文件集合不匹配: 实际={sorted(actual_set)}, 预期={sorted(expected_files)}")

    # 7. 生成可直接上传 GitHub Release 的最终纯净 ZIP 包
    bundle_name = f"{APP_NAME}-v{APP_VERSION}-win64.zip"
    bundle_path = rel_dir / bundle_name
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bzf:
        for fname in sorted(expected_files):
            bzf.write(rel_dir / fname, arcname=fname)

    print(f"Release staged and verified successfully: {rel_dir}")
    print(f"Release archive ready: {bundle_path}")
    return bundle_path


if __name__ == "__main__":
    stage_clean_release()
