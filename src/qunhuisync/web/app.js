/* 群晖电信同步工具 前端逻辑 */
const $ = (id) => document.getElementById(id);

let STATE = { rules: [], jobs: [], cloud: {}, running: {} };
let ACL_MAP = {};
let editingRuleId = null;
let webPassword = localStorage.getItem('qhs_password') || '';

function headers(json) {
  const h = { 'Content-Type': 'application/json' };
  if (webPassword) h['X-QHS-Password'] = webPassword;
  return h;
}

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign(headers(), options.headers || {});
  if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  delete opts.tolerateError;
  const res = await fetch(path, opts);
  if (res.status === 401) {
    const pwd = prompt('请输入网页访问密码');
    if (pwd) {
      webPassword = pwd;
      localStorage.setItem('qhs_password', pwd);
      return api(path, options);
    }
    throw new Error('未授权');
  }
  // 反向代理返回 502/504 时，响应体是 HTML 而不是 JSON。
  // 这里必须显式区分，否则会拿到 {}，后面读 .length 直接崩掉整个界面。
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    throw new Error(res.ok ? '服务返回了非预期的内容' : `服务异常（HTTP ${res.status}）`);
  }
  if (!data || typeof data !== 'object') {
    throw new Error('服务返回了非预期的内容');
  }
  // tolerateError：接口自己会处理错误分支时，不在这里抛异常
  if (data.error && !options.tolerateError) throw new Error(data.error);
  return data;
}

function toast(message, isError) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast' + (isError ? ' err' : '');
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3200);
}

function humanSize(num) {
  if (num === undefined || num === null) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Number(num);
  let i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i += 1; }
  return `${size.toFixed(1)} ${units[i]}`;
}

