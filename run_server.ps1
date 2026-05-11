$Host.UI.RawUI.WindowTitle = "SubFlow Server"
Set-Location $PSScriptRoot

Write-Host "========================================"
Write-Host "       SubFlow Server Running..."
Write-Host "========================================"
Write-Host ""
Write-Host "Press Ctrl+C to stop the server."
Write-Host ""

& ".\.venv\Scripts\Activate.ps1"
python app.py

Write-Host ""
Write-Host "Server stopped."
Read-Host "Press Enter to close"
