#!/usr/bin/env node
/**
 * P0-2：从既有会话日志提取生产可见 skill 目录，对照各根目录定位文件，产出 skills-inventory.yaml。
 * 用法：node tools/p0-2-inventory.mjs <session.v4.jsonl.zstd> --roots <dir,...> [--agent-version <v>] [-o skills-inventory.yaml]
 * 名称清单来自日志的 <available_skills> 目录消息（生产真实所见）；来源根对照各根目录的 frontmatter name。
 */
import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { parseArgs } from 'node:util'

const { values, positionals } = parseArgs({
  args: process.argv.slice(2),
  options: {
    roots: { type: 'string' },
    'agent-version': { type: 'string' },
    o: { type: 'string' },
  },
  allowPositionals: true,
  strict: true,
})

const logPath = positionals[0]
if (!logPath || !values.roots) {
  console.error('用法：node tools/p0-2-inventory.mjs <session 日志> --roots <skill根,...> [--agent-version v] [-o out.yaml]')
  process.exit(1)
}

// runner 编译产物里的多帧 zstd 解压
const { decodeZstdLog } = await import(new URL('../runner/lib/vendor/zstd.js', import.meta.url).href)
const bytes = await readFile(logPath)
const text = decodeZstdLog(bytes)

// 逐帧解析 JSON，取出所有 user/message 文本，找 <available_skills> 目录消息
let catalog = null
for (const line of text.split('\n')) {
  if (catalog) break
  if (!line.trim()) continue
  let frame
  try {
    frame = JSON.parse(line)
  } catch {
    continue
  }
  const content = frame?.data?.content
  if (!Array.isArray(content)) continue
  for (const block of content) {
    if (block?.type === 'text' && typeof block.text === 'string' && block.text.includes('<available_skills>')) {
      catalog = block.text
      break
    }
  }
}
if (!catalog) {
  console.error('日志中未找到 <available_skills> 目录消息')
  process.exit(1)
}
const m = catalog.match(/<available_skills>([\s\S]*?)<\/available_skills>/)
const names = [...m[1].matchAll(/^- `([a-z0-9][a-z0-9-]*)`/gm)].map((x) => x[1])
console.error(`目录消息列出 ${String(names.length)} 个 skill`)

const roots = values.roots.split(',').map((s) => s.trim()).filter(Boolean)
const sha256 = (s) => createHash('sha256').update(s).digest('hex')

async function* walk(dir, prefix = '') {
  let entries
  try {
    entries = await readdir(dir, { withFileTypes: true })
  } catch {
    return
  }
  for (const e of entries.sort((a) => a.name)) {
    if (e.name === 'node_modules' || e.name === '.git') continue
    const rel = prefix === '' ? e.name : `${prefix}/${e.name}`
    if (e.isDirectory()) yield* walk(join(dir, e.name), rel)
    else if (e.isFile()) yield [rel, await readFile(join(dir, rel), 'utf8').catch(() => '')]
  }
}

const index = [] // {name, root, file, sha256, manifest}
for (const root of roots) {
  const files = []
  for await (const [rel, content] of walk(root)) {
    files.push([rel, content])
    const fm = content.match(/^---\n([\s\S]*?)\n---/)
    if (fm) {
      const name = fm[1].match(/^name:\s*"?([a-z0-9][a-z0-9-]*)"?\s*$/m)
      if (name) index.push({ name: name[1], root, file: rel, sha256: sha256(content) })
    }
  }
  index.push({ __manifest: root, sha256: sha256(files.sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([r, c]) => `${r}\u0000${c}`).join('\u0001')) })
}

const manifests = index.filter((x) => x.__manifest)
const skills = index.filter((x) => !x.__manifest)
// 同名 skill：先列出的根优先（生产证据：forced 注入的 base directory 为 .dsh 根；P0-1 现场最终确认）
const located = new Map()
for (const s of skills) if (!located.has(s.name)) located.set(s.name, s)
const lines = [
  '# P0-2 盘点（p0-2-inventory.mjs 生成）',
  `exported_at: ${new Date().toISOString()}`,
  values['agent-version'] ? `agent_version: ${values['agent-version']}` : '# agent_version: 待填（dsh --version）',
  'roots:',
  ...manifests.map((mm) => `  - path: ${mm.__manifest}\n    manifest_sha256: ${mm.sha256}`),
  'skills:',
]
const missing = []
for (const name of names) {
  const hit = located.get(name)
  if (hit) {
    lines.push(`  - {name: ${name}, root: "${hit.root}", file: "${hit.file}", sha256: ${hit.sha256}}`)
  } else {
    missing.push(name)
    lines.push(`  - {name: ${name}, root: bundled?, file: null, sha256: null}   # 各根未找到，疑为 CLI 内置`)
  }
}
const out = values.o ?? 'runner/skills-inventory.yaml'
await writeFile(out, lines.join('\n') + '\n', 'utf8')
console.error(`→ ${out}：定位 ${String(names.length - missing.length)}/${String(names.length)}`)
if (missing.length > 0) console.error(`未定位（bundled/待补根）：${missing.join(', ')}`)
