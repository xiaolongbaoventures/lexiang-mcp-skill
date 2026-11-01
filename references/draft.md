# 乐享Markdown 草稿

> **前置条件**：本 skill 需要已配置乐享 MCP 连接。如未配置，请先读取 `references/setup.md`。
> **触发场景**：用户需要先保存草稿再发布，或查看/管理某个页面的未发布草稿时使用。

> ⚠️ **边界说明**：draft 系列写入的是条目的 Markdown **文件版本**（`file_list_revisions` 中可见的 `.md` 版本），实测**不会写入 page 的 block 正文**——接口全部返回成功、条目 `status=finished`，但页面打开可能是空白（静默失败）。
> - **写入/重写页面正文**不要走 draft 系列：新建用 `entry_import_content`，整页覆盖已有页面用 `entry_import_content_to_entry`，局部编辑用 `block_update_page`（见 `references/writer.md`、`references/blocks.md`、`references/block-update.md`）
> - 若确需使用 draft 系列，发布后用 `block_fetch_page(render_mode="markdown")` 回读校验页面是否真的有内容

---

## 工具概览

| 工具 | 说明 |
|------|------|
| `draft_save_markdown_draft` | 保存草稿（创建或更新） |
| `draft_describe_markdown_draft` | 获取草稿内容和版本信息 |
| `draft_publish_markdown_draft` | 发布草稿为正式版本 |
| `draft_delete_markdown_draft` | 删除草稿，放弃未发布的修改 |

---

## 标准草稿工作流

### 新建草稿并发布

```
Step 1: 获取当前版本 revision_id
  ⚠️ entry_describe_entry 只返回条目元信息，不含 revision_id
  - 新建页面：取 entry_create_entry 返回值中的 revision.id
  - 已有页面：file_list_revisions(file_id=<entry_describe_entry 返回的 target_id>)
    → 取最新一条版本的 id

Step 2: 保存草稿
  draft_save_markdown_draft(
    entry_id=<entry_id>,
    revision_id=<revision_id>,
    content=<markdown内容>,
    seq=0← 首次创建传0
  )
  → 返回草稿信息，包含 seq

Step 3: 查看草稿（可选确认）
  draft_describe_markdown_draft(entry_id=<entry_id>)
  → 返回草稿内容，空表示无草稿

Step 4: 发布
  draft_publish_markdown_draft(
    entry_id=<entry_id>,
    revision_id=<revision_id>,
    content=<markdown内容>← 必填，不传会报 code 51
  )
```

### 更新已有草稿

```
Step 1: 查看当前草稿
  draft_describe_markdown_draft(entry_id=<entry_id>)
  → 取返回值中的 seq

Step 2: 更新草稿
  draft_save_markdown_draft(
    entry_id=<entry_id>,
    revision_id=<revision_id>,
    content=<新内容>,
    seq=<当前seq>← 乐观锁校验，防止并发冲突
  )
```

### 放弃草稿

```
draft_delete_markdown_draft(entry_id=<entry_id>)
→ 删除未发布草稿，页面内容恢复到上次发布状态
```

---

## ⚠️ 注意事项

1. **`seq` 参数是乐观锁**：首次创建传 `0`，更新时必须传当前草稿的 `seq` 值，防止并发冲突
2. **草稿与正式版本独立**：草稿不影响页面当前的正式内容；发布生成的是文件版本，**不会更新 page 的 block 正文**（见文首边界说明）
3. **`draft_describe_markdown_draft` 返回空**：表示该页面当前没有未发布的草稿，属于正常状态
4. **`draft_publish_markdown_draft` 的 `content` 参数**：实测**必填**（不传报 `code 51 content: value is required`），发布时需传入完整 Markdown 内容
5. **`force_publish=true`**：跳过版本冲突检测，强制发布，谨慎使用
6.参数不确定时用 `get_tool_schema(tool_name="xxx")` 获取定义；若当前只暴露元工具（代理模式），业务工具需经 `call_tool(tool_name=...)` 代理调用（见 `references/base.md`）
