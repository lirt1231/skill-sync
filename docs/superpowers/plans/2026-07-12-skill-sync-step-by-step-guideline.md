# Skill Sync 分步实施指南

## 1. 使用方式

本文档把平台路线图拆成可以逐项执行和验收的工程步骤。实施时必须按
顺序推进；前一步未通过验收，不开始下一步。

每一个 commit 遵循同一节奏；一个 Step 通常由多个 commit 完成：

1. 确认当前工作区和真实机器状态。
2. 先补测试，证明当前缺少目标能力。
3. 实现当前 commit 声明的最小闭环。
4. 跑单元测试、集成测试和必要的真实机器冒烟测试。
5. 只更新与当前 commit 行为直接相关的 README、设计文档和命令帮助。
6. 检查迁移和回退路径。
7. 对照本文的 commit map，确认没有混入下一个 commit 的内容。
8. 等待用户确认是否创建 Git commit。
9. 未得到用户明确指令，不执行 push。

不要在一个步骤中顺便实现后续功能。尤其不要同时修改 Agent 身份模型、
链接目标、Git 同步协议和 Web UI 数据结构。

### Commit 执行契约

1. 一个 commit 只对应下文 commit map 中的一个编号，例如 `5.2`。
2. 实现、该实现的测试和直接相关的文档必须放在同一个 commit；不允许先提交
   无测试实现，再用后续 commit 补测试。
3. 数据模型、CLI wiring、核心 mutation、Web UI 和真实迁移默认拆开提交。
4. 纯重构和行为变化默认拆开；如果行为变化必须依赖重构，先提交保持行为不变
   且全量测试通过的重构。
5. 每个 commit 必须能够独立 checkout、运行测试并保持已有命令可用；中间
   commit 不得依赖“下一个 commit 会修好”。
6. commit message 使用动词开头并描述一个结果，例如：
   `add edit session metadata store`，不使用 `misc changes`、`wip` 或
   `continue roadmap`。
7. 开始下一个 commit 前必须满足：当前 commit 的定向测试通过、全量测试通过、
   `git diff --check` 通过、工作区干净。
8. 发现本 commit 之外的问题时先记录到 roadmap/issue，不顺手修复；只有阻断
   当前验收的安全问题可以纳入，并需在交付说明中明确原因。
9. commit 和 push 仍是独立动作。用户允许 commit 不等于允许 push；不得自动
   push。

历史基线：Step 0–3 的 client-aware foundation 已落在 `33e5a6d`，Step 4
只读 Deployment Store 已落在 `6c670b7`。从 Step 5 开始严格使用下文的
commit 级拆分，不再以“一个 Step 一个大 commit”为执行单位。

## 2. 全程不变的安全规则

- `~/.agents` 是用户拥有的源数据范围。
- Agent 客户端中的真实目录绝不被静默覆盖。
- Windows 使用目录符号链接或 junction，不使用 `.lnk`。
- 任何冲突都不自动选择本地或远端版本。
- `edit apply`、Git commit 和 Git push 是三个独立动作。
- 用户要求修改 Skill 不等于授权 push。
- 普通 Web UI 刷新不访问网络。
- 生成目录、缓存、绝对机器路径和凭据不进入 Skill 数据仓库。
- 每个迁移步骤都必须支持预览，并且可从备份恢复。

## 3. Step 0：冻结并验收当前 V2 基线

### 目标

先把当前已有功能形成稳定基线，避免在未完成的 Web UI 和 Kimi 合并改动上
继续叠加架构变化。

### 任务

1. 盘点当前未提交文件，标记每个文件属于：
   - Kimi family 合并；
   - Web UI 性能和视觉改进；
   - `pipx` 打包；
   - 平台路线图；
   - 用户已有或来源不明的修改。
2. 不修改来源不明的文件，尤其是当前工作区中的 `design-qa.md` 和 Web UI
   资源，除非确认其归属。
3. 核对 README 描述的命令都真实存在。
4. 核对 CLI、Web API、测试和文档对 Agent 名称的使用一致：
   `codex`、`workbuddy`、`kimi`、`claude`。
5. 验证当前实际链接：Codex、WorkBuddy、Kimi Code、Kimi Desktop、Claude。
6. 验证 `pipx` 构建包含 Web UI 静态资源。

### 测试

```bash
python -m unittest discover -s tests
git diff --check
./skill-sync doctor --json
```

另做一次临时虚拟环境安装测试：

```bash
pip install --no-deps <local-repo>
skill-sync --help
```

### 完成标准

- 全量测试通过。
- `doctor` 没有未解释的问题。
- README 不引用不存在的命令。
- 当前真实 Agent 链接状态已记录。
- 工作区每项修改的归属清楚。
- 是否 commit 由用户单独决定；不 push。

## 4. Step 1：建立稳定的数据契约和版本能力

### 目标

在增加新命令前，先固定 JSON、配置和本地状态的兼容边界。

### 任务

1. 给包增加统一版本来源，避免 `pyproject.toml` 和 Python 常量分别维护。
2. 增加：

   ```bash
   skill-sync version
   skill-sync version --json
   ```

3. 定义所有机器可读输出的公共字段：
   - `schema_version`；
   - `command`；
   - `ok`；
   - `result`；
   - `warnings`；
   - `errors`。
