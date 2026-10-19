
var AUTH_TOKEN = "__AUTH_TOKEN__";
var REFRESH_OPTIONS = [[60,'1 分钟'],[300,'5 分钟'],[600,'10 分钟'],[900,'15 分钟'],[1800,'30 分钟']];
var refreshSec = 300;
var countdown = refreshSec;
var fetchBusy = false;   // 防止慢查询期间定时器 / 手动刷新并发发起，多个请求互相覆盖渲染
var _updateUrl = null;   // 设置页加载时写入：发现新版本时的 release 页地址
var _lastWarning = null; // 去重：同一条配置告警不重复弹

// ---------- Toast 通知 ----------
function showToast(msg, type, ms) {
  type = type || 'info';
  ms = ms || (type === 'error' ? 5000 : 3000);
  var c = document.getElementById('toastContainer');
  if (!c) return;
  var t = document.createElement('div');
  t.className = 'toast toast-' + type;
  // 先转义再换行：<br> 由我们插入，不会被子串注入
  var safe = escHtml(msg).replace(/\n/g, '<br>');
  t.innerHTML = '<span class="toast-msg">' + safe + '</span>' +
                '<button class="toast-close" aria-label="关闭">×</button>';
  t.querySelector('.toast-close').onclick = function() { dismiss(); };
  c.appendChild(t);
  function dismiss() { t.classList.add('toast-out'); setTimeout(function() { t.remove(); }, 200); }
  setTimeout(dismiss, ms);
}

// ---------- 按钮 busy 态 ----------
function btnBusy(btn, busyText) {
  if (!btn) return function() {};
  var orig = btn.textContent;
  var origDisabled = btn.disabled;
  btn.disabled = true;
  if (busyText) btn.textContent = busyText;
  return function() { btn.disabled = origDisabled; btn.textContent = orig; };
}

// 左侧菜单切换
function showPage(name) {
  var pages = document.querySelectorAll('.page');
  for (var i = 0; i < pages.length; i++) pages[i].classList.remove('active');
  var target = document.getElementById('page-' + name);
  if (target) { target.classList.add('active'); }
  var items = document.querySelectorAll('.nav-item');
  for (var j = 0; j < items.length; j++) {
    var isActive = items[j].getAttribute('data-page') === name;
    items[j].classList.toggle('active', isActive);
    if (isActive) items[j].setAttribute('aria-current', 'page');
    else items[j].removeAttribute('aria-current');
  }
  // 重置滚动位置：切页后不应停留在上一页的滚动位置
  var main = document.querySelector('.main');
  if (main) main.scrollTop = 0;
  if (name === 'settings') loadSettings();
  if (name === 'apikeys') loadApiKeys();
  if (name === 'imagegen') loadImageAccounts();
}

// 统一请求封装：自动附带会话令牌；鉴权失效时给出明确提示
function apiFetch(url, options) {
  options = options || {};
  var headers = options.headers || {};
  headers['X-Auth-Token'] = AUTH_TOKEN;
  options.headers = headers;
  return fetch(url, options).then(function(resp) {
    if (resp.status === 401) { throw new Error('鉴权失效，请重启应用'); }
    return resp;
  });
}

function fmt(n) {
  n = parseFloat(n) || 0;
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return Math.round(n).toString();
}

function buildGauge(id, pct, usedStr, totalStr, remainStr, color, title, detail, resetTs) {
  var r = 54;
  var c = 2 * Math.PI * r;
  var offset = c * (1 - Math.min(pct, 100) / 100);
  var refreshHtml = '';
  if (resetTs && resetTs > 0) {
    var dt = new Date(resetTs * 1000);
    var dateStr = (dt.getMonth()+1) + '月' + dt.getDate() + '日 ' +
      (dt.getHours()<10?'0':'') + dt.getHours() + ':' +
      (dt.getMinutes()<10?'0':'') + dt.getMinutes();
    var now = new Date();
    var diffMs = dt - now;
    var countdownStr = '';
    if (diffMs > 0) {
      var diffMin = Math.floor(diffMs / 60000);
      if (diffMin >= 60) {
        var h = Math.floor(diffMin / 60);
        var m = diffMin % 60;
        countdownStr = h + '小时' + (m > 0 ? m + '分' : '') + '后';
      } else {
        countdownStr = diffMin + '分钟后';
      }
    } else {
      countdownStr = '已到刷新时间';
    }
    refreshHtml = '<div class="gauge-next-refresh">' +
      '<span class="nr-time">下次刷新: ' + dateStr + '</span>' +
      '<span class="nr-countdown">（' + countdownStr + '）</span>' +
    '</div>';
  }
  // 圆环内只放百分比，已用/剩余移到下方 gauge-detail，避免文字超出环内径
  return '<div class="gauge-wrap">' +
    '<div class="gauge-title">' + title + '</div>' +
    '<div class="gauge">' +
      '<svg width="130" height="130">' +
        '<circle class="bg-ring" cx="65" cy="65" r="' + r + '"></circle>' +
        '<circle class="fg-ring" cx="65" cy="65" r="' + r + '" stroke="' + color + '" stroke-dasharray="' + c + '" stroke-dashoffset="' + c + '" id="' + id + '"></circle>' +
      '</svg>' +
      '<div class="center">' +
        '<div class="pct">' + pct.toFixed(1) + '<span class="unit">%</span></div>' +
        '<div class="gauge-remain">剩余 ' + remainStr + '</div>' +
      '</div>' +
    '</div>' +
    '<div class="gauge-detail">' + detail + ' · 已用 ' + usedStr + ' / ' + totalStr + '</div>' +
    refreshHtml +
  '</div>';
}

function colorForPct(pct) {
  if (pct < 60) return '#52c41a';
  if (pct < 85) return '#ad6800';
  return '#f5222d';
}

function addAccountRow(username, hasPassword) {
  username = username || '';
  var container = document.getElementById('accountRows');
  var div = document.createElement('div');
  div.className = 'config-row';
  div.dataset.hasPassword = hasPassword ? '1' : '';
  div.dataset.clearPass = '';
  div.dataset.origUser = username || '';
  div.innerHTML = '<div class="col-user"><input type="text" placeholder="用户名" value="' + escHtml(username) + '"></div>' +
    '<div class="col-pass"><input type="password" placeholder="' + (hasPassword ? '留空沿用已保存密码' : '密码') + '" value=""></div>' +
    '<div class="col-actions"><button class="btn btn-sm btn-primary" data-action="loginAccount">登录</button>' +
    '<button class="btn btn-sm" data-action="clearPass" title="保存时清除已保存的密码" style="color:#ad6800;display:none">清密码</button>' +
    '<button class="btn btn-sm" data-action="removeRow" style="color:#f5222d">删</button></div>';
  container.appendChild(div);
  updateClearBtn(div);
}

// 「清密码」按钮可见性：仅当该行有已保存密码、未在输入新密码、且未标记清除时显示
function updateClearBtn(row) {
  var cp = row.querySelector('[data-action="clearPass"]');
  if (!cp) return;
  var hasPwd = row.dataset.hasPassword === '1';
  var typing = !!row.querySelector('.col-pass input').value;
  var marked = row.dataset.clearPass === '1';
  cp.style.display = (hasPwd && !typing && !marked) ? '' : 'none';
}

