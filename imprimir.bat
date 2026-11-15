@echo off
rem Arraste o .g exportado do Orca para cima deste arquivo. Ctrl+C cancela (desliga os aquecedores).
title Impressora - %~nx1
py -3.14 -u "%~dp0ff_stream.py" "%~1" %2
pause
