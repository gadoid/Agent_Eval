/**
 * 领域模型。执行单位是 Episode（一段完整会话 = 用例 × 干预 × 第 k 次采样），
 * 由若干 Turn（用户输入 → agent 回答）组成；单轮用例就是只有一轮的 Episode。
 *
 * 文件契约：
 *   evalset.jsonl 的一行 = EvalRow（md2case build 产出）
 *   results.jsonl 的一行 = EpisodeRecord
 *   run.json            = RunManifest
 * 契约只增不改：新增字段一律可选；删除或改语义要升 RECORD_SCHEMA。
 */
import type { TokenUsage, ToolCallRecord, ToolError, ToolResultRecord } from './vendor/types.js'

export const RECORD_SCHEMA = 3

// ---------------------------------------------------------------- 输入

export type HistoryMode = 'live' | 'prefill'

export interface CompiledTurn {
  role: string
  content: string
}

/** 编译行中的注入操作（md2case v2 解析 @注入点/选项 的产物，运行期可直接物化） */
export interface InjectionOp {
  id: string
  point: string
  option: string
  rev: number
  file: string
  key: string
  value: string
}

export interface EvalRow {
  uid: string
  case_id: string
  variant_index: number
  evalset_version: string
  dim: string
  /** schema 2 为 {role, content}[]；旧 schema 1 为 string[]。经 userTurnsOf 归一 */
  turns: (string | CompiledTurn)[]
  /** 每轮之后记录的助手回复，与 turns 对齐；live 模式下只作参考 */
  assistant_refs?: (string | null)[]
  /** live：逐轮发送；prefill：前面各轮用记录的回复预置为历史，只发送最后一轮 */
  history?: HistoryMode
  topic?: string | null
  granularity?: string | null
  /** 编译行解析后的注入操作；无注入的用例为空 */
  injections?: InjectionOp[]
  /** facts、preconditions、path、evidence 等其余字段留给打分，执行侧原样内嵌进结果行 */
  [key: string]: unknown
}

/** 两种 turns 形态归一为用户消息列表（每条 user 消息一轮） */
export function userTurnsOf(row: Pick<EvalRow, 'turns'>): string[] {
  return row.turns.map((t) => (typeof t === 'string' ? t : t.content))
}

/** 干预方式：natural 不干预；forced 在第一轮前加 /skill名，由 DSH 确定性注入 skill */
export type Mode = 'natural' | 'forced'
export const MODES: readonly Mode[] = ['natural', 'forced']

/** skill 环境模式：default-roots（过渡期，P0 前）或 isolated（P0 后，副本机制）；ec 维度只在 isolated 运行 */
/** bundled-copy：逐 episode 物化 skill 副本并经 DSH_BUNDLED_SKILL_DIR 指向它
 * （产品 rc.6 实测有效的隔离路线，绕开 overlay；见 doc/03 §5.2 与 P0-3 验证记录） */
export type SkillsMode = 'default-roots' | 'isolated' | 'bundled-copy'

export interface Profile {
  id: string
  description?: string
  /** 仅记录；实际模型由 dsh profile 的配置决定 */
  model?: string
  dsh: {
    bin?: string
    profile: string
    timeout_ms: number
    /** 可选：每段会话结束后把 session 日志镜像到该目录（如 dsh/eline 客户端的会话树），实现双端留存 */
    mirror_sessions?: string
    /** 快照 eline_profile（method: files）的哈希清单；缺省取 profile 目录三件套 */
    profile_files?: string[]
    /** CLI 通道的 DSH_HOME（凭据与 profile 所在）；缺省 ~/.dsh */
    home?: string
    /** 启动 CLI 需要的额外环境变量（如 ElineAI 自带链路的 ELECTRON_RUN_AS_NODE） */
    env?: Record<string, string>
  }
  /** web-ui 适配器：驱动 dsh web 服务（独立 DSH_HOME），会话由 web 服务原生落盘，双端天然同步 */
  web?: { home: string; port: number; headless?: boolean }
  /** isolated 必填：评测基底（生产可见 skill 根），本 run 唯一 skill 来源 */
  skills_roots?: string[]
  /** P0-2 盘点结果（含同名 skill 的生效来源） */
  skills_inventory?: string
  /** 可选预算：dry-run 估算超出时提醒，不阻断 */
  budget?: { max_tokens?: number; max_hours?: number }
  skills_mode: SkillsMode
  /** 模型加载 skill 时调用的工具名 */
  skill_tool: string
  /** 业务主题 -> skill 名列表 */
  routing: Record<string, string[]>
}

// ---------------------------------------------------------------- 一轮的原始结果

/** 运行健康状态，不表示答对 */
export type TurnStatus = 'ok' | 'incomplete' | 'timeout' | 'error'

