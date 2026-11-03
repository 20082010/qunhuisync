"""Web 服务：规则管理、同步触发、状态与日志查询（仅用标准库实现）。"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import posixpath
import re
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import acl
from .cloud import CloudError, CloudService
from .config import Config
from .scheduler import Scheduler
from .syncer import SyncEngine, SyncJob

log = logging.getLogger("qunhuisync.server")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class AppState:
    """进程内共享状态。"""

    def __init__(self, config_path: str):
        self.config = Config(config_path)
        self.cloud = CloudService(self.config.cloud, self.config.token_path())
        self.engine = SyncEngine(self.config, self.cloud)
        self.jobs: "OrderedDict[str, SyncJob]" = OrderedDict()
        self._lock = threading.Lock()
        self._disk_cache: Optional[Dict[str, Any]] = None
        self.scheduler = Scheduler(self.config, self.trigger)
        # 待完成的二维码登录
        self.pending_qr: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ---------- 任务 ----------
    def add_job(self, job: SyncJob) -> None:
        with self._lock:
            self.jobs[job.id] = job
            while len(self.jobs) > 50:
                self.jobs.popitem(last=False)

    def list_jobs(self) -> List[SyncJob]:
        with self._lock:
            return list(self.jobs.values())

    def trigger(self, rule_id: str) -> Optional[SyncJob]:
        """在后台线程里执行一条规则。"""
        rule = self.config.get_rule(rule_id)
        if rule is None:
            return None
        if self.engine.is_running(rule_id):
            return None
        job = SyncJob(id=uuid.uuid4().hex[:12], rule_id=rule_id, rule_name=rule.name or rule.local_path)
        # 先置为 running，否则在 worker 线程启动前的这一小段时间里，
        # 界面按 status == "running" 过滤会看不到这个任务
        job.status = "running"
        self.add_job(job)

        def worker():
            try:
                self.engine.run(rule, job)
            except Exception:
                log.exception("任务执行异常")

        threading.Thread(target=worker, name="qhs-job-%s" % job.id, daemon=True).start()
        return job

    # ---------- 账号 ----------
    def reconnect(self) -> None:
        self.cloud.disconnect()
        self.cloud = CloudService(self.config.cloud, self.config.token_path())
        self.engine.cloud = self.cloud
        self.cloud.connect()

    # ---------- 二维码登录 ----------
    def begin_qr_login(self) -> Dict[str, Any]:
        """取一张二维码图片。

        返回 ``{"ok": True, "qr_id": str, "qr": str}``，``qr`` 是 PNG 的 base64。
        """
        from . import pc189

        session = pc189.Pc189Qr()
        try:
            png = session.prepare()
        except pc189.Pc189Error as exc:
            session.close()
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            session.close()
            log.exception("二维码初始化失败")
            return {"ok": False, "error": "二维码初始化失败：%s" % exc}

        qr_id = uuid.uuid4().hex[:16]
        with self._lock:
            self.pending_qr[qr_id] = {"qr": session, "at": time.time()}
            while len(self.pending_qr) > 5:
                _old_id, old = self.pending_qr.popitem(last=False)
                try:
                    old["qr"].close()
                except Exception:
                    pass
        return {
            "ok": True,
            "qr_id": qr_id,
            "qr": base64.b64encode(png).decode("ascii"),
        }

    def poll_qr_login(self, qr_id: str) -> Dict[str, Any]:
        """查询扫码状态。

        返回 ``{"ok": True, "status": int, "message": str}``；
        status 为 0 表示登录成功，此时会话已装载。
        """
        from . import pc189

        with self._lock:
            pending = self.pending_qr.get(qr_id)
        if pending is None:
            return {"ok": False, "error": "二维码已过期，请重新获取"}

        try:
            result = pending["qr"].poll()
        except pc189.Pc189Error as exc:
            return {"ok": False, "status": -1, "message": str(exc)}
        except Exception as exc:
            log.exception("二维码状态查询失败")
            return {"ok": False, "status": -1, "message": "查询扫码状态失败：%s" % exc}

        status = result.get("status")
        if status == pc189.QR_SUCCESS:
            # 登录成功，清理挂起状态并装载会话
            with self._lock:
                self.pending_qr.pop(qr_id, None)
            try:
                pending["qr"].close()
            except Exception:
                pass
            try:
                self.adopt_session(result["session"])
            except Exception as exc:
                return {"ok": False, "status": -1, "message": "登录失败：%s" % exc}
            return {"ok": True, "status": pc189.QR_SUCCESS, "message": "登录成功"}
        if status == pc189.QR_EXPIRED:
            with self._lock:
                self.pending_qr.pop(qr_id, None)
            try:
                pending["qr"].close()
            except Exception:
                pass
        return {"ok": True, "status": status, "message": result.get("message") or ""}

    def adopt_session(self, session_data: Dict[str, Any]) -> None:
        """把登录得到的会话装到当前 CloudService 上。"""
        client = self.cloud.build_client(session_data)
        client.disk_info()
        self.cloud.client = client
        self.cloud.error = None
        self._disk_cache = None

    def state(self) -> Dict[str, Any]:
        """界面状态。

        **绝不能阻塞**：这个接口每 4 秒被轮询一次，一旦卡住，反向代理就会
        返回 502，界面直接崩掉。所以容量信息只用缓存，不在这里发请求。
        """
        rules = self.config.rules()
        cloud_ok = self.cloud.connected()
        return {
            "version": __version__,
            "time": time.time(),
            "cloud": {
                "connected": cloud_ok,
                "username": self.config.cloud.get("username"),
                "root_folder_id": self.config.cloud.get("root_folder_id"),
                "error": self.cloud.error,
                "disk": self._disk_cache["data"] if self._disk_cache else None,
            },
            "running": self.engine.running_rules(),
            "rules": [rule.to_dict() for rule in rules],
            # 主界面只显示当前正在做的任务
            "jobs": [job.to_dict() for job in self.list_jobs()
                     if job.status == "running"],
        }

    def _refresh_disk_info(self) -> None:
        """后台线程里刷新容量缓存。

        单独放在后台线程，是为了让 ``/api/state`` 永远立刻返回——
        网盘接口慢或断线时，界面依然能用。
        """
        try:
            data = self.cloud.disk_info()
        except Exception as exc:
            log.info("刷新网盘容量失败：%s", exc)
            data = None
        self._disk_cache = {"at": time.time(), "data": data}


class Handler(BaseHTTPRequestHandler):
    server_version = "QunHuiSync/" + __version__
    protocol_version = "HTTP/1.1"

    state: AppState = None  # 由 serve() 注入
    routes: List[Tuple[str, Any, Callable]] = []
    _cached_body: Dict[str, Any] = {}

    # ---------- 基础工具 ----------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self) -> None:
        """客户端提前断开时不打印堆栈，避免污染 NAS 日志。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body)

    def _body(self) -> Dict[str, Any]:
        """取请求体（JSON）。

        请求体在 :meth:`_dispatch` 里已经读进 ``_cached_body``，这里直接返回。
        统一在分发时读取，保证任何接口都不会漏读——HTTP/1.1 keep-alive 下
        残留的请求体会被下一个请求当成请求行解析，报 501。
        """
        return self._cached_body

    def _read_body(self) -> None:
        """把请求体读干净并存起来。"""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            self._cached_body = {}
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        self._cached_body = data if isinstance(data, dict) else {}

    def _authorized(self, params: Dict[str, List[str]]) -> bool:
        expected = (self.state.config.server.get("password") or "").strip()
        if not expected:
            return True
        given = self.headers.get("X-QHS-Password") or ""
        if not given:
            given = (params.get("password") or [""])[0]
        if not given:
            cookie = self.headers.get("Cookie") or ""
            match = re.search(r"qhs_password=([^;]+)", cookie)
            given = match.group(1) if match else ""
        return given == expected

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        # 先把请求体读干净：HTTP/1.1 keep-alive 下，没读掉的请求体会被
        # 下一个请求当成请求行解析
        self._read_body()
        if not self._authorized(params):
            self._json({"error": "密码错误"}, 401)
            return
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    handler(self, match, params)
                except Exception as exc:
                    log.exception("接口处理失败 %s" % path)
                    self._json({"error": str(exc)}, 500)
                return
        if method == "GET":
            self._serve_static(path)
            return
        self._json({"error": "not found"}, 404)

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        relative = posixpath.normpath(path).lstrip("/")
        if relative.startswith(".."):
            self._json({"error": "forbidden"}, 403)
            return
        full = os.path.join(WEB_DIR, relative.replace("/", os.sep))
        if not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as handle:
            self._send(200, handle.read(), ctype)


