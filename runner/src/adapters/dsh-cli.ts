import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import type { AdapterCapabilities, Profile, TurnOutcome, TurnStatus, TurnTrace } from '../domain.js'
import type { AgentAdapter, AgentSession, SessionContext } from '../ports.js'
import { collectFromJsonl } from '../vendor/collector.js'
import { buildModelOverlayEntries, buildSkillsOverlayEntries } from '../materialize.js'
import { buildDshArgs, buildOverlayYaml, findSessionFile, resolveDshCommand, runOne } from '../vendor/dsh-kernel.js'
import type { DshCommand } from '../vendor/dsh-kernel.js'
import { emptyTokenUsage } from '../vendor/types.js'
import { flattenSessionLog } from '../mirror.js'
import { decodeZstdLog } from '../vendor/zstd.js'

/**
 * dsh 无头模式：每次调用只执行一个任务，--session-id 续接已持久化的会话。
 * 续接要求工作目录、会话持久化位置、agent 预设与创建时一致，因此同一段会话的
 * 所有轮次共用一个工作区与一份 overlay。dsh 无头模式不支持预置助手回复。
 */
export const DSH_CLI_CAPABILITIES: AdapterCapabilities = { multiTurn: true, prefillHistory: false }

export class DshAdapter implements AgentAdapter {
  readonly name = 'dsh-cli'
  readonly version: string | undefined
  readonly capabilities = DSH_CLI_CAPABILITIES
  private readonly dsh: DshCommand

  /** 构造时探测 dsh --version；找不到 dsh 直接抛错 */
  constructor(
    private readonly profile: Profile,
    resolve: (bin?: string) => DshCommand = resolveDshCommand,
  ) {
    this.dsh = resolve(profile.dsh.bin)
    this.version = this.dsh.version
  }

  async open(context: SessionContext): Promise<AgentSession> {
    const sessionRoot = join(context.runDir, 'session')
    const overlayPath = join(context.runDir, 'overlay.patch.yml')
    await mkdir(sessionRoot, { recursive: true })
    await mkdir(context.workspace, { recursive: true })
    // bundled-copy：知识接线走逐会话 env（DSH_BUNDLED_SKILL_DIR），不写 skill overlay
    const viaEnv = context.env?.DSH_BUNDLED_SKILL_DIR !== undefined
    const skillsOverlay = !viaEnv && context.skillsDirs && context.skillsDirs.length > 0 ? buildSkillsOverlayEntries(context.skillsDirs) : ''
    const modelOverlay = buildModelOverlayEntries(this.profile)
    await writeFile(overlayPath, buildOverlayYaml(sessionRoot) + skillsOverlay + modelOverlay)
    return new DshSession(this.dsh, this.profile.dsh.profile, context, sessionRoot, overlayPath)
  }
}

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

class DshSession implements AgentSession {
  private sessionId: string | null = null
  private logPath: string | null = null
  /** 已归属到之前轮次的日志行数；本轮只解析其后的新增行 */
  private consumedLines = 0
  private readonly openedAt = Date.now()
  private turns = 0

  constructor(
    private readonly dsh: DshCommand,
    private readonly dshProfile: string,
    private readonly context: SessionContext,
    private readonly sessionRoot: string,
    private readonly overlayPath: string,
  ) {}

