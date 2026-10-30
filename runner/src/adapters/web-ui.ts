import { spawn, type ChildProcess } from 'node:child_process'
import type { Dirent } from 'node:fs'
import { readdir, readFile, stat } from 'node:fs/promises'
import { basename, join } from 'node:path'
import type { Browser, Page } from 'playwright'
import type { AdapterCapabilities, Profile, TurnOutcome, TurnStatus, TurnTrace } from '../domain.js'
import type { AgentAdapter, AgentSession } from '../ports.js'
import { collectFromJsonl } from '../vendor/collector.js'
import { parseSessionHeader } from '../probes/index.js'
import { resolveDshCommand } from '../vendor/dsh-kernel.js'
import type { DshCommand } from '../vendor/dsh-kernel.js'
import { emptyTokenUsage } from '../vendor/types.js'
import { decodeZstdLog } from '../vendor/zstd.js'

/**
 * web-ui 适配器：起一个 dsh web 服务（独立 DSH_HOME，如 ~/.eline-eval），用无头浏览器
 * 驱动真实 UI 发送消息；轮次结束判定与观测采集读 web 服务原生落盘的 session 日志。
 *
 * 会话由 web 服务自己创建并注册——评测端 out/ 归档轨迹，web 端（浏览器 UI）原生可见，
 * 双端同步不靠文件搬运；独立 home 与桌面客户端互不影响。
 */
export const WEB_UI_CAPABILITIES: AdapterCapabilities = { multiTurn: true, prefillHistory: false }

const SERVICE_READY_TIMEOUT_MS = 120_000
const NEW_SESSION_TIMEOUT_MS = 30_000

/** 从服务 stdout 解析「dsh web: http://127.0.0.1:PORT/?token=XXX」 */
export function parseServiceUrl(line: string): string | null {
  const m = line.match(/https?:\/\/[\w.-]+:\d+\/\?token=[A-Za-z0-9_-]+/)
  return m ? m[0] : null
}

export interface WebUiConfig {
  home: string
  port: number
  headless: boolean
  dshBin?: string
  profile: string
  timeoutMs: number
}

/** 由 Profile 构造 web-ui 适配器配置；缺 web 段时抛错 */
export function webUiConfigFromProfile(profile: Profile): WebUiConfig {
  const web = profile.web
  if (!web) throw new Error('Profile 缺少 web 段（home/port），无法使用 web-ui 适配器')
  return {
    home: web.home,
    port: web.port,
    headless: web.headless ?? true,
    dshBin: profile.dsh.bin,
    profile: profile.dsh.profile,
    timeoutMs: profile.dsh.timeout_ms,
  }
}

export class WebUiAdapter implements AgentAdapter {
  readonly name = 'dsh-web-ui'
  readonly version: string | undefined
  readonly capabilities = WEB_UI_CAPABILITIES

  private readonly dsh: DshCommand
  private service: ChildProcess | null = null
  private serviceUrl = ''
  private browser: Browser | null = null
  private started: Promise<void> | null = null
  /** 串行化「首条消息发送 + 识别新 session 目录」临界区，并发 Episode 不互抢 */
  private openMutex = Promise.resolve()

  constructor(private readonly cfg: WebUiConfig) {
    this.dsh = resolveDshCommand(cfg.dshBin)
    this.version = this.dsh.version ? `dsh ${this.dsh.version} (web)` : undefined
  }

  async open(_context: unknown): Promise<AgentSession> {
    await this.ensureStarted()
    const context = await this.browser!.newContext()
    const page = await context.newPage()
    await page.goto(this.serviceUrl, { waitUntil: 'domcontentloaded' })
    // 应用是异步渲染的：等页面稳定后再关弹窗，且关两轮（关掉一个可能又出下一个）
    await page.locator('div[role="textbox"][aria-label*="调用指令"]').waitFor({ timeout: 60_000 })
    await page.waitForTimeout(1_500)
    await dismissDialogs(page)
    await dismissDialogs(page)
    return new WebUiSession(page, this.cfg.home, this.cfg.timeoutMs, (fn) => this.underMutex(fn))
  }

