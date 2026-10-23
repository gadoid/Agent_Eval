/* 关键词标注：在容器内的文本节点上包 <mark class="rv-hit">，不破坏 HTML 结构。 */
(function () {
  function markText(node, kw) {
    var rest = [node];
    while (rest.length) {
      var n = rest.pop();
      if (n.nodeType === 3) {
        var text = n.textContent, hit = null;
        for (var i = 0; i < kw.length; i++) {
          if (kw[i] && text.indexOf(kw[i]) >= 0) { hit = kw[i]; break; }
        }
        if (!hit) continue;
        var at = text.indexOf(hit);
        var mark = document.createElement('mark');
        mark.className = 'rv-hit';
        mark.textContent = hit;
        var after = n.splitText(at);
        after.textContent = after.textContent.slice(hit.length);
        n.parentNode.insertBefore(mark, after);
        rest.push(after, mark.nextSibling ? mark.previousSibling : null);
      } else if (n.nodeType === 1 && n.tagName !== 'MARK' && n.tagName !== 'SCRIPT' && n.tagName !== 'STYLE') {
        for (var c = n.firstChild; c; c = c.nextSibling) rest.push(c);
      }
    }
  }
  window.rvMark = function (root, keywords) {
    if (!root || !keywords || !keywords.length) return;
    markText(root, keywords.filter(Boolean));
  };
  /* 复制按钮（rejudge 命令等，局域网 http 剪贴板不可用时退化为选中文字） */
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.rv-cmd button').forEach(function (b) {
      b.addEventListener('click', function () {
        var text = b.parentNode.firstChild.textContent.trim();
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { b.textContent = '已复制'; }, function () { select(b.parentNode); });
        } else select(b.parentNode);
      });
    });
    function select(el) {
      var r = document.createRange(); r.selectNodeContents(el);
      var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      b = el.querySelector('button'); if (b) b.textContent = '已选中，Ctrl+C';
    }
  });
})();
