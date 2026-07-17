"""Regression tests for failures caught by the strict quality gates."""

from oculai_mcp.tools.errors import ValidationError
from oculai_mcp.utils.html_denoise import html_to_fit_markdown


def test_error_instance_can_override_subclass_defaults() -> None:
    error = ValidationError("unprocessable", code="CUSTOM_VALIDATION", status_code=422)

    assert error.code == "CUSTOM_VALIDATION"
    assert error.status_code == 422
    assert error.to_dict()["error"]["code"] == "CUSTOM_VALIDATION"


def test_html_denoiser_handles_multi_value_attributes_and_relative_links() -> None:
    html = (
        '<html><body><main class="article content"><h1>Title</h1><p>'
        + "Useful content " * 30
        + '</p><a href="/profile">Profile</a></main></body></html>'
    )

    markdown = html_to_fit_markdown(html, "https://example.com/jobs")

    assert "# Title" in markdown
    assert "[Profile](https://example.com/profile)" in markdown
