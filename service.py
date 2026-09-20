"""湾区代表队行程联控的运行入口。"""

import argparse
import json
from pathlib import Path

from app.server import Api, build_server
from app.seed import load_seed

SERVICE_ID = "gba-delegation-control"
SERVICE_NAME = "湾区代表队行程联控"

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true",
                        help="自检服务身份与种子数据后退出")
    parser.add_argument("--load", type=str, default=None,
                        help="启动时载入的种子数据 JSON（默认使用 fixtures/sample.json）")
    args = parser.parse_args()

    fixture = Path(args.load) if args.load else DEFAULT_FIXTURE
    api = Api()
    summary = load_seed(api.store, json.loads(fixture.read_text(encoding="utf-8")))

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert summary["teams"] >= 4 and summary["games"] >= 1
        print("基础检查通过")
        print(json.dumps(summary, ensure_ascii=False))
        return

    print(f"{SERVICE_NAME} 监听 :{args.port}，已载入 {fixture}")
    build_server(args.port, api).serve_forever()


if __name__ == "__main__":
    main()
