

import os
import json
import traceback
from dotenv import load_dotenv

from webull.core.client import ApiClient
from webull.core.http.initializer.token.token_manager import TokenManager
from webull.data.request.get_option_snapshot_request import (
    GetOptionSnapshotRequest,
)

load_dotenv()


def get_env(*names):
    """按顺序读取环境变量。"""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def print_value(label, value):
    """尽可能清晰地打印响应字段。"""
    print(f"\n--- {label} ---")

    if isinstance(value, (dict, list, tuple)):
        print(
            json.dumps(
                value,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    elif isinstance(value, bytes):
        print(value.decode("utf-8", errors="replace"))
    else:
        print(value)


def print_response_details(response):
    """打印 SDK 返回对象及其可读取的数据。"""
    print("\n========== API RESPONSE ==========")
    print("Response type:", type(response).__name__)
    print("Response repr:", repr(response))

    if response is None:
        print("响应对象为 None。")
        return

    # 常见响应字段；SDK 不支持的字段会自动跳过。
    for attr in (
        "status_code",
        "status",
        "headers",
        "text",
        "content",
        "data",
        "body",
        "json",
    ):
        try:
            value = getattr(response, attr, None)

            if value is None:
                continue

            # json 可能是一个方法，需要调用才能获得 JSON 数据。
            if attr == "json" and callable(value):
                try:
                    value = value()
                except Exception as exc:
                    print(f"\n读取 response.json() 失败: {exc}")
                    continue

            if callable(value):
                continue

            print_value(attr, value)

        except Exception as exc:
            print(f"读取属性 {attr} 失败: {exc}")

    # Pydantic 模型常见的序列化方式。
    try:
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            print_value("model_dump()", model_dump())
    except Exception as exc:
        print(f"model_dump() 失败: {exc}")

    # 某些 SDK 对象使用 to_dict()。
    try:
        to_dict = getattr(response, "to_dict", None)
        if callable(to_dict):
            print_value("to_dict()", to_dict())
    except Exception as exc:
        print(f"to_dict() 失败: {exc}")

    # 打印对象实例属性，辅助判断 SDK 的实际返回结构。
    try:
        if hasattr(response, "__dict__"):
            print_value("instance attributes (__dict__)", vars(response))
    except Exception as exc:
        print(f"读取 __dict__ 失败: {exc}")

    print("\n========== END RESPONSE ==========")


def main():
    app_key = get_env(
        "WEBULL_APP_KEY",
        "WEBULL_OPENAPI_APP_KEY",
        "APP_KEY",
    )

    app_secret = get_env(
        "WEBULL_APP_SECRET",
        "WEBULL_OPENAPI_APP_SECRET",
        "APP_SECRET",
    )

    region_id = get_env(
        "WEBULL_REGION_ID",
        "REGION_ID",
    ) or "us"

    if not app_key or not app_secret:
        raise RuntimeError(
            "缺少 App Key 或 App Secret，请检查项目根目录的 .env 文件。"
        )

    # 先验证客户端初始化及认证。
    print("Initializing Webull OpenAPI client...")
    print(f"Region: {region_id}")

    try:
        client = ApiClient(
            app_key,
            app_secret,
            region_id,
        )

        TokenManager().init_token(client)

        print("Token initialization completed.")

    except Exception as exc:
        print(
            f"\n客户端初始化或认证失败："
            f"{type(exc).__name__}: {exc}"
        )
        traceback.print_exc()
        return

    # 期权合约。
    symbol = "SPY261218C00640000"

    request = GetOptionSnapshotRequest()
    request.set_symbols(symbol)
    request.set_category("US_OPTION")

    # 保留 SDK 默认 API endpoint，不手动覆盖。
    print(f"\nRegion: {region_id}")
    print(f"Option: {symbol}")
    print("正在请求期权快照……")

    try:
        response = client.get_response(request)
        print_response_details(response)

    except Exception as exc:
        print(
            f"\n期权快照请求失败："
            f"{type(exc).__name__}: {exc}"
        )
        traceback.print_exc()


if __name__ == "__main__":
    main()
