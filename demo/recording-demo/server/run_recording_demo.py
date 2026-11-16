"""Start a fresh, loopback-only recording demo without the platform .env."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

SERVER = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="在独立新目录启动全流程演示；历史运行数据不会删除")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--allow-live", action="store_true", help="明确允许使用当前进程环境中配置的真实模型；不读取 .env，可能产生费用")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口须位于 1024–65535")
    os.environ.update(SKIP_ENV_FILE="1", APP_ENV="development", STUDY_DEMO_ENABLED="false",
                      STUDY_WORKER_ENABLED="true", COOKIE_SECURE="false", RECORDING_DEMO_ALLOW_LIVE="1" if args.allow_live else "0")
    # Delete configuration by key without reading or printing inherited secrets.
    for key in tuple(os.environ):
        if key.startswith(("MAXKB_", "KNOWLEDGE_")) or (not args.allow_live and key.startswith("LLM_")):
            os.environ.pop(key, None)
    sys.path.insert(0, str(SERVER))
    from app.recording_demo import create_recording_app
    import uvicorn
    app = create_recording_app(allow_live=args.allow_live)
    print("演示地址：http://127.0.0.1:%d/prototype/" % args.port, flush=True)
    print("独立数据目录：" + str(app.state.runtime.data_dir), flush=True)
    print("人物、课程为虚构演示；保存与提交真实落库，模拟部分在界面和结果中标注。", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
