"""Real platform server with a file-triggered, cross-platform graceful shutdown."""

import argparse
import json
from pathlib import Path
import sys
import threading


SERVER = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(SERVER))
    from app.config import Settings
    from app.main import create_app
    import uvicorn

    config = Settings()
    if config.demo_enabled or config.llm_configured or config.maxkb_url or config.maxkb_api_key:
        raise RuntimeError("platform flow requires the isolated formal configuration")
    application = create_app(config)
    runtime = application.state.runtime
    server = uvicorn.Server(uvicorn.Config(
        application, host="127.0.0.1", port=args.port, workers=1, lifespan="on",
        access_log=False, log_level="warning", timeout_graceful_shutdown=10,
    ))
    watcher_stop = threading.Event()

    def watch_shutdown():
        while not watcher_stop.wait(0.1):
            if args.stop_file.exists():
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch_shutdown, name="platform-flow-stop", daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        watcher_stop.set()
        watcher.join(timeout=2)
        runtime.stop()
        args.status_file.write_text(json.dumps({
            "started": server.started, "stop_requested": args.stop_file.exists(),
            "workers": runtime.worker_status(),
        }), encoding="utf-8")
    return 0 if server.started and args.stop_file.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
