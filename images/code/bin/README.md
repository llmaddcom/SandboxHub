# 预装 CLI 目录（issue #15 / #16）

放在本目录的单文件脚本会在 code 镜像构建时装进容器 `/usr/local/bin`（去 `.py`
扩展名，如 `skillhub.py` → `/usr/local/bin/skillhub`）。

要求：

- 单文件、零第三方依赖（仅 Python 标准库），自带 shebang（`#!/usr/bin/env python3`）；
- 本体由上游仓维护并随版本同步到这里：
  - `skillhub` — createrole 仓（dev）`server/infrastructure/skills/builtin/skillhub/scripts/skillhub.py`
    （见 issue #15/#18）；
  - `todo` — createrole 仓（dev）`server/infrastructure/skills/builtin/todo/scripts/todo.py`
    （见 issue #19，llmaddcom/createrole#362）。
- 更新脚本后须 bump `images/ubuntu/app/VERSION` 并重建镜像。

`.md` 文件会被构建脚本跳过。
