# 示例任务

安装 SmithCode 后，可以用下面的任务体验工具调用能力（在任意工作目录运行，建议先在测试目录尝试）。

## 单次任务模式

```bash
# 读代码并解释
smithcode 解释当前目录下 main.py 每个函数的作用

# 生成文件
smithcode 写一个 Python 脚本统计当前目录所有 .py 文件的总行数

# 修改代码
smithcode 把 utils.py 里的日期字符串统一改成 ISO 8601 格式

# 运行并修复
smithcode 运行 pytest，如果有测试失败就修复它们
```

## 交互模式

```bash
smithcode          # 进入 REPL
```

进入后直接描述任务，会话内支持多轮追问；`/new` 清空上下文，`/save` 导出会话记录。
