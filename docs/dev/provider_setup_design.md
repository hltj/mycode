# provider_setup 模型提供商配置流程设计

`mycode.provider_setup` 负责 `/provider` 模型提供商管理流程，是通用界面
（`ask_ui` / `filter_ui` / `form_ui`）与数据层（`models_registry` /
`providers`）之间的编排层。

## 数据流

- 候选模型提供商来自 `models_registry.ProviderInfo`（api.json 中所有
  `npm == "@ai-sdk/openai-compatible"` 的条目，当前约 182 家）。
- 配置写入 `providers.save_provider`（`[providers.<id>]`），当前模型经
  `providers.set_current`。
- 模型库状态（更新于 … · N 家可用 / 更新失败 / 尚未就绪）来自
  `models_registry.load_meta()` 与 `candidate_providers` 数量。

## /provider 主菜单

`ask_ui` 单问题：

```
模型提供商配置
  ❯ 🟢 添加模型提供商（模型库更新于 09-26 01:00 · 182 家可用）
    ⚪ 添加自定义模型提供商
    ⚪ 编辑：deepseek（DeepSeek · 2 模型）
    ⚪ 编辑：user-defined-1（api.openai.com · 1 模型）
    ⚪ 取消
```

- “添加模型提供商”的 `description` 承载模型库状态；除“取消”外各选项
  value 用带前缀标识（`add_from_catalog` / `add_custom` / `edit:<id>` /
  `cancel`）区分。
- 循环：一次菜单返回后，若用户未取消则执行对应动作，再回到主菜单；
  取消退出循环。

## 添加提供商（来自 models.dev）

1. `filter_ui` 单选候选（`名称 (id) · N 模型` 作为 label，value=id；
   `ProviderInfo.models` 作为候选数量展示）。
2. `form_ui` 填写 `ProviderInfo.env` 中的变量：
   - 每个 env 变量一个字段（`name=变量名`，`label=变量名`）；
   - 密钥类（`is_secret_env_var`）`password=True`；
   - 变量名出现在 `api` 模板 `${VAR}` 中时 `hint="用于拼接 API 地址"`。
   - 提交后：密钥类取**第一个**填值存 `api_key`；所有填值组成
     `${VAR}` 渲染上下文，`resolve_base_url` 得到最终 `base_url`。
3. `filter_ui` 多选勾选启用模型（上限 15，达上限禁止继续勾选并提示）：
   label 用 `模型名称（模型id）`；模型名称来自 api.json 该模型的 `name`
   字段（缺失回退 id）。
4. `providers.save_provider`；若此前无任何提供商，同时设为当前（第一个
   勾选模型）。

## 添加自定义提供商

`form_ui` 四字段：

- 显示名（name，必填，空则回退 base_url host）
- Base URL（必填）
- API Key（password，可留空——本地服务无密钥）
- 模型列表（逗号分隔；必填，不允许留空）

写入 `[providers.user-defined-N]`。

## 编辑已配置提供商

二级菜单 `ask_ui` 单问题：

```
编辑：deepseek
  ❯ 🟢 修改变量
    ⚪ 重选模型
    ⚪ 删除提供商
    ⚪ 返回
```

- **修改变量**：`form_ui` 三字段 Base URL / API Key / 模型列表
  （models.dev 提供商与自定义提供商统一按此编辑；不做 `${VAR}` 反解）。
- **重选模型**：models.dev 提供商 → `filter_ui` 多选（回显现有勾选）；
  自定义提供商 → 也走模型列表编辑（同名字段）；勾选上限同 15。
- **删除**：删除 section；若该提供商是当前 `model_provider`，同时
  清空当前模型并提示先切换。

## 模型勾选上限

`MAX_MODELS_PER_PROVIDER = 15`。`filter_ui` 多选中处理：新增勾选前检查
已勾选数量，达上限时忽略新增勾选。回显时只回显前 15 个（已有超限时
截断保留前 15 个）。

## 测试

`tests/test_provider_setup.py` 不弹真实界面：monkeypatch
`provider_setup.ask_ui` / `filter_ui` / `form_ui` 返回预设结果，验证
编排分支与写入结果（`providers.load_providers`）。