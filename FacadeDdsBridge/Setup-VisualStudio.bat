@echo off
REM Double-click wrapper for Setup-VisualStudio.ps1 (see that script's own header comment for
REM what it does and why: generates the CMake/Visual Studio solution and writes
REM build\Directory.Build.props so build\FacadeDdsBridge.sln can be opened and built directly
REM in Visual Studio 2022 without hitting the VCToolsVersion link errors).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-VisualStudio.ps1" %*
pause
