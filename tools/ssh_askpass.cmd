@echo off
rem SSH_ASKPASS helper for FacadePreviewer's 고해상도 전송 (password login).
rem ssh (tools\cygwin_rsync\bin\ssh.exe) runs this with SSH_ASKPASS_REQUIRE=force and reads the
rem password from its stdout; the password comes from the SSHPASS environment variable that
rem RsyncTransfer.cpp / TransferSettingsWindow.xaml.cs set on the ssh process only (never on a
rem command line). Replaces sshpass.exe, whose pseudo-terminal made Windows 11 open a visible
rem Windows Terminal window in front of the transfer window (2026-10-05).
rem Delayed expansion prints the value as-is, including & | < > ^ characters.
setlocal EnableDelayedExpansion
echo(!SSHPASS!
