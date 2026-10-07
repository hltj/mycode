# provider_setup 模型提供商配置流程设计

`mycode.provider_setup` 负责 `/provider` 模型提供商管理流程，是通用界面
（`ask_ui` / `filter_ui` / `form_ui`）与数据层（`models_registry` /
`providers`）之间的编排层。

## 数据流

- 候选模型提供商来自 `models_registry.ProviderInfo`：api.json 中
  `npm == "@ai-sdk/openai-compatible"` 的条目，外加 npm 标注了其他包但
  `provider_presets` 预设了 `openai_compatible` 的条目（如 MiniMax 的 4
  条，其 `api` 的 URL path 被替换为预设的 `path`），当前约 189 家；
  `ProviderInfo.models` 为模型 id → `ModelInfo` 映射，含 id/name 与
  `interleaves_reasoning`（由 `interleaved.field == "reasoning_content"`
  推导，是回传开关的默认值来源）。
- 配置写入 `providers.save_provider`（`[providers.<id>]`），当前模型经
  `providers.set_current`。
- 模型级配置写入 `providers.save_model_config`
  （`[providers.<id>.models.<model>]`）：显示名 / 回传
  `reasoning_content` 布尔 / `extra_body`（JSON 字符串）；提供商启用的
  模型 id 列表存于同级的 `enabled_models` 键。
- 「修改设定值」可配提供商级的 `send_reasoning_content` / `extra_body`，
  作为该提供商所有模型的默认请求设置。
- 模型库状态（更新于 … · N 家可用 / 更新失败 / 尚未就绪）来自
  `models_registry.load_meta()` 与 `candidate_providers` 数量。

## /provider 主菜单

`ask_ui` 单问题：

```
模型提供商配置
  ❯ 🟢 添加模型提供商（模型库更新于 09-26 01:00 · 182 家可用）
    ⚪ 添加自定义模型提供商
    ⚪ 编辑：DeepSeek（deepseek · 2 模型）
    ⚪ 编辑：api.openai.com（udf-provider-1 · 1 模型）
    ⚪ 返回
```

- “添加模型提供商”的 `description` 承载模型库状态；除“返回”外各选项
  value 用带前缀标识（`add_from_catalog` / `add_user_defined` / `edit:<id>` /
  `cancel`）区分。提供商名称/id 统一按 `名称（id · N 模型）` 格式展示。
- 循环：一次菜单返回后，若用户未取消则执行对应动作，再回到主菜单；
  选择“返回”退出循环。

## 添加提供商（来自 models.dev）

1. `filter_ui` 单选候选（`名称（id · N 模型）` 作为 label，value=id；
   `ProviderInfo.models` 作为候选数量展示）。已添加的提供商在括号内
   id 前标注「【已添加】」，模型数为 `{m}/{n}`（m = 已选个数）；选中
   已添加项不重复走添加流程，进入「编辑：提供商（id · N 模型）」
   二级菜单。
2. `form_ui` 填写 `ProviderInfo.env` 中的变量：
   - 每个 env 变量一个字段（`name=变量名`，`label=变量名`）；
   - 密钥类（`is_secret_env_var`）`password=True`；
   - 变量名出现在 `api` 模板 `${VAR}` 中时 `hint="用于拼接 API 地址"`。
   - 提交后：密钥类取**第一个**填值存 `api_key`；所有填值组成
     `${VAR}` 渲染上下文，`resolve_base_url` 得到最终 `base_url`。
3. `filter_ui` 多选勾选启用模型（上限 15，达上限禁止继续勾选并提示）：
   label 用 `模型名称（模型id）`，名称取 `ProviderInfo.models` 的显示名
   （缺失回退 id）；候选按显示名排序（不区分大小写，同名回退按 id）。
4. `providers.save_provider`；若此前无任何提供商，同时设为当前（第一个
   勾选模型）。

## 添加自定义提供商

`form_ui` 五字段：

- id 后缀（id_suffix，提供商 id 为 `udf-<后缀>`；默认自动分配
  `provider-N`；留空回退自动分配；重名经校验器阻止提交）
- 显示名（name，必填，空则回退 base_url host）
- Base URL（必填）
- API Key（password，必填）
- 模型列表（逗号分隔；必填，不允许留空）

