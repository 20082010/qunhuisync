"""构建群晖 SPK 安装包（DSM 7 标准布局）。

流程：校验版本 -> 拉取依赖 -> 组装内层 package.tgz -> 生成图标
      -> 写 INFO -> 打包外层 spk -> 校验产物。

外层 spk 结构（官方规范）：
    package/
    ├── INFO                  必需
    ├── package.tgz           必需，内含应用文件（解压后落在 /var/packages/<pkg>/target/）
    ├── scripts/              必需，生命周期脚本
    ├── conf/                 可选
    ├── PACKAGE_ICON.PNG      必需，64x64
    └── PACKAGE_ICON_256.PNG  必需，256x256

官方要求「不要把不同平台的二进制打进同一个 spk」，因此默认按架构各出一个包：
    qunhuisync-x86_64-1.0.0-0001.spk
    qunhuisync-armv8-1.0.0-0001.spk

用法：
    python build/build_spk.py                     # 完整构建
    python build/build_spk.py --offline           # 用 build/wheels 缓存，不联网
    python build/build_spk.py --arch x86_64       # 只打包指定架构
    python build/build_spk.py --set-version 1.1.0 # 改版本号并重建
    python build/build_spk.py --clean             # 清理中间产物
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_icon import create_icons  # noqa: E402
from validate_spk import validate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
STAGE = BUILD_DIR / "package"          # 外层 spk 暂存目录
INNER = BUILD_DIR / "inner"            # 内层 package.tgz 内容目录
WHEEL_CACHE = BUILD_DIR / "wheels"
DIST_DIR = ROOT / "dist"

PKG_NAME = "qunhuisync"
INFO_FILE = ROOT / "spk" / "INFO"
INIT_FILE = ROOT / "src" / "qunhuisync" / "__init__.py"

# DSM 7 最低要求版本（官方：os_min_ver 至少 7.0-40000，否则无法正确安装）
OS_MIN_VER = "7.0-40000"

# 目标 DSM 版本，写进文件名（官方 pkg_get_spk_family_name 格式：
# <包名>_<架构>-<DSM版本>_<版本>-<build>.spk）
TC_VERSION = "7.2"

# 纯 Python 依赖：所有架构通用
PURE_DEPS = [
    "httpx==0.28.1",
    "httpcore==1.0.9",
    "h11==0.16.0",
    "anyio==4.2.0",
    "idna==3.7",
    "sniffio==1.3.1",
    "certifi==2024.8.30",
]

# 含 C 扩展的依赖：必须按架构分别准备
# pycryptodome 官方对 Linux 只发布 x86_64 / aarch64 / i686 轮子，
# armv7 没有任何预编译包，只能在 NAS 上用 gcc 从源码编译（DSM 默认没有）。
ARCH_DEPS = {
    "x86_64": ("manylinux2014_x86_64", "pycryptodome==3.21.0"),
    "aarch64": ("manylinux2014_aarch64", "pycryptodome==3.21.0"),
}

# DSM 架构族名：aarch64 在 DSM 里叫 armv8（官方附录 A）
DSM_ARCH_NAMES = {
    "x86_64": "x86_64",
    "aarch64": "armv8",
    "armv7": "armv7",
}

# 官方没有轮子的架构，不作为校验失败项
UNSUPPORTED_ARCHS = {
    "armv7": "pycryptodome 没有官方 armv7 轮子，armv7 机型需自行编译或改用 x86_64/armv8 机型",
}

# 默认打包的架构
DEFAULT_ARCHS = ["x86_64", "aarch64"]

# 打包后必须能在包内 import 成功的模块
REQUIRED_MODULES = ["httpx", "httpcore", "h11", "anyio", "idna", "sniffio", "certifi"]


class BuildError(Exception):
    """构建过程中的致命错误。"""


def log(message: str) -> None:
    print("[build] " + message)


# ---------------------------------------------------------------- 版本号

def read_info() -> dict:
    """解析 INFO 文件的 key=value。"""
    info = {}
    for line in INFO_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip().strip('"')
    return info


def write_version(version: str) -> None:
    """把版本号同步到 INFO 和 __init__.py。

    version 支持 "1.2.3" 或 "1.2.3-0007" 两种写法，后者带 build 号。

    只给 feature（如 "1.2.0"）时，build 号自动递增——DSM 官方文档明确要求
    「Build number should be increased on every version」，同一 build 号的包
    在套件中心无法作为升级包安装。
    """
    current = read_info().get("version", "")
    current_build = current.split("-")[1] if "-" in current else ""

    if "-" in version:
        feature, build = version.split("-", 1)
    else:
        feature = version
        if current_build.isdigit():
            build = "%04d" % (int(current_build) + 1)
        else:
            build = "0001"

    text = INFO_FILE.read_text(encoding="utf-8-sig")
    new_text, count = re.subn(r'^version=".*"$', 'version="%s-%s"' % (feature, build),
                              text, count=1, flags=re.M)
    if not count:
        raise BuildError("INFO 里没有找到 version 字段")
    INFO_FILE.write_text(new_text, encoding="utf-8")

    init_text = INIT_FILE.read_text(encoding="utf-8-sig")
    new_init, count = re.subn(r'^__version__\s*=\s*".*"$', '__version__ = "%s"' % feature,
                              init_text, count=1, flags=re.M)
    if not count:
        raise BuildError("__init__.py 里没有找到 __version__")
    INIT_FILE.write_text(new_init, encoding="utf-8")
    log("版本号已同步为 %s-%s（原 %s）" % (feature, build, current or "无"))


def check_no_bom() -> None:
    """检查参与打包的文本文件没有 UTF-8 BOM。

    群晖解析带 BOM 的 INFO 会失败，脚本文件带 BOM 在部分 shell 下也会报错。
    """
    targets = [INFO_FILE, ROOT / "spk" / "conf" / "privilege"]
    resource = ROOT / "spk" / "conf" / "resource"
    if resource.exists():
        targets.append(resource)
    targets += sorted((ROOT / "spk" / "scripts").iterdir())
    targets += sorted((ROOT / "src" / "qunhuisync").rglob("*.py"))
    bad = []
    for path in targets:
        if path.is_file() and path.read_bytes().startswith(b"\xef\xbb\xbf"):
            bad.append(path.relative_to(ROOT).as_posix())
    if bad:
        raise BuildError("以下文件带有 UTF-8 BOM，会导致群晖解析异常：\n  %s"
                         % "\n  ".join(bad))


def check_version_consistent() -> str:
    """确保 INFO 与 __init__.py 的 feature 版本一致，且格式合规。"""
    info = read_info()
    version = info.get("version", "")
    if not re.match(r"^\d+\.\d+(\.\d+)?-\d+$", version):
        raise BuildError(
            "INFO 的 version 格式非法：%s。DSM 要求形如 1.0.0-0001（feature-build）" % version)

    feature = version.split("-")[0]
    init_text = INIT_FILE.read_text(encoding="utf-8-sig")
    match = re.search(r'^__version__\s*=\s*"(.*)"$', init_text, flags=re.M)
    init_version = match.group(1) if match else ""
    if feature != init_version:
        raise BuildError(
            "版本号不一致：INFO=%s，__init__.py=%s。请用 --set-version %s 同步。"
            % (feature, init_version, feature))
    return version


# ---------------------------------------------------------------- 依赖

def run_pip(args: list) -> tuple:
    command = [sys.executable, "-m", "pip"] + args
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode, (result.stderr or "").strip()


def download_pure_deps() -> None:
    """下载纯 Python 依赖。失败即终止，避免打出残缺的包。"""
    target = WHEEL_CACHE / "pure"
    target.mkdir(parents=True, exist_ok=True)
    log("下载纯 Python 依赖：%s" % ", ".join(PURE_DEPS))
    code, err = run_pip([
        "download", "--only-binary=:all:", "--no-deps",
        "--dest", str(target),
    ] + PURE_DEPS)
    if code != 0:
        raise BuildError("纯 Python 依赖下载失败：\n%s" % (err or "未知错误"))
    missing = [dep for dep in PURE_DEPS if not list(target.glob("%s-*.whl" % dep.split("==")[0]))]
    if missing:
        raise BuildError("以下依赖没有下载到缓存：%s" % ", ".join(missing))


def download_arch_deps(archs: list) -> None:
    """下载指定架构的加密库。单个架构失败不阻断（安装时会回退 pip）。"""
    for arch in archs:
        if arch not in ARCH_DEPS:
            log("跳过 %s：%s" % (arch, UNSUPPORTED_ARCHS.get(arch, "该架构不支持")))
            continue
        platform, spec = ARCH_DEPS[arch]
        target = WHEEL_CACHE / arch
        target.mkdir(parents=True, exist_ok=True)
        log("下载 %s 架构依赖：%s" % (arch, spec))
        code, err = run_pip([
            "download", "--only-binary=:all:", "--no-deps",
            "--platform", platform,
            "--python-version", "38",
            "--implementation", "cp",
            "--abi", "abi3",
            "--dest", str(target),
            spec,
        ])
        if code != 0:
            log("警告：%s 架构依赖下载失败（%s），安装时将回退 pip 安装"
                % (arch, err.splitlines()[-1] if err else "未知错误"))
        else:
            log("%s 架构依赖就绪" % arch)


def unpack_wheels(wheel_dir: Path, target_dir: Path) -> int:
    """解压 wheel 到目标目录，返回解压数量。"""
    if not wheel_dir.exists():
        return 0
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for wheel in sorted(wheel_dir.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(target_dir)
        count += 1
    return count


def verify_deps() -> None:
    """打包前检查依赖是否齐全、位置是否正确。"""
    deps_dir = INNER / "deps"
    missing = []
    for module in REQUIRED_MODULES:
        if not (deps_dir / module).exists():
            missing.append(module)
    if missing:
        raise BuildError("包内缺少依赖：%s" % ", ".join(missing))

    # 加密库必须在依赖目录里（Python 直接 import Crypto）
    if not (deps_dir / "Crypto").exists():
        raise BuildError("依赖目录缺少加密库 Crypto（该架构的 pycryptodome 未就绪）")

    # 通用依赖里不能混入其它架构的原生库
    for arch_dir in (INNER / "deps-arch").glob("*") if (INNER / "deps-arch").exists() else []:
        raise BuildError("内层不应包含 deps-arch（当前为按架构单独出包）：%s" % arch_dir.name)


# ---------------------------------------------------------------- 组装

def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def stage_inner(arch: str) -> None:
    """组装内层 package.tgz 的内容。

    DSM 会把 package.tgz 解压到 /var/packages/<pkg>/target/，
    因此这里不能带 target/ 前缀，直接放 app/ 与 deps/。
    """
    if INNER.exists():
        shutil.rmtree(INNER)
    INNER.mkdir(parents=True)

    # 应用代码 -> target/app/qunhuisync
    copy_tree(ROOT / "src" / "qunhuisync", INNER / "app" / "qunhuisync")

    # DSM 端口协议文件 -> target/app/<pkg>.sc（conf/resource 里引用）
    sc_file = ROOT / "spk" / "app" / ("%s.sc" % PKG_NAME)
    if sc_file.exists():
        shutil.copy2(sc_file, INNER / "app" / sc_file.name)
        log("端口协议文件已就位：app/%s" % sc_file.name)

    # 授权脚本 -> target/scripts/（spk 的 scripts/ 不随包安装，需单独放一份）
    grant_src = ROOT / "spk" / "scripts" / "grant-access.sh"
    if grant_src.exists():
        (INNER / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(grant_src, INNER / "scripts" / "grant-access.sh")
        (INNER / "scripts" / "grant-access.sh").chmod(0o755)
        log("授权脚本已就位：scripts/grant-access.sh")

    # 纯 Python 依赖 -> target/deps
    deps_dir = INNER / "deps"
    count = unpack_wheels(WHEEL_CACHE / "pure", deps_dir)
    log("解压 %d 个纯 Python 依赖包" % count)

    # 架构相关加密库 -> 并入 target/deps（Python 直接 import Crypto）
    if arch in ARCH_DEPS:
        arch_dir = INNER / "deps-arch" / arch
        count = unpack_wheels(WHEEL_CACHE / arch, arch_dir)
        log("解压 %s 架构加密库 %d 个" % (arch, count))
        for item in arch_dir.iterdir():
            target = deps_dir / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(arch_dir, ignore_errors=True)
        shutil.rmtree(INNER / "deps-arch", ignore_errors=True)
    else:
        raise BuildError("架构 %s 没有可用的加密库，无法生成独立安装包" % arch)

    verify_deps()


def make_package_tgz() -> str:
    """把内层目录打成 package.tgz，返回 md5。"""
    target = STAGE / "package.tgz"
    entries = sorted(INNER.rglob("*"), key=lambda p: p.relative_to(INNER).as_posix())
    raw = target.open("wb")
    try:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for path in entries:
                    rel = path.relative_to(INNER).as_posix()
                    if path.is_dir():
                        info = tarfile.TarInfo(rel + "/")
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        tar.addfile(info)
                    else:
                        data = path.read_bytes()
                        info = tarfile.TarInfo(rel)
                        info.size = len(data)
                        info.mode = 0o755 if rel.startswith("bin/") else 0o644
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        tar.addfile(info, io.BytesIO(data))
    finally:
        raw.close()
    return hashlib.md5(target.read_bytes()).hexdigest()


def write_info(arch: str, checksum: str) -> None:
    """生成 INFO（官方字段规范）。"""
    info = read_info()
    version = info.get("version", "0.0.0-0001")
    dsm_arch = DSM_ARCH_NAMES.get(arch, arch)

    lines = [
        'package="%s"' % PKG_NAME,
        'version="%s"' % version,
        'displayname="%s"' % info.get("displayname", "群晖电信同步工具"),
        'description="%s"' % info.get("description", "Sync local folders to Cloud189"),
    ]
    # 多语言描述：DSM 只认 chs / cht / enu 这类语言代码
    if info.get("description_chs"):
        lines.append('description_chs="%s"' % info["description_chs"])
    if info.get("description_cht"):
        lines.append('description_cht="%s"' % info["description_cht"])
    lines += [
        'arch="%s"' % dsm_arch,
        'maintainer="%s"' % info.get("maintainer", "qunhuisync"),
        'maintainer_url="%s"' % info.get("maintainer_url", ""),
        'distributor="%s"' % info.get("distributor", "qunhuisync"),
        'distributor_url="%s"' % info.get("distributor_url", ""),
        'os_min_ver="%s"' % OS_MIN_VER,
        'install_type="%s"' % info.get("install_type", "system"),
        'silent_install="no"',
        'silent_upgrade="no"',
        'thirdparty="yes"',
        'checksum="%s"' % checksum,
    ]
    if info.get("support_url"):
        lines.append('support_url="%s"' % info["support_url"])

    (STAGE / "INFO").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("INFO 已生成（arch=%s, os_min_ver=%s, version=%s）" % (dsm_arch, OS_MIN_VER, version))


def stage_outer() -> None:
    """组装外层 spk 目录。"""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # 生命周期脚本
    scripts_src = ROOT / "spk" / "scripts"
    scripts_dst = STAGE / "scripts"
    scripts_dst.mkdir()
    for script in sorted(scripts_src.iterdir()):
        if script.is_file():
            shutil.copy2(script, scripts_dst / script.name)

    # conf（DSM 7 强制要求 conf/privilege，文件名是单数）
    (STAGE / "conf").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "spk" / "conf" / "privilege", STAGE / "conf" / "privilege")
    resource = ROOT / "spk" / "conf" / "resource"
    if resource.exists():
        shutil.copy2(resource, STAGE / "conf" / "resource")

    # 图标
    create_icons(str(STAGE))
    log("图标已生成（64x64 + 256x256）")


# ---------------------------------------------------------------- 打包

def make_tar(archive_path: Path) -> str:
    """打包为**未压缩** tar，返回 sha256。

    官方定义：「The Synology package is a SPK file in tar format」，
    群晖工具包用 `tar cpf`（不带 z）生成，DSM 期望的就是未压缩 tar。

    内容直接位于归档根目录（package.tgz / INFO / scripts / conf / 图标），
    **没有 package/ 前缀**——这一点与官方 pkg_make_spk 一致。

    固定 mtime/uid/gid 与顺序，保证相同输入产出字节一致的文件（可复现构建）。
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    entries = sorted(STAGE.rglob("*"), key=lambda p: p.relative_to(STAGE).as_posix())
    with tarfile.open(archive_path, "w", format=tarfile.GNU_FORMAT) as tar:
        for path in entries:
            rel = path.relative_to(STAGE).as_posix()
            if path.is_dir():
                info = tarfile.TarInfo(rel + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                tar.addfile(info)
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if rel.startswith("scripts/") else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (archive_path.with_suffix(archive_path.suffix + ".sha256")).write_text(
        "%s  %s\n" % (digest, archive_path.name), encoding="utf-8")
    return digest


# ---------------------------------------------------------------- 清理

def clean() -> None:
    for target in (STAGE, INNER, WHEEL_CACHE, BUILD_DIR / "__pycache__"):
        if target.exists():
            shutil.rmtree(target)
            log("已清理 %s" % target.relative_to(ROOT))
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


# ---------------------------------------------------------------- 主流程

def build_one(arch: str, version: str) -> Path:
    """为一个架构生成 spk。"""
    dsm_arch = DSM_ARCH_NAMES.get(arch, arch)
    feature, build = version.split("-", 1)

    stage_inner(arch)
    stage_outer()
    checksum = make_package_tgz()
    log("package.tgz 已生成（md5 %s）" % checksum)
    write_info(arch, checksum)

    archive = DIST_DIR / ("%s_%s-%s_%s-%s.spk" % (PKG_NAME, dsm_arch, TC_VERSION, feature, build))
    digest = make_tar(archive)
    log("已生成 %s（%.1f MB）" % (archive.name, archive.stat().st_size / 1024 / 1024))
    log("sha256 %s" % digest)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="构建群晖 SPK 安装包")
    parser.add_argument("--offline", action="store_true", help="不联网，使用 build/wheels 缓存")
    parser.add_argument("--arch", action="append",
                        choices=sorted(list(ARCH_DEPS) + list(UNSUPPORTED_ARCHS)),
                        help="只打包指定架构，可重复；默认 %s" % ", ".join(DEFAULT_ARCHS))
    parser.add_argument("--set-version", metavar="X.Y.Z[-NNNN]",
                        help="先同步版本号再构建，如 1.1.0 或 1.1.0-0007")
    parser.add_argument("--clean", action="store_true", help="清理中间产物后退出")
    parser.add_argument("--no-validate", action="store_true", help="跳过构建后的结构校验")
    parser.add_argument("--keep-stage", action="store_true", help="保留 build/package 暂存目录")
    args = parser.parse_args()

    if args.clean:
        clean()
        return 0

    try:
        if args.set_version:
            write_version(args.set_version)
        check_no_bom()
        version = check_version_consistent()
        log("版本 %s" % version)

        archs = args.arch or list(DEFAULT_ARCHS)
        log("目标架构：%s" % ", ".join("%s(DSM %s)" % (a, DSM_ARCH_NAMES.get(a, a)) for a in archs))
        for arch in archs:
            if arch in UNSUPPORTED_ARCHS:
                raise BuildError("架构 %s 无法出包：%s" % (arch, UNSUPPORTED_ARCHS[arch]))

        if args.offline:
            log("离线模式：使用 build/wheels 缓存")
            if not (WHEEL_CACHE / "pure").exists():
                raise BuildError("离线模式下没有依赖缓存，请先完整构建一次")
        else:
            download_pure_deps()
            download_arch_deps(archs)

        archives = []
        for arch in archs:
            log("—— 构建 %s ——" % DSM_ARCH_NAMES.get(arch, arch))
            archives.append(build_one(arch, version))

        if not args.no_validate:
            for archive in archives:
                if validate(archive, [archs[archives.index(archive)]]) != 0:
                    raise BuildError("包结构校验未通过：%s" % archive.name)
            log("包结构校验全部通过")

        if not args.keep_stage:
            shutil.rmtree(STAGE, ignore_errors=True)
            shutil.rmtree(INNER, ignore_errors=True)
        log("构建完成，共 %d 个包" % len(archives))
        return 0
    except BuildError as exc:
        print("[build] 失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
