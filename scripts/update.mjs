#!/usr/bin/env node
// 乐享知识库 Skill 自动更新 —— Node.js 版（Node ≥ 18，零依赖）。
// 与 scripts/update.py 行为、命令、输出 JSON 完全一致，修改时须三者（py / mjs / ps1）同步。
//
//   node scripts/update.mjs check [--force] | apply | rollback | version
//
// 只会改动本 Skill 目录及其同级备份目录 `<目录名>.bak`，不触碰 MCP 客户端配置或任何密钥。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const SKILL_NAME = "lexiang-knowledge-base";
const DEFAULT_SKILL_DIR = path.dirname(path.dirname(fs.realpathSync(fileURLToPath(import.meta.url))));
const CHECK_FILE = ".last_update_check";
const PLACEHOLDER = "__MANIFEST_URL__";
const MANIFEST_TIMEOUT = 5000;
const DOWNLOAD_TIMEOUT = 30000;
const MAX_ZIP_SIZE = 50 * 1024 * 1024;
const DISABLED_SKILL_MD = "SKILL.md.bak-disabled";
// 仅供测试注入故障：rename_blocked（模拟 Windows 目录被占用）/ move / post_install
const FAULT = process.env.LEXIANG_UPDATE_FAULT || "";

class UpdateError extends Error {}
// Node 18 首次使用 fetch 会向 stderr 打印 ExperimentalWarning，这里静默
process.removeAllListeners("warning");

// ---------------------------------------------------------------- 基础工具

const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
const exists = (p) => fs.existsSync(p);
const stripBom = (s) => (s.charCodeAt(0) === 0xfeff ? s.slice(1) : s);
const readJson = (p) => JSON.parse(stripBom(fs.readFileSync(p, "utf8")));

function toInt(v) {
  // 等价于 Python int()：整数/浮点截断/纯整数字符串
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string" && /^\s*[+-]?\d+\s*$/.test(v)) return parseInt(v, 10);
  throw new TypeError("not an int");
}

function localVersion(skillDir) {
  const d = readJson(path.join(skillDir, "VERSION.json"));
  if (d === null || typeof d !== "object" || !("name" in d) || !("version" in d) || !("build" in d)) {
    throw new Error("bad VERSION.json");
  }
  return { name: d.name, version: String(d.version), build: toInt(d.build) };
}

function loadConfig(skillDir) {
  let cfg = {};
  try {
    cfg = readJson(path.join(skillDir, "update.json"));
    if (cfg === null || typeof cfg !== "object" || Array.isArray(cfg)) cfg = {};
  } catch {
    cfg = {};
  }
  const url = String(cfg.manifest_url || "").trim();
  let hours = cfg.check_interval_hours === undefined ? 8 : Number(cfg.check_interval_hours);
  if (cfg.check_interval_hours === null || Number.isNaN(hours)) hours = 8;
  return { url, hours };
}

function urlAllowed(url) {
  // https 任意主机；http 仅限本机（测试）；file:// 用于本地测试
  let u;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol === "https:") return Boolean(u.host);
  if (u.protocol === "http:") return ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname);
  return u.protocol === "file:";
}

const isConfigured = (url) =>
  Boolean(url) && !url.includes(PLACEHOLDER) && !url.startsWith("__") && urlAllowed(url);

