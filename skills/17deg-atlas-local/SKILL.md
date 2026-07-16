---
name: 17deg-atlas-local
description: 本地入口 Skill，用于在本人控制的持久本地环境中创建、连接、整理和检索个人知识库
---

# 17deg Atlas Local

仅在确认的持久本地环境中执行。环境不匹配时停止并提示使用远端入口。

## 首次连接

必要时运行 `scripts/bootstrap.py` 完成环境初始化。

入口 Skill 默认安装在当前项目内。除非用户明确要求，不安装到全局 Skill 目录，也不把全局安装作为验收步骤。

1. 使用 `scripts/atlas.py workspace plan` 规划标准初始化步骤。
2. 将同一计划中需要用户决定的真实动作汇总为一份确认清单；平台要求逐项确认时遵循平台要求。
3. 确认后使用 `scripts/atlas.py workspace start` 继续同一份计划，直到工具返回完成结果。

工具返回多个连接或新建选项时，逐项标注现有内容去留与目标位置，等待用户选择；用户未明确选择前禁止新建、连接或迁移，保持现有内容不变。

## 旧实例处理流程

发现旧实例时展示三项选择：迁移到当前结构（推荐）、继续旧结构、新建空实例（明确不复制旧内容）。

当用户选择迁移、但计划给出的旧源目录不存在时，先展示要克隆的旧仓库和本地目标路径，确认连接现有仓库后运行 `workspace migration-source`；该命令仅准备迁移源，不得创建密钥、改写或删除远端。

### 迁移路径

1. 选“迁移”后先运行 `workspace migration-plan`，向用户展示：复制范围、凭据转移、模板保留、排除项。
2. 当 `workspace migration-plan` 返回 `missing_identity_tiers` 时，先向用户请求对应分区可用的 identity 或安全位置，全程不得回显任何私钥内容；补齐前严禁执行迁移完成、首次同步或退役。
3. 确认内容迁移；若计划要求，另行确认本地凭据转移。
4. 运行 `workspace migration-start`。
5. 仅当迁移返回 `verified=true` 后，运行 `workspace start` 创建或连接新仓库并首次同步。

### 退役路径

迁移和首次连接都完成后运行 `workspace retirement-plan`，展示保留、归档、删除三项，默认保留。

- 未明确选择，不得运行 `workspace retirement-start`。
- 删除旧本地实例目录、删除旧远端仓库时，分别显示精确目标、分别复述、分别确认。
- 删除前必须由工具生成并验证 Git 历史备份。

当结果同时包含 `onboarding_complete: true` 和 `terminal_state: complete` 时，以此作为首次连接的权威完成结果，向用户报告成功并停止。不得自行追加 `doctor`、再次连接、全局安装或其他验收动作。

操作确认只暂停对应真实动作，不改变用户目标。不得因为 GitHub 操作需要确认而静默改用 `knowledge agent-local-setup` 或其他纯本地流程；只有用户明确要求“仅保存在本机、不连接 GitHub”时才能切换为纯本地初始化。

## 使用方式

日常知识操作统一通过 `scripts/atlas.py knowledge` 执行。

Agent 使用以下最小路由：

- `knowledge trusted-build`：构建当前授权范围内的可信目录；
- `knowledge trusted-search`：检索可见内容；
- `knowledge trusted-trace`：核对结果来源；
- `knowledge trusted-evaluate`：评测检索结果；
- `knowledge lock`：结束后清理受控本地投影。

用户只需描述目标，Agent 将自动匹配相应能力：

- **保存资料**：收集来源、提取内容、整理候选、验证质量
- **检索资料**：构建可信目录、搜索内容、核对来源、评估质量
- **整理知识**：生成知识候选、分类提案、审核包
- **审核晋升**：审核候选知识、分类体系或关联关系
- **记录使用**：记录真实使用结果和反馈

所有自动整理结果保持候选状态，需用户确认后才能形成正式知识。

## 安全边界

涉及网络安装、GitHub 操作、敏感凭据、公开、降密或删除的操作，必须在执行前获得明确确认。
