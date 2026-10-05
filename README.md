# 考古地层关系核验服务

基于 **Python (FastAPI) + PostgreSQL** 的考古地层关系核验服务。登记遗址地层与带唯一证据编号的
“同一期 / 早于”断言，自动推导地层间关系，支持批次原子核验、冲突证据路径定位、关系查询与不可变定版快照。

## 语义规则

- **同一期（same_period）**：对称且可传递，构成等价类。
- **早于（earlier_than）**：沿“同一期 + 早于”混合路径推导。
  例如 `L1 ~ L2`（同一期）、`L2 < L3`（早于），则 `L1 早于 L3`。
- **硬约束**（对整批应用后的最终关系核验）：
  1. **组内早于禁止**——同一期等价类内部不得存在早于关系；
  2. **早于闭环禁止**——等价类之间的早于关系不得成环。
- **批次语义**：每批新增/撤回须携带当前修订号；按整批应用后的最终关系核验；
  发生矛盾时**整批拒绝**并返回冲突证据路径，原状态不变；全部通过则在同一事务内落库并**原子递增修订号**。
- **路径选取**：多条证据路径时，取证据数最少者；并列时按证据编号序列字典序最小者。
- **关系查询的完整视图一致性**：正式关系查询 `GET /relationship`（输入两地层编号）与
  推演关系查询 `GET /projections/{id}/relationship`（另带推演编号）均在
  **`REPEATABLE READ READ ONLY` 只读快照事务**内完成：一次响应中的修订号（推演另含
  推演状态与推演修订号）、关系结论、最优证据路径 `evidence_path` 与逐步证据明细
  `path_detail`（含每步 `status`）取自**同一完整视图**。并发正式批次（含野外整包
  汇入 / 勘误、推演并入）或推演批次在查询的多条读取语句之间提交时，响应只可能整体
  是**提交前**或整体是**提交后**的结果，不会出现旧修订号配新结论、或路径中的证据
  在逐步明细中显示为已撤回的混版响应。查询入口、输入输出字段、未知地层
  （400 `INVALID_STRATUM`）与未知推演（404 `PROJECTION_NOT_FOUND`）的错误约定、
  路径选取规则（证据数最少、并列按证据编号序列字典序最小）均不变；**已关闭推演
  （merged/discarded）仍可查询其冻结视图**。
- **定版（快照）**：保存当前修订号下全部关系结论的不可变快照；后续撤回不影响旧版结论。
  历史定版关系查询（`GET /snapshots/{version}/relationship`）的结论、最优证据路径与
  逐步证据明细 `path_detail` 均按**封存时**口径返回：明细取自定版冻结的断言副本
  （`snapshots.assertions`，与同期封存的成果包证据明细同一来源），不读在线断言表，
  因此后续正式批次、野外包勘误或推演并入撤回路径上的证据后，历史明细（含每步
  `status`）保持封存时的 `active` 不变；当前正式关系查询继续显示现时状态。
- **专题成果包**：把某定版中的一组（≥2、不重复、已登记）地层封存为不可变、可转交
  复核包：只含按编号排序的无序两两关系，结论与最优路径同该定版查询，确定结论携带
  路径按序原始断言，途经未入选地层随证据保留但不扩展关系，未定不附路径；同一定版 +
  同一地层集合（顺序无关）只封存一份，撤回 / 新修订 / 新定版均不改变包内容。
- **修订变化追踪**：对每次成功的正式断言批次及推演并入产生的正式修订，记录提交前后
  关系结论或最优支撑路径发生变化的地层对；同一对按地层编号固定顺序只列一次；
  关系不变但最优路径改变也记录；变为未定时路径为空。记录与批次同事务原子写入，
  失败批次、推演内编辑、无净变更并入不产生记录；已有历史修订在服务启动时幂等回填
  （每个待补修订以紧邻上一正式修订的当时生效断言为前态，封口/待补交错也不漏撤回；
  零变化修订同样封口，重复启动与在线提交交错不重写、不漏记、不重复）。
- **推演（沙盘）**：创建时固定当前正式修订号、已登记地层与生效断言；推演内沿用正式批次的
  新增 / 撤回 / 整批核验语义（携带推演自身修订号），正式状态始终不变。提交（并入）时把推演
  **相对基线的净变更**应用于最新正式状态并重新核验：基线目标已撤回 / 编号已被正式占用 /
  关系冲突（附证据路径）时返回具体原因且双方不变；通过则作为一次正式批次原子提交并关闭推演；
  净变更为空时只关闭推演，不递增正式修订号。
- **提交前预检（只读）**：`POST /batches/preflight` 输入沿用正式批次的 `expected_revision`、
  `operations`，并指定可额外撤回的生效旧证据数上限 `extra_retraction_budget`（0～3）。在该
  修订号对应的同一正式视图中先按原批次语义处理新增与撤回，再寻找**不包含本批新增证据、也不
  重复本批撤回**的额外撤回集合：撤回数最少、并列时按证据编号序列字典序最小，返回该集合与
  应用后是否通过关系核验。临时保全在同一视图内生效：本批**直接撤回**受保全证据按正式批次的
  409 `EVIDENCE_HELD` 拒绝（附 `blocked_evidence`），额外撤回搜索**排除受保全证据**
  （对其余生效旧证据仍按数量最少、编号序列字典序最小选解）。预检**不生成修订、变化记录
  或断言**，正式批次请求与返回格式不变。
- **分期试排（只读）**：`POST /periodization/arrangement`（正式）与
  `POST /projections/{id}/periodization/arrangement`（推演冻结视图，含已关闭推演）输入
  视图对应的 `expected_revision`、总期数 `total_periods`（1～64）及可选地层期位锚点
  （`anchors[]`，期位 1～总期数）。同一期等价组须占同一期，早于关系源组期位严格小于目标
  组，未锚定组可占任一期。**可行时返回每组所含地层，以及该组在所有可行排布中的最早 / 最晚
  期位**（`earliest_period`/`latest_period`，ASAP/ALAP 界，不是某一套任意排布）；**不可行
  时 HTTP 仍为 200、`feasible=false`，不返回任何组的期位**，`reasons[]` 区分三类：
  同组锚点矛盾 `anchor_group_conflict`、先后链与锚点不相容 `anchor_precedence_conflict`、
  先后链与期数不相容 `chain_exceeds_periods`，每条附关联锚点、组链与证据路径。分析在
  `REPEATABLE READ READ ONLY` 快照事务内完成，不写库、不递增修订号；锚点地层不存在
  （400 `INVALID_STRATUM`）、锚点地层重复（400 `DUPLICATE_STRATUM`）、锚点期位越界
  （400 `INVALID_PERIOD_POSITION`）、修订号过期（409 `STALE_REVISION`）、推演不存在
  （404 `PROJECTION_NOT_FOUND`）明确拒绝，既有查询与批次语义不变。
- **证据撤回承受力分析（只读）**：`POST /resilience/retraction-analysis` 输入视图对应的
  `expected_revision`、两个**不同的已登记**地层 `stratum_a` / `stratum_b` 及可为空的
  受保护生效证据编号集合 `protected_evidence`（自动去重）。服务在该修订号对应的**同一正式
  视图**中，只从**其余生效证据**（同一期 / 早于证据均可，受保护证据不可撤回）中求让两层
  关系变为未定所需的**最少撤回数**，并列时取**证据编号序列字典序最小**的一组，返回原关系、
  原最优证据路径、建议撤回编号与模拟撤回后的关系：
  - 关系由“同一期（无向）+ 早于（有向）”混合图上的路径支撑，撤回只删边；使关系变未定 ⇔
    切断支撑该关系的**全部**路径（最少撤回数即混合证据图上的 s–t 最小割）；
  - **同一期双向连通按一条证据撤回计数**（无向边割值为 1）；
  - **替代证据路径仍成立时不判失效**：若受保护证据单独仍支撑某条路径，返回
    `status=unreachable`、`minimum_retraction_count=null` 与仍成立的（字典序最小）证据路径
    `remaining_evidence_path`；
  - 原关系未定时返回 `status=already_undetermined`、撤回数 `0`、空集合；成功找到时
    `status=retractions_found`，模拟后关系恒为 `undetermined`；
  - 修订号过期（409 `STALE_REVISION`）、地层未登记（400 `INVALID_STRATUM`）、两层相同
    （400 `DUPLICATE_STRATUM`）、受保护证据不存在或非生效
    （400 `INVALID_PROTECTED_EVIDENCE`，`details` 区分 `not_found` / `not_active`）明确拒绝；
    分析不改断言、修订或变化记录。
- **野外终端离线整包汇入**：`POST /field-packages` 供无网络作业的终端回站后整包汇入。请求携带
  采集站点 `site`、终端编号 `terminal_id`、包号 `package_no`、预期正式修订号
  `expected_revision`、**短号→全库地层编号对照** `mappings[]` 与**带包内唯一序号**
  `seq` 的同一期 / 早于断言 `assertions[]`：
  - **断言端点只能引用本包短号**（400 `UNKNOWN_SHORT_CODE`）；包内短号不可重复
    （400 `DUPLICATE_SHORT_CODE`）、序号不可重复（400 `DUPLICATE_SEQUENCE`）；
  - **对照目标已登记时站点须一致**（409 `SITE_CONFLICT`），未登记时**随包建立**；
    **同一终端的同一短号跨包不可改绑**（409 `SHORT_CODE_REBOUND`）；不同终端的短号空间互不影响；
  - **证据编号由终端、包号、序号稳定确定**：`F:<base64url(终端)>:<base64url(包号)>:<12位序号>`
    （同输入恒同号、不同输入不碰撞）；该编号不得占用已有证据编号（含已撤回，
    409 `FIELD_EVIDENCE_TAKEN`）；
  - **整包沿用正式批次的最终关系核验**：组内早于 / 早于闭环冲突时整批拒绝并返回冲突证据路径
    （409 `FIELD_CONFLICT`）；**任一错误都不落地层、绑定或断言**（单事务回滚）；
  - 通过时**只递增一次正式修订**，与关系变化记录、包台账**同事务提交**，响应返回短号映射
    `mappings`（含每个目标是随包新建还是沿用已登记）、稳定证据编号清单 `evidence` 与修订号；
  - **幂等**：相同终端 + 包号 + 内容（指纹不含 `expected_revision`，与数组顺序无关）重试
    返回首次成功的原结果（`replayed=true`），**即使正式修订已前进、预期号已过期**；
    同终端同包号但**异内容**复用拒绝（409 `FIELD_PACKAGE_CONTENT_MISMATCH`，双指纹）；
    首次汇入仍做过期修订校验（409 `STALE_REVISION`）；失败不占包号，修正后可按原包号重提；
  - 该修订的关系变化同样进入 `/revision-changes`，来源为 `field_package`。
    台账可读：`GET /field-packages`（不带参数为摘要列表；同带
    `?terminal_id=...&package_no=...` 为详情）或路径形式
    `GET /field-packages/{terminal_id}/{package_no}`（终端/包号含 `/` 时用查询参数形式），
    含内容指纹与首次响应，不存在返回 404 `FIELD_PACKAGE_NOT_FOUND`。
- **成果包交付前复核（只读）**：`POST /packages/{id}/reconcile` 输入成果包编号（路径）
  与预期当前正式修订号 `expected_revision`（请求体），在**同一只读快照视图**中逐项比较
  包内每个地层对的**封存关系结论、最优证据路径与当前正式结果**，分类为
  `conclusion_changed`（结论改变）/ `path_only_changed`（仅路径改变）/
  `identical`（完全一致）；当前结果由**全部生效断言**推导，**途经未入选地层的证据仍
  参与当前推导**，未定关系路径为空。同时给出每对**自封存修订后首次偏离封存结果**的
  正式修订号与变化来源（`first_deviation`，含修订后关系/路径与上一状态），**即使后来
  已恢复一致也保留曾偏离记录**，从未偏离为 `null`。包不存在（404 `PACKAGE_NOT_FOUND`）
  或预期号过期（409 `STALE_REVISION`）明确拒绝；核对不改写成果包、断言或修订。