function escapeHtml(text) {
  return String(text === undefined || text === null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function scheduleText(schedule) {
  if (!schedule) return '仅手动';
  if (schedule.type === 'interval') return `每 ${schedule.minutes} 分钟`;
  if (schedule.type === 'daily') return `每天 ${schedule.time}`;
  return '仅手动';
}

/* ---------------- 渲染 ---------------- */
function renderStatus() {
  const chip = (id, text, cls) => { const el = $(id); el.textContent = text; el.className = 'chip' + (cls ? ' ' + cls : ''); };
  chip('chipService', '服务正常', 'ok');
  const cloud = STATE.cloud || {};
  if (cloud.connected) {
    chip('chipAccount', `已连接 ${cloud.username || ''}`, 'ok');
  } else {
    chip('chipAccount', '账号未连接', cloud.error ? 'bad' : 'warn');
  }
  const disk = cloud.disk || {};
  chip('chipDisk', disk.total ? `容量 ${humanSize(disk.total)} / 已用 ${humanSize(disk.used)}` : '容量未知');
  $('chipVersion').textContent = 'v' + (STATE.version || '');
}

function renderRules() {
  const box = $('ruleList');
  const rules = Array.isArray(STATE.rules) ? STATE.rules : [];
  if (!rules.length) {
    box.innerHTML = '<div class="empty">还没有同步规则，点击右上角「新建规则」开始。</div>';
    return;
  }
  box.innerHTML = rules.map((rule) => {
    const running = !!(STATE.running || {})[rule.id];
    const acl = ACL_MAP[rule.local_path];
    const tags = [];
    tags.push(rule.include && rule.include.length ? `类型 ${rule.include.join(', ')}` : '全部文件');
    tags.push(rule.verify_mode === 'md5' ? 'MD5 校验' : rule.verify_mode === 'none' ? '不校验' : '大小校验');
    tags.push(scheduleText(rule.schedule));
    if (rule.delete_source) tags.push('<b style="color:#ffb454">同步后删除源文件</b>');
    if (acl && !acl.writable) {
      tags.push('<b style="color:#ff6b6b">目录无读写权限</b>');
    }
    const status = running ? '<span class="chip warn">同步中</span>'
      : rule.last_status ? `<span class="chip ${rule.last_status === 'success' ? 'ok' : 'bad'}">${escapeHtml(rule.last_status)}</span>` : '';
    return `
      <div class="rule ${rule.enabled ? '' : 'off'}">
        <div class="rule-top">
          <div>
            <div class="rule-title">${escapeHtml(rule.name || '未命名规则')} ${status}</div>
            <div class="rule-path"><b>${escapeHtml(rule.local_path)}</b> → ${escapeHtml(rule.remote_path)}</div>
          </div>
          <div class="rule-actions">
            <button class="btn small" data-act="run" data-id="${rule.id}" ${running ? 'disabled' : ''}>立即同步</button>
            <button class="btn small" data-act="toggle" data-id="${rule.id}">${rule.enabled ? '停用' : '启用'}</button>
            <button class="btn small" data-act="edit" data-id="${rule.id}">编辑</button>
            <button class="btn small danger" data-act="del" data-id="${rule.id}">删除</button>
          </div>
        </div>
        <div class="rule-meta">${tags.map((t) => `<span class="chip ghost">${t}</span>`).join('')}</div>
      </div>`;
  }).join('');

  box.querySelectorAll('button[data-act]').forEach((btn) => {
    btn.addEventListener('click', () => onRuleAction(btn.dataset.act, btn.dataset.id));
  });
}

/* 检查规则目录的访问权限 */
async function checkAcl() {
  try {
    const res = await api('/api/acl/check', { method: 'POST', body: {} });
    ACL_MAP = {};
    (res.results || []).forEach((item) => { ACL_MAP[item.path] = item; });
    renderAclNotice(res.results || []);
    renderRules();
  } catch (err) {
    /* 权限检查失败不影响主流程 */
  }
}

/* 渲染权限提示区块 */
function renderAclNotice(results) {
  const box = $('aclNotice');
  const list = $('aclList');
  const bad = results.filter((item) => !item.writable);
  if (!bad.length) {
    box.hidden = true;
    list.innerHTML = '';
    return;
  }
  box.hidden = false;
  list.innerHTML = bad.map((item) => `
    <div class="acl-item">
      <span class="chip bad">${item.exists ? '权限不足' : '目录不存在'}</span>
      <code>${escapeHtml(item.path)}</code>
    </div>`).join('');
}

function renderJobs() {
  const box = $('jobList');
  const jobs = Array.isArray(STATE.jobs) ? STATE.jobs : [];
  if (!jobs.length) {
    $('chipJobs').textContent = '空闲';
    box.innerHTML = '<div class="empty">当前没有正在进行的任务。</div>';
    return;
  }
  $('chipJobs').textContent = `${jobs.length} 个任务进行中`;
  box.innerHTML = jobs.map((job) => {
    const files = Array.isArray(job.files) ? job.files : [];
    const cancelling = job.cancel_requested;
    const fileRows = files.map((file) => {
      const percent = Math.min(100, file.percent || 0);
      const speed = file.speed ? humanSize(file.speed) + '/s' : '—';
      const cancellingFile = file.cancelled;
      return `
        <div class="job-file">
          <div class="progress-file">
            <span class="progress-name" title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</span>
            <span class="progress-num">${humanSize(file.done)} / ${humanSize(file.size)} · ${speed}</span>
          </div>
          <div class="progress"><div class="progress-bar" style="width:${percent}%"></div></div>
          <div class="progress-foot">
            <span class="chip ghost">${percent}%</span>
            ${cancellingFile
              ? '<span class="chip warn">正在取消…</span>'
              : `<button class="btn small danger" data-act="cancel-file"
                   data-id="${escapeHtml(job.rule_id)}" data-path="${escapeHtml(file.path)}">取消此文件</button>`}
          </div>
        </div>`;
    }).join('');

    return `
      <div class="job">
        <div class="job-head">
          <div class="rule-title">${escapeHtml(job.rule_name || job.rule_id)} <span class="chip warn">同步中</span></div>
          <div class="rule-path">第 ${job.current_index || 0} / ${job.current_total_files || 0} 个文件</div>
        </div>
        ${fileRows ? `<div class="job-files">${fileRows}</div>` : ''}
        <div class="progress-foot">
          <span class="chip ghost">已上传 ${job.uploaded} 个</span>
          <span class="chip ghost">已删 0 字节 ${job.zero_deleted || 0} 个</span>
          <span class="chip ghost">失败 ${job.failed} 个</span>
        </div>
        <div class="job-actions">
          ${cancelling
            ? '<span class="chip warn">正在取消…</span>'
            : `<button class="btn small danger" data-act="cancel" data-id="${escapeHtml(job.rule_id)}">取消整个任务</button>`}
          <a class="btn small ghost" href="/logs.html?id=${encodeURIComponent(job.id)}" target="_blank">查看日志</a>
        </div>
      </div>`;
  }).join('');

  box.querySelectorAll("button[data-act='cancel']").forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await api('/api/cancel', {
          method: 'POST', tolerateError: true,
          body: { id: btn.dataset.id },
        });
        if (res && res.ok) toast('已发出取消请求');
        else toast('取消失败：' + ((res && res.error) || '未知错误'), true);
      } catch (err) {
        toast('取消失败：' + err.message, true);
      }
      refresh();
    });
  });

  box.querySelectorAll("button[data-act='cancel-file']").forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await api('/api/cancel/file', {
          method: 'POST', tolerateError: true,
          body: { id: btn.dataset.id, path: btn.dataset.path },
        });
        if (res && res.ok) toast('已取消该文件');
        else toast('取消失败：' + ((res && res.error) || '未知错误'), true);
      } catch (err) {
        toast('取消失败：' + err.message, true);
      }
      refresh();
    });
  });
}

