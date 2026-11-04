@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Interface web em MODO SIMULACAO: nada e enviado a Vivo nem ao Zabbix.
rem O nome do iniciador principal tem acentos; o conteudo deste arquivo fica
rem em ASCII e o localiza por curinga para nao depender da pagina de codigo.
set "ROBO_MAIN_BAT="
for %%F in ("%~dp0Executar automa*o vivo.bat") do set "ROBO_MAIN_BAT=%%~fF"
if not defined ROBO_MAIN_BAT goto :missing
call "%ROBO_MAIN_BAT%" --simulacao
exit /b %errorlevel%

:missing
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show('Iniciador principal nao encontrado. Extraia o ZIP completo.','Automacao Vivo','OK','Error')" >nul 2>&1
exit /b 1
