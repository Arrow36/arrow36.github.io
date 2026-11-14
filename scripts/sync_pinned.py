#!/usr/bin/env python3
"""Sync GitHub profile pinned repositories for Arrow36 into pinned.json and index.html."""

from __future__ import annotations

from datetime import datetime, timezone
import html as html_lib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

OWNER = "Arrow36"
ROOT_DIR = Path(__file__).resolve().parent.parent
PINNED_JSON_PATH = ROOT_DIR / "pinned.json"
INDEX_HTML_PATH = ROOT_DIR / "index.html"
PIN_BADGE_HTML = '<span class="pin-badge" title="GitHub Pinned Repository · GitHub 置顶项目">📌 置顶</span>'

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes {
        ... on Repository {
          name
        }
      }
    }
  }
}
"""

GRID_RE = re.compile(
    r'(<div class="grid" id="projectGrid">)(.*?)(      </div>\s*<div class="empty-state" id="emptyState">)',
    re.S,
)
ARTICLE_RE = re.compile(r"<article\b([^>]*)>(.*?)</article>", re.S)


def fetch_pinned_via_graphql(token: str) -> list[str]:
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": OWNER}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "arrow36-pinned-sync",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {data['errors']}")

    nodes = (
        data.get("data", {})
        .get("user", {})
        .get("pinnedItems", {})
        .get("nodes", [])
    )
    names: list[str] = []
    for node in nodes:
        if isinstance(node, dict) and node.get("name"):
            name = str(node["name"]).strip()
            if name and name not in names:
                names.append(name)
    return names


def fetch_pinned_via_html() -> list[str]:
    req = urllib.request.Request(
        f"https://github.com/{OWNER}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; arrow36-pinned-sync/1.0)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        page_html = resp.read().decode("utf-8", errors="replace")

    raw_names = re.findall(
        r'pinned-item-list-item.*?<span[^>]*class="[^"]*\brepo\b[^"]*"[^>]*>\s*([^<\s]+)\s*</span>',
        page_html,
        flags=re.S,
    )
    names: list[str] = []
    for raw in raw_names:
        name = html_lib.unescape(raw).strip()
        if name and name not in names:
            names.append(name)
    return names


def fetch_pinned_repos() -> list[str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    names: list[str] = []

    if token:
        try:
            names = fetch_pinned_via_graphql(token)
        except Exception as exc:
            print(f"GraphQL fetch failed ({exc}); falling back to profile HTML.", file=sys.stderr)

    if not names:
        names = fetch_pinned_via_html()

    if not names:
        raise RuntimeError(f"No pinned repositories fetched for {OWNER}; aborting update.")

    return names


def update_index_html(pinned_names: list[str]) -> bool:
    original_text = INDEX_HTML_PATH.read_text(encoding="utf-8")
    normalized = original_text.replace("\r\n", "\n")

    grid_match = GRID_RE.search(normalized)
    if not grid_match:
        raise RuntimeError('Could not find <div class="grid" id="projectGrid"> in index.html')

    grid_open, grid_inner, grid_close = grid_match.groups()
    article_matches = ARTICLE_RE.findall(grid_inner)
    if len(article_matches) < 20:
        raise RuntimeError(f"Expected >= 20 project cards in #projectGrid, found {len(article_matches)}")

    pinned_rank = {name: i for i, name in enumerate(pinned_names)}
    cards: list[tuple[tuple[int, int], str, str]] = []

    for idx, (attrs_str, inner_html) in enumerate(article_matches):
        cat_match = re.search(r'data-category="([^"]+)"', attrs_str)
        if not cat_match:
            raise RuntimeError(f"Missing data-category on card index {idx}")
        category = cat_match.group(1)

        order_match = re.search(r'data-default-order="(\d+)"', attrs_str)
        default_order = int(order_match.group(1)) if order_match else idx

        title_match = re.search(r'<h3 class="card-title"><a[^>]*>([^<]+)</a></h3>', inner_html)
        if not title_match:
            raise RuntimeError(f"Missing card title link on card index {idx}")
        repo_name = html_lib.unescape(title_match.group(1)).strip()

        is_pinned = repo_name in pinned_rank

        # Strip any prior pin badge while preserving indentation
        cleaned_inner = re.sub(
            r'\n?[ \t]*<span class="pin-badge"[^>]*>.*?</span>',
            "",
            inner_html,
            flags=re.S,
        )

        if is_pinned:
            cleaned_inner, count = re.subn(
                r'(<div class="badge-row">)(\n[ \t]*)',
                r"\1\2" + PIN_BADGE_HTML + r"\2",
                cleaned_inner,
                count=1,
            )
            if count != 1:
                raise RuntimeError(f"Missing <div class=\"badge-row\"> in card {repo_name}")

        card_class = "card is-pinned" if is_pinned else "card"
        pinned_attr = ' data-pinned="true"' if is_pinned else ""
        open_tag = (
            f'<article class="{card_class}" data-category="{category}" '
            f'data-repo="{repo_name}" data-default-order="{default_order}"{pinned_attr}>'
        )

        sort_key = (0, pinned_rank[repo_name]) if is_pinned else (1, default_order)
        cards.append((sort_key, open_tag, cleaned_inner))

    cards.sort(key=lambda item: item[0])

    rendered_cards = "\n\n".join(
        f"        {open_tag}{body}</article>" for _, open_tag, body in cards
    )
    new_grid_block = f"{grid_open}\n{rendered_cards}\n{grid_close}"

    updated = normalized[: grid_match.start()] + new_grid_block + normalized[grid_match.end() :]
    updated = re.sub(
        r'(<button[^>]*id="pinnedFilterPill"[^>]*>)(.*?)(</button>)',
        rf"\g<1>📌 置顶精选 ({len(pinned_names)})\g<3>",
        updated,
        count=1,
        flags=re.S,
    )

    if updated != normalized:
        with INDEX_HTML_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(updated)
        return True
    return False


def main() -> int:
    force = "--force" in sys.argv[1:]
    pinned_names = fetch_pinned_repos()

    existing_pinned: list[str] | None = None
    if PINNED_JSON_PATH.exists():
        try:
            existing_data = json.loads(PINNED_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(existing_data, dict) and isinstance(existing_data.get("pinned"), list):
                existing_pinned = existing_data["pinned"]
        except Exception:
            existing_pinned = None

    if existing_pinned == pinned_names and not force:
        print(f"Pinned repositories unchanged ({len(pinned_names)}): {pinned_names}")
        return 0

    payload = {
        "owner": OWNER,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pinned": pinned_names,
    }
    with PINNED_JSON_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    html_changed = update_index_html(pinned_names)
    print(f"Synced pinned repositories ({len(pinned_names)}): {pinned_names} (index_updated={html_changed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
