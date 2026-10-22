#!/usr/bin/env node
/**
 * 把一次评测运行的会话日志镜像到 dsh/eline 客户端的会话目录（双端备份）。
 * out/ 下的原件不动，拷贝到客户端会话树后，客户端重启/刷新即可在对话列表看到。
 *
 * 用法：
 *   node scripts/mirror-to-client.mjs <运行目录> [--home <DSH_HOME>] [--workspace <注册工作区路径>]
 * 示例：
 *   node scripts/mirror-to-client.mjs out/field-verify-eline-r2
 *
 * --home 默认 ~/.eline（eline 客户端）。工作区默认取注册表里的第一个，
 * 会话会放进它对应的 slug 目录；已存在的同名会话跳过（可重复执行）。
 */
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'

const args = process.argv.slice(2)
const outDir = resolve(args.shift() ?? '')
let home = join(homedir(), '.eline')
let workspaceArg
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--home') home = resolve(args[++i])
  else if (args[i] === '--workspace') workspaceArg = args[++i]
  else die(`未知参数：${args[i]}`)
}
if (!outDir || !existsSync(join(outDir, '.work'))) die(`用法：node scripts/mirror-to-client.mjs <运行目录>\n<运行目录> 下需要存在 .work（评测输出目录）`)

const registryPath = join(home, 'storages', 'workspace.json')
if (!existsSync(registryPath)) die(`${registryPath} 不存在：${home} 不是客户端使用的 DSH_HOME`)
const registry = JSON.parse(readFileSync(registryPath, 'utf8'))
const workspaces = Object.values(registry.tables?.workspaces ?? {})
const wsPath = workspaceArg ?? workspaces[0]?.path
if (!wsPath) die('注册表中没有工作区')

const sessionsRoot = join(home, 'sessions')
const slugCore = wsPath.replace(/[^A-Za-z0-9]+/g, '-')
const slug = readdirSync(sessionsRoot).find((d) => d.includes(slugCore))
if (!slug) die(`${sessionsRoot} 下没有对应「${wsPath}」的 slug 目录`)
const target = join(sessionsRoot, slug)

let copied = 0
let skipped = 0
for (const episode of readdirSync(join(outDir, '.work'))) {
  const sess = join(outDir, '.work', episode, 'session')
  if (!existsSync(sess)) continue
  for (const name of readdirSync(sess)) {
    const walk = (dir) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const p = join(dir, entry.name)
        if (entry.isDirectory()) {
          if (/^session-[0-9a-f-]{36}$/.test(entry.name)) {
            const dest = join(target, entry.name)
            if (existsSync(dest)) { skipped++; continue }
            cpSync(p, dest, { recursive: true })
            copied++
          } else walk(p)
        }
      }
    }
    walk(sess)
  }
}
console.log(`镜像完成：${copied} 个会话已拷入 ${target}（跳过已存在 ${skipped} 个）`)
console.log('打开/刷新客户端即可在对话列表看到。')

function die(msg) {
  console.error(msg)
  process.exit(1)
}
