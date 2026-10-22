import { cp, mkdir, readdir } from 'node:fs/promises'
import { join } from 'node:path'

/** 父会话目录名：session-<uuid>；子会话（delegationDepth>0）是裸 UUID，不镜像 */
const SESSION_DIR = /^session-[0-9a-f-]{36}$/

/**
 * 把本段会话落盘目录（runDir/session，含 workspace slug 层级）下的父会话目录
 * 拷贝到目标目录（如 dsh/eline 客户端的会话树），实现双端留存：
 * out/ 下原件不动，客户端重启/刷新后即可在对话列表看到。
 * 已存在的同名会话跳过；返回实际镜像的目录数（没有可镜像的返回 0）。
 */
export async function mirrorSessions(sessionRoot: string, targetRoot: string): Promise<number> {
  let copied = 0
  const walk = async (dir: string): Promise<void> => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      const p = join(dir, entry.name)
      if (SESSION_DIR.test(entry.name)) {
        await cp(p, join(targetRoot, entry.name), { recursive: true, force: false })
        copied++
      } else {
        await walk(p)
      }
    }
  }
  await mkdir(targetRoot, { recursive: true })
  try {
    await walk(sessionRoot)
  } catch {
    // 会话目录不存在（适配器未落盘）时没有可镜像的日志
  }
  return copied
}
