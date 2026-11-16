@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ============================================
echo  个人微信智能客服 · 主服务 (默认 29999)
echo  Webhook: http://127.0.0.1:29999/webhook
echo ============================================
python webhook_server.py
pause
