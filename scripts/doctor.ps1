$ErrorActionPreference = "SilentlyContinue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$failures = 0
function Check($label, [scriptblock]$probe) {
    $null = & $probe 2>$null
    if ($LASTEXITCODE -eq 0 -or $?) {
        Write-Host ("  ok  {0}" -f $label) -ForegroundColor Green
    } else {
        Write-Host (" fail {0}" -f $label) -ForegroundColor Red
        $script:failures++
    }
}

Write-Host "# host tools" -ForegroundColor White
Check "docker"        { docker info | Out-Null }
Check "kubectl"       { Get-Command kubectl }
Check "kind"          { Get-Command kind }
Check "helm"          { Get-Command helm }
Check "python 3.11+"  { $v = (python --version 2>&1).Split(" ")[1]; if ([version]$v -lt [version]"3.11") { throw } }
Check ".venv exists"  { Test-Path .venv }

Write-Host "`n# kind cluster" -ForegroundColor White
Check "cluster 'chaos' exists"   { (kind get clusters) -contains "chaos" }
Check "kube-context kind-chaos"  { (kubectl config current-context) -eq "kind-chaos" }

Write-Host "`n# chaos-mesh" -ForegroundColor White
Check "ns chaos-mesh"  { kubectl get ns chaos-mesh | Out-Null }
Check "controller"     { kubectl -n chaos-mesh wait --for=condition=available --timeout=5s deploy/chaos-controller-manager | Out-Null }
Check "CRDs present"   { kubectl get crd | Select-String "chaos-mesh.org" }

Write-Host "`n# observability" -ForegroundColor White
Check "ns observability" { kubectl get ns observability | Out-Null }
Check "prometheus"       { kubectl -n observability wait --for=condition=available --timeout=5s deploy/kps-kube-prometheus-stack-prometheus | Out-Null }

Write-Host "`n# target app" -ForegroundColor White
Check "ns otel-demo"          { kubectl get ns otel-demo | Out-Null }
Check "annotation set"        {
    $a = kubectl get ns otel-demo -o jsonpath="{.metadata.annotations.chaos\.kosta\.dev/allowed}"
    if ($a -ne "true") { throw }
}

Write-Host "`n# security tools" -ForegroundColor White
foreach ($t in @("trivy","syft","grype","gitleaks","cosign","kubescape")) {
    Check "$t installed" { Get-Command $t }
}
Check "ZAP image pulled" { docker image inspect owasp/zap2docker-stable | Out-Null }

if ($failures -eq 0) {
    Write-Host "`nall clear" -ForegroundColor Green
} else {
    Write-Host "`n$failures check(s) failed" -ForegroundColor Yellow
}
exit $failures
