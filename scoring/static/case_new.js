/* 新建用例页：按 markdown 文档结构填表 → /api/case/preview 实时预览 →
 * /api/case/create 落盘 md。左栏是文档（frontmatter + ## 分节），右栏是
 * 导出与 md 源码预览；动态行全部前端拼装，提交时按填写顺序重编号。 */
;(function () {
  var D = JSON.parse(document.getElementById('cn-data').textContent);
  var EC_DIM = '错误知识纠正';
  var previewTimer = null, previewSeq = 0, lastMd = '', liveLocked = false;

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function val(el) { return el ? el.value.trim() : ''; }
  function num(el) { var v = val(el); return v ? parseInt(v, 10) : null; }
  function flt(el) { var v = val(el); return v ? parseFloat(v) : null; }
  function kwSplit(s) { return String(s || '').split(/[、,，/／]+/).map(function (x) { return x.trim(); }).filter(Boolean); }
  function optHtml(list, cur) {
    return list.map(function (o) {
      return '<option value="' + esc(o) + '"' + (o === cur ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
  }
  function f(sel, root) { return (root || document).querySelector(sel); }
  function rowsOf(id) { return Array.prototype.slice.call($(id).children); }
  function row(html) {
    var d = document.createElement('div');
    d.className = 'cn-item';
    d.innerHTML = html;
    return d;
  }
  function rmBtn() { return '<button class="cn-rm" type="button" data-rm title="删除本行" aria-label="删除本行">✕</button>'; }
  function itemHead(left) { return '<div class="cn-item-head">' + (left || '') + rmBtn() + '</div>'; }
  function badge(cls) { return '<span class="cn-badge ' + cls + '"></span>'; }
  function micro(label, inner, cls) {
    return '<label class="cn-micro' + (cls ? ' ' + cls : '') + '"><span>' + label + '</span>' + inner + '</label>';
  }
  function wireRm(list) {
    list.querySelectorAll('[data-rm]').forEach(function (b) {
      b.onclick = function () {
        b.closest('.cn-item').remove();
        renumber(); refreshEvOptions(); schedulePreview();
      };
    });
  }
  function turnData() {
    var users = [], refs = [];
    rowsOf('cn-turns').forEach(function (r) {
      var u = val(f('[data-f=user]', r));
      if (u) { users.push(u); refs.push(val(f('[data-f=assistant]', r))); }
    });
    return { users: users, refs: refs };
  }
  function updateDialogName() {
    var t = turnData();
    var name = t.users.length > 1 && !t.refs.some(Boolean) && !t.users.some(function (u) { return u.indexOf('\n') >= 0; })
      ? '对话' : (t.users.length > 1 ? '对话记录' : '问题');
    $('cn-dialog-name').textContent = name;
  }

  /* ---------------- 条目模板 ---------------- */
  function addTurn() {
    if ($('cn-turns').children.length >= D.maxTurns) { alert('最多 ' + D.maxTurns + ' 轮'); return; }
    var r = row(
      itemHead(badge('cn-badge--turn') + '<span class="cn-item-k">用户 / 助手</span>') +
      micro('用户消息', '<textarea class="rv-input" data-f="user" rows="2" placeholder="第 N 轮用户消息"></textarea>', 'cn-micro--area') +
      micro('助手参考回复 <span class="cn-opt">prefill 必填</span>',
        '<textarea class="rv-input" data-f="assistant" rows="2" placeholder="live 可留空"></textarea>', 'cn-micro--area'));
    $('cn-turns').appendChild(r); wireRm($('cn-turns')); renumber(); schedulePreview();
  }
  function addCond() {
    var r = row(
      itemHead('<span class="cn-item-k">槽位条件</span>') +
      '<div class="cn-metarow">' +
        micro('槽位', '<input class="rv-input" data-f="slot" placeholder="如 业务类型">', 'cn-grow') +
        micro('值', '<input class="rv-input" data-f="value" placeholder="如 融资业务">', 'cn-grow') +
        micro('关键词 <span class="cn-opt">可空</span>', '<input class="rv-input" data-f="kw" placeholder="用 / 分隔">', 'cn-w210') +
        micro('轮次', '<input class="rv-input" data-f="turn" type="number" min="1" placeholder="空">', 'cn-w72') +
      '</div>');
    $('cn-conds').appendChild(r); wireRm($('cn-conds')); schedulePreview();
  }
  function addInj() {
    var r = row(
      itemHead(badge('cn-badge--inj') + '<span class="cn-item-k">注入</span>') +
      '<div class="cn-metarow">' +
        micro('注入点', '<select class="rv-select" data-f="point">' + optHtml([''].concat(D.points.map(function (p) { return p.id; }))) + '</select>', 'cn-grow') +
        micro('选项', '<select class="rv-select" data-f="option"><option value="">选项</option></select>', 'cn-grow') +
      '</div>');
    $('cn-injs').appendChild(r); wireRm($('cn-injs'));
    var ps = f('[data-f=point]', r);
    ps.onchange = function () {
      var p = D.points.filter(function (x) { return x.id === ps.value; })[0];
      f('[data-f=option]', r).innerHTML = '<option value="">选项</option>' + optHtml(p ? p.options : []);
    };
    renumber();
    schedulePreview();
  }
  function addPre() {
    var r = row(
      itemHead(badge('cn-badge--pre') + '<span class="cn-item-k">前提</span>') +
      micro('前提内容 <span class="cn-opt">自包含，不用指代词</span>',
        '<textarea class="rv-input" data-f="text" rows="2" placeholder="如 用户所在公司已开通融资业务"></textarea>', 'cn-micro--area') +
      '<div class="cn-metarow">' +
        micro('轮次', '<input class="rv-input" data-f="turn" type="number" min="1" placeholder="空">', 'cn-w72') +
        micro('引用注入', '<input class="rv-input" data-f="inj" placeholder="如 J1（可空）">', 'cn-grow') +
      '</div>');
    $('cn-pres').appendChild(r); wireRm($('cn-pres')); renumber(); schedulePreview();
  }
  function factRow(kind, list, ph) {
    var cls = kind === 'required' ? 'f' : kind === 'expected' ? 'e' : 'x';
    var label = kind === 'required' ? '必要' : kind === 'expected' ? '期望' : '禁止';
    var html = itemHead(badge('cn-badge--' + cls) + '<span class="cn-item-k">' + label + '事实</span>') +
      micro('事实内容', '<textarea class="rv-input" data-f="text" rows="2" placeholder="' + esc(ph) + '"></textarea>', 'cn-micro--area') +
      '<div class="cn-metarow">';
    if (kind !== 'forbidden') {
      html += micro('关键词', '<input class="rv-input" data-f="kw" placeholder="用 / 分隔，可空">', 'cn-w210');
    }
    html += micro('轮次', '<input class="rv-input" data-f="turn" type="number" min="1" placeholder="空">', 'cn-w72') +
      micro('组', '<input class="rv-input" data-f="group" placeholder="空">', 'cn-w72') +
      micro('证据', '<select class="rv-select" data-f="evidence">' + evOptions() + '</select>', 'cn-w96');
    if (kind !== 'forbidden') {
      html += micro('权重', '<input class="rv-input" data-f="weight" type="number" min="0" step="0.5" placeholder="空">', 'cn-w84');
    }
    html += '<label class="cn-subj" title="合理表述不唯一的做法 / 建议；判「部分」以部分判据为准">' +
        '<input type="checkbox" data-f="subj">主观</label>' +
      '</div>';
    if (kind !== 'forbidden') {
      html += micro('部分判据 <span class="cn-opt">仅主观条目，如 只写甲未写乙算部分</span>',
        '<input class="rv-input" data-f="prule" placeholder="如 只写短期有效、未给具体期限算部分">', 'cn-micro--area');
    }
    var r = row(html);
    if (kind !== 'forbidden') {
      var prule = f('[data-f=prule]', r).closest('.cn-micro');
      prule.hidden = true;
      f('[data-f=subj]', r).addEventListener('change', function () {
        prule.hidden = !this.checked;
      });
    }
    list.appendChild(r); wireRm(list); renumber(); schedulePreview();
  }
  function addPath() {
    var r = row(
      itemHead('<span class="cn-item-k">skill 路径</span>') +
      '<div class="cn-metarow">' +
        micro('类型', '<select class="rv-select" data-f="ptype"><option value="must">必须</option><option value="forbid">禁止</option></select>', 'cn-w96') +
        micro('skill', '<input class="rv-input" data-f="skill" placeholder="如 trade-documents">', 'cn-grow') +
        micro('轮次', '<input class="rv-input" data-f="turn" type="number" min="1" placeholder="空">', 'cn-w72') +
      '</div>');
    $('cn-paths').appendChild(r); wireRm($('cn-paths')); schedulePreview();
  }
  function addEv() {
    var r = row(
      itemHead(badge('cn-badge--ev')) +
      '<div class="cn-metarow">' +
        micro('方式', '<select class="rv-select" data-f="mode"><option value="point">注入点</option><option value="inline">skill 原文</option></select>', 'cn-w110') +
        '<div class="cn-ev-point" data-ev-point>' +
          micro('注入点', '<select class="rv-select" data-f="point">' + optHtml([''].concat(D.points.map(function (p) { return p.id; }))) + '</select>', 'cn-grow') +
        '</div>' +
        '<div class="cn-inlinefields" data-ev-inline hidden>' +
          micro('skill', '<input class="rv-input" data-f="skill" placeholder="如 trade-finance">', 'cn-w120') +
          micro('文件', '<input class="rv-input" data-f="file" placeholder="如 trade-finance.md">', 'cn-w160') +
          micro('章节', '<input class="rv-input" data-f="section" placeholder="可空">', 'cn-w120') +
          micro('原文', '<input class="rv-input" data-f="quote" placeholder="可空">', 'cn-grow') +
        '</div>' +
      '</div>');
    $('cn-evs').appendChild(r); wireRm($('cn-evs')); renumber(); refreshEvOptions();
    var mode = f('[data-f=mode]', r), pt = f('[data-ev-point]', r), inl = f('[data-ev-inline]', r);
    mode.onchange = function () {
      var isPoint = mode.value === 'point';
      pt.hidden = !isPoint;
      inl.hidden = isPoint;
    };
    schedulePreview();
  }
  function addVar() {
    var r = row(
      itemHead(badge('cn-badge--var')) +
      micro('替换后的用户消息', '<textarea class="rv-input" data-f="text" rows="2" placeholder="用于生成对照变体"></textarea>', 'cn-micro--area') +
      '<div class="cn-metarow">' +
        micro('轮次 <span class="cn-opt">空 = 末轮</span>', '<input class="rv-input" data-f="turn" type="number" min="1" placeholder="空">', 'cn-w84') +
        micro('改写词', '<select class="rv-select" data-f="rewrite">' + optHtml([''].concat(D.rewrites)) + '</select>', 'cn-w110') +
      '</div>');
    $('cn-vars').appendChild(r); wireRm($('cn-vars')); renumber(); schedulePreview();
  }

  /* ---------------- 编号徽标 / 证据选项联动 ---------------- */
  function renumber() {
    var ts = $('cn-turns').children;
    for (var i = 0; i < ts.length; i++) { f('.cn-badge--turn', ts[i]).textContent = '轮 ' + (i + 1); }
    [['cn-pres', 'cn-badge--pre', 'P'], ['cn-facts-req', 'cn-badge--f', 'F'],
     ['cn-facts-exp', 'cn-badge--e', 'E'], ['cn-facts-x', 'cn-badge--x', 'X'],
     ['cn-evs', 'cn-badge--ev', 'R'], ['cn-vars', 'cn-badge--var', 'V'],
     ['cn-injs', 'cn-badge--inj', 'J']].forEach(function (cfg) {
      var rs = $(cfg[0]).children;
      for (var j = 0; j < rs.length; j++) { f('.' + cfg[1], rs[j]).textContent = cfg[2] + (j + 1); }
    });
    updateDialogName();
  }
  function evOptions() {
    var n = $('cn-evs').children.length, list = ['', '外部'];
    for (var i = 1; i <= n; i++) { list.push('R' + i); }
    return optHtml(list);
  }
  function refreshEvOptions() {
    var html = evOptions();
    ['cn-facts-req', 'cn-facts-exp', 'cn-facts-x'].forEach(function (id) {
      $(id).querySelectorAll('[data-f=evidence]').forEach(function (s) {
        var cur = s.value;
        s.innerHTML = html;
        if (cur && Array.prototype.indexOf.call(s.options, s.querySelector('option[value="' + cur + '"]')) >= 0) {
          s.value = cur;
        }
      });
    });
  }

  /* ---------------- 维度 / 目录 / 文档路径联动 ---------------- */
  function suggestedId() {
    var prefix = D.dims[val($('cn-dim'))];
    return D.suggested[prefix] || (prefix + '-0001');
  }
  function docDir() {
    var d = $('cn-dir').value;
    if (d === '__new__') { d = val($('cn-dir-new')); }
    return d || '…';
  }
  function updateDocPath() {
    $('cn-doc-dir').textContent = docDir();
    $('cn-doc-id').textContent = val($('cn-id')) || suggestedId();
    updateDialogName();
  }
  function syncDirRow() {
    $('cn-dir-new-row').hidden = $('cn-dir').value !== '__new__';
    updateDocPath();
  }
  function dimChanged(init) {
    var dim = val($('cn-dim'));
    if (!init && !val($('cn-id'))) { $('cn-id').placeholder = '如 ' + suggestedId(); }
    $('cn-inj-card').hidden = dim !== EC_DIM;
    if (dim !== EC_DIM && $('cn-injs').children.length) { $('cn-injs').innerHTML = ''; }
    var hints = [];
    if (D.multiTurnDims.indexOf(dim) >= 0) { hints.push('该维度至少 2 轮'); }
    if (dim === EC_DIM) { hints.push('必须写「注入」与「禁止事实」'); }
    $('cn-dim-hint').textContent = hints.length ? hints.join('；') : '单轮填一条；最多 ' + D.maxTurns + ' 轮';
    updateDocPath();
  }

  /* ---------------- 收集表单 ---------------- */
  function collect() {
    var dim = val($('cn-dim'));
    var users = [], refs = [];
    rowsOf('cn-turns').forEach(function (r) {
      var u = val(f('[data-f=user]', r));
      if (u) { users.push(u); refs.push(val(f('[data-f=assistant]', r))); }
    });
    var facts = [];
    [['cn-facts-req', 'required'], ['cn-facts-exp', 'expected'], ['cn-facts-x', 'forbidden']].forEach(function (cfg) {
      rowsOf(cfg[0]).forEach(function (r) {
        if (!val(f('[data-f=text]', r))) { return; }
        facts.push({ kind: cfg[1], text: val(f('[data-f=text]', r)),
          keywords: kwSplit(val(f('[data-f=kw]', r))), turn: num(f('[data-f=turn]', r)),
          group: val(f('[data-f=group]', r)), evidence: val(f('[data-f=evidence]', r)),
          weight: flt(f('[data-f=weight]', r)),
          nature: f('[data-f=subj]', r) && f('[data-f=subj]', r).checked ? '主观' : '',
          partial_rule: val(f('[data-f=prule]', r)) });
      });
    });
    var evs = [];
    rowsOf('cn-evs').forEach(function (r) {
      if (val(f('[data-f=mode]', r)) === 'point') {
        if (val(f('[data-f=point]', r))) { evs.push({ point: val(f('[data-f=point]', r)) }); }
      } else if (val(f('[data-f=skill]', r)) && val(f('[data-f=file]', r))) {
        evs.push({ point: '', skill: val(f('[data-f=skill]', r)), file: val(f('[data-f=file]', r)),
          section: val(f('[data-f=section]', r)), quote: val(f('[data-f=quote]', r)) });
      }
    });
    return {
      id: val($('cn-id')), title: val($('cn-title')), dim: dim, topic: val($('cn-topic')),
      granularity: val($('cn-granularity')), source: val($('cn-source')), owner: val($('cn-owner')),
      tags: Array.prototype.map.call(document.querySelectorAll('input[name=cn-tag]:checked'), function (c) { return c.value; }),
      history: val($('cn-history')) || 'live',
      answerable: !$('cn-refusal').checked,
      refusal_weight: $('cn-refusal').checked ? flt($('cn-refusal-w')) : null,
      turns: users, assistant_refs: refs,
      conditions: rowsOf('cn-conds').filter(function (r) {
        return val(f('[data-f=slot]', r)) && val(f('[data-f=value]', r));
      }).map(function (r) {
        return { slot: val(f('[data-f=slot]', r)), value: val(f('[data-f=value]', r)),
          turn: num(f('[data-f=turn]', r)), keywords: kwSplit(val(f('[data-f=kw]', r))) };
      }),
      injections: dim === EC_DIM ? rowsOf('cn-injs').filter(function (r) {
        return val(f('[data-f=point]', r)) && val(f('[data-f=option]', r));
      }).map(function (r) { return { point: val(f('[data-f=point]', r)), option: val(f('[data-f=option]', r)) }; }) : [],
      preconditions: rowsOf('cn-pres').filter(function (r) { return val(f('[data-f=text]', r)); }).map(function (r) {
        return { text: val(f('[data-f=text]', r)), turn: num(f('[data-f=turn]', r)),
          injections: kwSplit(val(f('[data-f=inj]', r))) };
      }),
      facts: facts,
      paths: rowsOf('cn-paths').filter(function (r) { return val(f('[data-f=skill]', r)); }).map(function (r) {
        return { type: val(f('[data-f=ptype]', r)), skill: val(f('[data-f=skill]', r)),
          turn: num(f('[data-f=turn]', r)), weight: flt(f('[data-f=weight]', r)) };
      }),
      evidences: evs,
      variants: rowsOf('cn-vars').filter(function (r) { return val(f('[data-f=text]', r)); }).map(function (r) {
        return { text: val(f('[data-f=text]', r)), turn: num(f('[data-f=turn]', r)),
          rewrite: val(f('[data-f=rewrite]', r)) };
      }),
      reference_answer: val($('cn-refanswer')), note: val($('cn-note')),
      directory: $('cn-dir').value === '__new__' ? val($('cn-dir-new')) : $('cn-dir').value
    };
  }

  /* ---------------- md 源码预览（逐行着色，纯文本写入） ---------------- */
  function appendLine(frag, text, cls) {
    var s = document.createElement('span');
    s.className = 'cn-md-line' + (cls ? ' ' + cls : '');
    s.textContent = text;
    frag.appendChild(s);
  }
  function renderMd(md) {
    lastMd = md || '';
    var pre = $('cn-md-preview');
    var keep = pre.scrollTop;
    pre.textContent = '';
    if (!lastMd) {
      pre.textContent = '填写左侧内容后自动生成 md 预览';
      $('cn-md-lines').textContent = '';
      return;
    }
    var frag = document.createDocumentFragment();
    var inFm = false;
    lastMd.split('\n').forEach(function (line) {
      if (/^---\s*$/.test(line)) {
        inFm = !inFm;
        appendLine(frag, line, 'l-rule');
        return;
      }
      if (inFm) {
        var i = line.indexOf(':');
        if (i > 0) {
          var w = document.createElement('span');
          w.className = 'cn-md-line l-fmline';
          var ks = document.createElement('span'), vs = document.createElement('span');
          ks.className = 'l-key'; vs.className = 'l-val';
          ks.textContent = line.slice(0, i + 1);
          vs.textContent = line.slice(i + 1);
          w.appendChild(ks); w.appendChild(vs); frag.appendChild(w);
          return;
        }
        appendLine(frag, line, 'l-fm');
        return;
      }
      var m = /^(#{1,6})\s/.exec(line);
      if (m) { appendLine(frag, line, 'l-h l-h' + m[1].length); return; }
      if (/^\s*-\s/.test(line)) { appendLine(frag, line, 'l-li'); return; }
      appendLine(frag, line, '');
    });
    pre.appendChild(frag);
    pre.scrollTop = keep;
    $('cn-md-lines').textContent = lastMd.split('\n').length + ' 行';
  }

  /* ---------------- 校验结果 / 状态 ---------------- */
  function notice(tone, head, items) {
    var html = '<div class="cn-notice" data-tone="' + tone + '"><div class="cn-notice-head">' + head + '</div>';
    if (items && items.length) {
      html += '<ul>' + items.map(function (l) { return '<li>' + esc(l) + '</li>'; }).join('') + '</ul>';
    }
    return html + '</div>';
  }
  function problemsHtml(data) {
    var html = '';
    if (data.error) {
      html += notice('error', '<b>' + esc(data.error) + '</b>', data.errors || []);
    } else if (data.errors && data.errors.length) {
      html += notice('error', '<b>校验未通过（' + data.errors.length + ' 条）</b>', data.errors);
    }
    if (data.warnings && data.warnings.length) {
      html += notice('notice', '<b>提示（' + data.warnings.length + ' 条，不阻断保存）</b>', data.warnings);
    }
    return html;
  }
  function setStatus(tone, text) {
    var el = $('cn-status');
    el.dataset.tone = tone;
    el.textContent = text;
  }
  function statusOf(data) {
    var errs = (data.errors || []).length + (data.error ? 1 : 0);
    var warns = (data.warnings || []).length;
    if (errs) { setStatus('fail', errs + ' 条错误'); }
    else if (warns) { setStatus('notice', '通过 · ' + warns + ' 条提示'); }
    else { setStatus('pass', '✓ 校验通过'); }
  }
  function renderLive(data) {
    renderMd(data.md || '');
    if (liveLocked) { return; }        // 保存后的提示不被晚到的实时预览覆盖
    statusOf(data);
    $('cn-result').innerHTML = problemsHtml(data);
  }
  function wireCopyCommands(root) {
    (root || document).querySelectorAll('.cn-cmdline .rv-cmd button').forEach(function (b) {
      b.addEventListener('click', function () {
        var text = b.parentNode.firstChild.textContent.trim();
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { b.textContent = '已复制'; });
        }
      });
    });
  }

  /* ---------------- 网络 ---------------- */
  function schedulePreview() {
    liveLocked = false;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(fetchPreview, 350);
  }
  function fetchPreview() {
    var seq = ++previewSeq;
    fetch('/api/case/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(collect()) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) { if (seq === previewSeq) { renderLive(res.j); } })
      .catch(function (e) {
        if (seq !== previewSeq) { return; }
        setStatus('fail', '预览失败');
        $('cn-result').innerHTML = notice('error', '<b>请求失败</b>', [e.message]);
      });
  }

  $('cn-save-btn').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    liveLocked = true;
    clearTimeout(previewTimer);
    previewSeq++;
    fetch('/api/case/create', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(collect()) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) {
          statusOf(res.j);
          $('cn-result').innerHTML = problemsHtml(res.j);
          return;
        }
        renderMd(res.j.md || lastMd);
        setStatus('pass', '✓ 已保存');
        var html = problemsHtml(res.j) +
          notice('pass', '已保存：<b>' + esc(res.j.path) + '</b>（status: structured）', []);
        html += '<div class="cn-hints"><div class="rv-meta">后续三步（check → build → 执行后导入）：</div>';
        (res.j.hint || []).forEach(function (cmd) {
          html += '<div class="cn-cmdline"><span class="rv-cmd">' + esc(cmd) +
            '<button type="button">复制</button></span></div>';
        });
        $('cn-result').innerHTML = html + '</div>';
        wireCopyCommands($('cn-result'));
      })
      .catch(function (e) {
        btn.disabled = false;
        setStatus('fail', '保存失败');
        $('cn-result').innerHTML = notice('error', '<b>请求失败</b>', [e.message]);
      });
  });

  $('cn-copy-btn').addEventListener('click', function () {
    var b = this;
    if (!lastMd || !navigator.clipboard || !navigator.clipboard.writeText) { return; }
    navigator.clipboard.writeText(lastMd).then(function () {
      b.textContent = '已复制';
      setTimeout(function () { b.textContent = '复制'; }, 1200);
    });
  });

  /* ---------------- 事件 ---------------- */
  var form = $('cn-form');
  form.addEventListener('input', function () { updateDocPath(); schedulePreview(); });
  form.addEventListener('change', function (e) {
    if (e.target.id === 'cn-dim') { dimChanged(false); }
    if (e.target.id === 'cn-dir') { syncDirRow(); }
    if (e.target.id === 'cn-refusal') { $('cn-refusal-w-field').hidden = !e.target.checked; }
    updateDocPath();
    schedulePreview();
  });

  $('cn-add-turn').onclick = addTurn;
  $('cn-add-cond').onclick = addCond;
  $('cn-add-inj').onclick = addInj;
  $('cn-add-pre').onclick = addPre;
  $('cn-add-fact-req').onclick = function () { factRow('required', $('cn-facts-req'), '必须命中的事实，如 开船后的提单'); };
  $('cn-add-fact-exp').onclick = function () { factRow('expected', $('cn-facts-exp'), '期望提到的加分事实'); };
  $('cn-add-fact-x').onclick = function () { factRow('forbidden', $('cn-facts-x'), '被采信即零分的错误说法'); };
  $('cn-add-path').onclick = addPath;
  $('cn-add-ev').onclick = addEv;
  $('cn-add-var').onclick = addVar;

  /* ---------------- 初始化 ---------------- */
  addTurn(); addCond();
  syncDirRow();
  dimChanged(true);
  updateDocPath();
  renderMd('');
  schedulePreview();
})();
