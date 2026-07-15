# Codex History Suite

[English](README.md) | [简体中文](README.zh-CN.md)

Codex History Suite 将本地 Codex 会话记录提炼成可移植、证据优先的知识库。两个 Codex Skill 共用同一个核心引擎：

- `build-codex-history`：初始化、发现、规划、完整建库、增量更新、审计、迁移、修复与备份。
- `codex-history`：只读的渐进式与联合检索、上下文组装、claim 检查、证据回溯、比较和历史文件查询。

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

新建 profile 默认使用模型优先的 `auto` 摘要模式。生成的配置推荐百炼非思考模式 `deepseek-v4-flash`；如果找不到 `DASHSCOPE_API_KEY`，系统会说明原因并自动回退到确定性的 `extractive` 规则摘要。回退保证首次使用仍能完成建库，但为了得到更好的跨 turn 归纳、长期资产和带证据引用的 Overview，强烈建议优先配置模型。

插件升级不会改写已有 profile。旧用户如需采用新策略，应把[配置参考](skills/build-codex-history/references/configuration.md)中的 summarization 和 estimation 两节合并到现有 `config.toml`，再重新运行 `plan`。

```bash
export DASHSCOPE_API_KEY='你的-key'  # PowerShell：$env:DASHSCOPE_API_KEY='你的-key'
python3 scripts/codex_history.py plan --mode full --json
```

生成配置中的价格只是可编辑的估算输入。以 2026-07-15 为基准，阿里云百炼中国内地部署的 `deepseek-v4-flash` 公示价为输入 1 元/百万 Token、输出 2 元/百万 Token；实际使用前请复核[百炼最新模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)。直连 DeepSeek API 也可使用其 OpenAI 兼容端点，并根据 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing)把价格换算成人民币后填入配置。

`plan` 和 `update --dry-run` 会统计 transcript 总体积、新增或需要重处理的字节数、摘要输入的期望值与保守上限、缓存输入、输出和 embedding Token、期望成本与保守成本上限，并把预计磁盘体积分解为 snapshot、SQLite、artifact CAS、语义索引和模型响应缓存。内联 data-URI base64 会被扫描并从模型 Token 估算中排除，但仍计入 snapshot 存储估算。即使 `auto` 因缺少 Key 而回退，报告仍会给出“启用模型后”的潜在成本，因此估价本身不需要 API Key。

构建完成后，返回结果还会提供实际 `usage` 汇总，包括模型输入、模型提供商缓存输入、输出、embedding Token、Codex History 响应缓存命中和人民币成本；`storage` 则同时给出 active 核心组件与保留历史 build 后整个 profile 的真实占用。

## 多设备知识库

v0.3 增加了带完整校验的 library bundle 和非破坏式多设备工作流。先给每台电脑设置稳定身份，再导出 active profile；这些 bundle 可以导入任一电脑，也可以统一导入一台中转设备：

```bash
python3 scripts/codex_history.py library device --name '工作笔记本' --json
python3 scripts/codex_history.py --profile default library export ~/work-laptop.zip --json
python3 scripts/codex_history.py library import ~/work-laptop.zip --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search '发布 决策' --deep --json
```

导入 profile 默认按“来源设备名 + 原 profile 名”自动命名，重名时自动追加数字。稳定的 `library_id` 会识别同一个设备库的后续版本：新 bundle 会更新对应 profile，同时把旧代保存在 `backups/imports`。bundle 内每个文件都执行 SHA-256 校验，并拒绝危险压缩路径；不可变 transcript chunk、artifact、语义索引和模型缓存会进入全局内容寻址 blob 仓库，在文件系统允许时通过硬链接实现物理去重。

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

`library sync` 会完成合并、构建并导出一份收敛 bundle；将同一个 bundle 导入两台设备即可完成离线双向收敛。重复导入和重复合并会按 library lineage 与内容摘要保持幂等。历史绝对路径仍作为证据保留，自动生成的文件/根路径映射和可选 `--path-map 'OLD=NEW'` 会在查询显示层提供本机可访问路径，不会篡改原始 provenance。

完整 bundle 格式、冲突规则、离线双向同步和恢复流程见[多设备参考](skills/build-codex-history/references/multi-device.md)。

安装 `.[semantic]` 可启用 ChromaDB。模型摘要和语义检索相互独立：模型优先摘要可继续搭配 SQLite 词法检索，Chroma 可按需单独启用。

如果语义检索依赖安装在独立虚拟环境中，可将 `profiles.<name>.runtime.python` 指向该环境的 Python。启用 embedding 后，CLI 会自动切换到该解释器；当前解释器已经安装 ChromaDB 或只使用词法检索时可留空。

## 状态机

```text
discover -> snapshot -> ingest -> lineage -> summarize -> index -> audit -> promote
```

每个阶段都会在暂存 SQLite 数据库和 `runs/<build-id>/run.json` 中记录检查点。任何阶段失败时，上一份 active build 仍然可用。

付费构建必须在审阅 dry-run 后显式传入 `--max-cost-cny`。Codex History 自身的精确模型响应缓存命中费用为零；模型提供商的输入缓存按用户录入的缓存单价和预期命中率单独估算。API 调用失败时不会悄悄把原本的模型构建降级成规则式结果。

## 增量等价约束

`codex-history audit --equivalence` 会根据同一份当前源数据执行一次干净全量构建，并比较 sources、chunks、events、turns、scopes、Evidence、Knowledge、claims、artifacts 和 semantic documents 的稳定逻辑摘要。增量结果只有在比较通过后才符合发布标准。

新构建只自动生成由证据直接支持的保守事实关系：经过验证的工具输出可以验证同一个 call，完成的 goal 可以验证此前相同的目标。系统不会自动猜测含义模糊的矛盾、失效或重新打开关系。

## 旧版迁移

`migrate --from-db` 会保留并审计已有 v2.1/v2.1.1 SQLite 权威库；`--from-chroma` 可以复制其语义索引。迁移后的数据库可以立即查询，但会被视为只读的旧版基线。第一次增量更新前应先执行一次完整构建。提升是原子的，因此迁移版本仍保留用于回滚和比较。

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
