"""模拟 DSM 安装流程：解包 spk -> 解压 package.tgz 到 target/ -> 启动服务验证。

用法：python build/simulate_install.py <spk 路径>
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def simulate(spk_path: Path) -> int:
    sim = Path(tempfile.mkdtemp(prefix="qhs_sim_"))
    target = sim / "target"          # 模拟 /var/packages/qunhuisync/target
    conf = sim / "config"            # 模拟 /var/packages/qunhuisync/config
    target.mkdir(parents=True)
    conf.mkdir(parents=True)

    print("== 解开外层 spk（未压缩 tar）==")
    with tarfile.open(spk_path, "r:") as tar:
        members = {m.name: m for m in tar.getmembers()}
        print("  外层成员: %s" % sorted(members))
        raw_info = tar.extractfile("INFO").read()
        info = raw_info.decode("utf-8-sig")
        tgz = tar.extractfile("package.tgz").read()

    # DSM 把 package.tgz 解压到 target/
    print("== 解压 package.tgz 到 target/ ==")
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as inner:
        inner.extractall(target)
    print("  target/ 内容: %s" % sorted(os.listdir(target)))

    # 写配置文件
    (conf / "config.json").write_text(json.dumps({
        "version": 1,
        "cloud": {"username": "", "password": ""},
        "server": {"host": "127.0.0.1", "port": 8791, "password": ""},
        "trash": {"retention_days": 7, "path": ""},
        "rules": [],
    }), encoding="utf-8")

    # 用打包内的 Python 依赖验证 import
    print("== 验证依赖可导入 ==")
    # 包里的加密库是 Linux 原生库，在 Windows 上无法加载；
    # 这里只验证「文件存在且能被找到」，加载测试留给真机。
    code = (
        "import sys, os;"
        "sys.path.insert(0, r'" + str(target / "deps") + "');"
        "sys.path.insert(0, r'" + str(target / "app") + "');"
        "import httpx, json;"
        "import Crypto;"
        "p = os.path.join(os.path.dirname(Crypto.__file__), 'Cipher');"
        "sos = [f for f in os.listdir(p) if f.endswith('.so')];"
        "assert sos, '未找到 Linux 原生库';"
        "print('deps OK: httpx', httpx.__version__, ', 原生库', len(sos), '个')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if result.returncode != 0:
        print("  依赖校验失败:\n%s" % result.stderr[-1500:])
        return 1
    print("  " + result.stdout.strip())

    # 启动服务
    print("== 启动服务 ==")
    env = dict(os.environ)
    env["PYTHONPATH"] = "%s;%s" % (target / "deps", target / "app")
    proc = subprocess.Popen(
        [sys.executable, "-m", "qunhuisync", "serve",
         "--config", str(conf / "config.json"), "--host", "127.0.0.1", "--port", "8791"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    import time
    import urllib.request

    ok = False
    for _ in range(30):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8791/api/state", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            print("  服务响应正常: version=%s rules=%d" % (data["version"], len(data["rules"])))
            ok = True
            break
        except Exception:
            if proc.poll() is not None:
                break
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    if not ok:
        print("  服务启动失败，输出：\n%s" % (proc.stdout.read() if proc.stdout else ""))
        return 1

    shutil.rmtree(sim, ignore_errors=True)
    print("== 模拟安装成功 ==")
    return 0


if __name__ == "__main__":
    target_spk = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(
        (ROOT / "dist").glob("*.spk"))[0]
    print("模拟安装: %s" % target_spk.name)
    sys.exit(simulate(target_spk))
