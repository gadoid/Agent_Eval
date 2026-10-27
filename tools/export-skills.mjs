#!/usr/bin/env node
// 从产品 asar 导出技能根到实体目录（P0-3 验证路线；bundled-copy 模式的 skills_roots 基底）。
// asar 内路径纯 Node 读不了，必须经 ELECTRON_RUN_AS_NODE 的 eline 以 Node 模式执行本脚本。
// 用法：
//   ELECTRON_RUN_AS_NODE=1 <eline.exe> tools/export-skills.mjs [--src <asar 内根>] [--dst <目标目录>]
// 默认：src = app.asar 内 dsh-web-app/skills/trade-expert，dst = .eval-skills/base
import { readdirSync, readFileSync, writeFileSync, mkdirSync, rmSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'

const argv = process.argv.slice(2)
const arg = (name, dflt) => {
  const i = argv.indexOf(name)
  return i >= 0 ? argv[i + 1] : dflt
}
const src = arg('--src', 'D:/eline-ai/resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-web-app/skills/trade-expert')
const dst = arg('--dst', 'D:/Agent_Eval/.eval-skills/base')
const version = arg('--version', null)

rmSync(dst, { recursive: true, force: true })
let files = 0
const rels = []
function walk(s, d, prefix = '') {
  mkdirSync(d, { recursive: true })
  for (const name of readdirSync(s)) {
    if (name === 'node_modules' || name === '.git') continue
    const sp = join(s, name), dp = join(d, name), rel = prefix === '' ? name : `${prefix}/${name}`
    if (statSync(sp).isDirectory()) walk(sp, dp, rel)
    else { writeFileSync(dp, readFileSync(sp)); files++; rels.push(rel) }
  }
}
walk(src, dst)

// 与 runner/src/snapshot.ts 的 hashTree 同构：排序后 `${rel}\u0000${content}`，以 \u0001 拼接
const parts = rels.sort().map((rel) => `${rel}\u0000${readFileSync(join(dst, rel), 'utf8')}`)
const sha256 = createHash('sha256').update(parts.join('\u0001'), 'utf8').digest('hex')
writeFileSync(`${dst}.manifest.json`, JSON.stringify({
  source: src,
  version,
  exported_at: new Date().toISOString(),
  files,
  sha256,
}, null, 2) + '\n')
console.log(`exported ${files} files -> ${dst} (sha256 ${sha256.slice(0, 12)}…)`)