async function fetchBytes(url, timeout) {
  if (!urlAllowed(url)) throw new UpdateError(`不支持的下载地址：${url}`);
  if (new URL(url).protocol === "file:") return fs.readFileSync(fileURLToPath(url));
  const res = await fetch(url, {
    headers: { "User-Agent": `${SKILL_NAME}-updater`, "Cache-Control": "no-cache" },
    signal: AbortSignal.timeout(timeout),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return Buffer.from(await res.arrayBuffer());
}

async function fetchManifest(url) {
  const raw = (await fetchBytes(url, MANIFEST_TIMEOUT)).subarray(0, 256 * 1024);
  const m = JSON.parse(stripBom(new TextDecoder("utf-8", { fatal: true }).decode(raw)));
  if (m === null || typeof m !== "object" || Array.isArray(m)) throw new Error("manifest is not an object");
  for (const k of ["name", "version", "build", "zip_url", "sha256"]) {
    if (!(k in m)) throw new Error(`manifest missing ${k}`);
  }
  m.build = toInt(m.build);
  return m;
}

function readLastCheck(skillDir) {
  try {
    const s = stripBom(fs.readFileSync(path.join(skillDir, CHECK_FILE), "utf8")).trim();
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s)) return null;
    return Number(s);
  } catch {
    return null;
  }
}

function writeLastCheck(skillDir) {
  try {
    fs.writeFileSync(path.join(skillDir, CHECK_FILE), String(Math.floor(Date.now() / 1000)), "utf8");
  } catch {}
}

// ---------------------------------------------------------------- check

async function cmdCheck(skillDir, force) {
  let cur;
  try {
    cur = localVersion(skillDir);
  } catch {
    out({ status: "error", reason: "local_version_unreadable" });
    return 0;
  }
  const base = { current: cur.version, current_build: cur.build };
  const { url, hours } = loadConfig(skillDir);
  if (!isConfigured(url)) {
    out({ status: "skipped", reason: "not_configured", ...base });
    return 0;
  }
  if (!force) {
    const last = readLastCheck(skillDir);
    const now = Date.now() / 1000;
    if (last !== null && now - last >= 0 && now - last < hours * 3600) {
      out({ status: "skipped", reason: "checked_recently", ...base });
      return 0;
    }
  }
  let m;
  try {
    m = await fetchManifest(url);
  } catch {
    writeLastCheck(skillDir);
    out({ status: "error", reason: "manifest_unavailable", ...base });
    return 0;
  }
  writeLastCheck(skillDir);
  if (m.name !== cur.name) {
    out({ status: "error", reason: "name_mismatch", ...base });
    return 0;
  }
  const res = { ...base, latest: String(m.version), latest_build: m.build, notes: m.notes === undefined ? "" : m.notes };
  res.status = m.build > cur.build ? "update_available" : "up_to_date";
  out(res);
  return 0;
}

// ---------------------------------------------------------------- zip（最小实现：中央目录 + stored/deflate）

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function readZip(buf) {
  const bad = () => new UpdateError("安装包不是有效的 zip 文件");
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 65535); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw bad();
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const entries = [];
  for (let i = 0; i < count; i++) {
    if (p + 46 > buf.length || buf.readUInt32LE(p) !== 0x02014b50) throw bad();
    const flags = buf.readUInt16LE(p + 8);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const nameBuf = buf.subarray(p + 46, p + 46 + nameLen);
    entries.push({
      name: flags & 0x800 ? nameBuf.toString("utf8") : nameBuf.toString("latin1"),
      flags,
      method: buf.readUInt16LE(p + 10),
      crc: buf.readUInt32LE(p + 16),
      compSize: buf.readUInt32LE(p + 20),
      size: buf.readUInt32LE(p + 24),
      externalAttr: buf.readUInt32LE(p + 38),
      offset: buf.readUInt32LE(p + 42),
    });
    p += 46 + nameLen + extraLen + commentLen;
  }
  const read = (e) => {
    const o = e.offset;
    if (o + 30 > buf.length || buf.readUInt32LE(o) !== 0x04034b50 || e.flags & 0x1) throw bad();
    const start = o + 30 + buf.readUInt16LE(o + 26) + buf.readUInt16LE(o + 28);
    const comp = buf.subarray(start, start + e.compSize);
    let data;
    try {
      if (e.method === 0) data = Buffer.from(comp);
      else if (e.method === 8) data = zlib.inflateRawSync(comp);
      else throw bad();
    } catch {
      throw bad();
    }
    if (data.length !== e.size || crc32(data) !== e.crc) throw bad();
    return data;
  };
  return { entries, read };
}