4. 定义本地 config schema 和 portable registry schema 的升级规则。
5. 所有新增 JSON 字段遵循只增不删原则；破坏性变化必须升级
   `schema_version`。
6. 定义稳定退出码：
   - `0`：命令成功完成；
   - `1`：业务操作失败；
   - `2`：参数或配置错误；
   - `3`：冲突，需要用户决策；
   - `4`：安全检查阻止操作。

### 测试

- CLI 文本输出快照测试。
- JSON schema 兼容测试。
- 旧 config 和 registry fixture 读取测试。
- Python 3.10、3.11、3.12 的最小兼容测试。

### 完成标准

- Agent 可以稳定解析命令结果。
- 旧配置无需写回即可读取。
- 新版本不会因为仅新增字段而破坏旧管理 Skill。

## 5. Step 2：拆分 Agent Family 和 Agent Client

### 目标

保留用户看到的 Kimi 家族，同时让后台独立识别 Kimi Code 和 Kimi
Desktop，为具体客户端变体和 ownership 查询打基础。

### 任务

1. 引入两个稳定类型：
   - `AgentFamily`；
   - `AgentClient`。
2. 初始 client ID：
   - `codex`；
   - `workbuddy`；
   - `kimi-code`；
   - `kimi-desktop`；
   - `claude-code`。
3. 每个 client adapter 声明：
   - family；
   - display name；
   - 检测规则；
   - Skill 目录；
   - 当前链接能力。
4. CLI 继续允许用户使用 family：

   ```bash
   skill-sync link --agent kimi
   ```

5. 内部将 family 展开成检测到的 clients。
6. `doctor --json` 同时返回 family 汇总和 client 明细。
7. 保持 registry v2 中 `targets: ...,kimi,...` 不变。
8. 旧的 `kimi-code,kimi-desktop` target 值继续迁移为 `kimi` family。

### 测试

- 只安装 Kimi Code。
- 只安装 Kimi Desktop。
- 同时安装两者。
- 一个 client linked、另一个 missing 时 family 为 `partial`。
- family enable/disable 正确作用于两个 clients。
- macOS 带空格路径和 Windows home 路径。

### 完成标准

- UI 仍可显示一个 Kimi 分组。
- 后台状态能精确指出是哪一个 Kimi client 出问题。
- 现有 registry 和 CLI 命令行为不变。

## 6. Step 3：实现 `managed check`，暂不改变链接模型

### 目标

先让 Agent 能可靠查询一个实际路径是否被 Skill Sync 管理，再改变部署方式。

### CLI

```bash
skill-sync managed check <path-or-name> [--client id] [--json]
skill-sync managed list [--client id] [--json]
```

### 实现顺序

1. 新建 ownership/inspection 模块，不把判断散落在 CLI handler 中。
2. 输入优先使用绝对路径；名称查询只作为便利功能。
3. 支持输入：
   - Skill 根目录；
   - `SKILL.md`；
   - Skill 内任意子文件；
   - Agent 目录中的符号链接；
   - Windows junction；
   - canonical source；
   - 错误链接和损坏链接。
4. 从输入文件向上寻找 Skill 边界，但不得越过已知 Agent Skill 根或
   `~/.agents/skills` 根。
5. 结合 config、registry、canonical path、Agent adapter 和 `samefile`
   判断 ownership。
6. 现阶段尚无 rendered provenance，因此输出中明确：

   ```json
   {
     "role": "direct-source-link",
     "migration_required": true
   }
   ```

7. 将 ownership 和 health 分开：损坏的托管链接仍是 `managed=true`、
   `healthy=false`。
8. 查询成功但 unmanaged 时退出码为 `0`，由调用方读取 `managed`；无法
   判断或配置错误时非零退出。
9. 查询完全本地执行，不 fetch Git。

### 必测场景

- 通过 Codex 链接查询。
- 通过 WorkBuddy 链接查询。
- 通过 Kimi Code/Desktop 查询同一 Skill。
- 通过 canonical `SKILL.md` 查询。
- 通过 `scripts/foo.py` 查询。
- 项目中存在同名但未托管的 Skill。
- wrong-link、broken-link、真实目录冲突。
- Windows junction 的 `samefile` 判断。
- 传入不存在路径、不可访问路径和模糊名称。

### 完成标准

- Agent 能在写文件前可靠判断 ownership。
- 同名项目 Skill 不会被误判。
- 该步骤没有改变任何已有链接。
- `doctor` 可复用 ownership 模块，而不是维护第二套判断逻辑。

## 7. Step 4：建立统一的只读 Deployment Store

### 目标

让所有托管 Agent 链接脱离可编辑 canonical source。即使没有变体，也链接
到可验证的只读部署快照。

### 目录

```text
<local-data>/skill-sync/rendered/
└── sha256-<resolution-hash>/
    └── <skill>/
        ├── SKILL.md
        └── .skill-sync-provenance.json
```

### 实现顺序

1. 实现 deterministic renderer，第一版只渲染 Base，不支持 Variant。
2. provenance 包含：
   - logical Skill name；
   - source hash；
   - resolution hash；
   - resolver version；
   - rendered hash；
   - target client；
   - applied layers，当前只有 `base`。
3. 在同一父目录创建临时输出，完成后原子 rename。
4. 渲染目录设为只读；Windows 同时使用合理的 read-only/ACL guardrail，
   但完整性仍依赖哈希验证。
