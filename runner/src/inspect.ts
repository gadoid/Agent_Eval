import { readdir, readFile, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { extractLoadedSkills } from './probes/index.js'
import { collectFromJsonl } from './vendor/collector.js'
import { decodeZstdLog } from './vendor/zstd.js'

const SESSION_FILE = /^session(\.v\d+)?\.jsonl(\.zstd)?$/
const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

export interface InspectReport {
  file: string
  header: Record<string, unknown> | null
  frameTypes: Record<string, number>
  /** 内容里出现 skill 字样的事件，用于确认 skill 目录、强制注入在日志中的形式 */
  skillFrames: { line: number; type: string; preview: string }[]
  toolsCalled: string[]
  loadedSkills: ReturnType<typeof extractLoadedSkills>
  turnEnd: string | undefined
  finalTextPreview: string
}

/** 递归找出路径下所有 session 日志；传入文件则只看该文件 */
export async function findSessionFiles(path: string): Promise<string[]> {
  if (!(await stat(path)).isDirectory()) return [path]
  const found: string[] = []
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const p = join(path, entry.name)
    if (entry.isDirectory()) found.push(...(await findSessionFiles(p)))
    else if (SESSION_FILE.test(entry.name)) found.push(p)
  }
  return found.sort()
}

export async function inspectSession(file: string, skillTool = 'skill', previewChars = 300): Promise<InspectReport> {
  const bytes = await readFile(file)
  const isZstd = bytes.length >= 4 && bytes.subarray(0, 4).equals(ZSTD_MAGIC)
  const text = isZstd ? decodeZstdLog(bytes) : bytes.toString('utf8')

  const frameTypes: Record<string, number> = {}
  const skillFrames: InspectReport['skillFrames'] = []
  let header: Record<string, unknown> | null = null
  text.split(/\r?\n/).forEach((line, i) => {
    if (line.trim() === '') return
    let frame: { type?: unknown }
    try {
      frame = JSON.parse(line) as { type?: unknown }
    } catch {
      frameTypes['<非 JSON 行>'] = (frameTypes['<非 JSON 行>'] ?? 0) + 1
      return
    }
    const type = typeof frame.type === 'string' ? frame.type : '<无 type>'
    if (i === 0 && type === '<无 type>') header = frame as Record<string, unknown>
    frameTypes[type] = (frameTypes[type] ?? 0) + 1
    if (/skill/i.test(line)) {
      skillFrames.push({ line: i + 1, type, preview: line.length > previewChars ? `${line.slice(0, previewChars)}…` : line })
    }
  })

  const trace = collectFromJsonl(text)
  return {
    file,
    header,
    frameTypes,
    skillFrames,
    toolsCalled: trace.toolsCalled,
    loadedSkills: extractLoadedSkills(trace.toolCalls, trace.toolResults, skillTool),
    turnEnd: trace.turnEnd,
    finalTextPreview: trace.finalText.slice(0, 200),
  }
}

export function renderInspect(r: InspectReport): string {
  const lines = [`== ${r.file}`]
  if (r.header) lines.push(`header 字段：${Object.keys(r.header).join(', ')}`)
  lines.push('事件类型：')
  for (const [type, n] of Object.entries(r.frameTypes).sort((a, b) => b[1] - a[1])) {
    lines.push(`  ${type.padEnd(32)} ${String(n)}`)
  }
  lines.push(`回合结束：${r.turnEnd ?? '<无>'}`)
  lines.push(`工具调用：${r.toolsCalled.length > 0 ? r.toolsCalled.join(' -> ') : '<无>'}`)
  lines.push(`加载的 skill：${r.loadedSkills.length > 0 ? r.loadedSkills.map((s) => `${s.name}${s.ok ? '' : '（失败）'}`).join(', ') : '<无>'}`)
  lines.push(`含 skill 字样的事件（${String(r.skillFrames.length)} 条）：`)
  for (const f of r.skillFrames) lines.push(`  #${String(f.line)} [${f.type}] ${f.preview}`)
  lines.push(`最终回答（前 200 字）：${r.finalTextPreview || '<空>'}`)
  return lines.join('\n')
}
