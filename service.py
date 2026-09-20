"""湾区代表队行程联控运行入口。

默认启动 HTTP 服务；``--check`` 只做基础自检。
``--seed`` 启动时装载 ``fixtures/sample.json``，便于联调演示。
"""

import argparse

from app import DispatchService, Store
from app.api import SERVICE_ID, SERVICE_NAME, build_server, health_payload
from app.bootstrap import load_seed


def build_service(seed=False, fixture_path=None):
    service = DispatchService(Store())
    if seed:
        load_seed(service, fixture_path)
    return service


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--seed", action="store_true",
                        help="启动时装载 fixtures/sample.json")
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        service = build_service(seed=True, fixture_path=args.fixture)
        assert service.list_competitions(), "种子数据应包含正式比赛"
        print("基础检查通过")
        return
    service = build_service(seed=args.seed, fixture_path=args.fixture)
    server = build_server(service, args.port)
    print(f"{SERVICE_NAME} 已启动：0.0.0.0:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
