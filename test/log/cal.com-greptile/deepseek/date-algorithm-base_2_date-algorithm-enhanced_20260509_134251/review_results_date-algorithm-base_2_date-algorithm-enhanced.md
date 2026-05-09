# 代码审查报告

## 摘要

本次审查涉及 4 个文件，共发现 2 个已确认问题。其中包含 1 个严重错误（Error）和 1 个重要警告（Warning）。主要问题集中在时区处理逻辑和跨午夜边界场景的缺陷上，这些问题可能导致日程计算错误和功能失效。整体代码质量尚可，但关键逻辑部分需要立即修复。

## 严重问题（Error）

### 1. 复制粘贴错误导致跨午夜时段判断失效
- **文件**: `packages/trpc/server/routers/viewer/slots.ts`
- **行号**: 141-142
- **风险类型**: 需求意图与语义一致性
- **严重程度**: 错误
- **描述**: 第141-142行中，`start` 和 `end` 都被赋值为 `slotStartTime.hour() * 60 + slotStartTime.minute()`，导致 `end` 始终等于 `start`。根据 `WorkingHours` 类型定义，`startTime` 和 `endTime` 分别表示工作时段开始和结束的分钟数（自午夜起）。第143行检查 `end > workingHour.endTime` 时，由于 `end` 错误地使用了 `slotStartTime` 而非 `slotEndTime`，对于跨午夜边界的 slot（如 23:00 开始持续 90 分钟到 00:30），`end` 会被计算为 23:00 的分钟数（1380）而非 00:30 的分钟数（30），导致超出工作时段的判断完全失效。这明显是复制粘贴错误。
- **建议**: 将第142行的 `slotStartTime` 改为 `slotEndTime`：`const end = slotEndTime.hour() * 60 + slotEndTime.minute();`

## 重要问题（Warning）

### 1. 跨午夜 override 处理逻辑缺失
- **文件**: `packages/lib/slots.ts`
- **行号**: 218-223
- **风险类型**: 需求意图与语义一致性
- **严重程度**: 警告
- **描述**: override 的 `startTime`/`endTime` 计算仅提取小时和分钟（`hour()*60+minute()`，范围 0-1439），未保留日期信息。当 override 跨越午夜（如 start=23:00, end=次日01:00）时，`endTime`（如 60）会小于 `startTime`（如 1380），导致在 `buildSlots` 第68行的 `if (start >= end) continue;` 检查中被丢弃，使得跨午夜 override 完全失效。第89行 TODO 注释也明确承认“This logic does not allow for past-midnight bookings”。对比 `getWorkingHours`（availability.ts 第103-121行）有专门的跨天溢出处理逻辑，但 override 计算缺少类似处理。
- **建议**: 在计算 override 的 `startTime`/`endTime` 时，需要保留日期信息以支持跨午夜场景。一种方案是：将 `override.start` 和 `override.end` 转换为相对于当天 00:00 的分钟偏移量（而非仅提取 `hour*60+minute`），例如使用 `dayjs` diff 计算：`dayjs(override.start).utc().add(offset, 'minute').diff(startOfDay, 'minute')`，其中 `startOfDay` 是当天的 UTC 00:00。同时需要确保 `buildSlots` 中的边界处理逻辑能正确处理 `endTime > 1440` 或 `startTime < 0` 的情况。

## 建议（Info）

无其他建议性问题。

## 按风险类型统计

- Robustness (健壮性与边界条件): 0
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 0
- Intent & Semantics (需求意图与语义一致性): 2
- Lifecycle & State (生命周期与状态一致性): 0
- Syntax (语法与静态错误): 0

## 建议与结论

本次审查发现的两个问题均属于“需求意图与语义一致性”类别，且都与时间计算逻辑相关。其中复制粘贴错误（Error）必须立即修复，因为它直接导致跨午夜时段判断功能完全失效。跨午夜 override 处理缺失（Warning）虽然影响范围可能较小，但同样需要尽快处理，以避免在特定场景下出现不可预期的行为。

建议开发团队：
1. **优先修复严重错误**：立即修改 `slots.ts` 中的复制粘贴错误，并添加相应的单元测试覆盖跨午夜场景。
2. **完善边界处理**：参考 `availability.ts` 中已有的跨天溢出处理逻辑，为 override 计算添加类似支持。
3. **加强代码审查**：在未来的代码审查中，特别关注时间计算相关的复制粘贴错误和边界条件处理。
4. **补充测试用例**：为跨午夜时段、时区转换等复杂场景添加更多测试用例，确保逻辑正确性。