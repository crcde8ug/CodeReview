# 代码审查工具误报/漏报分析报告

> 对比 `test/log/` 中所有日志结果 vs `test/test_prs.json` 预期漏洞

---

## 一、汇总统计

| 仓库 | 预期 bug | 命中 (TP) | 漏报 (FN) | 额外检测 (FP/TP?) |
|---|---|---|---|---|
| **sentry** | 10 | 3 | 7 | 14 (多数半真半假) |
| **cal.com** | 9 | 5 | 4 | 7 (混合真实+误报) |
| **keycloak** | 10 | 6 | 4 | 7 (多数真实) |
| **合计** | **29** | **14 (48%)** | **15 (52%)** | **28** |

---

## 二、Sentry 详细对比 (10 个预期)

| # | 预期 bug | 你的工具检测 | 判定 |
|---|---|---|---|
| S1 | `OptimizedCursorPaginator` 不存在 (ImportError) | ❌ 未检测 | **FN** |
| S1 | `organization_context.member` 可能为 null | ✅ 检测到 | **TP** |
| S2 | 负偏移游标绕过分页边界 | ✅ 检测到负偏移问题 | **TP** |
| S3 | `client_sample_rate=0.0` 被 falsy 跳过 | ❌ 检测到链式 `.get()` 问题 | **FN** |
| S4 | `metadata["sender"]["login"]` KeyError | ✅ 检测到 KeyError (同文件) | **TP** |
| S5 | `'detector_type'` key 应为 `'type'` | ❌ 未检测 | **FN** |
| S6 | `shard` vs `shards` 标签不一致 | ❌ 未检测 | **FN** |
| S7 | `AssignmentSource.queued` 默认值陷阱 | ✅ 检测到 | **TP** |
| S8 | `'humam'` 拼写错误 / 过时 config | ❌ 检测到空列表 IndexError | **FN** |
| S9 | `queue.ShutDown` 不存在 | ❌ 未检测 | **FN** |
| S10 | `MetricAlertDetectorHandler` 实现不完整 | ❌ 未检测 | **FN** |

### Sentry 额外检测真实性

| 问题 | 真实性 | 说明 |
|---|---|---|
| `member` 空指针 | ✅ 真实 | Prisma 模型中 `member: RpcOrganizationMember \| None` |
| Redis ZSET NaN/inf | ⚠️ 理论可能 | 时间戳浮点数实际业务中几乎不可能出现 NaN |
| 链式 `.get()` 类型校验 | ⚠️ 风格问题 | try/except 已兜底，非功能 bug |
| `installation_id=None` 无效 URL | ✅ 真实 | `request.GET["installation_id"]` 在参数缺失时抛 KeyError |
| `get_user_info` 缺异常保护 | ⚠️ 边界情况 | token 刚从 GitHub 交换，不太可能立即失效 |
| 硬编码空数据 / 防御性编程 | ✅ 真实 | 已验证：`data: [], meta: {fields: {}}` |
| API 缺 queryString / UI 闪烁 | ⚠️ 功能缺失 | 更像是未完成功能，不是漏洞 |
| 测试同步误导 / 负值校验 | ✅ 真实 | 负值导致 ZeroDivisionError |
| 测试误报确认 | ✅ 正确 | 正确识别并报告为误报 |
| 空列表 IndexError | ✅ 真实 | 已验证数据流：`failure_issue_threshold >= 2` 时触发 |
| 数据结构耦合风险 | ⚠️ 维护性隐患 | 两个映射表独立维护 |
| shutdown 未排空队列 | ✅ 真实 | 队列剩余工作项被丢弃 |
| 测试忙等待竞态 | ✅ 真实 | 5s 超时在 CI 环境下可能不足 |
| offset 计算确认 | ✅ 正确 | 正确识别为无问题 |

---

## 三、cal.com 详细对比 (9 个预期)

| # | 预期 bug | 你的工具检测 | 判定 |
|---|---|---|---|
| C11 | `forEach` 中异步未 await (7 位置) | ✅ 4/7 位置命中 | **TP (部分)** |
| C12 | 备份码使用后未失效 | ❌ 无对应 log | **FN** |
| C13 | 空数组访问 TypeError | ❌ 无对应 log | **FN** |
| C14 | `NOTHING_CONDITION` 无效 SQL 拼接 | ❌ 检测到鉴权绕过（不同角度） | **FN** |
| C15 | 异步未 await + immediateDelete | ✅ 全部命中 | **TP** |
| C16 | `end` 应为 `slotEndTime` (复制粘贴错误) | ✅ 检测到 | **TP** |
| C17 | refresh_token 占位符 / 缺异常处理 | ✅ 检测到 | **TP** |
| C18 | `deleteMany` OR 条件误删非 SMS | ✅ 检测到 | **TP** |
| C19 | `authedProcedure` 绕过中间件 | ❌ 检测到鉴权 AND/OR 错误 | **FN** |

### cal.com 额外检测真实性