5. Agent 链接指向 rendered deployment。
6. link state 增加：
   - `stale-render`；
   - `tampered-render`；
   - `missing-render`。
7. `managed check` 能从 rendered path/provenance 反查 canonical source。
8. 实现只删除未被任何客户端引用的缓存清理。

### 迁移命令

```bash
skill-sync deploy preview
skill-sync deploy migrate
skill-sync deploy status --json
```

迁移必须逐 Skill 预览：

```text
Codex: direct canonical link -> rendered deployment link
WorkBuddy: direct canonical link -> rendered deployment link
```

### 回退

迁移前保存链接清单。若迁移失败：

- 保留 canonical source；
- 恢复原有正确链接；
- 不删除尚未确认无引用的 rendered output。

### 必测场景

- Base 渲染哈希确定性。
- 隐藏文件和二进制文件。
- 拒绝 source 内 symlink。
- 临时构建失败和原子交换。
- 多个 clients 引用同一或不同 client deployment。
- rendered 文件被修改后识别为 `tampered-render`。
- Windows junction 指向 rendered deployment。

### 完成标准

- Codex 通过客户端 Skill 路径写文件时不能直接修改 canonical source。
- WorkBuddy 不会在 Codex 修改部署目录的过程中看到半成品。
- 所有托管客户端路径都能追溯到 canonical source 和 provenance。
- 用户仍可直接编辑 `~/.agents/skills`，但需运行 refresh/deploy 才发布。

## 8. Step 5：实现 Base-only Managed Edit Session

### 目标

先跑通“不带变体”的安全修改闭环，再增加 family/client scope。

### CLI

```bash
skill-sync edit begin <skill> --base [--actor client-id]
skill-sync edit list --json
skill-sync edit status <session-id> --json
skill-sync edit diff <session-id>
skill-sync edit validate <session-id>
skill-sync edit impact <session-id>
skill-sync edit apply <session-id>
skill-sync edit abort <session-id>
```

### 实现顺序

1. `begin`：
   - 读取 canonical baseline hash；
   - 创建 session ID；
   - 保存 baseline snapshot；
   - 创建 writable workspace；
   - 建立每个 logical Skill 一个 session 的本机锁；
   - 返回绝对 workspace path。
2. Agent 只修改 workspace。
3. `diff`：显示 workspace 与 baseline 的文件级和文本级差异。
4. `validate`：检查 `SKILL.md`、frontmatter、路径、安全 symlink 和内容哈希。
5. `impact`：列出哪些 clients 的 deployment 会改变。
6. `apply` 前重新计算 canonical hash；如果与 baseline 不同，返回 conflict。
7. `apply` 创建时间戳备份和 operation receipt。
8. 同父目录原子替换 canonical source。
9. 重建受影响 deployments 并验证链接。
10. `apply` 不执行 Git add、commit 或 push。
11. `abort` 只删除 session workspace，不动 canonical source。

### 恢复能力

实现：

```bash
skill-sync edit recover <skill> --client <id>
```

当 rendered deployment 被修改时：

1. 显示 tampered diff。
2. 用户可选择捕获到新的 Base edit session。
3. 或丢弃并从 canonical source 重建。
4. 不自动把 tampered 内容写入源。

### 必测场景

- 两个 Agent 同时开始编辑同一个 Skill。
- session 期间用户直接修改 canonical source。
- validation 失败。
- apply 中途进程退出。
- deployment rebuild 失败时 canonical 和链接恢复。
- abort、过期 session、恢复 session。
- operation receipt 不包含 secret 内容。

### 完成标准

- Agent 对托管 Skill 的修改都有 session、diff、validation、impact、backup
  和 receipt。
- apply 前其他 clients 不受影响。
- apply 后受影响 clients 得到完整的新 deployment。
- 没有任何隐式 push。

## 9. Step 6：升级 `skill-sync-manager` 并跑通真实 Agent

### 目标

CLI 能力存在后，再让 Codex、WorkBuddy 等 Agent 强制遵循新流程。

### 更新管理 Skill

在 `skill-sync-manager/SKILL.md` 中加入：

1. 修改任何 Skill 文件前，先解析实际目标路径。
2. 执行：

   ```bash
   skill-sync managed check <actual-path> --client <client-id> --json
   ```

3. `managed=true` 时禁止直接写目标路径和 canonical source。
4. 使用 `edit begin --base` 返回的 workspace。
5. 修改后依次执行：
   - `edit diff`；
   - `edit validate`；
   - `edit impact`；
   - `edit apply`。
6. `managed=false` 时不自动 import。
7. `ambiguous`、`wrong-link`、`tampered-render` 时停止并报告。
8. apply 不等于 push；没有用户明确指令时不 push。
9. 在 frontmatter 或 references 中记录所需最低 `skill-sync` 版本。

### 真实流程测试

分别让以下客户端修改一个临时托管 Skill：

- Codex；
- WorkBuddy；
- Claude Code；
- Kimi Code/Desktop 中可可靠测试的客户端。

验证：

- Agent 首先执行 ownership 查询。
- Agent 写入 session workspace，而不是客户端路径。
- apply 前其他客户端内容不变。
- apply 后 deployment 更新。
- Agent 不执行 push。

### 完成标准

- 至少 Codex 和 WorkBuddy 的真实流程跑通。
- 管理 Skill 不引用不存在的命令。
- 旧版 CLI 遇到新版管理 Skill 时能得到明确升级提示。

