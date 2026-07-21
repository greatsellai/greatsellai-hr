# GreatSell HR 团队共建工作流

本工作流同时适用于人类开发者和 Codex。目标是让任何同事在任何电脑或新会话中，都先
从 GitHub 获得同一份当前事实，再开始修改。

## 1. 每次开工检查

先执行：

```bash
git status -sb
git remote -v
git fetch origin --prune --tags
git rev-list --left-right --count HEAD...origin/main
git log --oneline --decorate -8 origin/main
git log --oneline HEAD..origin/main
gh pr status
gh pr list --state open --limit 20
```

这些命令分别回答：本地有没有同事改动、连接的是不是正确仓库、本地与 `main` 相差多少、
最近 GitHub 改了什么、当前是否存在重叠 PR。不能用昨天的聊天记录替代这些检查。
如果 fetch 或 GitHub 查询失败，只能把本地结果标为“未验证”，不能继续共享写入或声称
本地已经是最新版本。

开工报告至少包含四项：`origin/main` 提交短码、本地领先/落后数量、工作区是否干净、
开放或重叠 PR；然后说明本次使用的分支或 worktree。

### 干净 main 仅落后远端

```bash
git pull --ff-only origin main
git switch -c agent/<task-name>
```

### 工作区已有修改

不要 `git reset --hard`、`git checkout -- .`、自动 stash 或把未知修改一起提交。先通过
`git diff --stat` 和 `git diff` 确认归属；本任务与现有修改无关时，从最新
`origin/main` 创建独立 worktree：

```bash
git worktree add ../greatsell-hr-<task-name> -b agent/<task-name> origin/main
```

### 正在继续已有 PR

```bash
git fetch origin --prune --tags
gh pr view <PR号> --comments
gh pr checks <PR号>
git diff --check origin/main...HEAD
```

若 `main` 已前进，先理解新提交，再选择 merge 或 rebase。解决冲突后必须重跑测试；
重写远端功能分支时只允许：

```bash
git push --force-with-lease origin <branch>
```

## 2. 实施和同步节奏

1. 从最新 `main` 建分支，先确认任务范围和不会覆盖的同事修改。
2. 完成一个可验证步骤后检查 `git diff --check` 和 `git status -sb`。
3. 运行该步骤对应的最小测试，提交后立即推送 GitHub。
4. 功能完成后运行完整后端测试与前端生产构建；迁移还要执行升级验证。
5. 创建 PR，写明做了什么、为什么、数据/隐私影响、兼容性和验证命令。
6. PR 出现新评论、冲突或 `main` 新提交时，重新执行开工检查，不沿用旧状态。
7. 创建 PR、准备合并和创建生产标签前分别再次 fetch，避免长任务期间基线漂移。

推荐提交粒度是“一次提交解决一个可解释问题”。不要把不同功能、格式化和同事改动混在
同一个提交中。

## 3. 同事之间如何拉齐

新同事或新的 Codex 会话只需收到仓库地址和以下指令：

> 先完整遵守仓库根目录 `AGENTS.md`，从 GitHub 最新 `main` 开始；检查开放 PR，保护
> 本地和同事未提交修改。所有共享成果通过功能分支和 PR，同步后回报分支、提交、PR、
> 测试结果和待办，不直接修改服务器业务代码。

交接时至少提供：

- GitHub 仓库和目标 PR 链接。
- 分支名、最新提交号、基于哪个 `main` 提交。
- 已完成/未完成范围和相关文档。
- 已运行的测试及结果。
- 是否有迁移、生产影响、未解决冲突或需要用户决策的问题。

接手者不得只相信交接文字；仍要 fetch 并用 GitHub 当前状态复核。

## 4. PR 与共同账号

共同使用一个 GitHub 账号不会自然产生独立的审核身份，因此不能把“按钮能点”当作审核
证据。至少要在合并前完成：

- PR diff 范围审查，没有夹带其他任务。
- 后端测试、前端构建及必要迁移验证通过。
- 对数据隔离、隐私、认证和不可逆迁移进行专项检查。
- 在 PR 描述或评论记录审核结论、验证结果和遗留风险。

若需要可审计的双人审批，应为同事建立独立 GitHub 身份；在此之前，共同账号只能依靠
明确的人工复核记录，不能伪装成两个独立审批人。

## 5. 发布与回滚

服务器 `/home/ubuntu/resume-screening-v3` 只接收已合并并带生产标签的源码。标准顺序是：

1. PR 合并到 `main`。
2. `main` 的四项 CI 检查全部成功。
3. GitHub Actions 自动创建不可变生产标签并部署该提交。

需要重试或回滚时，使用 GitHub Actions 中的 **Production deploy** 与
**Production rollback**；本地 `scripts/create-production-tag.sh` 和
`scripts/deploy-production.sh` 仅用于受控应急恢复，完整规则见 `docs/CI_CD.md`。

部署脚本不传输或删除 `.env.production`、数据库、候选人 PDF 和 Docker 卷。迁移发布前
必须完成备份；上线后验证健康、HTTPS、登录、关键 API 和 PDF 鉴权。回滚操作见
`docs/DEPLOYMENT.md`，只能指向已有 `prod-*` 标签。

## 6. 不能进入 GitHub 的内容

- `.env.production` 及任何真实环境变量值。
- API Key、邮箱密码、管理口令、数据库密码、SSH 私钥。
- 候选人 PDF、简历正文、个人身份信息和生产数据库导出。
- 服务器部署目录中的数据文件、Docker 卷内容和临时备份。

发现上述内容时停止提交，先从待提交范围中移除并报告；不得为了“同步方便”降低安全边界。
