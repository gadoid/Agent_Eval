/* 命名改造：run / 判定配置 重命名。内联输入替换原控件，POST 后整页刷新；
 * 空串提交 = 清除命名（run 回默认拼接标题，链回「未命名」）。 */
;(function () {
  window.rvRename = function (cfg) {
    cfg.btn.addEventListener('click', function () {
      var id = cfg.getId();
      if (!id) { cfg.btn.title = cfg.emptyHint || '先选择目标'; return; }
      var wrap = document.createElement('span');
      wrap.style.cssText = 'display:inline-flex;gap:6px;align-items:center';
      var inp = document.createElement('input');
      inp.className = 'rv-input';
      inp.maxLength = 60;
      inp.style.width = '240px';
      inp.placeholder = cfg.placeholder || '';
      inp.value = cfg.getLabel() || '';
      var ok = document.createElement('button');
      ok.className = 'rv-btn rv-btn--primary'; ok.type = 'button'; ok.textContent = '保存';
      var no = document.createElement('button');
      no.className = 'rv-btn'; no.type = 'button'; no.textContent = '取消';
      wrap.append(inp, ok, no);
      cfg.host.style.display = 'none';
      cfg.btn.style.display = 'none';
      cfg.host.after(wrap);
      inp.focus(); inp.select();
      function done() { wrap.remove(); cfg.host.style.display = ''; cfg.btn.style.display = ''; }
      ok.addEventListener('click', function () {
        var body = {};
        body[cfg.key] = id;
        body[cfg.field] = inp.value;
        fetch(cfg.url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(body) })
          .then(function (r) { if (!r.ok) throw new Error('保存失败'); location.reload(); })
          .catch(function (e) { done(); alert(e.message); });
      });
      no.addEventListener('click', done);
      inp.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); ok.click(); }
        if (ev.key === 'Escape') done();
      });
    });
  };
})();
