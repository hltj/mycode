# model_switch 模型切换设计

`/model` 命令用于从所有已配置模型提供商中切换当前使用的模型。

## 交互模型

**外挂 tab 切换提供商 + ask_ui 单问题选择模型。**不采用 ask_ui 多问题
（多问题必须逐题作答，不符合“从某一个提供商选一个模型”的语义）。

- 顶部标题行显示当前提供商，含位置指示：
  ``◄ DeepSeek (2/5) ►　←→ 切换提供商``
- 主体为 ask_ui 单问题：选项是该提供商勾选的模型，显示
  ``模型名称（模型id）``；当前使用的模型项标注“当前”。
- ``←`` / ``→`` / ``tab`` 切到相邻提供商并重建主体（记忆光标，回到该
  提供商时恢复）；Enter 选定模型 → 写回并生效；Ctrl-C 取消不改动。

## ask_ui 横向导航扩展点

现有 ask_ui 单问题没有左右导航语义。为支持外挂 tab 轮换，为 `ask_ui`
增加可选参数：

- ``on_navigate: Callable[[int], bool] | None``：左右/tab 键触发，
  参数为方向（-1/+1）；返回 True 表示已处理（调用方重建问题），
  False 表示不处理（维持默认无行为）。
- 仅在单问题模式（``len(questions) == 1``）且 on_navigate 非 None 时
  绑定 left/right（及 C-b/C-f）/tab；多问题保持既有导航。
- 实现方式：回调触发时退出当前 ask_ui 实例（记录 cursor_index），由
  调用方在循环里用相邻 provider 重建并再次调用 ask_ui。

## model_select.py

```python
def choose_model() -> tuple[str, str] | None:
    """返回选中的 (provider_id, model_id)；取消返回 None。"""
```

- 读 `providers.load_providers()`；空则打印一行提示，返回 None。
- 按 provider id 排序（保持稳定）。
- 每次进入重建 ask_ui 问题：标题为 ``◄ 名称 (i/n) ►　←→ 切换提供商``，
  选项是当前提供商勾选的模型（标“当前”）。
- ``on_navigate`` 回调：切换 provider index 并返回 True；调用方重新进入
  ask_ui（初始 cursor_index 用该 provider 上次记忆的 cursor）。
- Enter 选定后：
  - ``providers.set_current(pid, model)``；
  - ``cli.get_client(refresh=True)`` 重建 OpenAI client（切换生效）；
  - 返回 (pid, model)。

## cli 集成

- ``client`` 改为懒构建：``get_client(refresh=False)`` 按当前
  ``model_provider`` / ``model_name`` 从 providers 读 base_url/api_key
  构建 OpenAI 实例；``refresh=True`` 关闭旧实例并重建。
- ``/model`` 命令命中时：``choose_model()``；成功返回后派发
  ``ModelChangeEvent(model=f"{pid}/{model}", provider=pid, model_name=model)``。
- 补全列表加入 ``/model``（连同 ``/provider``）。

## 事件持久化

``session.ModelChangeEvent``（新增）：字段 ``provider``、``model_name``；
``model`` 字段写 ``f"{provider}/{model_name}"``。
进入历史但不作为消息注入模型。

## renderer

两风格渲染 ``ModelChangeEvent`` 一行：
``已切换模型：DeepSeek/deepseek-chat``。

## 测试

- `tests/test_session.py`：ModelChangeEvent 序列化/反序列化。
- `tests/test_renderer.py`：渲染文案含 `已切换模型：DeepSeek/deepseek-chat`。
- `tests/test_model_select.py`：monkeypatch ask_ui / providers，验证
  提供商轮换、选定写回、取消不改动。
- `tests/test_cli.py`：CLI 主循环识别到 `/model`、`/provider` 命令时
  调用对应处理函数（`choose_model` / `run_provider_setup`）。

## 开发规范

- 测试以 TDD 方式编写（见 `tests/test_model_select.py` /
  `tests/test_session.py` / `tests/test_renderer.py`）：交互流程经
  monkeypatch `ask_ui` 返回预设结果驱动，事件走 JSONL 往返断言。
- 新增渲染样式：ModelChangeEvent 复用 renderer 已有的提示文案样式
  （`\x1B[90m` 灰色与模式切换一致），不新增样式类，两风格无需额外登记。