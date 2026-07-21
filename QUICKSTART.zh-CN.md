# Codex History Suite 中文快速入门

Codex History Suite 会把你自己电脑上的 Codex 历史会话整理成可检索、可追溯、可增量更新的本地知识库。插件只提供建库工具，不包含作者的会话、知识库、图片、文件或 API Key。

## 1. 安装前检查

- 已安装 Codex CLI 或带 Codex 的 ChatGPT 桌面端。
- Python 3.11 或更高版本。
- SQLite 支持 FTS5。安装后运行 `doctor` 会自动检查。
- 首次建库可能占用较多磁盘空间。WSL 用户应把知识库放在 Linux 文件系统，不要放在 `/mnt/c`。

默认是“模型优先、规则式应急”：DeepSeek 归并新增证据，Qwen 更新 thread/family 总览。没有 Key 时仍完整保存原始证据与规则式 fact block，但状态会标记 `pending_model_consolidation`，不能当作最终知识产物。以后配置 Key 后可直接运行 `update` 补齐，不需要 transcript 再发生变化。

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

初始化会生成百炼 `deepseek-v4-flash` reducer + `qwen3.7-max` writer 的非思考推荐预设。先把 Key 放进环境变量，不要写进仓库：

```bash
export DASHSCOPE_API_KEY='你的-key'
```

Windows PowerShell：

```powershell
$env:DASHSCOPE_API_KEY='你的-key'
```

然后重新运行 full plan。报告会同时显示：

- transcript 总体积以及本次新增或需要重处理的体积；
- 待归纳 fact block，以及 reducer/writer 各自的输入、缓存输入与输出 Token；
- 期望费用、无缓存保守费用上限和 embedding 费用；
- snapshot、SQLite、artifact CAS、Chroma 和模型缓存的预计体积区间。

确认扫描范围和 `estimated_cost_cny` 后，再发送：

```text
请使用 $build-codex-history 按刚才的模型优先计划执行首次完整建库，
max-cost-cny 设为我已确认的保守费用上限。完成后检查所有状态机阶段和审计结果。
```

若明确只想离线暂存，将 `summarization.mode` 改为 `"extractive"`；也可以保持 `"auto"` 且不配置 Key。两者都会保留证据，但高层知识保持 pending。`"openai-compatible"` 是严格模型模式，缺少 reducer 或 writer 配置会直接失败。

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

## 6. 导出原始会话并人工核查

先找出会话 ID，再导出需要的 turn 或时间范围：

```bash
python3 scripts/codex_history.py conversation '会话标题关键词' --list
python3 scripts/codex_history.py conversation THREAD_ID --turn-range 4:12 \
  --include-raw --embed-attachments -o evidence.html
```

打开 `evidence.html` 后，用户/助手消息中的 Markdown 表格和 Mermaid 图会离线
渲染，并可随时切回原文；工具调用和原始事件始终按字面显示。页面还能继续按正文、
角色和时间筛选，勾选任意消息，拖拽调整顺序，再导出为 HTML、Markdown 或 JSON。这样既能人工复核知识库结论，也能
为某个具体问题整理一份带原始来源的证据链。默认不包含内部环境注入和附件实体，但会
显示消息对应的附件元数据。完整审计可增加 `--include-internal`；轻量图片包可增加
`--embed-images`；需要跨设备查看或下载已经进入 CAS 的 PDF、PPT、Word、ZIP、源码等
文件时使用 `--embed-attachments`。默认每个附件最多 25 MiB、全部附件合计最多 100 MiB，
超限文件会在卡片中明确标注，可按需调整 `--max-attachment-mb` 和 `--max-embedded-mb`。
这项导出不调用模型，也不修改现有知识库。

## 7. 把旧会话还原到原生 Codex 继续聊

在新设备导入便携知识库后，可以让 `$restore-codex-session` 把其中一条完整历史会话
创建为新的原生 Codex fork。先发送：

```text
请使用 $restore-codex-session 按标题找到我以前关于支付回调的会话。
先做 dry-run，确认会话 ID、时间范围、体积、图片和新设备工作目录，不要立即创建。
```

确认后再要求执行还原。CLI 等价命令如下：

```bash
python3 scripts/codex_history.py --profile 导入后的-profile restore THREAD_ID \
  --cwd /新设备/项目目录 --dry-run --json
python3 scripts/codex_history.py --profile 导入后的-profile restore THREAD_ID \
  --cwd /新设备/项目目录 --json
```

成功结果会提供桌面端 `codex://threads/THREAD_ID` 链接和 `codex resume THREAD_ID`
命令。它不会把内容并入当前会话，而是创建一条新 ID 的独立会话，避免 Goal、工具状态和
thread ID 串线。默认对重复图片去重，文本、工具调用、Goal、compact 和时间戳保持在原生
fork 的物化源中；Codex 可能按当前有效分支过滤已回滚或中止事件，差异会进入审计清单，
完整记录仍可回到知识库核查。整个过程不调用模型。Windows、WSL、macOS 和 Linux 都应
在目标 Codex 所属的同一环境运行命令。

