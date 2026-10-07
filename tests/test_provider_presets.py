"""
提供商预设值表（mycode.provider_presets）测试。

生产表 ``_PROVIDER_PRESETS`` 会随内置厂商增减，测试**不依赖其内容**：
需要具体数据的用例统一 monkeypatch 一张固定的测试表（见 ``table``
fixture）；只校验「表本身的结构约定」时直接读生产表，不假设任何一条记录
存在。

覆盖：

- 表的结构约定（生产表的合法性）：两类预设信息至少设置一类
- ``_rebuild_cfg_index`` / ``_rebuild_oai_index``：``for_providers`` 展开
  为「提供商 id → 只读值」索引
- ``lookup_config``：命中 / 未命中 / 结果只读
- ``lookup_openai_compatible``：命中 / 未命中 / 结果只读
"""

from __future__ import annotations

import pytest

from mycode import provider_presets as pd


@pytest.fixture
def table(monkeypatch) -> list:
    """安装一张固定的测试用表并同步重建两个索引，返回该表。

    生产表会被改（增删内置厂商），故所有依赖具体内容的用例都改用这张表。
    ``monkeypatch`` 负责还原 ``_PROVIDER_PRESETS``、``_CFG_BY_PROVIDER`` 与
    ``_OAI_BY_PROVIDER``。
    """
    t = [
        pd.ProviderPreset(
            id="test-1", name="测试一",
            for_providers=("test-a", "test-a2"),
            config=pd.PresetConfig(
                send_reasoning_content=True,
                extra_body={"thinking": {"type": "enabled"}})),
        pd.ProviderPreset(
            id="test-2", name="测试二",
            for_providers=("test-b",),
            config=pd.PresetConfig(send_reasoning_content=False)),
        pd.ProviderPreset(
            id="test-3", name="测试三",
            for_providers=("test-c",),
            openai_compatible=pd.OpenAICompatible(path="/v1")),
        pd.ProviderPreset(
            id="test-4", name="测试四",
            for_providers=("test-d",),
            config=pd.PresetConfig(send_reasoning_content=True),
            openai_compatible=pd.OpenAICompatible(path="/openai/v1")),
    ]
    monkeypatch.setattr(pd, "_PROVIDER_PRESETS", t)
    monkeypatch.setattr(pd, "_CFG_BY_PROVIDER", pd._rebuild_cfg_index(t))
    monkeypatch.setattr(pd, "_OAI_BY_PROVIDER", pd._rebuild_oai_index(t))
    return t


# ===================================================================
# 表的结构约定（直接校验生产表）
# ===================================================================

