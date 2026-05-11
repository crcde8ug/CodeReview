# 代码审查报告

## 摘要
本次代码审查共涉及 17 个文件，确认 6 个问题。其中包含 2 个重要警告（Warning）和 4 个信息性建议（Info）。主要问题集中在生命周期状态一致性（Prisma `updateMany` 空对象导致 `updatedAt` 静默失败）和鉴权错误处理（未使用 `TRPCError` 区分 `NOT_FOUND` 与 `FORBIDDEN`）。整体代码质量良好，但存在两处需要优先修复的隐患。

## 重要问题（Warning）

### 1. 生命周期状态一致性：Prisma `updateMany` 空对象导致 `updatedAt` 静默失败
- **文件**: `packages/lib/server/repository/selectedCalendar.ts` (行 400-405)
- **风险类型**: Lifecycle_State_Consistency
- **描述**: `updateManyByCredentialId` 方法接受 `data` 参数但不验证其是否为空。调用方在 `CalendarService.ts:1024` 传入空对象 `{}`，意图是更新所有关联日历记录的 `updatedAt` 时间戳。然而 Prisma 的 `updateMany` 在 `data` 为空对象时不会生成任何 `SET` 子句，也不会自动触发 `@updatedAt` 字段更新（与 `update` 不同），导致副作用静默失败。
- **建议**: 推荐采用方案三：在调用方 `CalendarService.ts:1024` 处改为传入 `{ updatedAt: new Date() }` 而非空对象。此方案改动最小且意图明确。备选方案一：在 `updateManyByCredentialId` 中显式设置 `updatedAt: new Date()`，例如 `data = { ...data, updatedAt: new Date() }`。

### 2. 鉴权与数据暴露风险：`deleteCache` 端点未使用 `TRPCError` 区分错误类型
- **文件**: `packages/trpc/server/routers/viewer/calendars/deleteCache.handler.ts` (行 24-26)
- **风险类型**: Robustness_Boundary_Conditions
- **描述**: 凭据不存在时抛出普通 `Error('Credential not found or access denied')`，未使用 `TRPCError` 区分 `NOT_FOUND` 与 `FORBIDDEN`。由于 `prisma.credential.findFirst` 同时按 `id` 和 `userId` 过滤，`null` 可能由两种原因导致：(1) `credentialId` 不存在（NOT_FOUND），(2) `credentialId` 存在但不属于当前用户（FORBIDDEN）。使用统一消息会暴露内部状态信息（让攻击者知道 `credentialId` 是否存在），且不符合项目规范（同目录 `setDestinationCalendar.handler.ts` 使用了 `TRPCError`）。tRPC 的 `errorFormatter` 仅对 `ZodError` 做特殊处理，普通 `Error` 的 `message` 会直接暴露给客户端。
- **建议**: 建议使用 `TRPCError` 区分两种场景：当 `credential` 为 `null` 时，先通过 `prisma.credential.findUnique({ where: { id: credentialId }, select: { id: true } })` 检查 `credentialId` 是否存在。若不存在则抛出 `new TRPCError({ code: 'NOT_FOUND', message: 'Credential not found' })`；若存在但不属于当前用户则抛出 `new TRPCError({ code: 'FORBIDDEN', message: 'Access denied' })`。这样既符合项目规范，又避免泄露内部状态。

## 建议（Info）

### 1. 健壮性与边界条件：`CredentialActionsDropdown` 中 `cacheUpdatedAt` 类型安全
- **文件**: `packages/features/apps/components/CredentialActionsDropdown.tsx` (行 89-92)
- **风险类型**: Robustness_Boundary_Conditions
- **描述**: 误报。`cacheUpdatedAt`（类型 `Date | null | undefined`）在第92行传入 `new Date()`，但第69行的 `hasCache = isGoogleCalendar && cacheUpdatedAt` 已通过真值检查排除了 `null/undefined` 情况，且第82行的 `{hasCache && (...)}` 渲染守卫确保仅在 `hasCache` 为 truthy 时才执行该代码路径。数据源（`connectedCalendars.handler.ts:35`）返回 `Date | null`，调用方传入 `cacheUpdatedAt={connectedCalendar.cacheUpdatedAt}`。防御链完整。
- **建议**: 无需修改。如果担心 `Invalid Date` 边界情况，可在 `new Date(cacheUpdatedAt)` 后增加 `isValid` 检查，例如：`const date = new Date(cacheUpdatedAt); if (isNaN(date.getTime())) { /* fallback */ }`。