  /** 临界区执行（首条消息的目录发现必须与其他 Episode 串行） */
  private underMutex<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.openMutex.then(fn)
    this.openMutex = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }

  private async ensureStarted(): Promise<void> {
    this.started ??= this.start()
    return this.started
  }

  private async start(): Promise<void> {
    this.service = spawn(
      this.dsh.bin,
      [...this.dsh.prefixArgs, '--profile', this.cfg.profile, '--no-open', '--port', String(this.cfg.port)],
      { cwd: this.cfg.home, env: { ...process.env, DSH_HOME: this.cfg.home }, stdio: ['ignore', 'pipe', 'pipe'] },
    )
    this.serviceUrl = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`web 服务（端口 ${String(this.cfg.port)}）${String(SERVICE_READY_TIMEOUT_MS)}ms 内未就绪`)),
        SERVICE_READY_TIMEOUT_MS,
      )
      let buf = ''
      this.service!.stdout!.setEncoding('utf8').on('data', (chunk: string) => {
        buf += chunk
        const hit = buf.split('\n').map(parseServiceUrl).find(Boolean)
        if (hit) {
          clearTimeout(timer)
          resolve(hit)
        }
      })
      this.service!.on('error', (err) => {
        clearTimeout(timer)
        reject(new Error(`启动 web 服务失败：${err.message}`))
      })
      this.service!.on('close', (code) => {
        clearTimeout(timer)
        reject(new Error(`web 服务提前退出（码 ${String(code)}）`))
      })
    }).catch(async (err: Error) => {
      await this.dispose()
      throw err
    })
    const { chromium } = (await import('playwright')) as typeof import('playwright')
    this.browser = await chromium.launch({ headless: this.cfg.headless })
  }

  async dispose(): Promise<void> {
    await this.browser?.close().catch(() => {})
    this.browser = null
    const svc = this.service
    this.service = null
    if (svc?.pid) {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(svc.pid), '/T', '/F'], { stdio: 'ignore' })
      } else {
        try {
          svc.kill('SIGKILL')
        } catch {
          // 已退出
        }
      }
    }
  }
}

class WebUiSession implements AgentSession {
  private consumedLines = 0
  private logPath: string | null = null
  private sessionDir: string | null = null

  constructor(
    private readonly page: Page,
    private readonly home: string,
    private readonly timeoutMs: number,
    /** 首条消息的「发送 + 发现 session 目录」必须与其他 Episode 串行 */
    private readonly underMutex: <T>(fn: () => Promise<T>) => Promise<T>,
  ) {}

  async send(message: string): Promise<TurnOutcome> {
    const startedAt = Date.now()
    try {
      if (!this.sessionDir) {
        // 首条消息发送后按内容关联会话：应用可能复用加载时已存在的空白会话，
        // 「等新目录」不可靠；哪个会话日志出现本条消息，哪段就是本会话
        this.sessionDir = await this.underMutex(async () => {
          await this.fillAndSend(message)
          return findSessionForMessage(this.home, message, NEW_SESSION_TIMEOUT_MS, Date.now() - 2_000)
        })
      } else {
        await this.fillAndSend(message)
      }
    } catch (err) {
      return failed('error', `${this.sessionDir ? 'UI 操作失败' : '首条消息发送或会话识别失败'}：${(err as Error).message.split('\n')[0]}`, startedAt)
    }
    // 轮询本会话日志直到出现新的 turn/end；日志由 web 服务原生落盘。
    // 期间见到 agent 提问卡片（占据 composer 位，会挡住下一轮输入）立即跳过：空白作答、释放输入位
    const deadline = startedAt + this.timeoutMs
    for (;;) {
      await skipQuestions(this.page)
      const ended = await this.readNewTurn()
      if (ended) {
        const status: TurnStatus = ended.turnEnd === 'completed' ? 'ok' : 'incomplete'
        return { status, durationMs: Date.now() - startedAt, exitCode: null, trace: ended }
      }
      if (Date.now() > deadline) {
        return { status: 'timeout', error: `超时（${String(this.timeoutMs)}ms）未等到 turn/end`, durationMs: Date.now() - startedAt, exitCode: null, trace: emptyTrace() }
      }
      await sleep(1_000)
    }
  }

  private async fillAndSend(message: string): Promise<void> {
    // 点输入框带兜底：中途弹出的对话框会遮挡指针事件，失败先关弹窗再重试
    for (let i = 0; ; i++) {
      try {
        await this.page.locator('div[role="textbox"][aria-label*="调用指令"]').click({ timeout: 5_000 })
        break
      } catch (err) {
        if (i >= 5) throw err
        await dismissDialogs(this.page)
      }
    }
    await this.page.locator('div[role="textbox"][aria-label*="调用指令"]').fill(message)
    await this.page.getByRole('button', { name: '发送消息' }).click()
  }

  async close(): Promise<{ sessionLog: string | null; sessionId: string | null }> {
    // 会话留在 web 服务的 home 里（这正是双端同步的本体），不删除
    return { sessionLog: this.logPath, sessionId: this.sessionDir ? basename(this.sessionDir) : null }
  }

  /** 读日志新增行；本轮出现 turn/end 时返回轨迹，否则 null */
  private async readNewTurn(): Promise<TurnTrace | null> {
    if (!this.sessionDir) return null
    const path = this.logPath ?? (await findLogFile(this.sessionDir))
    if (!path) return null
    if (path !== this.logPath) {
      this.logPath = path
      this.consumedLines = 0
    }
    const lines = await readLogLines(path)
    const fresh = lines.slice(this.consumedLines)
    if (fresh.length === 0) return null
    if (!fresh.some((l) => l.includes('"turn/end"'))) return null
    this.consumedLines = lines.length
    const text = fresh.join('\n')
    const trace: TurnTrace = { ...collectFromJsonl(text) }
    const header = parseSessionHeader(text)             // 会话级观测（doc/18 §2.2）
    if (header) {
      if (header.tools.length > 0) trace.headerTools = header.tools
      if (header.systemPrompt !== '') trace.systemPrompt = header.systemPrompt
    }
    return trace
  }
}

