import os
import pytest

os.environ["TESTING"] = "True"

from src.main import sanitize_input


@pytest.mark.parametrize("test_input,expected", [
    ("Hello World", "Hello World"),
    ("<script>alert('XSS')</script>", "&lt;script&gt;alert('XSS')&lt;/script&gt;"),
    ("Some text with !@#$%^&*()", "Some text with !@#$%^&*()"),
    ("  leading and trailing spaces  ", "  leading and trailing spaces  "),
    ("Text with\nnewline", "Text with\nnewline"),
    ("Text with weird chars <>'\"`", "&lt;&gt;'\""),
    (123, ""),
    (None, ""),
])
def test_sanitize_input(test_input, expected):
    assert sanitize_input(test_input) == expected


def test_sanitize_input_removes_disallowed_chars():
    input_str = "Text with `backtick` and ~tilde~ and |pipe|"
    expected_str = "Text with backtick and tilde and pipe"
    assert sanitize_input(input_str) == expected_str
