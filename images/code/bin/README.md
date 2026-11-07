# 预装 CLI 目录（issue #15 / #16）

放在本目录的单文件脚本会在 code 镜像构建时装进容器 `/usr/local/bin`（去 `.py`
扩展名，如 `skills.py` → `/usr/local/bin/skills`）。

要求：

- 单文件、零第三方依赖（仅 Python 标准库），自带 shebang（`#!/usr/bin/env python3`）；
- 本体由上游仓维护并随版本同步到这里（createrole_com_api 仓，各板块的 `interfaces/cli/`）：
  - `skills` — `server/skills/interfaces/cli/skills.py`（技能根读 `CR_SKILLS_DIR`，布局 v2 为 `/self/skills`）；
  - `todo` — `server/todo/interfaces/cli/todo.py`；
  - `tools` — `server/tools/interfaces/cli/tools.py`（挂载的外部工具 / MCP 经平台网关调用，凭据不进沙盒；说明在 `/opt/cr/tools/INDEX.md`）；
  - `memory` — `server/infrastructure/sandbox_cli/memory.py`（只读，见 issue #20，llmaddcom/createrole#366）。
- 更新脚本后须 bump `images/ubuntu/app/VERSION` 并重建镜像。

`.md` 文件会被构建脚本跳过。
