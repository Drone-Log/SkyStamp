"""The website states the app version in three places; a release must update all of them."""
import json
import re
from pathlib import Path

from skystamp import __version__

DOCS = Path(__file__).resolve().parent.parent / "docs"
HTML = (DOCS / "index.html").read_text(encoding="utf-8")


def _structured_data():
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', HTML, re.S)]


def test_structured_data_is_valid_json():
    types = {block["@type"] for block in _structured_data()}
    assert {"SoftwareApplication", "FAQPage"} <= types


def test_site_version_matches_app():
    app = next(b for b in _structured_data() if b["@type"] == "SoftwareApplication")
    assert app["softwareVersion"] == __version__
    assert f'<span id="version">{__version__}</span>' in HTML
    major_minor = ".".join(__version__.split(".")[:2])
    assert f"Current version: {major_minor}" in (DOCS / "llms.txt").read_text(encoding="utf-8")


def test_indexnow_key_file_matches_workflow():
    workflow = (DOCS.parent / ".github" / "workflows" / "indexnow.yml").read_text(encoding="utf-8")
    key = re.search(r"^\s*KEY:\s*(\w+)", workflow, re.M).group(1)
    assert (DOCS / f"{key}.txt").read_text(encoding="utf-8").strip() == key
