# 乐享文件上传管理

> **基础知识**：数据模型、URL 规则、写入安全规则见 `references/base.md`。
> **前置条件**：本 skill 需要已配置乐享 MCP 连接。如未配置，请先读取 `references/setup.md`。
> **遇到 401 错误**：不要重试，读取 `references/setup.md` 引导用户续期（点击续期按钮即可恢复，无需重新配置）。

---

## ⚠️ 前置检查：确认产品版本

**每次执行文件操作前**，先调用 `whoami()` 检查 `sub_server_type`：
- `v2` → 继续使用本 skill
- `v1` → **停止，提示用户加载 `lexiang-v1-docs`（文件上传）或 `lexiang-v1-assets`（图片上传）**
- `v1v2` → 如果用户明确说了「乐享社区」→ 提示切换对应 v1 skill；否则继续使用本 skill

---

## 工具概览

### 📎 文件管理
- `file_apply_upload` — 申请文件上传凭证（返回 upload_url 和 session_id）
- `file_commit_upload` — 确认上传完成
- `file_describe_file` — 获取文件详情
- `file_download_file` — 获取文件下载地址（返回 1 小时有效的签名 URL）。用于**给用户看/发图片、文件、视频**（见下方「🖼️ 给用户看/发图片、文件、视频」）或需要原始文件时；仅分析内容请用 `entry_describe_ai_parse_content`
- `file_list_revisions` — 获取文件历史版本列表
- `file_revert_file` — 恢复文件到指定历史版本

---

## 📖 读取/分析文件内容（优先直接解析，不要下载）

**分析、引用、总结文件内容时，不需要下载文件，直接获取服务端 AI 解析文本：**

```
MCP Tool: entry_describe_ai_parse_content
Arguments: { "entry_id": "<文件条目的 entry_id>" }
→ 在线文档返回正文 HTML/markdown；文件附件（PDF/PPT/Word/图片）返回
  OCR 解析文本（PPT 逐页带 page-number 标记与图片 OCR 描述），可直接进入上下文分析
```

| 需求 | 正确工具 | 说明 |
|------|---------|------|
| 分析/引用/总结文件**内容** | `entry_describe_ai_parse_content` | 解析文本直接进上下文，无需下载 |
| 需要**原始文件**（本地编辑、格式转换、版式还原、二次处理） | `file_download_file` | 返回下载地址，下载后本地解析 |
| 用户要**看/要**图片、文件、视频（「发张图给我」「把附件给我」「看看这个视频」） | `file_download_file` → 下载到本地 → 本地路径展示 | 见下方「🖼️ 给用户看/发图片、文件、视频」 |

>🚨 仅分析内容却先下载文件再本地解析，会多一次网络传输与解析成本、浪费上下文，**禁止**这样退化。
> 用户要的是**文件本身**（看图、拿附件、看视频）时不属于「仅分析内容」，按下一节下载到本地展示。

---

## 🖼️ 给用户看/发图片、文件、视频（默认下载到本地再展示）

**适用**：用户要看/要知识库里的图片、附件、视频（「把知识库里那张图发给我」「这个 PDF 给我」「放一下培训视频」），或回答里要引用某张图/某个文件给用户看。

> 🚨 **禁止把远程地址当图片/文件给用户**：`file_download_file` 返回的是带签名的临时地址（`file.lexiang-asset.com/...?X-Amz-...`，默认 1 小时过期，响应头为 `Content-Disposition: attachment`）。
> 在 WorkBuddy 等桌面对话应用里写成 `![](远程URL)` 会显示成**破图占位**；地址过期后也打不开，而且签名不应外露。
> **一律先下载到本地，再用本地绝对路径展示。** 不要先给远程链接、等用户抱怨再补救。

### 流程

