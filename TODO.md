# logger「child + bind」重构 TODO

> 目标：把「无派生实例」哲学正式翻案为双模型 —— **child 管结构（命名层级），bind 管上下文（字段）**。

## 三条已确认的不变量

1. **`log` 不向父级投递**：child 写日志时用**创建时固化**的目标，只投那一份，无 propagate、无重复输出。
2. **`bind()` 对象不能再 `child`**：类型分离 —— `child()` 返回可继续 `child` 的树节点；`bind()` 返回只可 `bind`/`log`/`mute` 的上下文视图。
3. **child 是缓存树、创建时固化**：`ChildLogger` 持 `children` 缓存字典，派生那一刻把生效的级别 / 目标**解析并固化**进新节点（`_level`/`_targets` 永远是具体值，无「继承 / 待解析」），之后读 `level`/`targets` 不再沿父链解析；同名 `child` 命中缓存返回同一对象。因此父级后挂出口、改级别对**已派生**节点不生效，要新配置就再 `child` 一层。

## 改动清单

### 1. models.py
- [x] `Target` 加 `level: LogLevel | None = None` 字段（出口级最低级别，`None` = 全收）

### 2. 级别过滤收敛（filters.py + base.py 承接）
- [x] 删除 `LevelFilter` 类（级别过滤收敛为 `Target.level` 字段，不留类）
- [x] 更新 filters.py 模块 docstring 示例与 `DenyAllFilter` docstring 引用
- [x] 清理 filters.py 残留 import（`LogLevel` 不再使用）
- [x] `BaseLogger.mount(..., level=None)`：`Target` 带 level，替代 `LevelFilter` 的活
- [x] `_dispatch` 分发时按 `target.level` 过滤（低于不放行）
- [x] console 挂载改 `level=`（`console_level` 配置参数保留，映射到 `Target.level`）
- [x] 同步引用：`__init__.py` 导出、`console.py` docstring、tests、examples、docs/logger.md

### 3. base.py（核心改动）
- [x] 新增 `ChildLogger` 类：持 `_root` / `_parent` / `_children` / `_name` / `_level` / `_targets`，**children 缓存字典 + 创建时固化**（`__slots__` 即可）
- [x] `ChildLogger` 创建时固化：`level` / `targets` 未给则继承父（顶层 root）当前解析值，之后读 `level`/`targets` 不再沿父链解析
- [x] `ChildLogger.log()`：用**固化**的级别 / targets 构造记录 → 只投那一份，**不 propagate**
- [x] `ChildLogger` 提供 `child()`（继续生长，缓存命中）/ `bind()`（产 BoundLogger）/ `set_level()` / `is_enabled_for()` / `log()` + 各级别便捷方法 / `flush()`
- [x] `BaseLogger.child(name, *, level=None, targets=None) -> ChildLogger`：名字经 `qualify()` 拼接；`_children` 缓存命中返回同一对象
- [x] docstring 翻案：顶部「没有派生实例」段、`BoundLogger` 里那句 child 对比
- [x] `BoundLogger` 保持无 `child`（类型分离，现有已满足，仅文档确认）

### 4. core.py
- [x] `mount_module(name, processor, *, core=None, level=None)`：`level` 透传给 `route`

### 5. manager.py
- [x] `configure` docstring 注明 `processors` 可传 `Target`（携带 `level`）；签名不变

### 6. __init__.py
- [x] 导出 `ChildLogger`，移除 `LevelFilter`
- [x] docstring 更新（child 语义一句话）

### 7. docs/logger.md
- [x] 翻案「没有派生实例」相关段落，补 child/bind 分工说明

### 8. 示例与处理器 docstring
- [x] `processors/console.py`：docstring 改为「控制台不做过滤、挂载时用出口级 `level=` 设门槛」的表述（`LevelFilter` 字样随类删除一并清掉）
- [x] `examples/logging_demo.py`：本无 `LevelFilter` 引用，未动；演示改用 `child()` + `Target`，注释注明控制台目标自带 level 门槛

### 9. 测试
- [x] `test_log_core.py`：原 `LevelFilter` 级别过滤用例改 `mount(level=)`（2 处：`test_level_filter_only_lets_high_levels_through`、`test_replace_without_filter_clears_old_filter`）
- [x] 新增 child 用例：层级命名、缓存命中同一对象、创建时固化（父后改配置不影响已派生节点）、无父级重复投递、`bind()` 产物无 `child`、`child().bind()` 组合语义

## 验证
- [x] `python -m pytest tests -q` 全绿
- [x] `ruff check` 无新增错误

## 备注
- `route` / `publish` 保留不动：`route` 是「这条路换目标的具名缓存视图」（返回 `BoundLogger`），`child` 是「命名层级节点」——两者并存，docstring 讲清分工。
- **方向变更（2026-10-01）**：child 从「扁平化、写时沿父链解析」调整为「缓存树 + 创建时固化」（用户要求），不变量 3 与第 3 节描述已同步翻案。
- 已先行落地：1、2 两个文件（见 `[x]`），如审查不认可可 `git checkout -- <file>` 回退。
