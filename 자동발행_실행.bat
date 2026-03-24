@echo off
chcp 65001 >nul
cd /d "%~dp0"
python notion_wp_publisher.py
timeout /t 5
