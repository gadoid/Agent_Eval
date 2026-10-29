import { cp, mkdir, readdir, rename, rmdir, stat } from 'node:fs/promises'
import { basename, dirname, join, relative } from 'node:path'

/** 父会话目录名：session-<uuid>；子会话（delegationDepth>0）是裸 UUID，不镜像 */
const SESSION_DIR = /^session-[0-9a-f-]{36}$/
const CHILD_DIR = /^[0-9a-f-]{36}$/

/**
 * dsh 把会话写在 sessionRoot/<编码工作区路径>/session-<uuid>/ 下：编码串是运行机器的
 * 绝对路径，对评测数据没有语义。会话结束后把会话目录（含子会话裸 UUID 目录）上提到
 * sessionRoot 直下并删除空壳，使新 run 的落盘布局与已整理的产物一致；
 * findSessionFile / mirrorSessions 都是递归查找，不受影响。
 * 返回 logPath 压平后的位置；目标已存在（同名冲突）时保留原位置。
 */
export async function flattenSessionLog(sessionRoot: string, logPath: string): Promise<string> {
  const sessionDir = dirname(logPath)
  const entries = await readdir(sessionRoot, { withFileTypes: true }).catch(() => [])
  for (const entry of entries) {
    if (!entry.isDirectory() || SESSION_DIR.test(entry.name) || CHILD_DIR.test(entry.name)) continue
    const slugDir = join(sessionRoot, entry.name)
    const children = await readdir(slugDir, { withFileTypes: true })
    // 只压平真正装着父会话的容器目录（如 dsh 的编码工作区）；raw-* 之类的旁路目录不动
    if (!children.some((c) => c.isDirectory() && SESSION_DIR.test(c.name))) continue
    for (const child of children) {
      const target = join(sessionRoot, child.name)
      if (await exists(target)) continue
      await rename(join(slugDir, child.name), target)
    }
    await rmdir(slugDir).catch(() => {})
  }
  const moved = join(sessionRoot, basename(sessionDir), relative(sessionDir, logPath))
  return (await exists(moved)) ? moved : logPath
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path)
    return true
  } catch {
    return false
  }
}

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
