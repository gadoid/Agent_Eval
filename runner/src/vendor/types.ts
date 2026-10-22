// 自上游 src/types.ts 裁剪：只保留 collector.ts 用到的类型，定义原样未改。

/** 一次 tool/call 的记录（arguments 统一序列化为 JSON 字符串） */
export interface ToolCallRecord {
  name: string
  callId?: string
  argsJson: string
}

/** 一次 tool/result 的记录（text 为结果纯文本，兼容真实落盘与旧形状） */
export interface ToolResultRecord {
  name: string
  callId?: string
  text: string
}

/**
 * 聚合 token 用量（分字段；与 DSH TokenUsage 对齐，计数互斥：
 * inputTokens 仅未缓存输入，缓存部分单独计入 cacheRead/cacheWrite）。
 * total = input + output + reasoning——cacheRead 是缓存命中读回，多步会话里
 * 同一段缓存每步重复读，全额累加会让 max_tokens 随步数膨胀；故不进 total，
 * 但 cacheRead/cacheWrite 仍单独保留供观察。
 */
export interface TokenUsage {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  reasoning: number
  total: number
}

/** 工具硬错误（tool/result 的 data.error 或 isError） */
export interface ToolError {
  /** 工具名（经 tool/call 的 callId 关联；拿不到则为 callId 或 '<unknown>'） */
  name: string
  /** 错误摘要（截断到 200 字符） */
  error: string
}

export function emptyTokenUsage(): TokenUsage {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, total: 0 }
}

/** collector 从 session.jsonl 提取的观测结果 */
export interface CollectedTrace {
  /** 最后一个 turn/end 的 reason.kind */
  turnEnd?: string
  /** tool/call 名称序列（按出现顺序）——保留旧字段兼容旧报告 */
  toolsCalled: string[]
  /** tool/call 完整记录（含 arguments 序列化串） */
  toolCalls: ToolCallRecord[]
  /** tool/result 完整记录（含结果文本） */
  toolResults: ToolResultRecord[]
  /** 最后一条 assistant/message 的文本 */
  finalText: string
  /** step/end 事件数 */
  steps: number
  /** 累加 assistant/message 的 usage 各字段（含 cacheRead/cacheWrite/reasoning） */
  tokens: TokenUsage
  /** tool/result 的硬错误列表（无则为空数组） */
  toolErrors: ToolError[]
  /** 成功解析的事件帧数 */
  events: number
  /** 跳过的不良行数 */
  skippedLines: number
  /** request/header 观测到的模型标识（去重，按出现顺序）；日志无此字段为空 */
  models: string[]
}