## 10. Step 7：实现 Variant Source 和 Resolver

### 目标

在安全编辑闭环稳定后，再加入 Base、Family、Client 三层适配。

### Source 结构

```text
~/.agents/
├── skills/<skill>/
└── variants/<skill>/
    ├── <family>/
    └── <client-id>/
```

### 任务顺序

1. 实现严格的 `variant.yaml` parser。
2. 支持 `mode: overlay`：add、replace、delete。
3. 禁止绝对路径、`..` traversal、未知字段和 symlink。
4. 实现解析优先级：

   ```text
   base -> family variant -> exact client variant
   ```

5. resolution hash 包含：
   - base hash；
   - family variant hash；
   - client variant hash；
   - resolver version；
   - target client ID。
6. CLI：

   ```bash
   skill-sync variant list
   skill-sync variant create <skill> --family <id>
   skill-sync variant create <skill> --client <id>
   skill-sync variant validate <skill>
   skill-sync resolve <skill> --client <id> --dry-run
   skill-sync diff <skill> --base --client <id>
   ```

7. 第一版不支持任意脚本 transformation，也不支持 variant 完整复制模式。

### 必测场景

- Base only。
- Base + family。
- Base + client。
- Base + family + client。
- `kimi-desktop` 覆盖 `kimi`。
- variant 删除 Base 文件。
- 同一 shared script 被多个客户端复用。
- 只有一个 client variant 改变时，其他 deployment hash 不变。

### 完成标准

- 一个 logical Skill 能生成不同客户端版本。
- 公共文件只需维护一次。
- 任意 deployment 都能解释应用了哪些 layers。

## 11. Step 8：扩展 Edit Session 到 Family 和 Client

### 目标

让 Agent 修改时能够选择影响范围，而不是把所有改动都写入 Base。

### CLI

```bash
skill-sync edit begin <skill> --base --actor codex
skill-sync edit begin <skill> --family kimi --actor kimi-desktop
skill-sync edit begin <skill> --client codex --actor codex
```

### 任务

1. session metadata 增加 target scope 和 layer baseline。
2. 如果目标 variant 不存在，创建最小 overlay，而不是复制完整 Base。
3. `diff` 同时显示：
   - source-layer diff；
   - 每个受影响 client 的 resolved diff。
4. `impact` 区分：
   - Base：所有目标 clients；
   - Family：该 family clients；
   - Client：一个 client。
5. `apply` 只替换目标 source layer。
6. 变体被清空时提示是否删除空 variant；不自动删除。
7. 管理 Skill 的 scope 选择规则：
   - 通用业务逻辑用 Base；
   - 产品家族差异用 Family；
   - 工具名、命令、路径、运行时差异用 Client；
   - 有实质歧义先询问用户。

### 完成标准

- Codex-specific 修改不会改变 WorkBuddy deployment。
- Kimi family 修改同时影响 Kimi Code/Desktop。
- Base 修改能预览所有客户端影响。

## 12. Step 9：Registry v3 和多设备 Variant 同步

### 目标

把 variants 和 target intent 安全同步到另一台机器，同时保持机器路径本地化。

### 任务

1. 定义 registry v3。
2. v2 继续只读兼容；首次创建 variant 时才写 v3。
3. Git 数据仓库包含：
   - base Skills；
   - variant sources；
   - portable registry。
4. 不包含：
   - rendered deployments；
   - edit sessions；
   - backups；
   - absolute paths；
   - credentials；
   - local adapter detection。
5. `preview/status/doctor` 分别显示 Base 和 Variant 变化。
6. 两台机器检测到不同客户端时，只构建本机需要的 deployments。
7. 同一个 Base 或 Variant 本地和远端同时改变时停止。
8. 保持 push 必须显式触发。

### 两机器集成矩阵

| Machine A | Machine B | 必测结果 |
| --- | --- | --- |
| Codex | WorkBuddy | 同步 Base，各自渲染 |
| Kimi Desktop | Kimi Code | family variant 一致 |
| Codex | Codex | client variant hash 一致 |
| 修改 Base | 修改同一 Base | conflict stop |
| 修改 Codex variant | 修改 Kimi variant | 可安全合并不同单元 |

### 完成标准

- 新机器仅通过 Git source 能重现相同 resolution hash。
- 不会把机器 A 的绝对路径带到机器 B。
- Variant 冲突不会破坏当前已部署版本。

## 13. Step 10：Web UI Ownership、Deployment 和 Edit Session

### 目标

先把已有 CLI 能力可视化，不在 UI 中发明另一套业务逻辑。

### 页面顺序

1. Skill Inventory：
   - managed/unmanaged；
   - source hash；
   - deployment 状态；
   - active session；
   - variants badges。
2. Family/Client Matrix：
   - Base、Family、Client resolution；
   - linked、missing、stale、tampered、conflict；
   - Kimi 分组下展示两个 clients。
3. Edit Session：
   - 选择 Base/Family/Client；
   - source diff；
   - resolved diff；
   - validation；
   - impact；
   - backup 后 apply；
   - abort/resume。
4. Tamper Recovery：
   - capture to session；
   - discard and rebuild。

### 性能约束

- 普通刷新不 fetch。
- 对未改变目录使用 hash cache。
- mutation 只刷新 affected Skills/clients。
- 100 个 Skill 的缓存刷新目标小于 300 ms。
- 网络操作只能由明确的 Sync 按钮触发。