// 标记本行在保存时清除已保存的密码（不立即生效，需点「保存配置」）
function clearPass(btn) {
  var row = btn.closest('.config-row');
  var passInput = row.querySelector('.col-pass input');
  passInput.value = '';
  passInput.placeholder = '保存后清除已保存密码';
  row.dataset.clearPass = '1';
  updateClearBtn(row);
}

function removeRow(btn) { btn.closest('.config-row').remove(); }

function escHtml(s) { if (s === undefined || s === null) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function collectAccounts() {
  var rows = document.querySelectorAll('#accountRows .config-row');
  var accounts = [];
  rows.forEach(function(row) {
    var username = row.querySelector('.col-user input').value.trim();
    if (!username) return;
    var password = row.querySelector('.col-pass input').value;
    var item = {username: username};
    // 密码留空表示沿用已保存的密码（服务端不会覆盖）；不提交 jwt_token
    if (password) {
      item.password = password;
    } else if (row.dataset.clearPass === '1') {
      // 已标记清除：保存时让服务端把该账号已存密码置空
      item.password_clear = true;
    }
    accounts.push(item);
  });
  return accounts;
}

async function loginAccount(btn) {
  var row = btn.closest('.config-row');
  var username = row.querySelector('.col-user input').value.trim();
  var password = row.querySelector('.col-pass input').value;
  if (!username || !password) { showToast('请先填写用户名和密码', 'warning'); return; }
  // 不用 btnBusy：登录成功后要保留「已登录」状态，不能被无条件恢复
  btn.disabled = true; btn.textContent = '登录中...';
  try {
    var resp = await apiFetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:username, password:password})});
    var data = await resp.json();
    if (data.success) {
      var passInput = row.querySelector('.col-pass input');
      passInput.value = '';
      passInput.placeholder = '留空沿用已保存密码';
      row.dataset.hasPassword = '1';
      row.dataset.clearPass = '';
      updateClearBtn(row);
      btn.textContent = '已登录'; btn.style.color = '#52c41a';
      showToast('登录成功', 'success');
      await fetchUsage();
    } else { btn.textContent = '失败'; btn.style.color = '#f5222d'; showToast('登录失败: ' + (data.error || '未知'), 'error'); }
  } catch(e) { btn.textContent = '失败'; btn.style.color = '#f5222d'; }
  finally { setTimeout(function() {
    btn.disabled = false;
    // 成功保留「已登录」，其他状态 3 秒后恢复
    if (btn.textContent !== '已登录') { btn.textContent = '登录'; btn.style.color = ''; }
  }, 3000); }
}

async function saveConfig() {
  var accounts = collectAccounts();
  if (accounts.length === 0 && !confirm('当前没有任何账号，保存将清空已配置的账号，确定继续？')) return;
  // 重复用户名：服务端只保留第一条，后一条的密码会被静默丢弃
  var seen = {}, dupes = [];
  accounts.forEach(function(a) {
    if (seen[a.username] && dupes.indexOf(a.username) < 0) dupes.push(a.username);
    seen[a.username] = 1;
  });
  if (dupes.length) { showToast('账号名重复，重复项会被丢弃：\n' + dupes.join('、'), 'warning'); return; }
  // 改用户名 = 删旧建新：旧用户名下的已存密码不会随改名迁移，需二次确认避免静默丢失
  var renamed = [];
  document.querySelectorAll('#accountRows .config-row').forEach(function(row) {
    var user = row.querySelector('.col-user input').value.trim();
    var pass = row.querySelector('.col-pass input').value;
    var orig = row.dataset.origUser || '';
    if (user && user !== orig && row.dataset.hasPassword === '1' && !pass) renamed.push(orig);
  });
  if (renamed.length && !confirm('「' + renamed.join('、') + '」的账号名被改动，已保存的密码不会迁移到新用户名的账号，需重新登录。确定继续？')) return;
  var saveBtn = document.getElementById('saveConfigBtn');
  var done = btnBusy(saveBtn, '保存中...');
  try {
    var resp = await apiFetch('/api/accounts', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({accounts: accounts})});
    if (!resp.ok) {
      var ej = await resp.json().catch(function() { return {}; });
      throw new Error(ej.error || ('HTTP ' + resp.status));
    }
    var data = await resp.json();
    if (data.success) { var s = document.getElementById('saveStatus'); s.classList.add('show'); setTimeout(function() { s.classList.remove('show'); }, 2000); await loadConfigRows(); fetchUsage(); }
    else { showToast('保存失败: ' + (data.error || ''), 'error'); }
  } catch(e) { showToast('保存失败: ' + e.message, 'error'); }
  finally { done(); }
}

async function loadConfigRows() {
  var container = document.getElementById('accountRows');
  try {
    var resp = await apiFetch('/api/accounts');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    container.innerHTML = '';
    if (data.accounts && data.accounts.length > 0) data.accounts.forEach(function(a) { addAccountRow(a.username||'', a.has_password); });
    else addAccountRow();
  } catch(e) {
    container.innerHTML = '<div class="tip tip-error">加载账号失败: ' + escHtml(e.message) + '</div>';
  }
}

async function fetchUsage() {
  if (fetchBusy) return;   // 已有在途查询，跳过本次（手动刷新 / 定时刷新 / 登录后刷新共用）
  fetchBusy = true;
  var btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = '查询中...';
  document.getElementById('refreshInfo').textContent = '查询中...';
  try {
    var resp = await apiFetch('/api/usage');
    // 非 200（如频控 429）时 error 体没有 accounts 字段，不拦会被当成空数据渲染
    if (!resp.ok) {
      var ej = await resp.json().catch(function() { return {}; });
      throw new Error(ej.error || ('HTTP ' + resp.status));
    }
    var data = await resp.json();
    renderOverview(data);
    renderDetail(data);
    countdown = refreshSec;
  } catch(e) {
    document.getElementById('loading').style.display = 'none';
    // 失败后不必等满整个刷新间隔才重试，但也不要每秒重试
    countdown = Math.min(countdown, 30);
    renderCountdown();
    document.getElementById('overviewBody').innerHTML =
      '<div class="tip tip-error">查询失败: ' + escHtml(e.message) + '</div>';
    document.getElementById('detailBody').innerHTML = '';
  } finally { fetchBusy = false; btn.disabled = false; btn.textContent = '刷新'; }
}

function bar(pct) {
  var cls = pct < 60 ? 'green' : (pct < 85 ? 'yellow' : 'red');
  return '<span class="bar"><span class="bar-fill ' + cls + '" style="display:block;width:' +
    Math.min(pct, 100) + '%"></span></span>';
}

// 带千分位的数字串转数值
function toNum(s) { return parseFloat(String(s === undefined || s === null ? '' : s).replace(/,/g, '')) || 0; }

// 按 pool_type 定位积分池；类型缺失时退化为按名称关键字匹配
function findPool(items, type, nameKw) {
  var byName = null;
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    if (it.pool_type === type) return it;
    if (!byName && nameKw && String(it.pool_name || '').indexOf(nameKw) >= 0) byName = it;
  }
  return byName;
}

// 用量列：进度条 + 使用率；该积分池不存在时显示 -
function pctCell(item, which) {
  if (!item) return '<td style="color:#bbb">-</td>';
  var pct = parseFloat(which === '5h' ? item.usage_pct_5h : item.usage_pct_7d) || 0;
  return '<td>' + bar(pct) + escHtml(pct.toFixed(1)) + '%</td>';
}