function safeExtract(zipPath, destRoot, expectedBuild) {
  const prefix = SKILL_NAME + "/";
  fs.mkdirSync(destRoot, { recursive: true });
  destRoot = fs.realpathSync(destRoot);
  const { entries, read } = readZip(fs.readFileSync(zipPath));
  if (!entries.length) throw new UpdateError("安装包为空");
  for (const e of entries) {
    const n = e.name;
    const parts = n.split("/").filter((x) => x !== "");
    if (!n.startsWith(prefix) || n.includes("\\") || n.startsWith("/") || parts.includes("..") || parts.some((x) => x.includes(":"))) {
      throw new UpdateError(`安装包结构不正确或包含非法路径：${n}`);
    }
    if (((e.externalAttr >>> 16) & 0o170000) === 0o120000) {
      throw new UpdateError(`安装包包含符号链接，已拒绝：${n}`);
    }
  }
  const names = entries.map((e) => e.name);
  for (const req of ["SKILL.md", "VERSION.json"]) {
    if (!names.includes(prefix + req)) throw new UpdateError(`安装包缺少 ${req}`);
  }
  try {
    const ve = entries.filter((e) => e.name === prefix + "VERSION.json").pop();
    const inner = JSON.parse(stripBom(read(ve).toString("utf8")));
    if (inner === null || typeof inner !== "object" || inner.name !== SKILL_NAME || toInt(inner.build) !== expectedBuild) {
      throw new Error();
    }
  } catch (err) {
    if (err instanceof UpdateError && err.message.includes("zip")) throw err;
    throw new UpdateError("安装包内的 VERSION.json 与发布信息不一致");
  }
  for (const e of entries) {
    const target = path.resolve(destRoot, e.name);
    if (!target.startsWith(destRoot + path.sep)) throw new UpdateError(`安装包包含非法路径：${e.name}`);
    if (e.name.endsWith("/")) {
      fs.mkdirSync(target, { recursive: true });
      continue;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, read(e));
  }
  return path.join(destRoot, SKILL_NAME);
}

// ---------------------------------------------------------------- 目录操作

function copyTree(src, dst) {
  fs.mkdirSync(dst);
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isSymbolicLink()) fs.symlinkSync(fs.readlinkSync(s), d);
    else if (ent.isDirectory()) copyTree(s, d);
    else fs.copyFileSync(s, d);
  }
}

const rmTree = (p) => fs.rmSync(p, { recursive: true, maxRetries: 3 });

function clearDir(d) {
  for (const ent of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, ent.name);
    if (ent.isDirectory() && !ent.isSymbolicLink()) rmTree(p);
    else fs.unlinkSync(p);
  }
}

function replaceContents(dst, src) {
  // 保留 dst 目录本身，只替换其内容（Windows 上目录被占用、无法改名时使用）
  clearDir(dst);
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isSymbolicLink()) fs.symlinkSync(fs.readlinkSync(s), d);
    else if (ent.isDirectory()) copyTree(s, d);
    else fs.copyFileSync(s, d);
  }
}

function moveDir(src, dst) {
  // 等价于 shutil.move：同盘改名，跨盘复制后删除
  try {
    fs.renameSync(src, dst);
  } catch (err) {
    if (err.code !== "EXDEV") throw err;
    copyTree(src, dst);
    rmTree(src);
  }
}

function disableBackup(bak) {
  const p = path.join(bak, "SKILL.md");
  if (exists(p)) fs.renameSync(p, path.join(bak, DISABLED_SKILL_MD));
}

function enableBackup(bak) {
  const p = path.join(bak, DISABLED_SKILL_MD);
  if (exists(p)) fs.renameSync(p, path.join(bak, "SKILL.md"));
}

