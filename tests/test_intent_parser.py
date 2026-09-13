"""
Unit tests for intent_parser.parse_intent(), using the README's example
utterances (see "Execution API" and "One-shot execute examples" sections).
"""

import pytest

from intent_parser import parse_intent


def test_turn_on_office_light():
    intent = parse_intent("turn on the office light")
    assert intent.action == "turn_on"
    assert intent.device_query == "turn on the office light"
    assert intent.params == {}


def test_turn_off_variant_phrasing():
    intent = parse_intent("switch off the kitchen light")
    assert intent.action == "turn_off"


def test_dim_desk_light_to_40_percent():
    intent = parse_intent("dim the desk light to 40%")
    assert intent.action == "set_brightness"
    assert intent.params == {"brightness": 40}


def test_set_brightness_phrase_without_percent_sign():
    intent = parse_intent("set brightness to 75")
    assert intent.action == "set_brightness"
    assert intent.params == {"brightness": 75}


def test_brightness_is_clamped_to_100():
    intent = parse_intent("dim to 150")
    assert intent.action == "set_brightness"
    assert intent.params == {"brightness": 100}


def test_dim_without_a_number_yields_no_brightness_param():
    # app._execute_device() is documented to reject this with a 400 asking
    # for an explicit value — the parser itself just reports no params.
    intent = parse_intent("dim the light")
    assert intent.action == "set_brightness"
    assert intent.params == {}


def test_status_query_is_not_confused_with_turn_on():
    # "is it on" contains the bare word "on" but must not match the
    # turn_on rule (which requires "turn on"/"switch on"/"power on"/etc).
    intent = parse_intent("is it on")
    assert intent.action == "get_status"


def test_toggle_command():
    intent = parse_intent("toggle the fan")
    assert intent.action == "toggle"


def test_unrecognised_action_defaults_to_get_status():
    intent = parse_intent("office light please")
    assert intent.action == "get_status"


def test_too_short_command_raises_value_error():
    with pytest.raises(ValueError):
        parse_intent("on")


def test_empty_command_raises_value_error():
    with pytest.raises(ValueError):
        parse_intent("")