async function refresh() {
  try {
    STATE = await api('/api/state');
    renderStatus();
    renderRules();
    renderJobs();
  } catch (err) {
    toast('刷新失败：' + err.message, true);
  }
}

/* ---------------- 账号 ---------------- */
async function loadConfig() {
  const cfg = await api('/api/config');
  $('accRoot').value = cfg.cloud.root_folder_id || '-11';
  $('accThreads').value = cfg.cloud.upload_threads || 3;
  $('accWebPassword').value = cfg.server.password || '';
}

async function saveAccount() {
  const body = {
    cloud: {
      root_folder_id: $('accRoot').value.trim() || '-11',
      upload_threads: Math.max(1, Math.min(8, Number($('accThreads').value || 3))),
    },
    server: { password: $('accWebPassword').value },
  };
  if (!body.server.password) webPassword = '';
  await api('/api/config', { method: 'PUT', body });
  toast('账号设置已保存');
}

/* ---------------- 二维码登录 ---------------- */
let qrId = '';
let qrTimer = null;

async function startQrLogin() {
  stopQrTimer();
  try {
    const res = await api('/api/cloud/qr', { method: 'POST', body: {}, tolerateError: true });
    if (!res || !res.ok) {
      $('qrBox').hidden = false;
      $('qrHint').textContent = (res && res.error) || '获取二维码失败';
      return;
    }
    qrId = res.qr_id || '';
    $('qrImg').src = 'data:image/png;base64,' + res.qr;
    $('qrHint').textContent = '正在等待扫码…';
    $('qrBox').hidden = false;
    $('accountCard').scrollIntoView({ behavior: 'smooth', block: 'center' });
    qrTimer = setInterval(pollQrStatus, 2000);
  } catch (err) {
    toast('获取二维码失败：' + err.message, true);
  }
}

async function pollQrStatus() {
  if (!qrId) return;
  try {
    const res = await api('/api/cloud/qr/poll', {
      method: 'POST',
      body: { qr_id: qrId },
      tolerateError: true,
    });
    if (!res || !res.ok) {
      stopQrTimer();
      $('qrHint').textContent = (res && res.message) || (res && res.error) || '扫码登录失败';
      return;
    }
    if (res.status === 0) {
      stopQrTimer();
      $('qrHint').textContent = '登录成功';
      $('qrBox').hidden = true;
      toast('登录成功');
      refresh();
      return;
    }
    if (res.status === -11001) {
      stopQrTimer();
      $('qrHint').textContent = '二维码已过期，点「换一张」重新获取';
      return;
    }
    $('qrHint').textContent = res.message || '等待扫码…';
  } catch (err) {
    stopQrTimer();
    $('qrHint').textContent = '查询扫码状态失败：' + err.message;
  }
}