### 完成标准

- UI 和 CLI 调用同一 core service。
- UI 不允许直接编辑 rendered deployment。
- 所有 mutation 显示 plan 和 result。

## 14. Step 11：自定义 Adapter 和少量新客户端

### 目标

先提供扩展能力，再有选择地增加经过验证的内置客户端。

### 顺序

1. 机器本地自定义 adapter：

   ```bash
   skill-sync agent add my-agent --skills-dir ~/.my-agent/skills
   skill-sync agent remove my-agent
   skill-sync agent list --json
   ```

2. portable adapter template 只允许 home-relative 或 env-based 路径。
3. adapter 声明 capability：
   - symlink；
   - junction；
   - copy-only；
   - global；
   - project；
   - variant。
4. 按真实验证顺序增加：
   - OpenCode；
   - Gemini CLI；
   - Cursor；
   - GitHub Copilot；
   - Windsurf。
5. 每个 adapter 必须有路径证据、检测 fixture 和 OS 测试。

### 完成标准

- 自定义绝对路径不进入 portable registry。
- 不支持链接的客户端在执行前就明确报告 capability。
- 不为了数量添加未经验证的 Agent 路径。

## 15. Step 12：Conflict Center、History 和 Rollback

### 目标

在同步和变体稳定后，完善人工冲突解决与恢复。

### 任务

1. 按 Base/Variant 单元展示冲突。
2. 文本 diff；二进制文件只展示 metadata/hash。
3. 提供：
   - keep local；
   - use remote；
   - keep both；
   - abort。
4. 每次选择前强制 backup。
5. 实现：

   ```bash
   skill-sync history [skill]
   skill-sync backup list [skill]
   skill-sync backup restore <id>
   skill-sync restore <skill> --revision <sha>
   ```

6. Git restore 创建 forward commit，不 reset/force push。
7. UI 展示 Git commit、device identity、backup 和 operation receipt。

### 完成标准

- 用户能从一次错误选择恢复。
- 一个 Variant 冲突不阻止读取其他健康 Skill。
- 未得到明确 push 指令时，解决冲突后仍只停留在本地。

## 16. Step 13：Secret Scan 和审计日志

### 目标

在平台被广泛使用前，降低 Agent 将凭据写入私人 Git 仓库的风险。

### 任务

1. 检测：
   - private keys；
   - 常见 API tokens；
   - 高熵值；
   - 敏感文件名；
   - 明文 credential patterns。
2. 输出只显示脱敏片段，不打印完整 secret。
3. 默认在 push 前扫描。
4. 高危 finding 阻止 push，直到逐项处理或明确 acknowledge。
5. acknowledge 规则可版本化，不提供全局永久关闭按钮。
6. activity log 记录：
   - actor；
   - command；
   - Skill/scope；
   - before/after hash；
   - backup ID；
   - result；
   - 不记录 Skill 正文和 secret。
7. diagnostic export 默认排除源文件和凭据。

### 完成标准

- 测试凭据能够被识别。
- 日志和 JSON 不泄露完整值。
- Secret Scan 不触发自动 push 或自动删除内容。

## 17. Step 14：Skill Inspection 和适配辅助

### 目标

最后增加提升使用效率的能力，不阻塞核心平台闭环。

### 任务

- Skill 详情和 Markdown preview。
- 文件树、source、variant、resolved provenance。
- Base 与 client 的兼容性检查。
- 检查客户端专属工具名、路径和命令假设。
- 给出“应该改 Base 还是 Variant”的建议。
- 可选标签、筛选和来源信息。

### 非目标

- 不做公共市场。
- 不做排行榜。
- 不自动重写 Skill。
- 不实现任意构建脚本。
- 不同步 MCP credentials 或 Agent memory。

## 18. 推荐执行批次

### Batch A：先解决当前安全问题

按顺序完成：

1. Step 0：V2 基线。
2. Step 1：数据契约。
3. Step 2：Family/Client。
4. Step 3：`managed check`。
5. Step 4：只读 Deployment。
6. Step 5：Base Edit Session。
7. Step 6：更新管理 Skill 并跑通 Codex/WorkBuddy。

Batch A 完成后，当前“Codex 修改软链接导致全局立即变化”的风险才算真正
解决。

### Batch B：实现多 Agent Client 适配

按顺序完成：

1. Step 7：Variant Resolver。
2. Step 8：Family/Client Edit Session。
3. Step 9：多设备 Variant 同步。
4. Step 10：Web UI。

Batch B 完成后，产品具备“多设备同步 + 多 Agent Client 适配管理”的核心
定位。

### Batch C：扩展和产品化

按顺序完成：

1. Step 11：Adapters。
2. Step 12：Conflict/History/Rollback。
3. Step 13：Secret/Audit。
4. Step 14：Inspection/Authoring Assistance。

## 18.1 后续 Commit Map

下面的编号是实施和提交边界，不是可以合并处理的建议清单。每完成一项就停在
干净工作区，报告测试结果并等待下一步。所有 commit 都必须带本项测试；表中
“验收”是除全量测试和 `git diff --check` 之外的额外门槛。