// 活动固定积分列（grant_balance）
function grantCell(item) {
  if (!item) return '<td style="color:#bbb">-</td>';
  return '<td>' + escHtml(item.grant_balance) + '</td>';
}

// 5 小时窗口长度，用于换算「距下次刷新」的进度条比例
var RESET_WINDOW_SEC = 5 * 3600;

// 账号列：账号名 + （若有）当前套餐名 + 可用性状态徽标
function nameCell(r) {
  var s = escHtml(r.name || '未知');
  var meta = r.status ? _STATUS_META[r.status] : null;
  if (meta) s += '<span class="badge ' + meta.cls + '" style="margin-left:6px">' + meta.text + '</span>';
  if (r.plan) s += '<span class="plan-name">' + escHtml(r.plan) + '</span>';
  return '<td>' + s + '</td>';
}

// 明细页账号标题旁的套餐徽标
function planBadge(a) {
  return a.plan ? '<span class="badge badge-plan">' + escHtml(a.plan) + '</span>' : '';
}

// 距下次刷新列：进度条 + 刷新时间（优先取通用积分池，其次专属积分池）
function resetCell(def, ded) {
  var ts = ((def && def.reset_at_ts) || (ded && ded.reset_at_ts) || 0);
  if (!ts) return '<td style="color:#bbb">-</td>';
  var when = (def && def.reset_at) || (ded && ded.reset_at) || '';
  return '<td class="refresh-cell" data-reset="' + ts + '">' +
         '<span class="bar"><span class="bar-fill blue" style="display:block;width:100%"></span></span>' +
         escHtml(when) + '</td>';
}

// 每秒刷新所有「距下次刷新」进度条（时间文案固定，无需更新）
function updateResetCells() {
  var now = Math.floor(Date.now() / 1000);
  var cells = document.querySelectorAll('.refresh-cell');
  for (var i = 0; i < cells.length; i++) {
    var left = (parseInt(cells[i].getAttribute('data-reset'), 10) || 0) - now;
    var pct = Math.max(0, Math.min(100, left / RESET_WINDOW_SEC * 100));
    var fill = cells[i].querySelector('.bar-fill');
    if (fill) fill.style.width = pct.toFixed(1) + '%';
  }
}

function animateRings(rings) {
  setTimeout(function() {
    var c = 2 * Math.PI * 54;
    rings.forEach(function(pair) {
      var el = document.getElementById(pair[0]);
      if (el) el.setAttribute('stroke-dashoffset', c * (1 - Math.min(pair[1], 100) / 100));
    });
  }, 100);
}

// 账号可用性：基于积分池额度判断 可用 / 降级 / 不可用
// 可用  ：通用积分池 5h/7d 任一未满，或虽已满但活动固定积分 > 0 可兜底
// 降级  ：通用积分池耗尽，但 Flash-Lite 专属池 5h/7d 均未满（仍有额度）
// 不可用：通用池与专属池均耗尽（或无积分池数据）
function accountStatus(def, ded) {
  if (def) {
    var g5 = parseFloat(def.usage_pct_5h) || 0;
    var g7 = parseFloat(def.usage_pct_7d) || 0;
    if (g5 < 100 && g7 < 100) return 'available';
    if (toNum(def.grant_balance) > 0) return 'available';
  }
  if (ded) {
    var d5 = parseFloat(ded.usage_pct_5h) || 0;
    var d7 = parseFloat(ded.usage_pct_7d) || 0;
    if (d5 >= 100 || d7 >= 100) return 'unavailable';
    return 'degraded';
  }
  return 'unavailable';
}

var _STATUS_META = {
  available:   {cls: 'badge-ok',   text: '可用'},
  degraded:    {cls: 'badge-warn', text: '降级'},
  unavailable: {cls: 'badge-err',  text: '不可用'}
};

// ---------- 账号总览 ----------
function renderOverview(data) {
  document.getElementById('loading').style.display = 'none';
  var accounts = data.accounts || [];
  // 配置告警：只在内容变化时弹一次 toast，避免每次自动刷新都打断
  if (data.warning && data.warning !== _lastWarning) {
    _lastWarning = data.warning;
    showToast('配置提示：' + data.warning, 'warning', 8000);
  } else if (!data.warning) {
    _lastWarning = null;
  }

  var total5h = 0, used5h = 0, remain5h = 0;
  var total7d = 0, used7d = 0, remain7d = 0;
  var dTotal5h = 0, dUsed5h = 0, dRemain5h = 0;
  var dTotal7d = 0, dUsed7d = 0, dRemain7d = 0;
  var availCount = 0, degradCount = 0, unavailCount = 0, defCount = 0, dedCount = 0;
  var earliest5hReset = 0, earliest7dReset = 0;
  var earliest5hResetD = 0, earliest7dResetD = 0;
  var rows = [];

  accounts.forEach(function(a) {
    if (a.error) { unavailCount++; rows.push({name: a.username, err: a.error}); return; }
    var items = a.items || [];
    var def = findPool(items, 'default', '通用');
    var ded = findPool(items, 'dedicated', 'Flash-Lite');
    var st = accountStatus(def, ded);
    if (st === 'available') availCount++;
    else if (st === 'degraded') degradCount++;
    else unavailCount++;

    // 圆环合计：通用积分池与 Flash-Lite 专属积分池分别累加
    if (def) {
      defCount++;
      total5h += toNum(def.limit_5h); used5h += toNum(def.used_5h); remain5h += toNum(def.remain_5h);
      total7d += toNum(def.limit_7d); used7d += toNum(def.used_7d); remain7d += toNum(def.remain_7d);
      var t5 = def.reset_at_ts || 0;
      if (t5 > 0 && (earliest5hReset === 0 || t5 < earliest5hReset)) earliest5hReset = t5;
      var t7 = def.reset_at_7d_ts || 0;
      if (t7 > 0 && (earliest7dReset === 0 || t7 < earliest7dReset)) earliest7dReset = t7;
    }
    if (ded) {
      dedCount++;
      dTotal5h += toNum(ded.limit_5h); dUsed5h += toNum(ded.used_5h); dRemain5h += toNum(ded.remain_5h);
      dTotal7d += toNum(ded.limit_7d); dUsed7d += toNum(ded.used_7d); dRemain7d += toNum(ded.remain_7d);
      var dt5 = ded.reset_at_ts || 0;
      if (dt5 > 0 && (earliest5hResetD === 0 || dt5 < earliest5hResetD)) earliest5hResetD = dt5;
      var dt7 = ded.reset_at_7d_ts || 0;
      if (dt7 > 0 && (earliest7dResetD === 0 || dt7 < earliest7dResetD)) earliest7dResetD = dt7;
    }
    rows.push({name: a.username, def: def, ded: ded, plan: a.plan, status: st});
  });

  var pct5h = total5h > 0 ? used5h / total5h * 100 : 0;
  var pct7d = total7d > 0 ? used7d / total7d * 100 : 0;
  var pct5hD = dTotal5h > 0 ? dUsed5h / dTotal5h * 100 : 0;
  var pct7dD = dTotal7d > 0 ? dUsed7d / dTotal7d * 100 : 0;

  var h = '';
  h += '<div class="summary">';
  h += card('账号总数', String(accounts.length));
  h += card('可用', String(availCount), availCount > 0 ? '#52c41a' : '');
  h += card('降级', String(degradCount), degradCount > 0 ? '#ad6800' : '');
  h += card('不可用', String(unavailCount), unavailCount > 0 ? '#f5222d' : '');
  h += card('更新时间', (data.timestamp || '').slice(11) || '-');
  h += '</div>';

  h += '<div class="gauge-section">';
  h += buildGauge('ring5h', pct5h, fmt(used5h), fmt(total5h), fmt(remain5h), colorForPct(pct5h),
                  '通用积分池-5h', defCount + ' 个账号合计', earliest5hReset);
  h += buildGauge('ring7d', pct7d, fmt(used7d), fmt(total7d), fmt(remain7d), colorForPct(pct7d),
                  '通用积分池-7d', defCount + ' 个账号合计', earliest7dReset);
  h += buildGauge('ring5hD', pct5hD, fmt(dUsed5h), fmt(dTotal5h), fmt(dRemain5h), colorForPct(pct5hD),
                  'Flash-Lite专属积分池-5h', dedCount + ' 个账号合计', earliest5hResetD);
  h += buildGauge('ring7dD', pct7dD, fmt(dUsed7d), fmt(dTotal7d), fmt(dRemain7d), colorForPct(pct7dD),
                  'Flash-Lite专属积分池-7d', dedCount + ' 个账号合计', earliest7dResetD);
  h += '</div>';

  h += '<div class="account-card"><div class="account-header"><span class="name">各账号使用情况</span>' +
       '<span class="badge badge-ok">按积分池分类</span></div><table>';
  h += '<tr><th>账号</th>' +
       '<th class="th-wrap">通用积分池-5h</th>' +
       '<th class="th-wrap">通用积分池-7d</th>' +
       '<th class="th-wrap">通用积分池-活动固定积分</th>' +
       '<th class="th-wrap">Flash-Lite专属积分池-5h</th>' +
       '<th class="th-wrap">Flash-Lite专属积分池-7d</th>' +
       '<th class="th-wrap">距下次刷新</th></tr>';
  if (!rows.length) {
    h += '<tr><td colspan="7" style="color:#999">还没有配置任何账号，请到「账号配置」页添加</td></tr>';
  }
  rows.forEach(function(r) {
    if (r.err) {
      // 查询失败时无刷新时间可显示，改在该列给出失败原因（避免整行看不出问题）
      var errCls = (r.err.indexOf('过期') >= 0 || r.err.indexOf('未配置') >= 0) ? 'badge-warn' : 'badge-err';
      h += '<tr>' + nameCell(r) +
           '<td style="color:#bbb">-</td><td style="color:#bbb">-</td><td style="color:#bbb">-</td>' +
           '<td style="color:#bbb">-</td><td style="color:#bbb">-</td>' +
           '<td><span class="badge ' + errCls + '">' + escHtml(r.err) + '</span></td></tr>';
    } else {
      h += '<tr>' + nameCell(r) +
           pctCell(r.def, '5h') + pctCell(r.def, '7d') + grantCell(r.def) +
           pctCell(r.ded, '5h') + pctCell(r.ded, '7d') +
           resetCell(r.def, r.ded) + '</tr>';
    }
  });
  h += '</table></div>';

  document.getElementById('overviewBody').innerHTML = h;
  updateResetCells();
  animateRings([['ring5h', pct5h], ['ring7d', pct7d], ['ring5hD', pct5hD], ['ring7dD', pct7dD]]);
}

