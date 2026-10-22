/**
 * --cases：直接以 markdown 目录/文件启动——现场调用的 md2case 编译成一次性评测集。
 * 版本号 adhoc-<时间戳>，产物落在 run 输出目录（不动 evalset/dist，不参与版本对比与续跑）；
 * 编译用 --no-linkage（试跑不触发源用例的 rev 联动降级）。
 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdir } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'
import { ConfigError } from './profile.js'

const run = promisify(execFile)

/** 仓库根（src/ 的上一级） */
export const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const MD2CASE = join(REPO, 'evalset', 'md2case.py')

export async function buildEvalsetFromMd(
  cases: readonly string[],
  outDir: string,
  onStdout: (s: string) => void = () => {},
): Promise<{ path: string; version: string }> {
  const version = `adhoc-${new Date().toISOString().replace(/[-:T]/g, '').slice(0, 12)}-${randomBytes(1).toString('hex')}`
  const distDir = join(outDir, 'dist')
  await mkdir(distDir, { recursive: true })
  const out = join(distDir, `evalset-${version}.jsonl`)
  const paths = cases.map((c) => resolve(c))
  // win32 上 python3 常是 Microsoft Store 空壳别名（非 ENOENT 的失败、空 stderr），
  // 先试真解释器 python；其余平台维持 python3 优先。
  const candidates = process.platform === 'win32' ? ['python', 'python3'] : ['python3', 'python']
  for (const py of candidates) {
    try {
      const { stdout } = await run(
        py,
        [MD2CASE, 'build', ...paths, '-o', out, '--version', version,
         '--include', 'structured,reviewed,admitted', '--no-linkage'],
        { maxBuffer: 16 * 1024 * 1024 },
      )
      onStdout(stdout)
      return { path: out, version }
    } catch (e) {
      const err = e as { code?: string; stderr?: string; message: string }
      if (err.code === 'ENOENT') continue            // 该解释器不存在，试下一个
      throw new ConfigError(`md2case 编译失败：\n${err.stderr ?? err.message}`)
    }
  }
  throw new ConfigError('找不到 python3 / python，无法从 md 编译评测集')
}
