"""共享目录访问权限管理。

DSM 7 强制套件降权运行，服务账号是系统内部用户 sc-<包名>，
默认没有任何共享目录的读写权限。本模块负责：

1. 检测当前进程对某目录是否有读写权限
2. 尝试用 synoacltool 为服务账号授权（需要 root 权限时才有效）
3. 权限不足时给出用户可操作的中文提示
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

SERVICE_USER = "sc-qunhuisync"
ACL_TOOL = "synoacltool"

# rwxpd---：读、写、执行、删除；fd--：对文件与目录生效
ACL_ENTRY = "user:%s:rwxpd---:fd--" % SERVICE_USER


def _run_acl_tool(args: List[str]) -> bool:
    """调用 synoacltool，返回是否成功。"""
    if not _has_acl_tool():
        return False
    try:
        result = subprocess.run(
            [ACL_TOOL] + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _has_acl_tool() -> bool:
    for path in ("/usr/syno/sbin/synoacltool", "/usr/sbin/synoacltool",
                 "/usr/local/sbin/synoacltool", "/sbin/synoacltool"):
        if os.path.exists(path):
            return True
    return any(
        os.path.exists(os.path.join(folder, ACL_TOOL))
        for folder in os.environ.get("PATH", "").split(os.pathsep)
        if folder
    )


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def can_write(path: str) -> bool:
    """当前进程能否写入该目录（含新建文件）。"""
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".qhs_write_test")
    try:
        with open(probe, "w") as handle:
            handle.write("")
        os.unlink(probe)
        return True
    except OSError:
        return False


def can_read(path: str) -> bool:
    """当前进程能否列出该目录内容。"""
    if not path or not os.path.isdir(path):
        return False
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def grant(path: str) -> bool:
    """为服务账号授予目录读写权限。

    只有具备 root 权限时才会成功；否则返回 False 由调用方提示用户手动授权。
    """
    if not path or not os.path.isdir(path):
        return False
    if not is_root():
        return False
    return _run_acl_tool(["-enforce", "allow", ACL_ENTRY, path])


def check(path: str) -> Dict[str, object]:
    """返回目录的权限状态，供界面展示。"""
    exists = bool(path) and os.path.isdir(path)
    result = {
        "path": path or "",
        "exists": exists,
        "readable": can_read(path) if exists else False,
        "writable": can_write(path) if exists else False,
        "granted": False,
        "message": "",
    }
    if not exists:
        result["message"] = "目录不存在"
        return result
    if result["readable"] and result["writable"]:
        result["message"] = "权限正常"
        return result
    if grant(path):
        result["granted"] = True
        result["readable"] = can_read(path)
        result["writable"] = can_write(path)
        result["message"] = "已自动授权"
        return result
    result["message"] = ("当前服务账号无权访问该目录。"
                         "请在 DSM「控制面板 → 共享文件夹 → 权限」中，"
                         "把「系统内部用户」%s 设为读写权限。" % SERVICE_USER)
    return result


def check_all(paths: List[str]) -> List[Dict[str, object]]:
    return [check(path) for path in paths if path]