// ---------- 账号明细 ----------
function renderDetail(data) {
  var accounts = data.accounts || [];
  var errCount = 0;
  accounts.forEach(function(a) { if (a.error) errCount++; });

  var h = '';
  if (errCount > 0) h += '<div class="tip">部分账号 JWT 可能已过期，请到「账号配置」页重新登录</div>';
  if (!accounts.length) h += '<div class="tip">还没有配置任何账号，请到「账号配置」页添加</div>';

  // 使用率配色（文字色，区别于进度条的背景色）
  function pctColor(v) { return v < 60 ? '#52c41a' : (v < 85 ? '#ad6800' : '#f5222d'); }
  function barCls(v) { return v < 60 ? 'green' : (v < 85 ? 'yellow' : 'red'); }

  h += '<div class="detail-grid">';
  accounts.forEach(function(a) {
    var head = '<div class="acct-head"><span class="acct-name">' + escHtml(a.username || '未知') + '</span>' + planBadge(a);
    if (a.error) {
      var cls = (a.error.indexOf('过期')>=0 || a.error.indexOf('未配置')>=0) ? 'badge-warn' : 'badge-err';
      h += '<div class="acct-card">' + head + '<span class="badge ' + cls + '">' + escHtml(a.error) + '</span></div></div>';
      return;
    }
    if (!a.items.length) {
      h += '<div class="acct-card">' + head + '<span class="badge badge-ok">无资源包</span></div></div>';
      return;
    }
    h += '<div class="acct-card">' + head + '<span class="badge badge-ok">' + a.items.length + ' 个积分池</span></div>';
    h += '<div class="acct-body">';
    a.items.forEach(function(item) {
      var pct = parseFloat(item.usage_pct_5h) || 0;
      var pct7 = parseFloat(item.usage_pct_7d) || 0;
      var tagCls = item.pool_type==='default'?'tag-default':'tag-dedicated';
      var tagText = item.pool_type==='default'?'通用':'专属';
      var grantText = escHtml(item.grant_balance);
      if (item.nearest_expiry) grantText += ' (至' + escHtml(item.nearest_expiry) + ')';
      h += '<div class="acct-pool">';
      h += '<div class="acct-pool-top"><span class="tag '+tagCls+'">'+tagText+'</span>'+escHtml(item.pool_name)+'</div>';
      h += '<div class="acct-metric"><span class="acct-k">5h</span><span class="pool-bar"><span class="pool-bar-fill '+barCls(pct)+'" style="width:'+Math.min(pct,100)+'%"></span></span><span class="acct-pct" style="color:'+pctColor(pct)+'">'+pct.toFixed(1)+'%</span></div>';
      h += '<div class="acct-sub">已用 '+escHtml(item.used_5h)+' / '+escHtml(item.limit_5h)+' · 剩 '+escHtml(item.remain_5h)+'</div>';
      h += '<div class="acct-metric"><span class="acct-k">7d</span><span class="pool-bar"><span class="pool-bar-fill '+barCls(pct7)+'" style="width:'+Math.min(pct7,100)+'%"></span></span><span class="acct-pct" style="color:'+pctColor(pct7)+'">'+pct7.toFixed(1)+'%</span></div>';
      h += '<div class="acct-sub">已用 '+escHtml(item.used_7d)+' / '+escHtml(item.limit_7d)+' · 剩 '+escHtml(item.remain_7d)+'</div>';
      h += '<div class="acct-sub">赠送余额 '+grantText+'</div>';
      h += '<div class="acct-sub">刷新 '+escHtml(item.reset_at)+' / '+escHtml(item.reset_at_7d)+'</div>';
      h += '</div>';
    });
    h += '</div></div>';
  });
  h += '</div>';
  document.getElementById('detailBody').innerHTML = h;
}

