# PowerShell 7 / .NET runtime. Canonicalization only; semantic package QA is Python/site-owned.
$ErrorActionPreference = 'Stop'
$contractRoot = Split-Path -Parent $PSScriptRoot
if (-not ('DatasetCanonicalReference' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'Canonical.cs') -ReferencedAssemblies @('System.Text.Json','System.Text.RegularExpressions','System.Security.Cryptography','System.Collections','System.Runtime','System.Memory')
}
$vectors = Get-Content -LiteralPath (Join-Path $contractRoot 'canonical-vectors.json') -Raw | ConvertFrom-Json
$accepted = 0
$rejected = 0
foreach ($vector in $vectors) {
    $raw = [IO.File]::ReadAllBytes((Join-Path $contractRoot $vector.path))
    $failed = $false
    try { $canonical = [DatasetCanonicalReference]::Canonical($raw) } catch { $failed = $true }
    if ($vector.valid) {
        if ($failed -or [Text.Encoding]::UTF8.GetString($canonical) -cne $vector.canonical -or [DatasetCanonicalReference]::Hash($canonical) -cne $vector.sha256) { throw "Canonical mismatch: $($vector.id)" }
        $accepted++
    } else {
        if (-not $failed) { throw "Invalid canonical input accepted: $($vector.id)" }
        $rejected++
    }
}
$packageCount = 0
$cases = Get-Content -LiteralPath (Join-Path $contractRoot 'fixtures.json') -Raw | ConvertFrom-Json
foreach ($case in $cases) {
    if (-not $case.valid) { continue }
    $raw = [IO.File]::ReadAllBytes((Join-Path $contractRoot $case.initialize))
    if ([DatasetCanonicalReference]::Hash([DatasetCanonicalReference]::Canonical($raw)) -cne $case.requestHash) { throw "Request hash mismatch: $($case.id)" }
    if ([DatasetCanonicalReference]::Hash([DatasetCanonicalReference]::Manifest($raw)) -cne $case.manifestSha256) { throw "Manifest hash mismatch: $($case.id)" }
    $packageCount++
}
[ordered]@{ scope='Independent C# canonicalization vs Python golden bytes; no semantic package/API claim'; validCanonicalVectors=$accepted; invalidCanonicalVectors=$rejected; packageHashesCompared=$packageCount; passed=$true } | ConvertTo-Json