function renameDir(src, dst, skillDir) {
  if (FAULT === "rename_blocked" && skillDir && path.resolve(src) === path.resolve(skillDir)) {
    const e = new Error("simulated: directory in use");
    e.code = "EPERM";
    throw e;
  }
  fs.renameSync(src, dst);
}

function install(skillDir, newDir, bak) {
  // skill_dir → bak，new_dir → skill_dir。优先整体改名，失败（如 Windows 占用）则改为内容复制
  if (exists(bak)) rmTree(bak);
  try {
    renameDir(skillDir, bak, skillDir);
  } catch {
    copyTree(skillDir, bak);
    try {
      replaceContents(skillDir, newDir);
    } catch (err) {
      replaceContents(skillDir, bak);
      throw err;
    }
    return;
  }
  try {
    if (FAULT === "move") {
      fs.mkdirSync(skillDir);
      fs.writeFileSync(path.join(skillDir, "partial"), "x");
      throw new Error("simulated: move interrupted");
    }
    moveDir(newDir, skillDir);
  } catch (err) {
    try {
      fs.rmSync(skillDir, { recursive: true, force: true });
    } catch {}
    fs.renameSync(bak, skillDir);
    throw err;
  }
}

function restore(skillDir, bak) {
  // 把 bak 恢复为 skill_dir（尽力而为）
  enableBackup(bak);
  try {
    if (exists(skillDir)) rmTree(skillDir);
    fs.renameSync(bak, skillDir);
  } catch {
    fs.mkdirSync(skillDir, { recursive: true });
    replaceContents(skillDir, bak);
  }
}

function postInstall(skillDir, bak, expectedBuild) {
  if (FAULT === "post_install") throw new Error("simulated: disk full");
  const oldCheck = path.join(bak, CHECK_FILE);
  if (exists(oldCheck)) fs.copyFileSync(oldCheck, path.join(skillDir, CHECK_FILE));
  if (localVersion(skillDir).build !== expectedBuild) throw new UpdateError("安装后版本校验失败");
  disableBackup(bak);
}

function guardSkillDir(skillDir) {
  if (exists(path.join(skillDir, ".git"))) {
    throw new UpdateError("当前目录是源码仓库（含 .git），不是 Skill 安装目录，已拒绝更新");
  }
  if (!exists(path.join(skillDir, "SKILL.md"))) throw new UpdateError("未找到 SKILL.md，无法确认 Skill 安装目录");
}

const errName = (e) => (e && e.code ? e.code : e && e.constructor ? e.constructor.name : "Error");

// ---------------------------------------------------------------- apply / rollback

