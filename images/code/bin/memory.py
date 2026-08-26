#!/usr/bin/env python3
"""memory —— 长期记忆 CLI（沙盒内使用，零第三方依赖）。

数字人在沙盒终端里回想自己长期记忆的入口（只读）：

    memory recall 用户对出差的偏好
    memory show chuchai-pianhao
    memory list

服务端半边是后端 agent 面路由 ``/agent/memory/*``；凭据（后端基址 + 短时 scoped
token，与 skills / todo 共用同一枚）由后端在沙盒 acquire 时写进容器本地
``~/.config/createrole/credentials.json``（不落云盘），也可用环境变量
``CR_API_BASE`` / ``CR_SANDBOX_TOKEN`` 覆盖。
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
TIMEOUT_SECONDS = 20

# 容器内直连宿主后端，不走任何代理（env 里的 http_proxy 等一律忽略）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CliError(Exception):
    """面向使用者的失败：打印 message 后以退出码 1 结束。"""


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


def _request(path: str, *, params: dict | None = None) -> dict:
    api_base, token = _load_credentials()
    url = api_base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(req, timeout=TIMEOUT_SECONDS) as resp:
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
        raise CliError(f"记忆请求失败 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CliError(
            f"连不上后端服务 {api_base}: {exc.reason}。"
            "请确认沙盒到宿主后端网络可达。"
        ) from exc


def _quote(ref: str) -> str:
    return urllib.parse.quote(ref, safe="")


def cmd_recall(args: argparse.Namespace) -> None:
    query = " ".join(args.query).strip()
    if not query:
        raise CliError("检索词不能为空：memory recall <想找的事>")
    params: dict[str, object] = {"q": query}
    if args.limit:
        params["limit"] = args.limit
    print(_request("/agent/memory/recall", params=params).get("view", ""))


def cmd_show(args: argparse.Namespace) -> None:
    print(_request(f"/agent/memory/pages/{_quote(args.slug)}").get("view", ""))


def cmd_list(args: argparse.Namespace) -> None:
    params: dict[str, object] = {}
    if args.limit:
        params["limit"] = args.limit
    if args.offset:
        params["offset"] = args.offset
    print(_request("/agent/memory/pages", params=params).get("view", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory",
        description="长期记忆 CLI（只读）：回想/展开/浏览自己的长期记忆",
        epilog=(
            "例：memory recall 用户的口味偏好 ｜ memory show <slug> ｜ memory list。"
            "记忆写入无需操心——对话会自动沉淀。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    recall = sub.add_parser("recall", help="按线索回想（多词直接空格连写，无需引号）")
    recall.add_argument("query", nargs="+", help="想找的事，如：用户 出差 偏好")
    recall.add_argument("-n", "--limit", type=int, default=None, help="最多几条（默认 5）")
    recall.set_defaults(func=cmd_recall)

    show = sub.add_parser("show", help="展开一页记忆全文（slug 见钩子行或 list）")
    show.add_argument("slug", help="记忆页 slug")
    show.set_defaults(func=cmd_show)

    list_cmd = sub.add_parser("list", help="浏览全部记忆页索引（一行一页）")
    list_cmd.add_argument("-n", "--limit", type=int, default=None, help="每页条数（默认 20）")
    list_cmd.add_argument("--offset", type=int, default=None, help="翻页偏移")
    list_cmd.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CliError as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
