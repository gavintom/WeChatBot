/* 个人微信智能客服 · 管理后台 SPA
   无框架、无构建：单文件 IIFE，菜单 data-view ↔ section#view-xxx ↔ loadXxx()。 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }
  function $$(sel, root) {
    var scope = root || document;
    if (!scope || typeof scope.querySelectorAll !== 'function') return [];
    return Array.prototype.slice.call(scope.querySelectorAll(sel));
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET',
                 headers: { 'X-Requested-With': 'XMLHttpRequest' } };
    if (opts.body !== undefined) {
      if (opts.body instanceof FormData) { init.body = opts.body; }
      else {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
    }
    return fetch(path, init).then(function (r) {
      if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; })
        .then(function (d) {
          if (!d.ok && d.error) { var e = new Error(d.error); e.status = r.status; throw e; }
          return d;
        });
    });
  }
  /* 结果提示：正常结果停留 6s，警告 / 错误 8s（改备注这类提示文案较长，
     3.6s 经常没读完就消失）。点一下可立即关掉。 */
  function toast(msg, kind) {
    var el = document.createElement('div');
    el.className = 'toast ' + (kind === 'err' ? 'err' : (kind === 'warn' ? 'warn' : ''));
    el.textContent = msg;
    el.title = '点击关闭';
    var ms = (kind === 'err' || kind === 'warn') ? 8000 : 6000;
    var timer = setTimeout(function () { el.remove(); }, ms);
    el.addEventListener('click', function () { clearTimeout(timer); el.remove(); });
    $('toastWrap').appendChild(el);
  }
  function fail(e) { toast(e.message || String(e), 'err'); }
  function listStr(v) { return (v || []).join('、'); }
  function arrText(v) { return (v || []).join('\n'); }
  function kv(rows) {
    return rows.map(function (r) {
      return '<div class="kv-row"><span>' + esc(r[0]) + '</span><strong>' + esc(r[1]) + '</strong></div>';
    }).join('');
  }
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' 字节';
  }
  function emptyRow(cols, msg) {
    return '<tr><td colspan="' + cols + '"><div class="empty">' + esc(msg) + '</div></td></tr>';
  }
  function tag(ok, yes, no) {
    return '<span class="tag ' + (ok ? 'tag-reply' : 'tag-fail') + '">' + esc(ok ? yes : no) + '</span>';
  }
  function badge(text) { return '<span class="chip">' + esc(text) + '</span>'; }

  var OPTIONS = { policies: [], docs: [], groups: [] };
  // 每个列表记住总页数：下一页只能翻到 maxPage，避免无限翻到空页
  var recPage = 1, recLimit = 30, recMaxPage = 1;
  var llmPage = 1, llmLimit = 30, llmMaxPage = 1, chartLlm = null;
  var apiPage = 1, apiLimit = 30, apiMaxPage = 1, chartApi = null;
  // 统一渲染分页：后端已把 page 钳制到总页数内，这里回写页码并禁用首尾按钮
  function renderPager(infoId, prevId, nextId, d) {
    var limit = d.limit || 30;
    var maxPage = Math.max(1, d.max_page || Math.ceil((d.total || 0) / limit) || 1);
    var p = Math.max(1, Math.min(d.page || 1, maxPage));
    $(infoId).textContent = '第 ' + p + ' 页 / 共 ' + maxPage + ' 页';
    $(prevId).disabled = p <= 1;
    $(nextId).disabled = p >= maxPage;
    return { page: p, maxPage: maxPage };
  }
  var kbCurrent = '', kbDirty = false;
  var chartDaily = null;
  var chartDays = 30;
  var doneChart = null, transChart = null;
  var apiCallChart = null, apiTokenChart = null, apiLatencyChart = null;
  var currentView = 'dashboard';
  // 二级菜单分组：一级只负责展开/收起，真正的视图仍由 data-view 子项切换
  var NAV_GROUPS = {
    logs: ['records', 'chatlog', 'media', 'apicalls', 'llm', 'logs'],
    kb: ['policies', 'knowledge'],
    system: ['groups', 'private', 'config', 'models']
  };
  var VIEW_GROUP = {};
  Object.keys(NAV_GROUPS).forEach(function (g) {
    NAV_GROUPS[g].forEach(function (v) { VIEW_GROUP[v] = g; });
  });
  function expandNavGroup(g, on) {
    $$('.nav-parent[data-group-toggle="' + g + '"]').forEach(function (n) {
      n.classList.toggle('open', on);
    });
    $$('.nav-sub[data-sub="' + g + '"]').forEach(function (s) {
      s.classList.toggle('hidden', !on);
    });
  }
  var VIEW_META = {
    dashboard: ['运行监控', '主服务、手机插件与今日消息统计'],
    records: ['对话记录', '按日期、场景、结果查询与导出'],
    chatlog: ['聊天记录', '插件收到的原始消息归档，按系统配置保留 1~12 个月'],
    media: ['媒体文件', '群聊/私聊的图片与文件，按群名/wxid 检索，支持全部与分页'],
    llm: ['AI 用量', '大模型调用记录、token 与费用统计'],
    models: ['AI 大模型', '群聊 / 私聊分场景选模型，设置默认模型'],
    apicalls: ['API 记录', '知识库 HTTP 接口的每一次调用（含失败）'],
    groups: ['群聊配置', '只有配置表内的群才会响应'],
    private: ['私聊配置', '唤醒词、白名单、黑名单与知识库配置'],
    policies: ['知识库管理', '每个知识库包含哪些文档'],
    contacts: ['通讯录管理', '群聊 / 好友，群成员可导出'],
    knowledge: ['知识库文档', '文档编辑、导入、索引重建与问答测试'],
    config: ['系统配置', 'config.yaml 在线编辑（带备份回滚）'],
    logs: ['运行日志', '主服务日志尾部'],
    system: ['系统信息', '运行环境与依赖检查'],
    plugins: ['插件管理', '插件清单、定时任务实例与扩展路由，配置热生效'],
    moments: ['朋友圈', '按时间线 / 按好友查看动态，展示图片、视频、点赞与评论']
  };

  /* ---------------- 移动端导航抽屉（>900px 完全不启用） ----------------
     侧栏在窄屏是 fixed 抽屉，遮罩 z-index 高于普通弹窗，所以任何「切页面 /
     开弹窗」的动作都要先收起抽屉，否则弹窗会被遮罩盖住。 */
  function isNarrow() {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      try { return window.matchMedia('(max-width: 900px)').matches; } catch (e) {}
    }
    var w = (typeof window !== 'undefined' && window.innerWidth) || 0;
    return !!w && w <= 900;
  }
  function sidebarEl() { return $$('.sidebar')[0] || null; }
  function setDrawer(open) {
    var sb = sidebarEl();
    if (!sb || !sb.classList) return;
    if (sb.classList.toggle) sb.classList.toggle('open', !!open);
    var mask = $('sidebarMask');
    if (mask && mask.classList && mask.classList.toggle) mask.classList.toggle('show', !!open);
    if (document.body && document.body.classList && document.body.classList.toggle) {
      document.body.classList.toggle('drawer-open', !!open);
    }
    var btn = $('btnMenu');
    if (btn && typeof btn.setAttribute === 'function') {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }
  function collapseDrawer() { if (isNarrow()) setDrawer(false); }
  /* 朋友圈页头固定的高度要扣掉顶栏真实高度：不同手机的缩放 / 安全区
     会让顶栏高度不一样，量一次写进 CSS 变量，比在 CSS 里写死 58px 准。 */
  function syncMomViewportH() {
    var bar = document.querySelector ? document.querySelector('.topbar') : null;
    var root = document.documentElement;
    if (!bar || !bar.getBoundingClientRect || !root || !root.style ||
        typeof root.style.setProperty !== 'function') return;
    var h = Math.round(bar.getBoundingClientRect().height) || 58;
    root.style.setProperty('--mom-topbar-h', h + 'px');
  }
  /* 页面模板若还是浏览器旧缓存（没有菜单按钮 / 遮罩），运行时补上，
     否则用户会拿到新 CSS 却打不开抽屉。 */
  function ensureMobileChrome() {
    if (typeof document.createElement !== 'function' || !document.body) return;
    var bar = document.querySelector ? document.querySelector('.topbar') : null;
    if (!$('btnMenu') && bar && typeof bar.insertBefore === 'function') {
      var btn = document.createElement('button');
      btn.id = 'btnMenu'; btn.className = 'menu-toggle'; btn.type = 'button';
      if (typeof btn.setAttribute === 'function') btn.setAttribute('aria-label', '打开导航菜单');
      btn.innerHTML = '<span></span><span></span><span></span>';
      bar.insertBefore(btn, bar.firstChild);
    }
    if (!$('sidebarMask') && typeof document.body.appendChild === 'function') {
      var mask = document.createElement('div');
      mask.id = 'sidebarMask'; mask.className = 'sidebar-mask';
      document.body.appendChild(mask);
    }
  }
  /* 列多的表格在手机上仍靠容器内横向滚动查看，插入一次性滑动提示 */
  function enhanceMobileTables() {
    if (!isNarrow() || typeof document.createElement !== 'function') return;
    $$('.table-wrap, .chat-scroll').forEach(function (w) {
      if (w.dataset && w.dataset.scrollHint) return;
      if (w.dataset) w.dataset.scrollHint = '1';
      var hint = document.createElement('div');
      hint.className = 'scroll-hint';
      hint.textContent = '← 左右滑动查看完整表格 →';
      var parent = w.parentNode;
      if (!parent || typeof parent.insertBefore !== 'function') return;
      parent.insertBefore(hint, w);
      if (typeof w.addEventListener !== 'function') return;
      var done = false;
      w.addEventListener('scroll', function () {
        if (done) return;
        done = true;
        if (hint.classList && hint.classList.add) hint.classList.add('hidden');
      });
    });
  }

  var pendingView = null;
  function switchView(name, force) {
    if (!VIEW_META[name]) return;
    collapseDrawer();               // 切页先收起抽屉（含「未保存离开」确认前）
    if (!force && currentView === 'knowledge' && name !== 'knowledge' && kbDirty) {
      pendingView = name;
      return askUnsavedLeave();
    }
    currentView = name;
    $$('.nav-item[data-view]').forEach(function (n) {
      n.classList.toggle('active', n.dataset.view === name);
    });
    // 切到分组内的视图时自动展开该组并高亮父级；不强制收起用户手动展开的其它组
    var g = VIEW_GROUP[name] || '';
    $$('.nav-parent').forEach(function (n) {
      n.classList.toggle('parent-active', !!g && n.dataset.groupToggle === g);
    });
    if (g) expandNavGroup(g, true);
    $$('.view').forEach(function (v) { v.classList.add('hidden'); });
    $('view-' + name).classList.remove('hidden');
    $('viewTitle').textContent = VIEW_META[name][0];
    $('viewSub').textContent = VIEW_META[name][1];
    $('lastUpdate').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN');
    if (name === 'moments') syncMomViewportH();
    var L = {
      dashboard: loadDashboard, records: loadRecords, chatlog: loadChatLog,
      media: loadMedia, groups: loadGroups,
      private: loadPrivate, policies: loadPolicies, contacts: loadContacts,
      knowledge: loadKnowledge,
      config: function () { loadConfig(false); loadFollowup(); },
      logs: loadLog, system: loadSystem,
      llm: loadLlm, apicalls: loadApi, models: loadLlmModels,
      plugins: loadPlugins, moments: loadMoments
    };
    if (name === 'groups' || name === 'private' || name === 'policies' || name === 'knowledge') {
      refreshOptions().then(function () { L[name](); });
    } else { L[name](); }
    // 群聊 / 私聊配置页顶部的「应答模型」下拉：进页面就拉一次最新场景配置
    var picker = { groups: ['group', 'grpSceneModel', 'grpSceneModelEff'],
                   private: ['private', 'prvSceneModel', 'prvSceneModelEff'] }[name];
    if (picker) loadScenePicker(picker[0], picker[1], picker[2]);
  }
  function refreshOptions() {
    return api('/api/options').then(function (d) {
      OPTIONS.policies = d.policies || []; OPTIONS.docs = d.docs || [];
      OPTIONS.groups = d.groups || [];
    }).catch(function () {});
  }
  function policyOptions() {
    return OPTIONS.policies.map(function (p) {
      return { value: p.id, label: p.name + '（' + p.doc_count + ' 篇）' };
    });
  }
  function rulePolicyOptions() {
    // 规则表单专用：允许「不查询知识库」，关闭知识库开关时下拉就落在这个空项
    return [{ value: '', label: '（不查询知识库）' }].concat(policyOptions());
  }
  function groupOptions() {
    return OPTIONS.groups.map(function (g) {
      return { value: g.wxid, label: (g.name || g.wxid) + ' · ' + g.wxid };
    });
  }

  /* 规则表单联动：关「知识库」→ 下拉禁用且清空；关「转人工」→ 人工接待禁用且清空。
     后端也会按开关把 policy_id / human_* 存空，前后端口径一致。 */
  function bindRuleFlagToggles(withHuman) {
    var kb = $('f_need_kb'), tr = $('f_need_transfer'), pol = $('f_policy_id');
    var human = withHuman ? ['f_human_wxid', 'f_human_name'] : [];
    var lastPolicy = pol ? pol.value : '';
    function apply() {
      var kbOn = !kb || kb.checked;
      if (pol) {
        pol.disabled = !kbOn;
        if (!kbOn) {
          if (pol.value) lastPolicy = pol.value;  // 误关再打开时恢复上次选择
          pol.value = '';
        } else if (!pol.value && lastPolicy) {
          pol.value = lastPolicy;
        }
      }
      var trOn = !tr || tr.checked;
      human.forEach(function (id) {
        var el = $(id);
        if (!el) return;
        el.disabled = !trOn;
        if (!trOn) el.value = '';
      });
    }
    if (kb) kb.addEventListener('change', apply);
    if (tr) tr.addEventListener('change', apply);
    apply();
  }

  /* ------------------------------ 通用表单 / 详情模态 ------------------------------ */
  var formSave = null;
  function updateMultiCount(key) {
    var boxes = $$('[data-multi="' + key + '"]');
    var n = boxes.filter(function (x) { return x.checked; }).length;
    var el = $('multi_count_' + key);
    if (el) el.textContent = '已选 ' + n + ' / ' + boxes.length;
  }
  function setMultiAll(key, on) {
    $$('[data-multi="' + key + '"]').forEach(function (x) { x.checked = on; });
    updateMultiCount(key);
  }
  function openForm(title, fields, values, onSave, afterRender) {
    values = values || {}; formSave = onSave; window.__formFields = fields;
    $('formTitle').textContent = title;
    $('formError').classList.add('hidden');
    $('formFields').innerHTML = fields.map(function (f) {
      if (f.type === 'group') {
        return '<div class="form-group-title">' + esc(f.label) + '</div>';
      }
      var v = values[f.key];
      var id = 'f_' + f.key;
      var h = '<div class="field' + (f.compact ? ' field-compact' : '') + '">' +
        (f.label ? '<label>' + esc(f.label) + '</label>' : '');
      if (f.type === 'textarea') {
        h += '<textarea id="' + id + '" rows="' + (f.rows || 3) + '" placeholder="' + esc(f.placeholder || '') + '">' + esc(v || '') + '</textarea>';
      } else if (f.type === 'select') {
        h += '<select id="' + id + '">' + (f.options || []).map(function (o) {
          var val = (typeof o === 'object') ? o.value : o;
          var lab = (typeof o === 'object') ? o.label : o;
          return '<option value="' + esc(val) + '"' + (String(v) === String(val) ? ' selected' : '') + '>' + esc(lab) + '</option>';
        }).join('') + '</select>';
      } else if (f.type === 'multiselect') {
        var sel = {}; (v || []).forEach(function (x) { sel[x] = 1; });
        var opts = (f.options || []).slice();
        (v || []).forEach(function (x) { if (opts.indexOf(x) < 0) opts.push(x); });
        // 文件多了不能让几十个 checkbox 撑爆弹窗：固定高度 + 内部滚动，
        // 顶部给「已选 N / 共 M」与全选 / 清空，避免逐个数、逐个点。
        h += '<div class="check-list-head">' +
          '<span class="muted" id="multi_count_' + esc(f.key) + '">已选 ' +
          (v || []).length + ' / ' + opts.length + '</span>' +
          '<span class="row-gap">' +
          '<button type="button" class="link-btn" data-multi-all="' + esc(f.key) + '">全选</button>' +
          '<button type="button" class="link-btn" data-multi-none="' + esc(f.key) + '">清空</button>' +
          '</span></div>';
        h += '<div class="check-list" data-multi-box="' + esc(f.key) + '">' + (opts.length ? opts.map(function (o) {
          return '<label class="check-item"><input type="checkbox" data-multi="' + esc(f.key) + '" value="' + esc(o) + '"' +
            (sel[o] ? ' checked' : '') + '> ' + esc(o) + '</label>';
        }).join('') : '<span class="muted">暂无知识库文档，请先到「知识库」上传</span>') + '</div>';
      } else if (f.type === 'checkbox') {
        h += '<label class="check-item"><input type="checkbox" id="' + id + '"' + (v ? ' checked' : '') + '> ' + esc(f.checkLabel || '启用') + '</label>';
      } else if (f.type === 'static') {
        h += '<div class="small">' + esc(v || '') + '</div>';
      } else {
        h += '<input type="' + (f.type === 'number' ? 'number' : 'text') + '" id="' + id + '" value="' + esc(v === undefined || v === null ? '' : v) +
          '" placeholder="' + esc(f.placeholder || '') + '"' + (f.datalist ? ' list="dl_' + id + '"' : '') +
          (f.disabled ? ' disabled' : '') + '>';
        if (f.datalist) {
          h += '<datalist id="dl_' + id + '">' + (f.datalist || []).map(function (o) {
            var val = (typeof o === 'object') ? o.value : o;
            var lab = (typeof o === 'object') ? o.label : o;
            return '<option value="' + esc(val) + '">' + esc(lab) + '</option>';
          }).join('') + '</datalist>';
        }
      }
      if (f.help) h += '<div class="muted" style="margin-top:4px">' + esc(f.help) + '</div>';
      return h + '</div>';
    }).join('');
    $$('[data-multi-all]').forEach(function (b) {
      b.addEventListener('click', function () { setMultiAll(b.dataset.multiAll, true); });
    });
    $$('[data-multi-none]').forEach(function (b) {
      b.addEventListener('click', function () { setMultiAll(b.dataset.multiNone, false); });
    });
    $$('[data-multi-box] input[type=checkbox]').forEach(function (x) {
      x.addEventListener('change', function () { updateMultiCount(x.dataset.multi); });
    });
    $('formModal').classList.remove('hidden');
    setFormBusy(false);             // 上次提交可能留下「保存中…」，每次打开都复位
    if (afterRender) afterRender();
  }
  function collectForm(fields) {
    var out = {};
    fields.forEach(function (f) {
      var id = 'f_' + f.key;
      if (f.type === 'static' || f.type === 'group') return;
      if (f.type === 'multiselect') {
        out[f.key] = $$('[data-multi="' + f.key + '"]:checked').map(function (x) { return x.value; });
      } else if (f.type === 'checkbox') { out[f.key] = $(id).checked; }
      else { out[f.key] = $(id).value; }
    });
    return out;
  }
  function closeForm() { $('formModal').classList.add('hidden'); formSave = null; }
  /* 提交中把「保存」置灰并改成「保存中…」，避免连点造成重复写入手机微信；
     取消按钮一起禁掉，防止请求在途时把弹窗关掉又看不到失败原因。 */
  var FORM_SAVE_LABEL = '保存';
  function setFormBusy(busy) {
    var b = $('formSave');
    if (!b) return;
    b.disabled = !!busy;
    b.textContent = busy ? '保存中…' : FORM_SAVE_LABEL;
    if ($('formCancel')) $('formCancel').disabled = !!busy;
  }
  /* 居中确认框：替代原生 confirm（原生框贴顶显示、长文案也难读）。
     返回 Promise<boolean>；点取消 / 遮罩 / Esc 都是 false。 */
  var cfmResolve = null;
  function askConfirm(msg, opts) {
    opts = opts || {};
    if (cfmResolve) cfmResolve(false);          // 上一个没关就先当取消，避免挂住
    $('cfmTitle').textContent = opts.title || '请确认';
    $('cfmMsg').textContent = msg;
    $('cfmOk').textContent = opts.okText || '确定';
    $('cfmCancel').textContent = opts.cancelText || '取消';
    $('confirmModal').classList.remove('hidden');
    $('cfmOk').focus();
    return new Promise(function (resolve) {
      cfmResolve = function (v) {
        cfmResolve = null;
        $('confirmModal').classList.add('hidden');
        resolve(v);
      };
    });
  }
  function closeConfirm(v) { if (cfmResolve) cfmResolve(!!v); }
  function openDetail(title, html, extra) {
    $('detailTitle').textContent = title;
    $('detailBody').innerHTML = html;
    $('detailExtra').innerHTML = extra || '';
    $('detailModal').classList.remove('hidden');
    enhanceMobileTables();          // 详情里动态生成的宽表格也补上滑动提示
  }
  function closeDetail() { $('detailModal').classList.add('hidden'); }

  /* ------------------------------ 运行监控 ------------------------------ */
  var svcTimer = null, svcPoll = 0, svcBusy = false;
  function svcButtons(locked) {
    ['btnSvcStart', 'btnSvcRestart', 'btnSvcStop', 'btnSvcRefresh'].forEach(function (id) {
      $(id).disabled = !!locked;
    });
  }
  function loadService() {
    return api('/api/service').then(function (d) {
      var busy = svcBusy;
      var state = d.running ? '运行中' : ((d.starting || busy) ? '启动中…' : '已停止');
      // 忙状态下进程已退出、且连续轮询仍不在线 → 判定启动失败，恢复按钮
      if (busy && !d.running && !d.starting && svcPoll >= 4) {
        svcBusy = false; svcPoll = 0;
        $('svcState').textContent = '启动失败';
        svcButtons(false);
        toast('启动失败：主服务未就绪，请到「运行日志 → 管理端日志」查看启动输出', 'err');
        return d;
      }
      if (d.running) svcBusy = false;
      $('svcState').textContent = state;
      $('svcInfo').innerHTML = kv([
        ['状态', d.running ? '在线（' + d.source + '）' : state],
        ['PID', d.pid || '—'],
        ['启动方式', d.source],
        ['探活地址', (d.url || '') + '/healthz'],
        ['启动命令', d.command || '—']
      ]);
      if (svcBusy) {
        svcButtons(true);
      } else {
        $('btnSvcStart').disabled = !!d.running;
        $('btnSvcStop').disabled = !d.managed;
        $('btnSvcRestart').disabled = false;
        $('btnSvcRefresh').disabled = false;
      }
      if (svcTimer) clearTimeout(svcTimer);
      if ((d.starting || svcBusy) && svcPoll < 20) {
        svcPoll++;
        svcTimer = setTimeout(loadService, 2000);   // 初始化期间持续轮询
      } else {
        svcPoll = 0;
      }
      return d;
    }).catch(function (e) {
      if (svcBusy) { svcBusy = false; svcButtons(false); $('svcState').textContent = '启动失败'; }
      else { $('svcState').textContent = '状态未知'; }
      fail(e);
    });
  }
  function serviceAction(action) {
    // 点击瞬间就给反馈：显示「启动中」并灰掉全部按钮，不等接口返回
    if (action === 'start' || action === 'restart') {
      svcBusy = true; svcPoll = 0;
      $('svcState').textContent = '启动中…';
      svcButtons(true);
    } else if (action === 'stop') {
      svcBusy = false;
      $('svcState').textContent = '停止中…';
      svcButtons(true);
    }
    api('/api/service', { method: 'POST', body: { action: action } }).then(function (d) {
      toast(d.message || '已完成');
      if (action === 'stop') svcBusy = false;
      if (svcTimer) clearTimeout(svcTimer);
      setTimeout(loadDashboard, 400);   // loadDashboard() 内部已经会调 loadService()
    }).catch(function (e) {
      // 接口报错同样要恢复按钮，避免一直灰着
      svcBusy = false; svcPoll = 0;
      svcButtons(false);
      $('svcState').textContent = (action === 'stop') ? '停止失败' : '启动失败';
      fail(e);
      loadService();
    });
  }
  // ---- 运行监控图表（微信对话与知识库接口调用分开画，
  //      两者量级差几个数量级，叠一张图会把对话那条线压成贴底直线） ----
  var CHART_COMMON = {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(28,36,48,.94)', padding: 11, cornerRadius: 7,
        titleFont: { size: 12.5 }, bodyFont: { size: 12.5 }, displayColors: true,
        boxWidth: 8, boxHeight: 8, boxPadding: 4
      }
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: '#8b96a8', font: { size: 11 },
        maxRotation: 0, autoSkipPadding: 14 } },
      y: { beginAtZero: true, grid: { color: '#eef1f5' },
        ticks: { color: '#8b96a8', font: { size: 11 }, precision: 0 } }
    }
  };
  function shortDate(d) { return String(d || '').slice(5).replace('-', '/'); }
  function chartLegend() {
    return { display: true, position: 'top', align: 'end',
      labels: { color: '#5a6577', font: { size: 12 }, usePointStyle: true,
                pointStyle: 'circle', boxWidth: 7 } };
  }
  function destroyChart(c) { if (c) { c.destroy(); } }

  function renderCharts(series) {
    if (typeof Chart === 'undefined') return;
    var labels = (series || []).map(function (x) { return shortDate(x.day); });
    var reply = (series || []).map(function (x) { return x.replies || 0; });
    var trans = (series || []).map(function (x) { return x.transfers || 0; });
    destroyChart(chartDaily);
    chartDaily = new Chart($('chartDaily').getContext('2d'), {
      type: 'line',
      data: { labels: labels, datasets: [
        { label: 'AI 回复', data: reply, borderColor: '#157a4d',
          backgroundColor: 'rgba(21,122,77,.1)', borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: true },
        { label: '转人工', data: trans, borderColor: '#d98324',
          backgroundColor: 'rgba(217,131,36,.08)', borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: true }
      ] },
      options: Object.assign({}, CHART_COMMON, {
        plugins: Object.assign({}, CHART_COMMON.plugins, { legend: chartLegend() })
      })
    });
    var barOpts = Object.assign({}, CHART_COMMON, {
      plugins: Object.assign({}, CHART_COMMON.plugins, { legend: { display: false } })
    });
    destroyChart(doneChart);
    doneChart = new Chart($('doneChart').getContext('2d'), {
      type: 'bar',
      data: { labels: labels, datasets: [{ label: 'AI 回复', data: reply,
        backgroundColor: 'rgba(21,122,77,.78)', borderRadius: 3, maxBarThickness: 22 }] },
      options: barOpts
    });
    destroyChart(transChart);
    transChart = new Chart($('transChart').getContext('2d'), {
      type: 'bar',
      data: { labels: labels, datasets: [{ label: '转人工', data: trans,
        backgroundColor: 'rgba(217,131,36,.78)', borderRadius: 3, maxBarThickness: 22 }] },
      options: barOpts
    });
  }

  function renderApiCharts(daily) {
    if (typeof Chart === 'undefined') return;
    var rows = daily || [];
    var labels = rows.map(function (x) { return shortDate(x.day); });
    var calls = rows.map(function (x) { return x.calls || 0; });
    var fails = rows.map(function (x) {
      return typeof x.fail === 'number' ? x.fail : Math.max(0, (x.calls || 0) - (x.ok || 0));
    });
    var tokens = rows.map(function (x) { return x.total_tokens || 0; });
    var lat = rows.map(function (x) { return (x.avg_latency_ms || 0) / 1000; });
    destroyChart(apiCallChart);
    apiCallChart = new Chart($('apiCallChart').getContext('2d'), {
      type: 'line',
      data: { labels: labels, datasets: [
        { label: '调用量', data: calls, borderColor: '#2b6cf0',
          backgroundColor: 'rgba(43,108,240,.1)', borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: true },
        { label: '失败/被拒', data: fails, borderColor: '#d64545',
          backgroundColor: 'rgba(214,69,69,.08)', borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: true }
      ] },
      options: Object.assign({}, CHART_COMMON, {
        plugins: Object.assign({}, CHART_COMMON.plugins, { legend: chartLegend() })
      })
    });
    destroyChart(apiTokenChart);
    apiTokenChart = new Chart($('apiTokenChart').getContext('2d'), {
      type: 'bar',
      data: { labels: labels, datasets: [{ label: 'token', data: tokens,
        backgroundColor: 'rgba(43,108,240,.75)', borderRadius: 2, maxBarThickness: 20 }] },
      options: Object.assign({}, CHART_COMMON, {
        plugins: Object.assign({}, CHART_COMMON.plugins, { legend: { display: false } })
      })
    });
    destroyChart(apiLatencyChart);
    apiLatencyChart = new Chart($('apiLatencyChart').getContext('2d'), {
      type: 'line',
      data: { labels: labels, datasets: [{ label: '平均耗时(秒)', data: lat,
        borderColor: '#d98324', backgroundColor: 'rgba(217,131,36,.12)',
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: true }] },
      options: Object.assign({}, CHART_COMMON, {
        plugins: Object.assign({}, CHART_COMMON.plugins, { legend: chartLegend() })
      })
    });
  }

  function loadStats() {
    api('/api/stats?days=' + chartDays).then(function (d) {
      renderCharts(d.daily || []);
      renderApiCharts(d.api_daily || []);
    }).catch(fail);
  }

  /* ------------------------------ 知识库接口启停 ------------------------------ */
  function apiSvcButtons(busy) {
    ['btnApiStart', 'btnApiRestart', 'btnApiStop', 'btnApiRefresh'].forEach(function (id) {
      $(id).disabled = !!busy;
    });
  }
  function renderApiService(d) {
    var s = d.state || {};
    // 主服务是旧版本（缺少 /internal/api-control）或未启动时，后端会返回降级状态。
    // 这时不要把错误刷成 toast，直接把「怎么修」写进卡片里。
    if (d.degraded || s.available === false) {
      $('apiSvcState').textContent = '未就绪';
      $('apiSvcInfo').innerHTML = kv([
        ['状态', '主服务未就绪'],
        ['处理建议', s.hint || '请重启主服务']
      ]);
      apiSvcButtons(true);
      $('btnApiRefresh').disabled = false;
      return;
    }
    $('apiSvcState').textContent = s.enabled ? '运行中' : '已停止';
    $('apiSvcInfo').innerHTML = kv([
      ['状态', s.enabled ? '运行中' : '已停止'],
      ['路由', s.registered ? '/httpapi/query、/httpapi/image 已挂载' : '未挂载（检查 api.api_key）'],
      ['配置开关', s.config_enabled ? 'api.enabled=true' : 'api.enabled=false'],
      ['API Key', s.key_set ? '已配置' : '未配置'],
      ['重启次数', num(s.restarts || 0)]
    ]);
    $('btnApiStart').disabled = !!s.enabled || !s.registered;
    $('btnApiStop').disabled = !s.enabled;
    $('btnApiRestart').disabled = !s.registered;
    $('btnApiRefresh').disabled = false;
  }
  function loadApiService() {
    return api('/api/api-service').then(function (d) {
      renderApiService(d);
    }).catch(function (e) {
      $('apiSvcState').textContent = '不可达';
      $('apiSvcInfo').innerHTML = kv([['状态', '主服务不可达，请先启动主服务']]);
      apiSvcButtons(true);
      $('btnApiRefresh').disabled = false;
      fail(e);
    });
  }
  function apiSvcAction(action) {
    apiSvcButtons(true);
    api('/api/api-service', { method: 'POST', body: { action: action } })
      .then(function (d) { toast(d.message || '已完成'); loadApiService(); })
      .catch(function (e) { apiSvcButtons(false); fail(e); });
  }

  function loadDashboard() {
    loadService();
    loadApiService();
    api('/api/status').then(function (d) {
      var m = d.main || {}, p = d.plugin || {}, s = d.summary || {};
      $('dashMainState').textContent = m.reachable ? '在线' : '离线';
      $('dashMain').innerHTML = kv([
        ['主服务地址', d.main_service || '—'], ['模式', m.mode || '—'],
        ['知识库接口', m.api_enabled ? '已开启 /httpapi/query' : '未开启'],
        ['检索器', (m.brain || '').slice ? (m.brain || '').slice(0, 90) : '—'],
        ['索引重载', (m.reload || {}).state || '—']
      ]);
      $('dashPluginState').textContent = p.reachable ? '在线' : '离线';
      // /api/license：授权状态、到期日、剩余天数、机器码。插件离线或
      // 授权过期（插件会主动关侦听）时只显示提示，不让卡片报错。
      var lic = p.license || {};
      var stateMap = { trial: '试用中', active: '已激活', expired: '已过期', invalid: '激活码无效' };
      var lState = lic.state || '';
      var lPill = stateMap[lState] ||
        (p.license_error ? '读取失败' : (p.reachable ? '授权未知' : '插件离线'));
      var lText = lic.stateText || lPill;
      var lPillEl = $('dashLicenseState');
      lPillEl.textContent = lPill;
      lPillEl.className = 'count-pill' +
        (lState === 'active' ? '' :
          (lState === 'expired' || lState === 'invalid' || p.license_error ? ' bad' : ' warn'));
      // 1.1.1-beta1：手机活跃监控（探活 + 收到消息推算，服务端跨进程共享）
      var ph = d.phone || {};
      var activePill = $('dashPhoneActive');
      if (ph.online) {
        activePill.textContent = '活跃中';
        activePill.className = 'count-pill';
      } else {
        activePill.textContent = '已离线';
        activePill.className = 'count-pill bad';
      }
      function durText(sec) {
        sec = Math.max(0, Math.floor(sec || 0));
        if (sec < 60) return sec + ' 秒';
        var min = Math.floor(sec / 60), s = sec % 60;
        if (min < 60) return min + ' 分 ' + s + ' 秒';
        var h = Math.floor(min / 60);
        return h + ' 小时 ' + (min % 60) + ' 分';
      }
      $('dashPlugin').innerHTML = kv([
        ['当前微信号', p.self || m.self_wxid || '—'],
        ['当前微信昵称', p.self_name || '—'],
        ['微信就绪', p.wechatReady ? '是' : '否'],
        ['插件版本', p.version || '—'],
        ['上次活跃时间', ph.last_active_str || '—'],
        ['后台连续运行', ph.online ? durText(ph.continuous_seconds) : '已断开（当前未连续在线）'],
        ['当前推送地址', p.webhook || '（未设置）'],
        ['当前告警地址', p.alertPushUrl || ph.alert_url || '（未设置）'],
        ['激活状态', lText],
        ['到期日期', lic.expire || '—'],
        ['剩余天数', (lic.daysLeft === undefined || lic.daysLeft === null) ? '—' : lic.daysLeft + ' 天'],
        ['授权用户', lic.user || '—'],
        ['机器码', lic.machine || '—'],
        ['错误', p.error || p.license_error || '—']
      ]);
      $('stTotal').textContent = s.today_total || 0;
      $('stReplies').textContent = s.today_replies || 0;
      $('stAvg').textContent = (s.avg_ms || 0) + ' ms';
      $('stGroupPrivate').textContent = (s.today_group_replies || 0) + ' / ' + (s.today_private_replies || 0);
      $('stPolicyDocs').textContent = (s.policies || 0) + ' / ' + (s.docs || 0);
      $('stGroups').textContent = s.group_rules || 0;
      $('stPrivate').textContent = s.private_rules || 0;
      // 地址输入框：优先回显插件当前地址，没有则用本机建议地址；
      // 用户手动改过的值在刷新前的同一次会话内保留，不被自动回显覆盖。
      var hookInput = $('hookUrl'), alertInput = $('alertUrl');
      if (hookInput && !hookInput.dataset.dirty) {
        hookInput.value = p.webhook || d.webhook_suggest || '';
      }
      if (alertInput && !alertInput.dataset.dirty) {
        alertInput.value = p.alertPushUrl || ph.alert_url || d.alert_suggest || '';
      }
      $('miniStatus').innerHTML = '<span class="dot ' + (m.reachable && p.reachable ? 'dot-green' : 'dot-red') +
        '"></span><span class="mini-status-text">' + (m.reachable ? '服务在线' : '服务离线') + '</span>';
    }).catch(fail);
    loadStats();      // 图表统一走 /api/stats?days=N，与天数切换共用一份数据
  }
  /* ------------------------------ 对话记录 ------------------------------ */
  // 配图单元格：null/undefined = 老数据没有这个维度（显示「-」），
  // 0 = 确实没发图（「无」），>0 = 张数。三种状态不能混成同一个 0。
  // 触发方式：把内部枚举翻成中文，followup = 对话延续窗口内免 @ 的追问
  function fmtTrigger(t) {
    t = String(t || '');
    if (t === 'at') return '@ 本机';
    if (t === 'atAll') return '@所有人';
    if (t === 'followup') return '对话延续';
    if (t === 'quote:self') return '引用本机消息';
    if (t === 'default') return '默认规则';
    if (t === 'media') return '媒体消息';
    if (t.indexOf('wake:') === 0) return '唤醒词：' + t.slice(5);
    return t;
  }
  function imagesCell(n) {
    if (n === null || n === undefined) return '<span class="muted">-</span>';
    if (!n) return '<span class="muted">无</span>';
    return '<span title="附带 ' + n + ' 张图片">' + Number(n) + ' 张</span>';
  }
  function recParams() {
    var p = new URLSearchParams({ page: recPage, limit: recLimit });
    ['recStart', 'recEnd', 'recKind', 'recChatType', 'recKeyword'].forEach(function (id) {
      var map = { recStart: 'start', recEnd: 'end', recKind: 'kind', recChatType: 'chat_type', recKeyword: 'keyword' };
      var v = $(id).value.trim(); if (v) p.set(map[id], v);
    });
    return p;
  }
  function loadRecords() {
    api('/api/records?' + recParams()).then(function (d) {
      $('recTotal').textContent = d.total;
      var pg = renderPager('recPgInfo', 'recPrev', 'recNext', d);
      recPage = pg.page; recMaxPage = pg.maxPage;
      $('recBody').innerHTML = d.items.length ? d.items.map(function (r) {
        var kindText = { reply: 'AI 回复', skip: '跳过', unsupported: '暂不支持', transfer: '转人工', error: '错误' }[r.kind] || r.kind;
        return '<tr><td class="cell-time">' + esc(r.time_str) + '</td>' +
          '<td>' + esc(r.chat_name || r.chat) + '<div class="muted">' + esc(r.chat_type) + '</div></td>' +
          '<td class="cell-user">' + esc(r.sender_name || r.sender) + '</td>' +
          '<td>' + esc(r.msg_type) + '</td><td>' + tag(r.kind === 'reply', kindText, kindText) + '</td>' +
          '<td>' + esc(r.policy_name || '—') + '<div class="muted">' + esc(fmtTrigger(r.trigger)) + '</div></td>' +
          '<td>' + (r.plugin ? tag(false, r.plugin, r.plugin) : '<span class="muted">—</span>') + '</td>' +
          '<td class="cell-q">' + esc((r.question || '').slice(0, 60)) + '</td>' +
          '<td class="cell-a">' + esc((r.answer || r.reason || '').slice(0, 180)) + '</td>' +
          '<td>' + esc(r.ms || '') + '</td>' +
          '<td>' + imagesCell(r.n_images) + '</td></tr>';
      }).join('') : emptyRow(11, '暂无记录');
    }).catch(fail);
  }

  /* ------------------------------ 聊天记录（独立消息库） ------------------------------ */
  var chatPage = 1, chatLimit = 30, chatMaxPage = 1;
  function chatParams() {
    var p = new URLSearchParams({ page: chatPage, limit: chatLimit });
    [['chatStart', 'start'], ['chatEnd', 'end'], ['chatType', 'chat_type'],
      ['chatWho', 'who'], ['chatKeyword', 'keyword'],
      ['chatMsgType', 'msg_type'], ['chatDirection', 'direction']].forEach(function (x) {
      var v = $(x[0]).value.trim(); if (v) p.set(x[1], v);
    });
    return p;
  }
  function resetChatFilters() {
    ['chatStart', 'chatEnd', 'chatType', 'chatWho', 'chatKeyword',
      'chatMsgType', 'chatDirection'].forEach(function (id) { $(id).value = ''; });
    chatPage = 1;
  }
  function loadChatLog() {
    api('/api/chat-messages?' + chatParams()).then(function (d) {
      var s = d.stats || {};
      $('chatTotal').textContent = num(s.total);
      $('chatStats').innerHTML = kv([
        ['保留期', s.enabled ? ('已启用 · 保留 ' + num(s.retention_months) + ' 个月') : '已停用'],
        ['独立数据库', (s.db_file || '—') + ' · ' +
          num(Math.round((s.db_bytes || 0) / 1024)) + ' KB'],
        ['最早 / 最新', (s.earliest || '—') + ' ~ ' + (s.latest || '—')],
        ['自动清理', '每 ' + num(s.purge_interval_hours) + ' 小时，清理 ' +
          (s.cutoff_before || '—') + ' 之前的消息']
      ]);
      var pg = renderPager('chatPgInfo', 'chatPrev', 'chatNext', d);
      chatPage = pg.page; chatMaxPage = pg.maxPage;
      $('chatBody').innerHTML = d.items.length ? d.items.map(function (r) {
        var scene = r.chat_type === 'group' ? '群聊'
          : (r.chat_type === 'private' ? '私聊' : (r.chat_type || '—'));
        var dir = r.direction === 'out' ? '发出' : '收到';
        var txt = String(r.text || r.content || '—');
        var at = [];
        if (r.at_me) at.push('@我');
        if (r.at_all) at.push('@所有人');
        // 单行表格：一条消息一行，正文完整不截断，列宽不够由 .chat-scroll 横向滚动
        return '<tr>' +
          '<td class="chat-time">' + esc(r.time_str || '—') + '</td>' +
          '<td><span class="chat-chip">' + esc(scene) + '</span></td>' +
          '<td class="chat-who" title="' + esc(r.chat || '') + '">' +
            esc(r.chat_name || r.chat || '—') + '</td>' +
          '<td class="chat-sender" title="' + esc(r.sender || '') + '">' +
            esc(r.sender_name || r.sender || '—') + '</td>' +
          '<td>' + tag(r.direction !== 'out', dir, dir) + '</td>' +
          '<td class="muted">' + esc(r.type_name || r.type || '—') + '</td>' +
          '<td class="chat-at">' + (at.length ? esc(at.join(' · ')) : '—') + '</td>' +
          '<td class="chat-text" title="' + esc(txt) + '">' + esc(txt) + '</td>' +
        '</tr>';
      }).join('') : emptyRow(8, '暂无聊天记录');
    }).catch(fail);
  }

  /* ------------------------------ 媒体文件（图片/文件，全部 + 分页） ------------------------------ */
  var mediaPage = 1, mediaMaxPage = 1;
  function mediaPageSize() {
    var el = $('mediaPageSize');
    return el ? (Number(el.value) || 50) : 50;
  }
  function mediaParams() {
    var p = new URLSearchParams();
    [['mediaChatName', 'chat_name'], ['mediaChat', 'chat'], ['mediaWxid', 'wxid'],
      ['mediaMinutes', 'minutes'], ['mediaKind', 'kind'], ['mediaChatType', 'chat_type']].forEach(function (x) {
      var el = $(x[0]); if (!el) return;
      var v = el.value.trim(); if (v) p.set(x[1], v);
    });
    // 时间范围默认「全部」：minutes 为空时后端按不限时间处理
    p.set('page', mediaPage);
    p.set('page_size', mediaPageSize());
    return p;
  }
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
  }
  function loadMedia() {
    api('/api/media/recent?' + mediaParams()).then(function (d) {
      var st = d.stats || {};
      $('mediaIndexCount').textContent = num(st.index_count);
      $('mediaFileCount').textContent = num(st.file_count);
      $('mediaFileBytes').textContent = fmtBytes(st.file_bytes);
      $('mediaResultCount').textContent = num(d.total);
      // 分页信息：第 X / Y 页 · 共 N 条
      mediaPage = Math.max(1, d.page || 1);
      mediaMaxPage = Math.max(1, d.pages || 1);
      $('mediaPgInfo').textContent = '第 ' + mediaPage + ' / ' + mediaMaxPage + ' 页 · 共 ' +
        num(d.total) + ' 条' + (d.expired ? '（含 ' + d.expired + ' 条已过期）' : '');
      if ($('mediaPrev')) $('mediaPrev').disabled = mediaPage <= 1;
      if ($('mediaNext')) $('mediaNext').disabled = mediaPage >= mediaMaxPage;
      var items = d.items || [];
      $('mediaBody').innerHTML = items.length ? items.map(function (r) {
        var scene = r.chat_type === 'group' ? '群聊' : (r.chat_type === 'private' ? '私聊' : (r.chat_type || '—'));
        var kindTag = r.kind === 'image' ? tag(false, '图片', '图片') : tag(false, '文件', '文件');
        // 图片画质：thumb/preview 用醒目提醒，避免用户误以为存的是原图
        var quality = '';
        if (r.kind === 'image' && r.tier_label) {
          var low = (r.tier === 'thumb' || r.tier === 'preview');
          var qcls = low ? 'media-quality-low' : 'media-quality-ok';
          quality = '<div class="' + qcls + '" title="画质档位：' + esc(r.tier || '') + '">' +
            esc(r.tier_label) + (r.resolution ? ' ' + esc(r.resolution) : '') + '</div>';
        }
        // 可预览直接做成链接：图片和文件都可点开（文件走同源 send_file，浏览器按类型打开/下载）
        var status = r.available && r.path
          ? '<a class="tag tag-reply media-preview-link" href="/api/media/file?path=' +
            encodeURIComponent(r.path) + '" target="_blank" rel="noopener" title="点击预览/打开">可预览</a>'
          : '<span class="muted">已过期</span>';
        // 会话 / 发送者都显示两行：第一行名称，第二行 ID
        var chatName = r.chat_name || r.chat || '—';
        var chatCell = '<div class="media-line-main">' + esc(chatName) + '</div>' +
          (r.chat && r.chat !== chatName
            ? '<div class="media-line-sub">' + esc(r.chat) + '</div>' : '');
        var senderName = r.sender_name || r.wxid || '—';
        var senderCell = '<div class="media-line-main">' + esc(senderName) + '</div>' +
          (r.wxid && r.wxid !== senderName
            ? '<div class="media-line-sub">' + esc(r.wxid) + '</div>' : '');
        // 引用消息带图：场景列加角标，时间列用 title 提示引用消息的发送时间
        var quoteBadge = r.via === 'quote'
          ? '<span class="media-quote-badge" title="引用消息里的图片">引用</span>' : '';
        var timeTitle = (r.via === 'quote' && r.sent_time)
          ? ' title="引用发送时间：' + esc(r.sent_time) + '"' : '';
        return '<tr>' +
          '<td class="cell-time"' + timeTitle + '>' + esc(r.download_time) + '</td>' +
          '<td class="media-scene-cell"><span class="chat-chip">' + esc(scene) + '</span>' + quoteBadge + '</td>' +
          '<td class="media-cell-wide">' + chatCell + '</td>' +
          '<td class="media-cell-wide">' + senderCell + '</td>' +
          '<td>' + kindTag + quality + '</td>' +
          '<td class="chat-text" title="' + esc(r.orig_name || '') + '">' + esc(r.orig_name || '—') + '</td>' +
          '<td class="muted" title="' + esc(r.sha256 || '') + '">' + esc((r.hash_name || '').slice(0, 20)) + '…</td>' +
          '<td class="muted media-size-cell">' + fmtBytes(r.size) + '</td>' +
          '<td>' + status + (r.dedup ? '<div class="muted">哈希去重</div>' : '') + '</td></tr>';
      }).join('') : emptyRow(9, '没有符合条件的图片 / 文件');
    }).catch(fail);
  }
  function resetMediaFilters() {
    ['mediaChatName', 'mediaChat', 'mediaWxid'].forEach(function (id) { $(id).value = ''; });
    $('mediaMinutes').value = '0'; $('mediaKind').value = ''; $('mediaChatType').value = '';
    if ($('mediaPageSize')) $('mediaPageSize').value = '50';
    mediaPage = 1;
  }

  /* ------------------------------ AI 大模型管理 ------------------------------ */
  function loadLlmModels() {
    return api('/api/llm-models').then(function (d) { renderLlmModels(d); }).catch(fail);
  }
  function renderLlmModels(d) {
    var items = d.items || [], defId = d.default_id || 0, eff = d.effective || {};
    $('llmCount').textContent = items.length;
    $('llmEffective').innerHTML = kv([
      ['当前生效', eff.name || '—'], ['模型 ID', eff.model || '—'],
      ['接口地址', eff.base_url || '—'],
      ['来源', eff.source === 'default' ? '管理端默认模型（即时生效）' : 'config.yaml 兜底']
    ]);
    $('llmDefaultSel').innerHTML = '<option value="0">（不设默认，使用 config.yaml）</option>' +
      items.filter(function (x) { return x.enabled; }).map(function (x) {
        return '<option value="' + x.id + '"' + (x.id === defId ? ' selected' : '') + '>' +
          esc(x.name) + ' · ' + esc(x.model) + '</option>';
      }).join('');
    renderLlmScenes(d.scenes || {}, items);
    $('modelBody').innerHTML = items.length ? items.map(function (x) {
      return '<tr><td><strong>' + esc(x.name) + '</strong></td>' +
        '<td>' + esc(x.provider || '—') + '</td>' +
        '<td><code>' + esc(x.model) + '</code></td>' +
        '<td>' + esc(x.base_url) + '</td>' +
        '<td>' + esc(x.api_key_masked || '—') + '</td>' +
        '<td>' + tag(x.enabled, '启用', '停用') + '</td>' +
        '<td>' + (x.id === defId ? '<span class="chip">默认</span>' : '') + '</td>' +
        '<td><button class="btn btn-ghost btn-sm" data-llmedit="' + x.id + '">编辑</button> ' +
        '<button class="btn btn-warn btn-sm" data-llmdel="' + x.id + '">删除</button></td></tr>';
    }).join('') : emptyRow(8, '还没有纳管模型，点「新增模型」或「从当前配置导入」');
    $$('[data-llmedit]').forEach(function (b) {
      b.addEventListener('click', function () {
        var row = items.filter(function (x) { return String(x.id) === b.dataset.llmedit; })[0];
        if (row) editLlmModel(row);
      });
    });
    $$('[data-llmdel]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (!confirm('确定删除这个大模型？如果它是默认模型，删除后会回退到 config.yaml。')) return;
        api('/api/llm-models?id=' + encodeURIComponent(b.dataset.llmdel), { method: 'DELETE' })
          .then(function (r) { toast(r.message); renderLlmModels(r); }).catch(fail);
      });
    });
  }
  /* 分场景模型（群聊 / 私聊）：value=0 表示跟随全局默认。
     inherited=true 说明此刻用的是全局默认（没选，或原来选的模型已停用）。
     同一份配置在「AI 大模型」页和「群聊配置 / 私聊配置」页各有一个下拉，
     三处读写的都是同一个场景设置，改哪边都一样。 */
  function fillSceneSelect(selId, effId, sc, items) {
    var sel = $(selId), box = $(effId);
    if (!sel || !box) return;
    sc = sc || {};
    var enabled = (items || []).filter(function (x) { return x.enabled; });
    sel.innerHTML = '<option value="0">跟随全局默认</option>' + enabled.map(function (x) {
      return '<option value="' + x.id + '"' +
        (x.id === (sc.id || 0) ? ' selected' : '') + '>' +
        esc(x.name) + ' · ' + esc(x.model) + '</option>';
    }).join('');
    var eff = sc.effective || {};
    box.textContent = '当前生效：' + (eff.name || '—') +
      (eff.model ? ' · ' + eff.model : '') +
      (sc.inherited
        ? (sc.id ? '（原选模型已停用，回退全局默认）' : '（跟随全局默认）')
        : '（本场景独立配置）');
  }
  function renderLlmScenes(scenes, items) {
    fillSceneSelect('llmSceneGroup', 'llmSceneGroupEff', (scenes || {}).group, items);
    fillSceneSelect('llmScenePrivate', 'llmScenePrivateEff', (scenes || {}).private, items);
  }
  /* 群聊 / 私聊配置页的模型下拉：进页面时单独拉一次，避免依赖「AI 大模型」页是否打开过。 */
  function loadScenePicker(scene, selId, effId) {
    return api('/api/llm-models').then(function (d) {
      fillSceneSelect(selId, effId, (d.scenes || {})[scene], d.items);
    }).catch(function () {});
  }
  function saveSceneModel(scene, selId) {
    var body = {};
    body[scene] = $(selId).value;
    return api('/api/llm-models/scene', { method: 'POST', body: body })
      .then(function (d) {
        toast(d.message);
        fillSceneSelect(selId, selId + 'Eff', (d.scenes || {})[scene], d.items);
      }).catch(fail);
  }
  function editLlmModel(row) {
    var editing = !!row;
    var fields = [
      { key: 'name', label: '模型名称（唯一）', placeholder: '例：DeepSeek 主力' },
      { key: 'provider', label: '供应商（备注）', placeholder: '例：DeepSeek / 本地 Ollama' },
      { key: 'base_url', label: 'Base URL', placeholder: 'https://api.deepseek.com/v1' },
      { key: 'api_key', label: 'API Key',
        placeholder: editing ? '留空则不修改' : 'sk-…（本地网关可留空）',
        help: editing ? '页面不显示明文；留空表示保留原密钥' : '本地网关不需要鉴权时可留空' },
      { key: 'model', label: '模型 ID', placeholder: 'deepseek-chat' },
      { key: 'temperature', label: 'temperature（0~2）', type: 'number' },
      { key: 'stream', label: '流式', type: 'checkbox', checkLabel: '启用流式（推荐）' },
      { key: 'enabled', label: '启用', type: 'checkbox', checkLabel: '启用该模型' }
    ];
    var v = row || { temperature: 0.3, stream: true, enabled: true };
    openForm(editing ? '编辑大模型' : '新增大模型', fields, v, function (data) {
      if (editing) data.id = row.id;
      return api('/api/llm-models', { method: 'POST', body: data }).then(function (d) {
        toast(d.message); closeForm(); renderLlmModels(d);
      });
    });
  }

  /* ------------------------------ AI 用量 / API 调用记录 ------------------------------ */
  function num(v) {
    return String(Math.round(Number(v || 0))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function money(v) {
    return String(Number(Number(v || 0).toFixed(6)));
  }
  function sourceText(s) {
    return ({ wechat: '微信消息', api: 'HTTP 接口', kb_test: '问答测试',
              plugin: '插件调用', normal: '其他' })[s] || s || '—';
  }
  /* 场景列：分场景选模型后，这一列是核对「配置有没有真的生效」最直接的证据。
     老流水 / 非微信来源没有场景，显示空。 */
  function sceneText(s) {
    return ({ group: '群聊', private: '私聊' })[s] || '';
  }
  function llmParams() {
    var p = new URLSearchParams({ page: llmPage, limit: llmLimit, days: $('llmDays').value || 30 });
    [['llmStart', 'start'], ['llmEnd', 'end'], ['llmModel', 'model']].forEach(function (x) {
      var v = $(x[0]).value.trim(); if (v) p.set(x[1], v);
    });
    return p;
  }
  function apiParams() {
    var p = new URLSearchParams({ page: apiPage, limit: apiLimit, days: $('apiDays').value || 30 });
    [['apiStart', 'start'], ['apiEnd', 'end'], ['apiKeyword', 'keyword']].forEach(function (x) {
      var v = $(x[0]).value.trim(); if (v) p.set(x[1], v);
    });
    if ($('apiOnlyFailed').value === '1') p.set('only_failed', '1');
    return p;
  }
  function renderLlmChart(daily) {
    if (typeof Chart === 'undefined') return;
    var rows = daily || [];
    if (chartLlm) { chartLlm.destroy(); chartLlm = null; }
    chartLlm = new Chart($('chartLlm').getContext('2d'), {
      type: 'bar',
      data: {
        labels: rows.map(function (x) { return x.day.slice(5); }),
        datasets: [
          { label: '调用次数', data: rows.map(function (x) { return x.calls; }),
            backgroundColor: '#3b6ef5', yAxisID: 'y' },
          { label: 'token', type: 'line', data: rows.map(function (x) { return x.total_tokens; }),
            borderColor: '#e0a020', backgroundColor: '#e0a020', yAxisID: 'y1', tension: 0.3 }
        ]
      },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: true } },
        scales: { y: { beginAtZero: true, position: 'left', ticks: { precision: 0 } },
                  y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false } } } }
    });
  }
  function renderApiChart(daily) {
    if (typeof Chart === 'undefined') return;
    var rows = daily || [];
    if (chartApi) { chartApi.destroy(); chartApi = null; }
    chartApi = new Chart($('chartApi').getContext('2d'), {
      type: 'bar',
      data: {
        labels: rows.map(function (x) { return x.day.slice(5); }),
        datasets: [
          { label: '成功', data: rows.map(function (x) { return x.ok; }), backgroundColor: '#1a9c62' },
          { label: '失败', data: rows.map(function (x) { return x.fail; }), backgroundColor: '#d94c4c' }
        ]
      },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: true } },
        scales: { x: { stacked: true }, y: { beginAtZero: true, ticks: { precision: 0 } } } }
    });
  }
  function fillLlmModelSelect(d) {
    // 下拉取「全量出现过用量的模型」，不取筛选后的 models（否则筛完只剩一个可选项）
    var sel = $('llmModel'), cur = sel.value || '';
    var opts = (d.all_models || []).slice();
    if (cur && opts.indexOf(cur) < 0) opts.push(cur);   // 保留当前选择
    var labels = d.model_labels || {};                 // 上游 ID → 模型库里的纳管名称
    sel.innerHTML = '<option value="">全部模型</option>' + opts.map(function (m) {
      var lab = labels[m] ? (labels[m] + ' · ' + m) : (m + '（未纳管 / 历史）');
      return '<option value="' + esc(m) + '">' + esc(lab) + '</option>';
    }).join('');
    sel.value = cur;
  }
  function loadLlm() {
    var p = llmParams();
    api('/api/llm-usage?' + p).then(function (d) {
      fillLlmModelSelect(d);
      var t = d.today || {};
      $('llmTodayCalls').textContent = num(t.calls);
      $('llmTodayFoot').textContent = '今日 token ' + num(t.total_tokens) + ' · 费用 ¥' + money(t.cost);
      $('llmCalls').textContent = num(d.calls);
      $('llmSuccessFoot').textContent = '成功 ' + num(d.ok_calls) + ' · 失败 ' + num(d.fail_calls);
      $('llmTokens').textContent = num(d.total_tokens);
      $('llmTokenFoot').textContent = '输入 ' + num(d.prompt_tokens) + ' · 输出 ' + num(d.completion_tokens);
      $('llmCost').textContent = '¥' + money(d.cost);
      $('llmCostFoot').textContent = '平均耗时 ' + num(d.avg_latency_ms) + ' ms · 按配置单价估算';
      $('llmDaysLabel').textContent = p.get('days');
      // 「当前筛选结果合计」：条件与结果必须同屏对应，避免拿全量数字冒充筛选结果
      var cond = [d.model_filter ? ('模型 ' + d.model_filter) : '全部模型',
        (d.start || d.end) ? ((d.start || '…') + ' ~ ' + (d.end || '…')) : '全部日期'];
      if (d.model_filter) {
        // 明确写出这次筛选对应模型库里的哪个（些）模型，避免只看到一串上游 ID
        var matchNames = ((d.registry || {}).items || [])
          .filter(function (x) { return x.model === d.model_filter; })
          .map(function (x) { return x.name; });
        cond.push('模型库：' + (matchNames.length ? matchNames.join('、') : '未纳管 / 历史'));
      }
      $('llmScopeCond').textContent = cond.join(' · ');
      $('llmScopeCalls').textContent = num(d.calls);
      $('llmScopeIn').textContent = num(d.prompt_tokens);
      $('llmScopeOut').textContent = num(d.completion_tokens);
      $('llmScopeTokens').textContent = num(d.total_tokens);
      $('llmScopeCost').textContent = '¥' + money(d.cost);
      var un = d.unpriced_models || [], hint = $('llmUnpricedHint');
      if (un.length) {
        hint.textContent = '以下模型未配置单价，费用按 0 计：' + un.join('、') +
          '。请在「系统配置」的 brain.llm_pricing 里补充实际单价，费用才准确。';
        hint.classList.remove('hidden');
      } else { hint.classList.add('hidden'); }
      // 模型库 · 用量对照：以纳管模型为主体（含零用量），未纳管/历史单独一组
      var selModel = $('llmModel').value || '', grand = d.total_tokens || 0;
      var reg = d.registry || {}, regItems = reg.items || [], unmanaged = reg.unmanaged || [];
      function pctOf(t) { return (grand ? (t * 100 / grand) : 0).toFixed(1) + '%'; }
      function priceTag(p) {
        return p ? '<span class="tag tag-reply">已配置</span>'
                 : '<span class="tag tag-fail">未配置</span>';
      }
      var regRows = regItems.map(function (m) {
        return '<tr' + (String(m.model || '') === selModel ? ' class="row-active"' : '') +
          (m.calls ? '' : ' data-zero="1"') + '>' +
          '<td><button type="button" class="link-btn" data-llmpick="' +
            esc(m.model || '') + '">' + esc(m.name) + '</button>' +
            (m.is_default ? ' <span class="chip">默认</span>' : '') +
            '<span class="muted small">' + esc(m.base_url || '') + '</span></td>' +
          '<td>' + esc(m.provider || '—') + '</td>' +
          '<td><code>' + esc(m.model || '—') + '</code></td>' +
          '<td>' + tag(m.enabled, '启用', '停用') + '</td>' +
          '<td>' + num(m.calls) + '</td><td>' + num(m.prompt_tokens) + '</td>' +
          '<td>' + num(m.completion_tokens) + '</td><td>' + num(m.total_tokens) + '</td>' +
          '<td>' + pctOf(m.total_tokens) + '</td><td>' + money(m.cost) + '</td>' +
          '<td>' + num(m.avg_latency_ms) + ' ms</td><td>' + priceTag(m.priced) + '</td></tr>';
      });
      var unRows = unmanaged.map(function (u) {
        return '<tr class="row-unmanaged"><td><span class="muted">未纳管 / 历史</span></td>' +
          '<td>—</td><td><code>' + esc(u.model || '—') + '</code></td>' +
          '<td><span class="tag tag-fail">未纳管</span></td>' +
          '<td>' + num(u.calls) + '</td><td>' + num(u.prompt_tokens) + '</td>' +
          '<td>' + num(u.completion_tokens) + '</td><td>' + num(u.total_tokens) + '</td>' +
          '<td>' + pctOf(u.total_tokens) + '</td><td>' + money(u.cost) + '</td>' +
          '<td>' + num(u.avg_latency_ms) + ' ms</td><td>' + priceTag(u.priced) + '</td></tr>';
      });
      $('llmModelBody').innerHTML = regRows.concat(unRows).join('') ||
        emptyRow(12, '模型库里还没有模型，请到「AI 大模型」页新增或从当前配置导入');
      // 点模型名 = 把筛选条件切到该模型并立即重算所有合计
      $$('[data-llmpick]').forEach(function (b) {
        b.addEventListener('click', function () {
          $('llmModel').value = b.dataset.llmpick;
          llmPage = 1; loadLlm();
        });
      });
      renderLlmChart(d.daily || []);
      loadLlmRecords();
    }).catch(fail);
  }
  function loadLlmRecords() {
    api('/api/llm-records?' + llmParams()).then(function (d) {
      $('llmTotal').textContent = d.total;
      var pg = renderPager('llmPgInfo', 'llmPrev', 'llmNext', d);
      llmPage = pg.page; llmMaxPage = pg.maxPage;
      $('llmBody').innerHTML = d.items.length ? d.items.map(function (r) {
        return '<tr><td class="cell-time">' + esc(r.time_str) + '</td>' +
          '<td>' + (r.llm_name
            ? '<strong>' + esc(r.llm_name) + '</strong>' +
              (r.llm_inferred ? ' <span class="muted small">（按 ID 推断）</span>' : '') +
              '<div class="muted small"><code>' + esc(r.model || '') + '</code></div>'
            : '<span class="muted">未纳管 · </span><code>' + esc(r.model || '—') + '</code>') +
            '</td><td>' + esc(sourceText(r.source)) +
            (sceneText(r.scene) ? ' <span class="muted small">· ' + sceneText(r.scene) + '</span>' : '') +
            '</td>' +
          '<td>' + num(r.prompt_tokens) + '</td><td>' + num(r.completion_tokens) + '</td>' +
          '<td>' + num(r.total_tokens) + '</td><td>' + money(r.cost) + '</td>' +
          '<td>' + (r.latency_ms == null ? '—' : num(r.latency_ms) + ' ms') + '</td>' +
          '<td>' + (r.stream ? '流式' : '非流式') + '</td>' +
          '<td>' + tag(!!r.ok, '成功', '失败') + '</td></tr>';
      }).join('') : emptyRow(10, '暂无记录');
    }).catch(fail);
  }
  function loadApi() {
    var p = apiParams();
    api('/api/api-calls/stats?' + p).then(function (d) {
      var s = d.summary || {};
      $('apiCalls').textContent = num(s.calls);
      $('apiSessionFoot').textContent = '会话 ' + num(s.sessions) + ' · 来源 IP ' + num(s.ips);
      $('apiOkFail').textContent = num(s.ok_calls) + ' / ' + num(s.fail_calls);
      $('apiRateFoot').textContent = '成功率 ' +
        (s.calls ? Math.round(s.ok_calls * 100 / s.calls) : 0) + '%';
      $('apiLatency').textContent = num(s.avg_latency_ms) + ' ms';
      $('apiTokenFoot').textContent = '平均提问 ' + num(s.avg_q_chars) + ' 字 · token ' + num(s.total_tokens);
      $('apiCost').textContent = '¥' + money(s.cost);
      $('apiCostFoot').textContent = '含图片 ' + num(s.images) + ' 张 · LLM 调用 ' + num(s.llm_calls) + ' 次';
      $('apiDaysLabel').textContent = p.get('days');
      $('apiErrorBody').innerHTML = (s.errors || []).length ? s.errors.map(function (e) {
        return '<tr><td>' + esc(e.error) + '</td><td>' + num(e.count) + '</td><td>' +
          (s.fail_calls ? Math.round(e.count * 100 / s.fail_calls) : 0) + '%</td></tr>';
      }).join('') : emptyRow(3, '暂无失败记录');
      renderApiChart(d.daily || []);
      loadApiRecords();
    }).catch(fail);
  }
  function loadApiRecords() {
    api('/api/apicalls?' + apiParams()).then(function (d) {
      $('apiTotal').textContent = d.total;
      var pg = renderPager('apiPgInfo', 'apiPrev', 'apiNext', d);
      apiPage = pg.page; apiMaxPage = pg.maxPage;
      $('apiBody').innerHTML = d.items.length ? d.items.map(function (r) {
        return '<tr><td class="cell-time">' + esc(r.time_str) + '</td>' +
          '<td>' + tag(!!r.ok, '成功', '失败') + '</td>' +
          '<td class="cell-q">' + esc((r.question || '').slice(0, 60)) + '</td>' +
          '<td class="cell-a">' + esc((r.answer || r.error || '').slice(0, 90)) + '</td>' +
          '<td>' + esc(r.status == null ? '' : r.status) + '</td>' +
          '<td>' + (r.latency_ms == null ? '—' : num(r.latency_ms) + ' ms') + '</td>' +
          '<td>' + num(r.total_tokens) + '</td><td>' + money(r.cost) + '</td>' +
          '<td>' + esc((r.session_id || '—') + ' / ' + (r.client_ip || '—')) + '</td>' +
          '<td>' + imagesCell(r.n_images) + '</td></tr>';
      }).join('') : emptyRow(10, '暂无记录');
    }).catch(fail);
  }

  /* ------------------------------ 群聊配置 ------------------------------ */
  function sortValue(r, key) {
    var v = r[key];
    if (Array.isArray(v)) return key === 'docs' ? v.length : v.join('、');
    if (typeof v === 'boolean') return v ? 1 : 0;
    return v === null || v === undefined ? '' : v;
  }
  function sortRows(rows, state) {
    if (!state.key) return rows.slice();
    return rows.slice().sort(function (a, b) {
      var x = sortValue(a, state.key), y = sortValue(b, state.key), c;
      if (typeof x === 'number' && typeof y === 'number') c = x - y;
      else c = String(x).localeCompare(String(y), 'zh-CN');
      return c * state.dir;
    });
  }
  function bindSortHeaders(scope, state, render) {
    $$(scope + ' th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        var k = th.dataset.sort;
        state.dir = (state.key === k && state.dir === 1) ? -1 : 1;
        state.key = k;
        $$(scope + ' th[data-sort]').forEach(function (x) {
          x.classList.remove('sort-asc', 'sort-desc');
        });
        th.classList.add(state.dir === 1 ? 'sort-asc' : 'sort-desc');
        render();
      });
    });
  }
  var grpRows = [], grpSort = { key: '', dir: 1 };
  function groupFields() {
    return [
      { type: 'group', label: '基本信息' },
      { key: 'group_id', label: '群聊 ID', compact: true, datalist: groupOptions(),
        placeholder: '12345@chatroom', help: '可从已同步的通讯录里选，选择后自动带入群名称' },
      { key: 'group_name', label: '群名称', compact: true, help: '仅作备注，方便识别' },
      { key: 'enabled', label: '', type: 'checkbox', checkLabel: '启用该群配置' },

      { type: 'group', label: '触发条件' },
      { key: 'wake_words', label: '唤醒词（逗号 / 换行分隔）', type: 'textarea', rows: 2,
        value: '', help: '例如：小助、小范；@ 本机也触发，唤醒词可留空' },
      { key: 'at_all_trigger', label: '', type: 'checkbox', checkLabel: '@所有人也触发' },
      { key: 'quote_self_trigger', label: '', type: 'checkbox', checkLabel: '引用本机消息也触发',
        help: '客户回引机器人发过的消息时自动接着回答，不必再 @ 或加唤醒词' },

      { type: 'group', label: '知识库' },
      { key: 'need_kb', label: '', type: 'checkbox', checkLabel: '需要查询知识库',
        help: '不勾选则跳过知识库检索，直接由 AI 依据通用知识回答' },
      { key: 'policy_id', label: '知识库配置', type: 'select', compact: true,
        options: rulePolicyOptions() },

      { type: 'group', label: '转人工' },
      { key: 'need_transfer', label: '', type: 'checkbox', checkLabel: '允许转人工',
        help: '不勾选则忽略「转人工」关键词与 AI 的转人工标记，直接回复客户' },
      { key: 'human_wxid', label: '人工接待 wxid', compact: true,
        help: '转人工时 @ 这个微信号；留空用 transfer.default_wxid' },
      { key: 'human_name', label: '人工接待昵称', compact: true,
        help: '写入 atNames，让 @ 显示正确名字；留空用 transfer.default_name' },

      { type: 'group', label: '回复与备注' },
      { key: 'system_prompt', label: '提示词（留空用默认）', type: 'textarea', rows: 3,
        help: '留空使用 config.yaml 的 brain.system_prompt；填写后本配置的所有回复都改用它' },
      { key: 'note', label: '备注', compact: true }
    ];
  }
  function editGroup(v) {
    var fields = groupFields();
    var values = Object.assign({}, v);
    values.wake_words = arrText(v && v.wake_words);
    values.enabled = v ? v.enabled : true;
    values.at_all_trigger = v ? v.at_all_trigger : false;   // 新建默认不勾选
    values.quote_self_trigger = v ? (v.quote_self_trigger !== false) : true;
    values.need_kb = v ? (v.need_kb !== false) : true;      // 老数据缺字段时按「开」处理
    values.need_transfer = v ? (v.need_transfer !== false) : true;
    values.system_prompt = (v && v.system_prompt) || '';
    values.human_wxid = (v && v.human_wxid) || '';
    values.human_name = (v && v.human_name) || '';
    openForm(v ? '编辑群聊配置' : '新建群聊配置', fields, values, function (data) {
      if (v) data.id = v.id;
      return api('/api/group-rules', { method: 'POST', body: data }).then(function (d) {
        toast(d.message); closeForm(); loadGroups();
      });
    }, function () {
      // 从通讯录下拉选到某个群 → 自动把群名称带进表单
      var gid = $('f_group_id'), gname = $('f_group_name');
      function fill() {
        var val = (gid.value || '').trim();
        var hit = OPTIONS.groups.filter(function (g) { return g.wxid === val; })[0];
        if (hit) gname.value = hit.name || '';
      }
      gid.addEventListener('input', fill);
      gid.addEventListener('change', fill);
      // 开关联动：关知识库 → 下拉禁用且清空；关转人工 → 人工接待禁用且清空
      bindRuleFlagToggles(true);
    });
  }
  function renderGroupRows() {
    var rows = sortRows(grpRows, grpSort);
    $('grpBody').innerHTML = rows.length ? rows.map(function (r) {
      return '<tr><td><strong>' + esc(r.group_name || '（未命名群）') + '</strong></td>' +
        '<td><code>' + esc(r.group_id) + '</code></td><td>' + esc(listStr(r.wake_words) || '（仅 @ 触发）') + '</td>' +
        '<td>' + esc(r.policy_name || '—') + '</td><td>' + esc((r.docs || []).length) + '</td>' +
        '<td>' + promptBrief(r.system_prompt) + '</td>' +
        '<td>' + (r.human_wxid ? esc((r.human_name || '人工客服') + ' · ' + r.human_wxid)
                  : '<span class="muted">默认</span>') + '</td>' +
        '<td>' + tag(r.enabled, '启用', '停用') +
          '<span class="muted small">知识库 ' + (r.need_kb !== false ? '开' : '关') +
          ' · 转人工 ' + (r.need_transfer !== false ? '开' : '关') +
          ' · 引用触发 ' + (r.quote_self_trigger !== false ? '开' : '关') + '</span></td>' +
        '<td><button class="btn btn-ghost btn-sm" data-gedit="' + r.id + '">编辑</button> ' +
        '<button class="btn btn-warn btn-sm" data-gdel="' + r.id + '">删除</button></td></tr>';
    }).join('') : emptyRow(9, '还没有群聊配置；点右上角「新建配置」，只有表内的群才会被响应');
    $$('[data-gedit]').forEach(function (b) {
      b.addEventListener('click', function () {
        editGroup(grpRows.filter(function (x) { return x.id == b.dataset.gedit; })[0]);
      });
    });
    $$('[data-gdel]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (!confirm('确定删除这条群聊配置？')) return;
        api('/api/group-rules/' + b.dataset.gdel, { method: 'DELETE' })
          .then(function (r) { toast(r.message); loadGroups(); }).catch(fail);
      });
    });
  }
  function loadGroups() {
    api('/api/group-rules').then(function (d) {
      grpRows = d.items || []; $('grpTotal').textContent = d.total; renderGroupRows();
    }).catch(fail);
  }

  /* ------------------------------ 私聊配置 ------------------------------ */
  function loadGlobalDeny() {
    api('/api/global-deny').then(function (d) { $('globalDeny').value = arrText(d.items); }).catch(fail);
  }
  var prvRows = [], prvSort = { key: '', dir: 1 };
  function editPrivate(v) {
    var fields = [
      { type: 'group', label: '基本信息' },
      { key: 'name', label: '配置名', compact: true },
      { key: 'priority', label: '优先级', type: 'number', compact: true,
        help: '数字小的先匹配' },
      { key: 'enabled', label: '', type: 'checkbox', checkLabel: '启用该配置' },

      { type: 'group', label: '触发与范围' },
      { key: 'wake_words', label: '唤醒词（逗号 / 换行分隔）', type: 'textarea', rows: 2,
        help: '留空 = 默认配置，所有私聊消息都走它' },
      { key: 'allow_list', label: '白名单（wxid 或昵称）', type: 'textarea', rows: 2,
        help: '留空 = 所有用户' },
      { key: 'deny_list', label: '黑名单（wxid 或昵称）', type: 'textarea', rows: 2 },

      { type: 'group', label: '知识库' },
      { key: 'need_kb', label: '', type: 'checkbox', checkLabel: '需要查询知识库',
        help: '不勾选则跳过知识库检索，直接由 AI 依据通用知识回答' },
      { key: 'policy_id', label: '知识库配置', type: 'select', compact: true,
        options: rulePolicyOptions() },

      { type: 'group', label: '转人工' },
      { key: 'need_transfer', label: '', type: 'checkbox', checkLabel: '允许转人工',
        help: '不勾选则忽略「转人工」关键词与 AI 的转人工标记，直接回复客户' },
      { key: 'human_wxid', label: '人工接待 wxid', compact: true,
        help: '私聊转人工不 @ 人，此项仅作记录；留空用 transfer.default_wxid' },
      { key: 'human_name', label: '人工接待昵称', compact: true,
        help: '转人工话术里的 {name}；留空用 transfer.default_name' },

      { type: 'group', label: '回复与备注' },
      { key: 'system_prompt', label: '提示词（留空用默认）', type: 'textarea', rows: 3,
        help: '留空使用 config.yaml 的 brain.system_prompt；填写后本配置的所有回复都改用它' },
      { key: 'note', label: '备注', compact: true }
    ];
    var values = Object.assign({}, v);
    ['wake_words', 'allow_list', 'deny_list'].forEach(function (k) { values[k] = arrText(v && v[k]); });
    values.enabled = v ? v.enabled : true;
    values.priority = v ? v.priority : 0;
    values.need_kb = v ? (v.need_kb !== false) : true;
    values.need_transfer = v ? (v.need_transfer !== false) : true;
    values.system_prompt = (v && v.system_prompt) || '';
    values.human_wxid = (v && v.human_wxid) || '';
    values.human_name = (v && v.human_name) || '';
    openForm(v ? '编辑私聊配置' : '新建私聊配置', fields, values, function (data) {
      if (v) data.id = v.id;
      return api('/api/private-rules', { method: 'POST', body: data }).then(function (d) {
        toast(d.message); closeForm(); loadPrivate();
      });
    }, function () { bindRuleFlagToggles(true); });
  }
  function renderPrivateRows() {
    var rows = sortRows(prvRows, prvSort);
    $('prvBody').innerHTML = rows.length ? rows.map(function (r) {
      return '<tr><td><strong>' + esc(r.name) + '</strong></td>' +
        '<td>' + esc(listStr(r.wake_words) || '（默认，无需唤醒）') + '</td>' +
        '<td>' + esc(listStr(r.allow_list) || '所有人') + '</td>' +
        '<td>' + esc(listStr(r.deny_list) || '—') + '</td>' +
        '<td>' + esc(r.policy_name || '—') + '</td><td>' + esc(r.priority) + '</td>' +
        '<td>' + promptBrief(r.system_prompt) + '</td>' +
        '<td>' + tag(r.enabled, '启用', '停用') +
          '<span class="muted small">知识库 ' + (r.need_kb !== false ? '开' : '关') +
          ' · 转人工 ' + (r.need_transfer !== false ? '开' : '关') + '</span></td>' +
        '<td><button class="btn btn-ghost btn-sm" data-pedit="' + r.id + '">编辑</button> ' +
        '<button class="btn btn-warn btn-sm" data-pdel="' + r.id + '">删除</button></td></tr>';
    }).join('') : emptyRow(9, '还没有私聊配置');
    $$('[data-pedit]').forEach(function (b) {
      b.addEventListener('click', function () {
        editPrivate(prvRows.filter(function (x) { return x.id == b.dataset.pedit; })[0]);
      });
    });
    $$('[data-pdel]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (!confirm('确定删除这条私聊配置？')) return;
        api('/api/private-rules/' + b.dataset.pdel, { method: 'DELETE' })
          .then(function (r) { toast(r.message); loadPrivate(); }).catch(fail);
      });
    });
  }
  function loadPrivate() {
    loadGlobalDeny();
    api('/api/private-rules').then(function (d) {
      prvRows = d.items || []; $('prvTotal').textContent = d.total; renderPrivateRows();
    }).catch(fail);
  }

  /* ------------------------------ 知识库管理 ------------------------------ */
  function editPolicy(v) {
    var locked = !!(v && v.protected);
    var fields = [
      { key: 'name', label: '知识库名称', disabled: locked,
        help: locked ? '系统预置知识库不可改名、不可删除' : '' },
      { key: 'description', label: '说明' },
      { key: 'docs', label: '包含的知识库文档', type: 'multiselect', options: OPTIONS.docs,
        help: '新入库的文档默认只加入「默认知识库」；这里可手动勾选/取消本知识库包含的文档' },
      { key: 'enabled', label: '启用', type: 'checkbox', checkLabel: '启用该配置' }
    ];
    var values = Object.assign({}, v);
    values.docs = v ? (v.docs || []) : OPTIONS.docs.slice();
    values.enabled = v ? v.enabled : true;
    openForm(v ? '编辑知识库' : '新建知识库', fields, values, function (data) {
      if (v) {
        data.id = v.id;
        if (locked) data.name = v.name;   // 双保险：系统库名称不参与提交
      }
      return api('/api/policies', { method: 'POST', body: data }).then(function (d) {
        toast(d.message); closeForm(); refreshOptions().then(loadPolicies);
      });
    });
  }
  // 知识库里文档多时不能把几十个 chip 全铺开（一行被撑成几屏高）。
  // 只显示前 5 个 + 「+N 篇」，完整清单放 title 里悬停可看。
  function docsCell(docs) {
    var list = docs || [];
    // 每篇文档独占一行，明细一目了然；不再折叠成「+N 篇」。
    return '<div class="doc-lines">' + (list.length
      ? list.map(function (x) { return '<div class="doc-line">' + badge(x) + '</div>'; }).join('')
      : '<span class="muted">（空知识库：不检索任何文档）</span>') + '</div>';
  }
  function loadPolicies() {
    api('/api/policies').then(function (d) {
      $('polTotal').textContent = d.total;
      $('polBody').innerHTML = d.items.length ? d.items.map(function (r) {
        return '<tr><td><strong>' + esc(r.name) + '</strong></td><td class="cell-desc" title="' + esc(r.description || '') + '">' + esc(r.description || '—') + '</td>' +
          '<td>' + (r.docs.length ? docsCell(r.docs) : '<span class="muted">（空知识库：不检索任何文档）</span>') + '</td>' +
          '<td>' + esc(r.used_by) + ' 条配置</td><td>' + tag(r.enabled, '启用', '停用') + '</td>' +
          '<td><button class="btn btn-ghost btn-sm" data-poedit="' + r.id + '">编辑</button> ' +
          (r.protected
            ? '<span class="muted small">系统预置 · 不可删除</span>'
            : '<button class="btn btn-warn btn-sm" data-podel="' + r.id + '">删除</button>') +
          '</td></tr>';
      }).join('') : emptyRow(6, '还没有知识库');
      $$('[data-poedit]').forEach(function (b) {
        b.addEventListener('click', function () {
          editPolicy(d.items.filter(function (x) { return x.id == b.dataset.poedit; })[0]);
        });
      });
      $$('[data-podel]').forEach(function (b) {
        b.addEventListener('click', function () {
          if (!confirm('确定删除该知识库？')) return;
          api('/api/policies/' + b.dataset.podel, { method: 'DELETE' })
            .then(function (r) { toast(r.message); refreshOptions().then(loadPolicies); }).catch(fail);
        });
      });
    }).catch(fail);
  }

  /* ------------------------------ 通讯录 ------------------------------ */
  // 头像来自插件 v2 的 avatar（微信 CDN 直链）。CDN 带 Referer 只回占位图，
  // 所以用 referrerpolicy="no-referrer"；末尾 /0 是原尺寸，列表缩成 /132。
  function avatarUrl(u) {
    var s = String(u || '');
    return /^https?:\/\//i.test(s) ? s.replace(/\/0$/, '/132') : '';
  }
  function avatarHtml(name, url, size) {
    var u = avatarUrl(url);
    return '<span class="ct-avatar' + (size === 'lg' ? ' lg' : '') + '"><i>' +
      esc(String(name || '?').slice(0, 1)) + '</i>' +
      (u ? '<img src="' + esc(u) + '" alt="" loading="lazy" ' +
        'referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">' : '') +
      '</span>';
  }
  /* 写备注 / 群昵称后，后端缓存已即时回写，但页面上其它页签的 DOM 还是旧的。
     这里打脏标记，切到对应页签时按需重载，避免「切过去看到旧名字」。 */
  var contactsDirty = false;
  function markContactsDirty() { contactsDirty = true; }
  function loadContacts() { loadCtGroups(); loadCtFriends(); }
  // 三个页签互斥：群聊 / 好友 / 查找（全量搜索）。搜索条与结果都在「查找」里。
  function ctSwitchTab(kind) {
    var panes = { groups: ['tabGroups', 'ctGroups'],
                  friends: ['tabFriends', 'ctFriends'],
                  search: ['tabSearch', 'ctSearch'] };
    Object.keys(panes).forEach(function (k) {
      var on = k === kind;
      $(panes[k][0]).classList.toggle('active', on);
      $(panes[k][1]).classList.toggle('hidden', !on);
    });
    if (kind === 'search' && $('ctSearchInput').focus) $('ctSearchInput').focus();
    // 写操作之后各页签可能已过期：切到哪个就重载哪个
    if (contactsDirty) {
      if (kind === 'friends') loadCtFriends();
      else if (kind === 'groups') loadCtGroups();
      else if (kind === 'search') searchContacts();
    }
  }
  /* 全量通讯录搜索：好友 / 群聊 / 群友，按 wxid、name（昵称、备注、群名片也算）匹配。
     只读本地同步缓存，不实时访问手机插件；结果里的「查看详情 / 查看群友」复用已有弹窗。 */
  var ctSearchRows = [];
  function ctKindLabel(kind) {
    return kind === 'group' ? '群聊' : (kind === 'member' ? '群友' : '好友');
  }
  function renderCtSearch() {
    $('ctSearchBody').innerHTML = ctSearchRows.length ? ctSearchRows.map(function (r, i) {
      var name = r.shown_name || r.name || r.wxid;
      var sub = [r.nick_name, r.remark, r.group_nick].filter(Boolean).join(' / ') || '—';
      var act = r.kind === 'group' || (r.kind === 'member' && r.group_id)
        ? '<button class="btn btn-ghost btn-sm" data-ctsmem="' +
          esc(r.kind === 'group' ? r.wxid : r.group_id) + '">查看群友</button>'
        : '<button class="btn btn-ghost btn-sm" data-ctsfr="' + esc(r.wxid) +
          '">查看详情</button>' +
          (r.kind === 'friend'
            ? '<button class="btn btn-ghost btn-sm" data-ctsfrremark="' + esc(r.wxid) +
              '">改备注</button>' : '');
      return '<tr><td>' + avatarHtml(name, r.avatar) + '</td>' +
        '<td><span class="ct-kind ct-kind-' + esc(r.kind) + '">' + ctKindLabel(r.kind) +
        '</span></td><td><strong>' + esc(name) + '</strong></td>' +
        '<td><code>' + esc(r.wxid) + '</code></td><td>' + esc(sub) + '</td>' +
        '<td>' + (r.group_name ? esc(r.group_name) + '<br><span class="muted small">' +
          esc(r.group_id || '') + '</span>' : '—') + '</td>' +
        '<td>' + esc(r.match || '—') + '</td><td>' + act + '</td></tr>';
    }).join('') : emptyRow(8, '没有匹配的联系人，可先在右上角「从手机同步」更新缓存');
  }
  function searchContacts() {
    var q = String($('ctSearchInput').value || '').trim();
    if (!q) {
      $('ctSearchResult').classList.add('hidden');
      $('ctSearchInfo').textContent = '请输入 wxid 或名称';
      return;
    }
    $('ctSearchInfo').textContent = '搜索中…';
    return api('/api/contacts/search?q=' + encodeURIComponent(q)).then(function (d) {
      ctSearchRows = d.items || [];
      $('ctSearchInfo').textContent = '「' + q + '」命中 ' + (d.count || 0) +
        ' 条（好友 ' + (d.friends || 0) + ' · 群聊 ' + (d.groups || 0) +
        ' · 群友 ' + (d.members || 0) + '）';
      $('ctSearchResult').classList.remove('hidden');
      renderCtSearch();
    }).catch(fail);
  }
  function clearCtSearch() {
    $('ctSearchInput').value = '';
    ctSearchRows = [];
    $('ctSearchResult').classList.add('hidden');
    $('ctSearchInfo').textContent = '搜索只读本地同步缓存，不访问手机插件';
  }
  function loadCtGroups() {
    api('/api/contacts/groups').then(function (d) {
      $('ctGroupBody').innerHTML = d.items.length ? d.items.map(function (g) {
        return '<tr><td><strong>' + esc(g.name || '（未命名）') + '</strong></td>' +
          '<td><code>' + esc(g.wxid) + '</code></td><td>' + esc(g.member_count || 0) + '</td>' +
          '<td>' + esc(g.cached_members || 0) + '</td><td>' + esc(g.source || '—') + '</td>' +
          '<td>' + esc(g.synced_str || '—') + '</td>' +
          '<td><button class="btn btn-ghost btn-sm" data-ctmem="' + esc(g.wxid) + '">查看群友</button></td></tr>';
      }).join('') : emptyRow(7, '暂无群聊，点「从手机同步」拉取');
      $$('[data-ctmem]').forEach(function (b) {
        b.addEventListener('click', function () { showGroupMembers(b.dataset.ctmem); });
      });
    }).catch(fail);
  }
  /* 好友来源标签：由插件 uiClass 决定，别跟个人好友混在一起分不清。
     myEnterpriseMember  企业成员 —— 我所在企业的企微号（wxid 形如 xxx@openim）
     enterpriseContact   企微联系人 —— 通过企业微信加的联系人，不是本企业成员
     这两类都不在插件默认的 friend 分类里，同步时按 uiClass 各拉一次后合并。 */
  function ctClassTag(cls) {
    if (cls === 'myEnterpriseMember') return '<span class="tag tag-ent">企业</span>';
    if (cls === 'enterpriseContact') return '<span class="tag tag-wxwork">企微联系人</span>';
    return '';
  }
  function ctClassLabel(cls) {
    if (cls === 'myEnterpriseMember') return '企业成员（我所在企业的企微号）';
    if (cls === 'enterpriseContact') return '企微联系人（不是本企业成员）';
    return '微信好友';
  }
  function loadCtFriends() {
    api('/api/contacts/friends').then(function (d) {
      $('ctFriendBody').innerHTML = d.items.length ? d.items.map(function (c) {
        // 本人：插件的好友列表含自己，标出来；微信没有「给自己改备注」的通道，
        // 所以本人的行只留「查看详情」，不给改备注按钮。
        var remarkBtn = c.is_self ? ''
          : '<button class="btn btn-ghost btn-sm" data-ctfrremark="' + esc(c.wxid) + '">改备注</button>';
        var classTag = ctClassTag(c.ui_class);
        return '<tr><td>' + avatarHtml(c.name || c.wxid, c.avatar) + '</td>' +
          '<td><strong>' + esc(c.name || c.wxid) + '</strong>' + classTag + '</td>' +
          '<td>' + (c.is_self ? '<span class="tag tag-self">本人</span>' : '') + '</td>' +
          '<td><code>' + esc(c.wxid) + '</code></td><td>' + esc(c.nick_name || '—') + '</td>' +
          '<td>' + esc(c.remark || '—') + '</td><td>' + esc(c.source || '—') + '</td>' +
          '<td>' + esc(c.synced_str || '—') + '</td>' +
          '<td><button class="btn btn-ghost btn-sm" data-ctfr="' + esc(c.wxid) + '">查看详情</button>' +
          remarkBtn + '</td></tr>';
      }).join('') : emptyRow(9, '暂无好友，点「从手机同步」拉取');
      $$('[data-ctfr]').forEach(function (b) {
        b.addEventListener('click', function () { showFriend(b.dataset.ctfr); });
      });
      $$('[data-ctfrremark]').forEach(function (b) {
        b.addEventListener('click', function () {
          var wxid = b.dataset.ctfrremark;
          var c = d.items.filter(function (x) { return x.wxid === wxid; })[0] || {};
          editFriendRemark(wxid, c.remark || '', function () { loadCtFriends(); });
        });
      });
    }).catch(fail);
  }
  /* 备注 / 群昵称统一编辑：走通用表单弹窗，提交成功才关窗、才提示。
     三个接口都会**真的写到手机微信**（备注只自己可见，群昵称群内可见），
     所以清空 / 群昵称变更都先确认一次；失败信息直接显示在弹窗里。 */
  function openSocialEdit(o) {
    openForm(o.title,
      [{ key: 'value', label: o.label, type: 'text',
         placeholder: o.placeholder || '留空表示清除', help: o.help }],
      { value: o.value || '' },
      function (data) {
        var val = String(data.value || '').trim();
        var ask = typeof o.confirmText === 'function' ? o.confirmText(val) : o.confirmText;
        // 值没变就别再弹「对群里所有人可见」这种确认，直接提交（等于强制重写一次）
        if (ask && val === String(o.value || '').trim()) ask = '';
        var submit = function () {
          return api(o.path, { method: 'POST', body: o.body(val) }).then(function (r) {
            closeForm();
            if (r.confirmed !== false) markContactsDirty();
            toast(r.message || '已保存', r.confirmed === false ? 'warn' : '');
            if (o.done) o.done(r);
          });
        };
        if (!ask) return submit();
        // 确认框弹出时先把按钮复位，别在后面露一个「保存中…」；
        // 确定后再置灰，真正进入写手机的阶段。
        setFormBusy(false);
        return askConfirm(ask, { title: o.askTitle || '请确认写入手机微信' }).then(function (ok) {
          if (!ok) return;
          setFormBusy(true);
          return submit();
        });
      });
  }
  function editFriendRemark(wxid, current, done) {
    openSocialEdit({
      title: '改好友备注', label: '备注（只自己可见）', value: current,
      help: '备注会写入手机微信，只有你能看到；显示名会同步变成「备注 > 昵称 > wxid」。' +
            '这是全局备注，该联系人在所有群里本机看到的显示名也会跟着变。留空保存即清除备注。',
      path: '/api/contacts/friends/remark',
      body: function (v) { return { wxid: wxid, remark: v }; },
      confirmText: function (v) { return v ? '' : '确定要清除这个好友的备注吗？'; },
      done: done
    });
  }
  function editSelfGroupNick(chat, current, done) {
    openSocialEdit({
      title: '改我的群昵称', label: '群昵称', value: current,
      help: '群昵称是你在该群的显示名，群内所有人都能看到；改完群里会出现一条修改提示。' +
            '留空保存即清空群昵称（恢复微信昵称）。',
      path: '/api/contacts/groups/nickname',
      body: function (v) { return { chat: chat, nickname: v }; },
      askTitle: '确认修改群昵称',
      confirmText: function (v) {
        return '群昵称对群里所有人可见，确定改成「' + (v || '（清空）') + '」吗？';
      },
      done: done
    });
  }
  function editMemberRemark(chat, wxid, name, current, done) {
    var notice = '这是全局好友备注：该群友在所有群中本机看到的显示名都将是该备注。';
    openSocialEdit({
      title: '改群成员备注 · ' + name, label: '备注（只自己可见）', value: current,
      help: notice + ' 不要求群主 / 管理员权限，留空保存即清除备注。',
      path: '/api/contacts/groups/member/remark',
      body: function (v) { return { chat: chat, wxid: wxid, remark: v }; },
      askTitle: '确认修改群成员备注',
      confirmText: function (v) {
        return notice + '\n\n确定把这个群友的备注' + (v ? '改成「' + v + '」' : '清除') + '吗？';
      },
      done: done
    });
  }
  /* 群成员弹窗：表头点击排序；显示名统一为 display_name → name → wxid，
     与 CSV 导出的「显示名」列口径保持一致。
     操作列：本人 → 只能改我的群昵称（10.1）；其他成员 → 改备注（走全局好友备注
     通道，不要求群主 / 管理员）。改他人群昵称在微信 8.0.80 没有客户端通道，
     插件 v1.2.10-beta67 起已下线该入口，所以不提供。 */
  var memRows = [], memSort = { key: '', dir: 1 }, memChat = '', memRole = {};
  function memRoleLabel() {
    if (!memRole) return '未知（缓存里没有本人记录）';
    if (memRole.isOwner) return '群主';
    if (memRole.isAdmin) return '管理员';
    return '普通成员';
  }
  function renderMemberRows() {
    var body = $('ctMemBody');
    if (!body) return;
    var rows = sortRows(memRows, memSort);
    body.innerHTML = rows.length ? rows.map(function (m) {
      // v1.2.10-beta67 起群成员备注 = 全局好友备注，不再看 canManage
      var act = m.is_self
        ? '<button class="btn btn-ghost btn-sm" data-memself="1">改我的群昵称</button>'
        : '<button class="btn btn-ghost btn-sm" data-memremark="' + esc(m.wxid) + '">改备注</button>';
      return '<tr><td>' + avatarHtml(m.shown_name, m.avatar) + '</td>' +
        '<td>' + esc(m.shown_name) + '</td>' +
        '<td><code>' + esc(m.wxid) + '</code></td><td>' + esc(m.nick_name || '—') + '</td>' +
        '<td>' + esc(m.remark || '—') + '</td><td>' + esc(m.group_nick || '—') + '</td>' +
        '<td>' + (m.is_self ? '<span class="tag tag-self">本人</span>' : '') + '</td>' +
        '<td>' + (m.is_owner ? '<span class="tag tag-admin">群主</span>' : '') + '</td>' +
        '<td>' + (m.is_admin && !m.is_owner ? '<span class="tag tag-admin">管理员</span>' : '') + '</td>' +
        '<td>' + act + '</td></tr>';
    }).join('') : emptyRow(10, '没有成员数据');
    $$('[data-memself]').forEach(function (b) {
      b.addEventListener('click', function () {
        var me = memRows.filter(function (x) { return x.is_self; })[0] || {};
        editSelfGroupNick(memChat, me.group_nick || '', function () {
          showGroupMembers(memChat, false);
        });
      });
    });
    $$('[data-memremark]').forEach(function (b) {
      b.addEventListener('click', function () {
        var wxid = b.dataset.memremark;
        var m = memRows.filter(function (x) { return x.wxid === wxid; })[0] || {};
        editMemberRemark(memChat, wxid, m.shown_name || wxid, m.remark || '', function () {
          showGroupMembers(memChat, false);
        });
      });
    });
  }
  function showGroupMembers(chat, refresh) {
    memChat = chat;
    api('/api/contacts/groups/members?chat=' + encodeURIComponent(chat) + (refresh ? '&refresh=1' : ''))
      .then(function (d) {
        memRows = (d.members || []).map(function (m) {
          return Object.assign({}, m, {
            shown_name: m.display_name || m.name || m.wxid
          });
        });
        memRole = d.role || {};
        memSort = { key: '', dir: 1 };
        var me = memRows.filter(function (m) { return m.is_self; })[0] || {};
        var roleTip = ' · 我的角色：<strong>' + esc(memRoleLabel()) + '</strong>' +
          ' · 改备注是全局好友备注：<strong>该群友在所有群中本机看到的显示名都将是该备注</strong>';
        var html = '<div class="small muted" style="margin-bottom:10px">共 ' + esc(d.count) + ' 位群友' +
          (d.cached ? '（本地缓存）' : '（刚从手机同步）') + ' · 群 ID <code>' + esc(chat) + '</code>' +
          ' · 我的群昵称：<strong>' + esc(me.group_nick || '（未设置）') + '</strong>' +
          roleTip + ' · 点表头可排序</div>' +
          '<div class="table-wrap" style="max-height:70vh;overflow:auto"><table class="table"><thead><tr>' +
          '<th>头像</th>' +
          '<th data-sort="shown_name">显示名</th><th data-sort="wxid">wxid</th>' +
          '<th data-sort="nick_name">昵称</th><th data-sort="remark">备注</th>' +
          '<th data-sort="group_nick">群名片</th><th data-sort="is_self">本人</th>' +
          '<th data-sort="is_owner">群主</th><th data-sort="is_admin">管理员</th><th>操作</th>' +
          '</tr></thead><tbody id="ctMemBody"></tbody></table></div>';
        var extra = '<a class="btn btn-primary btn-sm" href="/api/contacts/groups/members/export?chat=' +
          encodeURIComponent(chat) + '">导出 CSV</a> ' +
          '<button class="btn btn-ghost btn-sm" id="btnMemRefresh">从手机重新拉取</button>';
        openDetail('群成员 · ' + (d.group_name || chat), html, extra);
        renderMemberRows();
        bindSortHeaders('#detailBody', memSort, renderMemberRows);
        $('btnMemRefresh').addEventListener('click', function () { showGroupMembers(chat, true); });
      }).catch(fail);
  }
  function showFriend(wxid) {
    // 只用通讯录同步后的本地缓存：不再带 refresh=1 触发插件 /api/contacts?wxid=
    api('/api/contacts/friends/detail?wxid=' + encodeURIComponent(wxid)).then(function (d) {
      var l = d.local || {};
      var isSelf = !!l.is_self;
      var html = '<div class="ct-detail-head">' +
        avatarHtml(l.name || wxid, l.avatar, 'lg') +
        '<div><strong>' + esc(l.name || wxid) + '</strong>' +
        (isSelf ? ' <span class="tag tag-self">本人</span>' : '') +
        ctClassTag(l.ui_class) +
        '<div class="muted small">' + esc(l.wxid || wxid) + '</div></div></div>' +
        '<div class="kv-list">' + kv([
        ['wxid', l.wxid || wxid], ['显示名', l.name || '—'],
        ['微信昵称', l.nick_name || '—'], ['备注', l.remark || '—'],
        ['类型', ctClassLabel(l.ui_class)],
        ['本人', isSelf ? '是（当前登录账号）' : '否'],
        ['来源', l.source || '—'], ['同步时间', l.synced_str || '—'],
        ['最近提问时间', l.last_seen || '—'], ['最近提问', l.last_question || '—']
      ]) + '</div>' +
        '<div class="small muted" style="margin-top:12px">' +
        (isSelf
          ? '这是你自己的账号，微信没有「给自己改备注」的通道，所以这里不提供改备注。'
          : '数据来自通讯录同步缓存；备注可直接在这里改，改完立即写入手机微信（只自己可见）。') +
        '</div>';
      openDetail('好友详情 · ' + (l.name || wxid), html,
        isSelf ? '' : '<button class="btn btn-primary btn-sm" id="btnFrRemark">改备注</button>');
      if (!isSelf) {
        $('btnFrRemark').addEventListener('click', function () {
          editFriendRemark(l.wxid || wxid, l.remark || '', function () {
            showFriend(wxid); loadCtFriends();
          });
        });
      }
    }).catch(fail);
  }
  function syncContacts() {
    if (!confirm('从手机同步通讯录会先清空后台现有的群聊和好友缓存，再用手机上的最新数据整表重建。\n手机上已经删除的群聊 / 好友也会一并从后台移除。\n\n确定继续吗？')) return;
    api('/api/contacts/sync', { method: 'POST', body: {} }).then(function (d) {
      toast(d.message); loadCtGroups(); loadCtFriends(); refreshOptions();
    }).catch(fail);
  }

  /* ------------------------------ 知识库 ------------------------------ */
  /* ------------------------------ 知识库：Markdown 预览 / 图片库 ------------------------------ */
  // 知识库 markdown 主要用于阅读与检索，图片只允许指向本服务
  // 的 /api/kb/image/raw，外链一律换成占位（避免后台打开别人的 tracker）。
  if (typeof marked !== 'undefined' && marked.setOptions) {
    marked.setOptions({ gfm: true, breaks: true, headerIds: false, mangle: false });
  }
  var kbMode = 'edit';
  var kbEditorPreviewTimer = null;
  var kbImages = [];

  function renderMd(text) {
    if (typeof marked === 'undefined') {
      return '<div class="md-empty">Markdown 渲染库未加载</div>';
    }
    var raw;
    try { raw = marked.parse(text || ''); }
    catch (e) { return '<div class="md-empty">渲染失败：' + esc(e.message) + '</div>'; }
    raw = rewriteImgSrc(raw);
    // 知识库内容会变成 innerHTML，不消毒就是 XSS 入口
    return (typeof DOMPurify !== 'undefined') ? DOMPurify.sanitize(raw) : raw;
  }

  // 把 ![](images/xxx.png) 重写成后台原图地址。
  // ⚠️ 必须先 decodeURIComponent 再 encodeURIComponent：marked 的 renderer 已经
  // 对 src 做过 encodeURI（中文 → %E4%B8%80…），直接再编码就是双重编码，
  // 服务端拿到 'images/%E4%B8%80….png' 这种带字面 % 的串会判非法 → 404。
  // 这正是「图就在 data/knowledge/images/ 里，预览却一张都出不来」的真凶。
  function rewriteImgSrc(html) {
    return String(html).replace(/<img\b[^>]*>/gi, function (tag) {
      var m = /src\s*=\s*"([^"]*)"/i.exec(tag);
      if (!m) return tag;
      var src = m[1], decoded;
      try { decoded = decodeURIComponent(src); } catch (e) { decoded = src; }
      if (/^(https?:)?\/\//i.test(decoded)) {
        return tag.replace(m[0], 'src="" data-kb-ext="' + esc(src) + '"');
      }
      var rel = decoded.replace(/^\.?\//, '');
      if (!/^images\//i.test(rel)) {
        return tag.replace(m[0], 'src="" data-kb-ext="' + esc(src) + '"');
      }
      rel = rel.split('#')[0].split('?')[0];
      return tag.replace(m[0],
        'src="/api/kb/image/raw?name=' + encodeURIComponent(rel) + '"');
    });
  }

  // 图片加载失败时给出可读提示，而不是一个莫名的碎图图标
  function markImgErrors(root) {
    $$('img', root).forEach(function (im) {
      im.addEventListener('error', function () {
        var a = im.getAttribute('src') || '';
        if (a.indexOf('/api/kb/image/raw') < 0 || im.dataset.kbBroken) return;
        im.dataset.kbBroken = '1';
        var txt = document.createTextNode('［图片缺失：' +
          (im.getAttribute('alt') || a.split('name=').pop()) + '］');
        if (im.replaceWith) im.replaceWith(txt);
        else if (im.parentNode && im.parentNode.replaceChild) {
          im.parentNode.replaceChild(txt, im);
        }
      });
    });
  }

  function updatePreview(force) {
    if (!force && kbMode === 'edit') return;
    var box = $('kbPreview');
    if (!box) return;
    box.innerHTML = renderMd($('kbEditor').value || '');
    markImgErrors(box);
  }

  function kbStatUpdate() {
    var t = $('kbEditor').value || '';
    var lines = t ? t.split('\n').length : 0, bytes = t.length;
    try { bytes = new Blob([t]).size; } catch (e) { /* 老环境按字符数估 */ }
    $('kbFileStat').textContent = lines + ' 行 · ' + (bytes / 1024).toFixed(1) + ' KB';
  }

  function insertMd(before, after, placeholder) {
    var ta = $('kbEditor');
    var s = ta.selectionStart || 0, e = ta.selectionEnd || 0;
    var sel = ta.value.slice(s, e) || placeholder;
    ta.value = ta.value.slice(0, s) + before + sel + after + ta.value.slice(e);
    if (ta.focus) ta.focus();
    ta.selectionStart = s + before.length;
    ta.selectionEnd = s + before.length + sel.length;
    markKbDirty();
    kbStatUpdate();
    updatePreview();
  }

  function insertImage(path) {
    var alt = String(path).split('/').pop().replace(/\.[^.]+$/, '');
    insertMd('\n![' + alt + '](' + path + ')\n', '', '');
    toast('已插入 ' + path + '（记得保存，再重建索引）');
  }

  // ---------- 图片库：上传 / 缩略图 / 点选插入 / 删除 ----------
  function loadKbImages() {
    var el = $('kbImgList');
    if (!el) return;
    api('/api/kb/images').then(function (d) {
      kbImages = d.items || [];
      if (!kbImages.length) {
        el.innerHTML = '<div class="empty">还没有图片。导入 Word/Excel/PDF 时抽出的图，' +
          '或在这里上传截图后，就能插入文档并随 AI 回答一起发出。</div>';
        return;
      }
      el.innerHTML = kbImages.map(function (im) {
        var dim = (im.width && im.height) ? (im.width + '×' + im.height) : '尺寸未知';
        return '<div class="img-item" data-path="' + esc(im.path) + '" title="点击插入到文档">' +
          '<img loading="lazy" src="/api/kb/image/raw?name=' +
          encodeURIComponent(im.path) + '" alt="' + esc(im.name) + '">' +
          '<div class="img-meta"><strong>' + esc(im.name) + '</strong>' +
          '<span>' + dim + ' · ' + (Number(im.size || 0) / 1024).toFixed(0) + ' KB</span>' +
          '<button class="btn-del-img" data-imgdel="' + esc(im.name) + '">删除</button></div></div>';
      }).join('');
      $$('.img-item', el).forEach(function (it) {
        it.addEventListener('click', function (e) {
          if (e.target && e.target.classList &&
              e.target.classList.contains('btn-del-img')) return;
          setKbMode('edit');
          insertImage(it.dataset.path);
          $('kbImgDlg').classList.add('hidden');
        });
      });
      $$('.btn-del-img', el).forEach(function (b) {
        b.addEventListener('click', function (e) {
          e.stopPropagation();
          var name = b.dataset.imgdel;
          if (!confirm('删除图片「' + name + '」？\n如果文档里还引用着它，那些位置会变成空缺。')) return;
          api('/api/kb/image?name=' + encodeURIComponent(name), { method: 'DELETE' })
            .then(function (d) {
              toast(d.message || '已删除', d.refs ? 'warn' : '');
              loadKbImages();
            }).catch(function (err) { toast('删除失败：' + err.message, 'err'); });
        });
      });
    }).catch(function (e) { toast('图片库加载失败：' + e.message, 'err'); });
  }

  function uploadKbImages(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    var i = 0;
    function next() {
      if (i >= list.length) { loadKbImages(); return; }
      var f = list[i++];
      var fd = new FormData();
      fd.append('file', f);
      api('/api/kb/image', { method: 'POST', body: fd }).then(function (d) {
        toast('已上传 ' + (d.name || f.name) + '（可直接点击插入）');
        next();
      }).catch(function (e) {
        toast('上传失败 ' + f.name + '：' + e.message, 'err');
        next();
      });
    }
    next();
  }

  function openKbImagePicker() {
    loadKbImages();
    $('kbImgDlg').classList.remove('hidden');
  }

  function renderImportResults(results) {
    var box = $('kbImportResult');
    if (!box) return;
    box.innerHTML = (results || []).map(function (x) {
      var r = x.result || {};
      var detail = x.ok
        ? ('→ ' + esc(r.name || '') + ' · ' + (r.chars || 0) + ' 字 · 图片 ' +
           (r.images || 0) + ' 张' + (r.overwritten ? ' · 已覆盖同名' : '') +
           ((r.warnings || []).length ? ' · ' + esc((r.warnings || []).join('；')) : ''))
        : esc(x.error || '失败');
      return '<div class="kb-import-row' + (x.ok ? '' : ' is-err') + '">' +
        '<strong>' + esc(x.name) + '</strong><span class="muted">' + detail + '</span></div>';
    }).join('');
  }

  function resetKbDirty() {
    kbDirty = false;
    $('btnKbSave').disabled = true;
  }
  function markKbDirty() {
    kbDirty = true;
    $('btnKbSave').disabled = false;
  }
  function setKbMode(mode) {
    if (mode !== 'preview' && mode !== 'split' && mode !== 'edit') mode = 'edit';
    kbMode = mode;
    // 预览态隐藏编辑器、编辑态隐藏预览、分栏态两边都显示。
    // CSS 里的 .kb-panes.mode-* 负责尺寸，hidden 只负责互斥，两者保持一致。
    $('kbEditor').classList.toggle('hidden', mode === 'preview');
    $('kbPreview').classList.toggle('hidden', mode === 'edit');
    $('kbPanes').className = 'kb-panes mode-' + mode;
    $('btnKbPreview').disabled = (mode === 'preview');
    $('btnKbSplit').disabled = (mode === 'split');
    $('btnKbEdit').disabled = (mode === 'edit');
    // 当前模式按钮高亮：disabled 只是防止重复点击，视觉上要能看出「点中」了哪个
    [['btnKbPreview', 'preview'], ['btnKbSplit', 'split'], ['btnKbEdit', 'edit']]
      .forEach(function (pair) {
        $(pair[0]).classList.toggle('mode-active', mode === pair[1]);
      });
    if (mode !== 'edit') updatePreview(true);
    kbStatUpdate();
  }
  function promptBrief(v) {
    var t = (v || '').trim();
    if (!t) return '<span class="muted">默认</span>';
    return esc(t.length > 24 ? t.slice(0, 24) + '…' : t);
  }
  function askUnsavedLeave() {
    openDetail('有未保存的修改',
      '<div class="alert alert-warn">知识库文档「' +
      esc($('kbFileName').value || kbCurrent || '未命名') +
      '」有未保存的修改，离开前请选择：</div>',
      '<button class="btn btn-primary btn-sm" id="leaveSave">保存并离开</button> ' +
      '<button class="btn btn-warn btn-sm" id="leaveDiscard">不保存离开</button> ' +
      '<button class="btn btn-ghost btn-sm" id="leaveCancel">取消</button>');
    $('leaveSave').addEventListener('click', function () {
      closeDetail();
      saveKb(function () {
        if (pendingView) { var v = pendingView; pendingView = null; switchView(v, true); }
      });
    });
    $('leaveDiscard').addEventListener('click', function () {
      closeDetail(); resetKbDirty();
      if (pendingView) { var v = pendingView; pendingView = null; switchView(v, true); }
    });
    $('leaveCancel').addEventListener('click', function () {
      closeDetail(); pendingView = null;
    });
  }
  /* ---------- 索引重建进度 ----------
     重建索引 = 主服务重新加载模型 + 重新编码全部片段，CPU 上要几十秒~几分钟。
     以前点了只有一个 toast「正在后台重建」，之后界面毫无变化，用户完全不知道
     在干什么、有没有卡住。这里轮询 /api/kb/rebuild/status，把阶段、百分比、
     实时明细和已用时显示出来。 */
  var rebuildPoll = null, rebuildStart = 0, rebuildLastStage = '';

  function rebuildLog(text, cls) {
    var el = $('rebuildLog');
    if (!el) return;
    var d = document.createElement('div');
    var t = new Date();
    d.innerHTML = '<span class="rl-time">' +
      String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0') +
      ':' + String(t.getSeconds()).padStart(2, '0') + '</span>' +
      '<span class="' + (cls || '') + '">' + esc(text) + '</span>';
    el.appendChild(d);
    while (el.children.length > 60) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }
  function rebuildElapsed() {
    var s = Math.floor((Date.now() - rebuildStart) / 1000);
    if (s < 60) return s + ' 秒';
    return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
  }
  function rebuildSetBar(pct, indeterminate) {
    var bar = $('rebuildBar');
    if (!bar) return;
    bar.classList.toggle('indeterminate', !!indeterminate);
    if (!indeterminate) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }
  function openRebuildModal() {
    rebuildStart = Date.now();
    rebuildLastStage = '';
    $('rebuildLog').innerHTML = '';
    $('rebuildTitle').textContent = '正在重建索引';
    $('rebuildStage').textContent = '准备中…';
    $('rebuildPct').textContent = '0%';
    $('rebuildDetail').textContent = '已用时 0 秒';
    rebuildSetBar(0, true);
    $('rebuildModal').classList.remove('hidden');
  }
  function closeRebuildModal() {
    $('rebuildModal').classList.add('hidden');
  }
  /** 用户主动关闭弹窗：任务仍在主服务后台跑，轮询继续，
   *  完成 / 失败时照样弹 toast —— 避免用户关了窗口就完全不知道结果。 */
  function dismissRebuildModal() {
    closeRebuildModal();
    if (rebuildPoll) {
      rebuildLog('已关闭进度窗口，重建继续在后台进行…');
      toast('重建继续在后台进行，完成后会有提示');
    }
  }
  function rebuildPollOnce() {
    api('/api/kb/rebuild/status').then(function (d) {
      var r = d.reload || {};
      var state = r.state || 'idle';
      var pct = Number(r.progress || 0);
      var stage = r.stage || '';
      var detail = r.detail || '';
      $('rebuildStage').textContent = stage || (state === 'running' ? '正在重建…' : '');
      $('rebuildPct').textContent = pct + '%';
      $('rebuildDetail').textContent = '已用时 ' + rebuildElapsed();
      // 「加载向量模型」这一步耗时不定（可能本地缓存命中，也可能走网络），
      // 用条纹动画表示「还在动」，比一个卡住的百分比更诚实
      rebuildSetBar(pct, state === 'running' && (stage === '加载向量模型' || stage === '准备中'));

      if (stage && stage !== rebuildLastStage) {
        rebuildLastStage = stage;
        rebuildLog(stage + (detail ? ' · ' + detail : ''));
      }
      if (state === 'done') {
        if (rebuildPoll) { clearInterval(rebuildPoll); rebuildPoll = null; }
        closeRebuildModal();
        toast('索引重建完成，用时 ' + rebuildElapsed());
        rebuildLog('重建完成', 'rl-ok');
        loadKnowledge();
      } else if (state === 'failed') {
        if (rebuildPoll) { clearInterval(rebuildPoll); rebuildPoll = null; }
        closeRebuildModal();
        var why = r.error || detail || '未知错误';
        rebuildLog('重建失败：' + why, 'rl-err');   // 弹窗虽关，日志留痕，下次打开能看到
        toast('索引重建失败：' + why, 'err');
      }
    }).catch(function (e) {
      if (rebuildPoll) { clearInterval(rebuildPoll); rebuildPoll = null; }
      closeRebuildModal();
      rebuildLog('重建状态查询失败：' + (e.message || e), 'rl-err');
      fail(e);
    });
  }
  function startRebuild() {
    openRebuildModal();
    rebuildLog('已请求重建索引…');
    api('/api/kb/rebuild', { method: 'POST', body: {} }).then(function (d) {
      if (d.message) rebuildLog(d.message);
      if (rebuildPoll) clearInterval(rebuildPoll);
      rebuildPoll = setInterval(rebuildPollOnce, 1200);
      rebuildPollOnce();
    }).catch(function (e) {
      closeRebuildModal();
      fail(e);
    });
  }

  function loadKnowledge() {
    fillAskPolicy();            // 切到知识库页时刷新问答测试的配置下拉
    // 先给列表一个「加载中」占位，避免慢响应时左栏整块空白、用户以为页面坏了
    var listEl = $('kbFileList');
    if (listEl && !listEl.children.length) {
      listEl.innerHTML = '<div class="empty">正在加载文档列表…</div>';
    }
    api('/api/kb/files').then(function (d) {
      $('kbCount').textContent = d.items.length;
      // 后端偶发没带 index 时不能让整个左栏渲染中断（曾导致统计区整块空白）
      var idx = d.index || {};
      var cacheFiles = (idx.files || []).length;
      $('kbIndexInfo').innerHTML = '<div class="kv-list">' + kv([
        ['文档数', idx.doc_count === undefined ? '—' : idx.doc_count],
        ['索引缓存文件', cacheFiles + ' 个' + (cacheFiles ? '' : '（未生成向量缓存）')],
        ['缓存大小', fmtBytes(idx.total_bytes)],
        ['知识库图片', (d.stats || {}).images || 0],
        ['指纹', idx.fingerprint || '—']
      ]) + '</div><div class="cfg-hint">改完文档请点「重建索引并重载」，主服务会在后台重新构建索引；图片可在「图片库」里查看与插入。</div>';
      $('kbFileList').innerHTML = d.items.length ? d.items.map(function (f) {
        return '<div class="file-item' + (f.name === kbCurrent ? ' active' : '') + '" data-kb="' + esc(f.name) + '">' +
          '<span class="file-name">' + esc(f.name) + '</span>' +
          '<small class="file-size">' + esc(Math.round(f.size / 1024)) + ' KB</small>' +
          '<button class="btn btn-warn btn-sm file-del" data-kbdel="' + esc(f.name) +
          '" title="删除该文档">删</button></div>';
      }).join('') : '<div class="empty">知识库为空，点「新建」或「上传」</div>';
      $$('[data-kb]').forEach(function (el) {
        el.addEventListener('click', function (e) {
          if (e.target.closest('[data-kbdel]')) return;   // 点行内删除按钮不要触发打开
          openKbFile(el.dataset.kb);
        });
      });
      $$('[data-kbdel]').forEach(function (b) {
        b.addEventListener('click', function (e) {
          e.stopPropagation();
          deleteKbByName(b.dataset.kbdel);
        });
      });
    }).catch(fail);
  }
  function openKbFile(name, preview) {
    api('/api/kb/file?name=' + encodeURIComponent(name)).then(function (d) {
      kbCurrent = name;
      $('kbFileName').value = name; $('kbEditor').value = d.content;
      // 选中 / 上传 / 导入后默认进「预览」：知识库主要用于阅读，点开文档就落在
      // 可编辑的 textarea 上容易误改正文；要看内容编排与配图，预览也更直观。
      // 只有显式传 preview=false 才回到编辑态（新建 / 删除后另有 setKbMode('edit')）。
      resetKbDirty(); setKbMode(preview === false ? 'edit' : 'preview');
      $$('.file-item').forEach(function (x) { x.classList.toggle('active', x.dataset.kb === name); });
    }).catch(fail);
  }
  function normDocName(name) {
    var n = (name || '').trim();
    if (!n) return '';
    return /\.(md|txt)$/i.test(n) ? n : (n + '.md');   // 后端只收 .md/.txt，没写后缀就自动补
  }
  function saveKb(onDone) {
    var name = normDocName($('kbFileName').value);
    if (!name) return toast('请先填文件名', 'warn');
    $('kbFileName').value = name;
    api('/api/kb/file', { method: 'POST', body: { name: name, content: $('kbEditor').value } })
      .then(function (d) {
        toast(d.message || '已保存');
        kbCurrent = d.name || name;
        $('kbFileName').value = kbCurrent;
        resetKbDirty();
        loadKnowledge(); refreshOptions();
        if (typeof onDone === 'function') onDone();
      }).catch(fail);
  }
  function deleteKbByName(name) {
    var n = normDocName(name);
    if (!n) return toast('请先点选左侧文件，或填写要删除的文件名', 'warn');
    if (!confirm('确定删除「' + n + '」？\n会备份到 .kb_backup/，并从所有知识库移除')) return;
    api('/api/kb/file?name=' + encodeURIComponent(n), { method: 'DELETE' })
      .then(function (d) {
        toast(d.message);
        if (kbCurrent === n || normDocName($('kbFileName').value) === n) {
          kbCurrent = ''; $('kbFileName').value = ''; $('kbEditor').value = '';
          resetKbDirty(); setKbMode('edit');
        }
        loadKnowledge(); refreshOptions();
      }).catch(fail);
  }
  // 问答测试：查询条件与按钮常驻页面顶部，结果直接渲染到 #askResult。
  // 查完不清空输入、不关弹窗，可以接着改问题再查（以前是「表单弹窗 → 结果弹窗」两步）。
  var askBusy = false;
  function fillAskPolicy() {
    var sel = $('askPolicy');
    if (!sel) return;
    var cur = sel.value;                 // 刷新选项时保留当前选择
    sel.innerHTML = '<option value="">全库检索</option>' +
      policyOptions().map(function (o) {
        return '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>';
      }).join('');
    sel.value = cur;
  }
  function renderAskResult(r) {
    var html = '<div class="kv-list">' + kv([
      ['知识库', r.policy || '全库'], ['命中片段', (r.retrieved || []).length],
      ['耗时', (r.elapsed || 0) + ' s'], ['文档范围', (r.sources || []).join('、') || '全部']
    ]) + '</div>' + '<div class="qa-block" style="margin-top:14px"><h4>回答</h4><div class="qa-answer">' +
      esc(r.answer || r.error || '（无结果）') + '</div></div>' +
      // 本次检索判定要一起发出的知识库图片（路径是 images/xxx.png）
      ((r.images || []).length ? '<div class="qa-block"><h4>本次会随回答发出的图片</h4>' +
        r.images.map(function (p) {
          return '<img loading="lazy" src="/api/kb/image/raw?name=' +
            encodeURIComponent(p) + '" alt="' + esc(p) +
            '" style="max-height:180px;margin:4px 8px 4px 0">';
        }).join('') + '</div>' : '') +
      ((r.retrieved || []).length ? '<div class="qa-block"><h4>召回片段</h4><div class="qa-hits">' +
        r.retrieved.map(function (x) { return '<div class="qa-hit">' + esc(x) + '</div>'; }).join('') + '</div></div>' : '');
    $('askResult').innerHTML = html;
  }
  function runKbAsk() {
    var q = ($('askInput').value || '').trim();
    if (!q) { toast('请先填测试问题', 'warn'); return; }
    if (askBusy) return;                 // 一次只跑一个子进程，避免重复提交
    askBusy = true;
    var btn = $('btnAsk');
    btn.disabled = true;
    btn.textContent = '查询中…';
    $('askState').textContent = '查询中…';
    $('askResult').innerHTML = '<div class="alert alert-info">正在检索并生成回答，首次加载模型可能要 1~3 分钟，请稍候…</div>';
    api('/api/kb/ask', { method: 'POST',
      body: { question: q, policy: $('askPolicy').value || '' } })
      .then(function (r) {
        renderAskResult(r);
        $('askState').textContent = '完成 · ' + (r.elapsed || 0) + ' s';
      })
      .catch(function (e) {
        $('askResult').innerHTML = '<div class="alert alert-error">' +
          esc(e.message || String(e)) + '</div>';
        $('askState').textContent = '失败';
      })
      .then(function () {                // 成功/失败都要恢复按钮，允许继续查询
        askBusy = false;
        btn.disabled = false;
        btn.textContent = '查询';
      });
  }

  /* ------------------------------ 配置 / 日志 / 系统 ------------------------------ */
  /* 群聊对话延续：配置写在 config.yaml，保存即写回、立即生效 */
  function loadFollowup() {
    return api('/api/followup').then(function (d) {
      var mins = Number(d.window_minutes);
      if (!(mins >= 1 && mins <= 30)) mins = 3;
      $('followupEnabled').checked = !!d.enabled;
      $('followupMinutes').value = mins;
      $('followupHintEnabled').checked = !!d.hint_enabled;
      $('followupHint').value = (d.hint == null) ? '' : d.hint;
      $('followupStatus').textContent = '当前生效：' + (d.enabled ? '开启' : '关闭') +
        ' · ' + mins + ' 分钟 · 仅同一群友' +
        ' · 提示：' + (d.hint_preview || '不显示') +
        ' · 活跃窗口 ' + (d.active_windows || 0);
    }).catch(fail);
  }
  function loadConfig(masked) {
    api('/api/config?redact=' + (masked ? '1' : '0')).then(function (d) {
      $('cfgText').value = d.text || '';
      $('cfgStatus').textContent = d.path || '';
      renderBackups(d.backups || []);
    }).catch(fail);
  }
  function refreshBackups() {
    // 只重绘备份表格，绝不重新给 cfgText 赋值 —— 用户可能正编辑到一半，
    // 点一下「删除备份」不该把编辑内容冲掉。
    return api('/api/config?redact=1').then(function (d) {
      renderBackups(d.backups || []);
    });
  }
  function renderBackups(backups) {
    $('cfgBackupBody').innerHTML = backups.length ? backups.map(function (b) {
      var n = (typeof b === 'string') ? b : (b.name || '');
      return '<tr><td>' + esc(n) + '</td><td>' +
        '<button class="btn btn-ghost btn-sm" data-cfgview="' + esc(n) + '">查看</button> ' +
        '<button class="btn btn-warn btn-sm" data-cfgroll="' + esc(n) + '">回滚</button> ' +
        '<button class="btn btn-warn btn-sm" data-cfgdel="' + esc(n) + '">删除</button></td></tr>';
    }).join('') : emptyRow(2, '暂无备份');
    $$('[data-cfgview]').forEach(function (b) {
      b.addEventListener('click', function () {
        api('/api/config/backup/' + encodeURIComponent(b.dataset.cfgview)).then(function (r) {
          $('cfgText').value = r.text; toast('已载入备份内容，点保存才会写回');
        }).catch(fail);
      });
    });
    $$('[data-cfgroll]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (!confirm('确定回滚到该备份？')) return;
        api('/api/config/rollback', { method: 'POST', body: { name: b.dataset.cfgroll } })
          .then(function () { toast('已回滚'); loadConfig(false); }).catch(fail);
      });
    });
    $$('[data-cfgdel]').forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.dataset.cfgdel;
        if (!confirm('确定删除备份「' + name + '」？删除后不可恢复。')) return;
        api('/api/config/backup/' + encodeURIComponent(name), { method: 'DELETE' })
          .then(function (d) { toast(d.message || '已删除'); refreshBackups(); })
          .catch(fail);
      });
    });
  }
  var logTab = 'runtime', logData = { text: '', admin_text: '' };
  var logAutoTimer = null;      // 自动刷新定时器；null = 未开启
  function setLogAuto(on, silent) {
    if (logAutoTimer !== null && typeof clearInterval === 'function') {
      clearInterval(logAutoTimer);
    }
    logAutoTimer = null;
    if (on) {
      logAutoTimer = setInterval(function () {
        // 切到别的页面时暂停轮询（按钮状态保留，回来自动继续），
        // 避免后台一直发无意义的请求
        if (currentView === 'logs') loadLog();
      }, 5000);
    }
    var b = $('btnLogAuto');
    b.textContent = on ? '停止刷新' : '自动刷新';
    b.classList.toggle('btn-warn', on);
    b.classList.toggle('btn-ghost', !on);
    b.title = on ? '每 5 秒自动刷新中，点击停止' : '开启后每 5 秒自动刷新一次日志';
    if (!silent) toast(on ? '已开启自动刷新（每 5 秒）' : '已停止自动刷新');
  }
  function renderLog() {
    var box = $('logBox');
    var raw = (logTab === 'admin' ? logData.admin_text : logData.text) || '（暂无日志）';
    var kw = (($('logFilter') && $('logFilter').value) || '').trim();
    if (kw) {
      var low = kw.toLowerCase();
      var lines = raw.split('\n').filter(function (l) {
        // 分组标题行始终保留，方便知道当前看的是哪份日志
        return l.indexOf('=====') >= 0 || l.toLowerCase().indexOf(low) >= 0;
      });
      raw = lines.length ? lines.join('\n') : '（没有匹配「' + kw + '」的日志行）';
    }
    box.textContent = raw;
    // 日志按时间正序输出，查看时自动定位到最新一行（两个标签与自动刷新都走这里）
    box.scrollTop = box.scrollHeight;
  }
  function loadLog() {
    api('/api/log').then(function (d) { logData = d; renderLog(); }).catch(fail);
  }
  function loadSystem() {
    api('/api/sysinfo').then(function (d) {
      $('sysInfo').innerHTML = kv([
        ['平台', d.platform], ['Python', d.python], ['项目目录', d.base_dir],
        ['配置文件', d.config_exists ? '已创建' : '缺失（使用 example）'],
        ['数据库', d.db_path + '（' + Math.round(d.db_size / 1024) + ' KB）'],
        ['知识库文档', d.knowledge_docs], ['向量检索', d.vector ? '开启' : '关闭（纯 BM25）']
      ]);
      $('sysDeps').innerHTML = Object.keys(d.deps || {}).map(function (k) {
        return '<tr><td><code>' + esc(k) + '</code></td><td>' + tag(d.deps[k], '已安装', '缺失') +
          '</td><td>' + (d.deps[k] ? '—' : '可选或按需安装，见 requirements.txt') + '</td></tr>';
      }).join('');
    }).catch(fail);
  }

  /* ------------------------------ 插件管理（P2 / P4） ------------------------------ */
  var plgItems = [], plgTaskItems = [], plgTaskEdit = null;

  function loadPlugins() {
    return api('/api/plugins').then(function (d) {
      renderPlugins(d.plugins || {}, d);
      return d;
    }).catch(fail);
  }

  function renderPlugins(p, d) {
    var items = p.plugins || [];
    plgItems = items;
    var nEnabled = items.filter(function (x) { return x.enabled; }).length;
    $('plgSwitch').textContent = p.enabled ? '已开启' : '已关闭';
    $('plgSwitchFoot').textContent = (d && d.degraded) ? '主服务不可用'
      : ('config.yaml · plugins.enabled=' + (p.enabled ? 'true' : 'false'));
    $('plgEnabled').textContent = nEnabled;
    $('plgEnabledFoot').textContent = '共 ' + items.length + ' 个插件';
    $('plgHandlers').textContent = p.handlers || 0;
    $('plgErrors').textContent = (p.errors || []).length;
    $('plgErrorsFoot').textContent = p.loaded_at
      ? '扫描于 ' + new Date(p.loaded_at * 1000).toLocaleString('zh-CN')
      : '尚未加载';
    $('plgCount').textContent = items.length;

    var warn = (d && d.hint) || (d && d.warning) || p.reload_error || '';
    $('plgWarning').textContent = warn ? ('提示：' + warn) : '';
    $('plgWarning').classList.toggle('hidden', !warn);

    $('plgBody').innerHTML = items.length ? items.map(function (x) {
      return '<tr' + (x.enabled ? '' : ' class="row-unmanaged"') + '>' +
        '<td><strong>' + esc(x.name) + '</strong>' +
          (x.description ? '<div class="muted small">' + esc(x.description) + '</div>' : '') +
        '</td>' +
        '<td>' + esc(x.version || '—') +
          '<div class="muted small">' + esc(x.author || '—') + '</div></td>' +
        '<td>' + tag(!!x.enabled, '启用', '停用') +
          (x.disabled_reason ? '<div class="muted small plg-reason">' +
            esc(x.disabled_reason) + '</div>' : '') + '</td>' +
        '<td><input type="number" class="plg-pri" data-plgpri="' + esc(x.name) +
          '" value="' + Number(x.priority || 0) + '" min="-100" max="100" step="1" ' +
          'title="留空恢复 manifest / 装饰器声明的默认优先级"></td>' +
        '<td>' + (x.hooks || []).map(function (h) {
          return '<span class="chip">' + esc(h) + '</span>';
        }).join(' ') + '</td>' +
        '<td>' + (x.fail_count ? '<span class="tag tag-fail">' +
          Number(x.fail_count) + ' 次</span>' : '0') + '</td>' +
        '<td class="small">' + esc(x.source || '—') + '</td>' +
        '<td><button class="btn btn-ghost btn-sm" data-plgtoggle="' + esc(x.name) +
          '">' + (x.enabled ? '禁用' : '启用') + '</button> ' +
        '<button class="btn btn-ghost btn-sm" data-plgcfg="' + esc(x.name) + '">配置</button>' +
        /* 「启用」本身就会清零熔断计数，所以禁用时不再重复给一个做同样事情的按钮；
           「清除熔断」只出现在「仍启用但已有失败计数」的行里，两者语义不重叠。 */
        ((x.enabled && x.fail_count) ? ' <button class="btn btn-warn btn-sm" data-plgreset="' +
          esc(x.name) + '" title="启用并清零连续失败计数与上次错误">清除熔断</button>' : '') + '</td></tr>';
    }).join('') : emptyRow(8, (d && d.degraded)
      ? '主服务未启动，请先在「运行监控」启动主服务'
      : '插件目录为空，或 config.yaml 的插件总开关未开启');

    var errs = (p.errors || []).map(function (e) { return ['加载错误', e]; });
    items.forEach(function (x) {
      if (x.fail_count || x.disabled_reason) {
        errs.push([x.name, '连续失败 ' + (x.fail_count || 0) + ' 次' +
          (x.disabled_reason ? ' · ' + x.disabled_reason : '')]);
      }
    });
    $('plgErrorList').innerHTML = errs.length ? kv(errs)
      : '<div class="empty">没有加载错误，也没有插件被熔断</div>';

    var sel = $('plgCfgName'), prev = sel.value;
    sel.innerHTML = items.length ? items.map(function (x) {
      return '<option value="' + esc(x.name) + '">' + esc(x.name) + '</option>';
    }).join('') : '<option value="">（暂无插件）</option>';
    if (items.some(function (x) { return x.name === prev; })) sel.value = prev;

    // P3/P4：定时任务。优先用主服务的「任务实例视图」，旧版主服务回落到调度器快照。
    var sch = p.scheduler || {}, tm = p.task_manager || {}, runtime = {};
    (sch.tasks || []).forEach(function (t) { runtime[t.key] = t; });
    var tItems = tm.items ? tm.items : (sch.tasks || []).map(function (t) {
      return { key: t.key, plugin: t.plugin, task: t.task, label: t.task,
               enabled: !t.disabled, plugin_disabled: false, custom: false,
               base_key: '', source: 'code', schedule: t.schedule,
               spec: { kind: t.kind || 'interval', seconds: 60 } };
    });
    plgTaskItems = tItems;
    var taskOn = tItems.filter(function (t) { return t.enabled; }).length;
    $('plgTaskCount').textContent = tItems.length;
    var foot;
    if (!tItems.length) {
      // 空列表最容易让人误以为功能坏了：区分「插件被停用」「代码没声明」「总开关关着」
      var anyOn = (p.plugins || []).some(function (x) { return x.enabled; });
      foot = !p.enabled
        ? '插件总开关未开启（config.yaml · plugins.enabled）'
        : (anyOn
            ? '调度器正常，但当前启用的插件没有声明 @schedule 定时任务'
            : '暂无任务：所有插件均已停用，请先在「插件清单」启用一个带定时任务的插件（如 ExamplePlugin）');
    } else {
      foot = sch.running
        ? ('运行中 · 单 tick 线程（pid ' + (sch.pid || '—') + '，间隔 ' +
           (sch.tick_seconds || 1) + 's，已 tick ' + (sch.ticks || 0) + ' 次）· 启用 ' +
           taskOn + '/' + tItems.length + ' 个任务')
        : (p.enabled ? '调度器未运行（检查 config.yaml · plugins.scheduler.enabled）'
                     : '插件总开关未开启');
    }
    if (tm.error) foot += ' · 配置异常：' + tm.error;
    $('plgTaskFoot').textContent = foot;

    // 顶部状态条：三个关键开关一眼可见
    setPlgBadge('plgSchBadge', sch.running ? '运行中' : '未运行', !!sch.running);
    var hrx = p.http_routes || {};
    setPlgBadge('plgExtBadge', hrx.enabled ? '/ext 已开启' : '/ext 已关闭', !!hrx.enabled);
    $('plgTaskBadge').textContent = taskOn + ' / ' + tItems.length +
      (tm.instance_count ? '（含 ' + tm.instance_count + ' 个自定义实例）' : '');

    // 「复制自」只列代码声明的任务，实例不能再被复制（避免引用链）
    var decls = tItems.filter(function (t) { return !t.custom; });
    var fsel = $('plgTaskFrom'), prevFrom = fsel.value;
    fsel.innerHTML = decls.length ? decls.map(function (t) {
      return '<option value="' + esc(t.key) + '">' + esc(t.label + ' · ' + t.key) + '</option>';
    }).join('') : '<option value="">（没有已声明的任务）</option>';
    if (decls.some(function (t) { return t.key === prevFrom; })) fsel.value = prevFrom;

    $('plgTaskBody').innerHTML = tItems.length ? tItems.map(function (t) {
      var st = runtime[t.key] || {};
      var src = t.source === 'instance' ? '<span class="plg-source instance">实例</span>'
              : t.source === 'override' ? '<span class="plg-source override">已改配置</span>' : '';
      return '<tr' + (t.enabled ? '' : ' class="row-unmanaged"') + '>' +
        '<td><strong>' + esc(t.label || t.task || '—') + '</strong>' + src +
          '<div class="muted small">' + esc(t.key || '') + '</div>' +
          (t.base_key ? '<div class="muted small">复制自 ' + esc(t.base_key) + '</div>' : '') +
        '</td>' +
        '<td>' + esc(t.plugin || '—') + '</td>' +
        '<td>' + esc(t.schedule || plgSpecText(t.spec)) + '</td>' +
        '<td>' + tag(!!t.enabled, '启用', '已停用') +
          (t.plugin_disabled ? '<div class="muted small plg-reason">插件已停用</div>' : '') + '</td>' +
        '<td class="small">' + (st.next_run
          ? new Date(st.next_run * 1000).toLocaleString('zh-CN') : '—') + '</td>' +
        '<td>' + Number(st.runs || 0) + ' / ' +
          (Number(st.fails || 0)
            ? '<span class="tag tag-fail">' + Number(st.fails) + '</span>' : '0') + '</td>' +
        '<td class="small">' + (st.last_run ? (Number(st.last_ms || 0) + ' ms') : '—') + '</td>' +
        '<td class="small">' + (st.last_error
          ? '<span class="plg-reason">' + esc(st.last_error) + '</span>' : '—') + '</td>' +
        '<td class="plg-actions">' + plgTaskButtons(t) + '</td></tr>';
    }).join('') : emptyRow(9, '暂无定时任务（插件未启用，或代码里没有 @schedule 声明）');

    // P3：HTTP 扩展路由清单（默认关闭时也要如实标灰）
    var hr = p.http_routes || {}, rts = hr.routes || [], prefix = hr.prefix || '/ext';
    $('plgRouteCount').textContent = rts.length;
    $('plgRouteFoot').textContent = hr.enabled
      ? ('已开启 · 前缀 ' + prefix + ' · ' +
         (hr.require_internal_token ? '强制 X-Internal-Token' : '未强制令牌（不安全）'))
      : '未开启（config.yaml · plugins.http_routes.enabled=false）';
    $('plgRouteBody').innerHTML = rts.length ? rts.map(function (r) {
      var full = prefix + '/' + (r.plugin || '') + (r.path || '');
      return '<tr><td>' + esc(r.plugin || '—') + '</td>' +
        '<td><span class="chip">' + esc(r.method || 'GET') + '</span></td>' +
        '<td><code>' + esc(full) + '</code></td>' +
        '<td>' + tag(!!hr.enabled, '可访问', '未开启') + '</td></tr>';
    }).join('') : emptyRow(4, '没有插件声明扩展路由');
    bindPluginRows();
  }

  function postPlugin(body, msg) {
    return api('/api/plugins', { method: 'POST', body: body }).then(function (d) {
      renderPlugins(d.plugins || {}, d);
      toast(msg);
      if (d.warning) toast(d.warning, 'warn');
    }).catch(function (e) { fail(e); loadPlugins(); });
  }

  function bindPluginRows() {
    $$('[data-plgtoggle]').forEach(function (b) {
      b.addEventListener('click', function () {
        var row = plgItems.filter(function (x) { return x.name === b.dataset.plgtoggle; })[0];
        if (row) postPlugin({ name: row.name, enabled: !row.enabled },
                            (row.enabled ? '已禁用 ' : '已启用 ') + row.name);
      });
    });
    $$('[data-plgreset]').forEach(function (b) {
      b.addEventListener('click', function () {
        postPlugin({ name: b.dataset.plgreset, enabled: true },
                   '已清除 ' + b.dataset.plgreset + ' 的熔断计数');
      });
    });
    $$('[data-plgcfg]').forEach(function (b) {
      b.addEventListener('click', function () {
        // 配置面板在另一个 tab，只加载内容不切过去会像「点了没反应」。
        switchPlgTab('config');
        $('plgCfgName').value = b.dataset.plgcfg;
        loadPluginConfig();
      });
    });
    $$('[data-plgpri]').forEach(function (el) {
      el.addEventListener('change', function () {
        var v = String(el.value || '').trim();
        postPlugin({ name: el.dataset.plgpri, priority: v === '' ? null : Number(v) },
                   '优先级已更新');
      });
    });
    $$('[data-plgtaskedit]').forEach(function (b) {
      b.addEventListener('click', function () {
        var t = plgTaskItems.filter(function (x) {
          return x.key === b.dataset.plgtaskedit; })[0];
        if (t) openPlgTaskForm(t);
      });
    });
    $$('[data-plgtasktoggle]').forEach(function (b) {
      b.addEventListener('click', function () {
        postPlgTask({ action: 'toggle', key: b.dataset.plgtasktoggle,
                      enabled: b.dataset.on === '1' }, '任务状态已更新');
      });
    });
    $$('[data-plgtaskdel]').forEach(function (b) {
      b.addEventListener('click', function () {
        var key = b.dataset.plgtaskdel;
        var t = plgTaskItems.filter(function (x) { return x.key === key; })[0] || {};
        if (!confirm(t.custom
              ? '确定删除实例「' + key + '」？删除后不可恢复。'
              : '确定移除任务「' + key + '」？插件代码声明仍在，可随时点「恢复」还原。')) return;
        postPlgTask({ action: 'delete', key: key }, '已移除任务');
      });
    });
    $$('[data-plgtaskrestore]').forEach(function (b) {
      b.addEventListener('click', function () {
        postPlgTask({ action: 'restore', key: b.dataset.plgtaskrestore }, '已恢复任务');
      });
    });
  }

  function loadPluginConfig() {
    var name = $('plgCfgName').value;
    if (!name) { $('plgCfgStatus').textContent = '请先选择一个插件'; return; }
    return api('/api/plugins/config?name=' + encodeURIComponent(name))
      .then(function (d) {
        $('plgCfgText').value = d.text || '';
        $('plgCfgStatus').textContent = (d.exists ? '已读取 ' : '尚无私有配置，可在此新建 ')
          + (d.path || '');
      }).catch(fail);
  }

  function savePluginConfig() {
    var name = $('plgCfgName').value;
    if (!name) { fail(new Error('请先选择一个插件')); return; }
    return api('/api/plugins/config', { method: 'POST',
      body: { name: name, text: $('plgCfgText').value } })
      .then(function (d) {
        $('plgCfgStatus').textContent = d.message || '已保存';
        toast(d.message || '已保存');
      }).catch(fail);
  }

  /* ------------------------------ P4 · 定时任务实例 ------------------------------ */
  function setPlgBadge(id, text, ok) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    var box = el.parentNode;
    if (!box || !box.classList) return;
    box.classList.toggle('is-ok', !!ok);
    box.classList.toggle('is-off', !ok);
  }

  function plgSpecText(spec) {
    var s = spec || {};
    if (s.kind === 'daily') {
      return '每天 ' + ('0' + Number(s.hour || 0)).slice(-2) + ':' +
             ('0' + Number(s.minute || 0)).slice(-2);
    }
    return '每 ' + Number(s.seconds || 60) + ' 秒' +
      (s.config_key ? '（可由 ' + s.config_key + ' 覆盖）' : '');
  }

  function plgTaskButtons(t) {
    var k = esc(t.key);
    var s = '<button class="btn btn-ghost btn-sm" data-plgtaskedit="' + k + '">编辑</button> ';
    s += '<button class="btn btn-ghost btn-sm" data-plgtasktoggle="' + k +
         '" data-on="' + (t.enabled ? '0' : '1') + '">' +
         (t.enabled ? '停用' : '启用') + '</button> ';
    if (t.custom) {
      s += '<button class="btn btn-warn btn-sm" data-plgtaskdel="' + k + '">删除</button>';
    } else if (!t.enabled) {
      s += '<button class="btn btn-ghost btn-sm" data-plgtaskrestore="' + k + '">恢复</button>';
    } else {
      s += '<button class="btn btn-warn btn-sm" data-plgtaskdel="' + k + '">移除</button>';
    }
    return s;
  }

  function switchPlgTab(name) {
    $$('#plgTabs .plg-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.plgtab === name);
    });
    $$('.plg-panel').forEach(function (p) {
      p.classList.toggle('hidden', p.dataset.plgpanel !== name);
    });
  }

  function syncPlgTaskKind() {
    var daily = $('plgTaskKind').value === 'daily';
    $('plgTaskIntervalRow').classList.toggle('hidden', daily);
    $('plgTaskDailyRow').classList.toggle('hidden', !daily);
  }

  function openPlgTaskForm(t) {
    var isNew = !t;
    // 复制源只能取代码声明的任务（实例不能再被复制）。插件全被停用时注册表
    // 不加载任何任务，此时打开新增表单保存必然失败，先把原因讲清楚。
    var decls = plgTaskItems.filter(function (x) { return !x.custom; });
    if (isNew && !decls.length) {
      fail(new Error('当前没有可复制的任务声明：请先在「插件清单」启用一个带定时任务的插件'));
      return;
    }
    plgTaskEdit = t || null;
    $('plgTaskFormTitle').textContent = isNew ? '新增任务实例'
      : ((t.custom ? '编辑实例 · ' : '编辑任务 · ') + t.key);
    $('plgTaskFormHint').textContent = isNew
      ? '新实例复用所复制任务的插件函数，但有独立的周期与运行计数。'
      : '周期改动会覆盖插件代码声明值；「移除」后可用「恢复」还原。';
    var fsel = $('plgTaskFrom');
    fsel.value = isNew ? (fsel.value || (decls[0] || {}).key || '')
                        : (t.base_key || t.key);
    if (!fsel.value) fsel.selectedIndex = 0;
    fsel.disabled = !isNew;
    $('plgTaskSuffix').value = isNew ? '' : (t.key.split('#')[1] || '');
    $('plgTaskSuffix').disabled = !isNew;
    $('plgTaskLabel').value = isNew ? '' : (t.label || '');
    var spec = isNew ? { kind: 'interval', seconds: 60 } : (t.spec || {});
    $('plgTaskKind').value = spec.kind === 'daily' ? 'daily' : 'interval';
    $('plgTaskSeconds').value = Number(spec.seconds || 60);
    $('plgTaskHour').value = Number(spec.hour || 9);
    $('plgTaskMinute').value = Number(spec.minute || 0);
    $('plgTaskEnabled').checked = isNew ? true : !!t.enabled;
    syncPlgTaskKind();
    $('plgTaskForm').classList.remove('hidden');
    switchPlgTab('tasks');
    if (isNew && typeof $('plgTaskSuffix').focus === 'function') {
      $('plgTaskSuffix').focus();
    }
  }

  function savePlgTaskForm() {
    var isNew = !plgTaskEdit;
    var from = $('plgTaskFrom').value;
    if (!from) { fail(new Error('请先选择要复制的任务')); return; }
    var suffix = String($('plgTaskSuffix').value || '').trim();
    if (isNew && !/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(suffix)) {
      fail(new Error('实例标识只能含字母、数字、下划线，且以字母开头'));
      return;
    }
    return postPlgTask({
      action: 'save', key: isNew ? (from + '#' + suffix) : plgTaskEdit.key,
      from: from, label: String($('plgTaskLabel').value || '').trim(),
      enabled: $('plgTaskEnabled').checked, kind: $('plgTaskKind').value,
      seconds: Number($('plgTaskSeconds').value || 60),
      hour: Number($('plgTaskHour').value || 0),
      minute: Number($('plgTaskMinute').value || 0)
    }, isNew ? '已新增任务实例' : '已保存任务设置');
  }

  function postPlgTask(body, msg) {
    return api('/api/plugins/tasks', { method: 'POST', body: body })
      .then(function (d) {
        if (d.plugins) renderPlugins(d.plugins, d);
        toast(d.message || msg);
        $('plgTaskForm').classList.add('hidden');
        plgTaskEdit = null;
        return loadPlugins();
      }).catch(function (e) { fail(e); loadPlugins(); });
  }

  /* ------------------------------ 朋友圈（手机插件 v3） ------------------------------
     插件侧不存动态：gethead 重置到最新、getnext 追加一页，游标由微信维护。
     所以这里自己累积 items（按 tid 去重），并按「时间线 / 好友」记住当前视图；
     媒体一律走 /api/moments/media 代理，绝不把 CDN 直链或 token 交给浏览器。 */
  var momItems = [], momAtEnd = false, momBusy = false,
      momSig = '', momFriendsLoaded = false,
      momWriting = {},              // idx -> 'like' / 'comment'：写操作期间锁住该卡片与翻页
      momSource = 'timeline',       // 当前页签：timeline / user
      momSelfWxid = '',             // 插件 /api/health 里的 self，用来认出「本人已赞」那条
      momBlobs = {},                // tid:index -> blob: URL，媒体地址不落到 DOM / 地址栏
      momMediaMeta = {};            // key -> {tid,index,isVideo}，灯箱与播放共用

  function momSourceOf() {
    return momSource === 'user' ? 'user' : 'timeline';
  }
  function momSignature() {
    return momSourceOf() + '|' + (momSourceOf() === 'user'
      ? String($('momUser').value || '').trim() : '');
  }
  function syncMomSourceUI() {
    var byUser = momSourceOf() === 'user';
    $('momUserBar').classList.toggle('hidden', !byUser);
    $$('.mom-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.momsource === momSourceOf());
    });
  }
  function switchMomSource(src) {
    var next = src === 'user' ? 'user' : 'timeline';
    if (momBusy || momHasWriting()) { toast('请等待当前操作完成再切换', 'warn'); return; }
    momSource = next;
    momItems = []; momAtEnd = false; momSig = ''; momWriting = {};
    momRevokeBlobs(); momMediaMeta = {}; momCloseViewer();
    syncMomSourceUI();
    if (next === 'user') {
      $('momFoot').textContent = '好友动态 · 请选择好友';
      renderMoments();                 // 等用户选人，不主动发空 user 请求
    } else {
      $('momFoot').textContent = '读取中…';
      renderMoments();
      momFetch('head');                // 点页签（含重复点当前页签）即刷新到最新
    }
  }
  function loadMomFriends() {
    return api('/api/moments/friends').then(function (d) {
      var items = d.items || [], prev = $('momUser').value;
      $('momFriend').innerHTML = '<option value="">选择好友</option>' +
        items.map(function (f) {
          return '<option value="' + esc(f.wxid) + '">' + esc(f.name || f.wxid) +
            '（' + esc(f.wxid) + '）</option>';
        }).join('');
      if (items.some(function (f) { return f.wxid === prev; })) {
        $('momFriend').value = prev;
      }
      momFriendsLoaded = true;
    }).catch(fail);
  }
  function loadMoments() {
    syncMomSourceUI();
    var p = momFriendsLoaded ? Promise.resolve() : loadMomFriends();
    return p.then(function () {
      // 首次进入 / 换了查看对象时自动拉最新一页；同一对象下保留已累积的动态
      if (!momItems.length || momSignature() !== momSig) return momFetch('head');
      renderMoments();
    });
  }

  function momFetch(action) {
    if (momBusy) return;
    // head 会清空 momItems，写操作回调还持着旧对象引用，两者必须互斥
    if (momHasWriting()) { toast('有操作正在进行，请稍候', 'warn'); return; }
    var src = momSourceOf(),
        user = src === 'user' ? String($('momUser').value || '').trim() : '';
    if (src === 'user' && !user) {
      $('momFoot').textContent = '请先选择好友';
      toast('请先选择好友', 'warn');
      return;
    }
    momBusy = true;
    $('momFoot').textContent = action === 'head' ? '读取中…' : '加载中…';
    renderMoments();
    var url = '/api/moments/feed?source=' + encodeURIComponent(src) +
      '&action=' + action + '&user=' + encodeURIComponent(user) + '&media=1';
    return api(url).then(function (d) {
      if (action === 'head') {
        momItems = [];
        momWriting = {};
        momRevokeBlobs();               // 旧页的 blob: 全部释放，避免内存堆积
        momMediaMeta = {};
        momCloseViewer();
      }
      var seen = {};
      momItems.forEach(function (m) { seen[m.tid] = 1; });
      (d.items || []).forEach(function (m) {
        if (m && m.tid && !seen[m.tid]) { momItems.push(m); seen[m.tid] = 1; }
      });
      if (d.selfWxid) momSelfWxid = String(d.selfWxid);
      momAtEnd = !!d.atEnd;
      momSig = momSignature();
      // 状态栏只留「来源 · 已加载 N 条」：好友模式不再重复长 wxid，
      // 总数 / 可继续加载 / stopReason 都是冗余提示（加载按钮本身已表达状态）
      var foot = (src === 'user' ? '好友' : '时间线') +
        ' · 已加载 ' + momItems.length + ' 条' +
        (momAtEnd ? ' · 已到末尾' : '');
      $('momFoot').textContent = foot;
      momBusy = false;
      renderMoments();
    }).catch(function (e) {
      momBusy = false;
      $('momFoot').textContent = '读取失败：' + (e.message || String(e));
      renderMoments();
      fail(e);
    });
  }

  function momTime(t) {
    var n = Number(t) || 0;
    return n ? new Date(n * 1000).toLocaleString('zh-CN') : '—';
  }
  function momIsMe(x) {
    if (!x) return false;
    var id = String(x.wxid || x.username || '');
    return !!(x.isSelf || id === 'me' ||
              (momSelfWxid && id === momSelfWxid));
  }
  function momNames(list) {
    return (list || []).map(function (x) {
      if (momIsMe(x)) return '我';
      return (x && (x.nickname || x.wxid)) || '';
    }).filter(Boolean);
  }
  // 媒体（原图 / 封面 / 转码视频）一律先经后台代理取成 blob:，再交给 <img>/<video>。
  // 页面上不会出现任何 /api/moments/... 查询串，也不开新标签，地址栏不会暴露媒体地址。
  function momMediaPath(kind, tid, index) {
    var base = kind === 'cover' ? '/api/moments/cover'
             : kind === 'video' ? '/api/moments/video'
             : '/api/moments/media';
    return base + '?tid=' + encodeURIComponent(tid) +
      '&index=' + encodeURIComponent(index);
  }
  function momFetchBlob(path, cacheKey) {
    if (momBlobs[cacheKey]) return Promise.resolve(momBlobs[cacheKey]);
    return fetch(path, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) {
        if (r.status === 401 || r.redirected) {
          location.href = '/login'; throw new Error('登录已过期，请重新登录');
        }
        if (Number(r.status) >= 400) throw new Error('媒体读取失败（HTTP ' + r.status + '）');
        var ct = (r.headers && r.headers.get) ? (r.headers.get('Content-Type') || '') : '';
        if (ct && !/^(image|video)\//.test(ct)) throw new Error('返回内容不是媒体文件');
        return r.blob();
      }).then(function (b) {
        var U = (typeof window !== 'undefined' && (window.URL || window.webkitURL)) ||
                (typeof URL !== 'undefined' ? URL : null);
        if (!U || !U.createObjectURL) throw new Error('浏览器不支持本地媒体预览');
        var u = U.createObjectURL(b);
        momBlobs[cacheKey] = u;
        return u;
      });
  }
  function momBlob(tid, index, kind) {
    kind = kind || 'media';
    return momFetchBlob(momMediaPath(kind, tid, index),
                        kind + '|' + tid + '|' + index);
  }
  // 发布者头像也走后台代理：同一 username 复用一个 blob，卡片里不出现 CDN 直链
  function momAvatarBlob(user) {
    return momFetchBlob('/api/moments/avatar?user=' + encodeURIComponent(user),
                        'avatar|' + user);
  }
  function momRevokeBlobs() {
    var U = (typeof window !== 'undefined' && (window.URL || window.webkitURL)) ||
            (typeof URL !== 'undefined' ? URL : null);
    Object.keys(momBlobs).forEach(function (k) {
      if (U && U.revokeObjectURL) { try { U.revokeObjectURL(momBlobs[k]); } catch (e) {} }
    });
    momBlobs = {};
  }
  function momHydrate() {
    // 图片缩略图与视频封面在此取 blob；视频本体等用户点开灯箱才取，避免预载大 mp4。
    $$('#momList [data-momhydrate]').forEach(function (el) {
      var m = momMediaMeta[el.getAttribute('data-momhydrate')];
      if (!m) return;
      var kind = el.getAttribute('data-momkind') || 'media';
      momBlob(m.tid, m.index, kind).then(function (u) {
        el.src = u;
      }).catch(function () {
        if (kind === 'cover') el.style.display = 'none';
        else el.classList.add('mom-media-failed');
      });
    });
    // 发布者头像：取不到就隐藏 <img>，露出底下的昵称首字母
    $$('#momList [data-momavatar]').forEach(function (el) {
      var u = el.getAttribute('data-momavatar');
      if (!u) return;
      momAvatarBlob(u).then(function (url) {
        el.src = url;
      }).catch(function () {
        el.style.display = 'none';
      });
    });
  }
  var momViewerKey = '', momViewerTimer = null, momSavedScrollY = 0;
  // 视频灯箱：所有点击都交给原生播放器，只靠上滑 / 下滑（或桌面按钮、Esc）关闭
  var momViewerIsVideo = false;
  function momCloseViewer() {
    momViewerKey = '';
    momViewerIsVideo = false;
    if (momViewerTimer) { clearTimeout(momViewerTimer); momViewerTimer = null; }
    var box = $('momViewerBox');
    if (box) {
      var v = box.querySelector ? box.querySelector('video') : null;
      if (v && typeof v.pause === 'function') { try { v.pause(); } catch (e) {} }
      box.innerHTML = '';                 // 立即停止视频播放
    }
    $('momViewerHint').textContent = '';
    $('momViewer').classList.add('hidden');
    // 真机上全屏灯箱可能带着背景一起滚：关掉后回到打开前的位置
    if (momSavedScrollY && typeof window !== 'undefined' &&
        typeof window.scrollTo === 'function') {
      try { window.scrollTo(0, momSavedScrollY); } catch (e) {}
    }
    momSavedScrollY = 0;
  }
  // 微信朋友圈视频可能是 HEVC：浏览器能解 AAC 音轨但解不了视频轨，表现为
  // 「有声音无图像」（videoWidth 一直是 0）。检测到就自动换后端 H.264 转码版。
  function momViewerVideoCheck(v, key, m) {
    if (typeof v.videoWidth !== 'number') return;   // 非真实 DOM，不做检测
    var done = false;
    var transcode = function () {
      if (done || momViewerKey !== key) return;
      done = true;
      if (momViewerTimer) { clearTimeout(momViewerTimer); momViewerTimer = null; }
      $('momViewerHint').textContent = '浏览器无法解码原视频，正在转换为兼容格式…';
      momBlob(m.tid, m.index, 'video').then(function (u) {
        if (momViewerKey !== key) return;
        v.src = u;
        if (typeof v.load === 'function') v.load();
        if (typeof v.play === 'function') { try { v.play(); } catch (e) {} }
        $('momViewerHint').textContent = '已切换到兼容格式';
      }).catch(function (e) {
        if (momViewerKey === key) {
          $('momViewerHint').textContent = '画面无法解码：' + (e.message || '转换失败');
        }
      });
    };
    // 元数据一加载就知道有没有画面，通常 1 秒内触发，不必等定时兜底
    if (v.addEventListener) {
      ['loadedmetadata', 'playing'].forEach(function (ev) {
        try {
          v.addEventListener(ev, function () {
            if (v.videoWidth === 0) transcode();
          });
        } catch (e) {}
      });
    }
    var tries = 0;
    var tick = function () {
      if (done || momViewerKey !== key) return;
      if (v.videoWidth > 0) { $('momViewerHint').textContent = ''; return; }
      if (++tries < 5) { momViewerTimer = setTimeout(tick, 700); return; }
      momViewerTimer = null;
      transcode();
    };
    momViewerTimer = setTimeout(tick, 700);
  }
  function momOpenViewer(key) {
    var m = momMediaMeta[key];
    if (!m) return;
    momViewerKey = key;
    momViewerIsVideo = !!m.isVideo;
    $('momViewerCap').textContent = m.label;
    $('momViewerHint').textContent = '';
    $('momViewerBox').innerHTML = '<div class="mom-viewer-loading">正在加载…</div>';
    momSavedScrollY = (typeof window !== 'undefined' && window.pageYOffset) || 0;
    $('momViewer').classList.remove('hidden');
    // 视频先取封面当 poster，加载期间不会是纯黑块
    var poster = (m.isVideo && m.hasCover)
      ? momBlob(m.tid, m.index, 'cover').catch(function () { return ''; })
      : Promise.resolve('');
    Promise.all([momBlob(m.tid, m.index), poster]).then(function (r) {
      if (momViewerKey !== key) return;
      $('momViewerBox').innerHTML = m.isVideo
        ? '<video id="momViewerVideo" src="' + esc(r[0]) + '"' +
          (r[1] ? ' poster="' + esc(r[1]) + '"' : '') +
          ' controls autoplay playsinline webkit-playsinline x5-playsinline></video>'
        : '<img src="' + esc(r[0]) + '" alt="' + esc(m.label) + '">';
      if (m.isVideo) {
        var v = $('momViewerVideo');
        if (v) momViewerVideoCheck(v, key, m);
      }
    }).catch(function (e) {
      if (momViewerKey === key) {
        $('momViewerBox').innerHTML = '<div class="mom-viewer-loading">' +
          esc(e.message || '读取失败') + '</div>';
      }
    });
  }
  function momHasWriting() {
    return Object.keys(momWriting).length > 0;
  }
  function syncMomTopButtons() {
    var writing = momHasWriting();
    $('momCount').textContent = momItems.length;
    $('btnMomNext').disabled = momBusy || writing || momAtEnd || !momItems.length;
  }
  function momLikeBtnText(m, i) {
    if (momWriting[i]) return '处理中…';
    return m.likeFlag ? '取消赞' : '点赞';
  }
  // 点赞 / 评论 / 评论名单都放在这一块里，写成功后只换它的 innerHTML：
  // 图片和视频节点完全不重建，不会重新加载、不会重置播放、不会跳滚动位置。
  function momSocialHtml(m, i) {
    var likes = momNames(m.likes),
        comments = (m.comments || []).filter(function (c) { return c && c.content; }),
        // 不能用 likeCount || likeUserCount || likes.length：取消赞后 likeCount=0，
        // || 会把它当成假值回退到名单长度，界面上人数看起来完全没变。
        nLike = Math.max(Number(m.likeCount || m.likeUserCount || 0) || 0,
                        likes.length),
        html = '';
    if (likes.length || nLike > 0) {
      html += '<div class="mom-likes"><span class="mom-ico">♥</span>' +
        (likes.length ? esc(likes.join('、')) + ' 等 ' + nLike + ' 人点赞'
                      : nLike + ' 人点赞') + '</div>';
    }
    if (comments.length) {
      html += '<div class="mom-comments">' + comments.map(function (c) {
        var del = '';
        if (momIsMe(c)) {
          // 插件 8.11 只接受 commentId / content 定位；刚本地追加的评论还没有
          // commentId，刷新后才会带回来，此时如实提示而不是给个点了会失败的按钮。
          del = c.commentId
            ? ' <button type="button" class="mom-comment-del" data-momdel="' + i +
              '|' + esc(c.commentId) + '"' + (momWriting[i] ? ' disabled' : '') +
              '>删除</button>'
            : ' <span class="mom-comment-hint">刷新后可删除</span>';
        }
        return '<div class="mom-comment"><strong>' +
          esc(c.nickname || c.wxid || '匿名') + '：</strong>' + esc(c.content) +
          (c.pending ? ' <span class="mom-pending">微信未确认</span>' : '') +
          del + '</div>';
      }).join('') + '</div>';
    }
    return html;
  }
  function refreshMomCard(i) {
    var m = momItems[i];
    if (!m) return;
    var social = $('momSocial' + i);
    if (social) {
      var html = momSocialHtml(m, i);
      social.innerHTML = html;
      social.classList.toggle('hidden', !html);
    }
    var lb = $('momLikeBtn' + i), cb = $('momCommentBtn' + i);
    if (lb) {
      lb.textContent = momLikeBtnText(m, i);
      lb.className = 'btn ' + (m.likeFlag ? 'btn-ghost' : 'btn-primary') + ' btn-sm';
      lb.disabled = momBusy || !!momWriting[i];
    }
    if (cb) {
      cb.textContent = momWriting[i] === 'comment' ? '提交中…' : '发表';
      cb.disabled = momBusy || !!momWriting[i];
    }
    var inp = $('momInput' + i);
    if (inp) inp.disabled = momBusy || !!momWriting[i];
  }
  function momBeginWrite(i, kind) {
    momWriting[i] = kind;
    syncMomTopButtons();
    refreshMomCard(i);                 // 立刻灰掉按钮，避免重复点击
  }
  function momEndWrite(i, flash) {
    delete momWriting[i];
    syncMomTopButtons();
    refreshMomCard(i);                 // 恢复按钮 + 展示最新的赞 / 评论
    if (flash) {
      var s = $('momSocial' + i);
      if (s && s.classList) s.classList.add('mom-updated');
      setTimeout(function () {
        if (s && s.classList) s.classList.remove('mom-updated');
      }, 1800);
    }
  }

  function renderMoments() {
    syncMomTopButtons();
    if (!momItems.length) {
      var waitingUser = momSourceOf() === 'user' &&
        !String($('momUser').value || '').trim();
      $('momList').innerHTML = '<div class="card"><div class="empty">' +
        (momBusy ? '读取中…' : (waitingUser ? '请选择好友' : '暂无动态')) +
        '</div></div>';
      return;
    }
    $('momList').innerHTML = momItems.map(function (m, i) {
      var name = String(m.nickname || m.username || '未知'),
          tid = String(m.tid || ''),
          txt = String(m.contentDesc || m.contentTitle || '').trim(),
          media = m.media || [];
      var mediaHtml = media.length ? '<div class="mom-media">' + media.map(function (x) {
        var n = Number(x.index) + 1, key = i + '_' + n,
            hasCover = !!x.hasCover,
            caption = (x.isVideo ? '视频 ' : '图片 ') + n;
        momMediaMeta[key] = { tid: tid, index: x.index, isVideo: !!x.isVideo,
                              hasCover: hasCover, label: caption };
        // 没有 href / src 直链：data-momview 只带内部 key，实际地址由 momBlob 生成。
        // 缩略图与封面异步填 blob:；点图片或视频都在全屏灯箱查看。
        var hit = '<button type="button" class="mom-media-hit" data-momview="' + key +
          '" aria-label="全屏查看' + esc(caption) + '" title="全屏查看">';
        if (x.isVideo) {
          // 封面也走 /api/moments/cover 代理（后端不带 Referer 取缩略图，失败抽视频首帧），
          // 不再把 CDN 直链塞进 src —— 域名白名单 / 直链过期 / 混合内容都会让封面空白。
          return '<figure class="mom-media-item is-video"><div class="mom-video-frame">' +
            hit +
            (hasCover
              ? '<img class="mom-video-cover" data-momhydrate="' + key +
                '" data-momkind="cover" alt="视频封面" loading="lazy">'
              : '<span class="mom-video-cover mom-video-nocover" aria-hidden="true">▶</span>') +
            '<span class="mom-video-play static" aria-hidden="true"></span></button>' +
            '</div><figcaption>' + caption +
            (x.videoDuration ? ' · ' + Number(x.videoDuration) + ' 秒' : '') +
            ' · 全屏播放</figcaption></figure>';
        }
        return '<figure class="mom-media-item">' + hit +
          '<img class="mom-thumb" data-momhydrate="' + key + '" alt="' + esc(caption) +
          '" loading="lazy" draggable="false"></button>' +
          '<figcaption>' + caption + ' · 全屏查看</figcaption></figure>';
      }).join('') + '</div>' : '';
      var social = momSocialHtml(m, i),
          w = momWriting[i] || '',
          lock = momBusy || !!w;
      // 头像只带 data-momavatar，src 由 hydrate 填 blob:；拿不到就退回首字母
      var avatar = (m.hasAvatar && m.username)
        ? '<img data-momavatar="' + esc(String(m.username)) +
          '" alt="" draggable="false">'
        : '';
      return '<article class="mom-card" data-momidx="' + i + '">' +
        '<div class="mom-head"><div class="mom-avatar"><i>' + esc(name.slice(0, 1)) +
          '</i>' + avatar + '</div><div class="mom-who"><strong>' + esc(name) + '</strong>' +
          '<span class="muted small">' + esc(m.username || '') + ' · ' +
          esc(momTime(m.createtime)) + '</span></div>' +
          '<span class="mom-no">#' + (i + 1) + '</span></div>' +
        (txt ? '<div class="mom-text">' + esc(txt) + '</div>' : '') +
        mediaHtml +
        '<div class="mom-social' + (social ? '' : ' hidden') + '" id="momSocial' + i +
          '">' + social + '</div>' +
        '<div class="mom-actions"><button class="btn ' +
          (m.likeFlag ? 'btn-ghost' : 'btn-primary') + ' btn-sm" id="momLikeBtn' + i +
          '" data-momact="like" data-momidx="' + i + '"' + (lock ? ' disabled' : '') +
          '>' + esc(momLikeBtnText(m, i)) + '</button>' +
          '<input type="text" class="mom-input" id="momInput' + i +
          '" maxlength="1000" placeholder="写评论"' +
          (lock ? ' disabled' : '') + '>' +
          '<button class="btn btn-ghost btn-sm" id="momCommentBtn' + i +
          '" data-momact="comment" data-momidx="' + i + '"' + (lock ? ' disabled' : '') +
          '>' + (w === 'comment' ? '提交中…' : '发表') + '</button></div></article>';
    }).join('');
    momHydrate();                   // 缩略图异步填 blob:，媒体地址不出现在 DOM 里
  }

  // 写成功后本地追加 / 移除「我」：插件不会把刚发生的动作推回来，而 head 会重置列表，
  // 所以这一步必须做，否则点了赞 / 发了评论后界面上什么都看不到。
  // 取消赞的关键：插件返回的「本人已赞」是真实 wxid（后端已标 isSelf，feed 也透出
  // selfWxid），不能只过滤本地假条目 'me'，否则原点赞名单纹丝不动。
  function momApplyLike(m, unlike) {
    var likes = (m.likes || []).filter(function (x) { return x && !momIsMe(x); });
    if (unlike) {
      m.likeFlag = 0;
    } else {
      m.likeFlag = 1;
      likes.push({ wxid: momSelfWxid || 'me', nickname: '我', isSelf: 1 });
    }
    m.likes = likes;
    m.likeCount = likes.length;
    m.likeUserCount = likes.length;
  }
  function momApplyComment(m, text, confirmed) {
    m.comments = (m.comments || []).concat([{
      wxid: 'me', nickname: '我', content: text,
      createTime: Math.floor(Date.now() / 1000),
      pending: confirmed === false
    }]);
    m.commentCount = Number(m.commentCount || 0) + 1;
  }
  function momLike(i, unlike) {
    var m = momItems[i];
    if (!m) return;
    if (momWriting[i]) { toast('这条动态的操作正在进行，请稍候', 'warn'); return; }
    if (momBusy) { toast('正在读取朋友圈，请稍候', 'warn'); return; }
    var who = String(m.nickname || m.username || '这条动态');
    if (!confirm(unlike
        ? '确定取消对「' + who + '」这条动态的赞？\n这是真实社交动作。'
        : '确定给「' + who + '」点赞？\n这是真实社交动作，对方会收到通知。')) return;
    momBeginWrite(i, 'like');
    return api('/api/moments/like', { method: 'POST',
      body: { tid: m.tid, unlike: unlike } }).then(function (d) {
        momApplyLike(m, unlike);
        momEndWrite(i, true);
        toast(d.message || (unlike ? '已取消赞' : '已点赞'));
      }).catch(function (e) {
        momEndWrite(i, false);
        fail(e);
      });
  }
  function momComment(i) {
    var m = momItems[i];
    if (!m) return;
    if (momWriting[i]) { toast('这条动态的操作正在进行，请稍候', 'warn'); return; }
    if (momBusy) { toast('正在读取朋友圈，请稍候', 'warn'); return; }
    var el = $('momInput' + i), text = String((el && el.value) || '').trim();
    if (!text) { toast('请先输入评论内容', 'warn'); return; }
    if (!confirm('确定发表这条评论？\n\n「' + text +
        '」\n\n评论是真实社交动作，对方会收到通知；发表后可在本条评论旁删除。')) return;
    momBeginWrite(i, 'comment');
    return api('/api/moments/comment', { method: 'POST',
      body: { tid: m.tid, content: text } }).then(function (d) {
        momApplyComment(m, text, d.confirmed);
        momEndWrite(i, true);
        var inp = $('momInput' + i);
        if (inp) inp.value = '';
        toast(d.message || '评论已发表');
      }).catch(function (e) {
        momEndWrite(i, false);
        fail(e);
      });
  }
  function momDeleteComment(i, commentId) {
    var m = momItems[i];
    if (!m || !commentId) return;
    if (momWriting[i]) { toast('这条动态的操作正在进行，请稍候', 'warn'); return; }
    if (momBusy) { toast('正在读取朋友圈，请稍候', 'warn'); return; }
    if (!confirm('确定删除这条本人评论？\n删除是真实社交动作且不可撤回。')) return;
    momBeginWrite(i, 'delete');
    return api('/api/moments/rm_comment', { method: 'POST',
      body: { tid: m.tid, commentId: commentId } }).then(function (d) {
        m.comments = (m.comments || []).filter(function (c) {
          return String((c && c.commentId) || '') !== String(commentId);
        });
        m.commentCount = Math.max(0, Number(m.commentCount || 0) - 1);
        m.commentUserCount = m.commentCount;
        momEndWrite(i, true);
        toast(d.message || '评论已删除');
      }).catch(function (e) {
        momEndWrite(i, false);
        fail(e);
      });
  }

  /* ------------------------------ 事件绑定 ------------------------------ */
  /* 移动端抽屉：汉堡开合、点遮罩 / Esc 关闭、回到桌面宽度自动复位 */
  ensureMobileChrome();
  var menuBtn = $('btnMenu');
  if (menuBtn && typeof menuBtn.addEventListener === 'function') {
    menuBtn.addEventListener('click', function () {
      var sb = sidebarEl();
      var opened = !!(sb && sb.classList && sb.classList.contains &&
                      sb.classList.contains('open'));
      setDrawer(!opened);
    });
  }
  var maskEl = $('sidebarMask');
  if (maskEl && typeof maskEl.addEventListener === 'function') {
    maskEl.addEventListener('click', function () { setDrawer(false); });
  }
  if (typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') setDrawer(false);
    });
  }
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('resize', function () {
      if (!isNarrow()) setDrawer(false);
      syncMomViewportH();
    });
  }
  enhanceMobileTables();

  $$('.nav-item[data-view]').forEach(function (n) {
    n.addEventListener('click', function () { switchView(n.dataset.view); });
  });
  $$('.nav-parent').forEach(function (n) {
    n.addEventListener('click', function () {
      var g = n.dataset.groupToggle;             // 只展开/收起自己这一组
      expandNavGroup(g, !n.classList.contains('open'));
    });
  });
  $('btnRefresh').addEventListener('click', function () {
    // 朋友圈是累积列表：顶栏刷新要真的回到最新一页，不能只重绘缓存
    if (currentView === 'moments') { momFetch('head'); return; }
    switchView(currentView);
  });
  // 朋友圈：页签 / head-next 累积 / 视频封面 / 真实社交动作二次确认
  $('momTabTimeline').addEventListener('click', function () { switchMomSource('timeline'); });
  $('momTabUser').addEventListener('click', function () { switchMomSource('user'); });
  $('momFriend').addEventListener('change', function () {
    if ($('momFriend').value) {
      $('momUser').value = $('momFriend').value;
      momFetch('head');
    }
  });
  $('momUser').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') momFetch('head');
  });
  $('btnMomNext').addEventListener('click', function () { momFetch('next'); });
  $('momViewerClose').addEventListener('click', momCloseViewer);
  // 视频播放时画面 / 控件 / 遮罩的点击一律不关闭，否则播放、暂停、拖进度条
  // 一点就退出，播放器没法用；视频只靠上滑或下滑关闭，桌面另有按钮与 Esc。
  // 图片仍保持「点一下画面或空白即关闭」。
  function momViewerClickClose() {
    if (momViewerIsVideo) return;
    momCloseViewer();
  }
  $('momViewerMask').addEventListener('click', momViewerClickClose);
  var viewerBox = $('momViewerBox');
  if (viewerBox && typeof viewerBox.addEventListener === 'function') {
    viewerBox.addEventListener('click', momViewerClickClose);
  }
  var viewerRoot = $('momViewer');
  if (viewerRoot && typeof viewerRoot.addEventListener === 'function') {
    var momTouchY = 0, momTouchX = 0;
    viewerRoot.addEventListener('touchstart', function (e) {
      var p = e && e.touches && e.touches[0];
      momTouchY = p ? p.clientY : 0;
      momTouchX = p ? p.clientX : 0;
    }, { passive: true });
    viewerRoot.addEventListener('touchend', function (e) {
      var p = e && e.changedTouches && e.changedTouches[0];
      if (!p) return;
      var dy = p.clientY - momTouchY, dx = p.clientX - momTouchX;
      // 上滑或下滑都能退出；横向滑动（拖进度条等）不误触
      if (Math.abs(dy) > 70 && Math.abs(dx) < Math.abs(dy)) momCloseViewer();
    }, { passive: true });
  }
  if (document.addEventListener) {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('momViewer').classList.contains('hidden')) momCloseViewer();
    });
  }
  $('momList').addEventListener('click', function (e) {
    var v = e.target && e.target.closest ? e.target.closest('[data-momview]') : null;
    if (v && v.dataset.momview) { momOpenViewer(v.dataset.momview); return; }
    var del = e.target && e.target.closest ? e.target.closest('[data-momdel]') : null;
    if (del && del.dataset.momdel) {
      var parts = del.dataset.momdel.split('|');
      momDeleteComment(Number(parts[0]), parts.slice(1).join('|'));
      return;
    }
    var b = e.target && e.target.closest ? e.target.closest('[data-momact]') : null;
    if (!b) return;
    var i = Number(b.dataset.momidx);
    if (b.dataset.momact === 'like') momLike(i, !!(momItems[i] || {}).likeFlag);
    else if (b.dataset.momact === 'comment') momComment(i);
  });
  // 运行监控：7 / 30 / 90 天切换，所有图表共用同一个时间窗口
  $$('#rangePicker .chip').forEach(function (c) {
    c.addEventListener('click', function () {
      $$('#rangePicker .chip').forEach(function (x) { x.classList.remove('active'); });
      c.classList.add('active');
      chartDays = parseInt(c.dataset.days, 10) || 30;
      loadStats();
    });
  });
  $$('.modal-mask[data-close]').forEach(function (m) {
    m.addEventListener('click', function () {
      // 表单可能叠在详情弹窗上（通讯录里点「改备注」）：遮罩只关最上层，
      // 否则关掉输入弹窗时连详情一起消失，用户得重新点进来。
      if (!$('formModal').classList.contains('hidden')) { closeForm(); return; }
      closeDetail(); $('pwdModal').classList.add('hidden');
    });
  });
  $('formCancel').addEventListener('click', closeForm);
  $('detailClose').addEventListener('click', closeDetail);
  // 索引重建进度：关闭只是收起窗口，任务继续跑，轮询不停（完成时仍会 toast）
  $('rebuildClose').addEventListener('click', dismissRebuildModal);
  $('formSave').addEventListener('click', function () {
    if (!formSave || $('formSave').disabled) return;
    var data = collectForm(window.__formFields || []);
    $('formError').classList.add('hidden');
    setFormBusy(true);
    Promise.resolve(formSave(data)).catch(function (e) {
      $('formError').textContent = e.message || String(e);
      $('formError').classList.remove('hidden');
    }).then(function () {
      setFormBusy(false);           // 成功时弹窗已关，复位给下一次用
    });
  });
  $('cfmOk').addEventListener('click', function () { closeConfirm(true); });
  $('cfmCancel').addEventListener('click', function () { closeConfirm(false); });
  $('confirmModal').querySelector('.modal-mask').addEventListener('click', function () {
    closeConfirm(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && cfmResolve) { closeConfirm(false); return; }
    // 确认框开着时，回车 = 确定
    if (e.key === 'Enter' && cfmResolve && e.target && e.target.tagName !== 'TEXTAREA') {
      closeConfirm(true);
    }
  });

  // 运行监控
  $('btnSvcStart').addEventListener('click', function () { serviceAction('start'); });
  $('btnSvcStop').addEventListener('click', function () { serviceAction('stop'); });
  $('btnSvcRestart').addEventListener('click', function () { serviceAction('restart'); });
  $('btnSvcRefresh').addEventListener('click', function () { loadService(); toast('已刷新状态'); });
  $('btnApiStart').addEventListener('click', function () { apiSvcAction('start'); });
  $('btnApiRestart').addEventListener('click', function () { apiSvcAction('restart'); });
  $('btnApiStop').addEventListener('click', function () { apiSvcAction('stop'); });
  $('btnApiRefresh').addEventListener('click', function () {
    loadApiService(); toast('已刷新状态');
  });
  // 用户输入后标记为 dirty，避免 loadDashboard 的自动回显覆盖正在编辑的内容
  ['hookUrl', 'alertUrl'].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener('input', function () { el.dataset.dirty = '1'; });
  });
  $('btnSetHook').addEventListener('click', function () {
    var url = ($('hookUrl').value || '').trim();
    api('/api/contacts/webhook', { method: 'POST', body: { url: url } }).then(function (d) {
      toast(d.message); $('hookUrl').dataset.dirty = ''; loadDashboard();
    }).catch(fail);
  });
  $('btnSetAlert').addEventListener('click', function () {
    var url = ($('alertUrl').value || '').trim();
    api('/api/contacts/alert', { method: 'POST', body: { url: url } }).then(function (d) {
      toast(d.message); $('alertUrl').dataset.dirty = ''; loadDashboard();
    }).catch(fail);
  });
  $('btnTestAlert').addEventListener('click', function () {
    var url = ($('alertUrl').value || '').trim();
    api('/api/contacts/alert', { method: 'POST', body: { url: url, test: true } }).then(function (d) {
      toast(d.message); loadDashboard();
    }).catch(fail);
  });
  $('btnSyncContacts').addEventListener('click', syncContacts);
  $('btnReloadIndex').addEventListener('click', function () {
    startRebuild();
  });
  $('btnSyncDocs').addEventListener('click', function () {
    api('/api/policies/sync-docs', { method: 'POST', body: {} }).then(function (d) {
      toast(d.message); loadDashboard();
    }).catch(fail);
  });

  // 主动刷新授权：卡片里的授权状态默认跟 /api/status 一起取，
  // 客户刚在微信里续期后可以点这里立刻回读，不必刷新整页。
  (function () {
    var b = $('btnRefreshLicense');
    if (!b) return;
    b.addEventListener('click', function () {
      b.disabled = true;
      api('/api/plugin/license')
        .then(function (d) {
          var lic = d.license || {};
          toast(lic.stateText || ('授权状态：' + (lic.state || '未知')));
          loadDashboard();
        })
        .catch(fail)
        .then(function () { b.disabled = false; });
    });
  })();

  // 手机插件激活码：插件协议里空码 = 清除授权，这里三层拦截（required / JS / 后台）
  (function () {
    var form = $('activateForm'), input = $('activateCode'), btn = $('btnActivate');
    // 插件会自动忽略空格 / 换行 / 连字符，所以「---」这类也算空码
    function nonEmpty(v) { return String(v || '').replace(/[\s\-]+/g, ''); }
    function syncBtn() { btn.disabled = !nonEmpty(input.value); }
    input.addEventListener('input', syncBtn);
    syncBtn();
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var code = input.value.trim();
      if (!nonEmpty(code)) {
        toast('激活码不能为空（空码会清除当前授权）', 'warn');
        input.focus();
        return;
      }
      btn.disabled = true;
      api('/api/plugin/activate', { method: 'POST', body: { code: code } })
        .then(function (d) {
          toast(d.message || '激活成功');
          input.value = '';
          loadDashboard();
        })
        .catch(fail)
        .then(syncBtn);
    });
  })();

  // 对话记录
  if ($('btnMediaSearch')) $('btnMediaSearch').addEventListener('click', function () { mediaPage = 1; loadMedia(); });
  if ($('btnMediaReset')) $('btnMediaReset').addEventListener('click', function () { resetMediaFilters(); loadMedia(); });
  if ($('mediaPrev')) $('mediaPrev').addEventListener('click', function () {
    if (mediaPage > 1) { mediaPage--; loadMedia(); }
  });
  if ($('mediaNext')) $('mediaNext').addEventListener('click', function () {
    if (mediaPage < mediaMaxPage) { mediaPage++; loadMedia(); }
  });
  if ($('mediaPageSize')) $('mediaPageSize').addEventListener('change', function () { mediaPage = 1; loadMedia(); });
  ['mediaChatName', 'mediaChat', 'mediaWxid'].forEach(function (id) {
    if ($(id)) $(id).addEventListener('keydown', function (e) { if (e.key === 'Enter') { mediaPage = 1; loadMedia(); } });
  });
  $('btnRecSearch').addEventListener('click', function () { recPage = 1; loadRecords(); });
  $('btnRecReset').addEventListener('click', function () {
    ['recStart', 'recEnd', 'recKind', 'recChatType', 'recKeyword'].forEach(function (id) {
      $(id).value = '';
    });
    recPage = 1; loadRecords();
  });
  $('recPrev').addEventListener('click', function () { if (recPage > 1) { recPage--; loadRecords(); } });
  $('recNext').addEventListener('click', function () {
    if (recPage < recMaxPage) { recPage++; loadRecords(); }
  });
  // 聊天记录（独立消息库）
  $('btnChatSearch').addEventListener('click', function () { chatPage = 1; loadChatLog(); });
  $('btnChatReset').addEventListener('click', function () {
    resetChatFilters(); loadChatLog();
  });
  $('btnChatExport').addEventListener('click', function () {
    location.href = '/api/chat-messages/export?' + chatParams();
  });
  $('btnChatPurge').addEventListener('click', function () {
    if (!confirm('确定立即清理超过保留期的聊天记录？')) return;
    api('/api/chat-messages/purge', { method: 'POST', body: {} })
      .then(function (d) { toast(d.message); loadChatLog(); }).catch(fail);
  });
  $('chatPrev').addEventListener('click', function () {
    if (chatPage > 1) { chatPage--; loadChatLog(); }
  });
  $('chatNext').addEventListener('click', function () {
    if (chatPage < chatMaxPage) { chatPage++; loadChatLog(); }
  });
  $('btnRecExport').addEventListener('click', function () {
    location.href = '/api/records/export?' + recParams();
  });
  ['recStart', 'recEnd', 'recKind', 'recChatType'].forEach(function (id) {
    $(id).addEventListener('change', function () { recPage = 1; loadRecords(); });
  });
  $('recKeyword').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { recPage = 1; loadRecords(); }
  });

  // AI 用量
  $('btnLlmSearch').addEventListener('click', function () { llmPage = 1; loadLlm(); });
  $('btnLlmReset').addEventListener('click', function () {
    ['llmStart', 'llmEnd'].forEach(function (id) { $(id).value = ''; });
    $('llmModel').value = ''; $('llmDays').value = '30';
    llmPage = 1; loadLlm();
  });
  $('llmPrev').addEventListener('click', function () {
    if (llmPage > 1) { llmPage--; loadLlmRecords(); }
  });
  $('llmNext').addEventListener('click', function () {
    if (llmPage < llmMaxPage) { llmPage++; loadLlmRecords(); }
  });
  $('btnLlmExport').addEventListener('click', function () {
    location.href = '/api/llm-records/export?' + llmParams();
  });
  ['llmStart', 'llmEnd', 'llmDays', 'llmModel'].forEach(function (id) {
    $(id).addEventListener('change', function () { llmPage = 1; loadLlm(); });
  });

  // API 调用记录
  $('btnApiSearch').addEventListener('click', function () { apiPage = 1; loadApi(); });
  $('btnApiReset').addEventListener('click', function () {
    ['apiStart', 'apiEnd', 'apiKeyword'].forEach(function (id) { $(id).value = ''; });
    $('apiOnlyFailed').value = '';
    $('apiDays').value = '30';
    apiPage = 1; loadApi();
  });
  $('apiPrev').addEventListener('click', function () {
    if (apiPage > 1) { apiPage--; loadApiRecords(); }
  });
  $('apiNext').addEventListener('click', function () {
    if (apiPage < apiMaxPage) { apiPage++; loadApiRecords(); }
  });
  $('btnApiExport').addEventListener('click', function () {
    location.href = '/api/api-calls/export?' + apiParams();
  });
  ['apiStart', 'apiEnd', 'apiDays', 'apiOnlyFailed'].forEach(function (id) {
    $(id).addEventListener('change', function () { apiPage = 1; loadApi(); });
  });
  $('apiKeyword').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { apiPage = 1; loadApi(); }
  });

  // 群聊 / 私聊 / 配置
  bindSortHeaders('#view-groups', grpSort, renderGroupRows);
  bindSortHeaders('#view-private', prvSort, renderPrivateRows);
  $('btnGrpNew').addEventListener('click', function () { editGroup(null); });
  $('btnGrpSync').addEventListener('click', function () {
    syncContacts();
  });
  $('btnPrvNew').addEventListener('click', function () { editPrivate(null); });
  $('btnDenySave').addEventListener('click', function () {
    api('/api/global-deny', { method: 'POST', body: { items: $('globalDeny').value } })
      .then(function (d) { toast(d.message); }).catch(fail);
  });
  $('btnPolNew').addEventListener('click', function () { editPolicy(null); });
  $('btnPolSync').addEventListener('click', function () {
    api('/api/policies/sync-docs', { method: 'POST', body: {} }).then(function (d) {
      toast(d.message); refreshOptions().then(loadPolicies);
    }).catch(fail);
  });

  // 通讯录
  $('tabGroups').addEventListener('click', function () { ctSwitchTab('groups'); });
  $('tabFriends').addEventListener('click', function () { ctSwitchTab('friends'); });
  $('tabSearch').addEventListener('click', function () { ctSwitchTab('search'); });
  $('btnCtSync').addEventListener('click', syncContacts);
  $('btnCtSearch').addEventListener('click', searchContacts);
  $('ctSearchInput').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); searchContacts(); }
  });
  $('btnCtSearchClear').addEventListener('click', clearCtSearch);
  $('ctSearchBody').addEventListener('click', function (e) {
    var b = e.target && e.target.closest
      ? e.target.closest('[data-ctsfr],[data-ctsmem],[data-ctsfrremark]') : null;
    if (!b) return;
    if (b.dataset.ctsfrremark) {
      var r = ctSearchRows.filter(function (x) { return x.wxid === b.dataset.ctsfrremark; })[0] || {};
      editFriendRemark(b.dataset.ctsfrremark, r.remark || '', function () {
        searchContacts(); loadCtFriends();
      });
    } else if (b.dataset.ctsfr) showFriend(b.dataset.ctsfr);
    else if (b.dataset.ctsmem) showGroupMembers(b.dataset.ctsmem);
  });

  // 知识库
  $('btnKbNew').addEventListener('click', function () {
    openForm('新建知识库文档', [
      { key: 'name', label: '文件名', placeholder: '产品FAQ.md',
        help: '不写后缀会自动补 .md；创建后立即出现在左侧列表，并默认加入「默认知识库」' }
    ], { name: '新文档.md' }, function (data) {
      var name = normDocName(data.name || '新文档.md');
      var initial = '# ' + name.replace(/\.(md|txt)$/i, '') + '\n\n在这里写知识库内容。\n';
      return api('/api/kb/file', { method: 'POST', body: { name: name, content: initial } })
        .then(function () {
          closeForm();
          kbCurrent = name;
          $('kbFileName').value = name; $('kbEditor').value = initial;
          resetKbDirty(); setKbMode('edit');
          toast('已新建「' + name + '」，编辑后点「保存」');
          loadKnowledge(); refreshOptions();
        });
    });
  });
  // ⚠️ 不能直接写成 addEventListener('click', saveKb)：那样浏览器会把
  // MouseEvent 当成 saveKb(onDone) 的回调，保存成功后调用事件对象 →
  // "onDone is not a function"。必须包一层显式不传参。
  $('btnKbSave').addEventListener('click', function () { saveKb(); });
  $('btnKbPreview').addEventListener('click', function () { setKbMode('preview'); });
  $('btnKbSplit').addEventListener('click', function () { setKbMode('split'); });
  $('btnKbEdit').addEventListener('click', function () { setKbMode('edit'); });
  $('kbEditor').addEventListener('dblclick', function () { setKbMode('edit'); });
  $('kbEditor').addEventListener('input', function () {
    markKbDirty();
    kbStatUpdate();
    // 输入时节流渲染分栏/预览，避免每敲一个字就解析全文
    clearTimeout(kbEditorPreviewTimer);
    kbEditorPreviewTimer = setTimeout(function () { updatePreview(); }, 350);
  });
  // Markdown 快捷插入
  $('btnMdBold').addEventListener('click', function () { insertMd('**', '**', '重点'); });
  $('btnMdHead').addEventListener('click', function () { insertMd('\n## ', '\n', '小标题'); });
  $('btnMdList').addEventListener('click', function () { insertMd('\n- ', '', '列表项'); });
  $('btnMdTable').addEventListener('click', function () {
    insertMd('\n| 列1 | 列2 |\n| --- | --- |\n| ', ' |  |', '内容');
  });
  $('btnMdCode').addEventListener('click', function () { insertMd('\n```\n', '\n```\n', 'code'); });
  // 知识库图片库
  $('btnKbImages').addEventListener('click', openKbImagePicker);
  $('btnKbInsertImg').addEventListener('click', openKbImagePicker);
  $('kbImgClose').addEventListener('click', function () { $('kbImgDlg').classList.add('hidden'); });
  $('kbImgMask').addEventListener('click', function () { $('kbImgDlg').classList.add('hidden'); });
  $('btnKbImgPick').addEventListener('click', function () { $('kbImgInput').click(); });
  $('kbImgInput').addEventListener('change', function () {
    uploadKbImages(this.files);
    this.value = '';
  });
  (function bindImgDrop() {
    var drop = $('kbImgDrop');
    if (!drop) return;
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        drop.classList.add('dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        drop.classList.remove('dragover');
      });
    });
    drop.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files) uploadKbImages(e.dataTransfer.files);
    });
  })();
  $('kbFileName').addEventListener('input', markKbDirty);
  $('btnKbDelete').addEventListener('click', function () {
    deleteKbByName(kbCurrent || $('kbFileName').value || '');
  });
  $('btnKbUpload').addEventListener('click', function () { $('kbUploadInput').click(); });
  $('kbUploadInput').addEventListener('change', function () {
    var f = this.files[0]; if (!f) return;
    var fd = new FormData(); fd.append('file', f);
    api('/api/kb/upload', { method: 'POST', body: fd }).then(function (d) {
      toast(d.message); loadKnowledge(); refreshOptions(); openKbFile(d.name);
    }).catch(fail);
    this.value = '';
  });
  $('btnKbImport').addEventListener('click', function () { $('kbImportInput').click(); });
  $('kbImportInput').addEventListener('change', function () {
    if (!this.files.length) return;
    var fd = new FormData();
    Array.prototype.forEach.call(this.files, function (f) { fd.append('files', f); });
    toast('正在导入，请稍候…');
    api('/api/kb/import', { method: 'POST', body: fd }).then(function (d) {
      toast(d.message);
      renderImportResults(d.results);
      loadKnowledge(); refreshOptions();
      var errs = (d.results || []).filter(function (x) { return !x.ok; });
      if (errs.length) openDetail('导入失败明细', '<pre class="log-box">' +
        esc(errs.map(function (x) { return x.name + ': ' + x.error; }).join('\n')) + '</pre>', '');
      // 导入成功后自动打开首个 md 并切到预览：抽出的图片立刻可见，
      // 不用用户再手动点文件、点预览（以前导入完只见一条 toast，容易以为图丢了）
      var first = (d.results || []).filter(function (x) {
        return x.ok && x.result && /\.md$/i.test(x.result.name || '');
      })[0];
      if (first) openKbFile(first.result.name, true);
      else loadKbImages();       // 没生成 md（全失败）时至少刷新一下图片库数据
    }).catch(fail);
    this.value = '';
  });
  $('btnAsk').addEventListener('click', runKbAsk);
  $('askInput').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { runKbAsk(); }
  });
  $('btnKbRebuild').addEventListener('click', function () {
    startRebuild();
  });
  // AI 大模型：新增 / 设为默认 / 回退 config / 从当前配置导入
  $('btnLlmNew').addEventListener('click', function () { editLlmModel(null); });
  $('btnLlmDefault').addEventListener('click', function () {
    api('/api/llm-models/default', { method: 'POST',
      body: { id: $('llmDefaultSel').value } })
      .then(function (d) { toast(d.message); renderLlmModels(d); }).catch(fail);
  });
  $('btnLlmUseConfig').addEventListener('click', function () {
    api('/api/llm-models/default', { method: 'POST', body: { id: 0 } })
      .then(function (d) { toast(d.message); renderLlmModels(d); }).catch(fail);
  });
  $('btnLlmImport').addEventListener('click', function () {
    api('/api/llm-models/import-config', { method: 'POST', body: {} })
      .then(function (d) { toast(d.message); renderLlmModels(d); }).catch(fail);
  });
  /* 大模型配置导出 / 导入。
     导出文件里带明文 API Key（不带的话导到新机器上不能用），所以导出前明确提醒一次；
     导入方式由旁边下拉决定：merge=同名覆盖、其余保留；replace=文件里没有的模型会被删掉。 */
  $('btnLlmExport').addEventListener('click', function () {
    if (!confirm('导出文件包含 API Key 明文，请妥善保管，不要发到公开群或提交到公开仓库。\n\n确定导出？')) return;
    window.location.href = '/api/llm-models/export';
    toast('已开始下载模型配置（含明文密钥）');
  });
  $('btnLlmImportFile').addEventListener('click', function () { $('llmImportInput').click(); });
  $('llmImportInput').addEventListener('change', function () {
    var f = this.files[0];
    if (!f) return;
    var mode = $('llmImportMode').value;
    if (mode === 'replace' &&
        !confirm('「全量替换」会删除文件里没有的模型，并把默认模型 / 群聊私聊模型改成文件里的设置。\n\n确定继续？')) {
      this.value = '';
      return;
    }
    var fr = new FileReader();
    fr.onload = function () {
      var payload;
      try { payload = JSON.parse(String(fr.result || '')); }
      catch (e) { toast('「' + f.name + '」不是合法的 JSON 文件', 'err'); return; }
      api('/api/llm-models/import', { method: 'POST', body: { data: payload, mode: mode } })
        .then(function (d) {
          toast(d.message);
          renderLlmModels(d);
          var miss = (d.result && d.result.missing) || [];
          if (miss.length) toast('有引用没生效：' + miss.join('；'), 'warn');
        }).catch(fail);
    };
    fr.onerror = function () { toast('读取文件失败', 'err'); };
    fr.readAsText(f);
    this.value = '';
  });
  // 分场景模型：群聊 / 私聊一次保存（0 = 跟随全局默认）
  $('btnLlmSceneSave').addEventListener('click', function () {
    api('/api/llm-models/scene', { method: 'POST', body: {
      group: $('llmSceneGroup').value, private: $('llmScenePrivate').value
    } }).then(function (d) { toast(d.message); renderLlmModels(d); }).catch(fail);
  });
  // 群聊 / 私聊配置页各自的「应答模型」保存
  $('btnGrpModelSave').addEventListener('click', function () {
    saveSceneModel('group', 'grpSceneModel');
  });
  $('btnPrvModelSave').addEventListener('click', function () {
    saveSceneModel('private', 'prvSceneModel');
  });

  // 配置
  $('btnFollowupSave').addEventListener('click', function () {
    var mins = parseInt($('followupMinutes').value, 10);
    if (!(mins >= 1 && mins <= 30)) { toast('窗口时长需在 1~30 分钟之间', 'err'); return; }
    var hint = $('followupHint').value;
    if (hint.length > 200) { toast('续聊提示文案不能超过 200 字', 'err'); return; }
    api('/api/followup', { method: 'POST', body: {
      enabled: $('followupEnabled').checked,
      window_minutes: mins,
      hint_enabled: $('followupHintEnabled').checked,
      hint: hint
    }}).then(function (d) { toast(d.message || '已保存，立即生效'); loadFollowup(); })
      .catch(fail);
  });
  $('cfgMask').addEventListener('change', function () { loadConfig($('cfgMask').checked); });
  $('btnCfgValidate').addEventListener('click', function () {
    api('/api/config/validate', { method: 'POST', body: { text: $('cfgText').value } })
      .then(function (d) {
        $('cfgStatus').textContent = d.valid ? '语法正确' : ('语法错误：' + d.error);
        if (d.warns && d.warns.length) toast(d.warns.join('；'), 'warn');
        else toast(d.valid ? '校验通过' : '校验未通过', d.valid ? '' : 'err');
      }).catch(fail);
  });
  $('btnCfgSave').addEventListener('click', function () {
    api('/api/config', { method: 'POST', body: { text: $('cfgText').value, tag: 'admin' } })
      .then(function (d) { toast(d.message); loadConfig(false); }).catch(fail);
  });

  // 插件管理
  $('btnPlgRefresh').addEventListener('click', loadPlugins);
  $('btnPlgTaskRefresh').addEventListener('click', loadPlugins);
  $('btnPlgReload').addEventListener('click', function () {
    api('/api/plugins/reload', { method: 'POST', body: {} }).then(function (d) {
      renderPlugins(d.plugins || {}, d);
      toast(d.message || '已重载');
      if (d.warning) toast(d.warning, 'warn');
    }).catch(fail);
  });
  $('btnPlgCfgLoad').addEventListener('click', loadPluginConfig);
  $('btnPlgCfgSave').addEventListener('click', savePluginConfig);
  // P4：分区 tab 与任务实例表单（只绑一次，表格行按钮由 bindPluginRows 逐次绑定）
  $$('#plgTabs .plg-tab').forEach(function (b) {
    b.addEventListener('click', function () { switchPlgTab(b.dataset.plgtab); });
  });
  $('btnPlgTaskNew').addEventListener('click', function () { openPlgTaskForm(null); });
  $('btnPlgTaskSave').addEventListener('click', savePlgTaskForm);
  $('btnPlgTaskCancel').addEventListener('click', function () {
    $('plgTaskForm').classList.add('hidden');
    plgTaskEdit = null;
  });
  $('plgTaskKind').addEventListener('change', syncPlgTaskKind);

  // 日志
  $('btnLogRefresh').addEventListener('click', loadLog);
  // 按会话名 / 发送者 / wxid / 关键字本地过滤，自动刷新时保持筛选条件
  $('logFilter').addEventListener('input', renderLog);
  // 自动刷新：默认已开启，按一下停、再按一下开。按钮文案与颜色同步反映状态。
  $('btnLogAuto').addEventListener('click', function () {
    setLogAuto(logAutoTimer === null);
  });
  $('btnLogClear').addEventListener('click', function () {
    if (!confirm('确定清空当前标签的日志？')) return;
    api('/api/log/clear', { method: 'POST',
      body: { which: logTab === 'admin' ? 'admin' : 'runtime' } })
      .then(function (d) { toast(d.message); loadLog(); }).catch(fail);
  });
  $('tabLogRuntime').addEventListener('click', function () {
    logTab = 'runtime';
    $('tabLogRuntime').classList.add('active'); $('tabLogAdmin').classList.remove('active');
    renderLog();
  });
  $('tabLogAdmin').addEventListener('click', function () {
    logTab = 'admin';
    $('tabLogAdmin').classList.add('active'); $('tabLogRuntime').classList.remove('active');
    renderLog();
  });
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('beforeunload', function (e) {
      if (!kbDirty) return;
      e.preventDefault(); e.returnValue = '';
    });
  }

  // 修改密码
  $('userRow').addEventListener('click', function () {
    collapseDrawer();       // 修改密码弹窗不能被抽屉遮罩盖住
    $('pwdModal').classList.remove('hidden');
  });
  $('pwdCancel').addEventListener('click', function () { $('pwdModal').classList.add('hidden'); });
  $('pwdSave').addEventListener('click', function () {
    api('/api/password', { method: 'POST', body: { old: $('pwdOld').value, new: $('pwdNew').value } })
      .then(function () { toast('密码已修改，请重新登录'); setTimeout(function () { location.href = '/login'; }, 900); })
      .catch(function (e) { $('pwdError').textContent = e.message; $('pwdError').classList.remove('hidden'); });
  });

  resetKbDirty(); setKbMode('edit');      // 初始状态：保存不可用、编辑模式
  setLogAuto(true, true);                // 运行日志默认每 5 秒自动刷新（静默开启，不弹提示）
  refreshOptions().then(function () { switchView('dashboard'); });
  setInterval(function () { if (currentView === 'dashboard') loadDashboard(); }, 20000);
})();
