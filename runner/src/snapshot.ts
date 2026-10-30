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
  /** cordis 三件套解析出的被测身份（doc/18 §2.1）：persona/plugins 与有效模型 */
  cordis?: CordisIdentity | null
  skills_mode: string
  skills_base: string
  /** 实体技能根与导出清单（<root>.manifest.json）记录的哈希不一致时为 true */
  skills_base_stale?: boolean
  agent: { name: string; version: string | null }
  models_observed?: string[]
}

/** cordis 合成后的插件条目（doc/18 §2.1）：insert 已展开，按 id 去重覆盖 */
interface CordisEntry { id: string; name?: unknown; config?: unknown }

/** snapshot 的 cordis 段：persona_sha / plugins_sha 与有效模型（覆盖值优先在 subjectOf 里取） */
export interface CordisIdentity {
  persona_sha: string | null
  plugins_sha: string | null
  model: string | null
  reasoning_effort: string | null
  model_provider: string | null
  /** 取值来源：bundle_or_missing 时 run 启动给提示（doc/18 §2.1 兜底） */
  persona_from: 'cordis' | 'bundle_or_missing'
  model_from: 'cordis' | 'bundle_or_missing'
}

function yamlArray(text: string | null): unknown[] | null {
  if (text === null) return null
  try {
    const v = parse(text)
    return Array.isArray(v) ? v : null
  } catch {
    return null
  }
}

/** 合成 cordis 条目：cordis.yml → cordis.patch.yml；`- id:` 覆盖同 id 条目、`- insert:` 插入。
 * bundle（package.json dsh.profile.bundles）的内容不在本地文件里、解析不到（doc/18 §2.1 盲区），
 * 其清单并入 plugins_sha 载荷，生效差异由 header 观测兜底。 */
function composeCordis(cordisYml: string | null, patchYml: string | null): CordisEntry[] | null {
  if (cordisYml === null && patchYml === null) return null
  const out: CordisEntry[] = []
  const apply = (entries: unknown): void => {
    if (!Array.isArray(entries)) return
    for (const e of entries) {
      if (!e || typeof e !== 'object' || Array.isArray(e)) continue
      const entry = e as { id?: unknown; insert?: unknown }
      if (Array.isArray(entry.insert)) { apply(entry.insert); continue }
      if (typeof entry.id !== 'string' || entry.id === '') continue
      const i = out.findIndex((x) => x.id === entry.id)
      if (i >= 0) out[i] = entry as CordisEntry
      else out.push(entry as CordisEntry)
    }
  }
  apply(yamlArray(cordisYml))
  apply(yamlArray(patchYml))
  return out
}

/** cordis 三件套 → 被测身份段（doc/18 §2.1）。文件全缺（老环境）返回 null，
 * subject 各字段按 null 落——同套旧配置不因缺字段分裂。 */
