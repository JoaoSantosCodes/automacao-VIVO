@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROBO_ROOT=%~dp0arquivos\"
cd /d "%ROBO_ROOT%" || goto :folder_error

set "ROBO_APP_EXE=%ROBO_ROOT%Executar Automacao Vivo.exe"
set "ROBO_NODE_EXE=%ROBO_ROOT%runtime\node\bin\node.exe"
set "NODE_PATH=%ROBO_ROOT%runtime\node\node_modules"
set "ROBO_CHROME_EXE="

if not exist "%ROBO_APP_EXE%" goto :app_error
if not exist "%ROBO_NODE_EXE%" goto :node_error
if not exist "%NODE_PATH%\playwright\package.json" goto :playwright_error
if not exist "%NODE_PATH%\playwright-core\package.json" goto :playwright_error

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "ROBO_CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined ROBO_CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "ROBO_CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined ROBO_CHROME_EXE if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "ROBO_CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined ROBO_CHROME_EXE for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /i "REG_SZ"') do set "ROBO_CHROME_EXE=%%~B"
if not defined ROBO_CHROME_EXE for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /i "REG_SZ"') do set "ROBO_CHROME_EXE=%%~B"
if not defined ROBO_CHROME_EXE goto :chrome_error
if not exist "%ROBO_CHROME_EXE%" goto :chrome_error

"%ROBO_NODE_EXE%" -e "require('playwright')" >nul 2>&1
if errorlevel 1 goto :playwright_error

if /i not "%~1"=="--self-test" goto :launch
rem Fora de bloco ( ), para %errorlevel% refletir o resultado real do autoteste.
"%ROBO_APP_EXE%" --self-test
exit /b %errorlevel%

:launch
start "" "%ROBO_APP_EXE%"
if errorlevel 1 goto :start_error
exit /b 0

:app_error
set "ROBO_ERROR_MSG=Pacote incompleto: o programa principal nao foi encontrado. Extraia novamente o ZIP completo e mantenha a pasta arquivos junto do iniciador."
goto :show_error

:node_error
set "ROBO_ERROR_MSG=Pacote incompleto: o componente Node.js nao foi encontrado. Extraia novamente o ZIP completo."
goto :show_error

:playwright_error
set "ROBO_ERROR_MSG=Pacote incompleto: o componente de automacao do portal nao foi encontrado. Extraia novamente o ZIP completo."
goto :show_error

:chrome_error
set "ROBO_ERROR_MSG=Google Chrome nao foi encontrado neste computador. Instale o Chrome e tente novamente."
goto :show_error

:folder_error
set "ROBO_ERROR_MSG=Nao foi possivel acessar a pasta arquivos. Extraia o ZIP completo para uma pasta local e tente novamente."
goto :show_error

:start_error
set "ROBO_ERROR_MSG=Nao foi possivel iniciar a automacao. Extraia o pacote novamente ou contate a equipe responsavel."

:show_error
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show($env:ROBO_ERROR_MSG,'Automacao Vivo','OK','Error')" >nul 2>&1
exit /b 1
