import type { InjectionOp, LoadedSkill, Observations, Profile, TurnTrace } from '../domain.js'
import type { Probe } from '../ports.js'
import { sha256 } from '../profile.js'
import type { ToolCallRecord, ToolResultRecord } from '../vendor/types.js'

/** dsh-tool-skill 成功结果的渲染标记：<skill_content name="..."> */
const SKILL_CONTENT = /<skill_content\b/

interface SkillLoad {
  name: string
  /** 成功加载的内容；失败为 null */
  text: string | null
}

/** 把 skill 工具调用与结果按 callId 配对（无 callId 的旧日志按出现顺序配对） */
function skillLoads(toolCalls: readonly ToolCallRecord[], toolResults: readonly ToolResultRecord[], skillTool: string): SkillLoad[] {
  const resultsById = new Map<string, ToolResultRecord>()
  const unmatched: ToolResultRecord[] = []
  for (const r of toolResults) {
    if (r.callId) resultsById.set(r.callId, r)
    else if (r.name === skillTool) unmatched.push(r)
  }
  const loads: SkillLoad[] = []
  for (const call of toolCalls) {
    if (call.name !== skillTool) continue
    const result = call.callId ? resultsById.get(call.callId) : unmatched.shift()
    const ok = result !== undefined && SKILL_CONTENT.test(result.text)
    loads.push({ name: skillNameFromArgs(call.argsJson), text: ok && result ? result.text : null })
  }
  return loads
}

/**
 * 模型主动加载的 skill：工具名为 profile.skill_tool、参数 { name } 的调用；
 * 结果按 callId 关联，含 <skill_content 视为成功，内容取 sha256。
 */
export function extractLoadedSkills(toolCalls: readonly ToolCallRecord[], toolResults: readonly ToolResultRecord[], skillTool: string): LoadedSkill[] {
  return skillLoads(toolCalls, toolResults, skillTool).map((l) =>
    l.text === null ? { name: l.name, ok: false } : { name: l.name, ok: true, sha256: sha256(l.text) },
  )
}

function skillNameFromArgs(argsJson: string): string {
  try {
    const args = JSON.parse(argsJson) as unknown
    if (args && typeof args === 'object' && typeof (args as { name?: unknown }).name === 'string') return (args as { name: string }).name
  } catch {
    // 参数不是 JSON：原样记录，便于排查
  }
  return argsJson === '' ? '<unknown>' : `<unparsed:${argsJson.slice(0, 80)}>`
}

export const skillProbe: Probe<'skills'> = {
  key: 'skills',
  observe: (trace, profile) => extractLoadedSkills(trace.toolCalls, trace.toolResults, profile.skill_tool),
}

/** 注入文件所属 skill 名：`trade-documents.md` → trade-documents；`trade-documents/SKILL.md` → trade-documents */
function skillNameOfFile(file: string): string {
  const parts = file.split(/[\\/]/)
  return parts.length > 1 ? parts[0] : parts[0].replace(/\.[^.]+$/, '')
}

/**
 * 注入是否被 agent 看见：看本轮已加载的 skill 内容。
 * 改写（value 非空）命中改写文本即生效；删除（value 为空）要求原 key 不再出现。
 * 本轮没加载任何 skill 时返回 undefined（不可判，不是「未生效」）。
 */
export function extractInjectionsObserved(
  toolCalls: readonly ToolCallRecord[],
  toolResults: readonly ToolResultRecord[],
  skillTool: string,
  injections: readonly InjectionOp[],
): Record<string, boolean> | undefined {
  if (injections.length === 0) return undefined
  const loaded = skillLoads(toolCalls, toolResults, skillTool).filter((l) => l.text !== null)
  if (loaded.length === 0) return undefined
  const observed: Record<string, boolean> = {}
  for (const op of injections) {
    if (op.value !== '') {
      observed[op.id] = loaded.some((l) => (l.text as string).includes(op.value))
      continue
    }
    const named = loaded.filter((l) => l.name === skillNameOfFile(op.file))
    const scope = named.length > 0 ? named : loaded
    observed[op.id] = scope.every((l) => !(l.text as string).includes(op.key))
  }
  return observed
}

export const toolProbe: Probe<'tools'> = {
  key: 'tools',
  observe: (trace) => trace.toolCalls.map((c) => ({ name: c.name, args: c.argsJson })),
}

export const toolErrorProbe: Probe<'tool_errors'> = {
  key: 'tool_errors',
  observe: (trace) => trace.toolErrors,
}

export const usageProbe: Probe<'usage'> = {
  key: 'usage',
  observe: (trace) => ({ steps: trace.steps, tokens: trace.tokens }),
}

/** 反问（ask_user 工具调用）与 timed 自动放行信号 */
const ASK_USER_TOOL = 'ask_user'
const AUTO_RELEASE = /timed?.?(out)?|跳过|skip/i

export function extractAskUser(toolCalls: readonly ToolCallRecord[], toolResults: readonly ToolResultRecord[]): { count: number; auto: boolean } {
  const calls = toolCalls.filter((c) => c.name === ASK_USER_TOOL)
  if (calls.length === 0) return { count: 0, auto: false }
  const resultsById = new Map(toolResults.filter((r) => r.callId).map((r) => [r.callId as string, r]))
  const auto = calls.some((c) => {
    const r = c.callId ? resultsById.get(c.callId) : undefined
    return r !== undefined && AUTO_RELEASE.test(r.text)
  })
  return { count: calls.length, auto }
}

/** 依次运行全部探针，组装本轮观测（ask_user 并入工具探针、model 并入用量探针输出） */
export function observe(trace: TurnTrace, profile: Profile, injections: readonly InjectionOp[] = []): Observations {
  return {
    skills: skillProbe.observe(trace, profile),
    tools: toolProbe.observe(trace, profile),
    tool_errors: toolErrorProbe.observe(trace, profile),
    usage: usageProbe.observe(trace, profile),
    ask_user: extractAskUser(trace.toolCalls, trace.toolResults),
    model: trace.models?.[0] ?? null,
    injections_observed: extractInjectionsObserved(trace.toolCalls, trace.toolResults, profile.skill_tool, injections),
  }
}
