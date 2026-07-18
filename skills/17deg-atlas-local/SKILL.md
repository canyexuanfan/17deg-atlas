---
name: 17deg-atlas-local
description: 本地入口 Skill，用于在本人控制的持久本地环境中创建、连接、整理和检索个人知识库
---

# 17deg Atlas Local

仅在确认的持久本地环境中执行。环境不匹配时停止并提示使用远端入口。

## 首次连接

每次开始都先通过 `scripts/atlas.py` 进入工具；入口会核对并刷新项目运行时。若工具返回 `runtime-update-unverified`，不得继续使用来源不明的旧运行时，也不得宣布连接完成；应报告更新检查未完成并安全停止。

入口 Skill 默认安装在当前 Agent 官方支持的项目级隐藏 Skill 目录。Claude Code/Claudian 使用 `.claude/skills/17deg-atlas-local`，Codex 使用 `.codex/skills/17deg-atlas-local`；其他 Agent 只能使用其文档明确规定的项目级目录，不得自行发明 `.skills` 等目录。正式路径必须直接包含 `17deg-atlas-local/SKILL.md`，完整仓库的嵌套副本不能冒充安装完成。克隆源码只能放在项目 `.17deg-atlas/bootstrap-source` 等本地忽略目录，再把当前入口子目录复制到正式 Skill 目录。不得把 `SKILL.md`、`skill.md`、`atlas.py`、`17deg-atlas.py`、克隆源码或其他安装辅助文件放到用户素材根目录、`questions` 或任何知识目录。除非用户明确要求，不安装到全局 Skill 目录，也不把全局安装作为验收步骤。

首次安装或刷新 Skill 时，旧副本只能备份到 `.codex/backups/skills` 或项目 `.17deg-atlas/backups` 等本地忽略目录，不得备份到 `.codex/skills/_backups` 等 Agent 常规遍历可发现的位置。安装或刷新完成后必须验证正式 `SKILL.md` 直接存在、同名入口在整个工作区只有一个，并在运行 `workspace plan` 前确认素材根目录没有安装辅助文件。工具识别出的成套、未跟踪官方安装残留应可逆隔离到 `.17deg-atlas/install-residuals`，不得当成用户知识，也不得按文件名排除用户真实的同名资料。

1. 使用 `scripts/atlas.py workspace plan` 规划标准初始化步骤。
2. 将同一计划中需要用户决定的真实动作汇总为一份确认清单；平台要求逐项确认时遵循平台要求。
3. 确认后使用 `scripts/atlas.py workspace start` 继续同一份计划，直到工具返回完成结果。

所有步骤以 CLI 返回的 `terminal_state` 和 `next_action` 为准，不得用旧 Skill 文字覆盖新版工具状态。遇到 `needs-migration-repair` 时先运行 `workspace migration-repair-plan`，展示可恢复文件和需要确认的动作；确认后运行 `workspace migration-repair-start --confirm-migration-state-repair`。遇到 `needs-semantic-review` 时继续执行下方语义审核流程，不得重新运行普通首次连接来绕过。

工具返回多个连接或新建选项时，逐项标注现有内容去留与目标位置，等待用户选择；用户未明确选择前禁止新建、连接或迁移，保持现有内容不变。

未发现可复用仓库时，必须先向用户询问想使用的 GitHub 仓库名，并说明本地文件夹默认与该仓库名同名。`17deg-personal` 仅作为建议名；用户可输入任意合法名称。用户回答前不得创建仓库、不得进入迁移流程、不得首次同步。

## 处理已有候选材料

`workspace plan` 返回 `existing_materials.candidate_count>0` 时，按以下顺序处理；用户未做出关键选择前不得运行 `workspace start`。原始候选目录全程只读；排障记录、临时笔记、工具诊断只能写入目标实例的 `.17deg-atlas` 或 `.atlas` 本地忽略目录，禁止写回被迁移的源 `questions`、候选目录或任何知识目录。

1. 先向用户展示候选材料的分组、每组数量与少量样例，让用户选择处理方式：
   - `import-review`（推荐）：进入导入审核，逐批整理为正式知识候选；
   - `leave-in-place`：本次原地保留，不进入导入。
2. 选择 `import-review` 后，使用同一份计划运行：
   `workspace start --target <已选目标目录> --repository-name <已选仓库名> --existing-materials-action import-review --confirm-existing-materials-import`。
   工具返回 `needs-semantic-review` 后必须继续下面的导入审核流程；不得停止、不得宣称完成、不得直接触发首次同步。
   工具会在改动材料前创建只存在本机 Git 对象库中的原地址快照；该快照不进入新知识库分支，也不得推送到新仓库。