# ---------------- 接口实现 ----------------

def api_state(handler: Handler, match, params) -> None:
    handler._json(handler.state.state())


def api_get_config(handler: Handler, match, params) -> None:
    config = handler.state.config
    # 密码本身不回传（不往外泄），但要告诉前端「有没有设」，
    # 否则前端读不到密码，用户清空输入框保存后，
    # 不知道密码到底是被清掉了还是压根没读到。
    server = dict(config.server)
    server["has_password"] = bool((config.server.get("password") or "").strip())
    server.pop("password", None)
    handler._json({
        "cloud": config.cloud,
        "server": server,
        "rules": [rule.to_dict() for rule in config.rules()],
    })


def api_put_config(handler: Handler, match, params) -> None:
    body = handler._body()
    config = handler.state.config
    cloud = body.get("cloud") or {}
    if "password" in cloud and not cloud["password"]:
        cloud["password"] = config.cloud.get("password")
    config.set_cloud(cloud)
    config.set_server(body.get("server") or {})
    config.save()
    handler._json({"ok": True})


def api_check_acl(handler: Handler, match, params) -> None:
    """检查规则的本地目录权限，必要时尝试授权。"""
    body = handler._body()
    paths = body.get("paths")
    if not isinstance(paths, list):
        paths = [rule.local_path for rule in handler.state.config.rules()]
    handler._json({"results": acl.check_all([str(p) for p in paths])})


