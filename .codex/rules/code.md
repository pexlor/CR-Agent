# Python Code Style Guide

## 1. 基本要求

- Python >= 3.10。
- 遵循 PEP 8。
- 使用 4 个空格缩进，禁止 Tab。
- 单行长度不超过 100 字符。
- 文件统一 UTF-8 编码。
- 优先使用 Python 标准库，避免无必要引入第三方依赖。

## 2. 命名规范

- 文件名：snake_case.py
- 变量、函数：snake_case
- 类名：PascalCase
- 常量：UPPER_SNAKE_CASE
- 私有成员：以 `_` 开头。
- 名字必须表达业务含义，禁止大量使用 `data`、`tmp`、`obj`、`res` 等模糊命名。

推荐：
user_info
retry_count
load_config()

禁止：
d
tmp
handle()
do_something()

## 3. 类型标注

- 新增函数必须提供参数和返回值类型。
- 优先使用 Python 3.10+ 类型语法。

推荐：

def get_user(user_id: str) -> User | None:
    ...

禁止：

def get_user(user_id):
    ...

- 复杂类型可使用 type alias。
- 禁止为了消除类型检查错误大量使用 `Any`。

## 4. 函数设计

- 一个函数只负责一个明确职责。
- 单个函数原则上不超过 50 行。
- 参数原则上不超过 5 个，超过时考虑使用 dataclass / config object。
- 避免超过 3 层的嵌套。
- 优先使用 early return，减少嵌套。

推荐：

if not user:
    return None

if not user.enabled:
    return None

return process(user)

不推荐：

if user:
    if user.enabled:
        return process(user)

## 5. 类设计

- 类应该具有明确职责。
- 不创建无意义的 Manager / Utils / Helper 大杂烩类。
- 无状态逻辑优先使用函数，而不是强制封装成类。
- 数据对象优先使用 `dataclass`。
- 通过组合优先于复杂继承。

## 6. 异常处理

- 禁止：

try:
    ...
except Exception:
    pass

- 只捕获能够处理的异常。
- 异常信息必须包含必要上下文。
- 不使用异常控制正常业务流程。
- 底层异常需要保留原始异常链：

raise ConfigError(f"invalid config: {path}") from exc

## 7. 日志

- 禁止使用 print 输出运行日志。
- 使用 logging。
- 日志必须包含必要上下文，例如 request_id、task_id、user_id。
- 禁止打印密码、Token、Secret、Cookie 等敏感信息。

推荐：

logger.error(
    "failed to process request, request_id=%s, error=%s",
    request_id,
    exc,
)

## 8. 注释和 Docstring

- 注释解释“为什么”，而不是重复代码。
- 公共类、公共函数、复杂逻辑必须提供 docstring。
- 简单函数不强制写无意义 docstring。

禁止：

# 增加 count
count += 1

推荐：

# Retry count starts from 1 because 0 represents the initial request.
retry_count += 1

## 9. 数据结构

- 数据对象优先使用 dataclass / TypedDict / Pydantic Model。
- 禁止在业务代码中长期传递结构不明确的大型 dict。
- Magic number 必须定义成常量或配置。

## 10. 文件组织

推荐：

project/
├── app/
│   ├── api/
│   ├── service/
│   ├── repository/
│   ├── model/
│   └── utils/
├── tests/
├── config/
├── scripts/
└── README.md

- 不允许单文件无限膨胀。
- 同一业务领域代码放在同一模块中。
- 避免循环依赖。

## 11. 测试

- 新功能必须补充测试。
- Bug 修复必须补充对应回归测试。
- 测试命名：

def test_<function>_<scenario>_<expected>():
    ...

例如：

def test_create_user_invalid_email_raise_error():
    ...

- 至少覆盖：
  - 正常路径
  - 边界条件
  - 异常路径

## 12. AI 生成代码要求

AI 修改代码时必须：

1. 优先阅读和遵循现有项目结构。
2. 不进行与当前需求无关的重构。
3. 尽量保持改动范围最小。
4. 不随意修改公共接口。
5. 不随意增加第三方依赖。
6. 不删除已有异常处理、日志或测试，除非明确说明原因。
7. 修改逻辑后同步补充或修改测试。
8. 不确定需求时明确指出假设。
9. 禁止使用 TODO 代替核心实现。
10. 输出代码前检查明显的语法、类型和逻辑问题。