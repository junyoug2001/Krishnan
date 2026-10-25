# Session-scoped GigE networking. The parent camera backend owns this helper.
param(
    [Parameter(Mandatory=$true)][int]$OwnerPid,
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [Parameter(Mandatory=$true)][string]$Program,
    [Parameter(Mandatory=$true)][string]$HostAddress,
    [Parameter(Mandatory=$true)][string]$CameraAddress
)
$ErrorActionPreference = 'Stop'
$statePath = Join-Path $StateDirectory 'state.json'
$stopPath = Join-Path $StateDirectory 'stop'
$state = [ordered]@{Ready=$false;Closed=$false;OwnerPid=$OwnerPid;RuleName=('Krishnan-Aravis-Temporary-'+[guid]::NewGuid().ToString('N'));RuleAttempted=$false;RuleRemoved=$false;BindingAttempted=$false;CleanupErrors=@()}
function Save-State {
    $temporary = Join-Path $StateDirectory 'state.tmp'
    # A Python reader may briefly hold the previous file open on Windows.
    # Replace atomically and retry sharing violations, rather than delete/move.
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding UTF8
        try {
            if ([IO.File]::Exists($statePath)) { [IO.File]::Replace($temporary,$statePath,[NullString]::Value) }
            else { [IO.File]::Move($temporary,$statePath) }
            return
        } catch {
            if ($attempt -eq 29) { throw }
            Start-Sleep -Milliseconds 10
        }
    }
}
$mutex = $null
$locked = $false
try {
    $owner = [Diagnostics.Process]::GetProcessById($OwnerPid)
    $mutex = New-Object Threading.Mutex($false,('Local\Krishnan-Aravis-'+$HostAddress))
    try { $locked = $mutex.WaitOne(20000) } catch [Threading.AbandonedMutexException] { $locked=$true }
    if (-not $locked) { throw 'Another Aravis camera session owns this Ethernet interface' }
    if ($owner.HasExited) { throw 'Camera backend exited before network setup' }
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $HostAddress)
    if ($addresses.Count -ne 1) { throw 'Camera Ethernet address is absent or ambiguous' }
    $alias = $addresses[0].InterfaceAlias
    $adapter = Get-NetAdapter -Name $alias
    $state.InterfaceAlias = $alias
    $state.InterfaceGuid = [string]$adapter.InterfaceGuid
    $state.HostAddress = $HostAddress
    $state.CameraAddress = $CameraAddress
    $state.Program = $Program
    $state.BeforeBindings = @(Get-NetAdapterBinding -Name $alias | Select-Object ComponentID,Enabled)
    $legacy = @($state.BeforeBindings | Where-Object { $_.ComponentID -eq 'neptunegev' })
    $state.LegacyFilterOriginallyEnabled = $legacy.Count -eq 1 -and $legacy[0].Enabled
    Save-State
    if ($state.LegacyFilterOriginallyEnabled) {
        $state.BindingAttempted = $true
        Save-State
        Disable-NetAdapterBinding -Name $alias -ComponentID 'neptunegev' -Confirm:$false
        if ((Get-NetAdapterBinding -Name $alias -ComponentID 'neptunegev').Enabled) { throw 'Legacy camera filter did not detach' }
        $deadline = (Get-Date).AddSeconds(30)
        do {
            $ready = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -IPAddress $HostAddress -ErrorAction SilentlyContinue).Count -eq 1
            if ($ready -and (Get-NetAdapter -Name $alias).Status -eq 'Up') { break }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $deadline -and -not $owner.HasExited)
        if (-not $ready -or (Get-NetAdapter -Name $alias).Status -ne 'Up') { throw 'Camera Ethernet did not recover with its original address' }
    }
    $state.RuleAttempted = $true
    Save-State
    New-NetFirewallRule -Name $state.RuleName -DisplayName $state.RuleName -Direction Inbound -Action Allow -Protocol UDP -Program $Program -RemoteAddress $CameraAddress -LocalAddress $HostAddress -InterfaceAlias $alias -Profile Any | Out-Null
    $state.Ready = $true
    Save-State
    # Keep ownership even if the GUI vanishes; a dead backend triggers cleanup.
    while (-not (Test-Path -LiteralPath $stopPath) -and -not $owner.WaitForExit(250)) { }
} catch {
    $state.Error = $_.Exception.Message
} finally {
    if ($state.RuleAttempted) {
        try {
            Get-NetFirewallRule -Name $state.RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction Stop
            $state.RuleRemoved = @(Get-NetFirewallRule -Name $state.RuleName -ErrorAction SilentlyContinue).Count -eq 0
            if (-not $state.RuleRemoved) { throw 'Temporary camera firewall rule remains' }
        } catch { $state.CleanupErrors += $_.Exception.Message }
    }
    if ($state.BindingAttempted) {
        try {
            Enable-NetAdapterBinding -Name $state.InterfaceAlias -ComponentID 'neptunegev' -Confirm:$false
            $state.BindingRestored = [bool](Get-NetAdapterBinding -Name $state.InterfaceAlias -ComponentID 'neptunegev').Enabled
            if (-not $state.BindingRestored) { throw 'Original camera Ethernet filter binding was not restored' }
        } catch { $state.CleanupErrors += $_.Exception.Message }
    }
    if ($state.Contains('BeforeBindings')) {
        try {
            $actual = @(Get-NetAdapterBinding -Name $state.InterfaceAlias | Select-Object ComponentID,Enabled)
            $state.BindingDifferences = @($state.BeforeBindings | Where-Object {
                $original = $_
                $match = @($actual | Where-Object { $_.ComponentID -eq $original.ComponentID })
                $match.Count -ne 1 -or $match[0].Enabled -ne $original.Enabled
            })
            if ($state.BindingDifferences.Count -gt 0) { $state.CleanupErrors += 'Network binding readbacks differ from the original state' }
        } catch { $state.CleanupErrors += $_.Exception.Message }
    }
    $state.Closed=$true
    $state.EndedAt=Get-Date -Format o
    Save-State
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
if ($state.Error -or $state.CleanupErrors.Count) { exit 1 }
