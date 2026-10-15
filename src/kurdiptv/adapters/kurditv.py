"""kurditv.com: WordPress site. Posts (channels) come from the public REST API; each post's player asks
``admin-ajax.php?action=vembed_data&post_id=N`` for its streams - the same call the site's own embed makes."""

from __future__ import annotations

import html
from urllib.parse import urljoin

from ..fetcher import FetchProblem
from ..models import RawCandidate
from ..normalize import fix_mojibake, fold_ascii, latin_name_in_mixed_title
from .base import SourceAdapter

PAGE_SIZE = 100
MAX_PAGES = 5


class KurdiTvAdapter(SourceAdapter):
    name = "kurditv"

    async def discover(self) -> None:
        base = self.source.url
        cats_page = await self.fetcher.get(
            urljoin(base, "/wp-json/wp/v2/categories?per_page=100&_fields=id,name,slug,count")
        )
        categories = {c["id"]: html.unescape(c.get("name", "")) for c in cats_page.json()}
        wanted_names = {fold_ascii(n) for n in self.opt("include_categories", [])}
        wanted_ids = {
            c["id"]
            for c in cats_page.json()
            if fold_ascii(html.unescape(c.get("name", ""))) in wanted_names or fold_ascii(c.get("slug", "")) in wanted_names
        }
        if wanted_names and not wanted_ids:
            raise ValueError(f"none of the configured categories exist on the site: {sorted(wanted_names)}")

        posts: list[dict] = []
        for page_no in range(1, MAX_PAGES + 1):
            url = urljoin(base, f"/wp-json/wp/v2/posts?per_page={PAGE_SIZE}&page={page_no}&_fields=id,link,slug,title,categories")
            try:
                page = await self.fetcher.get(url)
            except FetchProblem as exc:
                if exc.status == 400 and page_no > 1:  # WordPress: page number beyond the last page
                    break
                raise
            batch = page.json()
            posts.extend(batch)
            if len(batch) < PAGE_SIZE:
                break

        out_of_scope = 0
        for post in posts:
            cat_ids = set(post.get("categories") or [])
            if wanted_ids and not (cat_ids & wanted_ids):
                out_of_scope += 1
                continue
            await self._read_post(base, post, [categories.get(c, "") for c in cat_ids])
        if out_of_scope:
            self.notes.append(f"{out_of_scope} posts outside the configured categories ignored (Arabic/Turkish/German TV)")
            self.skipped["other-category"] = out_of_scope

    async def _read_post(self, base: str, post: dict, category_names: list[str]) -> None:
        post_id = post.get("id")
        title = fix_mojibake(html.unescape((post.get("title") or {}).get("rendered", ""))).strip()
        if not post_id or not title:
            self.skip("post-without-title")
            return
        # The site's own embed page calls this endpoint with itself as Referer, and the endpoint refuses requests
        # without one (anti-hotlinking). We send the same Referer the player does; nothing else is imitated.
        page = await self.fetcher.get_optional(
            urljoin(base, f"/wp-admin/admin-ajax.php?action=vembed_data&post_id={post_id}"),
            headers={"Referer": urljoin(base, f"/embed/{post_id}/")},
        )
        if page is None:
            return
        try:
            data = page.json()
        except ValueError:
            self.skip("player-data-not-json")
            return
        sources = ((data or {}).get("data") or {}).get("sources") or []
        if not sources:
            self.skip("post-without-streams")
            return
        latin = latin_name_in_mixed_title(title)
        name = latin or title
        aliases = (title,) if latin else ()
        slug_name = (post.get("slug") or "").replace("-", " ")
        if slug_name and not slug_name.isdigit():
            aliases += (slug_name,)
        for src in sources:
            url = (src or {}).get("src", "")
            if not url or ".m3u8" not in url.lower():
                self.skip("not-hls")
                continue
            self.add(
                RawCandidate(
                    source_id=self.source.id,
                    url=url.replace("\\/", "/"),
                    name=name,
                    aliases=aliases,
                    tags=tuple(c for c in category_names if c),
                    page_url=post.get("link"),
                    name_confidence="medium",
                    note=(src or {}).get("name", ""),
                )
            )
