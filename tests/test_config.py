"""配置快照与 jmcomic 选项构造测试。"""

from __future__ import annotations

from astrbot_jmdownloader.core.config import (
    PluginConfig,
    flatten_config,
    normalize_proxy,
    parse_cookies,
)


def test_defaults():
    config = PluginConfig.from_mapping({})
    assert config.enabled is True
    assert config.encrypt_pdf is True
    assert config.split_auto is True
    assert config.split_size_mb == 100
    assert config.split_max_volumes == 8
    assert config.cache_albums == 5
    assert config.max_concurrent == 1


def test_bool_and_int_clamping():
    config = PluginConfig.from_mapping(
        {
            "enabled": "false",
            "encrypt_pdf": "on",
            "split_auto": 0,
            "split_size_mb": -10,
            "split_max_volumes": 99,
            "cache_albums": 500,
            "upload_retries": -3,
            "timeout": 1,
            "image_threads": 1000,
            "max_concurrent": 9,
        }
    )
    assert config.enabled is False
    assert config.encrypt_pdf is True
    assert config.split_auto is False
    assert config.split_size_mb == 0
    assert config.split_max_volumes == 20
    assert config.cache_albums == 50
    assert config.upload_retries == 0
    assert config.timeout == 5
    assert config.image_threads == 32
    assert config.max_concurrent == 3


def test_split_enabled_semantics():
    assert PluginConfig(split_auto=True, split_size_mb=0).split_enabled is True
    assert (
        PluginConfig(split_auto=False, split_size_mb=0).split_enabled is False
    )
    assert (
        PluginConfig(split_auto=False, split_size_mb=80).split_enabled is True
    )


def test_parse_cookies():
    assert parse_cookies("a=1; b=2") == {"a": "1", "b": "2"}
    assert parse_cookies(" broken ; c = 3 ") == {"c": "3"}
    assert parse_cookies("") == {}


def test_normalize_proxy():
    assert normalize_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert normalize_proxy("socks5://127.0.0.1:1080") == (
        "socks5://127.0.0.1:1080"
    )
    assert normalize_proxy("") == ""


def test_client_option_dict():
    config = PluginConfig.from_mapping(
        {
            "cookies": "a=b; c=d",
            "proxy": "127.0.0.1:7890",
            "timeout": 30,
            "retry_times": 4,
            "image_threads": 8,
            "photo_threads": 2,
        }
    )
    option = config.client_option_dict()
    assert option["client"]["impl"] == "api"
    assert option["client"]["retry_times"] == 4
    metadata = option["client"]["postman"]["meta_data"]
    assert metadata["timeout"] == 30
    assert metadata["cookies"] == {"a": "b", "c": "d"}
    assert metadata["proxies"]["http"] == "http://127.0.0.1:7890"
    assert option["download"]["threading"] == {"image": 8, "photo": 2}
    assert option["download"]["image"] == {"decode": True, "suffix": ".jpg"}
    assert "dir_rule" not in option


def test_jmcomic_option_dict_dir_rule(tmp_path):
    config = PluginConfig()
    option = config.jmcomic_option_dict(tmp_path)
    assert option["dir_rule"] == {
        "rule": "Bd/Pid",
        "base_dir": str(tmp_path),
    }


def test_flatten_config_nested_groups():
    flat = flatten_config(
        {
            "basic": {"enabled": False, "pdf_password": "pass"},
            "split": {"split_auto": False, "split_size_mb": 60},
            "network": {"timeout": 30},
        }
    )
    assert flat == {
        "enabled": False,
        "pdf_password": "pass",
        "split_auto": False,
        "split_size_mb": 60,
        "timeout": 30,
    }


def test_from_mapping_supports_nested_groups():
    config = PluginConfig.from_mapping(
        {
            "basic": {
                "enabled": False,
                "allow_group": False,
                "encrypt_pdf": True,
                "pdf_password": " fixed-pass ",
            },
            "split": {
                "split_auto": False,
                "split_size_mb": 60,
                "split_max_volumes": 4,
            },
            "upload": {"callback_api_base": "http://astrbot:6185/"},
            "cache": {"cache_albums": 9},
            "network": {
                "cookies": "a=b",
                "proxy": "127.0.0.1:7890",
                "timeout": 30,
                "retry_times": 4,
                "image_threads": 8,
                "photo_threads": 2,
                "max_concurrent": 2,
            },
        }
    )
    assert config.enabled is False
    assert config.pdf_password == "fixed-pass"
    assert config.split_auto is False
    assert config.split_size_mb == 60
    assert config.callback_api_base == "http://astrbot:6185/"
    assert config.cache_albums == 9
    assert config.timeout == 30
    assert config.max_concurrent == 2


def test_from_mapping_still_accepts_flat_config():
    config = PluginConfig.from_mapping(
        {"enabled": False, "pdf_password": "legacy", "split_auto": False}
    )
    assert config.enabled is False
    assert config.pdf_password == "legacy"
    assert config.split_auto is False
