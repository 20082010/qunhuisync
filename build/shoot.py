"""用 Playwright 驱动系统 Edge 截取界面图。

用法：python build/shoot.py <base_url> <输出目录>
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8687/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs"

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

SHOTS = [
    ("01-总览.png", None),
    ("02-新建规则.png", "#btnNewRule"),
    ("03-目录浏览.png", "#btnBrowse"),
    ("04-任务日志.png", "#jobDemo"),
    ("05-目录权限.png", "#aclDemo"),
    ("06-扫码登录.png", "#qrDemo"),
    ("07-账号设置.png", "#accountCard"),
]

# 演示用：伪造当前任务，展示「哈希阶段」和「传输阶段」两种进度
DEMO_RUNNING = """
window.__demoRunning = [{
  id:'r1', rule_id:'rule_photos', rule_name:'家庭照片归档', status:'running',
  started:1793190000, finished:0, scanned:12, uploaded:2, skipped:0, deleted:0,
  zero_deleted:0, failed:0, bytes_uploaded:52428800, message:'',
  cancel_requested:false, current_index:3, current_total_files:12,
  files:[
    {path:'/volume1/photos/20241012_091530.mp4', name:'20241012_091530.mp4',
     size:2147483648, done:1073741824, speed:3145728, percent:50, cancelled:false,
     elapsed:320, hashing:false, hash_done:0, hash_size:0, hash_percent:0},
    {path:'/volume1/photos/20241012_140215.mp4', name:'20241012_140215.mp4',
     size:1073741824, done:0, speed:0, percent:0, cancelled:false,
     elapsed:40, hashing:true, hash_done:322122547, hash_size:1073741824, hash_percent:30},
    {path:'/volume1/photos/IMG_20241012.jpg', name:'IMG_20241012.jpg',
     size:5242880, done:3145728, speed:1048576, percent:60, cancelled:false,
     elapsed:12, hashing:false, hash_done:0, hash_size:0, hash_percent:0}
  ]
}];
"""

# 演示用：伪造两条已完成任务，让「任务与日志」面板有内容
DEMO_JOBS = """
window.__demoJobs = [
  {id:'a1',rule_id:'rule_photos',rule_name:'家庭照片归档',status:'success',started:1793190000,finished:1793190120,
   scanned:9,uploaded:7,skipped:2,deleted:7,failed:0,bytes_uploaded:13107200,message:'完成',
   logs:['[2026-10-28 02:00:01] 开始同步：本地 /volume1/photos -> 云端 /群晖电信同步/照片',
         '[2026-10-28 02:00:01] 扫描到 9 个待同步文件',
         '[2026-10-28 02:00:03] 同步结束：上传 7，跳过 2，删除源文件 7，失败 0，传输 12.5 MB']},
  {id:'a2',rule_id:'rule_videos',rule_name:'行车视频转存',status:'partial',started:1793189000,finished:1793189400,
   scanned:3,uploaded:2,skipped:1,deleted:2,failed:1,bytes_uploaded:52428800,message:'完成，但有 1 个文件失败',
   logs:['[2026-10-28 01:30:00] 开始同步：本地 /volume1/videos -> 云端 /群晖电信同步/视频',
         '[2026-10-28 01:31:00] 校验未通过，保留源文件：20241012_091530.mp4',
         '[2026-10-28 01:31:40] 同步结束：上传 2，跳过 1，删除源文件 2，失败 1，传输 50.0 MB']}
];
"""

# 演示用：伪造「目录权限不足」的提示区块
DEMO_ACL = """
window.__demoAcl = [
  {path:'/volume1/photos', exists:true, readable:false, writable:false, granted:false,
   message:'当前服务账号无权访问该目录。请在 DSM「控制面板 → 共享文件夹 → 权限」中，把「系统内部用户」sc-qunhuisync 设为读写权限。'},
  {path:'/volume1/videos', exists:false, readable:false, writable:false, granted:false,
   message:'目录不存在'}
];
ACL_MAP = {'/volume1/photos': window.__demoAcl[0], '/volume1/videos': window.__demoAcl[1]};
renderAclNotice(window.__demoAcl);
renderRules();
"""

# 演示用：伪造一张登录二维码，模拟扫码登录界面
DEMO_QR = """
(function () {
  var canvas = document.createElement('canvas');
  canvas.width = 190; canvas.height = 190;
  var ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 190, 190);
  // 画一个像模像样的二维码方阵
  var cell = 5, cols = 33, off = 12;
  var seed = 20261102;
  function rnd() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; }
  ctx.fillStyle = '#111';
  function finder(x, y) {
    ctx.fillRect(off + x * cell, off + y * cell, cell * 7, cell * 7);
    ctx.fillStyle = '#fff';
    ctx.fillRect(off + (x + 1) * cell, off + (y + 1) * cell, cell * 5, cell * 5);
    ctx.fillStyle = '#111';
    ctx.fillRect(off + (x + 2) * cell, off + (y + 2) * cell, cell * 3, cell * 3);
  }
  finder(0, 0); finder(cols - 7, 0); finder(0, cols - 7);
  for (var y = 0; y < cols; y++) {
    for (var x = 0; x < cols; x++) {
      var inFinder = (x < 8 && y < 8) || (x > cols - 9 && y < 8) || (x < 8 && y > cols - 9);
      if (inFinder) continue;
      if (rnd() > 0.52) ctx.fillRect(off + x * cell, off + y * cell, cell, cell);
    }
  }
  document.getElementById('qrBox').hidden = false;
  document.getElementById('qrImg').src = canvas.toDataURL('image/png');
  document.getElementById('qrHint').textContent = '已扫码，请在手机上确认';
})();
"""


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=EDGE, args=["--force-device-scale-factor=2"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)

        # 截图是在 Windows 上跑的，本地目录树与真实群晖不同，
        # 这里拦截几个接口，换成群晖上的典型路径，避免截图里出现 Windows 目录。
        def handle_route(route):
            url = route.request.url
            if "/api/local/browse" in url:
                body = json.dumps({"path": "/volume1", "dirs": ["docker", "homes", "photos", "video"]})
                route.fulfill(status=200, content_type="application/json", body=body)
            elif "/api/acl/check" in url:
                body = json.dumps({"results": [
                    {"path": "/volume1/photos", "exists": True, "readable": True,
                     "writable": True, "granted": False, "message": "权限正常"},
                    {"path": "/volume1/videos", "exists": True, "readable": True,
                     "writable": True, "granted": False, "message": "权限正常"},
                ]})
                route.fulfill(status=200, content_type="application/json", body=body)
            elif "/api/state" in url:
                # 抹掉本机产生的失败任务，让截图保持干净
                try:
                    response = route.fetch()
                    state = json.loads(response.text())
                except Exception:
                    route.continue_()
                    return
                state["jobs"] = []
                state["cloud"]["connected"] = True
                state["cloud"]["disk"] = {"used": 128 * 1024 ** 3, "total": 1024 * 1024 ** 3,
                                          "free": 896 * 1024 ** 3}
                for rule in state.get("rules", []):
                    rule["last_status"] = "success"
                    rule["last_run"] = 1793190000
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(state, ensure_ascii=False))
            else:
                route.continue_()

        page.route("**/api/**", handle_route)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.evaluate(DEMO_JOBS)
        page.evaluate(DEMO_RUNNING)

        for name, click in SHOTS:
            if click == "#jobDemo":
                page.evaluate("STATE.jobs = window.__demoJobs; renderJobs();")
                page.wait_for_timeout(400)
                page.locator("#jobList").scroll_into_view_if_needed()
                page.wait_for_timeout(300)
            elif click == "#accountCard":
                # 「当前任务」面板展示哈希阶段 + 传输阶段
                page.evaluate("STATE.jobs = window.__demoRunning; renderJobs();")
                page.wait_for_timeout(400)
                page.locator("#accountCard").scroll_into_view_if_needed()
                page.wait_for_timeout(300)
            elif click == "#aclDemo":
                page.evaluate(DEMO_ACL)
                page.wait_for_timeout(500)
            elif click == "#qrDemo":
                page.evaluate(DEMO_QR)
                page.wait_for_timeout(600)
            elif click == "#btnBrowse":
                page.click("#btnNewRule")
                page.wait_for_timeout(600)
                page.click("#btnBrowse")
                page.wait_for_timeout(900)
            elif click:
                page.click(click)
                page.wait_for_timeout(700)
            path = os.path.join(OUT, name)
            page.screenshot(path=path, full_page=(click is None))
            print("saved:", path)
            if click == "#jobDemo":
                page.evaluate("STATE.jobs = []; renderJobs();")
                page.wait_for_timeout(200)
                continue
            if click == "#aclDemo":
                page.evaluate("document.getElementById('aclNotice').hidden = true;")
                page.wait_for_timeout(200)
                continue
            if click == "#qrDemo":
                page.evaluate("document.getElementById('qrBox').hidden = true;")
                page.wait_for_timeout(200)
                continue
            if click:
                page.evaluate("document.getElementById('ruleModal').hidden = true;"
                              "document.getElementById('dirPicker').hidden = true;")
                page.wait_for_timeout(300)

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
