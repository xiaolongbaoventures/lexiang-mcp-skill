# 乐享知识库 Skill 自动更新 —— PowerShell 版（Windows PowerShell 5.1 / pwsh 7，无需额外模块）。
# 与 scripts/update.py 行为、命令、输出 JSON 一致，修改时须三者（py / mjs / ps1）同步。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/update.ps1 check [--force] | apply | rollback | version
#
# 只会改动本 Skill 目录及其同级备份目录 `<目录名>.bak`，不触碰 MCP 客户端配置或任何密钥。
# 注意：本文件须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会读错中文。

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$SkillName = 'lexiang-knowledge-base'
$CheckFile = '.last_update_check'
$Placeholder = '__MANIFEST_URL__'
$ManifestTimeout = 5
$DownloadTimeout = 30
$MaxZipSize = 50 * 1024 * 1024
$DisabledSkillMd = 'SKILL.md.bak-disabled'
# 仅供测试注入故障：rename_blocked（模拟 Windows 目录被占用）/ move / post_install
$Fault = [Environment]::GetEnvironmentVariable('LEXIANG_UPDATE_FAULT')
if (-not $Fault) { $Fault = '' }
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$DefaultSkillDir = [IO.Path]::GetFullPath([IO.Path]::Combine((Split-Path -Parent $PSCommandPath), '..')).TrimEnd('\', '/')

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

# ---------------------------------------------------------------- 基础工具

function Write-Out($obj) {
    $json = ConvertTo-Json -InputObject $obj -Compress -Depth 5
    $bytes = $Utf8NoBom.GetBytes($json + "`n")
    $s = [Console]::OpenStandardOutput()
    $s.Write($bytes, 0, $bytes.Length)
    $s.Flush()
}

function New-UpdateError([string]$msg) {
    $e = New-Object System.Exception($msg)
    $e.Data['lexiang_update'] = $true
    return $e
}

function Test-UpdateError($ex) { return ($null -ne $ex -and $ex.Data.Contains('lexiang_update')) }

function Get-ErrName($ex) {
    while ($null -ne $ex.InnerException) { $ex = $ex.InnerException }
    return $ex.GetType().Name
}

function ConvertFrom-JsonText([string]$text) {
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    return (ConvertFrom-Json -InputObject $text)
}

function Read-JsonFile([string]$p) {
    return (ConvertFrom-JsonText ([IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)))
}

function Test-Obj($o) { return ($o -is [System.Management.Automation.PSCustomObject]) }
function Test-Prop($o, [string]$name) { return ((Test-Obj $o) -and $null -ne $o.PSObject.Properties[$name]) }
function Get-Prop($o, [string]$name) { return $o.PSObject.Properties[$name].Value }

function ConvertTo-PyInt($v) {
    # 等价于 Python int()：整数 / 浮点截断 / 纯整数字符串
    if ($v -is [bool]) { if ($v) { return [long]1 } else { return [long]0 } }
    if ($v -is [int] -or $v -is [long] -or $v -is [int16] -or $v -is [byte] -or $v -is [bigint]) { return [long]$v }
    if ($v -is [double] -or $v -is [single] -or $v -is [decimal]) {
        $d = [double]$v
        if ([double]::IsNaN($d) -or [double]::IsInfinity($d)) { throw (New-Object System.FormatException('not an int')) }
        return [long][Math]::Truncate($d)
    }
    if ($v -is [string] -and $v -match '^\s*[+-]?\d+\s*$') { return [long]::Parse($v.Trim(), [Globalization.CultureInfo]::InvariantCulture) }
    throw (New-Object System.FormatException('not an int'))
}

function Get-LocalVersion([string]$dir) {
    $d = Read-JsonFile ([IO.Path]::Combine($dir, 'VERSION.json'))
    foreach ($k in 'name', 'version', 'build') { if (-not (Test-Prop $d $k)) { throw (New-Object System.FormatException('bad VERSION.json')) } }
    return [ordered]@{ name = (Get-Prop $d 'name'); version = [string](Get-Prop $d 'version'); build = (ConvertTo-PyInt (Get-Prop $d 'build')) }
}

function Get-Config([string]$dir) {
    $cfg = $null
    try { $cfg = Read-JsonFile ([IO.Path]::Combine($dir, 'update.json')) } catch { $cfg = $null }
    $url = ''
    if ((Test-Prop $cfg 'manifest_url') -and (Get-Prop $cfg 'manifest_url')) { $url = ([string](Get-Prop $cfg 'manifest_url')).Trim() }
    $hours = 8.0
    if (Test-Prop $cfg 'check_interval_hours') {
        try { $hours = [double](Get-Prop $cfg 'check_interval_hours') } catch { $hours = 8.0 }
    }
    return @{ url = $url; hours = $hours }
}

function Test-UrlAllowed([string]$url) {
    # https 任意主机；http 仅限本机（测试）；file:// 用于本地测试
    if ($url -notmatch '^[A-Za-z][A-Za-z0-9+.-]*:') { return $false }
    $u = $null
    if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$u)) { return $false }
    if ($u.Scheme -eq 'https') { return (-not [string]::IsNullOrEmpty($u.Host)) }
    if ($u.Scheme -eq 'http') { return (@('127.0.0.1', 'localhost', '[::1]', '::1') -contains $u.Host) }
    return ($u.Scheme -eq 'file')
}

