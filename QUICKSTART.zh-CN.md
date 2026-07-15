# Codex History Suite 中文快速入门

Codex History Suite 会把你自己电脑上的 Codex 历史会话整理成可检索、可追溯、可增量更新的本地知识库。插件只提供建库工具，不包含作者的会话、知识库、图片、文件或 API Key。

## 1. 安装前检查

- 已安装 Codex CLI 或带 Codex 的 ChatGPT 桌面端。
- Python 3.11 或更高版本。
- SQLite 支持 FTS5。安装后运行 `doctor` 会自动检查。
- 首次建库可能占用较多磁盘空间。WSL 用户应把知识库放在 Linux 文件系统，不要放在 `/mnt/c`。

默认是“模型优先、规则式保底”：配置了模型 API Key 时使用证据链接的模型摘要；没有 Key 时会明确提示并回退到完全本地的 `extractive` 规则摘要。规则式适合离线和零成本场景，但跨会话归纳质量有限，推荐优先配置模型。

## 2. 从 GitHub 安装（推荐）

```bash
codex plugin marketplace add Lanqeur/codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

也可以使用完整 Git URL：

```bash
codex plugin marketplace add https://github.com/Lanqeur/codex-history-suite.git
codex plugin add codex-history-suite@codex-history-suite
```

安装后重启 ChatGPT 桌面端，并新建一个 Codex 会话。Skill 在已经打开的旧会话中可能不会刷新。

## 3. 从 ZIP 安装

把 ZIP 解压到一个以后不会移动的目录，在终端执行：

```bash
codex plugin marketplace add /absolute/path/to/codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

Windows PowerShell 示例：

```powershell
codex plugin marketplace add "D:\Tools\codex-history-suite"
codex plugin add codex-history-suite@codex-history-suite
```

## 4. 第一次建库

在新的 Codex 会话中发送：

```text
请使用 $build-codex-history 检查我的环境并初始化本地知识库。
先运行 doctor、discover 和 full plan，只给我看数据范围、磁盘规模和费用预估；
不要开始建库，也不要调用任何付费模型；同时告诉我配置模型后的预计 Token、成本和磁盘体积。
```

初始化会生成一个百炼 `deepseek-v4-flash` 推荐预设。先把 Key 放进环境变量，不要写进仓库：

```bash
export DASHSCOPE_API_KEY='你的-key'
```

Windows PowerShell：

```powershell
$env:DASHSCOPE_API_KEY='你的-key'
```

然后重新运行 full plan。报告会同时显示：

- transcript 总体积以及本次新增或需要重处理的体积；
- 模型输入 Token 的期望值和保守上限、预计缓存输入与输出 Token；
- 期望费用、无缓存保守费用上限和 embedding 费用；
- snapshot、SQLite、artifact CAS、Chroma 和模型缓存的预计体积区间。

确认扫描范围和 `estimated_cost_cny` 后，再发送：

```text
请使用 $build-codex-history 按刚才的模型优先计划执行首次完整建库，
max-cost-cny 设为我已确认的保守费用上限。完成后检查所有状态机阶段和审计结果。
```

若明确只想离线建库，将 `config.toml` 中的 `summarization.mode` 改为 `"extractive"`；也可以保持 `"auto"` 且不配置 Key，此时 plan 会说明回退原因，构建费用为零。`"openai-compatible"` 是严格模型模式，缺少配置会直接失败。

插件会自动寻找常见 Codex 目录。若电脑上有多个 Windows、WSL 或 Codex 用户目录，应在初始化时明确告诉它要使用哪一个。

## 5. 检索历史

示例：

```text
请使用 $codex-history 查找我以前关于支付回调重试的决定、失败记录和验证证据。
先看 overview，需要时再下钻到原始工具输出。
```

```text
请使用 $codex-history 查找最近 30 天仍未完成的任务，并区分 planned、blocked 和 failed。
```

`codex-history` 是只读 Skill，不会修改 transcript 或知识库。

## 6. 增量更新

有了新会话后发送：

```text
请使用 $build-codex-history 对我的知识库做增量更新。
先 dry-run，告诉我新增、追加、改写和删除的会话数量；等我确认后再执行。
```

第一次增量更新后，建议再让它运行一次 `audit --equivalence`，确认多次增量更新与干净全量重建得到相同的知识和证据链。

## 7. 模型摘要与可选语义检索

模型摘要无需 ChromaDB，默认推荐先启用它来提高知识归纳质量。词法检索也无需额外依赖；只有在需要语义召回时才安装 ChromaDB：

```bash
python3 -m pip install ".[semantic]"
```

Windows：

```powershell
py -3 -m pip install ".[semantic]"
```

然后在 `config.toml` 中启用 embedding，并配置你自己的 embedding 端点和 API Key。不要使用别人分享的 Key。

启用 embedding 或模型摘要后，相关文本会发送到你配置的模型提供商。执行前务必查看 `plan` 的 Token、人民币费用上限和磁盘估算。首次构建建议把 `cached_input_ratio` 保持为 `0.0`，避免用乐观缓存命中率低估预算；有真实账单后再按实际情况校准。

以 2026-07-15 为基准，百炼中国内地 `deepseek-v4-flash` 的公示价为输入 1 元/百万 Token、输出 2 元/百万 Token。价格会变化，执行前以[百炼模型价格页](https://help.aliyun.com/zh/model-studio/model-pricing)为准，并把当前输入、缓存输入和输出单价录入 `config.toml`。

## 8. 数据位置和隐私

- Windows：`%LOCALAPPDATA%\codex-history`
- macOS：`~/Library/Application Support/codex-history`
- Linux/WSL：`~/.local/share/codex-history` 或 `$XDG_DATA_HOME/codex-history`

源 transcript 永远按只读方式处理。构建失败不会替换上一份可用知识库；只有审计通过后，CLI 才会原子更新 `active.json`。

不要把生成后的数据库、CAS 或 transcript 和插件 ZIP 一起转发。它们属于个人数据，插件本身不需要这些内容即可在另一台电脑重新建库。

## 9. 多设备迁移与合并

先给每台设备设置名称并导出完整 bundle：

```bash
python3 scripts/codex_history.py library device --name '工作笔记本' --json
python3 scripts/codex_history.py --profile default library export D:\CodexHistory\work-laptop.zip --json
```

在另一台设备导入后，系统会自动命名并做完整 SHA-256 校验：

```bash
python3 scripts/codex_history.py library import D:\CodexHistory\work-laptop.zip --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search '项目 决策' --deep --json
```

联合检索不会重建数据，适合先直接使用。确实需要一个统一知识库时，再运行 `library merge`；第一次不要加 `--build`，先审阅返回的冲突分类、Token、费用和磁盘计划。确认后再用 `--build --max-cost-cny N`。两个来源 profile 始终保持不变。

需要让两台设备得到同一份合并库时，使用 `library sync` 导出收敛 bundle，并把同一个 bundle 分别导入两端。后续重复同步会按稳定 library ID 更新导入版本，旧版保存在 `backups/imports`。完整说明见[多设备参考](skills/build-codex-history/references/multi-device.md)。

## 10. 更新插件

GitHub marketplace 安装可执行：

```bash
codex plugin marketplace upgrade codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

更新后重新打开一个 Codex 会话。
