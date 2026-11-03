# dde

本仓库维护 DDE 各组件组的版本。发版时，在这里统一触发成员仓库的 changelog 更新。

这里原先维护 UOS 20 的 `dde` 元包，2023 年后停止更新，现已停用。旧打包文件保留在 Git 历史中。

## 版本规则

- 每轮发布完成后，同组所有仓库的 `debian/changelog` 版本须与 `groups.yaml` 中该组的 `version` 一致。
- 组内任一仓库有更新，整组一起升版本、重新打包。
- 统一发版沿用 `debian/control` 中现有的依赖下界，不修改 `>=` / `>>` 约束。

## 仓库文件

- [groups.yaml](./groups.yaml)：分组、成员、仓库名映射和当前版本，发版以此文件为准。
- [release-group.yml](./.github/workflows/release-group.yml)：创建本组的基线草稿 PR，再触发成员工作流。
- [sync-baseline.yml](./.github/workflows/sync-baseline.yml)：检查成员发布进度，更新基线 PR 的检查状态。

## 发版

整组发版从本仓库发起，由各成员工作流更新 changelog。不要单独到成员仓库修改版本。

打开 `Actions → Release group → Run workflow`，填写三个参数：

- `group`：要发布的组。
- `version`：目标版本，须高于基线和成员当前版本。继续未完成的发布时，填写原目标版本。
- `dry_run`：默认 `true`，检查当前状态并列出需要触发的成员；设为 `false` 后才创建 PR、触发工作流。

新一轮发布按以下顺序执行：

1. 检查目标版本和已有发布记录，确认本组没有未完成的发布，成员当前版本与基线一致。
2. 创建 `baseline/<组>` 分支和草稿 PR，只修改本组的版本。新基线随该 PR 合入默认分支后生效。
3. 并行触发成员工作流，由各仓库生成 changelog 并创建发布 PR。
4. 所有成员 PR 合并、changelog 版本均为目标版本后，`group-release-ready` 检查通过，基线草稿 PR 转为待审。
5. 人工审查并合并基线 PR，完成本轮发布，之后才能开始下一轮。

同一组一次只能发布一个目标版本，不同组可以同时发布。成员陆续合入时，组内版本可能暂时不同。
创建发布分支遇到冲突时，流程会报错停止，不会覆盖已有分支。

### 成员工作流

