#!/usr/bin/env python3
"""todo —— 待办事项 CLI（沙盒内使用，零第三方依赖）。

数字人在沙盒终端里管理自己待办事项的入口（镜像预装为 ``todo`` 命令）：

    todo list
    todo add "给领导订明早的机票"
    todo add "提醒开周会" --repeat weekly --weekdays 0 --time 09:30
    todo done 3f2a

服务端半边是后端 agent 面路由 ``/agent/todo/*``；凭据（后端基址 + 短时 scoped token，
与 skills 共用同一枚）由后端在沙盒 acquire 时写进容器本地 ``~/.config/createrole/credentials.json``，
也可用环境变量 ``CR_API_BASE`` / ``CR_SANDBOX_TOKEN`` 覆盖（镜像预装 + env 注入后走环境变量）。
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


def _request(method: str, path: str, *, payload: dict | None = None) -> dict:
    api_base, token = _load_credentials()
    url = api_base + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
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
        raise CliError(f"待办请求失败 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CliError(
            f"连不上后端服务 {api_base}: {exc.reason}。"
            "请确认沙盒到宿主后端网络可达。"
        ) from exc


def _quote(ref: str) -> str:
    return urllib.parse.quote(ref, safe="")


def cmd_list(args: argparse.Namespace) -> None:
    print(_request("GET", "/agent/todo/items").get("view", ""))


def cmd_add(args: argparse.Namespace) -> None:
    payload: dict[str, object] = {"content": args.content}
    if args.repeat:
        payload["repeat"] = args.repeat
    if args.date:
        payload["date"] = args.date
    if args.time:
        payload["time"] = args.time
    if args.weekdays:
        payload["weekdays"] = args.weekdays
    print(_request("POST", "/agent/todo/items", payload=payload).get("message", ""))


def cmd_done(args: argparse.Namespace) -> None:
    result = _request(
        "POST",
        f"/agent/todo/items/{_quote(args.entry_id)}/done",
        payload={"stop": bool(args.stop)},
    )
    print(result.get("message", ""))


def cmd_remove(args: argparse.Namespace) -> None:
    result = _request(
        "POST", f"/agent/todo/items/{_quote(args.entry_id)}/remove", payload={}
    )
    print(result.get("message", ""))


def cmd_edit(args: argparse.Namespace) -> None:
    result = _request(
        "PATCH",
        f"/agent/todo/items/{_quote(args.entry_id)}",
        payload={"content": args.content},
    )
    print(result.get("message", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="todo", description="待办事项 CLI：看/记/办结/删去/改自己的待办与提醒"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="看活跃事项（短 id 即操作引用键）")
    list_cmd.set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="记一条（时间参数全不给=纯待办）")
    add.add_argument("content", help="一条一事、单行内容")
    add.add_argument(
        "--repeat", choices=("once", "daily", "weekly"), default=None,
        help="提醒频次；缺省按 --date/--time/--weekdays 推断",
    )
    add.add_argument("--date", default=None, help="YYYY-MM-DD（北京时间）")
    add.add_argument("--time", default=None, help="HH:MM（北京时间）")
    add.add_argument(
        "--weekdays", type=int, nargs="+", default=None,
        help="每周提醒的星期（0=周一 … 6=周日，可多个）",
    )
    add.set_defaults(func=cmd_add)

    done = sub.add_parser("done", help="办结（循环条目整条停掉须加 --stop 确认）")
    done.add_argument("entry_id", help="条目短 id（todo list 里每行开头的 [xxxx]）")
    done.add_argument("--stop", action="store_true", help="确认整条停掉循环提醒")
    done.set_defaults(func=cmd_done)

    remove = sub.add_parser("remove", help="记错删除（软删留痕）")
    remove.add_argument("entry_id", help="条目短 id")
    remove.set_defaults(func=cmd_remove)

    edit = sub.add_parser("edit", help="只改内容不动时间（改时间请删旧建新）")
    edit.add_argument("entry_id", help="条目短 id")
    edit.add_argument("content", help="新内容（单行）")
    edit.set_defaults(func=cmd_edit)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CliError as exc:
        print(f"todo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