  async send(message: string): Promise<TurnOutcome> {
    const startedAt = Date.now()
    this.turns++
    if (this.turns > 1 && !this.sessionId) {
      return failed('error', '无法确定上一轮的会话 id，不能续接', startedAt)
    }
    const args = buildDshArgs(this.dshProfile, this.overlayPath, message)
    if (this.sessionId) args.splice(args.length - 1, 0, '--session-id', this.sessionId)

    let proc: { code: number | null; timedOut: boolean; stderrTail: string }
    try {
      const env = this.context.env && Object.keys(this.context.env).length > 0
        ? { ...process.env, ...this.context.env }
        : { ...process.env }
      proc = await runOne(this.dsh.bin, [...this.dsh.prefixArgs, ...args], this.context.workspace, this.context.timeoutMs, env)
    } catch (err) {
      return failed('error', (err as Error).message, startedAt)
    }
    const base = { exitCode: proc.code, stderrTail: proc.stderrTail || undefined }

    let trace: TurnTrace | null = null
    try {
      trace = await this.collectTurn()
    } catch (err) {
      return { ...failed('error', `解析 session 日志失败：${(err as Error).message}`, startedAt), ...base }
    }

    const durationMs = Date.now() - startedAt
    if (proc.timedOut) {
      return { ...base, status: 'timeout', error: `超时（${this.context.timeoutMs}ms）被终止`, durationMs, trace: trace ?? emptyTrace() }
    }
    if (proc.code !== 0) {
      const stderr = proc.stderrTail ?? ''
      const errorCode = /429|rate.?limit/i.test(stderr) ? 'rate-limited'
        : /session[- ]?id/i.test(stderr) ? 'session-invalid'
        : 'unknown'
      return { ...base, status: 'error', errorCode, error: `dsh 退出码 ${String(proc.code)}：${stderr.slice(-200) || '(无 stderr)'}`, durationMs, trace: trace ?? emptyTrace() }
    }
    if (!trace) {
      return { ...base, ...failed('error', `未在 ${this.sessionRoot} 找到 session 日志`, startedAt) }
    }
    return { ...base, status: trace.turnEnd === 'completed' ? 'ok' : 'incomplete', durationMs, trace }
  }

  async close(): Promise<{ sessionLog: string | null; sessionId: string | null }> {
    if (this.logPath) this.logPath = await flattenSessionLog(this.sessionRoot, this.logPath)
    return { sessionLog: this.logPath, sessionId: this.sessionId }
  }

  /** 定位日志，只解析本轮新增的行；第一轮之后确定会话 id */
  private async collectTurn(): Promise<TurnTrace | null> {
    const path = await findSessionFile(this.sessionRoot, this.openedAt).catch(() => null)
    if (!path) return null
    if (path !== this.logPath) {
      // 续接时日志换了文件：无法按行切分，从头解析新文件
      this.logPath = path
      this.consumedLines = 0
    }
    const lines = await readLogLines(path)
    const fresh = lines.slice(this.consumedLines)
    this.consumedLines = lines.length
    if (!this.sessionId) this.sessionId = deriveSessionId(path, lines[0])
    return fresh.length > 0 ? collectFromJsonl(fresh.join('\n')) : null
  }
}

async function readLogLines(path: string): Promise<string[]> {
  const bytes = await readFile(path)
  const isZstd = bytes.length >= 4 && bytes.subarray(0, 4).equals(ZSTD_MAGIC)
  const text = isZstd ? decodeZstdLog(bytes) : bytes.toString('utf8')
  const lines = text.split('\n')
  if (lines.at(-1) === '') lines.pop()
  return lines
}

/**
 * 会话 id：dsh 的父会话目录名即 session-<uuid>；取不到时读 header 行的 id 字段。
 * 首次接入真实 DSH 时需确认这一点（见 README「首次接入真实 DSH 时的验证」）。
 */
export function deriveSessionId(logPath: string, headerLine: string | undefined): string | null {
  const dir = basename(dirname(logPath))
  if (dir.startsWith('session-')) return dir
  try {
    const header = JSON.parse(headerLine ?? '') as Record<string, unknown>
    for (const key of ['sessionId', 'id']) {
      const value = header[key] ?? (header.data as Record<string, unknown> | undefined)?.[key]
      if (typeof value === 'string' && value !== '') return value
    }
  } catch {
    // header 不是 JSON
  }
  return null
}

function emptyTrace(): TurnTrace {
  return { finalText: '', toolCalls: [], toolResults: [], toolErrors: [], steps: 0, tokens: emptyTokenUsage(), models: [] }
}

function failed(status: TurnStatus, error: string, startedAt: number): TurnOutcome {
  return { status, error, durationMs: Date.now() - startedAt, trace: emptyTrace() }
}