- **已汇入整包勘误替换**：`POST /field-packages/corrections` 对已成功汇入的整包整包勘误。
  接收**原终端号、原包号、新包号、预期正式修订号**，以及新包的**站点、短号对照和带序号断言**：
  - **新包号须未使用且不同于原包号**（400 `FIELD_CORRECTION_SAME_PACKAGE`、
    409 `FIELD_PACKAGE_NO_TAKEN`）；**原包必须存在**（404 `FIELD_PACKAGE_NOT_FOUND`）、
    **未被替换**（409 `FIELD_PACKAGE_ALREADY_REPLACED`），且**原包证据须全部仍生效**
    （409 `FIELD_PACKAGE_EVIDENCE_INACTIVE`，`details` 列出已撤回 / 缺失证据）；
  - **新包沿用现有短号、序号、端点、站点、跨包绑定及稳定证据编号规则**
    （证据编号由终端、**新包号**、序号稳定确定；各类 400/409 拒绝码同整包汇入）；
  - **一次事务**完成：撤回原包全部证据、汇入新包，并对**最终断言集**核验组内早于和闭环
    （冲突 409 `FIELD_CORRECTION_CONFLICT` 附证据路径）。任一错误或冲突都**不改变地层、
    绑定、断言、台账和修订**（失败不占新包号，修正后可用同一新包号重提）；成功只
    **递增一次正式修订**，关系变化记录来源为 `field_package_correction`，原包台账追加
    `status=superseded` 与 `superseded_by_package_no` 指向，并维护多级**替换链**；
  - **幂等**：同一原包与新包的相同内容重试，**即使修订已前进、预期号已过期**也返回首次
    勘误结果（`replayed=true`）；同原包同新包**异内容**复用拒绝
    （409 `FIELD_CORRECTION_CONTENT_MISMATCH`，双指纹）；首次勘误仍做过期修订校验；
  - **历史可查**：原包台账、首次汇入响应（`stored_response` 永不修改）、历史定版与推演
    冻结结果保持可查；**原包号原请求重放**（`POST /field-packages`）仍返回首次汇入结果。
    勘误台账可读：`GET /field-packages/corrections`（可按 `terminal_id` 与
    `original_package_no` 过滤）与
    `GET /field-packages/corrections/{terminal_id}/{original_package_no}/{new_package_no}`。
- **争议证据临时保全**：交接复核期间防止争议中的正式证据被撤回。
  `POST /evidence-holds` 携带当前正式修订号 `expected_revision`、一组**不重复且当前均
  生效**的证据编号 `evidence_ids` 与事由 `reason`；成功返回保全记录编号 `id`、保全状态、
  每条证据当前的在保全来源，以及**仅在创建响应中显示一次**的解除凭据 `credential`
  （明文不落库，库内只存 sha256 哈希；`GET /evidence-holds`、`GET /evidence-holds/{id}`
  与 `GET /evidence-holds/evidence/{evidence_id}` 只返回状态、覆盖证据及每条证据的
  生效保全来源，**永不回传凭据**）。保全建立 / 解除**不递增正式修订、不改变既有断言
  与快照**。凭对应凭据 `POST /evidence-holds/{id}/release` 解除；明确拒绝：错误凭据
  （403 `INVALID_HOLD_CREDENTIAL`）、证据不存在 / 非生效（400 `INVALID_HOLD_EVIDENCE`，
  `details.invalid_evidence` 逐条给状态）、请求内编号重复
  （400 `HOLD_DUPLICATE_EVIDENCE`）、过期修订（409 `STALE_REVISION`）、保全不存在
  （404 `HOLD_NOT_FOUND`）、重复解除（409 `HOLD_ALREADY_RELEASED`）。
  **多份保全覆盖同一证据时须全部解除才可撤回**：普通批次（409 `EVIDENCE_HELD`）、
  野外包勘误（409 `FIELD_PACKAGE_EVIDENCE_HELD`）及推演并入（409 `MERGE_EVIDENCE_HELD`）
  凡将受保全覆盖证据撤回到正式库均**整笔拒绝**，响应 `details` 返回命中的证据
  `blocked_evidence` 及其全部生效保全记录，正式修订、包台账与推演状态均不变；
  **推演内试验撤回不触碰正式库，仍可进行**。保全建立 / 解除与正式写入在事务内以
  **同一顺序**（修订计数器行锁 `FOR UPDATE`）裁决，持锁后再做全部判定，无死锁、
  不出现“撤回成功且保全仍生效”的交叉态。

## 争议证据临时保全（交接复核期间冻结撤回入口）

争议中的正式证据需要临时保全，避免交接复核期间被撤回。保全**只冻结撤回入口**，
不改变证据本身、正式修订或任何既有快照。

**建立保全**：`POST /evidence-holds`，请求携带当前正式修订号、一组不重复且当前均
生效的证据编号与事由：

```json
{
  "expected_revision": 3,
  "evidence_ids": ["EV-002", "EV-005"],
  "reason": "二期交接复核：H2-H5 关系链存疑，待原始档案核对"
}
```

成功响应（HTTP 201）返回保全记录编号、状态、每条证据当前的在保全来源，以及
**仅在创建时显示一次**的解除凭据 `credential`（请妥善转交；丢失后只能由库内哈希
校验、无法找回明文）：

```json
{
  "id": 7,
  "revision": 3,
  "current_revision": 3,
  "reason": "二期交接复核：H2-H5 关系链存疑，待原始档案核对",
  "status": "active",
  "evidence_ids": ["EV-002", "EV-005"],
  "evidences": [
    {"evidence_id": "EV-002", "status": "active", "holds": [
      {"id": 7, "revision": 3, "reason": "二期交接复核：…", "created_at": "…"}]},
    {"evidence_id": "EV-005", "status": "active", "holds": [
      {"id": 7, "revision": 3, "reason": "二期交接复核：…", "created_at": "…"}]}
  ],
  "credential": "rcZFvOdh0VEO9dDt4nrB4M27tdJqqWHqVep_8EKUM5w",
  "created_at": "…"
}
```

- **查询不泄露凭据**：`GET /evidence-holds`（可加 `?status=active|released`）、
  `GET /evidence-holds/7` 返回保全状态、覆盖证据及每条证据**当前**的在保全来源
  （同一证据可能同时被多份保全覆盖，`holds` 给出全部生效保全），
  `GET /evidence-holds/evidence/EV-002` 按证据反查；任何查询响应都不含 `credential`。
- **撤回拦截**：保全期间正式写入路径上的撤回整笔拒绝，并返回命中的证据与保全记录：

| 路径 | 拒绝码 |
| --- | --- |
| `POST /batches`（普通批次撤回归档） | 409 `EVIDENCE_HELD` |
| `POST /field-packages/corrections`（勘误撤回原包证据） | 409 `FIELD_PACKAGE_EVIDENCE_HELD` |
| `POST /projections/{id}/merge`（并入撤回基线证据） | 409 `MERGE_EVIDENCE_HELD` |
| `POST /batches/preflight`（只读预检中的直接撤回，同一拒绝） | 409 `EVIDENCE_HELD` |

  拒绝体 `details.blocked_evidence[]` 形如
  `[{"evidence_id": "EV-002", "holds": [{"id": 7, "revision": 3, "reason": "…", "created_at": "…"}]}]`，
  并附 `blocked_evidence_ids` 与 `current_revision`；整笔拒绝意味着同批中未受保的
  其他撤回也不生效，**正式修订、断言、包台账与推演状态全部不变**。
  **推演内试验撤回**（`POST /projections/{id}/batches`）不写正式库，仍可进行；
  其并入尝试会被 `MERGE_EVIDENCE_HELD` 拦截、推演保持 open。
  **提交前预检**（`POST /batches/preflight`，只读）在同一快照视图内读取生效保全：
  直接撤回受保全证据按同一 409 `EVIDENCE_HELD` 拒绝，搜索额外撤回建议时排除
  受保全证据（避免给出照做必被正式批次拒绝的建议）。
- **多份保全覆盖同一证据须全部解除**：每份保全有独立凭据，只有覆盖该证据的全部
  生效保全都解除后，撤回才可进行。解除 `POST /evidence-holds/7/release`：

```json
{"expected_revision": 3, "credential": "rcZFvOdh0VEO9dDt4nrB4M27tdJqqWHqVep_8EKUM5w"}
```

  成功返回 `{"id": 7, "status": "released", "revision": 3, "current_revision": 3, …}`
  （不递增正式修订）。错误凭据 403 `INVALID_HOLD_CREDENTIAL`、保全不存在 404
  `HOLD_NOT_FOUND`、重复解除 409 `HOLD_ALREADY_RELEASED`、修订号过期 409
  `STALE_REVISION`，均不改变任何状态。已解除保全保留 `released` 留痕。
- **并发裁决**：保全建立 / 解除与正式批次 / 勘误 / 并入在各自事务内以**相同顺序**
  锁定修订计数器行（`SELECT … FOR UPDATE`），取锁后再完成全部校验与写入：并发
  请求按同一个全序串行裁决，终态必然自洽——保全先建则并发撤回收到
  `EVIDENCE_HELD`；撤回先成则并发建保全收到 `INVALID_HOLD_EVIDENCE`（证据已非生效），
  不会出现“撤回成功且保全仍生效”或两者都落空。保全操作只锁计数器、不更新其值，
  **不递增正式修订**，也不改变既有断言、定版快照与推演冻结视图。

```bash
bash examples/evidence_holds_demo.sh
```

## 快速开始

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式接口文档（Swagger UI）：**http://localhost:8000/docs**
- PostgreSQL：`localhost:5432`（容器内服务名 `db`，数据持久化于命名卷 `pgdata`）

首次启动时应用会自动等待数据库就绪并完成表结构初始化，无需手工迁移。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://strat:strat@db:5432/stratigraphy`（compose 内） | 应用连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `strat` / `strat` / `stratigraphy` | 数据库账号（compose） |
| `APP_PORT` | `8000` | API 对外端口 |
| `DB_PORT` | `5432` | 数据库对外端口 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://strat:strat@localhost:5432/stratigraphy
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/strata` | 登记地层 `{stratum_id, site?}` |
| GET | `/strata` | 地层列表 |
| POST | `/batches` | 提交一批断言新增/撤回（整批核验，成功原子递增修订号） |
| POST | `/field-packages` | 野外终端离线整包汇入：短号对照建层/绑定、稳定证据编号、整批核验、幂等返回原结果 |
| POST | `/field-packages/corrections` | 已汇入整包勘误：单事务撤回原包证据 + 新包号汇入 + 最终核验；成功只 +1 修订、记录替换链 |
| GET | `/field-packages/corrections` / `/field-packages/corrections/{terminal_id}/{original_package_no}/{new_package_no}` | 勘误台账列表（可按终端/原包过滤）/ 单条勘误详情（撤回证据与首次勘误响应） |
| POST | `/evidence-holds` | 争议证据临时保全：携带当前修订号、不重复且均生效的证据编号与事由；返回保全编号与仅显示一次的解除凭据（不递增修订） |
| GET | `/evidence-holds` / `/evidence-holds/{id}` | 保全台账列表（可按状态过滤）/ 单份保全状态与每条证据的在保全来源（均不泄露凭据） |
| GET | `/evidence-holds/evidence/{evidence_id}` | 按证据编号反查：是否在保全中、生效保全数与全部生效保全来源 |
| POST | `/evidence-holds/{id}/release` | 凭对应凭据解除指定保全（错误凭据 403；不递增修订） |
| GET | `/field-packages` / `/field-packages/{terminal_id}/{package_no}` | 野外汇入包台账列表 / 详情（内容指纹与首次响应；已替换包带 status/替换链；终端或包号含 `/` 等路径保留字符时用 `/field-packages?terminal_id=...&package_no=...`） |
| POST | `/batches/preflight` | 提交前只读预检：求消除冲突所需撤回的最少生效旧证据（预算 0～3），不落库 |
| GET | `/revision` | 当前修订号 |
| GET | `/assertions?status=active` | 断言列表 |
| GET | `/revision-changes?limit=100&cursor=...` | 游标分页追踪各正式修订前后关系/最优路径变化 |
| GET | `/relationship?a=L1&b=L3` | 查询两地层当前关系及支撑证据路径 |
| POST | `/resilience/retraction-analysis` | 只读证据撤回承受力分析：求让指定两层关系变为未定的最少生效证据撤回（含受保护证据集合） |
| GET | `/periodization?expected_revision=1` | 只读分期骨架：同一期等价组 + 组间传递约简后的直接早于边（含孤立地层） |
| POST | `/periodization/arrangement` | 正式视图只读分期试排：总期数 1～64 + 期位锚点，返回每组在所有可行排布中的最早/最晚期位 |
| POST | `/projections/{id}/periodization/arrangement` | 推演冻结视图（含已关闭）只读分期试排，返回结构同正式 |
| POST | `/snapshots` | 定版：保存不可变关系快照 `{label?}` |
| GET | `/snapshots` / `/snapshots/{version}` | 版本列表 / 版本详情 |
| GET | `/snapshots/{version}/relationship?a=L1&b=L3` | 查询历史版本结论（结论 / 最优路径 / 逐步明细均按封存时冻结口径，与同一定版封存的成果包明细一致） |
| POST | `/projections` | 创建推演（固定当前修订号、地层与生效断言） `{label?}` |
| GET | `/projections` / `/projections/{id}` | 推演列表 / 推演详情（含净变更） |
| POST | `/projections/{id}/batches` | 推演内提交批次（携带推演修订号，语义同正式批次） |
| GET | `/projections/{id}/relationship?a=L1&b=L3` | 查询推演视角下的关系与证据路径 |
| POST | `/projections/{id}/merge` | 并入：净变更应用于最新正式状态并重新核验 |
| DELETE | `/projections/{id}` | 放弃推演（仅 open 可放弃） |
| POST | `/packages` | 封存专题成果包：把定版中的一组地层关系冻结为可转交复核包 `{version, strata[]}` |
| POST | `/packages/{id}/reconcile` | 成果包交付前只读复核：逐对比较封存关系/最优路径与当前结果（结论改变/仅路径改变/完全一致），附首次偏离修订号与来源 |
| GET | `/packages` / `/packages/{id}` | 成果包列表 / 成果包详情（冻结内容与内容指纹） |

### 批次请求格式

```json
{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-009"}
  ]
}
```

- `relation`：`same_period`（同一期）/ `earlier_than`（`stratum_a` 早于 `stratum_b`）。
- 成功响应：`{"revision": 1, "applied": 3, "relation_changes": 3}`
  （`relation_changes` 为本修订导致关系或最优支撑路径变化的地层对数，可能为 0）。
- 冲突响应（HTTP 409，原状态不变）：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "批次与现有断言矛盾，整批拒绝，原状态不变",
    "details": {
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...", "evidence_path": ["EV-002", "EV-003", "EV-004"]}
      ]
    }
  }
}
```

