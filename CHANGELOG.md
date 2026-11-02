# 更新日志

版本号格式：`上游版本-hj.N`（基于上游 tencent-lexiang/lexiang-mcp-skill 的修订版）。`build` 为递增整数，用于判断新旧。

## 2.2.3-hj.1（build 1）

摘要：修复搜索结果文件链接和草稿发布后页面空白等问题，新增 Skill 自动更新与回滚。

基于上游 2.2.3：

- **搜索**：`kb_file` / `kb_video` 结果的来源链接改用 `/pages/{entry_id}`，修复打不开的问题（`references/search.md`）
- **草稿/写入**：明确草稿接口不会写入页面正文、`revision_id` 的正确来源、发布时必须带内容，修复「已发布但页面空白」；`common-errors.md` 补充该错误（`draft.md`、`writer.md`、`common-errors.md`）
- **安装/配置**：完成后不再主动提示 Token 续期或「内置连接器会忽略手动配置」，只在实际出现 401 或冲突时说明（`SKILL.md`、`setup.md`）
- **自动更新**：新增 `VERSION.json`、`update.json`、`scripts/update.py`（check / apply / rollback / version）和 `references/update.md`；每个会话首次使用时检查新版本，经用户同意后更新，支持回滚
- **发布工具**：新增 `scripts/release.py`（打包 zip、生成 `latest.json`）和维护者发布说明（`skill-maintenance.md`）
