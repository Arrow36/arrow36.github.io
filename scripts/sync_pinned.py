#!/usr/bin/env python3
"""Sync all Arrow36 public GitHub repositories & pinned items into pinned.json, index.html, sitemap.xml, and README.md."""

from __future__ import annotations

from datetime import datetime, timezone
import html as html_lib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

OWNER = "Arrow36"
SITE_REPO = "arrow36.github.io"
ROOT_DIR = Path(__file__).resolve().parent.parent
PINNED_JSON_PATH = ROOT_DIR / "pinned.json"
INDEX_HTML_PATH = ROOT_DIR / "index.html"
SITEMAP_XML_PATH = ROOT_DIR / "sitemap.xml"
README_MD_PATH = ROOT_DIR / "README.md"

PIN_BADGE_HTML = '<span class="pin-badge" title="GitHub Pinned Repository · GitHub 置顶项目">📌 置顶</span>'

CATEGORY_ORDER = {"web": 0, "media": 1, "hardware": 2, "nlp": 3}

NLP_KEYWORDS = (
    "hangul",
    "hanja",
    "nlp",
    "agent-skill",
    "claude-code",
    "skill",
    "llm",
    "dictionary",
    "translator",
)
HARDWARE_KEYWORDS = (
    "armbian",
    "esp32",
    "linux",
    "kernel",
    "driver",
    "bcm",
    "ubuntu",
    "kindle",
    "gpio",
    "jtag",
    "openocd",
    "firmware",
    "adb",
    "root",
    "mipi",
    "amlogic",
    "e-ink",
    "ble-reader",
    "systemproxy",
)

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