// ---------- API Key 管理 ----------
// 按 user+id 复合键缓存明文，避免不同账号 id 撞号时串明文；仅存活于当前页面内存
var keyPlainCache = {};
// /api/keys 结果缓存：60 秒内复用，避免每次进页/图像页取 Key 数都重拉
var _keysCache = null;
var _KEYS_CACHE_TTL = 60000;

async function fetchKeys(force) {
  if (!force && _keysCache && (Date.now() - _keysCache.ts) < _KEYS_CACHE_TTL) return _keysCache.data;
  var resp = await apiFetch('/api/keys');
  var data = await resp.json();
  if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
  _keysCache = {data: data, ts: Date.now()};
  keyPlainCache = {};  // 列表刷新后明文缓存失效
  return data;
}
// API Key 行内图标（Feather 风格线性 SVG，单色 currentColor）
var SVG_EYE = '<svg class="ki" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
var SVG_EYE_OFF = '<svg class="ki" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
var SVG_COPY = '<svg class="ki" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
var SVG_OK = '<svg class="ki" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
var SVG_ERR = '<svg class="ki" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

async function loadApiKeys(force) {
  var btn = document.getElementById('keysBtn');
  var body = document.getElementById('keysBody');
  if (btn) { btn.disabled = true; btn.textContent = '查询中...'; }
  if (!body.innerHTML.trim()) body.innerHTML = '<div class="tip">正在查询各账号的 API Key...</div>';
  try {
    var data = await fetchKeys(!!force);
    renderApiKeys(data);
  } catch (e) {
    body.innerHTML = '<div class="tip">查询失败：' + escHtml(e.message) + '</div>';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}

function renderApiKeys(data) {
  var accounts = data.accounts || [];
  var h = '';
  if (!accounts.length) h = '<div class="tip">还没有配置任何账号，请到「账号配置」页添加</div>';

  accounts.forEach(function(a) {
    var head = '<div class="account-header"><span class="name">' + escHtml(a.username || '未知') + '</span>';
    if (a.error) {
      var cls = (a.error.indexOf('过期') >= 0 || a.error.indexOf('未配置') >= 0) ? 'badge-warn' : 'badge-err';
      h += '<div class="account-card">' + head + '<span class="badge ' + cls + '">' + escHtml(a.error) + '</span></div></div>';
      return;
    }
    var keys = a.keys || [];
    h += '<div class="account-card">' + head + '<span class="badge badge-ok">' + keys.length + ' 个 Key</span></div>';
    if (!keys.length) { h += '<div class="empty-row">该账号暂无 API Key</div></div>'; return; }
    h += '<table><tr><th>名称</th><th class="col-key">API Key</th><th>创建时间</th><th>状态</th></tr>';
    keys.forEach(function(k) {
      var uid = escHtml(k.id || '');
      var user = escHtml(a.username || '');
      h += '<tr><td>' + escHtml(k.name || '-') +
           (k.is_default ? ' <span class="badge badge-plan">默认</span>' : '') + '</td>' +
           '<td class="col-key"><code class="key-mono" data-mid="' + uid + '">' + escHtml(k.masked || '-') + '</code>' +
             '<button class="key-icon" data-act="toggle" data-id="' + uid + '" data-user="' + user + '" title="显示/隐藏">' + SVG_EYE + '</button>' +
             '<button class="key-icon" data-act="copy" data-id="' + uid + '" data-user="' + user + '" title="复制">' + SVG_COPY + '</button></td>' +
           '<td>' + escHtml(k.create_time || '-') + '</td>' +
           '<td>' + (k.status === 'enabled' ? '启用' : escHtml(k.status || '-')) + '</td></tr>';
    });
    h += '</table></div>';
  });
  document.getElementById('keysBody').innerHTML = h;
}

// 复制：优先异步剪贴板 API，被拒（如窗口未聚焦）时退回 execCommand
function legacyCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(function() {
      if (!legacyCopy(text)) throw new Error('剪贴板不可用');
    });
  }
  if (!legacyCopy(text)) return Promise.reject(new Error('剪贴板不可用'));
  return Promise.resolve();
}

// 按需取明文：只在首次点「显示 / 复制」时请求一次，之后用内存缓存
async function fetchKeyPlain(id, user) {
  var ck = (user || '') + '\x00' + (id || '');   // 复合键：账号 + key id
  if (keyPlainCache[ck]) return keyPlainCache[ck];
  var resp = await apiFetch('/api/apikey?username=' + encodeURIComponent(user || '') +
                            '&id=' + encodeURIComponent(id));
  var d = await resp.json();
  if (!resp.ok || !d.api_key) throw new Error(d.error || '获取明文失败');
  keyPlainCache[ck] = d.api_key;
  return d.api_key;
}

async function onKeyAction(ev) {
  var btn = ev.target.closest ? ev.target.closest('[data-act]') : null;
  if (!btn) return;
  var id = btn.getAttribute('data-id');
  var user = btn.getAttribute('data-user');
  var act = btn.getAttribute('data-act');
  var code = btn.parentElement.querySelector('code.key-mono');
  var restore = btn.innerHTML;
  try {
    var plain = await fetchKeyPlain(id, user);
    if (act === 'copy') {
      await copyText(plain);
      btn.innerHTML = SVG_OK;
      setTimeout(function() { btn.innerHTML = SVG_COPY; }, 1500);
      return;
    }
    if (code.getAttribute('data-shown') === '1') {
      code.textContent = code.getAttribute('data-masked');
      code.setAttribute('data-shown', '0');
      btn.innerHTML = SVG_EYE;
    } else {
      if (!code.getAttribute('data-masked')) code.setAttribute('data-masked', code.textContent);
      code.textContent = plain;
      code.setAttribute('data-shown', '1');
      btn.innerHTML = SVG_EYE_OFF;
    }
  } catch (e) {
    btn.innerHTML = SVG_ERR;
    setTimeout(function() { btn.innerHTML = restore; }, 1500);
  }
}

// ---------- 图像生成 ----------
var _igImage = null;  // {b64, fmt, dlId, dataUri} 当前查看的图片，供下载使用
var _igAbort = null;  // 生成请求的 AbortController，供「取消」用