class TestTableStructure:
    """表由 dataclass 定义，字段类型即约束；这里校验生产表的数据本身合法。"""

    def test_records_are_dataclass(self):
        """每条记录都是 ProviderPreset；两类信息设置时类型正确。"""
        for entry in pd._PROVIDER_PRESETS:
            assert isinstance(entry, pd.ProviderPreset)
            assert entry.config is None or isinstance(entry.config, pd.PresetConfig)
            assert (entry.openai_compatible is None
                    or isinstance(entry.openai_compatible, pd.OpenAICompatible))

    def test_each_record_has_at_least_one_preset_kind(self):
        """两类预设信息至少设置一类（两类皆空的记录没有意义）。"""
        for entry in pd._PROVIDER_PRESETS:
            assert (entry.config is not None
                    or entry.openai_compatible is not None), \
                f"{entry.id} 两类预设信息都为空"

    def test_non_empty_id_and_name(self):
        """id / name 非空。"""
        for entry in pd._PROVIDER_PRESETS:
            assert entry.id
            assert entry.name

    def test_for_providers_non_empty_strings(self):
        """for_providers 非空且全为字符串。"""
        for entry in pd._PROVIDER_PRESETS:
            assert entry.for_providers, f"{entry.id} 未列 for_providers"
            for pid in entry.for_providers:
                assert isinstance(pid, str) and pid

    def test_config_has_at_least_one_key(self):
        """设置了 config 的记录至少配置了一项（两项皆空的 config 没有意义）。"""
        for entry in pd._PROVIDER_PRESETS:
            if entry.config is None:
                continue
            cfg = entry.config
            assert (cfg.send_reasoning_content is not None
                    or cfg.extra_body), f"{entry.id} 的 config 为空"

    def test_config_fields_are_typed(self):
        """config 两项类型正确（布尔 / dict），未设置时为 None。"""
        for entry in pd._PROVIDER_PRESETS:
            if entry.config is None:
                continue
            cfg = entry.config
            assert cfg.send_reasoning_content is None or \
                isinstance(cfg.send_reasoning_content, bool)
            assert cfg.extra_body is None or isinstance(cfg.extra_body, dict)

    def test_extra_body_is_json_like(self):
        """extra_body 若是 dict，其值应可 JSON 序列化（要作为请求体透传）。"""
        import json

        for entry in pd._PROVIDER_PRESETS:
            if entry.config is None:
                continue
            body = entry.config.extra_body
            if body is not None:
                assert isinstance(body, dict)
                assert json.loads(json.dumps(body)) == body

    def test_openai_compatible_path_well_formed(self):
        """设置了 openai_compatible 的记录，path 是非空且以 / 开头的字符串。"""
        for entry in pd._PROVIDER_PRESETS:
            if entry.openai_compatible is None:
                continue
            path = entry.openai_compatible.path
            assert isinstance(path, str) and path, f"{entry.id} 的 path 为空"
            assert path.startswith("/"), f"{entry.id} 的 path 未以 / 开头"

    def test_records_are_frozen(self):
        """记录不可变（frozen）。"""
        import dataclasses

        entry = pd._PROVIDER_PRESETS[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.id = "changed"                    # type: ignore[misc]

    def test_record_ids_unique(self):
        """记录 id 不重复，便于定位。"""
        ids = [e.id for e in pd._PROVIDER_PRESETS]
        assert len(ids) == len(set(ids))

    def test_provider_id_in_at_most_one_record(self):
        """同一提供商 id 只出现在一条记录里（索引无需合并）。"""
        seen: set[str] = set()
        for entry in pd._PROVIDER_PRESETS:
            for pid in entry.for_providers:
                assert pid not in seen, f"{pid} 出现在多条记录中"
                seen.add(pid)


class TestPresetConfig:
    """PresetConfig：默认值为 None，且 frozen 不可赋值。"""

    def test_defaults_are_none(self):
        """两项默认都不设置（为 None）。"""
        cfg = pd.PresetConfig()
        assert cfg.send_reasoning_content is None
        assert cfg.extra_body is None

    def test_frozen_blocks_assignment(self):
        """字段不可赋值。"""
        import dataclasses

        cfg = pd.PresetConfig(send_reasoning_content=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.send_reasoning_content = False  # type: ignore[misc]


class TestOpenAICompatible:
    """OpenAICompatible：path 必填，且 frozen 不可赋值。"""

    def test_path_required(self):
        """path 无默认值，必须显式给出。"""
        import dataclasses

        fields = dataclasses.fields(pd.OpenAICompatible)
        assert [f.name for f in fields] == ["path"]
        assert fields[0].default is dataclasses.MISSING

    def test_frozen_blocks_assignment(self, table):
        """字段不可赋值。"""
        import dataclasses

        oai = pd.OpenAICompatible(path="/v1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            oai.path = "/v2"  # type: ignore[misc]


# ===================================================================
# lookup
# ===================================================================

class TestLookupConfig:
    def test_matched_returns_config(self, table):
        """命中 for_providers 时返回该条的 config。"""
        got = pd.lookup_config("test-a")
        assert got.send_reasoning_content is True
        assert got.extra_body == {"thinking": {"type": "enabled"}}

    def test_all_listed_providers_match(self, table):
        """记录了 config 的每个提供商 id 都能命中同一份 config。"""
        for entry in table:
            if entry.config is None:
                continue
            for pid in entry.for_providers:
                got = pd.lookup_config(pid)
                assert (got.send_reasoning_content
                        == entry.config.send_reasoning_content)
                assert got.extra_body == entry.config.extra_body

    def test_second_record_resolves(self, table):
        """多条记录各自独立命中。"""
        got = pd.lookup_config("test-b")
        assert got.send_reasoning_content is False
        assert got.extra_body is None

    def test_unmatched_returns_empty(self, table):
        """未命中返回两项皆空的配置。"""
        assert pd.lookup_config("not-in-table").send_reasoning_content is None
        assert pd.lookup_config("not-in-table").extra_body is None

    def test_unmatched_is_not_none(self, table):
        """未命中返回配置对象而非 None。"""
        got = pd.lookup_config("not-in-table")
        assert isinstance(got, pd.PresetConfig)

    def test_lookup_returns_index_entry(self, table):
        """lookup 返回索引里的条目本身。"""
        assert pd.lookup_config("test-a") is pd._CFG_BY_PROVIDER["test-a"]

    def test_result_is_read_only(self, table):
        """结果不可写：调用方无法就地改动配置表。"""
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            pd.lookup_config("test-a").send_reasoning_content = "tampered"  # type: ignore[misc]

    def test_unmatched_result_is_read_only(self, table):
        """未命中时返回的空配置同样不可写。"""
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            pd.lookup_config("not-in-table").extra_body = {}  # type: ignore[misc]

    def test_unmatched_returns_shared_empty(self, table):
        """未命中时返回同一个空配置实例。"""
        assert pd.lookup_config("not-in-table") is pd._EMPTY

    def test_lookup_does_not_mutate_table(self, table):
        """查询不改动配置表。"""
        before = [(e.config, e.openai_compatible) for e in table]

        pd.lookup_config("test-a")
        pd.lookup_config("not-in-table")
        assert [(e.config, e.openai_compatible) for e in table] == before


# ===================================================================
# lookup_openai_compatible
# ===================================================================

class TestLookupOpenAICompatible:
    def test_matched_returns_oai(self, table):
        """命中 for_providers 时返回该条的 openai_compatible。"""
        got = pd.lookup_openai_compatible("test-c")
        assert got is not None
        assert got.path == "/v1"

    def test_record_with_both_kinds_resolves(self, table):
        """两类信息都设置的记录，两类查询各自命中。"""
        assert pd.lookup_openai_compatible("test-d") is not None
        assert pd.lookup_openai_compatible("test-d").path == "/openai/v1"  # type: ignore[union-attr]
        assert pd.lookup_config("test-d").send_reasoning_content is True

    def test_all_listed_providers_match(self, table):
        """记录了 openai_compatible 的每个提供商 id 都能命中同一份。"""
        for entry in table:
            if entry.openai_compatible is None:
                continue
            for pid in entry.for_providers:
                got = pd.lookup_openai_compatible(pid)
                assert got is not None
                assert got.path == entry.openai_compatible.path

    def test_unmatched_returns_none(self, table):
        """未命中返回 None。"""
        assert pd.lookup_openai_compatible("not-in-table") is None
        # test-a 所在记录未设置 openai_compatible → 同样未命中
        assert pd.lookup_openai_compatible("test-a") is None

    def test_lookup_returns_index_entry(self, table):
        """lookup_openai_compatible 返回索引里的条目本身。"""
        assert (pd.lookup_openai_compatible("test-c")
                is pd._OAI_BY_PROVIDER["test-c"])

    def test_result_is_read_only(self, table):
        """结果不可写：调用方无法就地改动预设表。"""
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            pd.lookup_openai_compatible(  # type: ignore[union-attr]
                "test-c").path = "tampered"


# ===================================================================
# _rebuild_cfg_index
# ===================================================================

class TestRebuildCfgIndex:
    def test_default_arg_uses_module_table(self, table):
        """不带参数时用模块里的 _PROVIDER_PRESETS 构建。"""
        assert pd._rebuild_cfg_index() == pd._CFG_BY_PROVIDER

    def test_maps_each_provider_to_its_config(self, table):
        """记录了 config 的每个 for_providers id 都映射到该条的 config。"""
        idx = pd._rebuild_cfg_index(table)
        assert idx["test-a"] == table[0].config
        assert idx["test-a2"] == table[0].config
        assert idx["test-b"] == table[1].config
        assert idx["test-d"] == table[3].config

    def test_configless_record_absent(self, table):
        """未设置 config 的记录不进本索引。"""
        assert "test-c" not in pd._rebuild_cfg_index(table)

    def test_unlisted_provider_absent(self, table):
        """不在任何 for_providers 里的 id 不进索引。"""
        assert "not-in-table" not in pd._rebuild_cfg_index(table)

    def test_empty_table_gives_empty_index(self):
        """空表得到空索引。"""
        assert pd._rebuild_cfg_index([]) == {}

    def test_values_are_independent_copies(self, table):
        """索引值是 config 的独立副本（既非原实例，内容也不同源）。"""
        idx = pd._rebuild_cfg_index(table)
        assert idx["test-a"] is not table[0].config
        assert idx["test-a"].extra_body is not table[0].config.extra_body

    def test_same_record_providers_get_separate_objects(self, table):
        """同一记录下的多个 id 指向等值但彼此独立的副本。"""
        idx = pd._rebuild_cfg_index(table)
        first, second = idx["test-a"], idx["test-a2"]
        assert first == second and first is not second
        assert first.extra_body is not second.extra_body

    def test_does_not_touch_source_table(self, table):
        """构建过程不改源表。"""
        pd._rebuild_cfg_index(table)
        assert table[0].config.send_reasoning_content is True
        assert table[0].config.extra_body == {"thinking": {"type": "enabled"}}
        assert table[0].for_providers == ("test-a", "test-a2")


# ===================================================================
# _rebuild_oai_index
# ===================================================================

class TestRebuildOaiIndex:
    def test_default_arg_uses_module_table(self, table):
        """不带参数时用模块里的 _PROVIDER_PRESETS 构建。"""
        assert pd._rebuild_oai_index() == pd._OAI_BY_PROVIDER

    def test_maps_each_provider_to_its_oai(self, table):
        """记录了 openai_compatible 的每个 for_providers id 都映射到该条。"""
        idx = pd._rebuild_oai_index(table)
        assert idx["test-c"] == table[2].openai_compatible
        assert idx["test-d"] == table[3].openai_compatible

    def test_oai_less_record_absent(self, table):
        """未设置 openai_compatible 的记录不进本索引。"""
        idx = pd._rebuild_oai_index(table)
        assert "test-a" not in idx
        assert "test-b" not in idx

    def test_unlisted_provider_absent(self, table):
        """不在任何 for_providers 里的 id 不进索引。"""
        assert "not-in-table" not in pd._rebuild_oai_index(table)

    def test_empty_table_gives_empty_index(self):
        """空表得到空索引。"""
        assert pd._rebuild_oai_index([]) == {}

    def test_values_are_shared_frozen_objects(self, table):
        """索引值就是记录里的对象本身（frozen 只读，直接共享即可）。"""
        idx = pd._rebuild_oai_index(table)
        assert idx["test-c"] is table[2].openai_compatible
        assert idx["test-d"] is table[3].openai_compatible

    def test_does_not_touch_source_table(self, table):
        """构建过程不改源表。"""
        pd._rebuild_oai_index(table)
        assert table[2].openai_compatible.path == "/v1"
        assert table[3].openai_compatible.path == "/openai/v1"