### 2. 健壮性与边界条件：`SelectedCalendarsSettingsWebWrapper` 中 `credentialId` 类型安全
- **文件**: `packages/platform/atoms/selected-calendars/wrappers/SelectedCalendarsSettingsWebWrapper.tsx` (行 72)
- **风险类型**: Robustness_Boundary_Conditions
- **描述**: 误报。`connectedCalendar.credentialId` 在数据流中始终为 `number` 类型。`credentialId` 来自 `getConnectedCalendars` 中 `credential.id`（Prisma `select { id: true }`，数据库主键 `Int`，非可空），所有返回路径（成功/错误/无calendar）均设置 `credentialId` 为 `credential.id`（`number` 类型）。`CredentialActionsDropdownProps` 也声明 `credentialId: number`（非可选）。不存在 `undefined/null` 的可能性。
- **建议**: 无需修改。`credentialId` 在数据流中始终为 `number` 类型，不存在空值风险。

### 3. 健壮性与边界条件：`connectedCalendars.handler` 中 `cacheStatusMap.get` 防御性写法
- **文件**: `packages/trpc/server/routers/viewer/calendars/connectedCalendars.handler.ts` (行 31-35)
- **风险类型**: Robustness_Boundary_Conditions
- **描述**: 误报。`cacheStatusMap.get(calendar.credentialId) || null` 是标准防御性写法。`Map.get()` 对不存在的 key 返回 `undefined`，`|| null` 将其统一转为 `null`，语义上表示'无有效缓存时间'。`getCacheStatusByCredentialIds` 使用 `groupBy` 只返回有缓存记录的 `credentialId`，不存在'静默吞没查询失败'的问题。
- **建议**: 当前写法正确，无需修改。如果业务上需要区分'从未缓存'和'缓存记录无 `updatedAt`'，可改用 `?? null`（仅转换 `undefined` 为 `null`，保留 `null` 原值），但当前场景下两种情况的业务语义等价。

### 4. 鉴权与数据暴露风险：`deleteCache` 端点所有权校验正确
- **文件**: `packages/trpc/server/routers/viewer/calendars/_router.tsx` (行 28-33)
- **风险类型**: Authorization_Data_Exposure
- **描述**: 误报。`deleteCache` 端点使用了 `authedProcedure`，且 handler (`deleteCache.handler.ts:13-33`) 内部通过 `prisma.credential.findFirst({ where: { id: credentialId, userId: user.id } })` 对 `credentialId` 进行了所有权校验（第17-23行），确保用户只能操作自己的 credential。未授权时抛出 `'Credential not found or access denied'` 错误。因此不存在 IDOR 风险。
- **建议**: 无需修复。handler 已正确实现 credential 所有权校验。

## 按风险类型统计
- Robustness (健壮性与边界条件): 3
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 2
- Intent & Semantics (需求意图与语义一致性): 0
- Lifecycle & State (生命周期与状态一致性): 1
- Syntax (语法与静态错误): 0

## 建议与结论
本次代码审查发现两处需要优先修复的警告问题：
1. **`updateManyByCredentialId` 空对象问题**：建议在调用方 `CalendarService.ts:1024` 处显式传入 `{ updatedAt: new Date() }`，确保 `updatedAt` 字段被正确更新。
2. **`deleteCache` 端点错误处理**：建议使用 `TRPCError` 区分 `NOT_FOUND` 与 `FORBIDDEN`，符合项目规范并避免信息泄露。

其余 4 个信息性建议均为误报，当前实现已具备足够的防御性。整体代码质量良好，数据流清晰，类型安全，建议在后续开发中保持当前编码规范。