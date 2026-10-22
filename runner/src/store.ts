import { appendFile, readFile, rename, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { EpisodeRecord, EvalRow, Profile, RunManifest, TurnStatus } from './domain.js'
import { RECORD_SCHEMA } from './domain.js'
import { ConfigError } from './profile.js'

const RESULTS = 'results.jsonl'
const MANIFEST = 'run.json'

/**
 * 结果落盘：results.jsonl 每段会话一行，完成即追加，中途中断不丢已完成的会话；
 * 全部结束后按 uid、干预、trial 排序重写。run.json 记录运行清单与配置指纹。
 */
export class ResultStore {
  readonly resultsPath: string
  readonly manifestPath: string
  private chain: Promise<void> = Promise.resolve()

  constructor(outDir: string) {
    this.resultsPath = join(outDir, RESULTS)
    this.manifestPath = join(outDir, MANIFEST)
  }

  async readRecords(): Promise<EpisodeRecord[]> {
    let text: string
    try {
      text = await readFile(this.resultsPath, 'utf8')
    } catch {
      return []
    }
    const records: EpisodeRecord[] = []
    for (const line of text.split('\n')) {
      if (line.trim() === '') continue
      try {
        records.push(JSON.parse(line) as EpisodeRecord)
      } catch {
        // 进程被杀时最后一行可能不完整：丢弃，续跑时重新执行该会话
      }
    }
    return records
  }

  async readManifest(): Promise<RunManifest | null> {
    try {
      return JSON.parse(await readFile(this.manifestPath, 'utf8')) as RunManifest
    } catch {
      return null
    }
  }

  append(record: EpisodeRecord): Promise<void> {
    this.chain = this.chain.then(() => appendFile(this.resultsPath, `${JSON.stringify(record)}\n`))
    return this.chain
  }

  async flush(): Promise<void> {
    await this.chain
  }

  async finalize(): Promise<EpisodeRecord[]> {
    await this.flush()
    const all = await this.readRecords()
    all.sort((a, b) => cmp(a.uid, b.uid) || cmp(a.mode, b.mode) || a.trial - b.trial)
    await writeTextAtomic(this.resultsPath, all.map((r) => `${JSON.stringify(r)}\n`).join(''))
    return all
  }

  async writeManifest(manifest: RunManifest): Promise<void> {
    await writeTextAtomic(this.manifestPath, `${JSON.stringify(manifest, null, 2)}\n`)
  }
}

/** 续跑前确认：schema、评测集版本，以及环境快照五键（01 §5）一致 */
export function checkResumable(
  previous: readonly EpisodeRecord[],
  profile: Profile,
  rows: readonly EvalRow[],
  snapshots?: { previous: Record<string, unknown> | null; current: Record<string, unknown> | null },
): void {
  const old = previous.filter((r) => r.schema !== RECORD_SCHEMA)
  if (old.length > 0) throw new ConfigError(`已有结果的格式版本是 ${String(old[0]?.schema)}，与当前 ${String(RECORD_SCHEMA)} 不同，不能续跑`)
  const versions = new Map(rows.map((r) => [r.uid, r.evalset_version]))
  const changed = previous.find((r) => versions.has(r.uid) && versions.get(r.uid) !== r.evalset_version)
  if (changed) throw new ConfigError(`已有结果的评测集版本与本次不一致（如 ${changed.uid}），不能续跑`)
  const keys = ['evalset', 'runner_profile', 'eline_profile', 'skills_mode', 'skills_base']
  if (snapshots?.previous && snapshots.current) {
    for (const k of keys) {
      const a = JSON.stringify(snapshots.previous[k])
      const b = JSON.stringify(snapshots.current[k])
      if (a !== b) throw new ConfigError(`环境快照「${k}」与已有 run 不一致，不能续跑（跨配置请换输出目录）`)
    }
  }
}

export function countStatuses(records: readonly EpisodeRecord[]): Record<TurnStatus, number> {
  const counts: Record<TurnStatus, number> = { ok: 0, incomplete: 0, timeout: 0, error: 0 }
  for (const r of records) counts[r.status]++
  return counts
}

async function writeTextAtomic(path: string, text: string): Promise<void> {
  const tmp = `${path}.tmp-${process.pid}`
  await writeFile(tmp, text)
  await rename(tmp, path)
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}