写入 `[providers.udf-provider-N]`。

## 编辑已配置提供商

二级菜单 `ask_ui` 单问题，按提供商类型出选项（标题用显示名，问题
`description` 承载 `{id} · N 模型`）：

models.dev 提供商：

```
编辑：DeepSeek

deepseek · 2 模型

  ❯ 🟢 修改设定值
    ⚪ 重选模型
    ⚪ 模型配置
    ⚪ 删除提供商
    ⚪ 返回
```

自定义提供商（`udf-` 前缀）无「重选模型」——模型列表在
「修改设定值」表单中编辑：

```
编辑：api.openai.com

udf-provider-1 · 1 模型

  ❯ 🟢 修改设定值
    ⚪ 模型配置
    ⚪ 删除提供商
    ⚪ 返回
```

- **修改设定值**：`form_ui` 字段 id 后缀（仅自定义提供商） / 显示名 /
  Base URL / API Key / 模型列表 / 回传 reasoning_content / extra_body
  （后两项是**提供商级**的默认请求设置，模型级同名配置优先于它们）；自定义提供商（`udf-` 前缀）改后缀即
  重命名提供商 id：section 键更新；若是当前 `model_provider`，顶层键
  同步为新 id。显示名留空回退现有名称；后缀留空保持原 id，重名经
  校验器阻止提交。改后缀后编辑循环跟随新 id 继续。models.dev 提供商
  与自定义提供商统一按此编辑（不做 `${VAR}` 反解）。当前模型不在
  勾选列表：只清当前模型（提供商保留）。
- **重选模型**：仅 models.dev 提供商 → `filter_ui` 多选（回显现有
  勾选）；勾选上限同 15。当前模型被取消勾选：只清当前模型（提供商保留）。
- **模型配置**：两类提供商都有，进入三级菜单（见下节）。
- **删除**：ask_ui 单问题二次确认（标题 `确认删除模型提供商`，描述
  `是否删除模型提供商：{name}（{id} · N 模型）`；`取消删除` 在前——
  光标默认停在该项，直接 Enter / Ctrl-C 均不删除；`确认删除` 才执行）。
  确认后删除 section；若该提供商是当前 `model_provider`，清空当前
  提供商与模型。

## 提供商预设值

`src/mycode/provider_presets.py` 内置一张**提供商预设值表**，承载两类
预设信息，一条记录至少设置一类（可同时设置两类，如 MiniMax）：

- `config`：提供商级请求设置预设（见下）；
- `openai_compatible`：OpenAI 兼容端点预设（见下）。

`ProviderPreset` / `PresetConfig` / `OpenAICompatible` 都是 `frozen`
dataclass（不可变），字段类型即约束；未设置的项为 `None`。每个提供商 id
只出现在一条记录的 `for_providers` 里，索引无需合并。

每条记录（以 MiniMax 为例，两类信息都设置）：

```python
ProviderPreset(
    id="minimax",               # 记录 id（仅便于识别）
    name="MiniMax",             # 显示名（仅便于识别）
    for_providers=("minimax-cn", "minimax", "minimax-cn-coding-plan", "minimax-coding-plan"),
    config=PresetConfig(send_reasoning_content=False),  # 可选；两项至少填一项
    openai_compatible=OpenAICompatible(path="/v1"),     # 可选
)
```

导入时把 `for_providers` 展开成两个索引：`_rebuild_cfg_index()` 得到
「提供商 id → config 副本」（`_CFG_BY_PROVIDER`），`_rebuild_oai_index()`
得到「提供商 id → openai_compatible」（`_OAI_BY_PROVIDER`）。

### config：提供商级请求设置预设

省去为多家共用同一套请求设置的提供商重复填写。`PresetConfig` 两项至少
填一项：

- `send_reasoning_content`：布尔，是否把 `reasoning_content` 回传给模型；
- `extra_body`：透传到请求体的额外字段（JSON）。

`lookup_config(pid)` 返回 `PresetConfig`（未命中返回两项皆空的配置）。
`_CFG_BY_PROVIDER` 里每个 id 各存一份独立副本（`extra_body` 深拷贝），
配合 `frozen` 使调用方改不动配置表；`resolve_extra_body` 另返回深拷贝
供请求使用。

