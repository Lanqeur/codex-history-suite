# Codex History Suite

[English](README.md) | [简体中文](README.zh-CN.md)

Codex History Suite 将本地 Codex 会话记录提炼成可移植、证据优先的知识库。两个 Codex Skill 共用同一个核心引擎：

- `build-codex-history`：初始化、发现、规划、完整建库、增量更新、审计、迁移、修复与备份。
- `codex-history`：只读的渐进式与联合检索、上下文组装、claim 检查、证据回溯、原始会话范围导出、比较和历史文件查询。

构建器不会修改源 transcript。它会把会话切成固定大小的内容寻址快照，将内联图片外置到 artifact CAS，保留规范化原始事件，派生 turn 和 Evidence，建立 SQLite FTS 与可选的 Chroma embedding，并在暂存数据库通过审计后才原子更新 `active.json`。

更详细的中文安装和首次使用说明见 [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md)。

## 安装插件

把本仓库添加为 Codex marketplace，然后安装插件：

```bash
codex plugin marketplace add Lanqeur/codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

安装后重启 ChatGPT 桌面端，并新建 Codex 会话，使两个 Skill 正确加载。

## 快速开始

插件是自包含的，无需安装 Python 包也能运行：

```bash
python3 scripts/codex_history.py doctor --json
python3 scripts/codex_history.py init --source ~/.codex --json
python3 scripts/codex_history.py plan --mode full --json
python3 scripts/codex_history.py build --max-cost-cny 30 --json  # 将 30 改为审核后的费用上限
python3 scripts/codex_history.py search '项目 决策' --json
```

Windows 使用 `py -3 scripts\codex_history.py`。需要 Python 3.11 或更高版本，以及支持 FTS5 的 SQLite。

也可以安装 CLI：

```bash
python3 -m pip install .
codex-history doctor
```

## 原始会话证据查看器

知识层负责导航，不替代原始记录。v0.6 可以从规范快照中精确还原指定
会话范围，并打包成一个完全离线、可直接打开的类 Codex HTML 页面：

```bash
# 先按标题或标题片段寻找 thread ID。
python3 scripts/codex_history.py conversation '支付回调' --list

# 导出第 4 至 12 个用户 turn，保留可见消息、工具调用和 Goal。
python3 scripts/codex_history.py conversation THREAD_ID --turn-range 4:12 \
  --include-raw --embed-images -o payment-evidence.html

# 合并一个 scope 下的全部会话，并按事件发生时间裁剪。
python3 scripts/codex_history.py conversation --scope FAMILY_ID \
  --since 2026-06-01 --until 2026-06-30 -o family-evidence.html
```

页面不依赖服务端或网络，支持按会话、正文、角色和时间过滤，长记录渐进渲染，
逐条查看来源位置，勾选证据后拖拽排序，并把选中的事实链另存为 HTML、Markdown
或 JSON。默认隐藏环境/插件注入上下文，图片只保留内容寻址引用；确实需要完整
审计或跨设备查看图片时，再使用 `--include-internal`、`--include-raw` 或
`--embed-images`。整个过程不调用模型，也不需要重建知识库。

新建 profile 默认采用两阶段模型优先方案：非思考模式 `deepseek-v4-flash` 负责把 Token 量最大的新增证据归并为只追加 ledger，非思考模式 `qwen3.7-max` 再更新体量很小的 thread/family Overview。找不到 `DASHSCOPE_API_KEY` 时仍会完整收录确定性证据，但知识库会明确标记为 `pending_model_consolidation`。这只是可检索的应急保底，不是已经完成的高层知识；以后配置模型后，即使 transcript 没有再次变化，执行 `update` 也会补齐 backlog。

插件升级不会改写已有 profile。旧用户如需采用新策略，应把[配置参考](skills/build-codex-history/references/configuration.md)中的 summarization 和 estimation 两节合并到现有 `config.toml`，再重新运行 `plan`。

```bash
export DASHSCOPE_API_KEY='你的-key'  # PowerShell：$env:DASHSCOPE_API_KEY='你的-key'
python3 scripts/codex_history.py plan --mode full --json
```

生成配置中的价格只是可编辑的估算输入：`deepseek-v4-flash` 为输入/输出 1/2 元每百万 Token，Qwen writer 为非缓存输入/缓存输入/输出 6/1.2/18 元，`text-embedding-v4` 为输入 0.5 元。实际使用前请按所选地域和部署复核[百炼最新模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)。

`plan` 和 `update --dry-run` 会统计 transcript 体积、新增字节、待归纳 fact block，并分别估算 reducer/writer 的输入、缓存输入、输出、embedding Token、人民币成本和磁盘体积。内联 data-URI base64 不计入模型 Token，但仍计入 snapshot 存储。没有 Key 也能先得到完整预算。

构建完成后，返回结果还会提供实际 `usage` 汇总，包括模型输入、模型提供商缓存输入、输出、embedding Token、Codex History 响应缓存命中和人民币成本；`storage` 则同时给出 active 核心组件与保留历史 build 后整个 profile 的真实占用。

## 引用文件与 Git 时间点快照

v0.5.2 可以保存 transcript 绝对路径指向的普通文件，并为会话引用过的 Git
仓库建立时间点 checkpoint。先启用经过审核的 artifact 策略，再执行零写入
规划和零模型 artifact-only 构建：

```bash
python3 scripts/codex_history.py --profile default library artifact-plan \
  --since 2026-07-10T00:00:00Z --json