async function loadImageAccounts() {
  var sel = document.getElementById('igAccount');
  if (!sel) return;
  // 取各账号 Key 数（复用 /api/keys 缓存），失败不阻塞账号加载
  var keyCounts = {};  // username -> {count} 或 {err:true}
  try {
    var kd = await fetchKeys(false);
    (kd.accounts || []).forEach(function(a) {
      if (a.error) keyCounts[a.username || ''] = {err: true};
      else keyCounts[a.username || ''] = {count: (a.keys || []).length};
    });
  } catch (e) { /* ignore */ }
  try {
    var resp = await apiFetch('/api/accounts');
    var data = await resp.json();
    var acctList = data.accounts || [];
    var prev = sel.value;
    sel.innerHTML = '';
    if (!acctList.length) {
      sel.innerHTML = '<option value="">（无账号）</option>';
      return;
    }
    acctList.forEach(function(a) {
      var opt = document.createElement('option');
      var uname = a.username || '';
      opt.value = uname;
      var label = uname || '未知';
      var kc = keyCounts[uname];
      if (kc && kc.err) label += '（Key 查询失败）';
      else if (kc) label += kc.count > 0 ? (' · ' + kc.count + ' Key') : '（无 Key）';
      opt.textContent = label;
      sel.appendChild(opt);
    });
    if (prev && Array.prototype.some.call(sel.options, function(o) { return o.value === prev; })) {
      sel.value = prev;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">（加载失败）</option>';
  }
}

var _igMode = 't2i';   // t2i 图片生成 / i2i 图片编辑
var _igFiles = [];     // [{dataUrl, name}] 上传的参考图（data URL，第 1 张为主图）

function setIgMode(mode) {
  _igMode = mode;
  var tabs = document.querySelectorAll('.ig-tab');
  for (var i = 0; i < tabs.length; i++) {
    var isActive = tabs[i].getAttribute('data-mode') === mode;
    tabs[i].classList.toggle('active', isActive);
    tabs[i].setAttribute('aria-selected', isActive ? 'true' : 'false');
  }
  document.getElementById('igUploadRow').style.display = (mode === 'i2i') ? '' : 'none';
  var sizeSel = document.getElementById('igSize');
  // auto 选项常驻：图片编辑启用，图片生成禁用并回退显式尺寸
  var autoOpt = sizeSel.querySelector('option[value="auto"]');
  if (autoOpt) autoOpt.disabled = (mode !== 'i2i');
  if (mode === 't2i' && sizeSel.value === 'auto') sizeSel.value = '2048x2048';
  else if (mode === 'i2i') sizeSel.value = 'auto';
  var btn = document.getElementById('igGenBtn');
  btn.textContent = (mode === 'i2i') ? '编辑图像' : '生成图像';
  var label = document.getElementById('igPromptLabel');
  label.textContent = (mode === 'i2i') ? '编辑指令' : '提示词';
  var ta = document.getElementById('igPrompt');
  ta.placeholder = (mode === 'i2i')
    ? '描述期望的修改，例如：把背景换成冰川，保持人物不变'
    : '例如：一只橘猫趴在书桌上，窗外的阳光，水彩画风格，温暖色调';
}

var MAX_REF_IMAGE_SIDE = 2048;                 // 参考图最长边，超出等比缩放
var MAX_REF_IMAGE_BYTES = 20 * 1024 * 1024;    // 单张上限，更大会先把整图解码进内存
var REF_JPEG_QUALITY = 0.9;

function readFileAsDataURL(file) {
  return new Promise(function(resolve, reject) {
    var fr = new FileReader();
    fr.onload = function() { resolve(fr.result); };
    fr.onerror = function() { reject(fr.error); };
    fr.readAsDataURL(file);
  });
}

// 5 张参考图以 base64 一起 POST，服务端 body 上限 32MB。手机原图 40MB → base64 53MB
// 会直接撞上限，只回一句「请求体过大」。这里解码后按需缩小：4K 以上细节对出图没有帮助，
// 而缩到 2048 转 JPEG 后单张约 1.5MB，5 张也稳在预算内。
function normalizeRefImage(file) {
  return new Promise(function(resolve) {
    readFileAsDataURL(file).then(function(dataUrl) {
      var img = new Image();
      img.onload = function() {
        var side = Math.max(img.width, img.height);
        var scale = Math.min(1, MAX_REF_IMAGE_SIDE / side);
        if (scale >= 1) { resolve(dataUrl); return; }
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff';                          // 先垫白底，透明 PNG 不糊成黑块
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', REF_JPEG_QUALITY));
      };
      img.onerror = function() { resolve(dataUrl); };    // 解码失败就原样交出去，由后端判定
      img.src = dataUrl;
    }).catch(function() { resolve(null); });
  });
}

async function addIgFiles(fileList) {
  var arr = Array.prototype.slice.call(fileList || []);
  if (!arr.length) return;
  // 仅接受图片
  arr = arr.filter(function(f) { return f.type && f.type.indexOf('image/') === 0; });
  if (!arr.length) { showToast('请选择图片文件', 'warning'); return; }
  var tooBig = arr.filter(function(f) { return f.size > MAX_REF_IMAGE_BYTES; });
  if (tooBig.length) {
    showToast('以下文件超过 ' + Math.round(MAX_REF_IMAGE_BYTES / 1024 / 1024) +
              'MB，已跳过：' + tooBig.map(function(f) { return f.name; }).join('、'), 'warning');
    return;
  }
  var before = _igFiles.length;
  for (var i = 0; i < arr.length && _igFiles.length < 5; i++) {
    try {
      var dataUrl = await normalizeRefImage(arr[i]);
      if (dataUrl) _igFiles.push({dataUrl: dataUrl, name: arr[i].name});
    } catch (e) { /* 单个文件读取失败跳过 */ }
  }
  var added = _igFiles.length - before;
  if (added < arr.length) {
    showToast('参考图最多 5 张，本次仅添加了 ' + added + ' 张', 'warning');
  }
  renderIgThumbs();
}

function renderIgThumbs() {
  var box = document.getElementById('igThumbs');
  box.innerHTML = '';
  _igFiles.forEach(function(f, idx) {
    var div = document.createElement('div');
    div.className = 'ig-thumb' + (idx === 0 ? ' ig-thumb-main' : '');
    div.innerHTML = '<img src="' + f.dataUrl + '" alt="参考图">' +
      (idx === 0 ? '<span class="ig-thumb-tag">主图</span>' : '') +
      '<button type="button" class="ig-thumb-rm" data-idx="' + idx + '" title="移除">×</button>';
    box.appendChild(div);
  });
}

function removeIgFile(idx) {
  _igFiles.splice(idx, 1);
  renderIgThumbs();
}

async function generateImage() {
  var username = document.getElementById('igAccount').value;
  var prompt = document.getElementById('igPrompt').value.trim();
  var size = document.getElementById('igSize').value;
  var model = document.getElementById('igModel').value;
  var promptExtend = document.getElementById('igPromptExtend').checked;
  var watermark = document.getElementById('igWatermark').checked;
  if (!username) { showToast('请先在「账号配置」页配置账号，并确保该账号有 API Key', 'warning'); return; }
  if (!prompt) { showToast('请输入' + (_igMode === 'i2i' ? '编辑指令' : '提示词'), 'warning'); return; }

  var endpoint = '/api/imagegen';
  var body = {username: username, prompt: prompt, size: size, model: model,
              prompt_extend: promptExtend, watermark: watermark};
  if (_igMode === 'i2i') {
    if (!_igFiles.length) { showToast('请至少上传一张参考图', 'warning'); return; }
    endpoint = '/api/imageedit';
    body.images = _igFiles.map(function(f) { return f.dataUrl; });
  }

  var btn = document.getElementById('igGenBtn');
  var cancelBtn = document.getElementById('igCancelBtn');
  var status = document.getElementById('igStatus');
  var result = document.getElementById('igResult');
  btn.disabled = true; btn.textContent = '生成中...';
  cancelBtn.style.display = '';
  status.className = 'ig-status working';
  status.textContent = '正在' + (_igMode === 'i2i' ? '编辑' : '生成') + '图像，约需 10–30 秒，请耐心等待...';
  result.innerHTML = '<div class="ig-empty"><div class="spinner"></div><p style="margin-top:10px">生成中...</p></div>';

  _igAbort = new AbortController();
  try {
    var resp = await apiFetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: _igAbort.signal
    });
    var data = await resp.json();
    if (!resp.ok || !data.b64) throw new Error(data.error || '生成失败');
    var fmt = data.output_format || 'png';
    var dataUri = 'data:image/' + fmt + ';base64,' + data.b64;
    _igImage = {fmt: fmt, dlId: data.dl_id, dataUri: dataUri};
    showCurrentImage(dataUri, fmt, data.size);
    pushHistory(_igImage);
    status.className = 'ig-status';
    status.textContent = '生成成功';
  } catch (e) {
    status.className = 'ig-status';
    if (e && e.name === 'AbortError') {
      status.textContent = '';
      result.innerHTML = '<div class="ig-empty">已取消</div>';
    } else {
      status.textContent = '';
      result.innerHTML = '<div class="tip tip-error">生成失败：' + escHtml(e.message) + '</div>';
    }
  } finally {
    _igAbort = null;
    btn.disabled = false;
    btn.textContent = (_igMode === 'i2i') ? '编辑图像' : '生成图像';
    cancelBtn.style.display = 'none';
  }
}