- 生效顺序：模型级 → 提供商级 → 本表 → models.dev 推导；
- 本表只作预设值，用户在 `/provider` 显式配置后即覆盖它；
- 表单里这两项留空时，占位文字会提示回退来源并展示实际生效内容：
  - 开关：按回退优先级标注来源——提供商手动配置 → 本表（`留空=按提供商预置：true/false`）
    → 按提供商类型显示「按模型库默认」或「不回传：false」；
  - `extra_body`：命中本表（或模型级场景下命中提供商级）时直接展示该
    JSON（压成单行），如 `留空=按提供商预置：{"thinking": {"type": "enabled"}}`；
    都没有时给通用示例 `如 DeepSeek 配置 {...}`。

### openai_compatible：OpenAI 兼容端点预设

目前只有 `path`（OpenAI 兼容端点的 URL path，如 `/v1`）。models.dev 里
npm 标注了其他包（如 MiniMax 的 `@ai-sdk/anthropic`）但实际提供 OpenAI
兼容 API 的提供商，由 `models_registry` 用预设 `path` 替换其 `api` 的
URL path（scheme / netloc 保留），从而纳入 OpenAI 兼容候选。

`lookup_openai_compatible(pid)` 返回 `OpenAICompatible`（未命中返回
`None`）。`_OAI_BY_PROVIDER` 的值是 `frozen` 只读对象，直接共享。

## 模型配置（三级菜单）

「模型配置」进入级联菜单，菜单项是该提供商已配置的所有模型
（label 为 `模型名（模型id）`；模型名取已配置的显示名，其次
models.dev 缓存里的名字，都没有时用模型 id），末项「返回」：

```
模型配置：DeepSeek

deepseek · 2 模型

  ❯ 🟢 DeepSeek Chat（deepseek-chat）
    ⚪ DeepSeek Reasoner（deepseek-reasoner）
    ⚪ 返回
```

- 该提供商没有已配置模型时直接返回，不弹空菜单；
- 已配置过开关或 extra_body 的模型在 `description` 标注「已配置」。

选中模型后进入该模型的配置表单（标题 `模型配置：{模型显式名}（{模型id}）`），
三个字段：

| 字段 | 说明 |
|------|------|
| 显示名 | models.dev 提供商留空回退模型库名称，自定义提供商留空回退模型 id。占位文字提示当前生效值 |
| 回传 reasoning_content | 布尔值 `true` / `false`（大小写不敏感），非布尔字面量经校验器报错；留空表示继承。占位文字按生效来源区分：`true/false，留空=按提供商级配置：true/false`（提供商级已配）、`true/false，留空=按模型库默认：true/false`（models.dev 提供商且提供商级未配）、`true/false，留空=不回传：false`（自定义提供商） |
| extra_body | JSON 对象，留空不传；非 JSON 或顶层非对象经校验器报错。占位文字 `如 DeepSeek 配置 {"thinking": {"type": "enabled"}}` |

> **回显必须单行**：`form_ui` 的输入是单行 `Buffer`，多行文本只会显示最后
> 一行。因此已配置的 extra_body 用 `json.dumps(..., ensure_ascii=False)`
> 压成单行回显，不加 `indent`。

- 开关的占位文字提示推导结果（如
  `true/false，留空=按模型库默认：true`）；已显式配置时回显当前值
  （`true` / `false`），未配置则初始值留空；
- 三项全空时删除该模型的配置子表，不留空配置；
- 写入 `providers.save_model_config`，各字段**按需写入**：显示名为空 /
  开关未配置 / extra_body 为空时对应键不落到 TOML，读取时按缺省值处理；
- `save_provider` 编辑提供商设定值时会保留 `models` 子表。

## 模型勾选上限

`MAX_MODELS_PER_PROVIDER = 15`。`filter_ui` 多选中处理：新增勾选前检查
已勾选数量，达上限时忽略新增勾选。回显时只回显前 15 个（已有超限时
截断保留前 15 个）。

## 测试

`tests/test_provider_setup.py` 不弹真实界面：monkeypatch
`provider_setup.ask_ui` / `filter_ui` / `form_ui` 返回预设结果，验证
编排分支与写入结果（`providers.load_providers`）。