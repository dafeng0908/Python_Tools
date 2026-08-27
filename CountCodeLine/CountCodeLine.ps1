$files = Get-ChildItem -Recurse -Include *.c,*.h,*.cpp,*.hpp

$totalCodeLines = 0

foreach ($file in $files) {

    $inBlockComment = $false
    $codeLines = 0

    foreach ($line in Get-Content $file.FullName) {

        $text = $line

        # 處理 /* ... */ 註解
        while ($true) {

            if ($inBlockComment) {
                $end = $text.IndexOf("*/")

                if ($end -ge 0) {
                    $text = $text.Substring($end + 2)
                    $inBlockComment = $false
                }
                else {
                    $text = ""
                    break
                }
            }
            else {
                $start = $text.IndexOf("/*")

                if ($start -ge 0) {
                    $end = $text.IndexOf("*/", $start + 2)

                    if ($end -ge 0) {
                        $text = $text.Remove(
                            $start,
                            $end - $start + 2
                        )
                    }
                    else {
                        $text = $text.Substring(0, $start)
                        $inBlockComment = $true
                        break
                    }
                }
                else {
                    break
                }
            }
        }

        # 移除 // 後面的內容
        $slash = $text.IndexOf("//")

        if ($slash -ge 0) {
            $text = $text.Substring(0, $slash)
        }

        # 排除空白
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            $codeLines++
        }
    }

    $totalCodeLines += $codeLines

    Write-Host "$($file.FullName) : $codeLines"
}

Write-Host ""
Write-Host "============================"
Write-Host "Files      : $($files.Count)"
Write-Host "Code Lines : $totalCodeLines"
Write-Host "============================"
