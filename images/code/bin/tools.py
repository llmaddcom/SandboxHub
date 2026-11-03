#!/usr/bin/env python3
"""tools —— 挂载工具 CLI（沙盒内使用，零第三方依赖）。

数字人调用挂载给自己的外部工具（MCP）的唯一入口（镜像预装为 ``tools`` 命令）：

    tools list                                   # 挂载的工具索引（同 context/tools/INDEX.md）
    tools show notion                            # 某个工具的说明与函数签名（同 README.md）
    tools call notion search '{"query": "周报"}'  # 调用；结果打到 stdout，失败退出码 1
    echo '{"query": "周报"}' | tools call notion search -

服务端半边是后端 agent 面路由 ``/agent/tools/*``：后端用平台侧保存的凭据访问 MCP，
凭据不进沙盒。本命令只带沙盒 CLI 凭据（后端基址 + 短时 scoped token，与 todo / skills
共用同一枚），由后端在沙盒 acquire 时写进容器本地 ``~/.config/createrole/credentials.json``，
也可用环境变量 ``CR_API_BASE`` / ``CR_SANDBOX_TOKEN`` 覆盖。

本体由 createrole_com_api 仓维护，SandboxHub 的 code 镜像预装同一份文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CREDENTIALS_FILE = os.path.expanduser("~/.config/createrole/credentials.json")
# 外部工具可能慢（检索、生成），默认给足；后端另有 MCP 调用墙钟兜底。
DEFAULT_TIMEOUT_SECONDS = 600

# 容器内直连宿主后端，不走任何代理（env 里的 http_proxy 等一律忽略）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CliError(Exception):
    """面向使用者的失败：打印 message 后以退出码 1 结束。"""


def _timeout() -> float:
    raw = os.environ.get("CR_TOOLS_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _load_credentials() -> tuple[str, str]:
    """凭据解析：环境变量优先，其次凭据文件；两者都缺给出可行动的提示。"""
    api_base = os.environ.get("CR_API_BASE", "").strip().rstrip("/")
    token = os.environ.get("CR_SANDBOX_TOKEN", "").strip()
    if api_base and token:
        return api_base, token
    path = os.environ.get("CR_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        api_base = api_base or str(data.get("api_base", "")).strip().rstrip("/")
        token = token or str(data.get("token", "")).strip()
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise CliError(f"凭据文件不可读 {path}: {exc}") from exc
    if not api_base or not token:
        raise CliError(
            "缺少沙盒凭据：未设置 CR_API_BASE/CR_SANDBOX_TOKEN，"
            f"且 {path} 不存在或不完整。稍后重试（凭据随沙盒使用自动刷新），"
            "或联系管理员确认沙盒 CLI 已启用（sandbox.market_cli_api_base）。"
        )
    return api_base, token


def _request(method: str, path: str, *, payload: dict | None = None) -> dict:
    api_base, token = _load_credentials()
    url = api_base + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=_timeout()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001 - 错误体解析失败就用状态码
            pass
        if exc.code == 401:
            raise CliError(
                "凭据无效或已过期。凭据会随沙盒使用自动刷新"
                f"（{DEFAULT_CREDENTIALS_FILE}），稍等片刻重试。"
            ) from exc
        raise CliError(f"工具请求失败 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CliError(
            f"连不上后端服务 {api_base}: {exc.reason}。请确认沙盒到宿主后端网络可达。"
        ) from exc
    except TimeoutError as exc:
        raise CliError(
            f"等待工具结果超时（{_timeout():.0f} 秒）。可设 CR_TOOLS_TIMEOUT 延长后重试。"
        ) from exc


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _parse_arguments(raw: str | None) -> dict:
    if raw is None or raw.strip() == "":
        return {}
    text = sys.stdin.read() if raw.strip() == "-" else raw
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"参数不是合法 JSON：{exc}。示例：'{{\"query\": \"周报\"}}'") from exc
    if not isinstance(value, dict):
        raise CliError("参数必须是 JSON 对象，例如 '{\"query\": \"周报\"}'")
    return value


def cmd_list(args: argparse.Namespace) -> int:
    print(_request("GET", "/agent/tools").get("view", ""))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    print(_request("GET", f"/agent/tools/{_quote(args.name)}").get("view", ""))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    arguments = _parse_arguments(args.arguments)
    data = _request(
        "POST",
        f"/agent/tools/{_quote(args.name)}/{_quote(args.function)}",
        payload={"arguments": arguments},
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 1 if data.get("is_error") else 0
    content = str(data.get("content", ""))
    if data.get("is_error"):
        print(content, file=sys.stderr)
        return 1
    print(content)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools",
        description="调用挂载给你的外部工具（MCP）。说明见 /workspace/context/tools/INDEX.md。",
    )
    sub = parser.add_subparsers(dest="command")
    p_list = sub.add_parser("list", help="挂载的工具索引")
    p_list.set_defaults(func=cmd_list)
    p_show = sub.add_parser("show", help="某个工具的说明与函数签名")
    p_show.add_argument("name", help="工具名（INDEX.md 里的目录名）")
    p_show.set_defaults(func=cmd_show)
    p_call = sub.add_parser("call", help="调用工具的一个函数")
    p_call.add_argument("name", help="工具名")
    p_call.add_argument("function", help="函数名（见该工具 README.md）")
    p_call.add_argument(
        "arguments", nargs="?", help="JSON 对象参数；写 - 从标准输入读；省略即无参数"
    )
    p_call.add_argument(
        "--json", action="store_true", help='输出原始 JSON {"content": ..., "is_error": ...}'
    )
    p_call.set_defaults(func=cmd_call)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_list
    try:
        return args.func(args)
    except CliError as exc:
        print(f"tools: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
