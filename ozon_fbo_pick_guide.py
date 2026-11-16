"""Read-only optional Ozon FBO picking advice.

Dimensions: Google Sheets columns A:D only. Locations: GRIT warehouse map only.
Unknown or conflicting dimensions are never guessed and are shown at the end.
No picking event, quantity, scanner, employee state or DB mutation occurs here.
"""


def create_picking_guide(items, catalog, assignments):
    locations = {}
    ambiguous = set()
    for article, label in assignments:
        key = str(article or "").strip().casefold()
        label = str(label or "").strip()
        if not key or not label:
            continue
        if key in locations and locations[key] != label:
            ambiguous.add(key)
        else:
            locations[key] = label

    result = {}
    for item in items:
        article = str(item.get("article") or "").strip()
        key = article.casefold()
        record = catalog.get("items", {}).get(key)
        dims = record.get("dimensions_cm") if record else None
        volume = (dims[0] * dims[1] * dims[2]) if dims else None
        if key in ambiguous:
            location, location_state = None, "multiple"
        elif key not in locations:
            location, location_state = None, "missing"
        else:
            location, location_state = locations[key], "found"
        result[item["id"]] = {
            "location": location,
            "location_state": location_state,
            "dimensions_cm": dims,
            "volume_cm3": volume,
            "dimensions_state": (
                "found" if dims else (
                    "conflict" if key in catalog.get("conflicts", {}) else "missing"
                )
            ),
        }
    return result


def order_picking_items(items, guide):
    """Incomplete items first, larger external boxes first, then by article.

    This is a visual recommendation only; scans are accepted independently.
    """
    return sorted(items, key=lambda item: (
        item.get("remaining", 0) <= 0,
        guide.get(item["id"], {}).get("volume_cm3") is None,
        -(guide.get(item["id"], {}).get("volume_cm3") or 0),
        str(item.get("article") or "").casefold(),
        str(item["id"]),
    ))