function stopQrTimer() {
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = null;
  }
}

/* ---------------- 规则 ---------------- */
function openRuleModal(rule) {
  editingRuleId = rule ? rule.id : null;
  $('ruleModalTitle').textContent = rule ? '编辑规则' : '新建规则';
  $('fName').value = rule ? rule.name : '';
  $('fLocal').value = rule ? rule.local_path : '';
  $('fRemote').value = rule ? rule.remote_path : '/';
  $('fScheduleType').value = rule ? rule.schedule.type : 'manual';
  $('fInterval').value = rule && rule.schedule.minutes ? rule.schedule.minutes : 60;
  $('fDaily').value = rule && rule.schedule.time ? rule.schedule.time : '02:00';
  $('fInclude').value = rule && rule.include ? rule.include.join(', ') : '';
  $('fExclude').value = rule && rule.exclude ? rule.exclude.join(', ') : '';
  setMinSizeFields(rule ? rule.min_size_bytes : 0);
  $('fVerify').value = rule ? rule.verify_mode : 'size';
  $('fEnabled').checked = rule ? rule.enabled : true;
  $('fRecursive').checked = rule ? rule.recursive : true;
  $('fPreserve').checked = rule ? rule.preserve_dir_structure : true;
  $('fDelete').checked = rule ? rule.delete_source : false;
  $('fOverwrite').checked = rule ? rule.overwrite : true;
  $('fDeleteEmpty').checked = rule ? rule.delete_empty_dirs : false;
  $('fDeleteZero').checked = rule ? rule.delete_zero_byte : false;
  syncScheduleFields();
  $('dirPicker').hidden = true;
  $('ruleModal').hidden = false;
}

function syncScheduleFields() {
  const type = $('fScheduleType').value;
  $('wrapInterval').hidden = type !== 'interval';
  $('wrapDaily').hidden = type !== 'daily';
}

function parseList(text) {
  return (text || '').split(/[,;，]/).map((s) => s.trim()).filter(Boolean);
}

/* 最小文件大小：数值 + 单位，存储时统一换算成字节 */
const MIN_SIZE_UNITS = [
  { value: 1, label: '字节' },
  { value: 1024, label: 'KB' },
  { value: 1048576, label: 'MB' },
  { value: 1073741824, label: 'GB' },
];

/* 按字节数挑一个合适的单位填进表单 */
function setMinSizeFields(bytes) {
  const total = Number(bytes || 0);
  if (!total) {
    $('fMinSize').value = 0;
    $('fMinSizeUnit').value = '1048576';
    return;
  }
  let unit = MIN_SIZE_UNITS[0];
  for (const item of MIN_SIZE_UNITS) {
    if (total >= item.value) unit = item;
  }
  $('fMinSizeUnit').value = String(unit.value);
  const value = total / unit.value;
  $('fMinSize').value = Number.isInteger(value) ? value : value.toFixed(3);
}

/* 读表单里的数值+单位，换算成字节 */
function readMinSizeBytes() {
  const value = Number($('fMinSize').value || 0);
  const unit = Number($('fMinSizeUnit').value || 1);
  if (!value || value <= 0) return 0;
  return Math.round(value * unit);
}

async function saveRule() {
  const body = {
    id: editingRuleId || undefined,
    name: $('fName').value.trim(),
    local_path: $('fLocal').value.trim(),
    remote_path: $('fRemote').value.trim() || '/',
    include: parseList($('fInclude').value),
    exclude: parseList($('fExclude').value),
    min_size_bytes: readMinSizeBytes(),
    verify_mode: $('fVerify').value,
    enabled: $('fEnabled').checked,
    recursive: $('fRecursive').checked,
    preserve_dir_structure: $('fPreserve').checked,
    delete_source: $('fDelete').checked,
    overwrite: $('fOverwrite').checked,
    delete_empty_dirs: $('fDeleteEmpty').checked,
    delete_zero_byte: $('fDeleteZero').checked,
    schedule: {
      type: $('fScheduleType').value,
      minutes: Number($('fInterval').value || 60),
      time: $('fDaily').value.trim() || '02:00',
    },
  };
  if (!body.local_path) { toast('请填写本地目录', true); return; }
  try {
    await api('/api/rules', { method: 'POST', body });
    $('ruleModal').hidden = true;
    toast('规则已保存');
    refresh();
    checkAcl();
  } catch (err) {
    toast('保存失败：' + err.message, true);
  }
}

