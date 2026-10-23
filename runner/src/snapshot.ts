/**
 * 环境快照（01 第 5 节）：run 开始时工具自动计算的一组哈希。
 * 不是门禁、不需要人登记；唯一消费方是版本对比视图与 --resume 一致性检查。
 */
import { readFile, readdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { parse } from 'yaml'
import type { Profile } from './domain.js'
import { sha256 } from './profile.js'

export interface Snapshot extends Record<string, unknown> {
  evalset: { file: string; sha256: string }
  runner_profile: string
  eline_profile: { method: 'files'; sha256: string; files: string[] }
  skills_mode: string
  skills_base: string
  agent: { name: string; version: string | null }
  models_observed?: string[]
}

/** 盘点结果（P0-2 产物 skills-inventory.yaml） */
export interface Inventory {
  exported_at?: string
  agent_version?: string
  roots: { path: string; manifest_sha256?: string }[]
  skills: { name: string; root: string; file: string; sha256?: string }[]
}

export async function loadInventory(path: string): Promise<Inventory | null> {
  try {
    const data = parse(await readFile(path, 'utf8')) as Inventory
    return data && Array.isArray(data.skills) ? data : null
  } catch {
    return null
  }
}

/** dsh/eline CLI 的 home：Profile.dsh.home 优先，其次 DSH_HOME，缺省 ~/.dsh */
export function dshHome(profile?: { dsh?: { home?: string } }): string {
  return profile?.dsh?.home || process.env.DSH_HOME || join(homedir(), '.dsh')
}

/** 稳定哈希：roots 下全部文件的 (relpath, content) 排序后拼接 */
export async function hashTree(roots: readonly string[]): Promise<string> {
  const parts: string[] = []
  for (const root of roots) {
    const files = await listFiles(root).catch(() => [] as string[])
    for (const rel of files.sort()) {
      const content = await readFile(join(root, rel), 'utf8').catch(() => '')
      parts.push(`${rel}\u0000${content}`)
    }
  }
  return sha256(parts.join('\u0001'))
}

async function listFiles(dir: string, prefix = ''): Promise<string[]> {
  const out: string[] = []
  let entries
  try {
    entries = await readdir(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const e of entries) {
    if (e.name === 'node_modules' || e.name === '.git') continue
    const rel = prefix === '' ? e.name : `${prefix}/${e.name}`
    if (e.isDirectory()) out.push(...(await listFiles(join(dir, e.name), rel)))
    else if (e.isFile()) out.push(rel)
  }
  return out
}

/** ElineAI 侧 profile 哈希（method: files）：默认三件套，缺哪个跳过哪个 */
async function elineProfileFingerprint(profile: Profile): Promise<Snapshot['eline_profile']> {
  const dir = join(dshHome(profile), 'profiles', profile.dsh.profile)
  const files = profile.dsh.profile_files ?? ['cordis.patch.yml', 'cordis.yml', 'package.json']
  const parts: string[] = []
  const used: string[] = []
  for (const f of files) {
    const text = await readFile(join(dir, f), 'utf8').catch(() => null)
    if (text === null) continue
    used.push(f)
    parts.push(`# ${f}\n${text}`)
  }
  return { method: 'files', sha256: sha256(parts.join('\n\u0000')), files: used }
}

async function skillsBaseFingerprint(profile: Profile): Promise<{ value: string; approx: boolean }> {
  if (profile.skills_mode === 'isolated' || profile.skills_mode === 'bundled-copy') {
    return { value: await hashTree(profile.skills_roots ?? []), approx: false }
  }
  // 过渡期：默认根清单哈希的近似（盘点存在则用盘点，否则对 CLI 用户根兜底）
  const inv = profile.skills_inventory ? await loadInventory(profile.skills_inventory) : null
  if (inv) {
    const names = inv.skills.map((s) => `${s.name}\u0000${s.sha256 ?? ''}`).sort()
    return { value: sha256(names.join('\u0001')), approx: false }
  }
  return { value: await hashTree([join(dshHome(profile), 'skills')]), approx: true }
}

export async function computeSnapshot(
  profile: Profile,
  evalsetFile: string,
  evalsetSha: string,
  agent: { name: string; version: string | null },
): Promise<Snapshot> {
  const base = await skillsBaseFingerprint(profile)
  const snap: Snapshot = {
    evalset: { file: evalsetFile, sha256: evalsetSha },
    runner_profile: sha256(JSON.stringify(profile)),
    eline_profile: await elineProfileFingerprint(profile),
    skills_mode: profile.skills_mode,
    skills_base: base.value,
    agent: { name: agent.name, version: agent.version },
  }
  if (base.approx) (snap as Record<string, unknown>).skills_base_approx = true
  return snap
}

/** 盘点是否过期：agent 版本或默认根清单哈希与盘点记录不符 */
export async function inventoryStale(profile: Profile): Promise<string | null> {
  if (!profile.skills_inventory) return null
  const inv = await loadInventory(profile.skills_inventory)
  if (!inv) return 'skills_inventory 文件不存在或格式不对'
  if (inv.agent_version && profile.dsh.bin) {
    // agent 版本由适配器探测，这里只比对清单哈希；版本比对在 orchestrator 传入时做
  }
  for (const r of inv.roots ?? []) {
    if (!r.manifest_sha256) continue
    const now = await hashTree([r.path]).catch(() => null)
    if (now !== null && now !== r.manifest_sha256) {
      return `根 ${r.path} 的 skill 清单与盘点记录不一致（inventory 过期）`
    }
  }
  return null
}

/** 续跑一致性检查的键（01 §5）：任一不同即拒绝 */
export const RESUME_KEYS = ['evalset', 'runner_profile', 'eline_profile', 'skills_mode', 'skills_base'] as const
