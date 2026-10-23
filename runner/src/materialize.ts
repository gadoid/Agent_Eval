/**
 * skill 副本物化与注入执行（03 第 5.2 节，仅 isolated 模式）：
 *   复制基底 → 同名去重（按盘点生效来源） → key→value 精确替换（计数） → overlay 接线 → sha256 留痕
 * 原始 skill 永远只读；无注入的会话即原样副本（环境对称原则）。
 */
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { InjectionOp } from './domain.js'
import type { Inventory } from './snapshot.js'
import { hashTree } from './snapshot.js'

export interface AppliedInjection {
  point: string
  option: string
  rev: number
  found: number
}

export interface MaterializedSkills {
  /** overlay customSkillDirs 指向的目录（每个基底根一项） */
  dirs: string[]
  applied: AppliedInjection[]
  sha256: string
  /** 物化失败（注入点文件缺失 / key 未命中）时非空，Episode 应判 error */
  error?: { code: 'injection-file-missing' | 'injection-key-missed'; detail: string }
}

export async function materializeSkills(
  runDir: string,
  roots: readonly string[],
  injections: readonly InjectionOp[],
  inventory?: Inventory | null,
): Promise<MaterializedSkills> {
  const skillsRoot = join(runDir, 'skills')
  await rm(skillsRoot, { recursive: true, force: true })
  const dirs: string[] = []
  for (let i = 0; i < roots.length; i++) {
    const dst = join(skillsRoot, String(i))
    await mkdir(dst, { recursive: true })
    await cp(roots[i] as string, dst, { recursive: true, force: true }).catch(() => undefined)
    dirs.push(dst)
  }
  // 同名 skill 去重：盘点记录了生效来源，非生效根中的同名文件删除
  if (inventory && roots.length > 1) {
    for (const s of inventory.skills ?? []) {
      const effective = roots.findIndex((r) => samePath(r, s.root))
      if (effective < 0) continue
      for (let i = 0; i < roots.length; i++) {
        if (i === effective) continue
        const dup = join(dirs[i] as string, s.file)
        await rm(dup, { force: true }).catch(() => undefined)
      }
    }
  }
  const applied: AppliedInjection[] = []
  for (const op of injections) {
    const hit = await findFile(dirs, op.file)
    if (hit === null) {
      return { dirs, applied, sha256: '', error: { code: 'injection-file-missing', detail: `注入点「${op.point}」的文件 ${op.file} 不在基底中` } }
    }
    const before = await readFile(hit, 'utf8')
    const found = countOccurrences(before, op.key)
    if (found === 0) {
      return { dirs, applied, sha256: '', error: { code: 'injection-key-missed', detail: `注入点「${op.point}」的 key 在 ${op.file} 中命中 0 次（skill 改版？）` } }
    }
    await writeFile(hit, op.value === '' ? before.split(op.key).join('') : before.split(op.key).join(op.value), 'utf8')
    applied.push({ point: op.point, option: op.option, rev: op.rev, found })
  }
  return { dirs, applied, sha256: await hashTree(dirs) }
}

/** 注入预演（规划期，只读）：对基底预演 key 匹配，不写文件 */
export async function rehearseInjections(
  rows: readonly { uid: string; injections?: InjectionOp[] }[],
  roots: readonly string[],
): Promise<Map<string, { ok: boolean; reason?: string; founds: number[] }>> {
  const out = new Map<string, { ok: boolean; reason?: string; founds: number[] }>()
  for (const row of rows) {
    if (!row.injections || row.injections.length === 0) continue
    const founds: number[] = []
    for (const op of row.injections) {
      const hit = await findFile(roots, op.file)
      if (hit === null) {
        out.set(row.uid, { ok: false, reason: `注入点「${op.point}」的文件 ${op.file} 不在基底中`, founds })
        break
      }
      const text = await readFile(hit, 'utf8')
      founds.push(countOccurrences(text, op.key))
    }
    if (!out.has(row.uid)) {
      const zero = founds.some((f) => f === 0)
      out.set(row.uid, { ok: !zero, reason: zero ? 'injection-key-missed' : undefined, founds })
    }
  }
  return out
}

/** overlay 中追加的 skill 提供方配置（与会话落盘重定向同层） */
export function buildSkillsOverlayEntries(customSkillDirs: readonly string[]): string {
  const lines = customSkillDirs.map((d) => `      - ${JSON.stringify(d)}`).join('\n')
  return [
    "- id: skill-filesystem",
    "  name: '@deepseek-ai/dsh-skill-filesystem'",
    '  config:',
    '    includeDefaultRoots: false',
    '    customSkillDirs:',
    lines,
    '',
  ].join('\n')
}

/**
 * overlay 中追加的会话模型覆盖（--patch 最高优先级层，压过 eline profile 的同名条目）。
 * model/reasoning_effort 任一存在时生成，provider/模型/努力程度三者齐写——同 id 条目
 * 整体生效，缺项会剥掉 eline profile 层的对应配置（parseProfile 已强制两项同设）。
 */
export function buildModelOverlayEntries(
  profile: { model?: string; reasoning_effort?: string; model_provider?: string },
): string {
  if (profile.model === undefined && profile.reasoning_effort === undefined) return ''
  return [
    '- id: agent-default-model',
    "  name: '@deepseek-ai/dsh-agent-default-model'",
    '  config:',
    `    provider: ${profile.model_provider ?? 'deepseek-account'}`,
    `    model: ${JSON.stringify(profile.model ?? '')}`,
    `    reasoningEffort: ${JSON.stringify(profile.reasoning_effort ?? '')}`,
    '',
  ].join('\n')
}

function countOccurrences(haystack: string, needle: string): number {
  if (needle === '') return 0
  let count = 0
  let i = haystack.indexOf(needle)
  while (i !== -1) {
    count++
    i = haystack.indexOf(needle, i + needle.length)
  }
  return count
}

async function findFile(dirs: readonly string[], rel: string): Promise<string | null> {
  for (const d of dirs) {
    const p = join(d, rel)
    try {
      await readFile(p, 'utf8')
      return p
    } catch {
      // 该根没有此文件，试下一个
    }
  }
  return null
}

function samePath(a: string, b: string): boolean {
  return a.replace(/\\/g, '/').replace(/\/+$/, '') === b.replace(/\\/g, '/').replace(/\/+$/, '')
}
