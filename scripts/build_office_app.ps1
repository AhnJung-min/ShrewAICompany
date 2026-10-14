# Shrew AI 오피스 실행 파일(ShrewOffice.exe) 빌드
# 사용: powershell -ExecutionPolicy Bypass -File scripts\build_office_app.ps1
# 결과: 프로젝트 루트에 ShrewOffice.exe (더블클릭 → 서버 시작 + 오피스 창, 창 닫으면 서버 종료)

$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $env:TEMP "shrew-office-build"

function Fail($msg) { Write-Host "실패: $msg"; exit 1 }

python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
  python -m pip install --user --quiet pyinstaller
  if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 설치" }
}

python "$root\scripts\make_icon.py" "$root\brand\shrew-office.ico"
if ($LASTEXITCODE -ne 0) { Fail "아이콘 생성" }

python -m PyInstaller --noconfirm --onefile --noconsole --log-level WARN `
  --name ShrewOffice `
  --icon "$root\brand\shrew-office.ico" `
  --paths "$root\scripts" `
  --distpath "$root" `
  --workpath "$work" `
  --specpath "$work" `
  "$root\scripts\office_app.py"
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 빌드" }

Write-Host "완료: $root\ShrewOffice.exe"
