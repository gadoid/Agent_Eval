# 上游来源

本目录文件来自 [BiBoyang/dsh-eval-harness](https://github.com/BiBoyang/dsh-eval-harness)，
commit `e90c255`（2026-10-04），MIT 许可。

| 文件 | 来源 | 处理方式 |
| --- | --- | --- |
| collector.ts | src/collector.ts | 原样复制 |
| zstd.ts | src/zstd.ts | 原样复制 |
| types.ts | src/types.ts | 只保留 collector 用到的类型，定义未改 |
| dsh-kernel.ts | src/runner.ts | 抽取 splitDshBin、resolveDshCommand、buildOverlayYaml、buildDshArgs、killProcessTree、runOne、findSessionFile，函数体未改；补充 export（splitDshBin 后有本地补丁，见下） |

## 同步规则

- 本目录不做业务修改，扩展一律写在 `src/` 下的自研代码中。
- DSH 升级导致日志格式或命令行参数变化时，对比上游新版本的同名文件并整体替换，再跑 `npm test`。
- 替换后更新上方 commit。

## 本地补丁（2026-10-22，v3.2 W2）

- collector.ts extractToolResultText：dsh 0.2.x 的 tool/result message.content 直接为 text 块（无 tool-result 包装）时，回退直接聚合 message 的 text 块。同步上游时保留此补丁；上游若已修复则删除。

## 本地补丁（2026-10-23）

- dsh-kernel.ts splitDshBin：支持双引号包裹的带空格可执行路径（如 `"D:/dsh/DeepSeek Harness.exe" --expose-internals …`，ElineAI 客户端 eline.exe 与本地安装路径都需要）；无引号时行为与上游一致（按空白拆分）。同步上游时保留此补丁；上游若已支持引号路径则删除。
