"""校验生成的 SPK 包结构是否符合群晖 DSM 7 规范。

依据官方开发者指南与官方工具包实现（pkg_make_spk / pkg_make_package）：
  https://help.synology.com/developer-guide/synology_package/introduction.html

要点：
  - SPK 是**未压缩 tar**（官方 `tar cpf`），内容直接位于归档根目录，无 package/ 前缀
  - 必须包含 package.tgz / INFO / scripts / conf / PACKAGE_ICON.PNG / PACKAGE_ICON_256.PNG
"""
from __future__ import annotations

import hashlib
import json
import struct
import tarfile
from pathlib import Path

REQUIRED_SCRIPTS = ["preinst", "postinst", "preuninst", "postuninst",
                    "preupgrade", "postupgrade", "start-stop-status"]
REQUIRED_INFO_KEYS = ["package", "version", "os_min_ver", "description", "arch", "maintainer"]
REQUIRED_DEPS = ["httpx", "anyio", "certifi", "httpcore", "h11", "idna", "sniffio"]

# DSM 7 要求 os_min_ver 至少为 7.0-40000
MIN_OS_MIN_VER = (7, 0, 40000)

# DSM 7.0+ 的图标尺寸要求
ICON_SIZE = 64
ICON_256_SIZE = 256

# 官方没有预编译加密库的架构，不作为校验失败项
UNSUPPORTED_ARCHS = ["armv7"]

# DSM 架构族名 -> 本项目内部架构名
DSM_ARCH_NAMES = {"x86_64": "x86_64", "aarch64": "armv8", "armv7": "armv7"}


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def parse_info(raw: bytes) -> dict:
    """解析 INFO 的 key=value。"""
    info = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip().strip('"')
    return info


