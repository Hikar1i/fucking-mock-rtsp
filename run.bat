@echo off
setlocal
echo === Local Mock RTSP ===
echo Web monitor: http://localhost:8080
echo RTSP webcam:  rtsp://localhost:8554/webcam
echo RTSP video:   rtsp://localhost:8554/video
echo.
echo Press Ctrl+C to stop.
echo.
uv run python main.py
pause
