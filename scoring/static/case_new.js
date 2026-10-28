/* 新建用例页：结构化表单 → /api/case/preview（校验+md 预览）→ /api/case/create（落盘 md）。
 * 动态行（轮次/条件/注入/前提/事实/路径/证据/变体）全部前端拼装、提交时按填写顺序重编号；
 * 维度联动：id 前缀建议、注入卡只对「错误知识纠正」开放、多轮维度提示。 */
;(function () {
  var D = JSON.parse(document.getElementById('cn-data').textContent);
  var EC_DIM = '错误知识纠正';
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
  function row(html) {
    var d = document.createElement('div');
    d.className = 'rv-cn-row';
    d.innerHTML = html;
    return d;
  }
  function rmBtn() { return '<button class="rv-btn rv-btn--ghost" type="button" data-rm title="删除本行">✕</button>'; }
  function badge(cls) { return '<span class="rv-cn-itemid ' + cls + '"></span>'; }
  function f(sel, root) { return (root || document).querySelector(sel); }
  function wireRm(list) {
    list.querySelectorAll('[data-rm]').forEach(function (b) {
      b.onclick = function () { b.closest('.rv-cn-row').remove(); renumber(); refreshEvOptions(); };
    });
  }

  /* ---------------- 行模板 ---------------- */
  function addTurn() {
    if ($('cn-turns').children.length >= D.maxTurns) { alert('最多 ' + D.maxTurns + ' 轮'); return; }
    var r = row(badge('cn-badge-turn') +
      '<textarea class="rv-input rv-cn-user" data-f="user" rows="2" placeholder="第 N 轮用户消息"></textarea>' +
      '<textarea class="rv-input rv-cn-ref" data-f="assistant" rows="2" placeholder="助手参考回复（可空；prefill 时前面各轮必填）"></textarea>' + rmBtn());
    $('cn-turns').appendChild(r); wireRm($('cn-turns')); renumber();
  }
  function addCond() {
    var r = row('<input class="rv-input" data-f="slot" style="width:130px" placeholder="槽位，如 业务类型">' +
      '<input class="rv-input" data-f="value" style="flex:1;min-width:200px" placeholder="值，如 融资业务">' +
      '<input class="rv-input" data-f="kw" style="width:180px" placeholder="关键词（/ 分隔，可空）">' +
      '<input class="rv-input" data-f="turn" type="number" min="1" style="width:64px" placeholder="轮次">' + rmBtn());
    $('cn-conds').appendChild(r); wireRm($('cn-conds'));
  }
  function addInj() {
    var r = row('<select class="rv-select" data-f="point" style="width:220px">' +
      optHtml([''].concat(D.points.map(function (p) { return p.id; }))) + '</select>' +
      '<select class="rv-select" data-f="option" style="width:160px"><option value="">选项</option></select>' + rmBtn());
    $('cn-injs').appendChild(r); wireRm($('cn-injs'));
    var ps = f('[data-f=point]', r);
    ps.onchange = function () {
      var p = D.points.filter(function (x) { return x.id === ps.value; })[0];
      f('[data-f=option]', r).innerHTML = '<option value="">选项</option>' +
        optHtml(p ? p.options : []);
    };
  }
  function addPre() {
    var r = row(badge('cn-badge-pre') +
      '<textarea class="rv-input" data-f="text" rows="2" style="flex:1;min-width:260px" placeholder="前提内容（自包含，不用指代词）"></textarea>' +
      '<input class="rv-input" data-f="turn" type="number" min="1" style="width:64px" placeholder="轮次">' +
      '<input class="rv-input" data-f="inj" style="width:110px" placeholder="注入，如 J1">' + rmBtn());
    $('cn-pres').appendChild(r); wireRm($('cn-pres')); renumber();
  }
  function factRow(kind, list, ph) {
    var r = row(badge('cn-badge-' + (kind === 'required' ? 'f' : kind === 'expected' ? 'e' : 'x')) +
      '<textarea class="rv-input" data-f="text" rows="2" style="flex:1;min-width:260px" placeholder="' + ph + '"></textarea>' +
      (kind === 'forbidden' ? '' : '<input class="rv-input" data-f="kw" style="width:180px" placeholder="关键词（/ 分隔，可空）">') +
      '<input class="rv-input" data-f="turn" type="number" min="1" style="width:64px" placeholder="轮次">' +
      '<input class="rv-input" data-f="group" style="width:64px" placeholder="组">' +
      '<select class="rv-select" data-f="evidence" style="width:80px">' + evOptions() + '</select>' +
      '<input class="rv-input" data-f="weight" type="number" min="0" step="0.5" style="width:70px" placeholder="权重">' +
      // 性质（doc/16 §3.7）：勾「主观」→ md 落【性质：主观】；部分判据输入框仅必要/期望提供
      '<label class="rv-cn-check" title="合理表述不唯一的做法/建议（【性质：主观】，判「部分」以部分判据为准）">' +
      '<input type="checkbox" data-f="subj">主观</label>' +
      (kind === 'forbidden' ? '' :
        '<input class="rv-input" data-f="prule" style="width:240px;display:none" placeholder="部分判据：如 只写甲未写乙算部分">') +
      rmBtn());
    var subj = f('[data-f=subj]', r), prule = f('[data-f=prule]', r);
    subj.addEventListener('change', function () {
      if (prule) { prule.style.display = subj.checked ? '' : 'none'; }
    });
    list.appendChild(r); wireRm(list); renumber();
  }
  function addPath() {
    var r = row('<select class="rv-select" data-f="ptype" style="width:80px"><option value="must">必须</option><option value="forbid">禁止</option></select>' +
      '<input class="rv-input" data-f="skill" style="flex:1;min-width:180px" placeholder="skill 名称，如 trade-documents">' +
      '<input class="rv-input" data-f="turn" type="number" min="1" style="width:64px" placeholder="轮次">' +
      '<input class="rv-input" data-f="weight" type="number" min="0" step="0.5" style="width:70px" placeholder="权重">' + rmBtn());
    $('cn-paths').appendChild(r); wireRm($('cn-paths'));
  }
  function addEv() {
    var r = row(badge('cn-badge-ev') +
      '<select class="rv-select" data-f="mode" style="width:90px"><option value="point">注入点</option><option value="inline">skill 原文</option></select>' +
      '<select class="rv-select" data-f="point" style="width:200px">' + optHtml([''].concat(D.points.map(function (p) { return p.id; }))) + '</select>' +
      '<span data-ev-inline style="display:none;flex:1;gap:6px">' +
      '<input class="rv-input" data-f="skill" style="width:120px" placeholder="skill，如 trade-finance">' +
      '<input class="rv-input" data-f="file" style="width:170px" placeholder="文件，如 trade-finance.md">' +
      '<input class="rv-input" data-f="section" style="width:130px" placeholder="章节（可空）">' +
      '<input class="rv-input" data-f="quote" style="flex:1;min-width:150px" placeholder="原文（可空）"></span>' + rmBtn());
    $('cn-evs').appendChild(r); wireRm($('cn-evs')); renumber(); refreshEvOptions();
    var mode = f('[data-f=mode]', r), pt = f('[data-f=point]', r), inl = f('[data-ev-inline]', r);
    function sync() {
      var isPoint = mode.value === 'point';
      pt.style.display = isPoint ? '' : 'none';
      inl.style.display = isPoint ? 'none' : '';
    }
    mode.onchange = sync; sync();
  }
  function addVar() {
    var r = row(badge('cn-badge-var') +
      '<textarea class="rv-input" data-f="text" rows="2" style="flex:1;min-width:260px" placeholder="替换后的用户消息"></textarea>' +
      '<input class="rv-input" data-f="turn" type="number" min="1" style="width:64px" placeholder="轮次（空=末轮）">' +
      '<select class="rv-select" data-f="rewrite" style="width:90px">' + optHtml([''].concat(D.rewrites)) + '</select>' + rmBtn());
    $('cn-vars').appendChild(r); wireRm($('cn-vars')); renumber();
  }

  /* ---------------- 编号徽标 / 证据选项联动 ---------------- */
  function renumber() {
    var ts = $('cn-turns').children;
    for (var i = 0; i < ts.length; i++) { f('.cn-badge-turn', ts[i]).textContent = '轮 ' + (i + 1); }
    [['cn-pres', 'cn-badge-pre', 'P'], ['cn-facts-req', 'cn-badge-f', 'F'],
     ['cn-facts-exp', 'cn-badge-e', 'E'], ['cn-facts-x', 'cn-badge-x', 'X'],
     ['cn-evs', 'cn-badge-ev', 'R'], ['cn-vars', 'cn-badge-var', 'V']].forEach(function (cfg) {
      var rows_ = $(cfg[0]).children;
      for (var i = 0; i < rows_.length; i++) { f('.' + cfg[1], rows_[i]).textContent = cfg[2] + (i + 1); }
    });
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
        if (Array.prototype.indexOf.call(s.options, s.querySelector('option[value="' + cur + '"]')) >= 0 && cur) {
          s.value = cur;
        }
      });
    });
  }

  /* ---------------- 维度联动 ---------------- */
  function dimChanged(init) {
    var dim = val($('cn-dim'));
    var prefix = D.dims[dim];
    if (!init && !val($('cn-id'))) { $('cn-id').placeholder = '如 ' + (D.suggested[prefix] || prefix + '-0001'); }
    $('cn-inj-card').hidden = dim !== EC_DIM;
    if (dim !== EC_DIM && $('cn-injs').children.length) { $('cn-injs').innerHTML = ''; }
    var hints = [];
    if (D.multiTurnDims.indexOf(dim) >= 0) { hints.push('该维度至少 2 轮'); }
    if (dim === EC_DIM) { hints.push('必须写「注入」与「禁止事实」'); }
    $('cn-dim-hint').textContent = hints.length ? hints.join('；') : '单轮填一条；最多 ' + D.maxTurns + ' 轮';
  }
  $('cn-dim').addEventListener('change', function () { dimChanged(false); });

  /* ---------------- 收集表单 ---------------- */
  function rowsOf(id) { return Array.prototype.slice.call($(id).children); }
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
      id: val($('cn-id')), dim: dim, topic: val($('cn-topic')),
      granularity: val($('cn-granularity')), source: val($('cn-source')), owner: val($('cn-owner')),
      tags: Array.prototype.map.call(document.querySelectorAll('input[name=cn-tag]:checked'), function (c) { return c.value; }),
      history: val($('cn-history')) || 'live',
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

  /* ---------------- 结果渲染 ---------------- */
  function notice(tone, lines) {
    return '<div class="rv-notice" data-tone="' + tone + '" style="margin-bottom:8px">' +
      lines.map(function (l) { return '<div>' + esc(l) + '</div>'; }).join('') + '</div>';
  }
  function cmdSpan(cmd) {
    return '<div><span class="rv-cmd">' + esc(cmd) + '<button type="button">复制</button></span></div>';
  }
  function renderResult(data, saved) {
    var html = '';
    if (data.errors && data.errors.length) {
      html += notice('error', ['<b>校验未通过（' + data.errors.length + ' 条）</b>'].concat(data.errors));
    } else if (data.error) {
      html += notice('error', ['<b>' + esc(data.error) + '</b>'].concat(data.errors || []));
    }
    if (data.warnings && data.warnings.length) {
      html += notice('notice', ['<b>提示（' + data.warnings.length + ' 条，不阻断保存）</b>'].concat(data.warnings));
    }
    if (saved && data.path) {
      html += notice('pass', ['已保存：<b>' + esc(data.path) + '</b>（status: structured）',
        '后续三步（check 已通过、build 编译新版本、执行后导入）：']);
      (data.hint || []).forEach(function (cmd) { html += cmdSpan(cmd); });
    }
    if (data.md) {
      html += '<div class="rv-cn-head" style="margin-top:8px"><b>md 预览</b>' +
        '<span class="rv-meta">' + (data.ok === false ? '当前表单产出的 md（先修上面的错误）' : '落盘内容与预览一致') + '</span></div>' +
        '<pre class="rv-cn-md">' + esc(data.md) + '</pre>';
    }
    $('cn-result').innerHTML = html;
    $('cn-result').querySelectorAll('.rv-cmd button').forEach(function (b) {
      b.addEventListener('click', function () {
        var text = b.parentNode.firstChild.textContent.trim();
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { b.textContent = '已复制'; });
        }
      });
    });
  }
  function post(url, body, onOk) {
    fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) { onOk(res); })
      .catch(function (e) { $('cn-result').innerHTML = notice('error', ['请求失败：' + e.message]); });
  }

  $('cn-preview-btn').addEventListener('click', function () {
    post('/api/case/preview', collect(), function (res) { renderResult(res.j, false); });
  });
  $('cn-save-btn').addEventListener('click', function () {
    post('/api/case/create', collect(), function (res) {
      renderResult(res.j, res.ok);
      if (res.ok) { window.scrollTo({ top: 0, behavior: 'smooth' }); }
    });
  });
  $('cn-dir').addEventListener('change', function () { $('cn-dir-new-row').hidden = $('cn-dir').value !== '__new__'; });

  /* ---------------- 初始：一轮空对话 + 默认目录 ---------------- */
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
  addTurn(); addCond();
  dimChanged(true);
})();