def validate(spk_path: Path, archs=None) -> int:
    """校验 SPK 包结构，返回 0 表示通过。"""
    problems = []

    # --- 必须是未压缩 tar（官方 tar cpf） ---
    with open(spk_path, "rb") as handle:
        magic = handle.read(512)
    if magic[:2] == b"\x1f\x8b":
        problems.append("SPK 是 gzip 压缩包，但官方格式是未压缩 tar（tar cpf）")
    elif len(magic) < 512 or magic[257:262] != b"ustar":
        problems.append("不是有效的 tar 文件（缺少 ustar 头）")

    with tarfile.open(spk_path, "r:") as tar:
        members = {member.name: member for member in tar.getmembers()}
        names = set(members)

        # --- 顶层内容直接位于根目录，无 package/ 前缀 ---
        if any(name.startswith("package/") for name in names):
            problems.append("存在 package/ 前缀，官方 spk 的内容应直接位于归档根目录")
        for required in ("INFO", "package.tgz"):
            if required not in names:
                problems.append("缺少 %s" % required)

        # --- INFO ---
        if "INFO" not in names:
            problems.append("缺少 INFO")
        else:
            raw = tar.extractfile("INFO").read()
            if raw.startswith(b"\xef\xbb\xbf"):
                problems.append("INFO 带有 UTF-8 BOM，群晖无法正确解析")
            info = parse_info(raw)

            for key in REQUIRED_INFO_KEYS:
                if key not in info or not info[key]:
                    problems.append("INFO 缺少必需字段：%s" % key)

            # 版本号必须是 feature-build 格式
            version = info.get("version", "")
            if version:
                if "-" not in version:
                    problems.append("INFO 的 version 缺少 build 号：%s（应形如 1.0.0-0001）" % version)
                else:
                    feature, build = version.rsplit("-", 1)
                    if not feature.replace(".", "").isdigit():
                        problems.append("INFO 的 version feature 部分非数字：%s" % feature)
                    if not build.isdigit():
                        problems.append("INFO 的 version build 部分非数字：%s" % build)

            # os_min_ver 必须 >= 7.0-40000
            os_min = info.get("os_min_ver", "")
            try:
                parts = [int(p) for p in os_min.split("-")[0].split(".")]
                build_no = int(os_min.split("-")[1]) if "-" in os_min else 0
                if (parts[0], parts[1] if len(parts) > 1 else 0, build_no) < MIN_OS_MIN_VER:
                    problems.append("os_min_ver=%s 低于 DSM 7 要求的 7.0-40000" % os_min)
            except Exception:
                problems.append("os_min_ver 格式非法：%s" % os_min)

            # arch 必须有值，且与目标架构一致
            arch_value = info.get("arch", "")
            if arch_value and arch_value != "noarch" and archs:
                for arch in archs:
                    want = DSM_ARCH_NAMES.get(arch, arch)
                    if want not in arch_value.split():
                        problems.append("INFO 的 arch 未声明 %s（DSM 架构名 %s）" % (arch, want))

            # package 名不能含特殊字符
            pkg = info.get("package", "")
            for ch in (":", "/", ">", "<", "|", "="):
                if ch in pkg:
                    problems.append("INFO 的 package 含非法字符：%s" % ch)

        # --- package.tgz ---
        if "package.tgz" not in names:
            problems.append("缺少 package.tgz（应用文件应封装在内层包中）")
        else:
            tgz = tar.extractfile("package.tgz").read()
            if info.get("checksum"):
                actual = hashlib.md5(tgz).hexdigest()
                if info["checksum"] != actual:
                    problems.append("checksum 与 package.tgz 不符：INFO=%s 实际=%s"
                                    % (info["checksum"], actual))
            try:
                import io

                with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as inner:
                    inner_names = {m.name for m in inner.getmembers()}
                for required in ("app/qunhuisync/__init__.py", "app/qunhuisync/acl.py",
                                 "app/qunhuisync/pc189.py", "app/qunhuisync/cloud.py",
                                 "app/qunhuisync.sc", "scripts/grant-access.sh"):
                    if required not in inner_names:
                        problems.append("package.tgz 内缺少 %s" % required)
                for dep in REQUIRED_DEPS:
                    if not any(n.startswith("deps/%s" % dep) for n in inner_names):
                        problems.append("package.tgz 内缺少依赖：%s" % dep)
                if not any(n.startswith("deps/Crypto/") for n in inner_names):
                    problems.append("package.tgz 内缺少加密库 deps/Crypto")
                if any(n.startswith("deps-arch/") for n in inner_names):
                    problems.append("package.tgz 内不应包含 deps-arch（应按架构单独出包）")
            except Exception as exc:
                problems.append("package.tgz 无法解析：%s" % exc)

        # --- scripts ---
        for script in REQUIRED_SCRIPTS:
            path = "scripts/" + script
            if path not in names:
                problems.append("缺少脚本：%s" % script)
                continue
            mode = members[path].mode
            if not mode & 0o111:
                problems.append("脚本不可执行：%s (mode %o)" % (script, mode))

        # --- conf（DSM 7 强制要求 conf/privilege，文件名是单数） ---
        if "conf/privilege" not in names:
            problems.append("缺少 conf/privilege（DSM 7 强制要求）")
        else:
            raw_priv = tar.extractfile("conf/privilege").read()
            if raw_priv.startswith(b"\xef\xbb\xbf"):
                problems.append("privilege 带有 UTF-8 BOM")
            try:
                priv = json.loads(raw_priv.decode("utf-8-sig"))
                if "defaults" not in priv:
                    problems.append("privilege 缺少 defaults 字段")
                elif "run-as" not in priv["defaults"]:
                    problems.append("privilege 的 defaults 缺少 run-as 字段")
                elif priv["defaults"]["run-as"] != "package":
                    # DSM 7 强制降权，run-as 为 root 会被套件中心拒绝安装
                    problems.append("privilege 的 run-as 必须是 package（当前 %s），"
                                    "DSM 7 不允许套件以 root 运行"
                                    % priv["defaults"]["run-as"])
            except Exception as exc:
                problems.append("privilege 不是合法 JSON：%s" % exc)

        # --- conf/resource（资源申请） ---
        if "conf/resource" in names:
            raw_res = tar.extractfile("conf/resource").read()
            if raw_res.startswith(b"\xef\xbb\xbf"):
                problems.append("resource 带有 UTF-8 BOM")
            try:
                json.loads(raw_res.decode("utf-8-sig"))
            except Exception as exc:
                problems.append("resource 不是合法 JSON：%s" % exc)

        # --- 图标（DSM 7 要求 64x64 与 256x256） ---
        for name, expect in (("PACKAGE_ICON.PNG", ICON_SIZE),
                             ("PACKAGE_ICON_256.PNG", ICON_256_SIZE)):
            if name not in names:
                problems.append("缺少 %s" % name)
                continue
            size = png_size(tar.extractfile(name).read())
            if size != (expect, expect):
                problems.append("%s 尺寸应为 %dx%d，实际 %s" % (name, expect, expect, size))

    if problems:
        print("校验未通过：")
        for item in problems:
            print("  - " + item)
        return 1
    print("校验通过：%s" % spk_path)
    return 0


def validate_file(spk_path: Path, archs=None) -> int:
    """兼容入口：允许传入 Path 或 str。"""
    return validate(Path(spk_path), archs)


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "dist"
    if target.is_dir():
        found = sorted(target.glob("*.spk"))
        if not found:
            print("dist 下没有 .spk 文件")
            sys.exit(1)
        sys.exit(max(validate(item) for item in found))
    sys.exit(validate(target))