export async function cordisIdentity(profile: Profile): Promise<CordisIdentity | null> {
  const dir = join(dshHome(profile), 'profiles', profile.dsh.profile)
  const read = async (f: string): Promise<string | null> =>
    await readFile(join(dir, f), 'utf8').catch(() => null)
  const [cordisYml, patchYml, pkgRaw] = await Promise.all([
    read('cordis.yml'), read('cordis.patch.yml'), read('package.json'),
  ])
  const entries = composeCordis(cordisYml, patchYml)
  if (entries === null) return null
  const bundles: string[] = (() => {
    if (pkgRaw === null) return []
    try {
      const b = (JSON.parse(pkgRaw) as { dsh?: { profile?: { bundles?: unknown } } }).dsh?.profile?.bundles
      return Array.isArray(b) ? b.filter((x): x is string => typeof x === 'string') : []
    } catch {
      return []
    }
  })()
  const byId = new Map(entries.map((e) => [e.id, e]))
  const promptPlugins = profile.prompt_plugins ?? ['persona']      // doc/18 §9-1
  const personaPayload: Record<string, unknown> = {}
  let personaFound = false
  for (const id of promptPlugins) {
    const e = byId.get(id)
    if (e) { personaPayload[id] = e.config ?? null; personaFound = true }
  }
  const pluginsPayload = {
    entries: entries
      .filter((e) => !promptPlugins.includes(e.id) && e.id !== 'agent-default-model')
      .map((e) => ({ id: e.id, name: e.name ?? null, config: e.config ?? null })),
    bundles,
  }
  const adm = byId.get('agent-default-model')
  const cfg = (adm?.config ?? {}) as Record<string, unknown>
  const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
  return {
    persona_sha: personaFound ? sha256(canonicalJson(personaPayload)) : null,
    plugins_sha: sha256(canonicalJson(pluginsPayload)),
    model: str(cfg.model),
    reasoning_effort: str(cfg.reasoningEffort),
    model_provider: str(cfg.provider),
    persona_from: personaFound ? 'cordis' : 'bundle_or_missing',
    model_from: adm ? 'cordis' : 'bundle_or_missing',
  }
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

async function manifestStale(root: string, value: string): Promise<boolean> {
  const raw = await readFile(`${root}.manifest.json`, 'utf8').catch(() => null)
  if (raw === null) return false
  try {
    const manifest = JSON.parse(raw) as { sha256?: string }
    return typeof manifest.sha256 === 'string' && manifest.sha256 !== value
  } catch {
    return false
  }
}

async function skillsBaseFingerprint(
  profile: Profile,
): Promise<{ value: string; approx: boolean; stale: boolean }> {
  if (profile.skills_mode === 'isolated' || profile.skills_mode === 'bundled-copy') {
    const roots = profile.skills_roots ?? []
    const value = await hashTree(roots)
    const stale = roots.length === 1 ? await manifestStale(roots[0], value) : false
    return { value, approx: false, stale }
  }
  // 过渡期：默认根清单哈希的近似（盘点存在则用盘点，否则对 CLI 用户根兜底）
  const inv = profile.skills_inventory ? await loadInventory(profile.skills_inventory) : null
  if (inv) {
    const names = inv.skills.map((s) => `${s.name}\u0000${s.sha256 ?? ''}`).sort()
    return { value: sha256(names.join('\u0001')), approx: false, stale: false }
  }
  return { value: await hashTree([join(dshHome(profile), 'skills')]), approx: true, stale: false }
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
    cordis: await cordisIdentity(profile),
    skills_mode: profile.skills_mode,
    skills_base: base.value,
    agent: { name: agent.name, version: agent.version },
  }
  if (base.approx) (snap as Record<string, unknown>).skills_base_approx = true
  if (base.stale) snap.skills_base_stale = true
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

// ---------------- 被测配置指纹（doc/15 §3.1）：与 scoring/subject.py 双实现 ----------------
// 同一 manifest fixture 两端必须得出相同指纹（tests/subject-fixture.json 对拍）。

/** 规范化 JSON：键排序 + 紧凑分隔符，等价 Python json.dumps(sort_keys=True, ensure_ascii=False, separators=(',',':')) */
export function canonicalJson(value: unknown): string {
  const out: string[] = []
  const walk = (v: unknown): void => {
    if (v === null || typeof v === 'number' || typeof v === 'boolean') {
      out.push(String(v))
    } else if (typeof v === 'string') {
      out.push(JSON.stringify(v))
    } else if (Array.isArray(v)) {
      out.push('[')
      v.forEach((x, i) => { if (i) out.push(','); walk(x) })
      out.push(']')
    } else if (v && typeof v === 'object') {
      out.push('{')
      const keys = Object.keys(v as Record<string, unknown>).sort()
      keys.forEach((k, i) => {
        if (i) out.push(',')
        out.push(JSON.stringify(k), ':')
        walk((v as Record<string, unknown>)[k])
      })
      out.push('}')
    } else {
      out.push('null')
    }
  }
  walk(value)
  return out.join('')
}

export type SubjectFields = Record<string, unknown>

/** 被测配置字段表（doc/15 §3.1 + doc/18 §2.1）。缺省与 profile 解析一致：dsh.profile 'headless'、skill_tool 'skill'。
 * model/effort/provider 取有效值：profile 覆盖 > cordis agent-default-model（bundle 提供时拿不到 → null，
 * 由观测兜底）；eline_profile_sha 移出 subject（doc/18 §2.1），仍在 snapshot 供 RESUME_KEYS 用。 */
export function subjectOf(profile: Profile, snapshot: Snapshot): SubjectFields {
  const dsh = (profile.dsh ?? {}) as { profile?: string }
  const cord = (snapshot as { cordis?: CordisIdentity | null }).cordis ?? null
  const agent = snapshot.agent ?? { name: 'dsh-cli', version: null as string | null }
  return {
    agent: { name: agent.name ?? 'dsh-cli', version: agent.version ?? null },
    dsh_profile: dsh.profile ?? 'headless',
    model: profile.model ?? cord?.model ?? null,
    model_provider: profile.model_provider ?? cord?.model_provider ?? null,
    persona_sha: cord?.persona_sha ?? null,
    plugins_sha: cord?.plugins_sha ?? null,
    reasoning_effort: profile.reasoning_effort ?? cord?.reasoning_effort ?? null,
    routing: profile.routing ?? {},
    skill_tool: profile.skill_tool ?? 'skill',
    skills_base: snapshot.skills_base ?? null,
    skills_mode: snapshot.skills_mode ?? profile.skills_mode ?? null,
  }
}

/** subject 段（写进 run.json；scoring.register_run 优先使用，不再自行计算）。 */
export function subjectSection(profile: Profile, snapshot: Snapshot): {
  subject_fp: string
  fields: SubjectFields
  incomplete: boolean
} {
  const fields = subjectOf(profile, snapshot)
  const incomplete = fields.skills_mode === null || fields.skills_base === null
  return { subject_fp: sha256(canonicalJson(fields)).slice(0, 16), fields, incomplete }
}
