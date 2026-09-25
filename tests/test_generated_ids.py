"""actions.id_looks_generated() -- tells a chart library's per-load random
id apart from an authored one. The positives are real ids taken from a
LinkedIn user's OrangeHRM run and the public demo (every one of them made
a replay's `#id` selector time out); the negatives are ordinary authored
ids, where a false positive would needlessly demote a good locator."""
import pytest

from flowscout.actions import id_looks_generated

GENERATED = [
    # from the reported run
    "xyKbEpDm", "rEcis_IZ", "DHuYVyY6", "Yw9o2G97",
    # from the same app on the public demo
    "1ZzSkjr6", "THYtyqxh", "A3RHS-sP", "5c4z-i3V", "ES8FI-K0", "3WGE4Vpp",
]

AUTHORED = [
    "user-name", "shopping_cart_link", "inventory_sidebar_link", "login-button",
    "loginButton", "submitBtn", "btnSubmit2", "myInputField", "getUserId",
    "firstName", "dropDownMenu", "react-burger-menu-btn", "root", "__next",
    "menu2", "checkout", "", "a",
]


@pytest.mark.parametrize("value", GENERATED)
def test_random_ids_are_detected(value):
    assert id_looks_generated(value), value


@pytest.mark.parametrize("value", AUTHORED)
def test_authored_ids_are_left_alone(value):
    assert not id_looks_generated(value), value