python3 scripts/codex_history.py --profile default library capture-artifacts \
  --since 2026-07-10T00:00:00Z --json
```

规划器会统一 WSL/Windows 路径别名，排除当前 profile、Codex 来源目录、已登记
artifact 根、用户配置的排除根与临时目录，再应用扩展名白名单和单文件上限，
最后按 SHA-256 去重。实际采集会复制 active SQLite，新增带 Event/Evidence
关系和捕获时间的文件观察记录，重建 artifact FTS，验证 CAS 闭合后原子晋升；
摘要模型和 embedding 调用均为零。

普通完整 clone 使用 `git bundle --all`。partial clone 默认生成不联网的当前
HEAD archive，不会暗中下载缺失历史；联网补齐必须显式开启。脏仓库会同时保存
历史 artifact 与确定性的 tracked/untracked worktree 快照。仓库指纹未变化时
直接复用已有 checkpoint。

## 多设备知识库

v0.4 增加了“规范基线 + 严格代际 delta”。每台电脑先设置稳定身份，只传输一次完整基线；以后只移动新增的内容寻址 transcript chunk、artifact 和模型缓存：

```bash
python3 scripts/codex_history.py library device --name '工作笔记本' --json
python3 scripts/codex_history.py --profile default coverage --json
python3 scripts/codex_history.py --profile default library artifact-audit --verify-hashes --json
python3 scripts/codex_history.py --profile default library export ~/work-laptop.zip \
  --artifacts referenced --json
python3 scripts/codex_history.py library import ~/work-laptop.zip --json

# 本机后续产生新会话并完成一次增量建库后：
python3 scripts/codex_history.py --profile default update --max-cost-cny 5 --json
python3 scripts/codex_history.py --profile default library export-delta ~/work-laptop-001.zip \
  --base ~/work-laptop.zip --artifacts referenced --json

# 接收设备只应用 delta，不再搬运完整基线：
python3 scripts/codex_history.py library apply-delta ~/work-laptop-001.zip \
  --max-cost-cny 5 --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search '发布 决策' --deep --json
```

下一份 delta 可以把上一份 delta 作为 `--base`。delta 保留完整的目标来源清单和 artifact 映射，但 ZIP 只携带基代中不存在的 blob。`apply-delta` 会严格检查 `library_id` 与兼容的 source generation，只重建发生变化的规范化 transcript，再走普通的审计式增量状态机；即使 transcript generation 没变，纯 artifact delta 仍会以零模型调用合并映射并校验 CAS 闭合，不会被误判为已应用。重复应用保持幂等，缺代、乱序、跨库或篡改的 delta 都会在晋升前拒绝。完整 SQLite、已有 Chroma、历史 snapshot 与 artifact CAS 因此只需传输一次。

导入 profile 默认按“来源设备名 + 原 profile 名”自动命名，重名时自动追加数字。稳定的 `library_id` 会识别同一个设备库的后续版本：新 bundle 会更新对应 profile，同时把旧代保存在 `backups/imports`。bundle 内每个文件都执行 SHA-256 校验，并拒绝危险压缩路径；不可变 transcript chunk、artifact、语义索引和模型缓存会进入全局内容寻址 blob 仓库，在文件系统允许时通过硬链接实现物理去重。

附件导出策略是显式的。`--artifacts none` 会生成体积较小的仅检索 bundle，SQLite 仍保留附件元数据，但有意不携带文件内容；默认的 `referenced` 会带上 active 数据库索引到的全部附件；`all` 还会带上本地或已登记外部 CAS 中暂未被数据库引用的文件。`referenced` 和 `all` 会在数据库到 CAS 不闭合、文件大小不符或 SHA-256 不符时拒绝导出，bundle manifest 也会记录所选策略和闭合统计。

每个新 bundle 还会记录 `history_coverage`：实际覆盖的最早/最晚会话活动时间、来源扫描与 snapshot 水位、构建完成时间、thread/source/event 数量、逻辑摘要和稳定的知识版本 ID。`latest_activity_at` 表示“库内真实出现的最晚时间”，`source_scan_started_at` 表示“何时观察了本地来源”；任何一个字段都不能单独证明时间区间内绝无漏会话。可随时通过 `coverage --json`、`status --json` 或 `library list --json` 检查同一水位。

联合检索会同时查询多个独立的 SQLite/Chroma 权威库，按知识内容折叠完全重复项，并保留所有命中的 profile 和 Record ID。它不重建知识库，可以导入后立刻使用。合并则是另一件事：系统按稳定 thread ID 重建 transcript，依次采用完全相同、最长前缀或确定性事件并集策略，将结果写入新的生成 profile，两个来源库都不会被修改：

```bash
python3 scripts/codex_history.py library merge \
  --from work-laptop-default --from desktop-default \
  --as personal-history --json
