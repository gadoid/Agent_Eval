import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { parse } from 'yaml'
import type { Profile } from './domain.js'

export class ConfigError extends Error {}

const SKILL_NAME = /^[a-z0-9][a-z0-9-]*$/

/** 校验并补全缺省值；所有问题一次报出 */
export function parseProfile(text: string, source = '<profile>'): Profile {
  let raw: unknown
  try {
    raw = parse(text)
  } catch (err) {
    throw new ConfigError(`${source}: YAML 格式错误：${(err as Error).message}`)
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ConfigError(`${source}: 顶层必须是映射`)
  }
  const r = raw as Record<string, unknown>
  const errors: string[] = []

  if (typeof r.id !== 'string' || !/^[A-Za-z0-9._-]+$/.test(r.id)) {
    errors.push('id 必填，只能包含字母、数字、. _ -')
  }
  const dsh = (r.dsh ?? {}) as Record<string, unknown>
  if (typeof dsh !== 'object' || Array.isArray(dsh)) errors.push('dsh 必须是映射')
  if (dsh.bin !== undefined && (typeof dsh.bin !== 'string' || dsh.bin.trim() === '')) {
    errors.push('dsh.bin 必须是非空字符串')
  }
  if (dsh.profile !== undefined && (typeof dsh.profile !== 'string' || dsh.profile === '')) {
    errors.push('dsh.profile 必须是非空字符串')
  }
  if (dsh.timeout_ms !== undefined && (!Number.isInteger(dsh.timeout_ms) || (dsh.timeout_ms as number) <= 0)) {
    errors.push('dsh.timeout_ms 必须是正整数')
  }
  if (dsh.mirror_sessions !== undefined && (typeof dsh.mirror_sessions !== 'string' || (dsh.mirror_sessions as string).trim() === '')) {
    errors.push('dsh.mirror_sessions 必须是非空字符串（客户端会话目录的绝对路径）')
  }
  if (dsh.home !== undefined && (typeof dsh.home !== 'string' || (dsh.home as string).trim() === '')) {
    errors.push('dsh.home 必须是非空字符串（CLI 通道 DSH_HOME 的绝对路径）')
  }
  const dshEnv: Record<string, string> = {}
  if (dsh.env !== undefined) {
    if (typeof dsh.env !== 'object' || dsh.env === null || Array.isArray(dsh.env)) {
      errors.push('dsh.env 必须是「变量名: 值」映射')
    } else {
      for (const [k, v] of Object.entries(dsh.env as Record<string, unknown>)) {
        if (typeof v !== 'string') errors.push(`dsh.env.${k} 的值必须是字符串`)
        else dshEnv[k] = v
      }
    }
  }
  if (r.skill_tool !== undefined && (typeof r.skill_tool !== 'string' || r.skill_tool === '')) {
    errors.push('skill_tool 必须是非空字符串')
  }

  const skillsMode = r.skills_mode === undefined ? 'default-roots' : r.skills_mode
  if (skillsMode !== 'default-roots' && skillsMode !== 'isolated' && skillsMode !== 'bundled-copy') {
    errors.push('skills_mode 只能是 default-roots、isolated 或 bundled-copy')
  }
  const skillsRoots: string[] = []
  if (r.skills_roots !== undefined && r.skills_roots !== null) {
    if (!Array.isArray(r.skills_roots) || r.skills_roots.some((x) => typeof x !== 'string' || x.trim() === '')) {
      errors.push('skills_roots 必须是非空字符串数组（isolated 必填，按 P0-2 盘点结果填写）')
    } else {
      skillsRoots.push(...(r.skills_roots as string[]).map((x) => x.trim()).filter(Boolean))
    }
  }
  if ((skillsMode === 'isolated' || skillsMode === 'bundled-copy') && skillsRoots.length === 0) {
    errors.push(`skills_mode: ${skillsMode} 必须填写 skills_roots（bundled-copy 为从产品 asar 导出的实体技能根，tools/export-skills.mjs）`)
  }
  if (r.skills_inventory !== undefined && typeof r.skills_inventory !== 'string') {
    errors.push('skills_inventory 必须是字符串（盘点结果文件路径）')
  }
  if (skillsMode === 'isolated' && r.skills_inventory === undefined) {
    errors.push('skills_mode: isolated 必须填写 skills_inventory（P0-2 盘点结果；bundled-copy 单根副本不需要）')
  }
  const budget: { max_tokens?: number; max_hours?: number } = {}
  if (r.budget !== undefined && r.budget !== null) {
    const b = r.budget as { max_tokens?: unknown; max_hours?: unknown }
    if (typeof b !== 'object' || Array.isArray(b)) errors.push('budget 必须是 { max_tokens?, max_hours? }')
    else {
      if (b.max_tokens !== undefined && (!Number.isFinite(b.max_tokens) || (b.max_tokens as number) <= 0)) errors.push('budget.max_tokens 必须是正数')
      else if (b.max_tokens !== undefined) budget.max_tokens = b.max_tokens as number
      if (b.max_hours !== undefined && (!Number.isFinite(b.max_hours) || (b.max_hours as number) <= 0)) errors.push('budget.max_hours 必须是正数')
      else if (b.max_hours !== undefined) budget.max_hours = b.max_hours as number
    }
  }
  const profileFiles: string[] = []
  if (dsh.profile_files !== undefined) {
    if (!Array.isArray(dsh.profile_files) || dsh.profile_files.some((x) => typeof x !== 'string')) {
      errors.push('dsh.profile_files 必须是字符串数组')
    } else {
      profileFiles.push(...(dsh.profile_files as string[]))
    }
  }

  const routing: Record<string, string[]> = {}
  const web = r.web as { home?: unknown; port?: unknown; headless?: unknown } | undefined | null
  if (web !== undefined && web !== null) {
    if (typeof web !== 'object' || Array.isArray(web)) errors.push('web 必须是 { home, port, headless? } 形式的映射')
    else {
      if (typeof web.home !== 'string' || web.home.trim() === '') errors.push('web.home 必须是非空字符串（web 服务的 DSH_HOME）')
      if (!Number.isInteger(web.port) || (web.port as number) < 1 || (web.port as number) > 65535) errors.push('web.port 必须是 1-65535 的整数')
      if (web.headless !== undefined && typeof web.headless !== 'boolean') errors.push('web.headless 必须是布尔值')
    }
  }
  if (r.routing !== undefined && r.routing !== null) {
    if (typeof r.routing !== 'object' || Array.isArray(r.routing)) {
      errors.push('routing 必须是「业务主题: [skill, ...]」形式的映射')
    } else {
      for (const [topic, value] of Object.entries(r.routing as Record<string, unknown>)) {
        const skills = typeof value === 'string' ? [value] : value
        if (!Array.isArray(skills) || skills.length === 0) {
          errors.push(`routing.${topic} 必须是非空的 skill 名列表`)
          continue
        }
        const bad = skills.filter((s) => typeof s !== 'string' || !SKILL_NAME.test(s))
        if (bad.length > 0) {
          errors.push(`routing.${topic} 含非法 skill 名：${bad.map(String).join(', ')}（应为小写字母、数字和 -）`)
          continue
        }
        routing[topic] = skills as string[]
      }
    }
  }

  for (const key of Object.keys(r)) {
    if (!['id', 'description', 'model', 'dsh', 'skill_tool', 'routing', 'web',
          'skills_mode', 'skills_roots', 'skills_inventory', 'budget'].includes(key)) {
      errors.push(`未知字段 ${key}`)
    }
  }
  if (errors.length > 0) {
    throw new ConfigError(`${source}:\n  ${errors.join('\n  ')}`)
  }

  return {
    id: r.id as string,
    description: typeof r.description === 'string' ? r.description : undefined,
    model: typeof r.model === 'string' ? r.model : undefined,
    dsh: {
      bin: dsh.bin as string | undefined,
      profile: (dsh.profile as string | undefined) ?? 'headless',
      timeout_ms: (dsh.timeout_ms as number | undefined) ?? 600_000,
      mirror_sessions: (dsh.mirror_sessions as string | undefined)?.trim() || undefined,
      profile_files: profileFiles.length > 0 ? profileFiles : undefined,
      home: (dsh.home as string | undefined)?.trim() || undefined,
      env: Object.keys(dshEnv).length > 0 ? dshEnv : undefined,
    },
    skills_mode: skillsMode as Profile['skills_mode'],
    skills_roots: skillsRoots.length > 0 ? skillsRoots : undefined,
    skills_inventory: typeof r.skills_inventory === 'string' ? r.skills_inventory : undefined,
    budget: Object.keys(budget).length > 0 ? budget : undefined,
    skill_tool: (r.skill_tool as string | undefined) ?? 'skill',
    routing,
    web:
      web && typeof web === 'object' && !Array.isArray(web)
        ? { home: (web.home as string).trim(), port: web.port as number, headless: (web.headless as boolean | undefined) ?? true }
        : undefined,
  }
}

export async function loadProfile(path: string): Promise<{ profile: Profile; sha256: string }> {
  let text: string
  try {
    text = await readFile(path, 'utf8')
  } catch (err) {
    throw new ConfigError(`无法读取 Profile ${path}：${(err as Error).message}`)
  }
  return { profile: parseProfile(text, path), sha256: sha256(text) }
}

export function sha256(data: string | Buffer): string {
  return createHash('sha256').update(data).digest('hex')
}

/** 强制加载模式下该用例要注入的 skill；没有 topic 或路由缺失返回 null */
export function forcedSkillsFor(topic: string | null | undefined, profile: Profile): string[] | null {
  if (!topic) return null
  const skills = profile.routing[topic]
  return skills && skills.length > 0 ? skills : null
}

/**
 * DSH 的用户显式调用：用户消息中以空白分隔的 /name 会注入该 skill 的完整内容。
 * 前缀放在问题之前，问题本身原样保留。
 */
export function buildForcedPrompt(question: string, skills: readonly string[]): string {
  return `${skills.map((s) => `/${s}`).join(' ')} ${question}`
}
