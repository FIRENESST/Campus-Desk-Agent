@echo off
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Set up the RAG virtual environment first. See README.md.
    pause
    exit /b 1
)
pushd "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -B "%~dp0rag_writer.py"
popd