### Step 5：Base-only Managed Edit Session

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 5.1 | `add edit session metadata store` | session ID、状态机、metadata schema、本地目录布局、每 Skill 锁；不创建 workspace、不改 CLI | schema round-trip、损坏 metadata fail closed、锁并发测试 |
| 5.2 | `add edit session inspection commands` | `edit list/status` 只读 CLI 和 JSON envelope；不增加 mutation | 不存在/损坏/active session 输出契约测试 |
| 5.3 | `add edit session begin and abort` | Base session 的 baseline snapshot、writable workspace、`begin/abort`；不实现 apply | canonical 变化、重复 begin、abort 不动 source |
| 5.4 | `add edit diff and validation` | 文件级/文本级 diff、`SKILL.md`/frontmatter/path/symlink 校验 | binary、hidden、symlink、非法路径和空变更测试 |
| 5.5 | `add edit impact preview` | 根据当前 registry/client resolution 计算受影响 deployment；只读 | 多 client、disabled/undetected client、stale baseline 测试 |
| 5.6 | `add transactional base edit apply` | baseline conflict、backup、receipt、canonical 原子替换；暂不自动重建 deployment | apply 中断、canonical winner、rollback、receipt fsync 测试 |
| 5.7 | `rebuild deployments after edit apply` | apply 后定向重建、验证并切换 deployment；失败恢复 canonical 和链接 | Codex/WorkBuddy 多端一致性、链式链接和 Windows junction 测试 |
| 5.8 | `add managed edit recovery` | `edit recover`、tampered diff、capture/discard；不自动采用 tampered 内容 | tampered capture/discard、过期 session、recovery receipt 测试 |
| 5.9 | `document base managed edit workflow` | README、`--help`、完整 CLI e2e fixture；不新增 core 行为 | wheel 安装后所有 edit 命令可用，示例与 JSON golden 一致 |

### Step 6：管理 Skill 和真实 Agent 流程

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 6.1 | `require managed checks before skill edits` | 更新 `skill-sync-manager`：先 check，再 begin/diff/validate/impact/apply；记录最低 CLI 版本 | 管理 Skill 不引用不存在命令，旧 CLI 给出升级提示 |
| 6.2 | `verify managed edits from codex` | 只加入 Codex 真实流程 fixture/记录和必要兼容修复 | Codex 不写 deployment/canonical、不隐式 push |
| 6.3 | `verify managed edits from workbuddy` | 只加入 WorkBuddy 真实流程和必要兼容修复 | 与 Codex 相同的完整闭环 |
| 6.4 | `verify managed edits from remaining clients` | Claude、Kimi Code/Desktop 的兼容验证；独立 adapter bug 另拆 fix commit | 每个可测 client 有明确结果和限制说明 |

### Step 7：Variant Source 和 Resolver

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 7.1 | `add strict variant manifest parser` | `variant.yaml` schema/parser/path 安全；不解析内容 | unknown field、traversal、absolute path、symlink fail closed |
| 7.2 | `add deterministic variant overlay engine` | add/replace/delete overlay；不接 registry/CLI | Base+family+client 文件级 golden fixtures |
| 7.3 | `add layered resolution provenance` | resolver priority、layer hashes、resolution hash/provenance | 单层变化只改变受影响 client hash |
| 7.4 | `add variant source management commands` | `variant list/create/validate`；不增加 edit session scope | family/client ID、重复 create、空 variant 测试 |
| 7.5 | `add variant resolve and diff commands` | `resolve --dry-run`、Base/client diff 和 JSON 输出 | Kimi family/client 优先级、binary metadata diff |
| 7.6 | `document variant resolution model` | README、architecture、迁移限制；不新增行为 | wheel CLI help 与文档命令逐项核对 |

### Step 8：Family/Client Edit Session

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 8.1 | `add scoped edit session metadata` | session 增加 Base/Family/Client scope 和 layer baseline；不改变 begin | 旧 Base session 兼容读取、非法 scope fail closed |
| 8.2 | `add family and client edit begin` | scoped begin、缺失 variant 的最小 overlay workspace | 不复制完整 Base，Kimi family 展开正确 |
| 8.3 | `add scoped edit diff and impact` | source-layer diff、resolved diff、scope impact | Base/Family/Client 影响矩阵 golden tests |
| 8.4 | `add transactional scoped edit apply` | 只替换目标 layer并重建受影响 deployments | Codex-only 不改变 WorkBuddy，family 同时影响两个 Kimi |
| 8.5 | `teach manager skill to choose edit scope` | 管理 Skill scope 规则和真实 Agent 验证 | 有歧义时询问，不自动扩大影响范围 |

### Step 9：Registry v3 和多设备同步

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 9.1 | `add registry v3 variant schema` | v3 读取/写入、v2 只读兼容、首次 variant 升级 | v2 fixtures 不写回，v3 deterministic serialization |
| 9.2 | `sync portable variant sources` | Git 仓库打包 Base/Variant/registry；排除本机状态 | 无 absolute path、deployment、session、backup、credential |
| 9.3 | `add variant aware sync conflicts` | Base/Variant 单元级 preview/status/conflict stop | 不同 variant 可合并，同一单元双改停止 |
| 9.4 | `add multi device resolution fixtures` | 两机器检测矩阵和可重复 resolution hash 测试 | Codex/WorkBuddy、Kimi Code/Desktop、同 client 三组矩阵通过 |
| 9.5 | `document registry v3 migration` | 升级、回退、多设备操作文档；不新增行为 | 新旧机器流程可按文档复现 |

