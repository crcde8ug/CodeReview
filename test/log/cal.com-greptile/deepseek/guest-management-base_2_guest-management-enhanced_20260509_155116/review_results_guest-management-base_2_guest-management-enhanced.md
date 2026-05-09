# 代码审查报告

## 摘要
本次审查共确认 **2** 个问题，涉及 **15** 个文件。问题包括 1 个严重级别（Error）和 1 个重要级别（Warning）。主要风险集中在鉴权逻辑错误和健壮性不足。整体代码结构清晰，但关键业务逻辑存在安全性和可靠性隐患，需优先修复。

## 严重问题（Error）

### 1. 鉴权逻辑错误：权限检查条件错误导致 ADMIN 角色被拒绝
- **风险类型**：Authorization_Data_Exposure（鉴权与数据暴露风险）
- **文件**：`packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts`
- **行号**：46-48
- **描述**：权限检查代码使用 `&&`（逻辑与）连接 `isTeamAdmin` 和 `isTeamOwner`，但变量名 `isTeamAdminOrOwner` 暗示只需 ADMIN 或 OWNER 之一。`isTeamAdmin` 函数已包含 ADMIN 和 OWNER 两种角色，而 `isTeamOwner` 仅匹配 OWNER。使用 `&&` 导致实际只允许 OWNER 角色通过，ADMIN 角色被错误拒绝，造成权限漏洞。
- **建议**：将第47行的 `&&` 改为 `||`：`(await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) || (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0))`。或者直接使用 `isTeamAdmin` 即可（因为它已包含 ADMIN 和 OWNER 两种角色）。

## 重要问题（Warning）

### 1. 邮件发送失败时静默吞异常，用户无法感知
- **风险类型**：Robustness_Boundary_Conditions（健壮性与边界条件）
- **文件**：`packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts`
- **行号**：167-171
- **描述**：发送邮件失败时仅打印静态日志字符串（`'Error sending AddGuestsEmails'`），未记录错误详情、未向上抛出异常或进行补偿操作。数据库中的 guests 已在第92-106行成功写入，但用户始终收到 `{ message: 'Guests added' }` 的成功响应（第173行），无法感知邮件发送失败。`sendAddGuestsEmails` 内部通过 `sendEmail()` 调用 nodemailer 发送邮件，`sendEmail` 函数的 catch 分支会 reject Promise，但 handler 的 try/catch 完全吞没了异常。
- **建议**：在 catch 块中至少记录错误详情（如 `console.error('Error sending AddGuestsEmails', err)`），并考虑是否需要在邮件发送失败时向调用方返回部分成功状态（如 `{ message: 'Guests added', warning: 'Email notification failed' }`），或通过监控/告警机制追踪邮件发送失败事件。

## 建议（Info）
无

## 按风险类型统计
- Robustness (健壮性与边界条件): 1
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 1
- Intent & Semantics (需求意图与语义一致性): 0
- Lifecycle & State (生命周期与状态一致性): 0
- Syntax (语法与静态错误): 0

## 建议与结论
1. **优先修复鉴权逻辑**：`addGuests.handler.ts` 中的权限检查错误是严重的安全问题，可能导致未授权的 ADMIN 角色无法执行操作，或更糟的是，如果逻辑被误用，可能暴露敏感数据。建议立即修复并添加单元测试覆盖 ADMIN 和 OWNER 两种角色的权限场景。
2. **增强邮件发送的健壮性**：当前静默吞异常的设计会掩盖故障，影响用户体验和运维监控。建议至少记录错误详情，并考虑引入部分成功响应或异步重试机制。
3. **整体代码质量**：新增的 `AddGuestsDialog` 组件和 `BookingListItem` 的集成代码结构清晰，遵循了项目现有模式。但后端 handler 的边界条件处理（如邮件失败）和权限检查的准确性需要加强。建议在后续开发中增加对关键业务路径的异常处理和日志记录规范。