async function onRuleAction(act, id) {
  try {
    if (act === 'run') {
      const res = await api('/api/run', { method: 'POST', body: { id } });
      if (!res.ok) toast(res.error || '无法启动同步', true);
      else toast('同步任务已启动');
      refresh();
    } else if (act === 'toggle') {
      const rule = STATE.rules.find((r) => r.id === id);
      await api('/api/rules/toggle', { method: 'POST', body: { id, enabled: !(rule && rule.enabled) } });
      refresh();
    } else if (act === 'edit') {
      const rule = STATE.rules.find((r) => r.id === id);
      openRuleModal(rule);
    } else if (act === 'del') {
      if (!confirm('确定删除这条规则？已上传的文件不会受影响。')) return;
      await api('/api/rules?id=' + encodeURIComponent(id), { method: 'DELETE' });
      toast('规则已删除');
      refresh();
    }
  } catch (err) {
    toast('操作失败：' + err.message, true);
  }
}

/* ---------------- 本地目录浏览 ---------------- */
async function browseDirs(path) {
  const box = $('dirPicker');
  let res;
  try {
    res = await api('/api/local/browse?path=' + encodeURIComponent(path || '/'));
  } catch (err) {
    box.hidden = false;
    box.innerHTML = '<span class="chip bad">' + escapeHtml(err.message) + '</span>';
    return;
  }
  box.hidden = false;
  box.innerHTML = '';
  const base = res.path.replace(/\/+$/, '');

  const head = document.createElement('span');
  head.className = 'dir-head';
  head.textContent = base || '/';
  box.appendChild(head);

  const up = document.createElement('button');
  up.type = 'button';
  up.className = 'btn small ghost';
  up.textContent = '..';
  up.addEventListener('click', () => browseDirs(base.replace(/\/[^/]*$/, '') || '/'));
  box.appendChild(up);

  (res.dirs || []).forEach((name) => {
    const full = (base === '' ? '' : base) + '/' + name;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn small';
    button.textContent = name;
    button.addEventListener('click', () => {
      $('fLocal').value = full;
      browseDirs(full);
    });
    box.appendChild(button);
  });

  const pick = document.createElement('button');
  pick.type = 'button';
  pick.className = 'btn small primary';
  pick.textContent = '选择当前目录';
  pick.addEventListener('click', () => {
    $('fLocal').value = base || '/';
    box.hidden = true;
  });
  box.appendChild(pick);
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
  $('btnNewRule').addEventListener('click', () => openRuleModal(null));
  $('btnCloseModal').addEventListener('click', () => {
    $('ruleModal').hidden = true;
    $('dirPicker').hidden = true;
  });
  $('btnSaveRule').addEventListener('click', saveRule);
  $('fScheduleType').addEventListener('change', syncScheduleFields);
  $('btnBrowse').addEventListener('click', () => {
    const current = $('fLocal').value.trim();
    browseDirs(current && current !== '/' ? current.replace(/\/[^/]*$/, '') || '/' : '/');
  });
  $('btnSaveAccount').addEventListener('click', () => saveAccount().catch((e) => toast(e.message, true)));
  $('btnRefresh').addEventListener('click', refresh);
  $('btnRecheckAcl').addEventListener('click', checkAcl);

  $('btnQrLogin').addEventListener('click', startQrLogin);
  $('btnQrRefresh').addEventListener('click', startQrLogin);

  loadConfig().catch(() => {});
  refresh();
  setInterval(refresh, 4000);
  setTimeout(checkAcl, 800);
});
