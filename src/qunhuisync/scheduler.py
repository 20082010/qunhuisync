"""定时调度：在后台线程里按规则的计划自动触发同步。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from .config import Config

log = logging.getLogger("qunhuisync.scheduler")


class Scheduler:
    """每 30 秒检查一次，到点的规则交给回调执行。"""

    def __init__(self, config: Config, runner: Callable[[str], None]):
        self.config = config
        self.runner = runner
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qhs-scheduler", daemon=True)
        self._thread.start()
        log.info("调度线程已启动")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._check()
            except Exception:
                log.exception("调度检查失败")

    def _check(self) -> None:
        now = time.time()
        for rule in self.config.rules():
            if not rule.enabled or rule.schedule.type == "manual":
                continue
            if not self._due(rule, now):
                continue
            log.info("计划触发规则：%s" % (rule.name or rule.local_path))
            try:
                self.runner(rule.id)
            except Exception:
                log.exception("计划任务启动失败：%s" % rule.id)

    @staticmethod
    def _due(rule, now: float) -> bool:
        schedule = rule.schedule
        last_run = rule.last_run or 0
        if schedule.type == "interval":
            interval = max(1, int(schedule.minutes or 60)) * 60
            return now - last_run >= interval
        if schedule.type == "daily":
            try:
                hour, minute = [int(part) for part in schedule.time.split(":")][:2]
            except Exception:
                return False
            current = time.localtime(now)
            # time.mktime 需要 9 元组：(年, 月, 日, 时, 分, 秒, 星期几, 年内第几天, 夏令时标志)
            target = time.mktime((current.tm_year, current.tm_mon, current.tm_mday, hour, minute, 0,
                                  current.tm_wday, current.tm_yday, -1))
            return now >= target and last_run < target
        return False