### 提交前预检：`POST /batches/preflight`（只读）

考古人员提交断言批次前，可先知道**最少放弃哪些现有证据**才能消除冲突。预检请求体与正式
批次完全相同，另加预算字段：

```json
{
  "expected_revision": 1,
  "extra_retraction_budget": 2,
  "operations": [
    {"op": "assert", "evidence_id": "EV-100", "stratum_a": "L4", "stratum_b": "L1", "relation": "earlier_than"}
  ]
}
```

- `extra_retraction_budget`：可额外撤回的**生效旧证据**数上限，取 `0`～`3`，默认 `0`。
- 处理顺序：在该修订号对应的**同一正式视图**中先按原批次语义处理本批新增与撤回（含同批
  “新增后又撤回”按最终不保留处理），再在旧证据中求额外撤回集合；
- 额外撤回集合**不包含本批新增证据**、也**不重复本批撤回目标**，只从“处理后仍生效的历史
  证据”中选取；
- **临时保全与正式批次同一口径**（生效保全在同一快照视图内读取，结果以读取时状态为准）：
  - 本批**直接撤回**受生效保全覆盖的证据：按正式批次的 **409 `EVIDENCE_HELD`** 拒绝，
    `details` 同样给出 `blocked_evidence`（命中证据及其全部生效保全记录）与
    `blocked_evidence_ids` / `current_revision`；
  - 额外撤回搜索**排除受保全证据**——否则照建议提交会被正式批次以 `EVIDENCE_HELD`
    拒绝；对其余生效旧证据仍按撤回数最少、并列编号序列字典序最小选解；预算内无解仍返回
    `budget_insufficient` 与冲突路径；解除保全后同一预检恢复原有建议；
- 选取规则：撤回数最少（0～预算）；并列时按**证据编号序列字典序最小**（同一期边与早于边
  都可入选——撤回同一期边会拆开等价类，同样可能消除组内早于或闭环）；
- 修订号过期、证据编号占用（含已撤回编号、同批重复）、撤回目标无效（不存在 / 已撤回 /
  同批重复撤回）、引用未登记地层等**全部沿用正式批次的拒绝语义与错误码**。

预检**只读**（在 `REPEATABLE READ READ ONLY` 快照事务内完成），不生成修订、变化记录或
断言，正式批次的既有请求与返回格式均未改变。

`resolution` 取值（HTTP 恒为 200；结构性拒绝仍为 4xx）：

| resolution | 含义 | `extra_retractions` | `passed` |
| --- | --- | --- | --- |
| `already_consistent` | 原批次本就可通过，无需放弃任何旧证据 | `[]` | `true` |
| `extra_retractions_found` | 预算内找到最少撤回集合；`passed` 为应用该集合后是否通过核验 | 排序后的证据编号序列 | `true` |
| `unfixable_new_assertions` | 本批最终保留的新断言自身矛盾（仅凭新断言即构成组内早于 / 早于闭环），无法靠撤回旧证据修复 | `[]` | `false` |
| `budget_insufficient` | 预算内不存在解（预算不足） | `[]`（`extra_retraction_count` 为 `null`） | `false` |

后两种情况都在 `conflicts` 中附上**当前仍存在的冲突证据路径**（处理后视图、未做额外撤回
时的组内早于 / 早于闭环，结构与正式批次 `CONFLICT.details.conflicts` 一致）。

找到方案的响应示例：

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "extra_retraction_budget": 1,
  "batch_new_evidence": ["EV-100"],
  "batch_retracted_evidence": [],
  "resolution": "extra_retractions_found",
  "passed": true,
  "extra_retractions": ["EV-001"],
  "extra_retraction_count": 1,
  "conflicts": []
}
```

把 `extra_retractions` 作为额外的 `{"op": "retract", ...}` 并入原批次后再提交正式批次，
即可通过核验（示例见 `examples/preflight_demo.sh`）。

**与临时保全的联动**（保全见下文「争议证据临时保全」）：已生效的 `E1`（`A 早于 B`）被
保全时，拟新增 `E2`（`B 早于 A`）构成闭环——预检**不会**建议撤回 `E1`：

```bash
# E1 已生效且被保全（POST /evidence-holds 建立，不递增修订）
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "extra_retraction_budget": 1,
  "operations": [
    {"op": "assert", "evidence_id": "E2", "stratum_a": "B", "stratum_b": "A", "relation": "earlier_than"}
  ]}'
# -> resolution=budget_insufficient（唯一可解证据 E1 受保全，不进建议），
#    conflicts 附当前冲突证据路径 ["E1", "E2"]

# 预检中直接撤回受保全证据：与正式批次同一拒绝
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [{"op": "retract", "evidence_id": "E1"}]}'
# -> HTTP 409 EVIDENCE_HELD，details.blocked_evidence 列出 E1 的全部生效保全记录