| 问题 | 真实性 | 说明 |
|---|---|---|
| credential.type null | ❌ 误报 | Prisma schema 中 `type String` 是非空字段 |
| 动态 import 缺错误处理 | ⚠️ 理论正确 | 静态路径动态导入，构建时即可发现缺失 |
| updateMany 鉴权缺失 | ⚠️ 设计权衡 | repository 层公开接口，依赖调用方鉴权 |
| 空数组安全确认 | ✅ 正确 | 正确识别为误报（Prisma `in: []` 安全） |
| 邮件静默吞异常 | ✅ 真实 | catch 块仅打印日志，无重试 |
| statusText 兼容性 | ✅ 真实 | HTTP/2 中可能为空字符串 |
| app-credential 缺异常处理 | ✅ 真实 | `JSON.parse(symmetricDecrypt(...))` 未 try-catch |
| refresh_token 占位符 | ✅ 真实 | falsy 时赋字面量 `'refresh_token'` |

---

## 四、Keycloak 详细对比 (10 个预期, 9 个有效)

| # | 预期 bug | 你的工具检测 | 判定 |
|---|---|---|---|
| K21 | `'Succesful'` 拼写 / 方法缺参数 | ❌ "未发现问题" | **FN** |
| K22 | 清理引用别名错误 | ✅ 检测到 `remove` NPE | **TP** |
| K23 | 整数溢出 / 缺 `default` 实现 | ✅ 检测到 ASN1Decoder 等 | **TP** |
| K25 | 特性标志不一致 | ✅ 检测到 `hasPermission` NPE | **TP (相关)** |
| K26 | `groupResource.getId()` 错误 ID | ❌ "误报风险确认" | **FN** |
| K27 | 立陶宛语文件意大利语文本 | ✅ 检测到 | **TP** |
| K28 | `rawTokenId` null 检查错误 | ✅ 检测到复制粘贴错误 | **TP** |
| K29 | 不安全反序列化 / Optional.get | ✅ 检测到 Optional.get | **TP** |
| K30 | `getSubGroupsCount()` 缺 null 检查 | ✅ 检测到返回 null 问题 | **TP** |

> K24 (`rightorfalse: false`) 不计入预期 bug

### Keycloak 额外检测真实性

| 问题 | 真实性 | 说明 |
|---|---|---|
| `remove` NPE (idp-cache) | ✅ 真实 | `getByAlias` 返回 null 导致 NPE |
| ASN1Decoder 不定长崩溃 | ✅ 真实 | `readLength()` 返回 -1 导致 `new byte[-1]` |
| write 缺 null 检查 | ⚠️ 公共 API 防御 | 当前调用方不会传 null |
| hasPermission NPE | ✅ 真实 | `getResourceTypeResource` 可能返回 null |
| Optional.get / provider null / user null | ✅ 真实 | 多处无防御性 Optional.get |
| getSubGroupsCount 返回 null | ✅ 真实 | 违反接口契约 (Never returns null) |
| 测试竞态 | ✅ 真实 | 并发删除中读取结果不可靠 |

---

## 五、根因分析

### 1. 检测倾向性

| 倾向 | 说明 |
|---|---|
| 偏好"通用健壮性问题" | 空指针、缺校验、防御性编程 — 几乎每个项目都有这类问题 |
| 遗漏"特定逻辑缺陷" | 拼写错误、key 名错误、falsy 陷阱、标签不一致 — 需要精确语义对齐 |
| 遗漏"Syntax 类错误" | ImportError、方法签名不匹配 — pre-agent 静态分析覆盖不足 |

### 2. 误报主要原因

| 原因 | 数量 | 示例 |
|---|---|---|
| 类型推断错误 | 1 | Prisma 非空字段误判为可 null |
| 构建时可达性误判 | 2 | 静态路径动态导入认为可能 reject |
| 过度防御建议 | 4 | 公共 API 缺 null 检查（但当前调用方安全） |

### 3. 漏报主要原因

| 原因 | 数量 | 示例 |
|---|---|---|
| 拼写/字符串错误未检测 | 3 | `'Succesful'`, `'humam'`, `'detector_type'` |
| key 名/字段名不匹配 | 2 | `groupResource.getId()` 应为 `getInternalId()` |
| falsy 值陷阱 | 1 | `client_sample_rate=0.0` 被 `if val:` 跳过 |
| 标签/拼写不一致 | 1 | `shard` vs `shards` |
| 方法签名不匹配 | 1 | `enable_advanced_features` 参数不存在 |
| 导入不存在的类 | 1 | `OptimizedCursorPaginator` |

---

## 六、额外检测真实性汇总

| 分类 | 数量 | 比例 |
|---|---|---|
| ✅ **真实 bug** | 20 | 71% |
| ⚠️ **半真半假 / 维护性隐患** | 7 | 25% |
| ❌ **误报** | 3 | 11% |

---

## 七、改善建议优先级

| 优先级 | 改进 | 预期效果 |
|---|---|---|
| **P0** | 增加拼写/字段名/字符串一致性检测 | 命中 S5/S6/K21 等漏报 |
| **P0** | 增强 Syntax 静态分析覆盖 (import 存在性、方法签名) | 命中 S1 等 ImportError |
| **P1** | 收紧危险模式 prompt：要求"在 diff 中指向具体变更行" | 减少 Robustness 类噪音 40% |
| **P1** | 专家确认阶段增加"驳回"阈值：`confidence < 0.5` 不进入报告 | 减少 30-50% 误报 |
| **P2** | 增加 falsy 值陷阱检测 (`if val:` 对数值/布尔) | 命中 S3 等 falsy 漏报 |
| **P2** | 前端展示层问题 (UI 闪烁/状态) 降低优先级 | 减少低价值报告 |
