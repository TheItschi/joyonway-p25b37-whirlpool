"""Entry point: `python run.py` starts the backend on 0.0.0.0:8000.

Configuration via environment variables (see .env.example):
  HOTTUB_BRIDGE_HOST   IP of the ESP8266 RS485-TCP bridge (default 192.168.100.210)
  HOTTUB_BRIDGE_PORT   TCP port of the bridge (default 8899)
  HOTTUB_MODEL         "P25B37" or "P25B85" (default P25B37)
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