# 解除保全（POST /evidence-holds/{id}/release）后同一预检恢复建议：
# -> resolution=extra_retractions_found，extra_retractions=["E1"]，passed=true
```

保全建立 / 解除不递增正式修订，预检结果以读取时状态为准；预检本身不写入任何状态，
上述联动只改变预检的**建议与拒绝**，不改变预检只读语义。

### 野外终端离线整包汇入：`POST /field-packages`

野外终端无网络时用**本地短号**记录地层关系，回站后一次性整包汇入正式库。请求体：

```json
{
  "site": "西山坡",
  "terminal_id": "T-07",
  "package_no": "2026-09-01-03",
  "expected_revision": 0,
  "mappings": [
    {"short_code": "s1", "stratum_id": "XSP-01"},
    {"short_code": "s2", "stratum_id": "XSP-02"},
    {"short_code": "s3", "stratum_id": "XSP-03"}
  ],
  "assertions": [
    {"seq": 1, "stratum_a": "s1", "stratum_b": "s2", "relation": "same_period"},
    {"seq": 2, "stratum_a": "s2", "stratum_b": "s3", "relation": "earlier_than"}
  ]
}
```

- `site`：采集站点。`mappings[]` 的对照目标**已登记**时其站点必须与之相等，否则
  409 `SITE_CONFLICT`（`details` 同时给出 `registered_site` / `request_site`）；
  目标**未登记时随本包建立**（站点取 `site`）；
- `terminal_id` + `package_no`：终端内包号唯一，是幂等键；
- `mappings[]`：短号→全库地层编号对照，包内短号不可重复（400 `DUPLICATE_SHORT_CODE`）；
- `assertions[]`：每条带**包内唯一序号** `seq`（≥1，重复 400 `DUPLICATE_SEQUENCE`），
  端点 `stratum_a` / `stratum_b` **只能引用本包 mappings 中的短号**
  （否则 400 `UNKNOWN_SHORT_CODE`，`details` 带 `seq` 与 `short_code`）；
- `expected_revision`：预期正式修订号。首次汇入过期返回 409 `STALE_REVISION`；
  **幂等重试不检查修订号**。

**稳定证据编号**：由终端、包号、序号确定性生成，同输入永远同号，可在离线端预计算：

```
F:<base64url(terminal_id)>:<base64url(package_no)>:<seq 补零 12 位>
```

例：终端 `T-07`、包号 `2026-09-01-03`、序号 1 →
`F:VC0wNw:MjAyNi0wOS0wMS0wMw:000000000001`。若生成的编号已被历史断言占用（含已撤回），
整包 409 `FIELD_EVIDENCE_TAKEN`，`details.occupied_evidence_ids` 列出全部碰撞编号。

成功响应（整包核验通过、单事务提交，正式修订号恰好 +1）：

```json
{
  "site": "西山坡",
  "terminal_id": "T-07",
  "package_no": "2026-09-01-03",
  "revision": 1,
  "mappings": [
    {"short_code": "s1", "stratum_id": "XSP-01", "site": "西山坡", "already_registered": false},
    {"short_code": "s2", "stratum_id": "XSP-02", "site": "西山坡", "already_registered": false},
    {"short_code": "s3", "stratum_id": "XSP-03", "site": "西山坡", "already_registered": false}
  ],
  "evidence": [
    {"seq": 1, "evidence_id": "F:VC0wNw:MjAyNi0wOS0wMS0wMw:000000000001",
     "stratum_a": "XSP-01", "stratum_b": "XSP-02", "relation": "same_period"},
    {"seq": 2, "evidence_id": "F:VC0wNw:MjAyNi0wOS0wMS0wMw:000000000002",
     "stratum_a": "XSP-02", "stratum_b": "XSP-03", "relation": "earlier_than"}
  ],
  "applied": 2,
  "relation_changes": 3,
  "replayed": false
}
```

- 冲突响应（HTTP 409 `FIELD_CONFLICT`，原状态不变——**地层、绑定、断言都不落地**），
  `details.conflicts[]` 与正式批次一致，含组内早于 / 早于闭环的 `evidence_path`：

```json
{
  "error": {
    "code": "FIELD_CONFLICT",
    "message": "野外整包与现有断言矛盾，整批拒绝，地层 / 绑定 / 断言均不落地",
    "details": {
      "terminal_id": "T-07",
      "package_no": "2026-09-01-03",
      "current_revision": 0,
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...",
         "evidence_path": ["F:VC0wNw:...:000000000001", "..."]}
      ]
    }
  }
}
```

**短号绑定的跨包规则**：`(终端, 短号) → 全库地层编号` 首次随成功的包建立；之后同一终端的
后续包里该短号**必须仍对照同一目标**，否则 409 `SHORT_CODE_REBOUND`（`details` 给出
`bound_stratum_id` / `bound_package_no` / `requested_stratum_id`）。沿用既有绑定时无需重复
登记、不校验站点（地层与站点在建绑时已确认一致）。不同终端的短号各自独立。

**幂等与复用**：内容指纹对 `site`、规范化（排序）后的 `mappings` / `assertions` 取 sha256，
**不含 `expected_revision`、与数组顺序无关**。

| 情形 | 结果 |
| --- | --- |
| 同终端 + 同包号 + 同内容重试（任何修订号、任何时机） | HTTP 200，返回**首次成功响应**原样（含原修订号、原证据编号），`replayed=true`，修订不再前进 |
| 同终端 + 同包号 + 异内容 | 409 `FIELD_PACKAGE_CONTENT_MISMATCH`（`details` 给原指纹与本次指纹） |
| 首次汇入携带过期修订号 | 409 `STALE_REVISION`（取当前修订号后重提即可） |
| 汇入失败（任何校验 / 冲突） | 不占包号、不留任何数据；修正后可用原包号重提 |

汇入产生的正式修订与正式批次完全同构：写入 `assertions` / `revisions` 审计（batch 带
`source=field_package`）、修订号 +1、`/revision-changes` 中对应变化的 `source` 为
`field_package`、定版 / 推演 / 查询等既有能力全部可见，无额外口径。

```bash
bash examples/field_packages_demo.sh
```

### 已汇入整包勘误：`POST /field-packages/corrections`

已经成功汇入的整包若需勘误（例如原包把两条“早于”错记成了“同一期”），终端回站后用
**新包号**提交勘误包，服务在**同一事务**内撤回原包全部证据、汇入新包并核验最终断言集。
请求体在整包汇入基础上把单个 `package_no` 拆成 `original_package_no` + `new_package_no`：

```json
{
  "terminal_id": "T-07",
  "original_package_no": "2026-09-01-03",
  "new_package_no": "2026-09-01-03F1",
  "expected_revision": 1,
  "site": "西山坡",
  "mappings": [
    {"short_code": "s1", "stratum_id": "XSP-01"},
    {"short_code": "s2", "stratum_id": "XSP-02"},
    {"short_code": "s3", "stratum_id": "XSP-03"}
  ],
  "assertions": [
    {"seq": 1, "stratum_a": "s1", "stratum_b": "s2", "relation": "earlier_than"},
    {"seq": 2, "stratum_a": "s2", "stratum_b": "s3", "relation": "earlier_than"}
  ]
}
```

- `original_package_no` / `new_package_no`：新包号须**未使用且不同于原包号**；
  原包须**存在、未被替换、证据全部仍生效**；
- 新包的 `site` / `mappings` / `assertions` 全部**沿用整包汇入规则**：短号不重复、
  序号不重复、端点只引本包短号、已登记地层站点须一致、同一终端短号跨包不可改绑；
- 新包证据编号仍按 `F:<base64url(终端)>:<base64url(新包号)>:<序号>` 稳定生成，
  与原包证据编号天然不碰撞（包号段不同）；
- 勘误先撤回原包全部证据、再落新包证据，随后对**最终断言集**做组内早于 / 闭环核验。

成功响应（HTTP 200，正式修订号恰好 +1；`evidence` 为新包证据，`correction` 给出
撤回证据与替换链，链上每节点含包号、状态、汇入修订号与替换指向）：

```json
{
  "site": "西山坡",
  "terminal_id": "T-07",
  "package_no": "2026-09-01-03F1",
  "revision": 2,
  "mappings": [
    {"short_code": "s1", "stratum_id": "XSP-01", "site": "西山坡", "already_registered": true},
    {"short_code": "s2", "stratum_id": "XSP-02", "site": "西山坡", "already_registered": true},
    {"short_code": "s3", "stratum_id": "XSP-03", "site": "西山坡", "already_registered": true}
  ],
  "evidence": [
    {"seq": 1, "evidence_id": "F:VC0wNw:MjAyNi0wOS0wMS0wM0Yx:000000000001",
     "stratum_a": "XSP-01", "stratum_b": "XSP-02", "relation": "earlier_than"},
    {"seq": 2, "evidence_id": "F:VC0wNw:MjAyNi0wOS0wMS0wM0Yx:000000000002",
     "stratum_a": "XSP-02", "stratum_b": "XSP-03", "relation": "earlier_than"}
  ],
  "applied": 2,
  "relation_changes": 1,
  "replayed": false,
  "original_package_no": "2026-09-01-03",
  "correction": {
    "original_package_no": "2026-09-01-03",
    "new_package_no": "2026-09-01-03F1",
    "original_revision": 1,
    "revision": 2,
    "retracted_evidence": [
      "F:VC0wNw:MjAyNi0wOS0wMS0wMw:000000000001",
      "F:VC0wNw:MjAyNi0wOS0wMS0wMw:000000000002"
    ],
    "replacement_chain": [
      {"package_no": "2026-09-01-03", "status": "superseded", "revision": 1,
       "superseded_by_package_no": "2026-09-01-03F1"},
      {"package_no": "2026-09-01-03F1", "status": "active", "revision": 2,
       "superseded_by_package_no": null}
    ]
  }
}
```

**原子性与历史可查**

- 撤回原包证据、新建地层 / 绑定、落新断言、最终核验、关系变化记录、修订号 +1、
  原包台账标记 `superseded`、勘误台账写入全部在**同一事务**；任一步失败整笔回滚——
  地层、绑定、断言、台账、修订都不变，**失败不占新包号**，修正后可按同一新包号重提；
- 原包**台账行与首次汇入响应永不修改**：`GET /field-packages/{终端}/{原包号}` 仍返回
  原 `response`（原修订号、原证据编号），仅额外带 `status=superseded`、
  `superseded_by_package_no` 与 `replacement_chain`；原包号对 `POST /field-packages`
  的**原请求重放仍返回首次汇入结果**（`replayed=true`）；
- 勘误前的**历史定版**（`/snapshots/...`）与**推演冻结视图**（`/projections/...`）
  结论保持勘误前口径，不受撤回影响；当前正式关系、后续定版 / 推演使用勘误后的新断言集；
- 替换链可多级延伸：新包之后仍可作为“原包”再被勘误，`replacement_chain` 从链上最早
  包一路列到当前 `active` 包；链上除现行包外每个原包都只能被勘误一次。

**幂等与复用**：勘误内容指纹口径与整包汇入一致（规范化 site / mappings / assertions，
不含 `expected_revision`、与数组顺序无关），幂等键为 **(终端, 原包号, 新包号)**。

| 情形 | 结果 |
| --- | --- |
| 同终端 + 同原包 + 同新包 + 同内容重试（任何修订号、任何时机） | HTTP 200，返回**首次勘误响应**原样（含原/新修订号、撤回证据、替换链），`replayed=true`，修订不再前进 |
| 同终端 + 同原包 + 同新包 + 异内容 | 409 `FIELD_CORRECTION_CONTENT_MISMATCH`（`details` 给原指纹与本次指纹） |
| 新包号与原包号相同 | 400 `FIELD_CORRECTION_SAME_PACKAGE` |
| 新包号已被该终端使用（active/superseded 均算） | 409 `FIELD_PACKAGE_NO_TAKEN` |
| 原包不存在 | 404 `FIELD_PACKAGE_NOT_FOUND` |
| 原包已被替换 | 409 `FIELD_PACKAGE_ALREADY_REPLACED`（`details` 给已替换到的包号） |
| 原包证据未全部仍生效 | 409 `FIELD_PACKAGE_EVIDENCE_INACTIVE`（`details.inactive_evidence` 给证据号与状态） |
| 首次勘误携带过期修订号 | 409 `STALE_REVISION`（幂等重试不受此限） |
| 最终关系组内早于 / 闭环冲突 | 409 `FIELD_CORRECTION_CONFLICT`（`details.conflicts[]` 附证据路径），一切不变、不占号 |
| 新包结构 / 站点 / 改绑 / 证据编号占用 | 沿用整包汇入的 `UNKNOWN_SHORT_CODE` / `DUPLICATE_SHORT_CODE` / `DUPLICATE_SEQUENCE` / `SITE_CONFLICT` / `SHORT_CODE_REBOUND` / `FIELD_EVIDENCE_TAKEN` |

勘误台账：

```bash
# 全部勘误摘要
curl "$BASE/field-packages/corrections"
# 按终端 + 原包过滤（一个原包至多一条成功勘误）
curl "$BASE/field-packages/corrections?terminal_id=T-07&original_package_no=2026-09-01-03"
# 单条勘误详情（撤回证据清单 + 首次勘误响应）
curl "$BASE/field-packages/corrections/T-07/2026-09-01-03/2026-09-01-03F1"
```

勘误产生的正式修订与正式批次完全同构：`revisions` 审计 `batch.source=
field_package_correction`（含原 / 新包号与撤回证据清单），`/revision-changes` 中对应
变化的 `source` 为 `field_package_correction`，原包证据在 `assertions` 表中置
`retracted`（`retracted_revision` 为勘误修订号），定版 / 推演 / 查询等既有能力无额外口径。

```bash
bash examples/field_package_corrections_demo.sh
```

### 关系查询响应

```json
{
  "stratum_a": "L1",
  "stratum_b": "L3",
  "relation": "earlier_than",
  "relation_label": "早于",
  "evidence_path": ["EV-001", "EV-002"],
  "path_detail": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active"}
  ],
  "revision": 1
}
```

`relation` 取值：`same_period`（同一期）/ `earlier_than`（早于）/ `later_than`（晚于）/ `undetermined`（未定）。

**同一次响应同一完整视图**：`GET /relationship?a=...&b=...` 在一条
`REPEATABLE READ READ ONLY` 只读快照事务内读取正式修订号、生效断言并推导关系结论、
最优证据路径与逐步明细。查询期间若有正式批次（含野外整包汇入 / 勘误、推演并入）
并发提交：

- 整个响应要么对应该批次**提交前**的完整视图，要么对应**提交后**的完整视图；
- 不会返回“旧 `revision` 配提交后新结论”的响应，也不会出现 `evidence_path` 引用
  某证据、但 `path_detail` 中该证据 `status` 已为 `retracted` 的响应；
- 查询只读，不写库、不递增修订号，不改变请求路径、查询参数与响应字段；地层未登记
  仍返回 400 `INVALID_STRATUM`；路径规则仍为证据数最少、并列按证据编号序列字典序最小。

推演关系查询 `GET /projections/{id}/relationship?a=...&b=...` 同口径：推演状态
`status`、`base_revision`、`projection_revision`、关系结论、最优证据路径与逐步明细
（`scope=base/projection`）取自该推演的同一冻结视图快照；并发推演批次在读取语句间
提交时同样只可能整体返回提交前或提交后的结果；已关闭推演（`merged` / `discarded`）
仍可查询冻结视图，地层不在推演基线内返回 400 `INVALID_STRATUM`、推演不存在返回
404 `PROJECTION_NOT_FOUND`。

```bash
# 并发提交下的同视图行为演示（含多线程并发查询校验、推演冻结视图、错误约定）
bash examples/relationship_view_demo.sh
```

### 历史定版关系查询：逐步明细按封存时断言返回（只读）

`GET /snapshots/{version}/relationship?a=...&b=...` 返回某一定版（快照）中两地层的
封存关系。定版之后若路径上的支撑证据被撤回——无论是**正式批次**
（`POST /batches`）、**野外整包勘误**（`POST /field-packages/corrections`）还是
**推演并入**（`POST /projections/{id}/merge`）——本查询的响应保持封存时口径：

- 关系结论与最优证据路径 `evidence_path` 取自该定版的冻结结论（`snapshot_conclusions`）；
- 逐步证据明细 `path_detail` 按**定版冻结的断言副本**（`snapshots.assertions`）
  逐步展开，**不读在线断言表**：每步的 `status` 恒为封存时的 `active`，与同一
  定版封存的成果包（`POST /packages`）证据明细**同一来源、逐步一致**（包内每步
  仅多一个 `in_selection` 标记）；
- 当前正式关系查询 `GET /relationship` 不受定版影响，继续显示现时状态
  （撤回后的结论与在线证据状态）；
- 输入（`version`、`a`、`b`）、响应字段、路径优选（证据数最少、并列按证据编号
  序列字典序最小）与错误约定不变：定版不存在 404 `SNAPSHOT_NOT_FOUND`、地层未登记
  400 `INVALID_STRATUM`；查询只读，不写断言或修订。

```bash
# 定版（修订 2 时：EV-001 L1~L2、EV-002 L2<L3、野外包证据 F:… L3<L4 均生效）
curl -X POST "$BASE/snapshots" -H 'Content-Type: application/json' -d '{"label": "二期定版"}'
# -> {"version": 1, "revision": 2, "conclusions": ...}

