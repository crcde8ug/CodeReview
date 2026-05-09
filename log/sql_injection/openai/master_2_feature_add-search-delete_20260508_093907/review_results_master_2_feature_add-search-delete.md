# 代码审查报告

## 摘要
本次审查针对 `user_service.py` 文件，共发现 **4 个严重问题**，均为 SQL 注入漏洞（风险类型：鉴权与数据暴露风险）。所有问题均源于未使用参数化查询，直接拼接用户输入到 SQL 语句中，攻击者可利用构造的恶意输入操纵数据库操作，导致数据泄露、绕过认证或数据破坏。代码整体安全性极低，需立即修复。

## 严重问题（Error）

### 1. `get_user_by_name` 函数 SQL 注入（第14-15行）
- **文件**：`user_service.py`
- **行号**：14-15
- **描述**：使用 f-string 将用户输入 `username` 直接拼接到 SQL 查询字符串中，未做任何输入校验或参数化处理。攻击者可传入 `' OR 1=1 --` 等恶意值，操纵查询语义。
- **建议**：替换为参数化查询：`cursor.execute("SELECT * FROM users WHERE username = ?", (username,))`

### 2. `get_user_orders` 函数 SQL 注入（第23-24行）
- **文件**：`user_service.py`
- **行号**：23-24
- **描述**：使用 `+` 拼接用户输入 `user_id` 到 SQL 查询中，且未对 `user_id` 进行类型校验（函数签名已改为 `str`）。攻击者可构造 `1 OR 1=1` 等值实现注入。
- **建议**：使用参数化查询：`cursor.execute("SELECT * FROM orders WHERE user_id = ?", (user_id,))`；同时建议对 `user_id` 进行类型转换（如 `int(user_id)`）并捕获异常，确保输入符合预期。

### 3. `search_users` 函数 SQL 注入（第32行）
- **文件**：`user_service.py`
- **行号**：32
- **描述**：在 LIKE 子句中使用 f-string 拼接用户输入 `keyword`，未参数化。攻击者可传入 `' OR 1=1 --` 等值，导致未授权数据访问。
- **建议**：使用参数化查询：`cursor.execute("SELECT id, username FROM users WHERE username LIKE ?", ('%' + keyword + '%',))`

### 4. `delete_user` 函数 SQL 注入（第39-40行）
- **文件**：`user_service.py`
- **行号**：39-40
- **描述**：使用 `+` 拼接用户输入 `user_id` 到 DELETE 语句中，危险性极高。攻击者可构造 `1 OR 1=1` 等值删除所有用户数据。
- **建议**：使用参数化查询：`cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))`；同时建议对 `user_id` 进行类型校验。

## 重要问题（Warning）
无

## 建议（Info）
- 所有数据库操作应统一使用参数化查询（`?` 占位符），杜绝字符串拼接。
- 对用户输入进行严格的类型校验（如 `int` 转换）和长度限制，作为防御深度。
- 考虑使用 ORM 框架（如 SQLAlchemy）进一步降低手动拼接风险。
- 建议对 `get_user_orders` 和 `delete_user` 的 `user_id` 参数类型恢复为 `int`，并在函数内部进行类型断言。

## 按风险类型统计
- Robustness (健壮性与边界条件): 0
- Concurrency (并发与时序正确性): 0
- Authorization (鉴权与数据暴露风险): 4
- Intent & Semantics (需求意图与语义一致性): 0
- Lifecycle & State (生命周期与状态一致性): 0
- Syntax (语法与静态错误): 0

## 建议与结论
当前代码存在严重的 SQL 注入漏洞，覆盖所有数据库操作函数，攻击面极大。**必须立即修复**，优先采用参数化查询替代所有字符串拼接。同时建议引入输入验证机制（如类型检查、白名单过滤）作为第二道防线。整体代码质量因安全缺陷而极低，修复后应进行回归测试，并考虑引入静态分析工具（如 Bandit）自动检测此类问题。