function cancelImage() {
  if (_igAbort) _igAbort.abort();
}

function showCurrentImage(dataUri, fmt, size) {
  var result = document.getElementById('igResult');
  result.innerHTML = '<img src="' + dataUri + '" alt="生成结果">' +
    '<div class="ig-actions"><button class="btn btn-sm btn-primary" data-action="downloadImage">下载图片</button>' +
    '<span class="ig-hint">' + escHtml(size || '') + ' · ' + (fmt || 'png').toUpperCase() + '</span></div>';
}

var _igHistory = [];  // [{fmt, dlId, dataUri}]  不存 b64：dataUri 已含同一份数据，存两份会翻倍内存

function pushHistory(item) {
  if (!item) return;
  _igHistory.unshift({fmt: item.fmt, dlId: item.dlId, dataUri: item.dataUri});
  if (_igHistory.length > 20) _igHistory.length = 20;
  renderIgHistory();
}

function renderIgHistory() {
  var box = document.getElementById('igHistory');
  if (!box) return;
  box.innerHTML = '';
  _igHistory.forEach(function(h, idx) {
    var div = document.createElement('div');
    div.className = 'ig-hist-item';
    div.title = '点击查看 · 下载';
    div.innerHTML = '<img src="' + h.dataUri + '" alt="历史">' +
      '<button type="button" class="ig-hist-rm" data-idx="' + idx + '" title="移除">×</button>';
    div.addEventListener('click', function(ev) {
      if (ev.target.closest('.ig-hist-rm')) return;
      _igImage = {fmt: h.fmt, dlId: h.dlId, dataUri: h.dataUri};
      showCurrentImage(h.dataUri, h.fmt, '');
    });
    box.appendChild(div);
  });
}

document.getElementById('igHistory').addEventListener('click', function(ev) {
  var rm = ev.target.closest('.ig-hist-rm');
  if (!rm) return;
  ev.stopPropagation();
  var idx = parseInt(rm.getAttribute('data-idx'), 10) || 0;
  _igHistory.splice(idx, 1);
  renderIgHistory();
});

async function downloadImage() {
  if (!_igImage || !_igImage.dlId) { showToast('图片不可用，请重新生成', 'warning'); return; }
  var dlId = _igImage.dlId;
  var fmt = _igImage.fmt || 'png';
  // 优先走 pywebview JS-Python 桥接：原生保存对话框，最可靠
  if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.save_image === 'function') {
    try {
      var r = await window.pywebview.api.save_image(dlId);
      if (r && r.ok) { flashDownloadBtn('已保存'); return; }
      if (r && r.cancelled) { return; }  // 用户取消，不打扰
      showToast('保存失败：' + ((r && r.error) || '未知错误'), 'error');
    } catch (e) {
      showToast('保存失败：' + (e && e.message ? e.message : '桥接调用异常'), 'error');
    }
    return;
  }
  // 回退：同源 fetch 取回字节再用 Blob 触发保存。
  // 不用锚点跳转 /api/imagedl?t=<令牌>，避免会话令牌出现在 URL（浏览器历史 / Referer）。
  try {
    var resp = await apiFetch('/api/imagedl?id=' + encodeURIComponent(dlId));
    if (!resp.ok) { showToast('图片已过期或不存在，请重新生成', 'warning'); return; }
    var url = URL.createObjectURL(await resp.blob());
    var a = document.createElement('a');
    a.href = url;
    a.download = 'sensenova_image.' + fmt;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 10000);
    flashDownloadBtn('已保存');
  } catch (e) {
    showToast('保存失败：' + (e && e.message ? e.message : '下载异常'), 'error');
  }
}

function flashDownloadBtn(text) {
  var btn = document.querySelector('#igResult .ig-actions .btn-primary');
  if (!btn) return;
  var orig = btn.textContent;
  btn.textContent = text;
  setTimeout(function() { btn.textContent = orig; }, 1500);
}

// ---------- 系统设置 ----------
// 用系统浏览器打开 release 页（优先走 pywebview 桥接，回退到 window.open）
async function openUpdateUrl(url) {
  if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.open_url === 'function') {
    try { var r = await window.pywebview.api.open_url(url); if (r && r.ok) return; } catch (e) { /* 回退 */ }
  }
  window.open(url, '_blank');
}

async function loadSettings() {
  var box = document.getElementById('settingsBody');
  box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    var resp = await apiFetch('/api/settings');
    if (!resp.ok) {
      var ej = await resp.json().catch(function() { return {}; });
      throw new Error(ej.error || ('HTTP ' + resp.status));
    }
    var d = await resp.json();
    refreshSec = (d.settings && d.settings.refresh_seconds) || 300;
    countdown = refreshSec;

    var opts = '';
    REFRESH_OPTIONS.forEach(function(o) {
      opts += '<option value="' + o[0] + '"' + (o[0] === refreshSec ? ' selected' : '') + '>' + o[1] + '</option>';
    });
    // 服务端接受 60–3600 秒，早于当前版本保存的取值可能不在列表里；
    // 补一个 option 而不是丢弃，否则下拉显示为空、下次保存又被重置成 300
    if (!REFRESH_OPTIONS.some(function(o) { return o[0] === refreshSec; })) {
      opts += '<option value="' + refreshSec + '" selected>' +
              (refreshSec % 60 === 0 ? (refreshSec / 60) + ' 分钟' : refreshSec + ' 秒') + '</option>';
    }

    var h = '';
    h += '<div class="settings-card"><h3 class="settings-title">常规</h3>';
    h += '<div class="settings-row"><span class="k">自动刷新</span><select id="setRefresh">' + opts + '</select>' +
         '<button class="btn btn-sm btn-primary" data-action="saveSettings">保存</button>' +
         '<span class="save-status" id="settingsStatus">已保存</span></div>';
    h += '<div class="settings-row"><span class="k">配置目录</span><span class="v">' + escHtml(d.config_dir) + '</span></div>';
    h += '<div class="settings-row"><span class="k">数据来源</span><span class="v">' + escHtml(d.api_base) + '</span></div>';
    h += '<div class="settings-row"><span class="k">本地服务</span><span class="v">127.0.0.1:' + escHtml(d.port) + '</span></div>';
    h += '</div>';

    h += '<div class="settings-card"><h3 class="settings-title">安全状态</h3><ul class="sec-list">' +
         '<li><span class="ok">✔</span>凭据加密存储（AES + 系统密钥保护，非明文落盘）</li>' +
         '<li><span class="ok">✔</span>界面不回传密码与 JWT 明文</li>' +
         '<li><span class="ok">✔</span>本地服务端口每次启动随机分配</li>' +
         '<li><span class="ok">✔</span>Host / Origin / 跨站来源校验</li>' +
         '<li><span class="ok">✔</span>一次性会话令牌（仅拦截浏览器来源）</li>' +
         '</ul></div>';

    var updateRow = '';
    if (d.update) {
      _updateUrl = d.update.url || '';
      updateRow = '<div class="settings-row"><span class="k">更新</span>' +
        '<span class="v" style="color:#ad6800">发现新版本 v' + escHtml(d.update.latest || '') +
        '</span> <button class="btn btn-sm btn-primary" data-action="openUpdate">去下载</button></div>';
    }
    h += '<div class="settings-card"><h3 class="settings-title">关于</h3>' +
         '<div class="settings-row"><span class="k">版本</span><span class="v">' + escHtml(d.version) + '</span></div>' +
         updateRow +
         '<div class="settings-row"><span class="k">说明</span><span class="v">仅在本机运行，凭据不上传；JWT 过期后会用保存的账号密码自动重登。</span></div></div>';

    box.innerHTML = h;
  } catch(e) {
    box.innerHTML = '<div class="tip tip-error">加载失败: ' + escHtml(e.message) + '</div>';
  }
}