# 定版后陆续撤回路径证据：正式批次撤回 EV-002、野外包勘误撤回原包证据、推演并入撤回 EV-001
curl -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [{"op": "retract", "evidence_id": "EV-002"}]}'

# 历史定版查询：结论 / 路径 / 逐步明细仍是封存时口径（各步 status 恒为 active）
curl "$BASE/snapshots/1/relationship?a=L1&b=L4"
```

```json
{
  "stratum_a": "L1",
  "stratum_b": "L4",
  "relation": "earlier_than",
  "relation_label": "早于",
  "evidence_path": ["EV-001", "EV-002", "F:VC0wNw:UC0wMQ:000000000001"],
  "path_detail": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active"},
    {"evidence_id": "F:VC0wNw:UC0wMQ:000000000001", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than", "status": "active"}
  ],
  "version": 1,
  "snapshot_revision": 2
}
```

```bash
# 对照：当前正式关系显示现时状态（三条证据均已撤回，L1 与 L4 已未定）
curl "$BASE/relationship?a=L1&b=L4"
# -> {"relation": "undetermined", "evidence_path": null, "path_detail": [], "revision": 5, ...}

# 与同一定版封存的成果包证据明细逐步一致（包内每步仅多 in_selection 标记）
curl "$BASE/packages/1"

# 完整端到端演示（三类撤回来源后历史明细不变、成果包逐步比对、错误约定）
bash examples/snapshot_frozen_detail_demo.sh
```

### 证据撤回承受力分析：`POST /resilience/retraction-analysis`（只读）

考古人员核对当前地层关系时，需要知道它对**证据撤回**的承受力：若允许在**同一正式视图**
中撤回若干生效证据，最少撤回多少条才能让指定两层的确定关系**变为未定**，以及并列时应撤回
哪一组。请求须携带预期的当前正式修订号 `expected_revision`、两个不同的已登记地层，以及
可为空的受保护生效证据编号集合 `protected_evidence`（自动去重；这些证据不可撤回）：

```json
{
  "expected_revision": 1,
  "stratum_a": "T1",
  "stratum_b": "T4",
  "protected_evidence": []
}
```

- **候选范围**：只从“其余生效证据”中选择撤回——同一期证据与早于证据均可入选，
  `protected_evidence` 中的证据不可撤回；
- **最少撤回数**：关系由“同一期（无向、可传递）+ 早于（有向）”混合图上的路径支撑，
  使关系变未定 ⇔ 切断支撑它的**全部**路径。最少撤回数即混合证据图上的 s–t **最小割**；
  并列时给出**证据编号序列字典序最小**的一组 `suggested_retractions`；
- **同一期双向连通按一条证据撤回计数**：同一期证据是无向边，割中该证据只计 1 次；
- **替代证据路径仍成立时不判失效**：若受保护证据单独仍支撑某条路径（受保护子图上关系仍
  成立），目标无法失效，`status=unreachable`，`minimum_retraction_count` 为 `null`，
  `remaining_evidence_path` 给出仍成立的（字典序最小）证据路径；
- **原关系未定时**：`status=already_undetermined`，撤回数 `0`、集合为空；
- **模拟结果**：成功找到时 `status=retractions_found`，`simulated_relation` 恒为
  `undetermined`，并附撤回后路径（空）；`original_evidence_path` 为撤回前的最优证据路径，
  `original_path_detail` / `simulated_path_detail` 逐步展开原始断言；
- 反向查询（如原关系为 `later_than`）自动规整方向，结果与正向一致。

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "stratum_a": "T1",
  "stratum_b": "T4",
  "protected_evidence": [],
  "original_relation": "earlier_than",
  "original_relation_label": "早于",
  "original_evidence_path": ["RV-4"],
  "original_path_detail": [
    {"evidence_id": "RV-4", "stratum_a": "T1", "stratum_b": "T4", "relation": "earlier_than", "status": "active"}
  ],
  "reachable": true,
  "status": "retractions_found",
  "minimum_retraction_count": 2,
  "suggested_retractions": ["RV-1", "RV-4"],
  "simulated_relation": "undetermined",
  "simulated_relation_label": "未定",
  "simulated_evidence_path": [],
  "simulated_path_detail": []
}
```

上例中 `T1 早于 T4` 同时由直达证据 `RV-4` 与混合链 `RV-1（T1~T2）, RV-2, RV-3` 支撑，
两条路径互不共边，须各撤一条（共 2 条）；字典序最小的组合是 `[RV-1, RV-4]`。
受保护使目标不可达时：

```json
{
  "reachable": false,
  "status": "unreachable",
  "minimum_retraction_count": null,
  "suggested_retractions": [],
  "simulated_relation": "earlier_than",
  "simulated_relation_label": "早于",
  "remaining_evidence_path": ["RV-4"],
  "remaining_path_detail": [
    {"evidence_id": "RV-4", "stratum_a": "T1", "stratum_b": "T4", "relation": "earlier_than", "status": "active"}
  ]
}
```

明确拒绝：修订号过期（409 `STALE_REVISION`，`details.current_revision`）、地层未登记
（400 `INVALID_STRATUM`）、两层相同（400 `DUPLICATE_STRATUM`）、受保护证据不存在或非生效
（400 `INVALID_PROTECTED_EVIDENCE`，`details.not_found` / `details.not_active`）。
分析在 `REPEATABLE READ READ ONLY` 快照事务内完成，**不改断言、修订或变化记录**。

```bash
bash examples/resilience_demo.sh
```

### 分期骨架视图：`GET /periodization`（只读）

考古人员需要一次性查看**当前全部地层**在同一正式修订视图下的分期骨架。请求必须
携带**预期的当前正式修订号** `expected_revision`（查询参数，≥0）：

- **同一期（same_period）断言连成等价组**：对称、可传递；未被任何同一期断言连接
  的地层（含完全没有断言时）**各自成组**，全部已登记地层恰好出现一次。
  组内地层编号排序，全部组按**组内最小编号**排序，组序号 `group` 从 1 起；
- **组间早于关系图**：每条生效“早于”断言投影到其两端所在的等价组上，
  先把同一组间的**平行断言合并为一条边**，再做**传递约简**——
  当源组到目标组已存在“经其他组”（路径长度 ≥ 2）的先后关系时，该直接边可由
  传递推出，予以省略；只保留**不存在经其他组到达**的直接先后边；
- 每条保留边给出 `from_group` / `to_group`、双方组最小编号
  （`from_min_stratum` / `to_min_stratum`，方向即时间方向：源组早于目标组），
  以及该组间**全部生效的直接早于证据编号** `evidence`（按编号排序）；
- `omitted_edge_count`：**被传递关系省略的组间边数**（平行断言合并后计数，
  即“直接商图边数 − 保留边数”）；
- 返回顺序稳定：组按最小编号排序，边按 `(源组最小编号, 目标组最小编号)` 排序，
  证据按编号排序。

**修订号与一致性**：视图在 `REPEATABLE READ READ ONLY` 快照事务内读取，
修订号在事务内只读一次：`expected_revision` 与当前一致才返回，响应中的
`expected_revision`、`current_revision`、`revision` 恒相同；过期返回 **HTTP 409
`STALE_REVISION`**（`details.current_revision` 为当前修订号），**不输出任何混合
版本数据**——调用方取当前修订号后重新查询。该接口只读，不写库、不递增修订号、
不产生变化记录；批次、关系查询、定版、推演的既有请求与返回约定均未改变。

