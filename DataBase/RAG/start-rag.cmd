@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Set up the RAG virtual environment first. See README.md.
    exit /b 1
)
pushd "%~dp0" || exit /b 1
".venv\Scripts\python.exe" -B -X utf8 -m rag_db start
set "rag_exit=%errorlevel%"
popd
exit /b %rag_exit%
