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


def _build_default_title_from_inventories(publication):
    inventories = publication.get("inventories") or []
    if not inventories:
        return None

    inv = inventories[0]
    product = inv.get("product") or {}
    collection = inv.get("collection") or {}

    collection_code = collection.get("code") or ""
    product_number = product.get("product_number") or ""

    translations = product.get("translations") or []
    sorted_translations = sorted(
        translations,
        key=lambda t: (t.get("language") or {}).get("priority_order", 999) or 999
    )
    primary_name = sorted_translations[0].get("name") if sorted_translations else None

    jp_name = None
    for t in sorted_translations:
        lang = t.get("language") or {}
        if lang.get("abbreviation") == "JP" or lang.get("name", "").lower() == "japanese":
            jp_name = t.get("name")
            break

    prefix_parts = []
    if collection_code:
        prefix_parts.append(collection_code)
    if product_number:
        prefix_parts.append(product_number)
    prefix = f"({' - '.join(prefix_parts)})" if prefix_parts else ""

    name_part = primary_name or ""
    jp_part = f" ({jp_name})" if jp_name else ""

    title = f"{prefix} {name_part}{jp_part}".strip()
    return title if title else None


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
                value = _build_default_title_from_inventories(publication)
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