async function cmdApply(skillDir) {
  skillDir = path.resolve(skillDir);
  const bak = skillDir + ".bak";
  process.chdir(os.tmpdir()); // 避免自身工作目录占用 Skill 目录（Windows）
  let installed = false;
  let tmp = null;
  try {
    guardSkillDir(skillDir);
    let cur;
    try {
      cur = localVersion(skillDir);
    } catch {
      throw new UpdateError("无法读取本地 VERSION.json");
    }
    const { url } = loadConfig(skillDir);
    if (!isConfigured(url)) throw new UpdateError("未配置更新地址（update.json），无法在线更新");
    let m;
    try {
      m = await fetchManifest(url);
    } catch {
      throw new UpdateError("无法获取最新版本信息，请检查网络后重试");
    }
    if (m.name !== cur.name || m.name !== SKILL_NAME) throw new UpdateError("发布信息中的 Skill 名称不匹配，已中止");
    if (m.build <= cur.build) {
      out({ status: "up_to_date", current: cur.version, message: `当前已是最新版本 ${cur.version}，无需更新` });
      return 0;
    }
    let size = 0;
    try {
      size = m.size ? toInt(m.size) : 0;
    } catch {
      throw new Error("bad size");
    }
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "lexiang-update-"));
    const zipPath = path.join(tmp, "package.zip");
    let data;
    try {
      data = await fetchBytes(String(m.zip_url), DOWNLOAD_TIMEOUT);
    } catch (err) {
      if (err instanceof UpdateError) throw err;
      throw new UpdateError("下载安装包失败，请检查网络后重试");
    }
    const limit = size || MAX_ZIP_SIZE;
    if (data.length > limit) throw new UpdateError("下载的安装包大小超出预期，已中止");
    if (size && data.length !== size) {
      throw new UpdateError(`安装包大小不符（应为 ${size} 字节，实际 ${data.length} 字节），已中止`);
    }
    fs.writeFileSync(zipPath, data);
    const digest = crypto.createHash("sha256").update(data).digest("hex");
    if (digest !== String(m.sha256).trim().toLowerCase()) throw new UpdateError("安装包校验失败（sha256 不一致），已中止");
    const newDir = safeExtract(zipPath, path.join(tmp, "extract"), m.build);
    install(skillDir, newDir, bak);
    installed = true;
    postInstall(skillDir, bak, m.build);
    writeLastCheck(skillDir);
    out({ status: "updated", from: cur.version, to: String(m.version), message: `已更新到 ${m.version}，请新开一个对话后生效` });
    return 0;
  } catch (e) {
    let restored = "";
    if (installed) {
      try {
        restore(skillDir, bak);
        restored = "，已恢复原版本";
      } catch {
        restored = "，自动恢复失败，请联系管理员";
      }
    }
    const msg = e instanceof UpdateError ? e.message : `更新出错（${errName(e)}）`;
    out({ status: "failed", message: `更新失败：${msg}${restored}` });
    return 1;
  } finally {
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  }
}

async function cmdRollback(skillDir) {
  skillDir = path.resolve(skillDir);
  const bak = skillDir + ".bak";
  process.chdir(os.tmpdir());
  let tmp = null;
  try {
    guardSkillDir(skillDir);
    if (!(exists(bak) && fs.statSync(bak).isDirectory())) throw new UpdateError("没有可回滚的备份版本");
    enableBackup(bak);
    let prev;
    try {
      prev = localVersion(bak);
    } catch {
      disableBackup(bak);
      throw new UpdateError("备份目录不完整，无法回滚");
    }
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "lexiang-rollback-"));
    const curCopy = path.join(tmp, "current");
    copyTree(skillDir, curCopy);
    try {
      restore(skillDir, bak); // bak → skill_dir
    } catch (err) {
      replaceContents(skillDir, curCopy);
      throw err;
    }
    // 原当前版本变成新的 .bak，便于再次回滚
    if (exists(bak)) rmTree(bak);
    copyTree(curCopy, bak);
    disableBackup(bak);
    out({ status: "rolled_back", to: prev.version, message: `已回滚到 ${prev.version}，请新开一个对话后生效` });
    return 0;
  } catch (e) {
    const msg = e instanceof UpdateError ? e.message : `回滚出错（${errName(e)}）`;
    out({ status: "failed", message: `回滚失败：${msg}` });
    return 1;
  } finally {
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function cmdVersion(skillDir) {
  try {
    out(localVersion(skillDir));
    return 0;
  } catch {
    out({ status: "error", message: "无法读取 VERSION.json" });
    return 1;
  }
}

async function main(argv) {
  const usage = "usage: update.mjs {check [--force],apply,rollback,version}";
  const [cmd, ...rest] = argv;
  const skillDir = DEFAULT_SKILL_DIR;
  const valid =
    (cmd === "check" && rest.every((a) => a === "--force")) ||
    (["apply", "rollback", "version"].includes(cmd) && rest.length === 0);
  if (!valid) {
    process.stderr.write(usage + "\n");
    return 2;
  }
  if (cmd === "check") return cmdCheck(skillDir, rest.includes("--force"));
  if (cmd === "apply") return cmdApply(skillDir);
  if (cmd === "rollback") return cmdRollback(skillDir);
  return cmdVersion(skillDir);
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  () => {
    out({ status: "error", reason: "internal_error" });
    process.exitCode = 1;
  },
);
