"""命令行入口：python3 -m qunhuisync serve|sync|check"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .cloud import CloudError, CloudService
from .config import Config
from .syncer import SyncEngine, SyncJob

DEFAULT_CONFIG = "/var/packages/qunhuisync/var/config/config.json"

COMMON_OPTIONS = ("--config", "--log-file", "--log-level")


def extract_common(argv):
    """把通用参数从任意位置抽出来，剩余部分交给子命令解析。"""
    values = {"--config": None, "--log-file": None, "--log-level": None}
    rest = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in COMMON_OPTIONS and index + 1 < len(argv):
            values[arg] = argv[index + 1]
            index += 2
            continue
        matched = False
        for name in COMMON_OPTIONS:
            if arg.startswith(name + "="):
                values[name] = arg.split("=", 1)[1]
                matched = True
                break
        index += 1
        if not matched:
            rest.append(arg)
    return rest, values


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def cmd_serve(args) -> int:
    from .server import serve

    config = Config(args.config)
    host = args.host or config.server.get("host") or "0.0.0.0"
    port = int(args.port or config.server.get("port") or 8686)
    serve(args.config, host, port)
    return 0


def cmd_sync(args) -> int:
    config = Config(args.config)
    cloud = CloudService(config.cloud, config.token_path())
    try:
        cloud.connect()
    except CloudError as exc:
        print("登录天翼网盘失败：%s" % exc, file=sys.stderr)
        return 2

    engine = SyncEngine(config, cloud)
    rules = config.rules()
    if args.rule and args.rule != "all":
        rules = [rule for rule in rules if rule.id == args.rule or rule.name == args.rule]
    if not rules:
        print("没有匹配的同步规则", file=sys.stderr)
        return 1

    failed = 0
    for rule in rules:
        job = SyncJob(id="cli", rule_id=rule.id, rule_name=rule.name or rule.local_path)
        engine.run(rule, job)
        print("[%s] %s：%s" % (rule.name or rule.local_path, job.status, job.message))
        for line in job.logs:
            print("  " + line)
        if job.status in ("failed", "partial"):
            failed = 1
    return failed


def cmd_check(args) -> int:
    config = Config(args.config)
    print("版本：%s" % __version__)
    print("配置：%s" % args.config)
    print("账号：%s" % (config.cloud.get("username") or "未配置"))
    print("规则数：%d" % len(config.rules()))
    try:
        import httpx  # noqa: F401
        from Crypto.Cipher import PKCS1_v1_5  # noqa: F401
        print("依赖：httpx、pycryptodome 已安装")
    except ImportError:
        print("依赖：缺少 httpx 或 pycryptodome", file=sys.stderr)
        return 2
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rest, common = extract_common(argv)

    parser = argparse.ArgumentParser(prog="qunhuisync", description="群晖电信同步工具：本地文件夹 -> 天翼网盘")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="启动 Web 服务")
    serve_parser.add_argument("--host", default="")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.set_defaults(func=cmd_serve)

    sync_parser = sub.add_parser("sync", help="命令行执行一次同步")
    sync_parser.add_argument("--rule", default="all", help="规则 id 或名称，all 表示全部")
    sync_parser.set_defaults(func=cmd_sync)

    check_parser = sub.add_parser("check", help="检查配置与依赖")
    check_parser.set_defaults(func=cmd_check)

    if not rest:
        rest = ["serve"]
    args = parser.parse_args(rest)

    args.config = common["--config"] or os.environ.get("QHS_CONFIG") or DEFAULT_CONFIG
    args.log_file = common["--log-file"] or os.environ.get("QHS_LOG_FILE") or ""
    args.log_level = common["--log-level"] or os.environ.get("QHS_LOG_LEVEL") or "INFO"
    setup_logging(args.log_level, args.log_file)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