function Test-Configured([string]$url) {
    return ([bool]$url -and -not $url.Contains($Placeholder) -and -not $url.StartsWith('__') -and (Test-UrlAllowed $url))
}

function Get-UrlBytes([string]$url, [int]$timeoutSec) {
    if (-not (Test-UrlAllowed $url)) { throw (New-UpdateError "不支持的下载地址：$url") }
    $u = [Uri]$url
    if ($u.Scheme -eq 'file') { return , [IO.File]::ReadAllBytes($u.LocalPath) }
    $tmp = [IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec -UserAgent "$SkillName-updater" `
            -Headers @{ 'Cache-Control' = 'no-cache' } -OutFile $tmp | Out-Null
        return , [IO.File]::ReadAllBytes($tmp)
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Get-Manifest([string]$url) {
    $raw = Get-UrlBytes $url $ManifestTimeout
    $n = [Math]::Min($raw.Length, 256 * 1024)
    $text = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($raw, 0, $n)
    $m = ConvertFrom-JsonText $text
    if (-not (Test-Obj $m)) { throw (New-Object System.FormatException('manifest is not an object')) }
    foreach ($k in 'name', 'version', 'build', 'zip_url', 'sha256') {
        if (-not (Test-Prop $m $k)) { throw (New-Object System.FormatException("manifest missing $k")) }
    }
    $m.build = ConvertTo-PyInt $m.build
    return $m
}

function Get-Now { return ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) }

function Read-LastCheck([string]$dir) {
    try {
        $s = ([IO.File]::ReadAllText([IO.Path]::Combine($dir, $CheckFile), [Text.Encoding]::UTF8)).Trim()
        $v = 0.0
        if ([double]::TryParse($s, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)) { return $v }
    } catch { }
    return $null
}

function Write-LastCheck([string]$dir) {
    try { [IO.File]::WriteAllText([IO.Path]::Combine($dir, $CheckFile), [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds(), $Utf8NoBom) } catch { }
}

# ---------------------------------------------------------------- check

function Invoke-Check([string]$dir, [bool]$force) {
    try { $cur = Get-LocalVersion $dir } catch {
        Write-Out ([ordered]@{ status = 'error'; reason = 'local_version_unreadable' }); return 0
    }
    $cfg = Get-Config $dir
    if (-not (Test-Configured $cfg.url)) {
        Write-Out ([ordered]@{ status = 'skipped'; reason = 'not_configured'; current = $cur.version; current_build = $cur.build }); return 0
    }
    if (-not $force) {
        $last = Read-LastCheck $dir
        $now = Get-Now
        if ($null -ne $last -and ($now - $last) -ge 0 -and ($now - $last) -lt ($cfg.hours * 3600)) {
            Write-Out ([ordered]@{ status = 'skipped'; reason = 'checked_recently'; current = $cur.version; current_build = $cur.build }); return 0
        }
    }
    try { $m = Get-Manifest $cfg.url } catch {
        Write-LastCheck $dir
        Write-Out ([ordered]@{ status = 'error'; reason = 'manifest_unavailable'; current = $cur.version; current_build = $cur.build }); return 0
    }
    Write-LastCheck $dir
    if (-not ($m.name -is [string]) -or $m.name -cne $cur.name) {
        Write-Out ([ordered]@{ status = 'error'; reason = 'name_mismatch'; current = $cur.version; current_build = $cur.build }); return 0
    }
    $notes = ''
    if (Test-Prop $m 'notes') { $notes = $m.notes }
    $status = 'up_to_date'
    if ($m.build -gt $cur.build) { $status = 'update_available' }
    Write-Out ([ordered]@{ current = $cur.version; current_build = $cur.build; latest = [string]$m.version; latest_build = $m.build; notes = $notes; status = $status })
    return 0
}

# ---------------------------------------------------------------- 解压与校验

function Expand-SafeZip([string]$zipPath, [string]$destRoot, [long]$expectedBuild) {
    $prefix = "$SkillName/"
    [void][IO.Directory]::CreateDirectory($destRoot)
    $destRoot = [IO.Path]::GetFullPath($destRoot).TrimEnd('\', '/')
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try { $zip = [IO.Compression.ZipFile]::OpenRead($zipPath) } catch { throw (New-UpdateError '安装包不是有效的 zip 文件') }
    try {
        $entries = @($zip.Entries)
        if ($entries.Count -eq 0) { throw (New-UpdateError '安装包为空') }
        $names = New-Object System.Collections.Generic.List[string]
        foreach ($e in $entries) {
            $n = $e.FullName
            $names.Add($n)
            $parts = @($n.Split('/') | Where-Object { $_ -ne '' })
            $colon = @($parts | Where-Object { $_.Contains(':') }).Count -gt 0
            if (-not $n.StartsWith($prefix, [StringComparison]::Ordinal) -or $n.Contains('\') -or $n.StartsWith('/') -or ($parts -contains '..') -or $colon) {
                throw (New-UpdateError "安装包结构不正确或包含非法路径：$n")
            }
            if ($null -ne $e.PSObject.Properties['ExternalAttributes']) {
                if ((($e.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw (New-UpdateError "安装包包含符号链接，已拒绝：$n") }
            }
        }
        foreach ($req in 'SKILL.md', 'VERSION.json') {
            if (-not $names.Contains($prefix + $req)) { throw (New-UpdateError "安装包缺少 $req") }
        }
        try {
            $ve = $null
            foreach ($e in $entries) { if ($e.FullName -ceq ($prefix + 'VERSION.json')) { $ve = $e } }
            $reader = New-Object IO.StreamReader($ve.Open(), [Text.Encoding]::UTF8)
            try { $inner = ConvertFrom-JsonText $reader.ReadToEnd() } finally { $reader.Dispose() }
            if (-not (Test-Prop $inner 'name') -or -not ($inner.name -is [string]) -or $inner.name -cne $SkillName) { throw 'mismatch' }
            if (-not (Test-Prop $inner 'build') -or (ConvertTo-PyInt $inner.build) -ne $expectedBuild) { throw 'mismatch' }
        } catch {
            throw (New-UpdateError '安装包内的 VERSION.json 与发布信息不一致')
        }
        $sep = [IO.Path]::DirectorySeparatorChar
        foreach ($e in $entries) {
            $target = [IO.Path]::GetFullPath([IO.Path]::Combine($destRoot, $e.FullName))
            if (-not $target.StartsWith($destRoot + $sep, [StringComparison]::Ordinal)) { throw (New-UpdateError "安装包包含非法路径：$($e.FullName)") }
            if ($e.FullName.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($target); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            $in = $e.Open()
            try {
                $fs = [IO.File]::Create($target)
                try { $in.CopyTo($fs) } finally { $fs.Dispose() }
            } finally { $in.Dispose() }
        }
    } finally { $zip.Dispose() }
    return [IO.Path]::Combine($destRoot, $SkillName)
}

# ---------------------------------------------------------------- 目录操作

function Copy-Tree([string]$src, [string]$dst) {
    [void][IO.Directory]::CreateDirectory($dst)
    foreach ($e in (New-Object IO.DirectoryInfo($src)).GetFileSystemInfos()) {
        $d = [IO.Path]::Combine($dst, $e.Name)
        if ($e -is [IO.DirectoryInfo]) { Copy-Tree $e.FullName $d } else { [IO.File]::Copy($e.FullName, $d, $true) }
    }
}

function Remove-Tree([string]$p) {
    foreach ($f in (New-Object IO.DirectoryInfo($p)).GetFiles('*', [IO.SearchOption]::AllDirectories)) {
        if ($f.IsReadOnly) { $f.IsReadOnly = $false }
    }
    [IO.Directory]::Delete($p, $true)
}

function Clear-Dir([string]$d) {
    foreach ($e in (New-Object IO.DirectoryInfo($d)).GetFileSystemInfos()) {
        if ($e -is [IO.DirectoryInfo]) { Remove-Tree $e.FullName } else { [IO.File]::Delete($e.FullName) }
    }
}

function Set-DirContents([string]$dst, [string]$src) {
    # 保留 dst 目录本身，只替换其内容（Windows 上目录被占用、无法改名时使用）
    Clear-Dir $dst
    foreach ($e in (New-Object IO.DirectoryInfo($src)).GetFileSystemInfos()) {
        $d = [IO.Path]::Combine($dst, $e.Name)
        if ($e -is [IO.DirectoryInfo]) { Copy-Tree $e.FullName $d } else { [IO.File]::Copy($e.FullName, $d, $true) }
    }
}

function Move-Dir([string]$src, [string]$dst) {
    # 等价于 shutil.move：能改名就改名，否则（如跨盘）复制后删除
    try { [IO.Directory]::Move($src, $dst) } catch { Copy-Tree $src $dst; Remove-Tree $src }
}

function Rename-Dir([string]$src, [string]$dst, [string]$skillDir) {
    if ($Fault -eq 'rename_blocked' -and $skillDir -and $src -eq $skillDir) { throw (New-Object System.IO.IOException('simulated: directory in use')) }
    [IO.Directory]::Move($src, $dst)
}

function Disable-Backup([string]$bak) {
    $p = [IO.Path]::Combine($bak, 'SKILL.md')
    if ([IO.File]::Exists($p)) { [IO.File]::Move($p, [IO.Path]::Combine($bak, $DisabledSkillMd)) }
}

function Enable-Backup([string]$bak) {
    $p = [IO.Path]::Combine($bak, $DisabledSkillMd)
    if ([IO.File]::Exists($p)) { [IO.File]::Move($p, [IO.Path]::Combine($bak, 'SKILL.md')) }
}

function Install-Skill([string]$skillDir, [string]$newDir, [string]$bak) {
    # skill_dir → bak，new_dir → skill_dir。优先整体改名，失败（如 Windows 占用）则改为内容复制
    if ([IO.Directory]::Exists($bak)) { Remove-Tree $bak }
    $renamed = $true
    try { Rename-Dir $skillDir $bak $skillDir } catch { $renamed = $false }
    if (-not $renamed) {
        Copy-Tree $skillDir $bak
        try { Set-DirContents $skillDir $newDir } catch { Set-DirContents $skillDir $bak; throw }
        return
    }
    try {
        if ($Fault -eq 'move') {
            [void][IO.Directory]::CreateDirectory($skillDir)
            [IO.File]::WriteAllText([IO.Path]::Combine($skillDir, 'partial'), 'x')
            throw (New-Object System.IO.IOException('simulated: move interrupted'))
        }
        Move-Dir $newDir $skillDir
    } catch {
        $err = $_
        try { if ([IO.Directory]::Exists($skillDir)) { Remove-Tree $skillDir } } catch { }
        [IO.Directory]::Move($bak, $skillDir)
        throw $err
    }
}

function Restore-Skill([string]$skillDir, [string]$bak) {
    # 把 bak 恢复为 skill_dir（尽力而为）
    Enable-Backup $bak
    try {
        if ([IO.Directory]::Exists($skillDir)) { Remove-Tree $skillDir }
        [IO.Directory]::Move($bak, $skillDir)
    } catch {
        [void][IO.Directory]::CreateDirectory($skillDir)
        Set-DirContents $skillDir $bak
    }
}

function Complete-Install([string]$skillDir, [string]$bak, [long]$expectedBuild) {
    if ($Fault -eq 'post_install') { throw (New-Object System.IO.IOException('simulated: disk full')) }
    $old = [IO.Path]::Combine($bak, $CheckFile)
    if ([IO.File]::Exists($old)) { [IO.File]::Copy($old, [IO.Path]::Combine($skillDir, $CheckFile), $true) }
    if ((Get-LocalVersion $skillDir).build -ne $expectedBuild) { throw (New-UpdateError '安装后版本校验失败') }
    Disable-Backup $bak
}

function Assert-SkillDir([string]$skillDir) {
    if (Test-Path -LiteralPath ([IO.Path]::Combine($skillDir, '.git'))) { throw (New-UpdateError '当前目录是源码仓库（含 .git），不是 Skill 安装目录，已拒绝更新') }
    if (-not [IO.File]::Exists([IO.Path]::Combine($skillDir, 'SKILL.md'))) { throw (New-UpdateError '未找到 SKILL.md，无法确认 Skill 安装目录') }
}

function Enter-TempDir {
    # 避免自身工作目录占用 Skill 目录（Windows）
    $t = [IO.Path]::GetTempPath()
    Set-Location -LiteralPath $t
    [Environment]::CurrentDirectory = $t
}

function New-TempDir([string]$prefix) {
    $p = [IO.Path]::Combine([IO.Path]::GetTempPath(), $prefix + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($p)
    return $p
}

# ---------------------------------------------------------------- apply / rollback

function Invoke-Apply([string]$skillDir) {
    $bak = $skillDir + '.bak'
    Enter-TempDir
    $installed = $false
    $tmp = $null
    try {
        Assert-SkillDir $skillDir
        try { $cur = Get-LocalVersion $skillDir } catch { throw (New-UpdateError '无法读取本地 VERSION.json') }
        $cfg = Get-Config $skillDir
        if (-not (Test-Configured $cfg.url)) { throw (New-UpdateError '未配置更新地址（update.json），无法在线更新') }
        try { $m = Get-Manifest $cfg.url } catch { throw (New-UpdateError '无法获取最新版本信息，请检查网络后重试') }
        if (-not ($m.name -is [string]) -or $m.name -cne $cur.name -or $m.name -cne $SkillName) { throw (New-UpdateError '发布信息中的 Skill 名称不匹配，已中止') }
        if ($m.build -le $cur.build) {
            Write-Out ([ordered]@{ status = 'up_to_date'; current = $cur.version; message = "当前已是最新版本 $($cur.version)，无需更新" })
            return 0
        }
        $size = [long]0
        if ((Test-Prop $m 'size') -and $m.size) { $size = ConvertTo-PyInt $m.size }
        $tmp = New-TempDir 'lexiang-update-'
        $zipPath = [IO.Path]::Combine($tmp, 'package.zip')
        try { $data = Get-UrlBytes ([string]$m.zip_url) $DownloadTimeout } catch {
            if (Test-UpdateError $_.Exception) { throw }
            throw (New-UpdateError '下载安装包失败，请检查网络后重试')
        }
        $limit = $MaxZipSize
        if ($size) { $limit = $size }
        if ($data.Length -gt $limit) { throw (New-UpdateError '下载的安装包大小超出预期，已中止') }
        if ($size -and $data.Length -ne $size) { throw (New-UpdateError "安装包大小不符（应为 $size 字节，实际 $($data.Length) 字节），已中止") }
        [IO.File]::WriteAllBytes($zipPath, $data)
        $digest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -cne ([string]$m.sha256).Trim().ToLowerInvariant()) { throw (New-UpdateError '安装包校验失败（sha256 不一致），已中止') }
        $newDir = Expand-SafeZip $zipPath ([IO.Path]::Combine($tmp, 'extract')) $m.build
        Install-Skill $skillDir $newDir $bak
        $installed = $true
        Complete-Install $skillDir $bak $m.build
        Write-LastCheck $skillDir
        Write-Out ([ordered]@{ status = 'updated'; from = $cur.version; to = [string]$m.version; message = "已更新到 $($m.version)，请新开一个对话后生效" })
        return 0
    } catch {
        $ex = $_.Exception
        $restored = ''
        if ($installed) {
            try { Restore-Skill $skillDir $bak; $restored = '，已恢复原版本' } catch { $restored = '，自动恢复失败，请联系管理员' }
        }
        if (Test-UpdateError $ex) { $msg = $ex.Message } else { $msg = "更新出错（$(Get-ErrName $ex)）" }
        Write-Out ([ordered]@{ status = 'failed'; message = "更新失败：$msg$restored" })
        return 1
    } finally {
        if ($tmp) { try { Remove-Tree $tmp } catch { } }
    }
}

function Invoke-Rollback([string]$skillDir) {
    $bak = $skillDir + '.bak'
    Enter-TempDir
    $tmp = $null
    try {
        Assert-SkillDir $skillDir
        if (-not [IO.Directory]::Exists($bak)) { throw (New-UpdateError '没有可回滚的备份版本') }
        Enable-Backup $bak
        try { $prev = Get-LocalVersion $bak } catch { Disable-Backup $bak; throw (New-UpdateError '备份目录不完整，无法回滚') }
        $tmp = New-TempDir 'lexiang-rollback-'
        $curCopy = [IO.Path]::Combine($tmp, 'current')
        Copy-Tree $skillDir $curCopy
        try { Restore-Skill $skillDir $bak } catch { Set-DirContents $skillDir $curCopy; throw }
        # 原当前版本变成新的 .bak，便于再次回滚
        if ([IO.Directory]::Exists($bak)) { Remove-Tree $bak }
        Copy-Tree $curCopy $bak
        Disable-Backup $bak
        Write-Out ([ordered]@{ status = 'rolled_back'; to = $prev.version; message = "已回滚到 $($prev.version)，请新开一个对话后生效" })
        return 0
    } catch {
        $ex = $_.Exception
        if (Test-UpdateError $ex) { $msg = $ex.Message } else { $msg = "回滚出错（$(Get-ErrName $ex)）" }
        Write-Out ([ordered]@{ status = 'failed'; message = "回滚失败：$msg" })
        return 1
    } finally {
        if ($tmp) { try { Remove-Tree $tmp } catch { } }
    }
}

function Invoke-Version([string]$dir) {
    try { Write-Out (Get-LocalVersion $dir); return 0 } catch {
        Write-Out ([ordered]@{ status = 'error'; message = '无法读取 VERSION.json' }); return 1
    }
}

# ---------------------------------------------------------------- 入口

$cmd = $null
$rest = @()
if ($args.Count -gt 0) { $cmd = [string]$args[0]; if ($args.Count -gt 1) { $rest = @($args[1..($args.Count - 1)] | ForEach-Object { [string]$_ }) } }
$valid = $false
if ($cmd -ceq 'check') { $valid = (@($rest | Where-Object { $_ -cne '--force' }).Count -eq 0) }
elseif (@('apply', 'rollback', 'version') -ccontains $cmd) { $valid = ($rest.Count -eq 0) }
if (-not $valid) {
    [Console]::Error.WriteLine('usage: update.ps1 {check [--force],apply,rollback,version}')
    exit 2
}
$code = 1
try {
    switch -CaseSensitive ($cmd) {
        'check' { $code = Invoke-Check $DefaultSkillDir ($rest -ccontains '--force') }
        'apply' { $code = Invoke-Apply $DefaultSkillDir }
        'rollback' { $code = Invoke-Rollback $DefaultSkillDir }
        'version' { $code = Invoke-Version $DefaultSkillDir }
    }
} catch {
    Write-Out ([ordered]@{ status = 'error'; reason = 'internal_error' })
    $code = 1
}
exit $code
