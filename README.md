# mycode - 编程智能体

> ⚠️ **警告**：本项目正在开发中，不建议在非测试环境使用。

## 功能特性

- **智能对话**：基于 OpenAI 兼容 API 的交互式编程助手
- **工具调用**：支持自定义工具注册系统（ToolsRegistry）
- **命令执行**：集成 bash 工具，直接执行 shell 命令
- **会话管理**：完整的对话上下文管理，支持多轮交互
- **命令历史**：持久化保存输入历史，支持上下键翻阅
- **自动补全**：内置命令补全功能

## 项目结构

```
myc/
├── myc.py          # 主程序入口
├── session.py      # 会话管理模块
├── tools.py        # 内置工具定义
├── tools_reg.py    # 工具注册
├── test_session.py # 会话测试
├── test_tools.py   # 工具测试
└── test_tools_reg.py # 工具注册测试
```

## 快速开始

### 前置要求

- Python 3.10+
- OpenAI API 密钥（或兼容的 API 服务）

### 安装依赖

```bash
pip install openai python-dotenv prompt_toolkit
```

### 配置环境变量

编辑 `.env` 文件：

```bash
API_KEY=your_api_key_here
BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-5.5
MYCODE_HOME_DIR=~/.mycode
```

### 运行

```bash
python myc.py
```

## 使用方法

启动后会进入交互式命令行界面，直接输入你的需求即可。例如：

- `创建一个 Python 项目结构`
- `在当前目录列出所有文件`
- `帮我写一个 Flask Web 应用`

## 开发

### 运行测试

```bash
pytest test_session.py -v
pytest test_tools.py -v
pytest test_tools_reg.py -v
```

### 代码风格

使用 mypy 进行类型检查：

```bash
mypy --exclude "test_.*\.py$" .
```

## License

MIT License. 详见 [LICENSE](./LICENSE) 文件。