def _github_headers(token: str | None = None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "arrow36-repo-sync/2.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_pinned_via_graphql(token: str) -> list[str]:
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": OWNER}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        method="POST",
        headers={**_github_headers(token), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {data['errors']}")

    nodes = data.get("data", {}).get("user", {}).get("pinnedItems", {}).get("nodes", [])
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
            "User-Agent": "Mozilla/5.0 (compatible; arrow36-repo-sync/2.0)",
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


def fetch_pinned_repos(token: str) -> list[str]:
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


def classify_repo(repo: dict) -> tuple[str, str, bool]:
    homepage = repo["homepage"]
    has_pages = repo["has_pages"]
    if has_pages or (homepage.startswith("http") and "github.com" not in homepage.lower()):
        return "web", "Live Web", True

    haystack = f"{repo['name']} {repo['description']} {' '.join(repo['topics'])}".lower()
    if any(kw in haystack for kw in NLP_KEYWORDS):
        return "nlp", ("AI Skill" if "skill" in haystack else "NLP / AI"), False

    if any(kw in haystack for kw in HARDWARE_KEYWORDS) or ("fnos" in haystack and "media-library" not in haystack):
        if "esp32" in haystack:
            badge = "ESP32"
        elif "armbian" in haystack or "linux" in haystack:
            badge = "Embedded Linux"
        else:
            badge = "Hardware"
        return "hardware", badge, False

    if "android-tv" in haystack:
        badge = "Android TV"
    elif repo["language"] in ("C#", "C++") or "windows" in haystack or "gui" in haystack or "headphones" in haystack:
        badge = "Desktop"
    else:
        badge = "Media"
    return "media", badge, False


def fetch_all_repos(token: str) -> list[dict]:
    all_raw: list[dict] = []
    page = 1
    while True:
        url = f"https://api.github.com/users/{OWNER}/repos?per_page=100&type=owner&sort=created&direction=desc&page={page}"
        req = urllib.request.Request(url, headers=_github_headers(token or None))
        with urllib.request.urlopen(req, timeout=20) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected REST API response format: {type(batch)}")
        all_raw.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    snapshots: list[dict] = []
    for raw in all_raw:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or raw.get("private") or name.lower() == SITE_REPO.lower():
            continue

        has_pages = bool(raw.get("has_pages", False))
        homepage = str(raw.get("homepage") or "").strip()
        if not homepage and has_pages:
            homepage = f"https://arrow36.github.io/{name}/"

        repo = {
            "name": name,
            "html_url": str(raw.get("html_url") or f"https://github.com/{OWNER}/{name}").strip(),
            "description": str(raw.get("description") or "").strip(),
            "homepage": homepage,
            "has_pages": has_pages,
            "language": str(raw.get("language") or "Open Source").strip(),
            "topics": [str(t).strip() for t in (raw.get("topics") or []) if str(t).strip()],
            "created_at": str(raw.get("created_at") or "").strip(),
        }
        category, cat_badge, is_live = classify_repo(repo)
        repo["category"] = category
        repo["cat_badge"] = cat_badge
        repo["is_live"] = is_live
        snapshots.append(repo)

    snapshots.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    if len(snapshots) < 20:
        raise RuntimeError(f"Fetched too few repositories ({len(snapshots)}); aborting to protect existing site.")
    return snapshots


def _render_topics_inner(repo: dict) -> str:
    topics = repo.get("topics") or [repo["language"].lower().replace(" ", "-") or "open-source"]
    return "".join(f'<span class="topic">{html_lib.escape(t)}</span>' for t in topics[:6])


def _render_actions_inner(repo: dict) -> str:
    html_url = html_lib.escape(repo["html_url"], quote=True)
    homepage = html_lib.escape(repo["homepage"], quote=True)
    if repo["category"] == "web" and homepage:
        return (
            f'            <a class="btn-primary" href="{homepage}" target="_blank" rel="noopener noreferrer">🚀 在线体验 / Launch</a>\n'
            f'            <a class="btn-secondary" href="{html_url}" target="_blank" rel="noopener noreferrer">GitHub 源码</a>'
        )
    return f'            <a class="btn-secondary" href="{html_url}" target="_blank" rel="noopener noreferrer">GitHub 源码 ↗</a>'


def build_card_inner(repo: dict) -> str:
    name_esc = html_lib.escape(repo["name"])
    html_url_esc = html_lib.escape(repo["html_url"], quote=True)
    primary_url = html_lib.escape(repo["homepage"], quote=True) if (repo["is_live"] and repo["homepage"]) else html_url_esc
    desc_esc = html_lib.escape(repo["description"] or f"Open-source project by {OWNER}.")
    cat_badge_cls = "cat-badge is-live" if repo["is_live"] else "cat-badge"
    cat_badge_esc = html_lib.escape(repo["cat_badge"])
    lang_badge_esc = html_lib.escape(repo["language"])
    topics_html = _render_topics_inner(repo)
    actions_html = _render_actions_inner(repo)

    return (
        f"\n"
        f"          <div>\n"
        f'            <div class="card-top">\n'
        f'              <h3 class="card-title"><a href="{primary_url}" target="_blank" rel="noopener noreferrer">{name_esc}</a></h3>\n'
        f'              <div class="badge-row">\n'
        f'                <span class="{cat_badge_cls}">{cat_badge_esc}</span>\n'
        f'                <span class="lang-badge">{lang_badge_esc}</span>\n'
        f"              </div>\n"
        f"            </div>\n"
        f'            <p class="card-desc">{desc_esc}</p>\n'
        f"          </div>\n"
        f'          <div class="topics">\n'
        f"            {topics_html}\n"
        f"          </div>\n"
        f'          <div class="card-actions">\n'
        f"{actions_html}\n"
        f"          </div>\n"
        f"        "
    )


def _sync_existing_card_body(inner_html: str, repo: dict, prev_repo: dict | None) -> str:
    updated = inner_html
    if prev_repo is not None:
        if prev_repo.get("description") != repo["description"] and repo["description"]:
            desc_esc = html_lib.escape(repo["description"])
            updated = re.sub(
                r'(<p class="card-desc">).*?(</p>)',
                lambda m: m.group(1) + desc_esc + m.group(2),
                updated,
                count=1,
                flags=re.S,
            )
        if prev_repo.get("topics") != repo["topics"] and repo["topics"]:
            topics_html = _render_topics_inner(repo)
            updated = re.sub(
                r'(<div class="topics">\s*).*?(\s*</div>)',
                lambda m: m.group(1) + topics_html + m.group(2),
                updated,
                count=1,
                flags=re.S,
            )
        if prev_repo.get("homepage") != repo["homepage"] or prev_repo.get("has_pages") != repo["has_pages"]:
            actions_html = _render_actions_inner(repo)
            updated = re.sub(
                r'(<div class="card-actions">\n).*?(\n[ \t]*</div>)',
                lambda m: m.group(1) + actions_html + m.group(2),
                updated,
                count=1,
                flags=re.S,
            )
            primary_url = html_lib.escape(
                repo["homepage"] if (repo["is_live"] and repo["homepage"]) else repo["html_url"], quote=True
            )
            updated = re.sub(
                r'(<h3 class="card-title"><a href=")[^"]+(")',
                lambda m: m.group(1) + primary_url + m.group(2),
                updated,
                count=1,
            )
    return updated


def update_index_html(
    pinned_names: list[str],
    repo_snapshots: list[dict],
    prev_repos_by_name: dict[str, dict],
) -> bool:
    original_text = INDEX_HTML_PATH.read_text(encoding="utf-8")
    normalized = original_text.replace("\r\n", "\n")

    grid_match = GRID_RE.search(normalized)
    if not grid_match:
        raise RuntimeError('Could not find <div class="grid" id="projectGrid"> in index.html')

    grid_open, grid_inner, grid_close = grid_match.groups()
    article_matches = ARTICLE_RE.findall(grid_inner)
    if len(article_matches) < 20:
        raise RuntimeError(f"Expected >= 20 project cards in #projectGrid, found {len(article_matches)}")

    repos_by_name = {r["name"]: r for r in repo_snapshots}
    pinned_rank = {name: i for i, name in enumerate(pinned_names)}
    existing_cards: dict[str, dict] = {}

    for idx, (attrs_str, inner_html) in enumerate(article_matches):
        title_match = re.search(r'<h3 class="card-title"><a[^>]*>([^<]+)</a></h3>', inner_html)
        if not title_match:
            raise RuntimeError(f"Missing card title link on card index {idx}")
        repo_name = html_lib.unescape(title_match.group(1)).strip()

        # Drop deleted/private repositories no longer in public GitHub list
        if repo_name not in repos_by_name:
            continue

        cat_match = re.search(r'data-category="([^"]+)"', attrs_str)
        if not cat_match:
            raise RuntimeError(f"Missing data-category on card {repo_name}")
        category = cat_match.group(1)

        repo = repos_by_name[repo_name]
        prev_repo = prev_repos_by_name.get(repo_name)
        if prev_repo is not None and (
            prev_repo.get("homepage") != repo["homepage"] or prev_repo.get("has_pages") != repo["has_pages"]
        ):
            if repo["is_live"]:
                category = "web"

        order_match = re.search(r'data-default-order="(\d+)"', attrs_str)
        default_order = int(order_match.group(1)) if order_match else idx

        cleaned_inner = re.sub(
            r'\n?[ \t]*<span class="pin-badge"[^>]*>.*?</span>',
            "",
            inner_html,
            flags=re.S,
        )
        cleaned_inner = _sync_existing_card_body(cleaned_inner, repo, prev_repo)

        existing_cards[repo_name] = {
            "name": repo_name,
            "category": category,
            "old_order": default_order,
            "is_new": False,
            "created_at": repo.get("created_at", ""),
            "inner_html": cleaned_inner,
        }

    # Add newly created repositories not yet present in index.html
    for repo in repo_snapshots:
        name = repo["name"]
        if name in existing_cards:
            continue
        existing_cards[name] = {
            "name": name,
            "category": repo["category"],
            "old_order": -1,
            "is_new": True,
            "created_at": repo.get("created_at", ""),
            "inner_html": build_card_inner(repo),
        }

    # Compute canonical default-order slots: grouped by category, newest additions first within category,
    # then older cards preserving their existing relative order.
    ordered_for_default = sorted(
        existing_cards.values(),
        key=lambda c: (
            CATEGORY_ORDER.get(c["category"], 9),
            0 if c["is_new"] else 1,
            -int(datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()) if (c["is_new"] and c["created_at"]) else 0,
            c["old_order"],
            c["name"].lower(),
        ),
    )
    for new_idx, item in enumerate(ordered_for_default):
        item["default_order"] = new_idx

    # Final DOM order: pinned repos first (exact GitHub pin order), then remaining by default_order
    rendered_items = sorted(
        ordered_for_default,
        key=lambda c: (
            0 if c["name"] in pinned_rank else 1,
            pinned_rank.get(c["name"], c["default_order"]),
        ),
    )

    rendered_blocks: list[str] = []
    cat_counts = {"web": 0, "media": 0, "hardware": 0, "nlp": 0}
    live_count = 0

    for item in rendered_items:
        repo_name = item["name"]
        category = item["category"]
        cat_counts[category] = cat_counts.get(category, 0) + 1
        repo_meta = repos_by_name[repo_name]
        if repo_meta["is_live"] or category == "web":
            live_count += 1

        is_pinned = repo_name in pinned_rank
        body = item["inner_html"]
        if is_pinned:
            body, count = re.subn(
                r'(<div class="badge-row">)(\n[ \t]*)',
                r"\1\2" + PIN_BADGE_HTML + r"\2",
                body,
                count=1,
            )
            if count != 1:
                raise RuntimeError(f'Missing <div class="badge-row"> in card {repo_name}')

        card_class = "card is-pinned" if is_pinned else "card"
        pinned_attr = ' data-pinned="true"' if is_pinned else ""
        open_tag = (
            f'<article class="{card_class}" data-category="{category}" '
            f'data-repo="{html_lib.escape(repo_name, quote=True)}" '
            f'data-default-order="{item["default_order"]}"{pinned_attr}>'
        )
        rendered_blocks.append(f"        {open_tag}{body}</article>")

    total_count = len(rendered_items)
    new_grid_block = f"{grid_open}\n" + "\n\n".join(rendered_blocks) + f"\n{grid_close}"
    updated = normalized[: grid_match.start()] + new_grid_block + normalized[grid_match.end() :]

    # Synchronize all counts across meta tags, topbar, stats bar, filter pills, and empty state
    updated = re.sub(r"Explore \d+\+ open-source tools", f"Explore {total_count}+ open-source tools", updated, count=1)
    updated = re.sub(
        r'(<meta property="og:description" content=")\d+( open-source tools)',
        rf"\g<1>{total_count}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(r"GitHub \(\d+ Repos\) ↗", f"GitHub ({total_count} Repos) ↗", updated, count=1)
    updated = re.sub(
        r"<strong>\d+</strong>\s*开源项目 / Repositories",
        f"<strong>{total_count}</strong> 开源项目 / Repositories",
        updated,
        count=1,
    )
    updated = re.sub(
        r"<strong>\d+</strong>\s*在线即用 Web 应用 / Live Pages",
        f"<strong>{live_count}</strong> 在线即用 Web 应用 / Live Pages",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(<div class="result-count" id="resultCount">).*?(</div>)',
        rf"\g<1>显示 {total_count} / {total_count} 个项目\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(data-filter="all">全部 \()\d+(\)<)',
        rf"\g<1>{total_count}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(<button[^>]*id="pinnedFilterPill"[^>]*>)(.*?)(</button>)',
        rf"\g<1>📌 置顶精选 ({len(pinned_names)})\g<3>",
        updated,
        count=1,
        flags=re.S,
    )
    updated = re.sub(
        r'(data-filter="web">🌐 在线 Web 工具 \()\d+(\)<)',
        rf"\g<1>{cat_counts.get('web', 0)}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(data-filter="media">🎬 影像与桌面工具 \()\d+(\)<)',
        rf"\g<1>{cat_counts.get('media', 0)}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(data-filter="hardware">🔧 硬件与 Linux 移植 \()\d+(\)<)',
        rf"\g<1>{cat_counts.get('hardware', 0)}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r'(data-filter="nlp">📚 语言与 AI 技能 \()\d+(\)<)',
        rf"\g<1>{cat_counts.get('nlp', 0)}\g<2>",
        updated,
        count=1,
    )
    updated = re.sub(
        r"切换回「全部 \(\d+\)」",
        f"切换回「全部 ({total_count})」",
        updated,
        count=1,
    )

    if updated != normalized:
        with INDEX_HTML_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(updated)
        return True
    return False


def update_sitemap_xml(repo_snapshots: list[dict]) -> bool:
    if not SITEMAP_XML_PATH.exists():
        return False

    original = SITEMAP_XML_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    existing_locs = set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", original))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    new_entries: list[str] = []
    for repo in repo_snapshots:
        hp = repo["homepage"]
        if not hp.startswith("https://arrow36.github.io/"):
            continue
        loc = hp if hp.endswith("/") else f"{hp}/"
        if loc in existing_locs:
            continue
        existing_locs.add(loc)
        new_entries.append(
            "  <url>\n"
            f"    <loc>{html_lib.escape(loc)}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            "    <changefreq>monthly</changefreq>\n"
            "    <priority>0.8</priority>\n"
            "  </url>"
        )

    if not new_entries:
        return False

    updated = original.replace("</urlset>", "\n".join(new_entries) + "\n</urlset>")
    if updated != original:
        with SITEMAP_XML_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(updated)
        return True
    return False


def update_readme_md(repo_snapshots: list[dict]) -> bool:
    if not README_MD_PATH.exists():
        return False

    original = README_MD_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    valid_names = {r["name"] for r in repo_snapshots}

    # Remove lines referencing deleted/private repos (never touching profile or ?tab=repositories links)
    filtered_lines: list[str] = []
    repo_link_re = re.compile(rf"https://github\.com/{OWNER}/([A-Za-z0-9_.-]+)")
    for line in original.splitlines():
        m = repo_link_re.search(line)
        if m and m.group(1) not in valid_names:
            continue
        filtered_lines.append(line)
    updated = "\n".join(filtered_lines) + ("\n" if original.endswith("\n") else "")

    section_headers = {
        "web": ("## 🌐 在线 Web 工具 / Live Web Apps (GitHub Pages)", "## 🎬"),
        "media": ("## 🎬 影像、音频与桌面应用 / Media, Audio & Desktop Apps", "## 🔧"),
        "hardware": ("## 🔧 嵌入式硬件、Linux 移植与驱动修复 / Hardware, Linux & Drivers", "## 📚"),
        "nlp": ("## 📚 语言工具与 AI 技能 / NLP & AI Agent Skills", None),
    }

    # Process chronological ascending so newest items end up prepended right below section intro or appended cleanly
    for repo in sorted(repo_snapshots, key=lambda r: r.get("created_at", "")):
        name = repo["name"]
        if f"[{name}](" in updated:
            continue

        cat = repo["category"]
        header, next_header = section_headers.get(cat, section_headers["media"])
        desc = " ".join((repo["description"] or f"Open-source project by {OWNER}.").split())

        if cat == "web" and repo["homepage"]:
            safe_desc = desc.replace("|", "\\|")
            entry = f"| [{name}]({repo['html_url']}) | [Launch ↗]({repo['homepage']}) | {safe_desc} |"
        else:
            entry = f"- **[{name}]({repo['html_url']})** — {desc}"

        if next_header and next_header in updated:
            idx = updated.index(next_header)
            before = updated[:idx].rstrip("\n")
            after = updated[idx:]
            updated = before + "\n" + entry + "\n\n" + after
        elif header in updated:
            updated = updated.rstrip("\n") + "\n" + entry + "\n"

    if updated != original:
        with README_MD_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(updated)
        return True
    return False


def main() -> int:
    force = "--force" in sys.argv[1:]
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    pinned_names = fetch_pinned_repos(token)
    repo_snapshots = fetch_all_repos(token)

    existing_pinned: list[str] | None = None
    existing_repos: list[dict] | None = None
    prev_repos_by_name: dict[str, dict] = {}

    if PINNED_JSON_PATH.exists():
        try:
            existing_data = json.loads(PINNED_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(existing_data, dict):
                if isinstance(existing_data.get("pinned"), list):
                    existing_pinned = existing_data["pinned"]
                if isinstance(existing_data.get("repos"), list):
                    existing_repos = existing_data["repos"]
                    prev_repos_by_name = {
                        r["name"]: r for r in existing_repos if isinstance(r, dict) and r.get("name")
                    }
        except Exception:
            existing_pinned = None
            existing_repos = None

    if existing_pinned == pinned_names and existing_repos == repo_snapshots and not force:
        print(f"Repositories ({len(repo_snapshots)}) and pinned items ({len(pinned_names)}) unchanged.")
        return 0

    html_changed = update_index_html(pinned_names, repo_snapshots, prev_repos_by_name)
    sitemap_changed = update_sitemap_xml(repo_snapshots)
    readme_changed = update_readme_md(repo_snapshots)

    payload = {
        "owner": OWNER,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pinned": pinned_names,
        "repos": repo_snapshots,
    }
    with PINNED_JSON_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(
        f"Synced {len(repo_snapshots)} repos & {len(pinned_names)} pinned "
        f"(index={html_changed}, sitemap={sitemap_changed}, readme={readme_changed})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
