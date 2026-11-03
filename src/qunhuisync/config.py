"""配置管理：读写 JSON 配置，提供同步规则的增删改查。"""
from __future__ import annotations

import copy
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .util import atomic_write_json, read_json

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "cloud": {
        "username": "",
        "password": "",
        "root_folder_id": "-11",
        # 同时上传几个文件。这是控制后台资源占用的唯一旋钮：
        # 线程越多传得越快，占用的 CPU 和带宽也越多。默认 2。
        "upload_threads": 2,
        # 上传限速（字节/秒），0 表示不限速。
        "speed_limit": 0,
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8686,
        "password": "",
    },
    "rules": [],
}

TRASH_DIR_NAME = ".qunhuisync_trash"


@dataclass
class Schedule:
    """执行计划：manual 手动 / interval 间隔分钟 / daily 每天固定时间。"""

    type: str = "manual"
    minutes: int = 60
    time: str = "02:00"

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]]) -> "Schedule":
        data = data or {}
        return Schedule(
            type=str(data.get("type", "manual")),
            minutes=int(data.get("minutes", 60) or 60),
            time=str(data.get("time", "02:00")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Rule:
    """一条同步规则：本地目录 -> 天翼网盘目录。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    local_path: str = ""
    recursive: bool = True
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    exclude_dirs: List[str] = field(default_factory=lambda: ["@eaDir", TRASH_DIR_NAME])
    remote_path: str = "/"
    preserve_dir_structure: bool = True
    delete_source: bool = False
    verify_mode: str = "size"
    overwrite: bool = True
    delete_empty_dirs: bool = False
    delete_zero_byte: bool = False
    min_size_bytes: int = 0
    schedule: Schedule = field(default_factory=Schedule)
    last_run: float = 0
    last_status: str = ""

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Rule":
        rule = Rule()
        rule.id = str(data.get("id") or uuid.uuid4().hex[:12])
        rule.name = str(data.get("name") or "")
        rule.enabled = bool(data.get("enabled", True))
        rule.local_path = str(data.get("local_path") or "").strip()
        rule.recursive = bool(data.get("recursive", True))
        rule.include = _as_list(data.get("include"))
        rule.exclude = _as_list(data.get("exclude"))
        exclude_dirs = _as_list(data.get("exclude_dirs")) or ["@eaDir", TRASH_DIR_NAME]
        if TRASH_DIR_NAME not in exclude_dirs:
            exclude_dirs.append(TRASH_DIR_NAME)
        rule.exclude_dirs = exclude_dirs
        rule.remote_path = str(data.get("remote_path") or "/").strip() or "/"
        rule.preserve_dir_structure = bool(data.get("preserve_dir_structure", True))
        rule.delete_source = bool(data.get("delete_source", False))
        rule.verify_mode = str(data.get("verify_mode") or "size")
        rule.overwrite = bool(data.get("overwrite", True))
        rule.delete_empty_dirs = bool(data.get("delete_empty_dirs", False))
        rule.delete_zero_byte = bool(data.get("delete_zero_byte", False))
        rule.min_size_bytes = _read_min_size(data)
        rule.schedule = Schedule.from_dict(data.get("schedule"))
        rule.last_run = float(data.get("last_run") or 0)
        rule.last_status = str(data.get("last_status") or "")
        return rule

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["schedule"] = self.schedule.to_dict()
        return data


def _read_min_size(data: Dict[str, Any]) -> int:
    """读「最小文件大小」，单位字节。

    旧配置存的是 ``min_size_mb``（MB），这里做兼容转换，
    以免用户升级后原有的规则设置失效。
    """
    if "min_size_bytes" in data and data["min_size_bytes"] is not None:
        try:
            return max(0, int(float(data["min_size_bytes"])))
        except (TypeError, ValueError):
            return 0
    if "min_size_mb" in data and data["min_size_mb"] is not None:
        try:
            return max(0, int(float(data["min_size_mb"]) * 1024 * 1024))
        except (TypeError, ValueError):
            return 0
    return 0


def _as_list(value) -> List[str]:
    """兼容字符串/列表两种写法。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").replace("，", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple)):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


class Config:
    """线程安全的配置容器。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        stored = read_json(self.path)
        with self._lock:
            if isinstance(stored, dict):
                self.data = self._merge(copy.deepcopy(DEFAULT_CONFIG), stored)
            else:
                self.data = copy.deepcopy(DEFAULT_CONFIG)

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self.data)

    @staticmethod
    def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = Config._merge(result[key], value)
            else:
                result[key] = value
        return result

    # ---------- 分区访问 ----------
    @property
    def cloud(self) -> Dict[str, Any]:
        with self._lock:
            return self.data["cloud"]

    @property
    def server(self) -> Dict[str, Any]:
        with self._lock:
            return self.data["server"]

    def set_cloud(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.data["cloud"].update({k: v for k, v in (data or {}).items() if v is not None})

    def set_server(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.data["server"].update({k: v for k, v in (data or {}).items() if v is not None})

    # ---------- 规则 ----------
    def rules(self) -> List[Rule]:
        with self._lock:
            return [Rule.from_dict(item) for item in self.data.get("rules", [])]

    def get_rule(self, rule_id: str) -> Optional[Rule]:
        for rule in self.rules():
            if rule.id == rule_id:
                return rule
        return None

    def upsert_rule(self, data: Dict[str, Any]) -> Rule:
        rule = Rule.from_dict(data)
        if not rule.local_path:
            raise ValueError("本地目录不能为空")
        with self._lock:
            rules = self.data.setdefault("rules", [])
            for index, item in enumerate(rules):
                if item.get("id") == rule.id:
                    merged = dict(item)
                    merged.update(rule.to_dict())
                    rules[index] = merged
                    break
            else:
                rules.append(rule.to_dict())
            self.save()
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            rules = self.data.setdefault("rules", [])
            remain = [item for item in rules if item.get("id") != rule_id]
            if len(remain) == len(rules):
                return False
            self.data["rules"] = remain
            self.save()
            return True

    def update_rule_state(self, rule_id: str, **fields) -> None:
        with self._lock:
            for item in self.data.setdefault("rules", []):
                if item.get("id") == rule_id:
                    item.update(fields)
                    break
            self.save()

    # ---------- 其它 ----------
    def token_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), "token.json")

    def state_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), "state.json")

    def load_state(self) -> Dict[str, Any]:
        """读取同步状态：记录哪些文件已经同步过，避免重复上传。"""
        stored = read_json(self.state_path())
        if not isinstance(stored, dict):
            return {"rules": {}}
        stored.setdefault("rules", {})
        return stored

    def save_state(self, state: Dict[str, Any]) -> None:
        atomic_write_json(self.state_path(), state)