## 8. 增量更新

有了新会话后发送：

```text
请使用 $build-codex-history 对我的知识库做增量更新。
先 dry-run，告诉我新增、追加、改写和删除的会话数量；等我确认后再执行。
```

第一次增量更新后，建议运行 `audit --equivalence`：规范来源、解析事件、Evidence、确定性 core/fact 与 artifact 必须等价；模型派生层的代际差异会单独报告。

## 9. 模型摘要与可选语义检索

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

推荐预设记录 `deepseek-v4-flash` 输入/输出 1/2 元、Qwen writer 非缓存输入/缓存输入/输出 6/1.2/18 元、`text-embedding-v4` 输入 0.5 元，单位均为每百万 Token。价格会变化，执行前以[百炼模型价格页](https://help.aliyun.com/zh/model-studio/model-pricing)为准。

## 10. 数据位置和隐私

- Windows：`%LOCALAPPDATA%\codex-history`
- macOS：`~/Library/Application Support/codex-history`
- Linux/WSL：`~/.local/share/codex-history` 或 `$XDG_DATA_HOME/codex-history`

源 transcript 永远按只读方式处理。构建失败不会替换上一份可用知识库；只有审计通过后，CLI 才会原子更新 `active.json`。

不要把生成后的数据库、CAS 或 transcript 和插件 ZIP 一起转发。它们属于个人数据，插件本身不需要这些内容即可在另一台电脑重新建库。

## 11. 多设备迁移与合并

先给每台设备设置名称、检查附件闭合，再创建只需传输一次的规范基线：

```bash
python3 scripts/codex_history.py library device --name '工作笔记本' --json
python3 scripts/codex_history.py --profile default coverage --json
python3 scripts/codex_history.py --profile default library artifact-audit --verify-hashes --json
python3 scripts/codex_history.py --profile default library export D:\CodexHistory\work-laptop.zip --artifacts referenced --json
```

`--artifacts none` 适合只需要文本检索的轻量迁移；默认 `referenced` 会携带数据库引用的全部历史文件；`all` 还会归档 CAS 中未被当前数据库引用的对象。只有后两种属于附件闭合的便携包，并会在缺失或哈希不符时拒绝导出。

导出结果中的 `history_coverage.latest_activity_at` 是实际知识内容截止水位，`source_scan_started_at` 是扫描本地来源的时点，`knowledge_version_id` 与 `logical_digest` 用来识别具体版本。迁移后先检查这些字段，不要仅根据 ZIP 文件创建时间判断内容新旧。

在另一台设备导入后，系统会自动命名并做完整 SHA-256 校验：

```bash
python3 scripts/codex_history.py library import D:\CodexHistory\work-laptop.zip --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search '项目 决策' --deep --json
```

以后有新会话时，不要再导出和搬运完整基线。源设备先正常执行 `update`，再以上一份基线或 delta 为基代导出下一代：

```bash
python3 scripts/codex_history.py --profile default update --dry-run --json
python3 scripts/codex_history.py --profile default update --max-cost-cny 5 --json
python3 scripts/codex_history.py --profile default library export-delta D:\CodexHistory\work-laptop-001.zip \
  --base D:\CodexHistory\work-laptop.zip --artifacts referenced --json
```

接收设备只应用这份小 delta：

```bash
python3 scripts/codex_history.py library apply-delta D:\CodexHistory\work-laptop-001.zip \
  --max-cost-cny 5 --json
```

delta 严格绑定稳定 `library_id` 和上一代 `source_generation_id`。缺代、乱序、误用其他设备库的 delta 或文件哈希不符都会被拒绝；重复应用同一份 delta 是安全的。默认携带新增模型缓存，使接收端尽量复用源端已经完成的模型结果；显式使用 `--without-model-cache` 时，接收端可能需要重新调用模型。

联合检索不会重建数据，适合先直接使用。确实需要一个统一知识库时，再运行 `library merge`；第一次不要加 `--build`，先审阅返回的冲突分类、Token、费用和磁盘计划。确认后再用 `--build --max-cost-cny N`。两个来源 profile 始终保持不变。

需要让两台设备得到同一份合并库时，使用 `library sync` 导出收敛基线，并把同一个基线分别导入两端。后续同一 lineage 可以改用 delta 传输；旧版仍保存在 `backups/imports`。完整说明见[多设备参考](skills/build-codex-history/references/multi-device.md)。

## 12. 更新插件

GitHub marketplace 安装可执行：

```bash
codex plugin marketplace upgrade codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

更新后重新打开一个 Codex 会话。