```bash
curl -s "$BASE/periodization?expected_revision=1"
```

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "groups": [
    {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"]},
    {"group": 2, "min_stratum": "L3", "strata": ["L3"]},
    {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"]},
    {"group": 4, "min_stratum": "L6", "strata": ["L6"]}
  ],
  "edges": [
    {
      "from_group": 1,
      "to_group": 2,
      "from_min_stratum": "L1",
      "to_min_stratum": "L3",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence": ["EV-002", "EV-020"]
    },
    {
      "from_group": 2,
      "to_group": 3,
      "from_min_stratum": "L3",
      "to_min_stratum": "L4",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence": ["EV-003"]
    }
  ],
  "omitted_edge_count": 1
}
```

上例中 `G1 早于 G3` 的直接断言（EV-010、EV-011）因 `G1 -> G2 -> G3` 已能推出而
被省略（平行两条只记为 1 条被省略边）。无断言时 `groups` 为各地层单元素组、
`edges` 为 `[]`、`omitted_edge_count` 为 `0`。完整端到端示例（无断言、平行合并、
传递省略、孤立地层、过期拒绝、撤回后重算）：

```bash
bash examples/periodization_demo.sh
```

### 分期试排：`POST /periodization/arrangement`（正式，只读）

考古人员试排**当前正式修订**的分期时，给出总期数与可选地层期位锚点，服务在
**同一只读快照视图**内读取地层与生效断言并求解：

- **输入**（JSON）：
  - `expected_revision`（≥0）：预期的当前正式修订号，过期返回 409 `STALE_REVISION`
    （`details.current_revision` 为当前修订号），不输出混合版本；
  - `total_periods`：总期数，取 **1～64**，期位自 1 起（超出由请求模型拒绝，HTTP 422）；
  - `anchors[]`（可选）：每项 `{stratum, period}`，把该地层（及其同一期等价组）固定在
    第 `period` 期；同一地层不可出现两次（400 `DUPLICATE_STRATUM`），期位须在
    **1～总期数**内（400 `INVALID_PERIOD_POSITION`），地层须已登记
    （400 `INVALID_STRATUM`）。
- **约束**：同一期等价组占同一期；早于关系源组期位**严格小于**目标组；未锚定的组可占
  1～总期数内任一期。等价组口径与 `/periodization` 骨架一致（组按最小编号排序）。
- **可行**（HTTP 200，`feasible=true`）：返回每个组的 `strata` 与
  `earliest_period` / `latest_period`——该组在**所有可行排布**中的最早 / 最晚期位
  （二者相等即期位被唯一确定；锚定组恒相等且 `anchored=true`）。这是界，**不是某一套
  任意排布**：界由 DAG 上的最早 / 最迟调度（ASAP/ALAP）求得，且 ASAP 排布与 ALAP
  排布各自都能扩展为完整可行排布，故这两个界是紧的。
- **不可行**（HTTP **200**，`feasible=false`）：**不返回任何组的期位**（不返回部分排布），
  仅在 `reasons[]` 给出全部可定位原因，每条含组链 `group_chain`、端点 `start`/`end`
  （锚点或首/末期边界）、所需 / 可用期位数与证据路径 `evidence_path`：

  | `type` | 含义 |
  | --- | --- |
  | `anchor_group_conflict` | 同一期等价组被锚点钉到不同期位；附该组全部锚点与组内同一期证据链 |
  | `anchor_precedence_conflict` | 先后链与锚点不相容：链端至少一侧是锚点（如两组被钉反序、锚点后容不下后继链）；`involved_anchors` 给关联锚点 |
  | `chain_exceeds_periods` | 先后链与期数不相容：一条不含锚点端的严格链所需期位数超过总期数 |

  `evidence_path` 沿组链取“同一期 + 早于”混合最优证据序列（证据数最少、并列字典序最小，
  口径与关系查询一致），端点固定为锚点地层；未锚定端取链端组的最小编号地层。

```bash
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L3", "period": 2}]
}'
```

```json
{
  "feasible": true,
  "total_periods": 4,
  "scope": "formal",
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "groups": [
    {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"],
     "earliest_period": 1, "latest_period": 1, "anchored": false, "anchors": []},
    {"group": 2, "min_stratum": "L3", "strata": ["L3"],
     "earliest_period": 2, "latest_period": 2, "anchored": true,
     "anchors": [{"stratum": "L3", "period": 2}]},
    {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"],
     "earliest_period": 3, "latest_period": 3, "anchored": false, "anchors": []}
  ]
}
```

不可行示例（总期数 2 容不下 3 个严格递增的组）：

```json
{
  "feasible": false,
  "total_periods": 2,
  "scope": "formal",
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "reasons": [
    {
      "type": "chain_exceeds_periods",
      "message": "先后链与期数不相容：3 个等价组须严格递增，至少需占 3 期，超过总期数 2",
      "group_chain": [
        {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"], "period": null},
        {"group": 2, "min_stratum": "L3", "strata": ["L3"], "period": null},
        {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"], "period": null}
      ],
      "start": {"kind": "boundary", "period": 1, "boundary": "first"},
      "end": {"kind": "boundary", "period": 2, "boundary": "last"},
      "required_periods": 3,
      "available_periods": 2,
      "involved_anchors": [],
      "evidence_path": ["EV-001", "EV-002", "EV-003"]
    }
  ]
}
```

### 推演冻结视图试排：`POST /projections/{id}/periodization/arrangement`（只读）

语义、请求体与返回结构与正式试排一致，差别仅在视图与修订号口径：

- 读取该推演**创建时固定的地层 / 基线断言**叠加推演内净变更（新增生效 + 基线撤回）；
- `expected_revision` 是**推演自身修订号**（自 0 起，随推演批次递增），过期返回
  409 `STALE_REVISION`（`details.projection_id`、`details.current_revision`）；
- **已关闭推演（`merged` / `discarded`）仍按其冻结视图分析**，响应回带 `status`；
  推演不存在返回 404 `PROJECTION_NOT_FOUND`；
- 响应额外含 `scope="projection"`、`projection_id`、`status`、`base_revision`；
  锚点地层以基线地层为准；分析不写任何表，推演状态与正式修订均不变。

```bash
curl -s -X POST "$BASE/projections/1/periodization/arrangement" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "total_periods": 3, "anchors": []}'
```

完整端到端示例（最早/最晚界、锚点拉直、三类不可行与证据路径、结构拒绝、
推演冻结视图、关闭后仍可读、全程只读）：

```bash
bash examples/arrangement_demo.sh
```



| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `INVALID_STRATUM` | 引用了未登记的地层 |
| 409 | `STALE_REVISION` | 批次携带的修订号过期（响应含当前修订号） |
| 409 | `DUPLICATE_EVIDENCE` | 证据编号重复（含已撤回的编号） |
| 409 | `ASSERTION_NOT_FOUND` | 撤回不存在或已撤回的断言 |
| 409 | `CONFLICT` | 整批核验发现组内早于 / 早于闭环（附冲突证据路径） |
| 409 | `STRATUM_EXISTS` | 地层重复登记 |
| 404 | `SNAPSHOT_NOT_FOUND` | 版本不存在 |
| 404 | `PROJECTION_NOT_FOUND` | 推演不存在 |
| 409 | `PROJECTION_CLOSED` | 推演已关闭（merged/discarded），不可再操作 |
| 409 | `MERGE_TARGET_RETRACTED` | 并入失败：撤回目标证据已在最新正式状态被撤回 |
| 409 | `MERGE_EVIDENCE_TAKEN` | 并入失败：推演新增证据编号已被正式历史占用 |
| 409 | `MERGE_CONFLICT` | 并入核验发现关系冲突（附冲突证据路径），双方不变 |
| 400 | `INVALID_CURSOR` | 变化追踪分页游标无效 |
| 400 | `INVALID_PERIOD_POSITION` | 分期试排：锚点期位越界（不在 1～总期数） |
| 400 | `INVALID_PROTECTED_EVIDENCE` | 撤回承受力分析：受保护证据不存在或非生效（`details` 区分 not_found/not_active） |
| 400 | `EMPTY_STRATA` | 成果包：地层集合为空 |
| 400 | `NOT_ENOUGH_STRATA` | 成果包：少于两个地层编号 |
| 400 | `DUPLICATE_STRATUM` | 成果包：地层编号重复 / 分期试排：锚点地层重复 |
| 404 | `PACKAGE_NOT_FOUND` | 成果包不存在 |
| 400 | `UNKNOWN_SHORT_CODE` | 野外包：断言端点引用了本包未对照的短号（details 带 seq/short_code） |
| 400 | `DUPLICATE_SHORT_CODE` | 野外包：包内短号对照重复 |
| 400 | `DUPLICATE_SEQUENCE` | 野外包：包内断言序号重复 |
| 409 | `SITE_CONFLICT` | 野外包：对照目标已登记且站点与本包不一致 |
| 409 | `SHORT_CODE_REBOUND` | 野外包：同一终端的同一短号跨包被请求改绑到不同全库编号 |
| 409 | `FIELD_EVIDENCE_TAKEN` | 野外包：稳定证据编号已被历史断言占用（含已撤回） |
| 409 | `FIELD_CONFLICT` | 野外包：整包最终关系核验冲突（附证据路径），地层/绑定/断言均不落地 |
| 409 | `FIELD_PACKAGE_CONTENT_MISMATCH` | 野外包：同终端同包号已以不同内容汇入，异内容复用拒绝 |
| 400 | `FIELD_CORRECTION_SAME_PACKAGE` | 野外包勘误：新包号与原包号相同 |
| 409 | `FIELD_PACKAGE_NO_TAKEN` | 野外包勘误：新包号已被该终端使用 |
| 409 | `FIELD_PACKAGE_ALREADY_REPLACED` | 野外包勘误：原包已被替换，不可再次勘误 |
| 409 | `FIELD_PACKAGE_EVIDENCE_INACTIVE` | 野外包勘误：原包证据未全部仍生效（已撤回 / 缺失，details 列出证据及状态） |
| 409 | `FIELD_CORRECTION_CONFLICT` | 野外包勘误：撤回原包证据并汇入新包后的最终关系核验冲突（附证据路径），一切不变 |
| 409 | `FIELD_CORRECTION_CONTENT_MISMATCH` | 野外包勘误：同原包同新包已以不同内容勘误成功，异内容复用拒绝 |
| 404 | `FIELD_CORRECTION_NOT_FOUND` | 勘误台账中不存在该 (终端, 原包号, 新包号) |
| 404 | `FIELD_PACKAGE_NOT_FOUND` | 野外汇入包台账中不存在该终端 + 包号（勘误原包不存在同码） |
| 400 | `HOLD_DUPLICATE_EVIDENCE` | 临时保全：请求中的证据编号重复 |
| 400 | `INVALID_HOLD_EVIDENCE` | 临时保全：证据不存在或当前非生效（details.invalid_evidence 逐条给状态） |
| 404 | `HOLD_NOT_FOUND` | 临时保全：保全记录不存在 |
| 403 | `INVALID_HOLD_CREDENTIAL` | 临时保全：解除凭据错误 |
| 409 | `HOLD_ALREADY_RELEASED` | 临时保全：保全已解除，重复解除拒绝 |
| 409 | `EVIDENCE_HELD` | 普通批次 / 提交前预检的撤回目标仍被生效临时保全覆盖（整批拒绝，details 给命中证据与保全记录） |
| 409 | `FIELD_PACKAGE_EVIDENCE_HELD` | 野外包勘误将撤回受保全覆盖的原包证据（整笔拒绝，台账/修订不变） |
| 409 | `MERGE_EVIDENCE_HELD` | 推演并入将把受保全覆盖证据撤回到正式库（并入拒绝，正式与推演均不变） |

## 正式修订关系变化追踪

考古人员需要按正式修订追踪**关系结论及支撑证据**的变化。服务对每次成功的
**正式断言批次**（`POST /batches`）、**推演并入产生的正式修订**
（`POST /projections/{id}/merge` 且实际递增修订号）、**野外整包汇入产生的正式修订**
（`POST /field-packages`）与**野外整包勘误替换产生的正式修订**
（`POST /field-packages/corrections`）记录变化明细。

**记录口径**

- 同一地层对按地层编号固定顺序只列一次：返回中恒有 `stratum_a < stratum_b`；
- 记录条件：提交前后**关系变化**（同一期 / 早于 / 晚于 / 未定之间）**或**
  关系不变但**最优支撑路径改变**（路径选取沿用现有规则：证据数最少、并列按证据
  编号序列字典序最小）；
- 变为未定时新路径为空数组 `[]`；首版从未定变为确定结论时旧路径也为 `[]`；
- 旧侧关系与路径按**该修订提交前的当时状态**推导：后来才被撤回的证据在旧侧仍按
  当时生效解释（无需回放后续撤回）；
- **失败批次不产生记录**（整批拒绝、原状态不变、修订号不增）；
  **推演内编辑不产生正式记录**（只动 `projection_*` 表）；
  **无净变更并入不产生记录**（只关闭推演、不递增正式修订号）；
- 成功修订也可能零变化（如撤回一条从未进入最优路径的平行断言）：修订号照常递增、
  审计与封口照写，但没有变化行。

**原子性与并发**

- 变化记录与断言落库、修订号递增在**同一事务**内提交，读不到“半批结果”；
  每次成功批次对计数器行加排他锁，并发提交串行化，修订号连续、不重不漏。

**变化计算：先按生效断言定位候选对，再精算（不全库逐对重算）**

- 在大量彼此无关的地层已登记后，一次局部断言变更不再对全库所有地层对逐对
  重跑 Dijkstra；
- **定位**：取提交前后生效断言集的差异（本修订新增 / 撤回的证据）。对每条变更
  证据 `e`（u→v，同一期证据按正反两弧、沿同期关系无向扩散），在其生效的一侧
  求两个锥集——`Anc(u)`（能沿“同期 + 早于”混合路径走到 u 的端点）与
  `Desc(v)`（从 v 能走到的端点），候选对即各变更证据两侧锥集笛卡尔积的并。
  原理：若某对 (x, y) 的走法集合前后有差异，则必有一条只在一侧存在的走法，
  它含某条变更证据弧 (u, v)，于是 x∈Anc(u)、y∈Desc(v)，该对必在候选集中；
- **精算**：仅对候选对沿用既有 `Derivation` 口径重算关系结论与最优路径
  （证据数最少、并列按证据编号序列字典序最小），逐条与旧侧比对。候选之外的
  对前后走法集合完全相同，结论与最优路径必不变，直接跳过；
- **覆盖扩散**：同一期合并 / 拆分经同期边无向锥集覆盖整个受影响等价类，
  早于路径改道（新增捷径、撤回致路径换边）经前 / 后方向锥集覆盖全部走法
  改变的对；真实变化很多（长链接入、整类合并）时候选对相应增多，工作量随
  实际变化规模增长，而与全库规模解耦；
- **口径统一**：普通批次（`POST /batches`）、推演并入
  （`POST /projections/{id}/merge`）、野外包汇入与勘误
  （`POST /field-packages*`）、服务启动历史回填共用同一计算，记录的关系、
  最优路径、顺序、`source` 与修订号与旧逐对算法逐条一致；查询与游标格式不变。
- 参考：`tests/test_relation_diffs_equivalence.py`（3000+ 随机 / 构造场景与
  全库逐对参照实现逐条比对）、`tests/bench_relation_diffs.py`（局部变更基准：
  2000 地层约千倍加速，万级地层由分钟级降至亚秒级）。

**游标分页** `GET /revision-changes`

- 参数：`limit`（每页 1–1000，默认 100）、`cursor`（首屏省略，后续原样回传
  上一页的 `next_cursor`，为不透明令牌）；
- **同一轮固定正式修订上界**：首屏把当前修订号固定为 `bound_revision` 并编码进
  游标；翻页期间发生的并发提交不会进入本轮窗口，因此整轮遍历不漏项、不重复；
  本轮读完（`has_more=false`、`next_cursor=null`）后重新发一个不带 `cursor` 的
  首屏请求，即可开启覆盖新修订的下一轮；
- 每页在 `REPEATABLE READ READ ONLY` 快照事务内读取。

响应示例：

```json
{
  "changes": [
    {
      "revision": 1,
      "stratum_a": "L1",
      "stratum_b": "L3",
      "old_relation": "undetermined",
      "new_relation": "earlier_than",
      "old_relation_label": "未定",
      "new_relation_label": "早于",
      "old_evidence_path": [],
      "new_evidence_path": ["EV-001", "EV-002"],
      "source": "formal_batch"
    }
  ],
  "bound_revision": 1,
  "limit": 100,
  "has_more": false,
  "next_cursor": null
}
```

`source`：`formal_batch`（正式断言批次）/ `projection_merge`（推演并入产生的
正式修订）/ `field_package`（野外整包汇入产生的正式修订）/
`field_package_correction`（野外整包勘误替换产生的正式修订）。

**历史修订可追溯**：服务启动时幂等检查 `revisions` 审计，对部署前已存在、尚未
追踪的历史修订按当时断言集（`added_revision` / `retracted_revision` 回放）重算
旧/新关系与最优路径并回填。每个待补修订都以**紧邻上一正式修订**（无论该上一修订
是否已封口）提交时刻的生效断言为前态、以本修订提交时刻为后态——**已封口修订与
待补修订交错**时也不例外：例如修订 1 已封口、修订 2 撤回证据使关系变未定且修订 2
待补，回填仍以修订 1 的当时生效断言（而非空库）为前态，撤回不会被漏记；否则成果包
复核的“首次偏离”会被错判到后续恢复修订。零变化修订同样封口（只是没有变化行）；
只补未封口修订，重复启动与在线提交交错（双方都在事务内先锁修订计数器行）均不重写、
不漏记、不重复。推演并入的历史修订 `source` 按审计记录还原为 `projection_merge`，
野外整包汇入 / 勘误分别还原为 `field_package` / `field_package_correction`。
再次启动不会重复（按修订封口）。

```bash
# 首屏
curl "$BASE/revision-changes?limit=100"
# 翻页：原样回传 next_cursor
curl "$BASE/revision-changes?limit=100&cursor=eyJiIjo1LCJyIjoxLCJzIjozLCJ2IjoxfQ"

# 完整端到端示例（含失败批次 / 推演并入 / 路径变化 / 分页上界）
bash examples/changes_demo.sh
# 封口/待补交错下的启动回填核查示例（撤回不漏记 + 成果包首次偏离 + 交错封口重启回填 + 幂等）
bash examples/revision_changes_backfill_demo.sh
```

## 推演（证据修订沙盘）

考古人员可在**不改动正式断言**的前提下推演证据修订：

1. **创建即固定基线**：`base_revision`（正式修订号）、`base_strata`（已登记地层）、
   `base_assertions`（生效断言快照）。推演创建之后正式侧新增的地层 / 断言对该推演不可见。
2. **推演内批次语义与正式批次一致**：
   - 请求携带的是**推演修订号**（自 0 起，每次成功批次 +1），过期返回 `STALE_REVISION`；
   - 按整批应用后的推演最终关系核验，组内早于 / 早于闭环整批拒绝，附冲突证据路径；
   - **正式历史已用证据编号不可复用**（含已撤回，查正式 `assertions`），
     **推演新增编号在本推演内不可复用**（含在推演内已撤回）；
   - 可撤回推演自己新增的断言，也可撤回基线断言；撤回同样只置状态、不可重复。
3. **推演查询**：`GET /projections/{id}/relationship` 返回推演视角下的关系与证据路径，
   `path_detail[].scope` 标注每步证据来源：`base`（基线）/ `projection`（推演新增）。
   查询在 `REPEATABLE READ READ ONLY` 只读快照事务内完成：推演状态、基线 / 推演修订号、
   关系结论、最优证据路径与逐步明细取自同一冻结视图，并发推演批次在读取语句之间提交
   时只可能整体读到提交前或提交后的结果（不混版）；已关闭推演（merged/discarded）
   仍可查询其冻结视图。
4. **并入（merge）**：把推演相对基线的**净变更**（推演内仍生效的新增 + 被撤回的基线断言；
   “新增后又撤回”不产生净变更）应用到**最新**正式状态并重新核验：
   - 基线撤回目标已在正式状态撤回 → `MERGE_TARGET_RETRACTED`；
   - 新增编号已被正式占用 → `MERGE_EVIDENCE_TAKEN`；
   - 净变更应用后出现组内早于 / 早于闭环 → `MERGE_CONFLICT`（附证据路径）；
   - 任一失败：**正式状态与推演均不变**。
   - 核验通过：作为一次正式批次原子提交（写 `assertions`、`revisions` 审计，
     批次记录带来源 `source=projection_merge`），正式修订号 +1 并关闭推演；
     该修订的关系变化同样可在 `/revision-changes` 追踪（`source=projection_merge`）；
   - 净变更为空：只关闭推演，**不递增正式修订号**。
5. 并入与放弃都会关闭推演；关闭后其冻结视图仍可查询，但不能再提交批次或并入。

### 推演请求示例

创建并查询：

```bash
curl -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "二期证据修订推演"}'
# -> {"id": 1, "base_revision": 1, "projection_revision": 0, "status": "open", ...}

