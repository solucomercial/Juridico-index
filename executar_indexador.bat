@echo off
cd /d "C:\Caminho\Para\O\Teu\Projeto"
call venv\Scripts\activate
python indexar_juridico.py >> log_indexacao.txt 2>&1