async function saveSettings() {
  var sel = document.getElementById('setRefresh');
  var val = parseInt(sel.value, 10) || 300;
  var saveBtn = sel.parentElement.querySelector('[data-action="saveSettings"]');
  var done = btnBusy(saveBtn, '保存中...');
  try {
    var resp = await apiFetch('/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({refresh_seconds: val})
    });
    var d = await resp.json();
    if (d.success) {
      refreshSec = d.settings.refresh_seconds;
      countdown = refreshSec;
      renderCountdown();
      var s = document.getElementById('settingsStatus');
      if (s) { s.classList.add('show'); setTimeout(function() { s.classList.remove('show'); }, 2000); }
      showToast('设置已保存', 'success');
    } else { showToast('保存失败: ' + (d.error || ''), 'error'); }
  } catch(e) { showToast('保存失败: ' + e.message, 'error'); }
  finally { done(); }
}

function card(label, value, color) { return '<div class="summary-card"><div class="label">'+escHtml(label)+'</div><div class="value" style="'+(color?'color:'+color:'')+'">'+escHtml(value)+'</div></div>'; }

function renderCountdown() {
  var m = Math.floor(countdown/60); var s = countdown%60;
  document.getElementById('refreshInfo').textContent = m+':'+(s<10?'0':'')+s+' 后自动刷新';
}

function tick() {
  updateResetCells();
  countdown--;
  if (countdown <= 0) {
    // 先复位再发请求：查询较慢（如多账号自动重登）时在途期间 countdown 会持续为负，
    // 不复位的话每秒都会再发一次 /api/usage，多个请求互相覆盖渲染结果
    countdown = refreshSec;
    fetchUsage();
    return;
  }
  renderCountdown();
}

// API Key 列表的「显示 / 复制」用事件委托，避免每次重绘都要重新绑定
document.getElementById('keysBody').addEventListener('click', onKeyAction);

// 图像生成页：上传区点击选择 / 拖拽放入 / 缩略图删除（事件委托）
(function initIgUpload() {
  var dz = document.getElementById('igDropzone');
  var input = document.getElementById('igFileInput');
  var thumbs = document.getElementById('igThumbs');
  if (!dz || !input) return;
  dz.addEventListener('click', function() { input.click(); });
  input.addEventListener('change', function() { addIgFiles(input.files); input.value = ''; });
  ['dragenter', 'dragover'].forEach(function(ev) {
    dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach(function(ev) {
    dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.remove('dragover'); });
  });
  dz.addEventListener('drop', function(e) {
    if (e.dataTransfer && e.dataTransfer.files) addIgFiles(e.dataTransfer.files);
  });
  thumbs.addEventListener('click', function(e) {
    var rm = e.target.closest ? e.target.closest('.ig-thumb-rm') : null;
    if (!rm) return;
    removeIgFile(parseInt(rm.getAttribute('data-idx'), 10) || 0);
  });
})();

// 账号配置：在密码框输入新密码时，撤销此前点「清密码」设的清除标记
document.getElementById('accountRows').addEventListener('input', function(ev) {
  if (!ev.target.matches || !ev.target.matches('.col-pass input')) return;
  var row = ev.target.closest('.config-row');
  if (!row || !ev.target.value) return;
  if (row.dataset.clearPass === '1') {
    row.dataset.clearPass = '';
    row.querySelector('.col-pass input').placeholder =
      row.dataset.hasPassword === '1' ? '留空沿用已保存密码' : '密码';
    updateClearBtn(row);
  }
});

// 全局点击委托：所有 [data-action] 按钮统一分发，避免内联 onclick（CSP 可去 'unsafe-inline'）
document.addEventListener('click', function(ev) {
  var el = ev.target.closest ? ev.target.closest('[data-action]') : null;
  if (!el) return;
  switch (el.getAttribute('data-action')) {
    case 'showPage': showPage(el.getAttribute('data-page') || ''); break;
    case 'fetchUsage': fetchUsage(); break;
    case 'addAccountRow': addAccountRow(); break;
    case 'saveConfig': saveConfig(); break;
    case 'loginAccount': loginAccount(el); break;
    case 'removeRow': removeRow(el); break;
    case 'clearPass': clearPass(el); break;
    case 'loadApiKeys': loadApiKeys(true); break;
    case 'setIgMode': setIgMode(el.getAttribute('data-mode') || 't2i'); break;
    case 'generateImage': generateImage(); break;
    case 'cancelImage': cancelImage(); break;
    case 'downloadImage': downloadImage(); break;
    case 'saveSettings': saveSettings(); break;
    case 'openUpdate': if (_updateUrl) openUpdateUrl(_updateUrl); break;
  }
});

// ---------- 窗口控制（无边框窗口） ----------
function initWindowControls() {
  var minBtn = document.getElementById('winMinBtn');
  var maxBtn = document.getElementById('winMaxBtn');
  var closeBtn = document.getElementById('winCloseBtn');
  var dragRegion = document.querySelector('.titlebar-drag');

  function callApi(method) {
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[method] === 'function') {
      return window.pywebview.api[method]();
    }
    return Promise.resolve();
  }

  if (minBtn) minBtn.addEventListener('click', function() { callApi('minimize_window'); });
  if (closeBtn) closeBtn.addEventListener('click', function() { callApi('close_window'); });

  if (maxBtn) {
    maxBtn.addEventListener('click', function() {
      callApi('toggle_maximize_window').then(function(isMax) {
        if (isMax === undefined) return;
        maxBtn.innerHTML = isMax ? '&#x2750;' : '&#x25A1;';  // ❐ restore / □ maximize
        maxBtn.setAttribute('title', isMax ? '还原' : '最大化');
      });
    });
  }

  // 双击标题栏切换最大化（桌面应用标准交互）
  if (dragRegion && maxBtn) {
    dragRegion.addEventListener('dblclick', function() {
      maxBtn.click();
    });
  }
}

initWindowControls();

loadSettings().then(function() { return loadConfigRows(); }).then(function() { fetchUsage(); });
setInterval(tick, 1000);
