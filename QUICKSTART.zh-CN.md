# Codex History Suite 中文快速入门

Codex History Suite 会把你自己电脑上的 Codex 历史会话整理成可检索、可追溯、可增量更新的本地知识库。插件只提供建库工具，不包含作者的会话、知识库、图片、文件或 API Key。

## 1. 安装前检查

- 已安装 Codex CLI 或带 Codex 的 ChatGPT 桌面端。
- Python 3.11 或更高版本。
- SQLite 支持 FTS5。安装后运行 `doctor` 会自动检查。
- 首次建库可能占用较多磁盘空间。WSL 用户应把知识库放在 Linux 文件系统，不要放在 `/mnt/c`。

默认的 `extractive + lexical` 模式完全在本机运行，不需要 API Key，也不会把会话发送给第三方模型。

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
不要开始建库，也不要调用任何付费模型。
```

确认扫描到的都是你自己的 Codex 目录后，再发送：

```text
请使用 $build-codex-history 按默认 extractive 模式执行首次完整建库，
max-cost-cny 设为 0。完成后检查所有状态机阶段和审计结果。
```

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

## 7. 可选语义检索和模型摘要

词法检索无需额外依赖。只有在确实需要语义召回时才安装 ChromaDB：

```bash
python3 -m pip install ".[semantic]"
```

Windows：

```powershell
py -3 -m pip install ".[semantic]"
```

然后在 `config.toml` 中启用 embedding，并配置你自己的模型端点和 API Key。不要使用别人分享的 Key。

启用 embedding 或模型摘要后，相关文本会发送到你配置的模型提供商。执行前务必查看 `plan` 的 token 和人民币费用上限。

## 8. 数据位置和隐私

- Windows：`%LOCALAPPDATA%\codex-history`
- macOS：`~/Library/Application Support/codex-history`
- Linux/WSL：`~/.local/share/codex-history` 或 `$XDG_DATA_HOME/codex-history`

源 transcript 永远按只读方式处理。构建失败不会替换上一份可用知识库；只有审计通过后，CLI 才会原子更新 `active.json`。

不要把生成后的数据库、CAS 或 transcript 和插件 ZIP 一起转发。它们属于个人数据，插件本身不需要这些内容即可在另一台电脑重新建库。

## 9. 更新插件

GitHub marketplace 安装可执行：

```bash
codex plugin marketplace upgrade codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

更新后重新打开一个 Codex 会话。
