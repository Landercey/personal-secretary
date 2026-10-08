# Windows built-in OCR (Windows.Media.Ocr). No install, no VRAM, CPU only.
#
# WHY a PowerShell script instead of a Python library:
#   The old project tested this: Windows OCR is the ONLY zero-install, zero-download,
#   pure-CPU OCR you get for free on any Windows box. Every Python alternative needs
#   a pip install plus 15 MB - 390 MB of models.
#
# WHY this file must stay pure ASCII:
#   PowerShell 5.1 reads .ps1 as ANSI unless there is a BOM. One Chinese character
#   in here turns the whole script into a syntax error on a GBK machine.
#   All Chinese output goes through the JSON file instead.
#
# Contract: always writes OutJson. Never throws to the caller.

param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [Parameter(Mandatory = $true)][string]$OutJson,
    [string]$Language = "zh-Hans-CN"
)

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    function Await($operation, $resultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
        $task = $asTask.Invoke($null, @($operation))
        $task.Wait(-1) | Out-Null
        $task.Result
    }

    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    # Build the language object explicitly: a bare New-Object with a variable
    # can bind the type name as the value in PowerShell 5.1.
    $langTag = [string]$Language
    $langObj = $null
    try {
        $langObj = New-Object -TypeName 'Windows.Globalization.Language' -ArgumentList $langTag
    } catch {
        $langObj = $null
    }

    $engine = $null
    if ($null -ne $langObj) {
        try {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
        } catch {
            $engine = $null
        }
    }
    if ($null -eq $engine) {
        # Fallback: use whatever OCR language the user profile has installed.
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) {
        throw "No OCR engine available (requested language: $langTag)"
    }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $sw.Stop()

    $lines = @()
    foreach ($line in $result.Lines) { $lines += $line.Text }

    $payload = [ordered]@{
        ok         = $true
        engine     = "Windows.Media.Ocr"
        language   = $engine.RecognizerLanguage.LanguageTag
        elapsed_ms = [int]$sw.ElapsedMilliseconds
        line_count = $lines.Count
        text       = ($lines -join "`n")
    }
    [System.IO.File]::WriteAllText($OutJson, ($payload | ConvertTo-Json -Depth 4),
        (New-Object System.Text.UTF8Encoding $false))
    exit 0
}
catch {
    $payload = [ordered]@{
        ok    = $false
        error = $_.Exception.Message
        text  = ""
    }
    try {
        [System.IO.File]::WriteAllText($OutJson, ($payload | ConvertTo-Json -Depth 4),
            (New-Object System.Text.UTF8Encoding $false))
    } catch {
        # Nothing else we can do. The caller reports "OCR produced no result".
    }
    exit 2
}
