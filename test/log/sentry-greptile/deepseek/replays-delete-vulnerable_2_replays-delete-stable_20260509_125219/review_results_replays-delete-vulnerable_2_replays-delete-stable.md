# 代码审查报告

## 摘要
本次代码审查共发现 **4 个已确认问题**，影响 **106 个文件**。问题主要集中在 **需求意图与语义一致性** 和 **生命周期与状态一致性** 方面，包含 2 个严重错误和 2 个重要警告。主要问题涉及：新功能实现中使用了硬编码空数据导致功能不可用、组件缺乏防御性编程可能导致运行时崩溃、API 调用缺失关键参数导致搜索功能缺失、以及 UI 状态初始化时错误提示短暂闪烁。整体代码质量存在明显缺陷，需优先修复严重问题。

## 严重问题（Error）

### 1. 新功能实现使用硬编码空数据，导致用户看到空白表格
- **文件**: `static/app/views/dashboards/widgetCard/chart.tsx`
- **行号**: 164-174
- **风险类型**: 需求意图与语义一致性
- **问题描述**: 当 feature flag `use-table-widget-visualization` 开启时，`TableWidgetVisualization` 组件被传入硬编码的空数据（`data: [], meta: {fields: {}, units: {}}, columns: []`），而非使用 `tableResults` 中的真实数据。对比 feature flag 关闭的分支（第176-192行）正确使用了 `result.data` 和 `result.meta`，说明这是未完成的占位实现，将导致用户看到空白表格。
- **建议**: 将 `TableWidgetVisualization` 的 props 替换为使用真实数据：`columns` 应基于 `widget.queries[i]?.fields` 构建，`tableData.data` 应使用 `result.data`，`tableData.meta` 应使用 `result.meta`。例如：`columns={fields.map(f => ({key: f, name: f}))}` `tableData={{data: result.data, meta: result.meta}}`。

### 2. 组件缺乏防御性编程，在特定条件下会直接崩溃
- **文件**: `static/app/views/dashboards/widgets/tableWidget/tableWidgetVisualization.tsx`
- **行号**: 92-99
- **风险类型**: 健壮性与边界条件
- **问题描述**: 当 `columns` 为 undefined 且 `tableData` 为 undefined/null 或 `tableData.meta` 为 undefined 时，第94行 `Object.keys(tableData?.meta.fields)` 会抛出 `TypeError: Cannot convert undefined or null to object`。可选链 `tableData?.meta` 在 `tableData` 为 undefined 时返回 undefined，随后 `Object.keys(undefined)` 直接崩溃。虽然 `TabularData` 类型中 `meta` 是必选字段，但实际数据源 `TableDataWithTitle`（来自 API）中 `meta` 是可选字段（`meta?: MetaType`），运行时可能缺失。当前唯一调用方（chart.tsx:165-174）传入了硬编码空数据不会触发此问题，但组件本身缺乏防御性编程。
- **建议**: 在访问 `tableData?.meta.fields` 前增加防御性检查。例如：将第92-99行改为 `columns ?? (tableData?.meta?.fields ? Object.keys(tableData.meta.fields).map(...) : [])`，或使用 `Object.keys(tableData?.meta?.fields ?? {}).map(...)` 确保即使 `meta` 或 `fields` 为 undefined 也不会崩溃。

## 重要问题（Warning）

### 1. API 调用缺失关键参数，导致搜索功能缺失
- **文件**: `static/app/views/explore/hooks/useTraceItemAttributeKeys.tsx`
- **行号**: 50-54
- **风险类型**: 需求意图与语义一致性
- **问题描述**: `queryFn` 调用 `getTraceItemAttributeKeys()` 时未传递 `queryString` 参数。`useGetTraceItemAttributeKeys` 返回的函数签名支持 `queryString?: string`（见 `useGetTraceItemAttributeKeys.tsx` 第65行），该参数会作为 `substringMatch` 传递给 API 实现搜索过滤。但 `useTraceItemAttributeKeys` 的 props 接口未定义搜索相关属性，且 `queryFn` 调用时未传参，导致搜索功能缺失。
- **建议**: 在 `UseTraceItemAttributeKeysProps` 接口中添加可选的 `search` 属性，并在调用 `makeTraceItemAttributeKeysQueryOptions` 时透传该值，同时在 `queryFn` 中将其传递给 `getTraceItemAttributeKeys(search)`。

### 2. UI 状态初始化时错误提示短暂闪烁
- **文件**: `static/app/views/replays/detail/ai/index.tsx`
- **行号**: 90-96
- **风险类型**: 生命周期与状态一致性
- **问题描述**: 可证伪断言：当 `replayRecord.project_id` 存在但 `useProjectFromId` 尚未完成加载时，条件 `!project` 为 true，导致错误提示短暂闪烁。问题类型：初始化陷阱（UI 状态不一致）。`useProjectFromId`（`static/app/utils/useProjectFromId.tsx:7-13`）内部调用 `useProjects()` 获取 projects 列表，但只返回 project 对象或 undefined，不暴露 fetching/loading 状态。在 `ProjectsStore` 初始加载完成前，projects 数组为空，project 为 undefined，此时第 90 行条件成立，渲染 'Project not found' 错误。随后 `ProjectsStore` 加载完成，project 变为有效值，组件重新渲染到正常内容。第 98 行的 `isPending/isRefetching` 仅处理 `useApiQuery` 的 loading 状态，未覆盖 `useProjectFromId` 的加载中状态。
- **建议**: 方案一：让 `useProjectFromId` 返回 `{project, fetching}` 或类似结构，调用方在 fetching 为 true 时显示 loading 而非错误。方案二：在 `AiContent` 中额外调用 `useProjects()` 获取 fetching 状态，组合判断：`if (replayRecord?.project_id && !project && !fetching)` 才显示错误。方案三：利用 `ProjectsStore` 的 loading 属性，在 store 未加载完成时跳过错误判断。

## 建议（Info）
无。

## 按风险类型统计
- Robustness (健壮性与边界条件): 1
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 0
- Intent & Semantics (需求意图与语义一致性): 2
- Lifecycle & State (生命周期与状态一致性): 1
- Syntax (语法与静态错误): 0

## 建议与结论
本次审查发现的问题主要集中在 **新功能实现的完整性** 和 **代码健壮性** 方面。两个严重问题（硬编码空数据和缺乏防御性编程）直接关联，建议优先修复。此外，API 调用参数缺失和 UI 状态闪烁问题也需尽快处理，以提升功能完整性和用户体验。整体来看，代码库在引入新功能时缺乏充分的边界条件测试和状态管理考虑，建议在后续开发中加强代码审查和单元测试覆盖，特别是针对新功能分支和 API 调用场景。