# CLAUDE.md

代理在本仓必须遵守的规则；接口、配置、镜像构建等细节见 [README.md](README.md)（中文版 [README_CN.md](README_CN.md)）。

## 测试

本仓没有 CI，开 PR 前必须在本地跑全量。测试依赖：`pip install -e ".[dev]"`。

- 全量（约 12 秒）：
  `WORK_AREA_ROOT=$(mktemp -d) python -m pytest -q && PYTHONPATH=images/ubuntu python -m pytest -q images/ubuntu/tests -n auto`
- 编排层 `tests/`（unit + integration）：串行跑，不加 `-n`，串行反而更快。临时 `WORK_AREA_ROOT` 让测试不碰 `.env` 里配置的工作区。
- 容器 app `images/ubuntu/tests`：加 `-n auto`，串行约 48 秒。
- 开发中只跑覆盖本次改动的测试文件，不要每轮修改都跑全量。
- live 验收（`SANDBOXHUB_LIVE=1`）只在需要真实环境时按需开启，见 README 的 Work-area v2 一节。

## 工作区

- 主检出运行共享 dev Hub（:8099，tmux `cr-sandbox`）：不在主检出里开发、切分支或 pull，也不重启这个 Hub。
- 开发用 `git worktree add` 在 `.claude/worktrees/<名字>` 下从 `origin/dev` 建分支；需要起 Hub 联调时另起隔离实例，不要复用共享 Hub。

## 容器 app 改动

- 改了 `images/ubuntu/app/` 就要 bump `images/ubuntu/app/VERSION`，并把新版本同步到 `config/system.yaml`（`image_code` / `image_ubuntu`）和 `src/config.py`（`DOCKER_IMAGE_CODE_V2` / `DOCKER_IMAGE_UBUNTU_V2`）里的 pin。
- 镜像构建命令见 README 的 Quick Start 一节。

## Git

- 本仓没有配置本地 git 身份，提交时用 `git -c user.name=llmaddcom -c user.email=createrole.com@gmail.com commit ...`。
- 功能分支向 `dev` 开 PR；`dev` → `main` 的 PR 即发布，需要用户确认。
