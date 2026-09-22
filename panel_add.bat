@echo off
:: Устанавливаем кодировку UTF-8
chcp 65001 > nul

:: Автоматический запрос прав Администратора
net session >nul 2>&1
if %errorLevel% == 0 (
    goto :admin
) else (
    echo Запрашиваем права администратора...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:admin
:: Данные для добавления
set "IP_ADDRESS=192.168.1.11"
set "DOMAIN_NAME=tmvos.panel"
set "HOSTS_PATH=%SystemRoot%\System32\drivers\etc\hosts"

echo Проверка файла hosts...

:: Проверяем, нет ли уже такой записи
findstr /I /C:"%DOMAIN_NAME%" "%HOSTS_PATH%" >nul
if %errorLevel% == 0 (
    echo [ОК] Запись для %DOMAIN_NAME% уже существует. Ничего не изменено.
    goto :end
)

:: Добавляем запись в конец файла с новой строки
echo. >> "%HOSTS_PATH%"
echo %IP_ADDRESS%    %DOMAIN_NAME% >> "%HOSTS_PATH%"
echo [ОК] Запись "%IP_ADDRESS%    %DOMAIN_NAME%" успешно добавлена!

:end
echo.
pause