3. 按同类文件批量向用户询问 Agent 无法自行推断的字段：`authorship_status`、`origin_kind`、`intended_role`、`rights`、`access`、`wiki_compilation`（`compile`、`raw-only` 或 `route`）。不得猜测作者、权利或用途；任一字段保持 `unknown` 或未明确时工具会拒绝确认，须先向用户补问。
4. 用户给出答案后，立即用 `workspace confirm-review` 把该批决定绑定到对应文件哈希并取得机器确认回执：
   `workspace confirm-review --target <目录> --source-path <路径1> --source-path <路径2> ... --access ... --rights ... --origin-kind ... --authorship-status ... --intended-role ... --wiki-compilation <compile|raw-only|route> --confirm-semantic-decision`。
   工具返回的 `receipts[].confirmation_id` 与 `batch_confirmation_id` 是后续导入的强制凭据，必须逐文件记录；不得混用、不得在字段变化后继续使用旧回执。
5. 每份文本材料由 Agent 逐文件完整阅读原文后生成准确的来源摘要、原子卡片与主题页候选，再调用：
   `workspace import-review --target <目录> --source-path <路径> --confirmation-id <该文件对应回执ID> --access ... --rights ... --origin-kind ... --authorship-status ... --intended-role ...`。
   责任字段必须与 `confirm-review` 完全一致；工具会逐项比对，不一致即拒绝写入。`import-review` 不得使用其他文件的 `confirmation_id`。
6. 知识类候选（`intended_role` 为 `knowledge` 或 `evidence`、`wiki-compilation=compile`）的 `import-review` 必须附上可在原文与摘要/卡片中同时核验的 `--evidence` 引文：每条至少 12 个字符、必须逐字出现在该文件原文中、且同时出现在 `--summary`/`--card-question`/`--card-answer` 文本里；原文较长时至少 2 条，否则至少 1 条。禁止按文件名套通用模板或仅凭标题生成摘要；摘要、卡片、主题页必须基于该文件实际内容。`wiki-compilation=raw-only` 时需附带 `--confirm-raw-only`，无需 evidence。非文本材料先尝试提取文字；提取失败则保持待处理，不得强行生成摘要或卡片。
7. 非 knowledge 路由确认：若用户确认某份候选材料不属于知识库（`intended_role` 非 `knowledge`/`evidence`），在 `confirm-review` 中将 `wiki-compilation` 设为 `route`，再用对应 `confirmation_id` 调用 `workspace import-review --confirm-route-outside-knowledge`：保留原件、不强行并入 knowledge、不创建空目录，然后继续审核其余候选材料。
8. 源文件在 `confirm-review` 之后发生变化（哈希不一致、删除、新增或修改）时，原 `confirmation_id` 立即失效；`import-review` 与 `completion-audit` 都会检测并拒绝。必须重新进入导入审核：重新展示变更、重新征询用户决定、重新取得机器确认回执后才能继续，不得沿用旧回执、不得静默跳过。
9. 每份候选成功吸收后，工具必须删除其 `knowledge/inbox/migration/workspace` 临时副本；长期只保留 Raw 与用户尚未处理的原地址文件。全部候选完成后，工具返回 `needs-source-materials-selection`，此时先运行 `workspace source-plan --target <目录>`，询问用户保留还是删除原地址文件。默认保留；删除时必须展示精确源根目录并取得明确确认，再运行 `workspace source-start --action delete --expected-source-root <精确源根目录> --confirm-delete`。选择保留也必须用 `workspace source-start --action preserve` 记录结果，不能由 Agent 默认为已选择。
10. `workspace source-start` 返回 `ready-for-initial-sync` 后，使用同一目标和用户已选的 GitHub 仓库重新运行 `workspace start`，在得到对应确认后完成首次同步。同步成功后、向用户报告完成之前，必须立即运行 `workspace completion-audit --target <目录>`。只有返回 `terminal_state=complete` 才可宣布完成；返回 `needs-action` 时按 `issues` 逐项修复（补审核、清理 inbox 暂存、补原地址去留选择、清理 runtime 安装残留、提交并同步 Git 等），再次同步并重新审计直至通过。
11. 完成审计后不得再写入源候选目录、迁移待整理区或目标 Git 工作树；如需补充整理，重新进入 `import-review` 流程、重新同步并再次审计。

## 新实例的人类工作区

完成首次连接后，仅向用户解释以下四个目录：

- `knowledge/inbox`：临时待整理区。
- `knowledge/raw`：按来源类型自动整理的原始材料，保留原件。
- `knowledge/library`：经确认后可长期使用的正式知识。
- `knowledge/wiki`：Agent 生成的来源摘要、原子卡片、主题页等候选内容，需确认后才转为正式知识。

只解释上述四个当前工作目录。访问范围和生命周期属于文档属性，不是目录；除非用户主动追问，不展开其他实现细节或后续设想。

## 旧实例处理流程

发现旧实例时展示三项选择：迁移到当前结构（推荐）、继续旧结构、新建空实例（明确不复制旧内容）。

当用户选择迁移、但计划给出的旧源目录不存在时，先展示要克隆的旧仓库和本地目标路径，确认连接现有仓库后运行 `workspace migration-source`；该命令仅准备迁移源，不得创建密钥、改写或删除远端。

迁移不是复制旧文件夹。结构化旧知识按映射并入新结构；非结构化旧文档先进入迁移待整理区。

