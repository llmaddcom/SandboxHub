#!/usr/bin/env python3
"""skills —— 产品内技能面 CLI（沙盒内使用，零第三方依赖）。

数字人在沙盒终端里发现/安装商城技能、查看本地全量技能的统一入口（镜像预装为
``skills`` 命令；#387 由 ``skillhub`` 改名——与第三方 skillhub.cn 的同名 CLI 冲突，且
``skill`` 单数撞 procps 的进程信号命令）：

    skills search 表格        # 商城搜索
    skills install excel-helper
    skills list               # 本地全量技能索引（不访问网络，接替原 skills/INDEX.md）

服务端半边是后端 agent 面路由 ``/agent/market/*``；凭据（后端基址 + 短时 scoped token）
由后端在沙盒 acquire 时写进容器本地 ``~/.config/createrole/credentials.json``，也可用环境变量
``CR_API_BASE`` / ``CR_SANDBOX_TOKEN`` 覆盖（P1 镜像预装 + env 注入后走环境变量）。

安装是服务端直写云盘（不经容器文件系统），rclone 挂载约 1 秒后在
``/workspace/skills/<slug>/`` 可见。
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
DEFAULT_SKILLS_DIR = "/workspace/skills"
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
            "缺少商城凭据：未设置 CR_API_BASE/CR_SANDBOX_TOKEN，"
            f"且 {path} 不存在或不完整。稍后重试（凭据随沙盒使用自动刷新），"
            "或联系管理员确认商城 CLI 已启用（sandbox.market_cli_api_base）。"
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
        raise CliError(f"商城请求失败 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CliError(
            f"连不上商城服务 {api_base}: {exc.reason}。"
            "请确认沙盒到宿主后端网络可达。"
        ) from exc


def _print_entries(items: list[dict]) -> None:
    if not items:
        print("没有找到匹配的技能。换个关键词试试，或直接 skills search 看全量货架。")
        return
    for item in items:
        official = "[官方] " if item.get("official") else ""
        tags = "".join(f"#{t} " for t in item.get("tags") or [])
        print(f"{item['slug']}  v{item.get('version')}  {official}{item['name']}  {tags}".rstrip())
        desc = (item.get("description") or "").strip().replace("\n", " ")
        if desc:
            print(f"    {desc}")
    print(f"\n共 {len(items)} 个。安装: skills install <slug>")


def cmd_search(args: argparse.Namespace) -> None:
    query = " ".join(args.query).strip()
    path = "/agent/market/skills"
    if query:
        path += "?" + urllib.parse.urlencode({"query": query})
    _print_entries(_request("GET", path).get("items", []))


def cmd_info(args: argparse.Namespace) -> None:
    item = _request(
        "GET", f"/agent/market/skills/{urllib.parse.quote(args.ref, safe='')}"
    )
    _print_entries([item])


def cmd_install(args: argparse.Namespace) -> None:
    payload = {"version": args.version} if args.version else None
    result = _request(
        "POST",
        f"/agent/market/skills/{urllib.parse.quote(args.ref, safe='')}/install",
        payload=payload or {},
    )
    print(f"已安装 {result['name']} v{result['version']} → skills/{result['skill_id']}/")
    print(result.get("note", ""))


def _read_frontmatter_description(skill_md_path: str) -> str:
    """极简 frontmatter 读取：只为 list 展示描述，解析失败返回空串。"""
    try:
        with open(skill_md_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:30]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line[len("description:") :].strip().strip("\"'")
    return ""


# 索引行内描述的截断长度（与 system 常驻清单回退口径一致）。
_MAX_LIST_DESC_LEN = 240


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _MAX_LIST_DESC_LEN:
        return text[: _MAX_LIST_DESC_LEN - 1] + "…"
    return text


def cmd_list(args: argparse.Namespace) -> None:
    """本地全量技能索引：每行「名称 + 单行钩子句 + SKILL.md 路径」。

    这是「拉取才是权威」的全量兜底路径——system 常驻清单截断/召回零命中时模型跑一次
    本命令即可看到全部技能；故只读本地文件，不依赖网络与凭据。
    """
    root = args.dir
    if not os.path.isdir(root):
        raise CliError(f"技能目录不存在: {root}")
    names = sorted(
        n
        for n in os.listdir(root)
        if os.path.isfile(os.path.join(root, n, "SKILL.md"))
    )
    if not names:
        print("本地还没有技能。用 skills search 逛逛商城。")
        return
    for name in names:
        path = os.path.join(root, name, "SKILL.md")
        desc = _one_line(_read_frontmatter_description(path))
        print(f"- {name}: {desc} (file: {path})" if desc else f"- {name} (file: {path})")
    print(f"\n共 {len(names)} 个（本地 {root}）。使用某技能前先读其 SKILL.md 全文。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skills", description="技能面 CLI：搜索/安装商城技能，list 看本地全量技能索引"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="搜索商城（无关键词=全量货架）")
    search.add_argument("query", nargs="*", help="关键词（匹配名称/描述）")
    search.set_defaults(func=cmd_search)

    info = sub.add_parser("info", help="看某个技能的详情")
    info.add_argument("ref", help="技能 slug 或条目 id")
    info.set_defaults(func=cmd_info)

    install = sub.add_parser("install", help="安装技能到自己的角色（装最新已批准版）")
    install.add_argument("ref", help="技能 slug 或条目 id")
    install.add_argument("--version", type=int, default=None, help="指定版本号")
    install.set_defaults(func=cmd_install)

    list_cmd = sub.add_parser("list", help="本地全量技能索引：名称 + 用途 + SKILL.md 路径（不访问网络）")
    list_cmd.add_argument("--dir", default=DEFAULT_SKILLS_DIR, help="技能目录")
    list_cmd.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CliError as exc:
        print(f"skills: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
