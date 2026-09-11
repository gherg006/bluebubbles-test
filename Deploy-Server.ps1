<#
Push the current branch to GitHub, pull it on the Debian server, and restart
the BlueBubbles service. Run from the repository folder:
    .\Deploy-Server.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repository = $PSScriptRoot
Set-Location $repository

$branch = (git branch --show-current).Trim()
if (-not $branch -or $branch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Check out a normal Git branch before deploying."
}
$remote = (git config --get "branch.$branch.remote").Trim()
if (-not $remote) {
    throw "The current branch has no configured Git remote."
}

Write-Host "Pushing $branch to GitHub..."
git push $remote $branch
if ($LASTEXITCODE -ne 0) {
    throw "Git push failed, so the server was not updated."
}

$key = Join-Path $env:USERPROFILE ".ssh\bluebubbles_deploy"
if (-not (Test-Path $key)) {
    throw "The deployment SSH key is missing: $key"
}

Write-Host "Updating the Debian server..."
$remoteCommand = "git -C /srv/bluebubbles-test pull --ff-only origin $branch && sudo /bin/systemctl restart bluebubbles.service && systemctl is-active --quiet bluebubbles.service"
& ssh -i $key -o BatchMode=yes zmacleod@192.168.0.150 $remoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "The server update failed. GitHub still has your pushed version."
}

Write-Host "Deployment complete."
