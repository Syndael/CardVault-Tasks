"""
Caption tag resolver for CardVault publishers.

Resolves dynamic tags in publication captions:
  <TITULO>      → publication title
  <URL_IG>      → Instagram permalink
  <URL_TIKTOK>  → TikTok permalink

If a tag is present but cannot be resolved, it is returned in the
unresolved list so the caller can decide how to handle it.
"""

import re

TAG_PATTERN = re.compile(r"<([A-Z_]+)>")

TAG_MAP = {
    "TITULO": "title",
    "URL_IG": "instagram",
    "URL_TIKTOK": "tiktok",
}


def resolve_caption_tags(caption, publication):
    if not caption:
        return caption, []

    unresolved = []

    def replacer(match):
        tag = match.group(1)
        if tag not in TAG_MAP:
            return match.group(0)

        kind = TAG_MAP[tag]

        if kind == "title":
            value = (publication.get("title") or "").strip()
            if not value:
                unresolved.append(tag)
                return match.group(0)
            return value

        platform = kind
        details = publication.get("details") or []
        for d in details:
            if d.get("platform") == platform:
                permalink = (d.get("permalink") or "").strip()
                if not permalink:
                    unresolved.append(tag)
                    return match.group(0)
                return permalink

        unresolved.append(tag)
        return match.group(0)

    resolved = TAG_PATTERN.sub(replacer, caption)
    return resolved, unresolved
