@echo off
subst P: C:\Users\akmal\OneDrive\Desktop\FACEBO~1 >nul 2>&1
C:\Python312\python.exe P:\agent.py --publish >> P:\logs\job2_publish.log 2>&1