### Step 10：Web UI

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 10.1 | `add deployment and session web read models` | 复用 core 的只读 API；不改页面 | Web/CLI 同输入输出一致，普通请求不 fetch |
| 10.2 | `show managed skill inventory` | inventory、source hash、deployment/session/variant badges | 搜索、选择和空状态测试 |
| 10.3 | `show family client deployment matrix` | client matrix、Kimi 分组、stale/tampered/conflict 状态 | 具体 client 问题不被 family 汇总掩盖 |
| 10.4 | `add managed edit session interface` | begin/diff/validate/impact/apply/abort UI | mutation 都显示 plan/result，不能写 deployment |
| 10.5 | `add tamper recovery interface` | capture/discard recovery UI | 高风险动作确认、错误时保留恢复信息 |
| 10.6 | `cache web inventory hashes` | affected-only refresh 和 hash cache；不改变业务状态 | 100 Skill 缓存刷新目标小于 300 ms |

### Step 11：Adapter 扩展

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 11.1 | `add local custom adapter schema` | 本机 adapter 配置和 capability model；不加新客户端 | absolute local path 不进入 portable registry |
| 11.2 | `add custom adapter commands` | `agent add/remove/list` 和 JSON 契约 | home-relative/env path、重复 ID、非法 capability 测试 |
| 11.3 | `enforce adapter link capabilities` | symlink/junction/copy-only/global/project/variant 前置检查 | 不支持能力在 mutation 前失败 |
| 11.4 | `add opencode adapter` | 只增加 OpenCode adapter、证据和 fixtures | macOS/Windows 路径与检测测试 |
| 11.5 | `add gemini cli adapter` | 只增加 Gemini CLI adapter | 同上 |
| 11.6 | `add cursor adapter` | 只增加 Cursor adapter | 同上 |
| 11.7 | `add github copilot adapter` | 只增加 GitHub Copilot adapter | 同上 |
| 11.8 | `add windsurf adapter` | 只增加 Windsurf adapter | 同上 |

### Step 12：Conflict、History 和 Rollback

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 12.1 | `add backup and history inspection` | history/backup list 只读模型和 CLI | Base/Variant、缺失和损坏 backup 测试 |
| 12.2 | `add forward only backup restore` | backup restore/revision restore，生成 forward change；不 push | 禁止 reset/force push，恢复前强制 backup |
| 12.3 | `add conflict decision engine` | keep local/use remote/keep both/abort core plan；不加 UI | 每项选择可预览、可恢复、不自动 push |
| 12.4 | `add conflict resolution commands` | Conflict Center CLI 和 JSON receipt | binary 只显示 metadata/hash，文本 diff golden |
| 12.5 | `add conflict center interface` | UI 展示和调用同一 core plan | 一个冲突不阻止读取其他健康 Skill |

### Step 13：Secret Scan 和审计

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 13.1 | `add redacted secret scanner` | key/token/entropy/filename/pattern 检测与脱敏输出 | fixture 中完整 secret 不出现在日志/JSON |
| 13.2 | `block pushes with secret findings` | push 前 gate 和逐项 acknowledge；不自动删除 | 高危阻止 push、ack 可审计且无永久全局关闭 |
| 13.3 | `add privacy safe activity log` | actor/command/scope/hash/backup/result 日志 | 不记录正文、secret、credential |
| 13.4 | `add redacted diagnostic export` | 默认排除 source/credential 的诊断包 | 包内容 allowlist 测试 |

### Step 14：Inspection 和适配辅助

| Commit | 建议 message | 该 commit 只完成什么 | 额外验收 |
| --- | --- | --- | --- |
| 14.1 | `add skill inspection read model` | 文件树、source/variant/resolved provenance 只读 core/CLI | binary、large file、tampered source 安全输出 |
| 14.2 | `add skill detail interface` | Markdown preview、文件树和 provenance UI | 不执行 Markdown 脚本，不允许编辑 deployment |
| 14.3 | `add client compatibility checks` | 工具名、路径、命令假设检查 | 每条 finding 有证据和 client scope |
| 14.4 | `add base variant scope recommendations` | Base/Family/Client 建议；不自动重写 | 建议可解释，有歧义明确提示用户决策 |

## 18.2 并行开发与合并顺序

并行只表示可以在独立 branch/worktree 中同时开发，不表示可以跳过依赖或任意
顺序合并。`main` 始终按照 commit map 的依赖顺序接收 commit；后合并的分支必须
基于最新 `main` rebase，并重新执行定向测试、全量测试和 `git diff --check`。

### 并行执行规则

1. 一个 branch/worktree 只负责一个 commit ID，不在同一分支预做后续 commit。
2. 每个并行 commit 仍必须包含自己的实现、测试和直接相关文档，并能独立验收。
3. 共享 schema、公共 CLI parser、registry migration、transaction apply 和 recovery
   属于串行热点；依赖它们的分支不得自行复制或提前发明接口。
4. 并行分支可以提前开发，但只有全部前置 commit 已进入 `main` 后才能合并。
5. 合并顺序默认使用 commit ID 顺序；表中注明的 rebase 顺序优先于开发完成时间。
6. 发生语义冲突时停止合并并回到 roadmap 重新划分范围，不为了消除 Git 冲突而
   把两个 commit 压成一个 commit。
7. 常规并发控制在 2–3 个 worktree；只有互不共享实现文件的独立 adapter 才提高
   到 4 个并发。
