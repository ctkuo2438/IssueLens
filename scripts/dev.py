"""Start the local development servers and the PostgreSQL analysis Worker."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dotenv import dotenv_values


if sys.argv[1:]:
    raise SystemExit("This dev launcher takes no command-line arguments.")

root = Path(__file__).resolve().parent.parent
frontend = root / "frontend"
if not (frontend / "node_modules" / "next").exists():
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True)

def start_api() -> subprocess.Popen:
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000",
         "--timeout-graceful-shutdown", "2"],
        cwd=root, start_new_session=True,
    )
    print(f"API PID: {api.pid}", flush=True)
    return api


# Inspect configuration without exporting dotenv credentials to the frontend process.
dotenv = dotenv_values(root / ".env")
database_configured = bool(os.getenv("DATABASE_URL", dotenv.get("DATABASE_URL") or "").strip())
model_configured = bool(os.getenv("OPENAI_API_KEY", dotenv.get("OPENAI_API_KEY") or ""))
worker = api = web = None
try:
    api = start_api()
    if database_configured and model_configured:
        worker = subprocess.Popen(
            [sys.executable, "-m", "backend.app.worker"], cwd=root, start_new_session=True,
        )
        print(f"Worker PID: {worker.pid}", flush=True)
    web = subprocess.Popen(["npm", "run", "dev"], cwd=frontend, start_new_session=True)
    print("IssueLens: http://127.0.0.1:3000", flush=True)
    print("API docs: http://127.0.0.1:8000/docs", flush=True)
    print("Press Ctrl+C to stop the local stack.", flush=True)
    api_started, restart_delay = time.monotonic(), 1
    # The Worker reconnects to PostgreSQL by itself, so its exit means a configuration
    # error or a stop request. Stopping the stack keeps that error visible.
    while web.poll() is None and (worker is None or worker.poll() is None):
        if api.poll() is not None:
            if not database_configured:
                break
            # Wait 2 seconds, doubling up to 30 while the API keeps failing soon after it starts.
            restart_delay = 2 if time.monotonic() - api_started >= 60 else min(30, restart_delay * 2)
            print(f"API stopped. Restarting it in {restart_delay} seconds.", flush=True)
            time.sleep(restart_delay)
            api, api_started = start_api(), time.monotonic()
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    processes = [process for process in (api, worker, web) if process is not None]
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
