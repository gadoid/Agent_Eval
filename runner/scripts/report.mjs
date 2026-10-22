#!/usr/bin/env node
/**
 * 把 results.jsonl 渲染成浏览器可读的对话记录（单文件 HTML，无外部依赖）。
 * 用法：node scripts/report.mjs <results.jsonl 路径或运行目录> [-o 输出.html]
 * 示例：node scripts/report.mjs out/field-verify-eline-r2
 *       省略 -o 时输出到同目录的 report.html。
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { existsSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'

const args = process.argv.slice(2)
const input = resolve(args.shift() ?? '')
let output = ''
for (let i = 0; i < args.length; i++) {
  if (args[i] === '-o') output = resolve(args[++i])
}
const resultsPath = input.endsWith('.jsonl') ? input : join(input, 'results.jsonl')
if (!existsSync(resultsPath)) die(`找不到 ${resultsPath}`)
const outPath = output || (resultsPath.endsWith('.jsonl') ? resolve(resultsPath, '..', 'report.html') : join(input, 'report.html'))

const records = readFileSync(resultsPath, 'utf8').split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l))
const fmt = (ms) => (ms >= 60000 ? `${(ms / 60000).toFixed(1)}分` : `${(ms / 1000).toFixed(1)}秒`)

const cards = records.map((r) => {
  const turns = (r.turns ?? [])
    .map((t) => {
      const who = t.source === 'prefill' ? '预置' : t.input.startsWith('/') && t === r.turns[0] ? '用户（含干预前缀）' : '用户'
      const err = t.error ? `<div class="err">${esc(t.error)}</div>` : ''
      const skills = t.observations?.skills?.length ? `<div class="meta">skill：${t.observations.skills.map((s) => esc(s.name) + (s.ok ? '' : '（未确认成功）')).join('、')}</div>` : ''
      return `<div class="turn"><div class="bubble user"><div class="who">${who} · 第${t.index}轮 · ${t.status}</div><div class="text">${esc(t.input)}</div></div>
<div class="bubble bot"><div class="who">助手 · ${fmt(t.duration_ms ?? 0)}${t.observations?.usage ? ` · ${(t.observations.usage.tokens.total ?? 0).toLocaleString()} tokens` : ''}</div><div class="text">${esc(t.output)}</div>${skills}${err}</div></div>`
    })
    .join('')
  return `<details class="card ${r.status}" data-mode="${r.mode}" data-status="${r.status}">
<summary><span class="uid">${esc(r.uid)}</span><span class="chip mode">${r.mode}</span><span class="chip">t${r.trial}</span><span class="chip status">${r.status}</span><span class="dim">${esc(r.dim ?? '')}</span><span class="skills">${(r.loaded_skills ?? []).map(esc).join('、') || '未加载 skill'}</span><span class="dur">${fmt(r.duration_ms ?? 0)}</span></summary>
<div class="turns">${turns}</div></details>`
})

const counts = records.reduce((a, r) => ((a[r.status] = (a[r.status] ?? 0) + 1), a), {})
const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>评测对话记录 · ${esc(basename(resultsPath))}</title><style>
body{font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#1f2328}
header{padding:16px 24px;background:#fff;border-bottom:1px solid #e1e4e8;position:sticky;top:0;z-index:1}
header h1{font-size:16px;margin:0 0 8px}
.chips button{border:1px solid #d0d7de;background:#fff;border-radius:14px;padding:3px 12px;margin-right:6px;cursor:pointer;font-size:12px}
.chips button.on{background:#0969da;color:#fff;border-color:#0969da}
main{max-width:900px;margin:16px auto;padding:0 12px}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:8px;margin-bottom:10px}
.card.error{border-color:#cf222e}.card.timeout{border-color:#9a6700}.card.incomplete{border-color:#9a6700}
summary{display:flex;gap:8px;align-items:center;padding:10px 14px;cursor:pointer;flex-wrap:wrap;font-size:13px}
.uid{font-weight:600}.chip{border-radius:10px;padding:1px 8px;font-size:11px;background:#eff2f5}
.chip.mode{background:#ddf4ff}.chip.status{background:#dafbe1}.error .chip.status{background:#ffebe9}.timeout .chip.status,.incomplete .chip.status{background:#fff8c5}
.dim{color:#57606a;font-size:12px}.skills{color:#8250df;font-size:12px;flex:1;min-width:120px}.dur{color:#57606a;font-size:12px}
.turns{padding:4px 14px 14px;border-top:1px solid #f0f1f3}
.turn{margin-top:10px}.bubble{border-radius:10px;padding:8px 12px;max-width:86%;font-size:13px;line-height:1.6}
.bubble.user{background:#e9f2ff;margin-left:auto;white-space:pre-wrap}.bubble.user .who{text-align:right}
.bubble.bot{background:#f6f8fa;margin-top:6px;white-space:pre-wrap}
.who{font-size:11px;color:#57606a;margin-bottom:4px}
.err{color:#cf222e;font-size:12px;margin-top:6px}.meta{font-size:12px;color:#8250df;margin-top:4px}
</style></head><body>
<header><h1>评测对话记录 · ${esc(basename(resultsPath))} · ${records.length} 段会话（${Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(' / ')}）</h1>
<div class="chips">
<button class="on" data-f="all">全部</button><button data-f="natural">natural</button><button data-f="forced">forced</button><button data-f="ok">ok</button><button data-f="error">非 ok</button>
</div></header><main>${cards.join('')}</main>
<script>
document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>{
document.querySelectorAll('.chips button').forEach(x=>x.classList.remove('on'));b.classList.add('on')
const f=b.dataset.f
document.querySelectorAll('.card').forEach(c=>{c.style.display=(f==='all'||c.dataset.mode===f||(f==='ok'&&c.dataset.status==='ok')||(f==='error'&&c.dataset.status!=='ok'))?'':'none'})
})</script></body></html>`

writeFileSync(outPath, html)
console.log(`报告已生成：${outPath}`)

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
}
function die(msg) {
  console.error(msg)
  process.exit(1)
}