### 迁移路径

1. 选"迁移"后先运行 `workspace migration-plan`，向用户展示：复制范围、凭据转移、模板保留、排除项。
2. 当 `workspace migration-plan` 返回 `missing_identity_tiers` 时，先向用户请求对应分区可用的 identity 或安全位置，全程不得回显任何私钥内容；补齐前严禁执行迁移完成、首次同步或退役。
3. 确认内容迁移；若计划要求，另行确认本地凭据转移。
4. 运行 `workspace migration-start`。
5. 当 `workspace migration-start` 返回 `needs-semantic-review` 时：
   - 先运行 `workspace start --no-initial-sync` 准备本地运行环境，不得触发首次同步；
   - 对迁移待整理区中的每份旧文档，批量询问会改变处理结果的信息（作者/AI 参与、用途、权利、安全级别等），不得仅复制内容；
   - 每份旧文档保留原件并建立 `raw` 对象，再由 Agent 生成有来源关系的来源摘要、原子卡片、主题页（候选 Wiki 条目），并更新索引；
   - 派生内容默认保持候选状态，不得覆盖或删除原件；
   - 每完成一份，使用 `workspace migration-review` 登记 raw 与 Wiki 编译的验证结果；
   - 全部旧文档完成 raw 与 Wiki 编译并登记验证前，不得首次同步、不得宣称迁移完成。
6. 全部语义审核登记完成并通过后，才可运行首次同步。

旧回执若声称已经验证，但存在待迁移文档且 `objects_checked=0`、缺少语义候选或编译回执，必须视为旧版假完成并进入 `migration-repair-plan`；不得信任旧 `verified` 字段。

不得仅凭 `verified=true` 提前结束迁移、提前首次同步或提前退役。

### 退役路径

首次同步成功后必须运行 `workspace retirement-plan`，向用户显示保留、归档、删除旧本地实例和旧远端仓库的选择，默认保留。

- 未明确选择，不得运行 `workspace retirement-start`。
- 删除旧本地实例目录、删除旧远端仓库时，分别显示精确目标、分别复述、分别确认。
- 删除前必须由工具生成并验证 Git 历史备份。

不得仅凭 `onboarding_complete=true` 宣布迁移流程完成。只有 `workspace retirement-start` 返回 `terminal_state=complete` 时，才能向用户报告整个迁移流程完成并停止；不得自行追加 `doctor`、再次连接、全局安装或其他验收动作。

操作确认只暂停对应真实动作，不改变用户目标。不得因为 GitHub 操作需要确认而静默改用 `knowledge agent-local-setup` 或其他纯本地流程；只有用户明确要求"仅保存在本机、不连接 GitHub"时才能切换为纯本地初始化。

GitHub 网页授权必须由用户本人完成。当工具返回 `terminal_state=needs-user-github-authorization` 时，向用户清楚展示 `user_action.verification_uri` 和 `user_action.device_code`，然后立即暂停，等待用户回复已经完成。禁止 Agent 代替用户输入设备码、点击授权页面或关闭浏览器，禁止以任何方式自动操作浏览器，也禁止再次启动授权。用户确认完成后，只重新运行原连接流程检查状态；若返回 `github-authorization-failed`，先说明 `failure_reason` 并重新取得用户确认，只有确认后才可加入 `--confirm-github-login-retry` 发起一次新授权。

## 使用方式

日常知识操作统一通过 `scripts/atlas.py knowledge` 执行。Agent 使用以下最小路由：

- `knowledge trusted-build`：在当前授权范围内构建可信目录；
- `knowledge trusted-search`：检索当前可见内容；
- `knowledge trusted-trace`：核对结果来源；
- `knowledge trusted-evaluate`：评测检索结果；
- `knowledge lock`：结束使用后清理受控本地投影。

用户只需描述目标，Agent 将自动匹配相应能力：

- **保存资料**：收集来源、提取内容、整理候选、验证质量。
- **检索资料**：构建可信目录、搜索内容、核对来源、评估质量。
- **整理知识**：生成候选摘要、卡片、主题页与分类提案。
- **审核晋升**：确认候选内容、分类或关联是否进入正式知识。
- **记录使用**：记录真实使用结果和反馈。

所有自动整理结果默认保持候选状态，需用户确认后才成为正式知识。

## 安全边界

涉及网络安装、GitHub 操作、敏感凭据、公开、降密或删除的操作，必须在执行前获得明确确认。

面向用户的说明只解释当前可用功能与必要的确认动作。

排障记录、Agent 诊断与运行时残留只能写入目标实例的 `.17deg-atlas`、`.atlas` 等本地忽略目录；禁止写回被迁移的源 `questions`、候选目录或其他知识目录。README、提交标题、提交正文、公开索引、帮助文字等对外文案只描述当前可用功能与必要确认动作；不得包含无关内部结构、敏感凭据细节、测试对象数量，也不得把本地开发过程或安全操作历史直接发布到公开仓库。