curl "$BASE/projections/1/relationship?a=L1&b=L3"
```

推演内批次（`expected_revision` 为推演修订号）：

```bash
curl -X POST "$BASE/projections/1/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"},
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}'
# -> {"id": 1, "projection_revision": 1, "applied": 2}（正式状态不变）
```

并入：

```bash
curl -X POST "$BASE/projections/1/merge"
# 成功 -> {"id": 1, "merged": true, "applied": 2, "revision": 2,
#          "revision_incremented": true, "relation_changes": 3,
#          "net_changes": {"added": [...], "retracted": [...], "count": 2}}
# 冲突 -> HTTP 409 MERGE_CONFLICT，details.conflicts[].evidence_path 给出证据路径
```

完整流程（含正式状态不受影响、编号复用拒绝、三类并入失败、空净变更只关闭）见
`examples/projection_demo.sh`：

```bash
bash examples/projection_demo.sh
```

## 专题成果包（定版地层关系封存转交）

考古人员可把**某个定版**中的一组地层关系封存为可转交复核的专题成果包。成果包是
**定版时刻的只读摘录**，独立于正式状态继续演化。

**封存口径**

- 请求给出定版编号 `version` 与至少两个、不重复的**已登记**地层编号 `strata`；
- 包内只列所选地层**按编号排序**后的无序两两关系（每对取编号较小者为
  `stratum_a`，`stratum_a < stratum_b`）；
- 关系结论与最优证据路径与**该定版查询**（`GET /snapshots/{version}/relationship`）
  严格一致：确定关系直接取自定版冻结结论，路径为该定版选取的最优证据序列
  （证据数最少、并列按证据编号字典序最小）；
- 每条确定结论的 `path_detail` 携带路径上**按顺序**的原始断言（证据编号、两端
  地层、关系），原始断言取自**定版冻结的断言副本**（`snapshots.assertions`），
  不读取在线断言表，因此其 `status` 恒为封存时的 `active`；
- 路径**途经但未入选**的地层只随证据保留：包顶层给出 `transit_strata`，每步断言
  用 `in_selection`（该步两端是否都在入选集合内）标注；不会因为途经而扩展出
  额外的两两关系；
- **未定关系**只返回 `relation=undetermined` 与中文标签，**不附** `evidence_path`
  和 `path_detail`；
- `evidence` 是包内所有路径引用到的去重原始断言清单（按证据编号排序），便于脱离
  本服务转交复核；`content_hash` 是包内容（定版、修订、入选集合、pairs、evidence、
  transit_strata）的 sha256 指纹，接收方可据此校验包未被篡改。

**身份与不可变性**

- 同一 `version` + **同一地层集合**无论输入顺序、是否重复输入，都只生成**一份**
  成果包（按排序去重后的集合取指纹 `strata_key`，数据库唯一约束
  `(version, strata_key)` 保证；并发封存也在行锁下串行）；
- 首次封存返回 **HTTP 201**；同集合重复封存幂等返回**同一份**（相同 `id` 与
  `content_hash`），HTTP **200**，响应带 `already_exists=true`；
- 包内容在封存事务内一次性冻结，之后**永不修改、永不删除**：正式断言撤回、后续
  修订、新定版都不会改变已有包（旧包仍展示定版当时的结论与当时生效断言）。

**拒绝语义（校验失败明确拒绝、事务回滚，不留半成品）**

| 情形 | HTTP | code |
| --- | --- | --- |
| `strata` 为空 | 400 | `EMPTY_STRATA` |
| 少于两个地层 | 400 | `NOT_ENOUGH_STRATA` |
| 地层编号重复 | 400 | `DUPLICATE_STRATUM` |
| 含空编号 / 地层未登记 | 400 | `INVALID_STRATUM` |
| 定版不存在 | 404 | `SNAPSHOT_NOT_FOUND` |
| 缺少 `version`/`strata` 字段、类型不符 | 422 | FastAPI 请求体校验 |

### 封存请求与响应

```bash
# 选 L1、L5、L6；L1 早于 L5 的证据链途经未入选的 L2、L3、L4
curl -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L1", "L5", "L6"]}'
```

```json
{
  "id": 1,
  "version": 1,
  "snapshot_revision": 1,
  "label": "二期定版",
  "strata": ["L1", "L5", "L6"],
  "pairs": [
    {
      "stratum_a": "L1",
      "stratum_b": "L5",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence_path": ["EV-001", "EV-002", "EV-003", "EV-004"],
      "path_detail": [
        {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active", "in_selection": false},
        {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active", "in_selection": false},
        {"evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than", "status": "active", "in_selection": false},
        {"evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period", "status": "active", "in_selection": false}
      ]
    },
    {"stratum_a": "L1", "stratum_b": "L6", "relation": "earlier_than", "relation_label": "早于", "...": "..."},
    {"stratum_a": "L5", "stratum_b": "L6", "relation": "undetermined", "relation_label": "未定"}
  ],
  "evidence": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"}
  ],
  "transit_strata": ["L2", "L3", "L4"],
  "content_hash": "cfa6dd4e0236a56f9b4f18f1fbeeaed6f8913451d2329ac46f007f05d379fd15",
  "created_at": "2026-10-01T01:24:31.192399+00:00",
  "already_exists": false
}
```

- 乱序等价：`{"version":1,"strata":["L6","L1","L5"]}` 与上例是**同一个集合**，
  返回同一 `id`、同一 `content_hash`（HTTP 200，`already_exists=true`）；
- 列表 `GET /packages` 只给摘要（不含 `pairs` 明细）；
  详情 `GET /packages/{id}` 返回完整冻结内容，不存在返回 404 `PACKAGE_NOT_FOUND`。

完整端到端演示（封存、乱序幂等、未定无路径、撤回 + 新定版后内容不变、各类拒绝）：

```bash
bash examples/packages_demo.sh
```

## 成果包交付前复核（只读）

成果包封存后，正式断言可能经历撤回、新增与多次定版。交付前复核人员需要回答两个问题：
**封存结论现在能否沿用**？**若曾偏离，是从哪个修订开始、由什么操作引起**？
`POST /packages/{package_id}/reconcile` 在**同一 `REPEATABLE READ READ ONLY` 快照事务**
内完成全部读取，包、修订号、当前生效断言与变化台账取自同一视图，不输出混合版本数据。

请求体只携带预期的当前正式修订号（包编号在路径中）：

```json
{"expected_revision": 4}
```

**逐对比较口径**

- 封存侧直接取包内冻结内容（`sealed`：封存关系与封存最优路径；未定封存路径按 `[]`）；
- 当前侧（`current`）由**当前全部生效断言**重新推导，**不按包入选集合裁剪证据图**：
  路径途经未入选地层的证据同样参与推导（与 `/relationship` 同一推导引擎，最少证据数、
  并列证据编号序列字典序最小），并附当前 `path_detail`（逐步当前生效断言，含 `status`）；
  当前未定时 `evidence_path` 为 `[]`；
- 分类（每对恰好一类）：

| `comparison` | 含义 |
| --- | --- |
| `conclusion_changed` | 封存关系与当前关系不同（同一期 / 早于 / 晚于 / 未定之间变化） |
| `path_only_changed` | 关系一致，但最优证据路径不同 |
| `identical` | 关系与路径都与封存一致 |

**首次偏离（`first_deviation`）**

- 在 `/revision-changes` 台账中扫描**封存修订（`snapshot_revision`）之后**该对
  （按地层编号固定顺序）的记录，取第一条**修订后状态（new_relation/new_path）与封存
  结果不一致**的记录：结论偏离或仅路径偏离都算；
- **已恢复一致也保留**：即使当前 `comparison=identical`，历史上首次偏离的修订号、
  来源（`formal_batch` / `projection_merge` / `field_package` /
  `field_package_correction`）、当时修订后的关系/路径与上一状态仍完整给出；
  从未偏离为 `null`；
- 零变化修订（如撤回从未进入最优路径的平行断言）本就不产生台账行，不会误判为偏离。

响应示例（包 1 封存于修订 1；修订 2 路径改变，修订 3 结论改变，修订 4 恢复）：

```json
{
  "package_id": 1,
  "version": 1,
  "snapshot_revision": 1,
  "label": "二期定版",
  "strata": ["L1", "L5", "L6"],
  "transit_strata": ["L2", "L3", "L4"],
  "content_hash": "5d58a936…",
  "expected_revision": 4,
  "current_revision": 4,
  "revision": 4,
  "summary": {
    "total_pairs": 3,
    "conclusion_changed": 0,
    "path_only_changed": 0,
    "identical": 3,
    "ever_deviated": 3
  },
  "pairs": [
    {
      "stratum_a": "L1",
      "stratum_b": "L5",
      "comparison": "identical",
      "comparison_label": "完全一致",
      "sealed": {"relation": "earlier_than", "relation_label": "早于",
                 "evidence_path": ["RV-001", "RV-002", "RV-003", "RV-004"]},
      "current": {"relation": "earlier_than", "relation_label": "早于",
                  "evidence_path": ["RV-001", "RV-002", "RV-003", "RV-004"],
                  "path_detail": [ ]},
      "first_deviation": {
        "revision": 2,
        "source": "formal_batch",
        "source_label": "正式断言批次",
        "previous_relation": "earlier_than",
        "relation": "earlier_than",
        "previous_evidence_path": ["RV-001", "RV-002", "RV-003", "RV-004"],
        "evidence_path": ["RV-010"]
      }
    }
  ]
}
```

调用：

```bash
curl -s -X POST "$BASE/packages/1/reconcile" \
  -H 'Content-Type: application/json' -d '{"expected_revision": 4}'
```

- 预期号与当前不一致：**HTTP 409 `STALE_REVISION`**，`details.current_revision` 给当前号，
  不输出任何比较结果（取当前号后重查）；
- 成果包不存在：**HTTP 404 `PACKAGE_NOT_FOUND`**；
- `expected_revision` 缺失 / 为负：HTTP 422（请求体校验）；
- 核对**只读**：不写库、不递增修订号、不产生变化记录，成果包内容指纹、断言状态均不变。

完整端到端演示（完全一致 / 仅路径改变 / 结论改变、恢复一致后保留曾偏离、
四类变化来源、过期与不存在拒绝、只读校验）：

```bash
bash examples/package_reconciliation_demo.sh
```

“已封口修订与待补修订交错导致撤回漏记、首次偏离错判”的专项修复核查
（在线造数 → 变化分页确认撤回行 → 复核确认 first_deviation → 模拟交错封口
重启回填 → 二次重启幂等）：

```bash
bash examples/revision_changes_backfill_demo.sh
# 自动化等价回归（嵌入式 PostgreSQL，无需手工重启）：
PYTHONPATH=/tmp/pylibs python3 tests/test_revision_changes_backfill.py
```

## 调用示例

端到端 curl 演示（覆盖登记、批次、推导查询、冲突拒绝、撤回、定版与旧版不可变）：

```bash
bash examples/demo.sh
```

两类关系查询“同一次响应同一完整视图”演示（正式查询修订号/结论/路径/逐步明细同视图、
并发批次在读取间提交时的不混版校验、推演冻结视图、已关闭推演可查、未知地层 / 未知
推演错误约定）：

```bash
bash examples/relationship_view_demo.sh
```

分期骨架视图演示（无断言各自成组、同一期等价组、平行断言合并、
传递边省略、孤立地层、过期修订号拒绝、撤回后骨架重算）：

```bash
bash examples/periodization_demo.sh
```

分期试排演示（每组最早/最晚期位界、锚点拉直、同组锚点矛盾 /
先后链与锚点 / 先后链与期数三类不可行及证据路径、结构拒绝、
推演冻结视图与关闭后可读、全程只读）：

```bash
bash examples/arrangement_demo.sh
```

提交前预检演示（原批可通过 / 建议最少撤回 / 预算不足附冲突路径 /
新断言自成矛盾不可修复 / 拒绝语义沿用正式批次 / 临时保全联动：直接撤回受保全证据
按 EVIDENCE_HELD 拒绝、额外撤回建议排除受保全证据、解除后恢复建议 / 全程只读）：

```bash
bash examples/preflight_demo.sh
```

证据撤回承受力分析演示（最小割最少撤回 / 字典序最小序列 / 同一期按一条计数 /
替代路径仍成立不可达 / 原关系未定返回零和空集 / 修订、地层、受保护证据无效明确拒绝）：

```bash
bash examples/resilience_demo.sh
```

推演（沙盘）端到端演示（创建固定基线、推演批次、关系查询、并入成功与三类并入失败、
净变更为空只关闭、放弃推演）：

```bash
bash examples/projection_demo.sh
```

正式修订关系变化追踪演示（正式批次 / 路径变化 / 失败批次不产生记录 /
推演内编辑与并入 / 游标分页与同一轮上界）：

```bash
bash examples/changes_demo.sh
```

历史修订启动回填修复核查演示（已封口 / 待补修订交错时撤回不被漏记、
成果包首次偏离=撤回修订、模拟交错封口重启回填、二次重启不重写 / 不重复）：

```bash
bash examples/revision_changes_backfill_demo.sh
```

专题成果包封存演示（定版选层封存、路径途经未入选地层、乱序幂等一份、
未定不附路径、撤回与新定版后不可变、空 / 重复 / 未登记 / 定版不存在明确拒绝）：

```bash
bash examples/packages_demo.sh
```

成果包交付前复核演示（逐对结论改变 / 仅路径改变 / 完全一致、首次偏离修订号
与四类变化来源、恢复一致仍保留曾偏离、包不存在与过期修订拒绝、核对全程只读）：

```bash
bash examples/package_reconciliation_demo.sh
```

野外终端离线整包汇入演示（短号随包建层与跨包绑定、稳定证据编号、整包核验附冲突路径、
同包同内容幂等返回原结果 / 异内容拒绝、未知短号 / 重复序号 / 站点冲突 / 改绑 /
过期修订明确报错）：

```bash
bash examples/field_packages_demo.sh
```

已汇入野外整包勘误演示（单事务撤回原包证据 + 新包汇入、修订只 +1、同键同内容幂等 /
异内容拒绝、新包号占用 / 原包不存在 / 原包已替换 / 原证据已撤回 / 最终核验冲突明确报错、
失败一切不变且不占号、原包台账与原请求重放保持首次结果、替换链与勘误台账、变化追踪来源）：

```bash
bash examples/field_package_corrections_demo.sh
```

争议证据临时保全演示（一次性解除凭据、查询不泄露凭据、多份保全覆盖须全部解除、
普通批次 / 野外包勘误 / 推演并入撤回整笔拒绝附命中证据与保全记录、推演内试验撤回
仍可进行、错误凭据 / 无效证据 / 过期修订 / 重复解除明确拒绝、保全不递增修订）：

```bash
bash examples/evidence_holds_demo.sh
```

Python 客户端示例：

```bash
pip install requests
python3 examples/client_demo.py
```

## 项目结构

```
app/
  main.py         # FastAPI 路由与错误处理
  service.py      # 正式批次核验、关系查询（只读快照同视图）、定版快照
  field.py        # 野外终端离线整包汇入与勘误替换：短号建层/跨包绑定、稳定证据编号、整批核验、包台账幂等、撤回替换链
  holds.py        # 争议证据临时保全：一次性解除凭据（哈希存证）、生效保全来源查询、正式撤回三路径拦截与预检只读拦截、行锁同序裁决
  preflight.py    # 提交前只读预检：求消除本批冲突所需撤回的最少生效旧证据（预算 0～3；受保全证据不进建议，直接撤回按 EVIDENCE_HELD 拒绝）
  resilience.py   # 证据撤回承受力（只读）：混合证据图最小割求让两层关系变未定的最少撤回（含受保护集合）
  packages.py     # 专题成果包：定版地层关系封存（冻结、集合幂等、不可变、可转交复核）
  reconciliation.py # 成果包交付前只读复核：封存结论/最优路径与当前结果逐项比较、首次偏离追溯
  projections.py  # 推演（沙盘）：创建固定基线、推演批次、关系查询（冻结快照同视图）、并入核验
  periodization.py # 分期骨架视图（只读）：同一期等价组、组间传递约简直接早于边
  arrangement.py   # 分期试排（只读）：总期数+锚点约束下每组在所有可行排布中的最早/最晚期位
  changes.py      # 正式修订变化追踪：锥集候选定位 + 候选对精算、游标分页、启动历史回填
  derivation.py   # 推导引擎：并查集等价类、DAG 校验、最优证据路径、分期骨架、分期试排
  db.py           # 连接池与模式初始化（启动时自动建表、幂等回填）
  models.py       # 请求模型
examples/
  demo.sh           # curl 端到端示例（批次/查询/冲突/撤回/定版）
  relationship_view_demo.sh # 两类关系查询同视图示例（并发提交不混版/推演冻结视图/已关闭可查/错误约定）
  snapshot_frozen_detail_demo.sh # 历史定版查询冻结明细示例（三类撤回来源后明细不变/成果包逐步比对/错误约定）
  periodization_demo.sh # 分期骨架视图示例（等价组/平行合并/传递省略/过期拒绝/撤回重算）
  arrangement_demo.sh # 分期试排示例（最早/最晚界、锚点、三类不可行证据链、推演冻结视图）
  preflight_demo.sh # 提交前只读预检示例（最少撤回/预算不足/新断言自相矛盾/拒绝/保全联动：EVIDENCE_HELD 与建议排除受保全证据/只读校验）
  resilience_demo.sh # 撤回承受力分析示例（最小割/字典序最小/受保护不可达/原关系未定/拒绝）
  projection_demo.sh # 推演端到端示例（沙盘批次、并入成功与三类失败、空净变更）
  changes_demo.sh  # 修订变化追踪示例（变化/路径改变/失败批次/并入/游标分页）
  revision_changes_backfill_demo.sh # 封口/待补交错启动回填修复核查（撤回不漏记/首次偏离/重启幂等）
  packages_demo.sh # 专题成果包示例（封存/幂等/不可变/拒绝分支）
  package_reconciliation_demo.sh # 成果包交付前复核示例（三类比较/首次偏离/曾偏离保留/只读拒绝）
  field_packages_demo.sh # 野外终端离线整包汇入示例（短号绑定/稳定编号/整批核验/幂等/拒绝分支）
  field_package_corrections_demo.sh # 已汇入整包勘误示例（撤回+新包单事务/幂等/替换链/台账/冲突回滚/历史可查）
  evidence_holds_demo.sh # 争议证据临时保全示例（一次性凭据/状态与来源查询/多保全重叠/撤回拦截三路径/推演内撤回不受限）
  client_demo.py    # Python 客户端示例
tests/
  test_relation_diffs_equivalence.py # 变化追踪候选精算 vs 全库逐对口径等价性（纯函数，无需数据库）
  test_revision_changes_backfill.py  # 启动回填前态修复回归（真实 PostgreSQL：封口/待补交错撤回、首次偏离复核、零变化封口、重复启动幂等、回填与在线提交交错/持锁等待）
  test_preflight_holds.py            # 预检与临时保全联动回归：EVIDENCE_HELD 同构拒绝、受保全证据排除、解除恢复（内存假连接，无需数据库）
  test_relationship_view_consistency.py # 两类关系查询同视图回归（内存假连接：注入并发提交，断言修订号/结论/路径明细不混版）
  test_relationship_view_consistency_e2e.py # 同上的真实 PostgreSQL 端到端验证（语句门闩确定性交错 + 多线程压力）
  test_snapshot_relationship_frozen_detail.py # 历史定版查询冻结明细回归（内存假连接：撤回后明细保持封存口径/与成果包同源/正式查询仍现态）
  bench_relation_diffs.py            # 大量无关地层 + 局部变更性能基准（纯函数）
Dockerfile
docker-compose.yml
requirements.txt
```