def api_qr_login(handler: Handler, match, params) -> None:
    """取一张登录二维码。"""
    result = handler.state.begin_qr_login()
    handler._json(result, 200)


def api_qr_poll(handler: Handler, match, params) -> None:
    """查询扫码状态。"""
    body = handler._body()
    qr_id = (body.get("qr_id") or "").strip()
    if not qr_id:
        handler._json({"ok": False, "error": "缺少二维码标识"}, 200)
        return
    handler._json(handler.state.poll_qr_login(qr_id), 200)


def api_connect(handler: Handler, match, params) -> None:
    try:
        handler.state.reconnect()
        handler._json({"ok": True})
    except CloudError as exc:
        handler._json({"ok": False, "error": str(exc)}, 200)


def api_remote_tree(handler: Handler, match, params) -> None:
    path = (params.get("path") or [""])[0]
    try:
        folder_id = handler.state.cloud.ensure_folder(path)
        entries = handler.state.cloud.list_dir(folder_id)
    except CloudError as exc:
        handler._json({"error": str(exc)}, 200)
        return
    handler._json({
        "path": path,
        "folders": [{"id": e.id, "name": e.name} for e in entries if e.is_folder],
    })


def api_save_rule(handler: Handler, match, params) -> None:
    body = handler._body()
    try:
        rule = handler.state.config.upsert_rule(body)
    except ValueError as exc:
        handler._json({"error": str(exc)}, 400)
        return
    handler._json({"ok": True, "rule": rule.to_dict()})


def api_delete_rule(handler: Handler, match, params) -> None:
    rule_id = (params.get("id") or [""])[0]
    handler._json({"ok": handler.state.config.delete_rule(rule_id)})


def api_toggle_rule(handler: Handler, match, params) -> None:
    body = handler._body()
    rule = handler.state.config.get_rule(body.get("id", ""))
    if rule is None:
        handler._json({"error": "规则不存在"}, 404)
        return
    rule.enabled = bool(body.get("enabled", True))
    handler.state.config.upsert_rule(rule.to_dict())
    handler._json({"ok": True})


def api_run_rule(handler: Handler, match, params) -> None:
    body = handler._body()
    rule_id = body.get("id") or (params.get("id") or [""])[0]
    job = handler.state.trigger(rule_id)
    if job is None:
        handler._json({"ok": False, "error": "规则不存在或正在运行"}, 200)
        return
    handler._json({"ok": True, "job": job.to_dict()})


def api_cancel_rule(handler: Handler, match, params) -> None:
    """取消正在运行的同步任务。"""
    body = handler._body()
    rule_id = body.get("id") or (params.get("id") or [""])[0]
    if handler.state.engine.cancel(rule_id):
        handler._json({"ok": True, "message": "已发出取消请求"})
    else:
        handler._json({"ok": False, "error": "该规则没有正在运行的任务"}, 200)


def api_jobs(handler: Handler, match, params) -> None:
    """正在运行的任务（只返回当前在做的）。"""
    running = [job for job in handler.state.list_jobs() if job.status == "running"]
    handler._json({"jobs": [job.to_dict() for job in running]})


