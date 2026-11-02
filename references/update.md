# Skill 自动更新

> **触发场景**：每个会话首次使用乐享时的版本检查（8 小时内最多一次）；用户说「检查乐享 skill 更新」「乐享 skill 自检」「更新乐享 skill」「升级乐享 skill」「回滚乐享 skill」「乐享 skill 是什么版本」，或用其他说法要求检查、更新、自检本 Skill。

脚本：`scripts/update.py`（仅依赖 Python 3 标准库）。路径相对于本 Skill 目录（即 `SKILL.md` 所在目录），常见位置：

| 客户端 | Skill 目录 |
|--------|-----------|
| WorkBuddy | `~/.workbuddy/skills/lexiang-knowledge-base/` |
| Codex | `~/.codex/skills/lexiang-knowledge-base/` |

Python 命令依次尝试：`python3` → `python` → `py -3`（Windows）。都不可用时**放弃更新相关操作，继续用户任务**，不要求用户安装 Python。

---

## 命令

| 命令 | 作用 | 输出 |
|------|------|------|
| `python3 scripts/update.py check` | 检查新版本（8 小时内只查一次） | 一行 JSON |
| `python3 scripts/update.py check --force` | 忽略间隔，立即检查 | 一行 JSON |
| `python3 scripts/update.py apply` | 下载、校验、安装新版本 | 一行 JSON，失败时退出码非 0 |
| `python3 scripts/update.py rollback` | 回滚到上一版本 | 一行 JSON |
| `python3 scripts/update.py version` | 查看本地版本 | 一行 JSON |

`check` 的 `status`：

| status | 含义 | AI 处理 |
|--------|------|---------|
| `update_available` | 有新版本，`latest` / `notes` 为新版本号和说明 | 按下方「会话首次检查」处理 |
| `up_to_date` | 已是最新 | 不提及 |
| `skipped` | 未配置更新地址或近期已检查 | 不提及 |
| `error` | 网络或解析失败 | 不提及，继续任务 |

---

## 会话首次检查（自动）

1. 首次使用乐享时运行 `check`，不论结果都**继续完成用户任务**
2. 仅当 `status` 为 `update_available`：任务完成后在回复**末尾加一行**，例如：
   > 乐享 Skill 有新版本 2.2.3-hj.2（修复了 XX 问题）。需要现在更新吗？
3. 同一会话只提示一次；用户未回应或拒绝则不再提
4. **只有用户明确同意（如「更新」「好」）后**才运行 `apply`

## 用户主动要求自检或更新

用户说「检查乐享 skill 更新」「乐享 skill 自检」「更新乐享 skill」「升级乐享 skill」等，或用其他说法要求检查、更新本 Skill 时：

1. 运行 `check --force`（不受检查间隔限制）
2. 用一两句话回复当前版本（`current`）和检查结果：`update_available` → 告知新版本号和 `notes`，询问是否更新；`up_to_date` → 告知已是最新版本；`error` → 告知暂时无法连接更新服务，稍后再试；`skipped`（`not_configured`）→ 告知当前安装未配置在线更新
3. 用户确认后运行 `apply`

## apply 结果

- `updated`：告诉用户「已更新到 X，**请新开一个对话**后生效」
- `up_to_date`：告知已是最新版本
- `failed`：原样转述 `message`（中文，已自动恢复原版本），不要自行重试或手动改文件

## 回滚

用户说「回滚乐享 skill」时运行 `rollback`：成功后提示新开对话生效；`failed` 时转述 `message`。上一版本保存在同级目录 `lexiang-knowledge-base.bak`（其中的 `SKILL.md` 已改名，避免被当作第二个 Skill 加载）。

---

## 安全约束

- **禁止静默自动更新**，`apply` / `rollback` 必须由用户明确同意或要求
- 脚本只改动 Skill 目录及其 `.bak` 备份目录，**不触碰 MCP 客户端配置（mcp.json 等）和 Token**
- 安装包会校验名称、版本号、大小、sha256 和目录结构，任一不符即中止并保持原版本
- 不要手动下载、解压或覆盖 Skill 文件来代替脚本
