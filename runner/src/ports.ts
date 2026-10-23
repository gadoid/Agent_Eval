/**
 * 四个扩展点。核心流程（EpisodeRunner、Orchestrator）只依赖这些接口：
 * - AgentAdapter：被测系统如何开启会话、发送一轮、结束会话；
 * - UserDriver：下一条用户消息是什么（脚本，或以后的模拟用户）；
 * - Intervention：对照实验的干预（不干预、强制加载 skill……）；
 * - Probe：从标准轨迹中提取观测信号。
 */
import type { AdapterCapabilities, EvalRow, Mode, Observations, Profile, TurnOutcome, TurnRecord, TurnTrace } from './domain.js'

export interface SessionContext {
  /** 本段会话独占的目录，适配器在其中放配置与日志 */
  runDir: string
  /** 本段会话独占的工作区，所有轮次共用 */
  workspace: string
  timeoutMs: number
  /** isolated 模式：本会话 skill 副本目录（overlay 指向它，includeDefaultRoots: false） */
  skillsDirs?: readonly string[]
  /** 逐会话环境变量覆盖（bundled-copy 模式的 DSH_BUNDLED_SKILL_DIR 等），spawn 时叠加在 process.env 之上 */
  env?: Readonly<Record<string, string>>
}

export interface PrefillTurn {
  user: string
  assistant: string
}

export interface AgentSession {
  /** 发送一条用户消息，返回本轮结果；不抛异常，失败体现在 status 中 */
  send(message: string): Promise<TurnOutcome>
  /** 结束会话，返回日志等产物位置 */
  close(): Promise<{ sessionLog: string | null; sessionId: string | null }>
}

export interface AgentAdapter {
  readonly name: string
  readonly version: string | undefined
  readonly capabilities: AdapterCapabilities
  /** prefill 仅在 capabilities.prefillHistory 为 true 时传入 */
  open(context: SessionContext, prefill?: readonly PrefillTurn[]): Promise<AgentSession>
  /** 可选：整个运行结束后释放适配器持有的长生命周期资源（如 web 服务、浏览器） */
  dispose?(): Promise<void>
}

export interface UserDriver {
  /** 根据已完成的轮次给出下一条用户消息；返回 null 表示对话结束 */
  next(history: readonly TurnRecord[]): string | null
}

export type InterventionPlan = { ok: true; forcedSkills: string[] } | { ok: false; reason: string }

export interface Intervention {
  readonly mode: Mode
  /** 规划阶段判断该用例能否施加本干预 */
  plan(row: EvalRow, profile: Profile): InterventionPlan
  /** 发送前改写消息；liveIndex 为本段会话中第几条实际发送的消息（从 0 开始） */
  transform(message: string, liveIndex: number, plan: Extract<InterventionPlan, { ok: true }>): string
}

export interface Probe<K extends keyof Observations = keyof Observations> {
  readonly key: K
  observe(trace: TurnTrace, profile: Profile): Observations[K]
}
