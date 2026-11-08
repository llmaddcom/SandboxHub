# 预装 CLI 目录（issue #15 / #16 / #79）

放在本目录的单文件脚本会在 code 镜像构建时装进容器 `/usr/local/bin`（去 `.py`
扩展名，如 `memory.py` → `/usr/local/bin/memory`）。

要求：

- 单文件、零第三方依赖（仅 Python 标准库），自带 shebang（`#!/usr/bin/env python3`）；
- 本体由上游仓维护并随版本同步到这里（createrole_com_api 仓，各板块的 `interfaces/cli/`）：
  - `memory` — `server/infrastructure/sandbox_cli/memory.py`（只读，见 issue #20，llmaddcom/createrole#366）。
- 待办、技能、外接工具不再预装（#79）：平台把零依赖客户端 `/opt/cr/tools/cr_tools.py`
  随 context 投影进容器（createrole_com_api#511），不随镜像发布；
- 更新脚本后须 bump `images/ubuntu/app/VERSION` 并重建镜像。

`.md` 文件会被构建脚本跳过。
