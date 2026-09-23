"""Menu (config) tools: list_menus, list_menu_items, list_menu_fields.

Thin GET wrappers — assert the project-scoped path and the success/error
string contract.
"""
import json

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

list_menus = tool_fn("list_menus")
list_menu_items = tool_fn("list_menu_items")
list_menu_fields = tool_fn("list_menu_fields")


def test_list_menus_path(project, token_req):
    token_req.return_value = {"menusData": [{"id": 1, "name": "Product"}]}
    out = list_menus()
    token_req.assert_called_once_with("GET", "/PROJ/configs/menus")
    assert json.loads(out)["menusData"][0]["name"] == "Product"


def test_list_menus_error_returns_string(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 404 Not Found: x")
    assert list_menus() == "Error: HTTP 404 Not Found: x"


def test_list_menu_items_path(project, token_req):
    token_req.return_value = {"itemsData": []}
    list_menu_items(2)
    token_req.assert_called_once_with("GET", "/PROJ/configs/menus/2/items")


def test_list_menu_fields_path(project, token_req):
    token_req.return_value = {"fieldsData": []}
    list_menu_fields(2)
    token_req.assert_called_once_with("GET", "/PROJ/configs/menus/2/fields")
