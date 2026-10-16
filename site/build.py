#!/usr/bin/env python3
"""Validate and build the static project website without third-party dependencies."""

import argparse
from html.parser import HTMLParser
from pathlib import Path
import shutil
from urllib.parse import unquote, urlsplit

SITE = Path(__file__).resolve().parent
SOURCE = SITE / "src"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.links = []
        self.has_title = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "title":
            self.has_title = True
        for key in ("href", "src", "poster"):
            if attrs.get(key):
                self.links.append(attrs[key])


def validate(source):
    source = source.resolve()
    for name in ("index.html", "contributing.html"):
        if not (source / name).is_file():
            raise ValueError(f"Missing source page: {name}")
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Site sources must not contain symbolic links")
    pages = {}
    for path in source.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for host in ("code.alibaba-inc.com", "gitlab.alibaba-inc.com", "registry.anpm.alibaba-inc.com"):
            if host in text:
                raise ValueError(f"Internal infrastructure URL in {path.name}")
        page = Page(text)
        if not page.has_title:
            raise ValueError(f"Missing page title: {path.name}")
        pages[path.resolve()] = page
    for path, page in pages.items():
        for link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            if url.path.startswith("/"):
                raise ValueError(f"Use relative links for project-site hosting: {link}")
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(source) or not target.is_file():
                raise ValueError(f"Missing or out-of-tree local link in {path.name}: {link}")
            if url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
                raise ValueError(f"Missing anchor in {path.name}: {link}")


def build(source=SOURCE, output=SITE / "dist"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Build output must be separate from source")
    validate(source)
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(source, output)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SITE / "dist")
    args = parser.parse_args()
    print(f"Built website in {build(output=args.output)}")
