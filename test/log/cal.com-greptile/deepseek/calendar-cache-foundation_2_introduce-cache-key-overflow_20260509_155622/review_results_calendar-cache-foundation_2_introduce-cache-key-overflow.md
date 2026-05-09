# 代码审查报告

## 摘要
本次审查共发现 **2 个已确认问题**，涉及 **17 个文件**。其中包含 **1 个重要问题（Warning）** 和 **1 个建议（Info）**。主要风险集中在 **鉴权与数据暴露** 方面，存在潜在的越权操作漏洞；另有一个误报问题已确认无需修复。整体代码质量尚可，但需优先处理权限校验缺失的严重隐患。

## 重要问题（Warning）

### 1. 鉴权缺失：`updateManyByCredentialId` 方法存在越权风险
- **文件**: `packages/lib/server/repository/selectedCalendar.ts`（第 400-405 行）
- **风险类型**: 鉴权与数据暴露风险（Authorization_Data_Exposure）
- **严重程度**: 警告（Warning）
- **描述**: `updateManyByCredentialId` 方法仅通过 `credentialId` 过滤记录，未校验当前用户是否有权操作该 `credentialId` 对应的数据。对比同类方法（如 `updateUserLevel`、`deleteUserLevel`、`findUserLevelUniqueOrThrow`），它们均使用 `ensureUserLevelWhere` 约束来限制操作范围。而 `updateManyByCredentialId` 没有任何权限校验或 `userId` 过滤，若被外部调用方传入任意 `credentialId`，可导致越权批量更新 `SelectedCalendar` 记录。当前唯一调用点位于 `CalendarService.fetchAvailabilityAndSetCache`（第 1024 行），使用 `this.credential.id`（来自已认证的 credential 对象），但方法本身作为公开 repository 接口存在越权风险。
- **建议**: 为 `updateManyByCredentialId` 增加 `userId` 参数或权限校验，确保调用者只能更新自己有权访问的 `credentialId` 对应的记录。例如：
  ```typescript
  static async updateManyByCredentialId(credentialId: number, data: Prisma.SelectedCalendarUpdateInput, userId?: number) {
    const where: any = { credentialId };
    if (userId) {
      where.userId = userId;
    }
    return await prisma.selectedCalendar.updateMany({ where, data });
  }
  ```
  或者创建一个带 `ensureUserLevelWhere` 约束的变体方法。

## 建议（Info）

### 1. 误报：`credentialIds` 数组为空时的安全性
- **文件**: `packages/trpc/server/routers/viewer/calendars/connectedCalendars.handler.ts`（第 27-31 行）
- **风险类型**: 健壮性与边界条件（Robustness_Boundary_Conditions）
- **严重程度**: 信息（Info）
- **描述**: 经确认，当 `credentialIds` 数组为空时，`getCacheStatusByCredentialIds` 不会崩溃。Prisma 的 `in: []` 查询返回空结果集（不抛异常），`.map()` 在空数组上安全返回空数组，`new Map([])` 创建空 Map，后续 `cacheStatusMap.get(...) || null` 也安全处理了 `undefined` 情况。整个调用链均有防御。
- **建议**: 无需修复。当前实现已正确处理空数组场景，代码健壮性良好。

## 按风险类型统计
- Robustness (健壮性与边界条件): 1（误报，无需修复）
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 1
- Intent & Semantics (需求意图与语义一致性): 0
- Lifecycle & State (生命周期与状态一致性): 0
- Syntax (语法与静态错误): 0

## 建议与结论
1. **优先处理鉴权问题**：`updateManyByCredentialId` 的权限校验缺失是本次审查最严重的问题，建议立即修复，防止潜在的越权数据操作。修复方案可参考同类方法的实现模式，增加 `userId` 参数或使用 `ensureUserLevelWhere` 约束。
2. **保持防御性编程习惯**：虽然 `connectedCalendars.handler.ts` 中的空数组场景已安全处理，但建议在类似场景中继续保持防御性检查，避免未来因代码变更引入风险。
3. **代码一致性**：建议统一 repository 层方法的权限校验模式，确保所有公开方法都遵循相同的安全约束，降低未来出现类似漏洞的概率。
4. **整体评估**：本次审查未发现严重错误或崩溃性问题，但鉴权漏洞需引起重视。代码整体质量良好，建议在后续开发中加强权限校验的自动化检查。