```
Step 1  拿 file_id
  - 文件/视频条目（entry_type=file/video）：entry_describe_entry(entry_id) → target_id 即 file_id
    ⚠️ search_kb_search 结果里 kb_file/kb_video 的 target_id 是条目 ID，不是 file_id；
       拿它调 file_download_file 会报 71「不存在ID为…的kb_file资源」
  - 在线文档里的图片/附件/视频块：block_fetch_page(entry_id) 正文中的
    <image file-id="…"> / <file file-id="…"> / <video file-id="…"> 的 file-id 即 file_id
  - 只有 origin-url、没有 file-id 的未转存图片：没有乐享文件，直接把 origin-url 交给 Step 4 下载

Step 2  file_describe_file(file_id) → name（含扩展名）、size（字节）、ext、entry_id

Step 3  本地已有就不重复下载、超过 50MB 先问（在本 Skill 目录运行）：
  python3 scripts/fetch_media.py --name "<name>" --file-id <file_id> --size <size>
  → exists：直接用返回的 markdown 展示，结束
  → too_large：告诉用户文件大小，问是否下载；同意后后续命令加 --yes
  → need_url：继续 Step 4

Step 4  file_download_file(file_id) → data.url，再运行
  python3 scripts/fetch_media.py --name "<name>" --file-id <file_id> --size <size> --url "<data.url>"
  → ok：用返回的 markdown / path 展示
  → error（http_403 / unexpected_content_type，多为地址过期）：重新调一次 file_download_file 再试一次；仍失败如实告知

Step 5  展示（见下方「展示格式」）
```

- 脚本只用 Python 标准库，输出一行 JSON：`status`、`kind`（image/video/audio/file）、`path`（本地绝对路径）、`size`、`markdown`；**输出里不含下载地址**。
- 运行时顺序与 `scripts/update.py` 相同：`python3` → `python` → `py -3`；都没有时用下方「无 Python 时」的 curl / PowerShell 命令，规则不变。
- 签名地址无需任何鉴权头（不要附带 MCP Token），直接 GET；`expire_seconds` 可调有效期，一般不用传。
- 下载地址来自 `file_download_file` 时**不要在回复里贴出**；若用户明确要「下载链接」，给乐享页面链接（下方），不要给签名地址。

### 保存位置