- G1（DTK）：调用 `call-auto-release.yml`，dtkcore 使用 `auto-release.yml`，对应 `Auto Release` 工作流。
- 其他组：调用 `call-update-changelog.yml`，对应 `Call update changelog` 工作流，见 [linuxdeepin/.github PR #790](https://github.com/linuxdeepin/.github/pull/790)。

新增成员前，先在成员仓库的默认分支接入对应工作流。工作流按上述文件名调用，文件名变更时要同步修改本仓库的调用配置。

### 更新 PR 分支

发布完成前，不要更改本组成员、各仓库的默认分支或 PR 目标分支。

基线 PR 落后默认分支时，使用 `Update branch` 的 merge 方式同步，保留最初的发布提交。
同步后的 PR 相对默认分支仍应只修改本组版本。不要对基线分支做 rebase 或强推；基线 PR 不支持 merge queue。

普通文档或代码 PR 在合并前需同步到最新默认分支，并重新通过检查。

## 发布检查与恢复

`Sync baseline` 每 15 分钟检查一次，也可以手动运行。它读取本轮成员 PR 的合并记录和默认分支的 changelog，
把检查结果写入基线 PR 的 `group-release-ready` 状态。若成员版本变化却没有对应的发布记录，工作流会报错，需先核实已有发布。

触发步骤成功只说明 GitHub 已接受请求，发布进度以成员运行结果和基线 PR 的检查状态为准。
某个成员触发失败时，其他成员仍会继续，整次 `Release group` 运行会标为失败。

恢复时，重新运行 `Release group`，填写原来的组和目标版本，将 `dry_run` 设为 `false`：

- 已合并、发布 PR 仍打开、工作流仍运行的成员会被跳过。
- 没有发布 PR，也没有正在运行的工作流的成员会重新触发。
- 基线分支已创建但草稿 PR 创建失败的，会从原始提交创建 PR。
- 基线 PR 误关闭的，会重新打开原 PR。成员 PR 已关闭或版本不符时，需先人工修复原成员 PR。

如果触发时网络超时，先查看成员仓库的 Actions 运行记录，再决定是否重试。请求可能已经成功提交，
重复运行仍可能再次触发成员工作流。本流程不会撤回已提交的请求。

关闭 PR 或删除分支不能代替完成本轮发布；工作流会读取原始发布提交和历史 PR 来检查进度。
已有成员合入时，应继续完成原目标。基线 PR 若被提前合入，需要先人工完成原成员 PR；流程不会自动回退基线，
也不会再次触发成员工作流，完成前不能开始下一轮。

## 配置与上线

在 dde 仓库添加 secret `RELEASE_TOKEN`，使用个人访问令牌（PAT）并授予以下权限：

- dde：`contents: write`、`pull requests: write`、`commit statuses: write`。
- 成员仓库：`actions: write`、`contents: read`、`pull requests: read`。

成员仓库创建 changelog PR 所用的 GitHub App secret 沿用现有工作流配置。
本仓库的工作流统一使用 `RELEASE_TOKEN` 访问 GitHub；认证或查询失败时，运行会直接停止。

给 dde 默认分支启用保护规则，把 **`group-release-ready`** 设为必需检查，并禁止绕过。
配置时选择由 `RELEASE_TOKEN` 发布的提交状态。成员发布未完成时，保护规则会阻止基线 PR 合入。
普通 PR 同样需要通过此检查；手工修改基线版本的 PR 无法通过。

基线 PR 创建成功前，保留 `baseline/*` 分支。此时发布记录只在该分支中，删除后无法自动恢复。

首次使用前，先等待成员仓库已有的发版工作流结束，处理现有成员 PR、旧格式的 `baseline/<组>-<版本>` PR
和一次修改多组的基线 PR，确认成员实际版本与基线一致。go-lib 的 `call-update-changelog.yml` 需先合入其默认分支。

## 本地测试

```sh
python3 -I .github/tests/test_group_release.py -v
```

## 组件侧校验

成员仓库尚未接入统一的基线版本校验，成员 PR 目前不会自动比对本仓库的版本。
计划在 `linuxdeepin/.github` 提供复用工作流 `verify-group-version.yml`，通过 `workflow-templates/call-*` 模板接入。

## 分组

分组主要考虑循环依赖、构建和接口耦合，以及共同交付需求。

| 组 | 说明 | 成员 |
|---|---|---|
| G1 | DTK 基础库 | dtkcommon、dtk6log、dtklog、dtkcore、dtkgui、dtkwidget、dtkdeclarative、qt5platform-plugins、qt5integration |
| G2 | Go 服务基座 | go-lib、go-dbus-factory、dde-api |
| G3 | 会话核心与桌面应用 | dde-daemon、dde-services、dde-session、dde-session-shell-v25、dde-polkit-agent、dde-session-ui、dde-shell、dde-tray-loader、dde-application-manager、deepin-service-manager、dde-control-center、dde-launchpad、dde-clipboard、dde-network-core、deepin-update-ui、dcc-insider-plugin、xdg-desktop-portal-dde、deepin-pw-check、dde-app-services |
| G4 | 外观与主题 | dde-appearance、deepin-desktop-theme、deepin-face、deepin-screensaver |
| G5 | 图标主题 | deepin-icon-theme |

deepin-pw-check 的密码库由控制中心和会话 UI 链接使用；dde-app-services 提供桌面配置服务，两者随 G3 一起交付。

dde-dialog、dde-manual-content、gnome-keyring 不参与组版本统一，保留各自的版本。

具体成员和版本以 [groups.yaml](./groups.yaml) 为准。