// ---------------------------------------------------------------- 弹窗与输入框

/** agent 的提问卡片占据 composer 位（ask_user_question）：跳过=空白作答并释放输入位 */
async function skipQuestions(page: Page): Promise<void> {
  for (let i = 0; i < 5; i++) {
    const skip = page.getByRole('button', { name: '跳过' }).first()
    if (!(await skip.isVisible().catch(() => false))) return
    await skip.click({ timeout: 2_000 }).catch(() => {})
    await page.waitForTimeout(300)
  }
}

/** 预览说明/API Key 引导/市场等弹窗会遮挡输入框：逐个点掉（短超时），再 Esc 兜底 */
async function dismissDialogs(page: Page): Promise<void> {
  for (const name of ['稍后配置', '继续', '关闭', '知道了', '好的', '确定', '跳过', '开始使用']) {
    const btn = page.getByRole('button', { name }).first()
    if (await btn.isVisible().catch(() => false)) await btn.click({ timeout: 3_000 }).catch(() => {})
  }
  await page.keyboard.press('Escape').catch(() => {})
  await page.waitForTimeout(400)
}

// ---------------------------------------------------------------- 会话目录发现与日志读取

/** 首条消息发送后，轮询所有会话日志，找到包含该消息文本的会话目录（内容关联，免疫空白会话复用）。
 *  只认 sinceMs 之后有落盘更新的会话（排除历史失败会话中的同文本旧记录），多个匹配取最新。 */
export async function findSessionForMessage(home: string, message: string, timeoutMs: number, sinceMs: number): Promise<string> {
  const needle = message.slice(0, 40)
  const deadline = Date.now() + timeoutMs
  for (;;) {
    let best: { dir: string; mtime: number } | null = null
    for (const dir of await listSessionDirs(home)) {
      const log = await findLogFile(dir)
      if (!log) continue
      const mtime = (await stat(log)).mtimeMs
      if (mtime < sinceMs) continue
      try {
        if ((await readLogText(log)).includes(needle) && (!best || mtime > best.mtime)) best = { dir, mtime }
      } catch {
        // 日志暂时不可读（写入中）
      }
    }
    if (best) return best.dir
    if (Date.now() > deadline) throw new Error(`首条消息发送后 ${String(timeoutMs)}ms 内没有会话日志包含该消息`)
    await sleep(1_000)
  }
}

/** home/sessions 下所有父会话目录（session-<uuid>）的绝对路径集合 */
export async function listSessionDirs(home: string): Promise<Set<string>> {
  const root = join(home, 'sessions')
  const found = new Set<string>()
  async function walk(dir: string): Promise<void> {
    let entries: Dirent[]
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const p = join(dir, e.name)
      if (!e.isDirectory()) continue
      if (/^session-[0-9a-f-]{36}$/.test(e.name)) found.add(p)
      else await walk(p)
    }
  }
  await walk(root)
  return found
}

/** 轮询直到出现 before 之外的新父会话目录（「新建会话」的产物） */
export async function waitForNewSessionDir(home: string, before: ReadonlySet<string>, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const now = await listSessionDirs(home)
    const fresh = [...now].filter((p) => !before.has(p))
    if (fresh.length > 0) return fresh.sort().at(-1) as string
    if (Date.now() > deadline) throw new Error(`点击「新建会话」后 ${String(timeoutMs)}ms 内未发现新会话目录`)
    await sleep(500)
  }
}

async function findLogFile(sessionDir: string): Promise<string | null> {
  try {
    for (const f of await readdir(sessionDir)) {
      if (/^session(\.v\d+)?\.jsonl(\.zstd)?$/.test(f)) return join(sessionDir, f)
    }
  } catch {
    // 目录尚不存在
  }
  return null
}

async function readLogText(path: string): Promise<string> {
  const bytes = await readFile(path)
  const isZstd = bytes.length >= 4 && bytes.subarray(0, 4).equals(ZSTD_MAGIC)
  return isZstd ? decodeZstdLog(bytes) : bytes.toString('utf8')
}

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

async function readLogLines(path: string): Promise<string[]> {
  const lines = (await readLogText(path)).split('\n')
  if (lines.at(-1) === '') lines.pop()
  return lines
}

function emptyTrace(): TurnTrace {
  return { finalText: '', toolCalls: [], toolResults: [], toolErrors: [], steps: 0, tokens: emptyTokenUsage(), models: [] as string[] }
}

function failed(status: TurnStatus, error: string, startedAt: number): TurnOutcome {
  return { status, error, durationMs: Date.now() - startedAt, exitCode: null, trace: emptyTrace() }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