# 先审阅返回的 full plan，再允许模型调用：
python3 scripts/codex_history.py library merge \
  --from work-laptop-default --from desktop-default \
  --as personal-history --build --max-cost-cny 30 --json
```

`library sync` 会完成合并、构建并导出一份收敛基线；将同一个基线导入两台设备即可完成离线双向收敛，同一合并 lineage 的后续版本也能继续使用 delta。重复导入、delta 应用和合并会按 library lineage、source generation 与内容摘要保持幂等。历史绝对路径仍作为证据保留，自动生成的文件/根路径映射和可选 `--path-map 'OLD=NEW'` 会在查询显示层提供本机可访问路径，不会篡改原始 provenance。

完整 bundle 格式、冲突规则、离线双向同步和恢复流程见[多设备参考](skills/build-codex-history/references/multi-device.md)。

安装 `.[semantic]` 可启用 ChromaDB。模型摘要和语义检索相互独立：模型优先摘要可继续搭配 SQLite 词法检索，Chroma 可按需单独启用。

如果语义检索依赖安装在独立虚拟环境中，可将 `profiles.<name>.runtime.python` 指向该环境的 Python。启用 embedding 后，CLI 会自动切换到该解释器；当前解释器已经安装 ChromaDB 或只使用词法检索时可留空。

## 状态机

```text
discover -> snapshot -> ingest -> lineage -> summarize -> index -> audit -> promote
```

每个阶段都会在暂存 SQLite 数据库和 `runs/<build-id>/run.json` 中记录检查点。任何阶段失败时，上一份 active build 仍然可用。

付费构建必须在审阅 dry-run 后显式传入 `--max-cost-cny`。Reducer 输入的每个 Record ID 都必须进入 ledger 事实或明确的无新增事实列表；修复后仍有遗漏、Writer 引用非法、API 失败或预算耗尽都会让候选 build 失败，旧 active 库保持不变。精确模型响应缓存命中费用为零。

## 增量等价约束

`codex-history audit --equivalence` 会根据当前源数据执行一次干净的规则式参考构建，并要求规范来源、events/turns、Evidence occurrence、确定性 core/fact 与 artifacts 完全一致。模型 ledger、Overview、claims 和 semantic documents 会随归纳代际演进，其差异单列为 `derived_layer_differences`，不会再被误判成原始证据不等价。

新构建只自动生成由证据直接支持的保守事实关系：经过验证的工具输出可以验证同一个 call，完成的 goal 可以验证此前相同的目标。系统不会自动猜测含义模糊的矛盾、失效或重新打开关系。

## 旧版迁移

`migrate --from-db` 会保留并审计已有 v2.1/v2.1.1 SQLite 权威库；`--from-chroma` 可以复制其语义索引。使用 `--from-artifacts ARTIFACT_PACK --artifact-mode reference` 可以先完整校验并登记大型外部 CAS，避免重复占用空间；也可以选择 `copy`、`hardlink` 或 `auto` 把文件实体写入 profile。迁移后的数据库可以立即查询，但还不是规范增量基线。执行 `hydrate-baseline` 可在保留已有 Overview、ledger、Evidence、关系和语义索引的同时补齐规范化来源快照；随后可用 `compact-storage` 在验证 trace 偏移回溯后清除重复 raw payload。这两步都不调用模型。提升是原子的，因此迁移版本仍保留用于回滚和比较。

## 跨平台存储

- Windows：`%LOCALAPPDATA%\codex-history`
- macOS：`~/Library/Application Support/codex-history`
- Linux 和 WSL：`$XDG_DATA_HOME/codex-history` 或 `~/.local/share/codex-history`

可使用 `CODEX_HISTORY_HOME` 或 `--home` 覆盖默认位置。WSL 用户应把 active SQLite、Chroma 和缓存保存在 Linux 文件系统中，将挂载的 Windows 磁盘用于导出备份。

## 开发

```bash
PYTHONPATH=src python3 -m pytest
python3 /path/to/skill-creator/scripts/quick_validate.py skills/build-codex-history
python3 /path/to/skill-creator/scripts/quick_validate.py skills/codex-history
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```
