# 更新日志

版本号格式：`上游版本-hj.N`（基于上游 tencent-lexiang/lexiang-mcp-skill 的修订版）。`build` 为递增整数，用于判断新旧。

## 2.2.3-hj.2（build 2，2026-11-02）

摘要：图片、文件、视频默认下载到本地再展示，不再给远程链接导致破图；无下载权限时给出明确说明。

- **媒体展示**：用户要看/要知识库里的图片、附件、视频时，先 `file_download_file` 拿地址、下载到本地（默认 `~/Downloads/lexiang/`），图片用 `![文件名](本地绝对路径)` 内嵌，文件/视频给本地路径链接和乐享页面链接；禁止把远程签名 URL 当图片给用户（`SKILL.md` 规则 9、`references/files.md`）
- **下载脚本**：新增 `scripts/fetch_media.py`（Python 标准库；同名同大小不重复下载、超过 50MB 先确认、校验大小与图片文件头、输出不含签名地址）及测试 `scripts/test_fetch_media.py`；无 Python 时提供 curl / PowerShell 写法
- **10066 无下载权限**：说明是当前账号对该条目没有下载权限，建议找知识库管理员/部门负责人开通「可查看/下载」，不重试、不绕过、不拿其他内容顶替（`files.md`、`common-errors.md`）
- **file_id 来源**：明确搜索结果 `target_id` 是条目 ID，file_id 取 `entry_describe_entry.target_id` 或页面正文 `<image file-id>`；补充 71 错误（`search.md`、`blocks.md`、`common-errors.md`）
- 路由表、索引补充对应入口（`SKILL.md`、`references/index.md`、`scripts/README.md`）

## 2.2.3-hj.1（build 1）

摘要：修复搜索结果文件链接和草稿发布后页面空白等问题，新增 Skill 自动更新与回滚。

基于上游 2.2.3：

- **搜索**：`kb_file` / `kb_video` 结果的来源链接改用 `/pages/{entry_id}`，修复打不开的问题（`references/search.md`）
- **草稿/写入**：明确草稿接口不会写入页面正文、`revision_id` 的正确来源、发布时必须带内容，修复「已发布但页面空白」；`common-errors.md` 补充该错误（`draft.md`、`writer.md`、`common-errors.md`）
- **安装/配置**：完成后不再主动提示 Token 续期或「内置连接器会忽略手动配置」，只在实际出现 401 或冲突时说明（`SKILL.md`、`setup.md`）
- **自动更新**：新增 `VERSION.json`、`update.json`、`scripts/update.py`（check / apply / rollback / version，另有等价的 `update.mjs`、`update.ps1`，无 Python 时可用）和 `references/update.md`；每个会话首次使用时检查新版本，经用户同意后更新，支持回滚
- **发布工具**：新增 `scripts/release.py`（打包 zip、生成 `latest.json`）和维护者发布说明（`skill-maintenance.md`）
