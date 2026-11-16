@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
.venv/Scripts/activate.bat
echo ============================================
echo  个人微信智能客服 · 管理后台 (默认 7780)
echo  打开 http://127.0.0.1:7780  admin / admin123
echo ============================================
python -m admin.app
pause
