$nativeArgs = @((Join-Path $PSScriptRoot 'video-mcp-launch.mjs')) + $args
# Older PowerShell rebuilds a Win32 command line instead of forwarding argument values.
if ($PSVersionTable.PSVersion -lt [version]'7.3' -or $PSNativeCommandArgumentPassing -eq 'Legacy') {
    $nativeArgs = @($nativeArgs | ForEach-Object {
        '"' + [regex]::Replace([regex]::Replace([string]$_, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    })
}
& "C:\Program Files\nodejs\node.exe" @nativeArgs
exit $LASTEXITCODE
