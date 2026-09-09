import os
import requests


def normalize_collection_target(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return "", ""

    group_id_from_url = ""
    if "/groups/" in value:
        group_part = value.split("/groups/", 1)[1]
        group_id_from_url = group_part.split("/", 1)[0].strip()

    # Accept either a raw key or a full Zotero URL containing /collections/<KEY>/
    if "/collections/" in value:
        value = value.split("/collections/", 1)[1]
        value = value.split("/", 1)[0]

    value = value.split("?", 1)[0].split("#", 1)[0].strip()
    return value, group_id_from_url


GROUP_ID = os.environ["GROUP_ID"]
COLLECTION_KEY_RAW = (
    os.getenv("COLLECTION_KEY")
    or os.getenv("SUBCOLLECTION_KEY")
    or os.getenv("COLLECTION_ID")
    or ""
).strip()
COLLECTION_KEY, GROUP_ID_FROM_COLLECTION_URL = normalize_collection_target(COLLECTION_KEY_RAW)
ACTIVE_GROUP_ID = GROUP_ID_FROM_COLLECTION_URL or GROUP_ID
ZOTERO_API_KEY = os.environ["ZOTERO_API_KEY"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK"]
INCLUDE_SUBCOLLECTIONS = os.getenv("INCLUDE_SUBCOLLECTIONS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

LAST_ITEM_FILE = "last_item.txt"

headers = {
    "Zotero-API-Key": ZOTERO_API_KEY
}


def get_last_saved():
    try:
        with open(LAST_ITEM_FILE, "r") as f:
            return f.read().strip()
    except OSError:
        return "none"


def save_last(key):
    with open(LAST_ITEM_FILE, "w") as f:
        f.write(key)


def format_authors(creators):
    authors = []

    for c in creators:
        if c.get("creatorType") == "author":
            first = c.get("firstName", "")
            last = c.get("lastName", "")
            authors.append(f"{first} {last}".strip())

    return ", ".join(authors) if authors else "Unknown authors"


def has_pdf(item_key):
    url = f"https://api.zotero.org/groups/{ACTIVE_GROUP_ID}/items/{item_key}/children"

    r = requests.get(
        url,
        headers=headers,
        timeout=30
    )

    if not r.ok:
        return False

    for child in r.json():
        data = child.get("data", {})

        if (
            data.get("itemType") == "attachment"
            and data.get("contentType") == "application/pdf"
        ):
            return True

    return False


def get_creator_name(meta):
    created_by = meta.get("createdByUser") or {}

    if created_by.get("name"):
        return created_by["name"]

    if created_by.get("username"):
        return created_by["username"]

    if created_by.get("id"):
        return f"User ID {created_by['id']}"

    return "Not available"


def fetch_collection_keys(root_collection_key):
    keys = []
    queue = [root_collection_key]
    seen = set()

    while queue:
        current = queue.pop(0)
        if current in seen:
            continue

        seen.add(current)
        keys.append(current)

        if not INCLUDE_SUBCOLLECTIONS:
            continue

        url = f"https://api.zotero.org/groups/{ACTIVE_GROUP_ID}/collections/{current}/collections"
        r = requests.get(url, headers=headers, params={"limit": 100, "format": "json"}, timeout=30)
        if not r.ok:
            if current == root_collection_key:
                raise RuntimeError(
                    f"Cannot access collection '{root_collection_key}' (HTTP {r.status_code}). "
                    "Check COLLECTION_KEY and Zotero API permissions."
                )
            print(f"Warning: failed to list child collections for {current} ({r.status_code}).")
            continue

        for collection in r.json():
            key = collection.get("key")
            if key and key not in seen:
                queue.append(key)

    return keys


def fetch_recent_items():
    params = {
        "sort": "dateAdded",
        "direction": "desc",
        "limit": 5,
        "include": "data",
    }

    if not COLLECTION_KEY:
        url = f"https://api.zotero.org/groups/{ACTIVE_GROUP_ID}/items/top"
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    collection_keys = fetch_collection_keys(COLLECTION_KEY)
    if not collection_keys:
        raise RuntimeError("No collection keys resolved from COLLECTION_KEY.")

    print(f"Resolved collections: {len(collection_keys)}")

    all_items = []
    for collection_key in collection_keys:
        url = f"https://api.zotero.org/groups/{ACTIVE_GROUP_ID}/collections/{collection_key}/items/top"
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if not r.ok:
            if collection_key == COLLECTION_KEY:
                raise RuntimeError(
                    f"Cannot read items for COLLECTION_KEY '{COLLECTION_KEY}' (HTTP {r.status_code}). "
                    "Check key value and permissions."
                )
            print(f"Warning: failed to read collection {collection_key} ({r.status_code}).")
            continue
        all_items.extend(r.json())

    deduped = {}
    for item in all_items:
        key = item.get("key")
        if key:
            deduped[key] = item

    items = list(deduped.values())
    items.sort(key=lambda item: item.get("data", {}).get("dateAdded", ""), reverse=True)
    return items


def main():

    last_seen = get_last_saved()
    print(f"last_item marker: {last_seen}")

    if COLLECTION_KEY_RAW and COLLECTION_KEY_RAW != COLLECTION_KEY:
        print("Normalized COLLECTION_KEY from URL/extended value.")

    if GROUP_ID_FROM_COLLECTION_URL:
        if GROUP_ID_FROM_COLLECTION_URL != GROUP_ID:
            print(
                "GROUP_ID mismatch detected. "
                f"Using group ID from COLLECTION_KEY URL: {GROUP_ID_FROM_COLLECTION_URL}"
            )
        else:
            print("GROUP_ID confirmed from COLLECTION_KEY URL.")

    if COLLECTION_KEY:
        mode = "including subcollections" if INCLUDE_SUBCOLLECTIONS else "without subcollections"
        print(f"Collection mode enabled for key '{COLLECTION_KEY}' ({mode}).")
    else:
        print("Group-wide mode enabled (all top-level items in the group library).")

    try:
        items = fetch_recent_items()
    except RuntimeError as err:
        print(str(err))
        raise SystemExit(1)

    print("Items found in query:", len(items))

    if not items:
        print("No items found.")
        return

    # Find items newer than the last one we processed.
    new_items = []

    for item in items:
        if item["key"] == last_seen:
            break

        new_items.append(item)

    if not new_items:
        print("No new items.")
        return

    print("New items to notify:", len(new_items))

    # Oldest first
    new_items.reverse()

    posted_count = 0
    failed_count = 0

    for item in new_items:

        item_key = item["key"]
        data = item["data"]
        meta = item.get("meta", {})

        title = data.get("title", "No title")

        abstract = data.get(
            "abstractNote",
            ""
        ).strip()

        creators = data.get("creators", [])

        doi = data.get(
            "DOI",
            ""
        ).strip()

        authors = format_authors(creators)

        creator_name = get_creator_name(meta)

        zotero_link = (
            f"https://www.zotero.org/groups/"
            f"{ACTIVE_GROUP_ID}/items/{item_key}"
        )

        pdf_status = "Yes" if has_pdf(item_key) else "No"

        if not abstract:
            abstract = "_No abstract available._"

        if doi:
            doi_link = f"https://doi.org/{doi}"
            doi_text = f"<{doi_link}|{doi}>"
        else:
            doi_text = "Not available"

        message = {
            "text":
                f"📚 *New Zotero item added*\n"
                f"*Title:* {title}\n"
                f"*Authors:* {authors}\n"
                f"*Added by:* {creator_name}\n"
                f"*DOI:* {doi_text}\n"
                f"*PDF attached:* {pdf_status}\n\n"
                f"*Abstract:*\n"
                f"{abstract[:1500]}\n\n"
                f"<{zotero_link}|Open in Zotero>"
        }

        slack_resp = requests.post(
            SLACK_WEBHOOK,
            json=message,
            timeout=15
        )

        if not slack_resp.ok:
            failed_count += 1
            print(
                f"Slack webhook failed "
                f"({slack_resp.status_code}): "
                f"{slack_resp.text[:300]}"
            )
            continue

        print(f"Posted: {title}")
        posted_count += 1

    print(f"Slack delivery summary: posted={posted_count}, failed={failed_count}")

    if failed_count > 0:
        print("At least one Slack delivery failed. Keeping last_item unchanged so items can be retried.")
        raise SystemExit(1)

    if posted_count == 0:
        print("No Slack messages were delivered. Keeping last_item unchanged.")
        raise SystemExit(1)

    # Save newest monitored item
    save_last(items[0]["key"])


if __name__ == "__main__":
    main()