| 优先级 | 目录 |
|---|---|
| 1 | `--dir` 参数 |
| 2 | 环境变量 `LEXIANG_MEDIA_DIR` |
| 3 | `~/Downloads/lexiang/`（macOS / Linux / Windows 的「下载」目录，存在且可写时） |
| 4 | 系统临时目录下的 `lexiang/`（如 `/tmp/lexiang/`、`%TEMP%\lexiang\`） |

文件名为 `<原文件名主干>_<file_id 前 8 位><扩展名>`（如 `ceshi的案例图1_efe7caf4.png`），同一目录下**同名且大小一致**视为已下载，不再重复下载。

### 展示格式

- **图片**：直接内嵌，`![文件名](本地绝对路径)`，例如 `![ceshi的案例图1.png](/Users/me/Downloads/lexiang/ceshi的案例图1_efe7caf4.png)`；
  Windows 路径用正斜杠（`C:/Users/me/Downloads/lexiang/x.png`）；路径含空格或括号时用尖括号包裹：`![x](</Users/me/My Files/x.png>)`。脚本返回的 `markdown` 字段已按此生成。
- **文件 / 视频 / 音频**：给本地路径的可点击链接 + 乐享页面链接，例如：
  ```
  📎 [完整版.mp4](/Users/me/Downloads/lexiang/完整版_8ddb1729.mp4)（12.3 MB，已保存到本地）
  乐享原文：https://lexiangla.com/pages/<entry_id>?company_from=<company_from>
  ```
  页面链接用 `/pages/<entry_id>`，按 `references/base.md` 链接生成规则拼接（`entry_id` 取 `file_describe_file` 返回的 `entry_id`；页面内嵌的块取所在页面的 entry_id）。
- 同时注明来源（知识库 / 页面名称），方便用户回到原处。

### 大小与批量

| 情况 | 处理 |
|---|---|
| 单个文件 > 50MB（以 `file_describe_file.size` 为准，视频常见） | **先问用户**是否下载，附大小；不同意就只给乐享页面链接 |
| 多个文件（如「把这页的图都给我」） | 沿用下方「大批量操作策略」：≤ 20 个正常执行，超过则每批 ≤ 20 个并汇报进度 |
| 本地已有同一文件 | 不重复下载，直接展示 |

### 10066 无下载权限

`file_download_file` 返回 `code: 10066`「无下载权限」时：**当前账号对这个条目只有查看权限、没有下载权限**（常见于只给了「仅查看 viewer」角色的知识库；在线文档里的内嵌图片同样受此限制）。

- **不要重试，不要换接口或换账号绕过**，也**不要用本地生成、截图或其他来源的图片/视频顶替**。
- 用平实的话告诉用户，并给乐享页面链接让用户在乐享里直接查看，例如：
  > 《AI知识库基本原理》这个视频你当前的乐享账号没有下载权限（乐享返回 10066 无下载权限），所以我没法把原文件取出来。你可以先点这里在乐享里观看：<页面链接>。如果需要下载，请联系这个知识库的管理员或部门负责人，为你开通「可查看/下载」权限。
- 批量时把 10066 的条目单独列出，其他照常展示。

### 无 Python 时（curl / PowerShell）

先自行判断目标文件是否已存在且大小一致（一致则不下载），`size` > 50MB 先问用户；URL 必须整体加引号（含 `&`）。

```bash
# macOS / Linux / Windows 10+（Windows 下写 curl.exe，避免 PowerShell 的 curl 别名）
mkdir -p ~/Downloads/lexiang
curl -fsSL -o ~/Downloads/lexiang/<文件名主干>_<file_id前8位>.<扩展名> "<data.url>"
```

```powershell
$dir = Join-Path $HOME "Downloads\lexiang"; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$out = Join-Path $dir "<文件名主干>_<file_id前8位>.<扩展名>"
Invoke-WebRequest -Uri "<data.url>" -OutFile $out -UseBasicParsing
(Get-Item $out).Length   # 与 file_describe_file.size 一致才算成功
```

下载后核对文件大小与 `size` 一致；图片可看文件头（PNG 以 `\x89PNG` 开头、JPG 以 `FF D8 FF` 开头），不一致则删除并按上面「error」处理。

---

## 文件上传完整流程（三步）

>⚠️ **必须严格按顺序执行以下三步，缺一不可。**

### Step 1: 申请上传凭证（MCP 调用）

```
MCP Tool: file_apply_upload
Arguments: {
  "parent_entry_id": "<目标目录的 entry_id>",
  "name": "example.pdf",
  "size": 12345,
  "mime_type": "application/pdf",
  "upload_type": "PRE_SIGNED_URL"
}
```

**必填参数说明**：

| 参数 | 说明 | 获取方式 |
|------|------|----------|
| `parent_entry_id` | 目标目录的 entry_id | 从知识库 URL 或 `entry_list_entries` 获取 |
| `name` | 文件名（含扩展名） | 本地文件名 |
| `size` | 文件大小（**字节数，必填**） | 通过 `wc -c<文件>` 或 `stat -f%z <文件>` 获取 |
| `mime_type` | MIME 类型 | 见下方常见类型表 |
| `upload_type` | 固定填`"PRE_SIGNED_URL"` | — |

**返回值**（关键字段）：
```json
{
  "session": {
    "session_id": "abc123",
    "upload_url": "https://cos.example.com/upload?sign=xxx"
  }
}
```

### Step 2: HTTP PUT 上传文件内容（curl命令，非 MCP）

>⚠️ **这一步不是 MCP 调用，必须用 curl 命令执行 HTTP PUT 请求。**

```bash
curl -X PUT \
  -H "Content-Type: <mime_type>" \
  --data-binary "@<本地文件路径>" \
  "<Step 1 返回的 upload_url>"
