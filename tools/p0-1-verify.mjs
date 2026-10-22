#!/usr/bin/env node
/**
 * P0-1：隔离机制现场验证（需要真实 ElineAI CLI 与平台凭据，现场执行）。
 *
 * 一次跑完四件事（01 §2 P0-1）：
 *   1. customSkillDirs + includeDefaultRoots:false 经 --patch 生效：会话只见副本、改动生效
 *   2. 并发两个 episode 互不可见（各自的副本互不污染）
 *   3. 隔离 overlay 下 forced /skill名 注入仍生效（<skill_content> 以 user/message 进入上下文）
 *   4. 记录 ask_user 在 CLI 通道的行为与放行语义（观察输出，人工判读）
 *
 * 用法：
 *   node tools/p0-1-verify.mjs --bin <dsh 命令> --profile <eline profile 名> \
 *        --skill-root <权威根> [--skill trade-documents] [--message "问题"]
 * 输出：逐项 PASS/FAIL 与日志目录，供验证记录归档。
 */
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { basename, dirname } from 'node:path'
import { parseArgs } from 'node:util'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const run = promisify(execFile)

const { values } = parseArgs({
  args: process.argv.slice(2),
  options: {
    bin: { type: 'string', description: 'dsh 命令（如 "node C:/.../dsh/lib/bin.js"，含空格请整体传一个参数）' },
    profile: { type: 'string', default: 'evalheadless' },
    'skill-root': { type: 'string' },
    skill: { type: 'string', default: 'trade-documents' },
    message: { type: 'string', default: '现在有哪些可用的 skill？请列出名字。' },
  },
  strict: true,
})
if (!values.bin || !values['skill-root']) {
  console.error('用法见文件头注释；--bin 与 --skill-root 必填')
  process.exit(1)
}

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `：${detail}` : ''}`)
}

const work = await mkdtemp(join(tmpdir(), 'p0-1-'))
const skill = values.skill
const skillFile = `${skill}.md`

// 副本 A：改 description 尾部加标记；副本 B：原样
async function makeCopy(tag, mutate) {
  const dir = join(work, `${tag}-skills`)
  await cp(values['skill-root'], dir, { recursive: true })
  if (mutate) {
    const path = join(dir, skillFile)
    const text = await readFile(path, 'utf8')
    await writeFile(path, text.replace(/^description:/m, `description: "[P0-1-${tag}] `).replace(/"\s*$/m, '"'), 'utf8')
  }
  return dir
}
const copyA = await makeCopy('A', true)
const copyB = await makeCopy('B', false)

function overlayFor(dir) {
  const p = join(work, `overlay-${basename(dir)}.yml`)
  return p
}
async function writeOverlay(dir, sessionRoot) {
  const p = overlayFor(dir)
  await writeFile(p, [
    "- id: session-persistence-jsonl",
    "  name: '@deepseek-ai/dsh-session-persistence-jsonl'",
    '  config:',
    `    root: "${sessionRoot.replace(/\\/g, '/')}"`,
    "- id: skill-filesystem",
    "  name: '@deepseek-ai/dsh-skill-filesystem'",
    '  config:',
    '    includeDefaultRoots: false',
    '    customSkillDirs:',
    `      - "${dir.replace(/\\/g, '/')}"`,
    '',
  ].join('\n'), 'utf8')
  return p
}

async function episode(tag, skillDir, extraMessage) {
  const epDir = join(work, tag)
  const sessionRoot = join(epDir, 'session')
  const workspace = join(epDir, 'workspace')
  await mkdir(sessionRoot, { recursive: true })
  await mkdir(workspace, { recursive: true })
  const overlay = await writeOverlay(skillDir, sessionRoot)
  const binParts = values.bin.split(/\s+/)
  const args = [...binParts.slice(1), '--profile', values.profile, '--patch', overlay,
                ...(extraMessage ? [] : []), extraMessage ?? values.message]
  try {
    const { stdout, stderr } = await run(binParts[0], args, { cwd: workspace, timeout: 600_000, maxBuffer: 32 * 1024 * 1024 })
    return { epDir, stdout, stderr }
  } catch (e) {
    return { epDir, stdout: e.stdout ?? '', stderr: e.stderr ?? String(e.message) }
  }
}

const decoder = await import(new URL('../runner/lib/vendor/zstd.js', import.meta.url).href)
const { readdir } = await import('node:fs/promises')

async function readSessionText(epDir) {
  const sessionRoot = join(epDir, 'session')
  const dirs = await readdir(sessionRoot, { recursive: true }).catch(() => [])
  const log = dirs.map((d) => join(sessionRoot, String(d))).find((p) => basename(p).endsWith('.zstd') || basename(p).endsWith('.jsonl'))
  if (!log) return ''
  const bytes = await readFile(log)
  const isZstd = bytes.subarray(0, 4).equals(Buffer.from([0x28, 0xb5, 0x2f, 0xfd]))
  return isZstd ? decoder.decodeZstdLog(bytes) : bytes.toString('utf8')
}

console.log(`工作目录 ${work}`)
// 1&2：并发两个 episode，A 改 description、B 原样
const [a, b] = await Promise.all([episode('epA', copyA), episode('epB', copyB)])
const textA = await readSessionText(a.epDir)
const textB = await readSessionText(b.epDir)
check('会话日志落盘（overlay 会话重定向生效）', textA.length > 0, `${a.epDir}`)
const catalogA = textA.match(/<available_skills>([\s\S]*?)<\/available_skills>/)?.[1] ?? ''
const catalogB = textB.match(/<available_skills>([\s\S]*?)<\/available_skills>/)?.[1] ?? ''
check('A 只见副本：目录含 [P0-1-A] 标记', catalogA.includes('[P0-1-A]'))
check('A 不见默认根：目录不含未复制的 skill（如 diagnose-windows-sandbox-acl）', !catalogA.includes('diagnose-windows-sandbox-acl'))
check('B 原样副本：目录无 A 标记', !catalogB.includes('[P0-1-A]'))
check('并发互不可见', catalogA.includes('[P0-1-A]') && !catalogB.includes('[P0-1-A]'))
// 3：forced 注入在隔离 overlay 下生效
const f = await episode('epF', copyB, `/${skill} 这份资料的核心规则是什么？`)
const textF = await readSessionText(f.epDir)
check('forced /skill 注入生效（<skill_content> 以 user/message 注入）', textF.includes(`<skill_content name="${skill}"`))
// 4：ask_user 行为观察（不判 PASS/FAIL，人工判读）
const asked = (textA.match(/ask_user/g) ?? []).length
console.log(`OBSERVE  ask_user 事件出现 ${String(asked)} 次（CLI 通道是否挂起/放行语义，人工判读；工作区 ${work} 保留）`)

const failed = results.filter((r) => !r.ok)
console.log(failed.length === 0 ? '\n全部通过：隔离机制可用，P0-1 完成，可切换 skills_mode: isolated' : `\n${String(failed.length)} 项未通过：详见上方 FAIL；退路（per-episode DSH_HOME）需另行验证`)
process.exit(failed.length === 0 ? 0 : 2)
