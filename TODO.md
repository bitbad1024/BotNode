# logger「child + bind」重构 TODO

> 目标：把「无派生实例」哲学正式翻案为双模型 —— **child 管结构（命名层级），bind 管上下文（字段）**。

## 三条已确认的不变量

1. **`log` 不向父级投递**：child 写日志时沿父链解析出目标，只投那一份，无 propagate、无重复输出。
2. **`bind()` 对象不能再 `child`**：类型分离 —— `child()` 返回可继续 `child` 的树节点；`bind()` 返回只可 `bind`/`log`/`mute` 的上下文视图。
3. **child 扁平化，不缓存、不复制**：不建 `children` 缓存字典，不复制父级状态 —— `ChildLogger` 只持 `parent` 引用 + 自身覆盖项（`_level`/`_targets`，`None` = 继承），写日志时沿父链**现解析**。因此父级后挂出口、改级别对已派生节点**立即生效**；「先取实例再挂出口」的顺序坑不复现。

## 改动清单

### 1. models.py
- [x] `Target` 加 `level: LogLevel | None = None` 字段（出口级最低级别，`None` = 全收）

### 2. filters.py
- [ ] 删除 `LevelFilter` 类（级别过滤收敛为 `Target.level` 字段，不留类）
- [ ] 更新模块 docstring 示例与 `DenyAllFilter` docstring 引用
- [ ] 清理残留 import（`LogLevel` 不再使用）

### 3. base.py（核心改动）
- [ ] 新增 `ChildLogger` 类：持 `_root` / `_parent` / `_name` / `_level` / `_targets`，**无 children 缓存字典、无父级状态复制**（扁平化，`__slots__` 即可）
- [ ] `ChildLogger` 动态解析：`level` = 沿父链最近一个显式设置；`targets` = 沿父链最近一份显式指定，都没有则跟随 root 当前默认目标（`None` = 继承）
- [ ] `ChildLogger.log()`：写时解析 → 构造记录 → 只投解析出的 targets，**不 propagate**
- [ ] `ChildLogger` 提供 `child()`（继续生长）/ `bind()`（产 BoundLogger）/ `set_level()` / `is_enabled_for()` / `log()` + 各级别便捷方法 / `flush()`
- [ ] `BaseLogger.child(name, *, level=None, targets=None) -> ChildLogger`：名字经 `qualify()` 拼接；每次返回新视图对象（无缓存，行为一致）
- [ ] `BaseLogger.mount(..., level=None)`：`Target` 带 level，替代 `LevelFilter` 的活
- [ ] `_dispatch` 分发时按 `target.level` 过滤（低于不放行）
- [ ] console 挂载改 `level=`（`console_level` 配置参数保留，映射到 `Target.level`）
- [ ] docstring 翻案：顶部「没有派生实例」段、`BoundLogger` 里那句 child 对比
- [ ] `BoundLogger` 保持无 `child`（类型分离，现有已满足，仅文档确认）

### 4. core.py
- [ ] `mount_module(name, processor, *, core=None, level=None)`：`level` 透传给 `route`

### 5. manager.py
- [ ] `configure` docstring 注明 `processors` 可传 `Target`（携带 `level`）；签名不变

### 6. __init__.py
- [ ] 导出 `ChildLogger`，移除 `LevelFilter`
- [ ] docstring 更新（child 语义一句话）

### 7. docs/logger.md
- [ ] 翻案「没有派生实例」相关段落，补 child/bind 分工说明

### 8. 示例与处理器 docstring
- [ ] `processors/console.py`、`examples/logging_demo.py` 中 `LevelFilter` 引用更新

### 9. 测试
- [ ] `test_log_core.py`：`LevelFilter` 用法改为 `mount(level=)`（约 3 处）
- [ ] 新增 child 用例：层级命名、动态继承（后挂出口/改级别实时生效）、无父级重复投递、`bind()` 产物无 `child`、`child().bind()` 组合语义

## 验证
- [ ] `python -m pytest tests -q` 全绿
- [ ] `ruff check` 无新增错误

## 备注
- `route` / `publish` 保留不动：`route` 是「这条路换目标的具名缓存视图」（返回 `BoundLogger`），`child` 是「命名层级节点」——两者并存，docstring 讲清分工。
- 已先行落地：1、2 两个文件（见 `[x]`），如审查不认可可 `git checkout -- <file>` 回退。