```

**curl 参数说明**：
- `-X PUT`：必须是 PUT 方法（不是 POST）
- `--data-binary`：必须用 `--data-binary`（不是 `-d` 或 `--data`），保持二进制完整性
- `@文件路径`：`@` 前缀表示读取文件内容，路径必须用绝对路径
- `Content-Type`：必须与 Step 1 中的 `mime_type` 一致

**成功标志**：curl 返回 HTTP 200 或空响应（无报错）

### Step 3: 确认上传完成（MCP 调用）

```
MCP Tool: file_commit_upload
Arguments: {
  "session_id": "<Step 1 返回的 session_id>"
}
```

**返回值**：包含新文件的 `entry_id`，上传完成。按 `references/base.md` 中的**链接生成规则**拼接访问链接返回给用户。

> `{company_from}` 优先从 mcp.json `url` 提取，其次取 `whoami().company.code`。

---

## 常用MIME 类型速查

| 文件类型 | 扩展名 | mime_type |
|----------|--------|-----------|
| PDF | .pdf | `application/pdf` |
| Word | .docx | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |
| Excel | .xlsx | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| PPT | .pptx | `application/vnd.openxmlformats-officedocument.presentationml.presentation` |
| 图片 PNG | .png | `image/png` |
| 图片 JPG | .jpg/.jpeg | `image/jpeg` |
| Markdown | .md | `text/markdown` |
| 文本 | .txt | `text/plain` |
| ZIP | .zip | `application/zip` |
| JSON | .json | `application/json` |

---

## 更新已有文件

更新文件需要额外的 `file_id` 参数，且`parent_entry_id` 填**文件自身的 entry_id**（不是父目录）。

1. 先获取 file_id：`entry_describe_entry(entry_id=<文件的 entry_id>)` → 返回值中 `target_id` 就是 `file_id`
2. 调用 `file_apply_upload` 时额外传入 `file_id` 参数
3. 同样执行 Step 2（curl PUT）和 Step 3（commit_upload）

---

## 文件版本管理

### 查看历史版本

```
MCP Tool: file_list_revisions
Arguments: { "file_id": "<file_id>" }
→ 返回版本列表，每个版本含 revision_id、创建时间等
```

### 恢复到指定版本

```
MCP Tool: file_revert_file
Arguments: {
  "file_id": "<file_id>",
  "revision_id": "<目标版本 revision_id>"
}
```

---

## ⚠️ 常见错误

| 错误 | 原因 | 修复 |
|------|------|------|
| apply_upload 失败 | 缺少 `size` 参数 | **必须传文件字节数** |
| curl PUT 返回 403 | upload_url 过期或格式错误 | 重新执行 Step 1 获取新URL |
| curl PUT 上传 0 字节 | 用了 `-d` 而不是 `--data-binary` | 改用 `--data-binary "@文件"` |
| commit后文件为空 | 跳过了 Step 2 | 必须先curl PUT 上传文件内容 |
| file_download_file 返回 10066「无下载权限」 | 当前账号对该条目没有下载权限（只有查看） | **不重试、不绕过**，告知用户并给页面链接，建议找知识库管理员/部门负责人开通「可查看/下载」，见「10066 无下载权限」 |
| file_download_file 返回 71「不存在ID为…的kb_file资源」 | 传的是条目 ID（如搜索结果的 target_id） | 用 `entry_describe_entry` 返回的 `target_id` 作为 file_id |
| 图片显示为破图占位 | 用远程签名 URL 写了 `![](...)` | 下载到本地后用本地绝对路径展示，见「给用户看/发图片、文件、视频」 |
| 更新文件变成新建| 没传 `file_id` | 更新时必须传 `file_id` |
| 更新时 parent_entry_id 错误 | 填了父目录 ID | 更新时填**文件自身的 entry_id** |

---

## 📦 大批量操作策略

> 适用于文件数量较多的上传、下载、遍历操作，遵循以下规则避免 token 超限和超时失败。

### 阈值规则

| 文件数量 | 处理方式 |
|---------|---------|
| ≤ 20 个 | 正常执行 |
| 21 ~ 50 个 | 分批处理，每批 ≤ 20 个，批次间汇报进度 |
| > 50 个 | 操作前**必须先告知用户**预期耗时和分批策略，确认后再执行 |

### 执行规范

1. **操作前预估**：先统计文件数量，超过 20 个时告知用户将分批处理
2. **分批执行**：每批完成后汇报 `已完成 X/总数`，再继续下一批
3. **遇到超时/限流**：等待 3 秒后重试当前文件；单文件重试 2 次仍失败则跳过并记录
4. **批次结束汇总**：所有批次完成后，汇报成功数、失败数，并列出失败文件名和原因
5. **禁止一次性列举超过 100 个条目**：使用分页参数（`limit` / `offset`）逐页查询

### 示例输出格式

```
正在处理第 1 批（1~20 / 共 163 个）...
✅ 第 1 批完成（20/163）

正在处理第 2 批（21~40 / 共 163 个）...
✅ 第 2 批完成（40/163）

...

📊 全部处理完成：
- 成功：160个
- 失败：3 个（image_42.png: 超时；image_87.png: 403; image_156.png: 超时）
```