/** 适配器把被测系统的日志转换成的标准轨迹（只含本轮新增部分） */
export interface TurnTrace {
  finalText: string
  turnEnd?: string
  toolCalls: ToolCallRecord[]
  toolResults: ToolResultRecord[]
  toolErrors: ToolError[]
  steps: number
  tokens: TokenUsage
  /** request/header 事件中观测到的模型标识（该版本日志可得；适配器可不填） */
  models?: string[]
}

export interface TurnOutcome {
  status: TurnStatus
  error?: string
  /** 错误原因码：rate-limited / session-invalid / injection-key-missed / unknown */
  errorCode?: string
  exitCode?: number | null
  durationMs: number
  stderrTail?: string
  trace: TurnTrace
}

// ---------------------------------------------------------------- 观测

export interface LoadedSkill {
  name: string
  /** 工具结果中含 skill 内容即为 true */
  ok: boolean
  /** 加载内容的 sha256；skill 内容无版本号，用它判断两次运行的 skill 是否一致 */
  sha256?: string
}

export interface AskUserSignal {
  count: number
  /** 至少一次反问被 timed 自动放行（无人值守语义） */
  auto: boolean
}

export interface Observations {
  skills: LoadedSkill[]
  tools: { name: string; args: string }[]
  tool_errors: ToolError[]
  usage: { steps: number; tokens: TokenUsage }
  ask_user?: AskUserSignal
  /** 本轮 request/header 观测到的模型标识；日志无此字段为 null */
  model?: string | null
}

// ---------------------------------------------------------------- 结果

export interface TurnRecord {
  /** 对话中的轮次，从 1 开始 */
  index: number
  /** live：实际发送并得到真实回复；prefill：预置的历史，未发送 */
  source: 'live' | 'prefill'
  /** 实际发送的文本（含干预前缀）；prefill 轮为记录中的用户消息 */
  input: string
  output: string
  status: TurnStatus
  error?: string
  turn_end: string | null
  exit_code: number | null
  duration_ms: number
  observations: Observations | null
}

export interface EpisodeRecord {
  schema: number
  run_id: string
  episode_id: string
  uid: string
  case_id: string
  variant_index: number
  dim: string
  topic: string | null
  granularity: string | null
  mode: Mode
  /** 从 1 开始 */
  trial: number
  history: HistoryMode
  /** 完整环境快照只在 run.json 一份（01 第 5 节）；行内只留定位与注入留痕 */
  evalset_version: string
  /** 本会话 skill 副本（注入后）的 sha256；default-roots 模式为 null */
  skills_sha256?: string | null
  /** 注入物化留痕：每条注入的 point/option/rev 与 key 命中次数 */
  injections_applied?: { point: string; option: string; rev: number; found: number }[]
  /** 锚点会话标记（绊线用） */
  anchor?: boolean
  forced_skills: string[]
  /** 整段会话的运行状态：全部轮次 ok 为 ok，否则为失败那一轮的状态 */
  status: TurnStatus
  error?: string
  error_code?: string
  /** 失败发生在第几轮；未失败为 null */
  failed_turn: number | null
  turns: TurnRecord[]
  /** 最后一轮的回答，默认的打分对象 */
  final_output: string
  /** 各轮加载过的 skill（去重，保留首次出现顺序） */
  loaded_skills: string[]
  usage: { steps: number; tokens: TokenUsage }
  duration_ms: number
  artifacts: { session_log: string | null; session_id: string | null; run_dir: string; session_mirror?: string | null }
  /** 编译行原样内嵌（随行字段），scoring 只读 results.jsonl 即可判定 */
  case?: unknown
  finished_at: string
}

export interface SkippedRow {
  uid: string
  mode: Mode | null
  reason: string
}

export interface RunManifest {
  schema: number
  run_id: string
  started_at: string
  finished_at: string | null
  evalset: { path: string; sha256: string; versions: string[]; rows: number }
  profile: { path: string; sha256: string; content: Profile }
  agent: { name: string; version: string | null; capabilities: AdapterCapabilities }
  modes: Mode[]
  trials: number
  concurrency: number
  filters: { dims: string[]; only: string[] }
  episodes: { planned: number; resumed: number; executed: number }
  status_counts: Record<TurnStatus, number>
  error_rate: number
  skipped: SkippedRow[]
  /** 环境快照（01 第 5 节），run 开始时由工具计算 */
  snapshot?: Record<string, unknown>
  /** 本 run 纳入的锚点 uid（绊线用） */
  anchor_uids?: string[]
  /** dry-run/运行前的成本估算与本 run 实际用量 */
  estimate?: { total_turns: number; est_tokens?: number | null; est_hours?: number | null }
  actuals?: { total_tokens: number; hours: number }
  /** 非阻断告警：model_drift / skills_mismatch / inventory_stale */
  warnings?: string[]
}

export interface AdapterCapabilities {
  /** 能否在同一会话中逐轮发送 */
  multiTurn: boolean
  /** 能否把记录的助手回复预置为历史（history: prefill） */
  prefillHistory: boolean
}
