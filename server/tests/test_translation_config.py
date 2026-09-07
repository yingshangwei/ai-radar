import tomllib

import pytest
from pydantic import ValidationError

from radar.config import (
    TRANSLATION_RESERVED_OPTIONS,
    RadarConfig,
    TranslationConfig,
)


def test_translation_defaults_preserve_existing_requests():
    config = TranslationConfig()
    assert config.request_options == {"thinking": {"type": "disabled"}}
    assert config.stage_request_options == {}
    assert config.max_tokens == 12000
    assert config.stage_max_tokens == {}
    assert config.timeout_seconds == 120


def test_stage_options_preserve_complete_replacement_and_explicit_empty():
    config = TranslationConfig(
        request_options={"thinking": {"type": "disabled"}, "vendor_flag": "common"},
        stage_request_options={
            "draft": {},
            "correction": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        },
        stage_max_tokens={"correction": 32768},
    )
    assert "draft" in config.stage_request_options
    assert config.stage_request_options["draft"] == {}
    assert config.stage_request_options["correction"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high",
    }
    assert "audit" not in config.stage_request_options
    assert config.stage_max_tokens == {"correction": 32768}
    assert config.max_tokens == 12000
    assert config.request_options["vendor_flag"] == "common"


def test_stage_options_toml_and_unknown_vendor_options_are_supported():
    config = RadarConfig.model_validate(tomllib.loads('''
        [translation]
        timeout_seconds = 300
        [translation.stage_request_options.correction]
        thinking = { type = "enabled" }
        reasoning_effort = "high"
        vendor_extension = { controls = ["a", "b"], enabled = true }
        [translation.stage_request_options.audit]
        [translation.stage_max_tokens]
        correction = 32768
        audit = 32768
    ''')).translation
    assert config.timeout_seconds == 300
    assert config.stage_request_options["audit"] == {}
    assert config.stage_request_options["correction"]["vendor_extension"] == {
        "controls": ["a", "b"], "enabled": True,
    }
    assert config.stage_max_tokens == {"correction": 32768, "audit": 32768}


@pytest.mark.parametrize("field", ["stage_request_options", "stage_max_tokens"])
@pytest.mark.parametrize("stage", ["review", "DRAFT", "secret-invalid-stage", 1])
def test_invalid_stage_is_rejected_without_echoing_dynamic_key(field, stage):
    with pytest.raises(ValidationError) as error:
        TranslationConfig(**{field: {stage: {} if field == "stage_request_options" else 512}})
    message = str(error.value)
    assert "Translation stage must be draft, correction or audit" in message
    assert "secret-invalid-stage" not in message
    assert "input_value" not in message


@pytest.mark.parametrize("key", sorted(TRANSLATION_RESERVED_OPTIONS))
@pytest.mark.parametrize("stage", [None, "correction"])
def test_workflow_fields_cannot_be_overridden_in_options(key, stage):
    options = {key: "secret-option-value"}
    kwargs = {"request_options": options} if stage is None else {
        "stage_request_options": {stage: options},
    }
    with pytest.raises(ValidationError) as error:
        TranslationConfig(**kwargs)
    message = str(error.value)
    assert "reserved workflow field" in message
    assert "secret-option-value" not in message
    assert "input_value" not in message


@pytest.mark.parametrize("options", [[], None, "secret-options-value", {1: "secret-options-value"}])
@pytest.mark.parametrize("stage", [None, "audit"])
def test_options_must_be_objects_with_string_keys(options, stage):
    kwargs = {"request_options": options} if stage is None else {
        "stage_request_options": {stage: options},
    }
    with pytest.raises(ValidationError) as error:
        TranslationConfig(**kwargs)
    assert "secret-options-value" not in str(error.value)


@pytest.mark.parametrize("value", [256, 65536])
def test_token_budget_inclusive_boundaries(value):
    config = TranslationConfig(max_tokens=value, stage_max_tokens={"draft": value})
    assert config.max_tokens == value
    assert config.stage_max_tokens["draft"] == value


@pytest.mark.parametrize("value", [255, 65537, 0, -1, True, 512.0, "512", None])
@pytest.mark.parametrize("stage", [None, "audit"])
def test_token_budgets_require_bounded_integers(value, stage):
    kwargs = {"max_tokens": value} if stage is None else {"stage_max_tokens": {stage: value}}
    with pytest.raises(ValidationError):
        TranslationConfig(**kwargs)


def test_nested_config_errors_do_not_echo_options_or_other_secrets():
    with pytest.raises(ValidationError) as error:
        RadarConfig.model_validate({"translation": {
            "request_options": {"extra_body": {"credential": "secret-request-value"}},
            "auxiliary_url": "https://secret-config-value.invalid",
        }})
    message = str(error.value)
    assert "secret-request-value" not in message
    assert "secret-config-value" not in message
    assert "input_value" not in message


def test_mutable_translation_defaults_are_not_shared():
    first, second = TranslationConfig(), TranslationConfig()
    first.stage_request_options["audit"] = {}
    first.stage_max_tokens["audit"] = 512
    first.request_options["thinking"]["type"] = "enabled"
    assert second.stage_request_options == {}
    assert second.stage_max_tokens == {}
    assert second.request_options == {"thinking": {"type": "disabled"}}
