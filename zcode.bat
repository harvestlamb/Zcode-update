@echo off
setlocal EnableExtensions

rem ZCode Mirror - Windows helper (Makefile equivalent)
rem Usage: zcode.bat collect

cd /d "%~dp0"

set "BUILD_ROOT=build"
set "DIST_DIR=dist"
set "CMD=%~1"
if "%CMD%"=="" set "CMD=help"

rem Prefer py launcher, then python.exe
set "PY_EXE="
where py >nul 2>&1 && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE (
  where python >nul 2>&1 && set "PY_EXE=python" && set "PY_ARGS="
)
if not defined PY_EXE (
  echo [ERROR] Python not found. Install Python 3 and add it to PATH.
  exit /b 1
)

if /i "%CMD%"=="help" goto help
if /i "%CMD%"=="deps" goto deps
if /i "%CMD%"=="collect" goto collect
if /i "%CMD%"=="collect-fast" goto collect_fast
if /i "%CMD%"=="dev-server" goto dev_server
if /i "%CMD%"=="import" goto import
if /i "%CMD%"=="up" goto up
if /i "%CMD%"=="down" goto down
if /i "%CMD%"=="logs" goto logs
if /i "%CMD%"=="reindex" goto reindex
if /i "%CMD%"=="clean" goto clean

echo [ERROR] Unknown command: %CMD%
goto help

:help
echo.
echo ZCode Mirror commands:
echo.
echo   zcode deps           install collector deps
echo   zcode collect        full .zdoc WITH Windows installer
echo   zcode collect-fast   .zdoc WITHOUT installer (download 404)
echo   zcode dev-server     preview build\site on :8765
echo   zcode clean          remove build\ (keep dist\*.zdoc)
echo.
echo   zcode import [pkg]   import latest/dist .zdoc into intranet\data
echo   zcode up             docker compose up -d --build
echo   zcode down           docker compose down
echo   zcode logs           docker compose logs -f
echo   zcode reindex        rebuild AI index
echo.
echo Example:
echo   zcode.bat collect
echo.
exit /b 0

:deps
echo === pip install collector deps ===
"%PY_EXE%" %PY_ARGS% -m pip install -r "collector\requirements.txt"
exit /b %ERRORLEVEL%

:collect
echo === full package (includes Windows installer ~140MB+) ===
"%PY_EXE%" %PY_ARGS% -m collector.export --build-root "%BUILD_ROOT%" --out "%DIST_DIR%"
exit /b %ERRORLEVEL%

:collect_fast
echo === fast package (NO installer; download page will 404) ===
echo [WARN] For release builds use: zcode.bat collect
"%PY_EXE%" %PY_ARGS% -m collector.export --build-root "%BUILD_ROOT%" --out "%DIST_DIR%" --skip-releases
exit /b %ERRORLEVEL%

:dev_server
echo Preview: http://localhost:8765/
"%PY_EXE%" %PY_ARGS% -m http.server 8765 --directory "%BUILD_ROOT%\site"
exit /b %ERRORLEVEL%

:import
set "PKG=%~2"
if not defined PKG (
  for /f "delims=" %%F in ('dir /b /a:-d /o:-d "%DIST_DIR%\*.zdoc" 2^>nul') do (
    if not defined PKG set "PKG=%DIST_DIR%\%%F"
  )
)
if not defined PKG (
  echo [ERROR] No .zdoc found. Run: zcode.bat collect
  exit /b 1
)
echo === import %PKG% ===
where bash >nul 2>&1
if errorlevel 1 (
  echo [ERROR] bash not found. Use Git Bash:
  echo   cd intranet ^&^& ./import.sh "../%PKG:\=/%"
  echo Or upload via admin: http://127.0.0.1:8090/admin/
  exit /b 1
)
bash -lc "cd intranet && ./import.sh '../%PKG:\=/%'"
exit /b %ERRORLEVEL%

:up
pushd intranet
docker compose up -d --build
set "ERR=%ERRORLEVEL%"
popd
exit /b %ERR%

:down
pushd intranet
docker compose down
set "ERR=%ERRORLEVEL%"
popd
exit /b %ERR%

:logs
pushd intranet
docker compose logs -f
set "ERR=%ERRORLEVEL%"
popd
exit /b %ERR%

:reindex
pushd intranet
docker compose exec -T ai curl -sf -X POST http://localhost:8000/api/reindex
set "ERR=%ERRORLEVEL%"
popd
exit /b %ERR%

:clean
echo === clean %BUILD_ROOT%\ ===
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
echo Done. dist\*.zdoc kept.
exit /b 0
