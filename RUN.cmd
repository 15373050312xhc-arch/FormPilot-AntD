@echo off
chcp 65001 >nul
echo ============================================
echo   简历自动填表工具 v3 — 客户端排单模式
echo ============================================
echo.
python launcher.py %*
if errorlevel 1 (
    echo.
    echo 执行失败，请查看上方错误信息
    pause
)