8. commit、合并和 push 分别授权；并行开发不授权自动 commit、自动合并或自动
   push。

### 推荐并行批次

| 前置条件 | 可并行开发 | 合并要求 | 并行度 |
| --- | --- | --- | --- |
| `5.1` 完成 | `5.2`、`5.3` | 先合并 `5.2`，`5.3` rebase 后验收 | 中：可能同时修改 edit CLI |
| `5.3` 完成 | `5.4`、`5.5` | 按 ID 合并；`5.6` 等两者完成 | 高：diff/validation 与 impact 相对独立 |
| `6.1` 完成 | `6.2`、`6.3`、`6.4` | 各 client 兼容修复不得混入其他分支 | 高：真实 Agent 流程相互独立 |
| `7.1` 完成 | `7.2`、`7.4` | `7.2` 先进入 resolver 主链；`7.4` rebase 后合并 | 中：core overlay 与 source CLI 分离 |
| `10.1` 完成 | `10.2`、`10.3` | 拆分页面组件，按 ID 合并并重跑前端测试 | 中：业务独立但可能共享页面文件 |
| `11.3` 完成 | `11.4`–`11.8` | 每个 adapter 单独 commit，按 ID 合并 | 最高：Agent adapter 相互独立 |
| `12.1` 完成 | `12.2`、`12.3` | 按 ID 合并；`12.3` 不依赖 restore CLI | 中：restore 与 decision engine 分离 |
| `13.1` 完成 | `13.2`、`13.3` | 按 ID 合并并统一复核 redaction | 高：push gate 与 activity log 分离 |
| `14.1` 完成 | `14.2`、`14.3` | 按 ID 合并；`14.4` 等待 `14.3` | 高：详情 UI 与 compatibility core 分离 |

### 必须保持串行的关键链

```text
5.1
├── 5.2
└── 5.3
    ├── 5.4
    └── 5.5
        ↓
5.6 → 5.7 → 5.8 → 5.9 → 6.1

7.2 → 7.3 → 7.5 → 7.6
8.1 → 8.2 → 8.3 → 8.4 → 8.5
9.1 → 9.2 → 9.3 → 9.4 → 9.5
10.4 → 10.5
11.1 → 11.2 → 11.3
12.3 → 12.4 → 12.5
13.1 → 13.2
14.3 → 14.4
```

transaction apply、deployment rebuild、recovery、registry migration 和 scoped
edit apply 直接影响用户源数据或托管部署，不能通过并行开发扩大接口不确定性。

## 19. 每一步结束时的统一检查清单

```text
[ ] 新功能有失败测试和成功测试
[ ] python -m unittest discover -s tests 通过
[ ] git diff --check 通过
[ ] README 和 --help 与实现一致
[ ] JSON 输出有 schema/version 测试
[ ] 没有覆盖真实 Agent Skill 目录
[ ] Windows 路径和 junction 有测试
[ ] 普通 UI 刷新不访问网络
[ ] 没有隐式 Git commit
[ ] 没有隐式 Git push
[ ] 有迁移预览和失败回退
[ ] 真实机器 smoke test 与风险匹配
[ ] 工作区未混入无关用户修改
[ ] 只完成当前 commit map 编号声明的范围
[ ] 实现、测试和直接相关文档在同一个 commit
[ ] 当前 commit 可独立 checkout 和通过验收
[ ] 并行分支只负责一个 commit ID
[ ] 所有前置 commit 已进入 main，当前分支已 rebase
[ ] rebase 后重新执行定向测试、全量测试和 diff check
[ ] 开始下一 commit 前工作区干净
[ ] 是否 commit 由用户确认
[ ] 是否 push 必须由用户明确指令
```

## 20. 现在应该从哪里开始

Step 0–4 和 commit `5.1`–`5.7` 已完成并验收。当前所有托管客户端都使用只读
rendered deployment；edit session 已具备严格 metadata、只读
`list/status/diff/validate/impact`、Base baseline snapshot、writable workspace、
`begin/abort` 和 transactional `apply`。apply 使用 `deployment.lock → per-Skill
lock`，检查 baseline conflict 和 workspace validation，持久化私有 backup/receipt，
再通过同父目录 no-replace rename 替换 canonical。apply 只为受影响且实际启用的
concrete clients 构建并验证新的 rendered deployment，并在同一事务中切换 Agent
链接；原始相对 symlink、链式链接或 Windows junction 对象会保留到全局提交点。
普通失败按相反顺序恢复所有链接，再恢复 canonical；外部 winner 永不覆盖。任一
回滚结果不确定时保留 winner、backup 和 artifact，并将 session/receipt 标记为
`needs-recovery`。canonical、链接、metadata 和 completed receipt 提交后的清理异常
只记录为 `cleanup_pending`，不会把已提交事务重新回滚。apply 仍不执行 Git 操作。

下一 commit 是 `5.8 add managed edit recovery`：提供 tampered deployment diff 和
显式 capture/discard 流程，绝不自动采用被篡改内容。它位于串行关键链上，不与
`5.7` 或 `5.9` 并行开发。

建议每次只完成 commit map 中的一个编号，并在交付时报告：

- 当前 commit 编号和 message；
- 改了什么；
- 验证了什么；
- 是否影响真实链接；
- 是否发生 schema migration；
- 下一 commit 编号是什么；
- 当前未 commit、未 push 的状态。