def api_job_history(handler: Handler, match, params) -> None:
    """历史任务（不含日志，日志请用 /api/job/logs）。"""
    limit = 50
    try:
        limit = max(1, min(500, int((params.get("limit") or ["50"])[0])))
    except (TypeError, ValueError):
        pass
    jobs = [job.to_dict() for job in reversed(handler.state.list_jobs())]
    for item in jobs:
        item.pop("logs", None)
    handler._json({"jobs": jobs[:limit]})


def api_job_logs(handler: Handler, match, params) -> None:
    """某个任务的日志。"""
    job_id = (params.get("id") or [""])[0]
    if not job_id:
        handler._json({"error": "缺少任务 id"}, 400)
        return
    for job in handler.state.list_jobs():
        if job.id == job_id:
            handler._json({
                "id": job.id,
                "rule_name": job.rule_name,
                "status": job.status,
                "started": job.started,
                "finished": job.finished,
                "message": job.message,
                "logs": job.logs,
            })
            return
    handler._json({"error": "任务不存在"}, 404)


def api_cancel_file(handler: Handler, match, params) -> None:
    """取消正在上传的单个文件。"""
    body = handler._body()
    rule_id = body.get("id") or (params.get("id") or [""])[0]
    rel_path = body.get("path") or (params.get("path") or [""])[0]
    if not rel_path:
        handler._json({"ok": False, "error": "缺少文件路径"}, 200)
        return
    if handler.state.engine.cancel_file(rule_id, rel_path):
        handler._json({"ok": True, "message": "已发出取消请求"})
    else:
        handler._json({"ok": False, "error": "该文件已不在上传中"}, 200)


def api_local_browse(handler: Handler, match, params) -> None:
    """列出本地可用的共享目录，方便在界面上选路径。"""
    path = (params.get("path") or ["/"])[0]
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        handler._json({"error": str(exc)}, 200)
        return
    dirs = [n for n in names if os.path.isdir(os.path.join(path, n))]
    handler._json({"path": path, "dirs": dirs})


def build_routes() -> List[Tuple[str, Any, Callable]]:
    """路由表：方法 -> 路径正则 -> 处理函数。"""
    table = [
        ("GET", "/api/state", api_state),
        ("GET", "/api/config", api_get_config),
        ("PUT", "/api/config", api_put_config),
        ("POST", "/api/cloud/connect", api_connect),
        ("POST", "/api/cloud/qr", api_qr_login),
        ("POST", "/api/cloud/qr/poll", api_qr_poll),
        ("GET", "/api/cloud/tree", api_remote_tree),
        ("POST", "/api/rules", api_save_rule),
        ("DELETE", "/api/rules", api_delete_rule),
        ("POST", "/api/rules/toggle", api_toggle_rule),
        ("POST", "/api/run", api_run_rule),
        ("POST", "/api/cancel", api_cancel_rule),
        ("GET", "/api/jobs", api_jobs),
        ("GET", "/api/jobs/history", api_job_history),
        ("GET", "/api/job/logs", api_job_logs),
        ("POST", "/api/cancel/file", api_cancel_file),
        ("GET", "/api/local/browse", api_local_browse),
        ("POST", "/api/acl/check", api_check_acl),
    ]
    return [(method, re.compile("^" + path + "$"), handler) for method, path, handler in table]


def serve(config_path: str, host: str = "0.0.0.0", port: int = 8686) -> None:
    """启动 Web 服务。"""
    app = AppState(config_path)

    class BoundHandler(Handler):
        pass

    BoundHandler.state = app
    BoundHandler.routes = build_routes()

    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    log.info("Web 服务启动：http://%s:%d", host, port)
    app.scheduler.start()

    # 后台尝试用已保存的令牌连接网盘，套件重启后不用再扫码。
    # 连不上也不影响服务：界面仍可手动扫码。
    def auto_connect() -> None:
        try:
            app.reconnect()
            log.info("已用保存的令牌连接天翼网盘")
        except Exception as exc:
            log.info("自动连接天翼网盘失败：%s", exc)
        app._refresh_disk_info()

    threading.Thread(target=auto_connect, name="qhs-connect", daemon=True).start()

    # 容量信息后台刷新：/api/state 只读缓存，保证接口永不卡住
    def disk_refresher() -> None:
        while True:
            time.sleep(60)
            try:
                if app.cloud.connected():
                    app._refresh_disk_info()
            except Exception:
                log.debug("容量刷新异常", exc_info=True)

    threading.Thread(target=disk_refresher, name="qhs-disk", daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.scheduler.stop()
        httpd.server_close()
