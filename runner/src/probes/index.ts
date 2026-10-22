import type { LoadedSkill, Observations, Profile, TurnTrace } from '../domain.js'
import type { Probe } from '../ports.js'
import { sha256 } from '../profile.js'
import type { ToolCallRecord, ToolResultRecord } from '../vendor/types.js'

/** dsh-tool-skill 成功结果的渲染标记：<skill_content name="..."> */
const SKILL_CONTENT = /<skill_content\b/

/**
 * 模型主动加载的 skill：工具名为 profile.skill_tool、参数 { name } 的调用；
 * 结果按 callId 关联，含 <skill_content 视为成功，内容取 sha256。
 */
export function extractLoadedSkills(toolCalls: readonly ToolCallRecord[], toolResults: readonly ToolResultRecord[], skillTool: string): LoadedSkill[] {
  const resultsById = new Map<string, ToolResultRecord>()
  const unmatched: ToolResultRecord[] = []
  for (const r of toolResults) {
    if (r.callId) resultsById.set(r.callId, r)
    else if (r.name === skillTool) unmatched.push(r)
  }
  const loaded: LoadedSkill[] = []
  for (const call of toolCalls) {
    if (call.name !== skillTool) continue
    const name = skillNameFromArgs(call.argsJson)
    const result = call.callId ? resultsById.get(call.callId) : unmatched.shift()
    const ok = result !== undefined && SKILL_CONTENT.test(result.text)
    loaded.push(ok && result ? { name, ok, sha256: sha256(result.text) } : { name, ok })
  }
  return loaded
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
export function observe(trace: TurnTrace, profile: Profile): Observations {
  return {
    skills: skillProbe.observe(trace, profile),
    tools: toolProbe.observe(trace, profile),
    tool_errors: toolErrorProbe.observe(trace, profile),
    usage: usageProbe.observe(trace, profile),
    ask_user: extractAskUser(trace.toolCalls, trace.toolResults),
    model: trace.models?.[0] ?? null,
  }
}
