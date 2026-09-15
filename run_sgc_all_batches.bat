@echo off
title Savar Government College - Scraping ALL Batches
chcp 65001 >nul
echo ======================================================
echo   Savar Government College (SGC) Scraping ALL Batches
echo ======================================================
echo.
python "scraper_sgc.py" --batch all
echo.
echo Process finished.
pause
