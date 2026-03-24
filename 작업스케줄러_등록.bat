@echo off
chcp 65001 >nul
echo.
echo  ╔════════════════════════════════════════════╗
echo  ║  Windows 작업 스케줄러 등록                ║
echo  ║  매일 오전 9시 자동 발행 설정              ║
echo  ╚════════════════════════════════════════════╝
echo.

REM 이 파일의 현재 폴더 경로 자동 감지
set SCRIPT_DIR=%~dp0
set BAT_PATH=%SCRIPT_DIR%자동발행_실행.bat

echo 등록할 경로: %BAT_PATH%
echo.

REM 작업 스케줄러에 등록 (매일 오전 9시)
schtasks /create /tn "노션WP자동발행" /tr "%BAT_PATH%" /sc DAILY /st 09:00 /f /rl HIGHEST

echo.
if %errorlevel% == 0 (
    echo  ✅ 등록 완료!
    echo  ✅ 매일 오전 9시에 자동으로 실행됩니다.
    echo  ✅ PC가 켜져 있을 때만 실행됩니다.
    echo.
    echo  확인 방법: 작업 스케줄러 앱 열기 - "노션WP자동발행" 항목 확인
) else (
    echo  ❌ 등록 실패. 관리자 권한으로 다시 실행해주세요.
    echo  방법: 이 파일 우클릭 - "관리자 권한으로 실행"
)

echo.
pause
