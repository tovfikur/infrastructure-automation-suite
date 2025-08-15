@echo off
echo Creating GitHub repository and pushing code...
echo.

REM Check if GitHub CLI is installed
gh --version >nul 2>&1
if errorlevel 1 (
    echo GitHub CLI not found. Please install it or create repository manually.
    echo Visit: https://cli.github.com/
    pause
    exit /b 1
)

REM Create repository and push
gh repo create infrastructure-automation-suite --public --description "Complete Infrastructure Automation Suite - AI-powered monitoring + Multi-tenant SaaS platform" --source=. --push

echo.
echo ✅ Repository created and pushed successfully!
echo 🔗 View at: https://github.com/tovfikur/infrastructure-automation-suite
echo.
pause
