import os
import sys

import requests

GROUP_ID = os.environ["GROUP_ID"]
ZOTERO_API_KEY = os.environ["ZOTERO_API_KEY"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK"]
COLLECTION_KEY = (
    os.getenv("COLLECTION_KEY")
    or os.getenv("SUBCOLLECTION_KEY")
    or os.getenv("COLLECTION_ID")
    or ""
).strip()
INCLUDE_SUBCOLLECTIONS = os.getenv("INCLUDE_SUBCOLLECTIONS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

LAST_ITEM_FILE = "last_item.txt"
headers = {"Zotero-API-Key": ZOTERO_API_KEY} 


def print_zotero_403_help():
    print("Zotero API returned 403 Forbidden.")
    print("This usually means the API key cannot access this group library.")
    print("Checklist:")
    print("- GROUP_ID is correct for the target Zotero group.")
    print("- ZOTERO_API_KEY is valid and active.")
    print("- The key has 'Read access to groups' enabled.")
    print("- The key has 'Read access to items' enabled.")
    print("- The key owner has membership/access to that Zotero group.")


def _handle_zotero_error(response):
    print(f"Zotero API request failed: {response.status_code}")
    if response.text:
        print(response.text[:500])
    if response.status_code == 403:
        print_zotero_403_help()


def zotero_get(url, params=None, timeout=30, required=True):
    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    if response.ok:
        return response

    if required:
        _handle_zotero_error(response)
        raise requests.HTTPError(response=response)

    print(f"Warning: Zotero request failed ({response.status_code}) for {url}")
    if response.text:
        print(response.text[:200])
    return None


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
    url = f"https://api.zotero.org/groups/{GROUP_ID}/items/{item_key}/children"
    r = requests.get(url, headers=headers, timeout=30)
    if not r.ok:
        return False

    children = r.json()
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") == "attachment":
            if data.get("contentType") == "application/pdf":
                return True
    return False


def get_creator_name(meta):
    created_by = meta.get("createdByUser") or {}

    # Zotero may return different fields depending on API permissions.
    if created_by.get("name"):
        return created_by["name"]
    if created_by.get("username"):
        return created_by["username"]
    if created_by.get("id"):
        return f"User ID {created_by['id']}"

    return "Not available (hidden or missing in API response)"


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

        url = f"https://api.zotero.org/groups/{GROUP_ID}/collections/{current}/collections"
        response = zotero_get(
            url,
            params={"limit": 100, "format": "json"},
            required=False,
        )
        if response is None:
            continue

        for collection in response.json():
            key = collection.get("key")
            if key and key not in seen:
                queue.append(key)

    return keys


def fetch_recent_items():
    params = {
        "sort": "dateAdded",
        "direction": "desc",
        "limit": 20,
        "include": "data",
    }

    if not COLLECTION_KEY:
        url = f"https://api.zotero.org/groups/{GROUP_ID}/items/top"
        response = zotero_get(url, params=params)
        return response.json()

    collection_keys = fetch_collection_keys(COLLECTION_KEY)
    if not collection_keys:
        print("No collection keys resolved. Check COLLECTION_KEY / SUBCOLLECTION_KEY.")
        return []

    all_items = []
    for collection_key in collection_keys:
        url = f"https://api.zotero.org/groups/{GROUP_ID}/collections/{collection_key}/items/top"
        response = zotero_get(url, params=params, required=False)
        if response is None:
            continue
        all_items.extend(response.json())

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

    if COLLECTION_KEY:
        mode = "including subcollections" if INCLUDE_SUBCOLLECTIONS else "without subcollections"
        print(f"Collection mode enabled for key '{COLLECTION_KEY}' ({mode}).")
    else:
        print("Group-wide mode enabled (all top-level items in the group library).")

    try:
        items = fetch_recent_items()
    except requests.HTTPError:
        raise SystemExit(1)

    if not items:
        print("No items found.")
        return

    new_items = []
    for item in items:
        if item["key"] == last_seen:
            break
        new_items.append(item)

    if not new_items:
        print("No new items.")
        return

    # Reverse so oldest new item posts first
    new_items.reverse()

    for item in new_items:
        item_key = item["key"]
        data = item["data"]
        meta = item.get("meta", {})

        title = data.get("title", "No title")
        abstract = data.get("abstractNote", "").strip()
        creators = data.get("creators", [])
        doi = data.get("DOI", "").strip()

        authors = format_authors(creators)

        creator_name = get_creator_name(meta)

        zotero_link = f"https://www.zotero.org/groups/{GROUP_ID}/items/{item_key}"

        pdf_status = "Yes" if has_pdf(item_key) else "No"

        if not abstract:
            abstract = "_No abstract available._"

        if doi:
            doi_link = f"https://doi.org/{doi}"
            doi_text = f"<{doi_link}|{doi}>"
        else:
            doi_text = "Not available"

        message = {
            "text": f"📚 *New Zotero item added*\n"
                    f"*Title:* {title}\n"
                    f"*Authors:* {authors}\n"
                    f"*Added by:* {creator_name}\n"
                    f"*DOI:* {doi_text}\n"
                    f"*PDF attached:* {pdf_status}\n\n"
                    f"*Abstract:*\n{abstract[:1500]}\n\n"
                    f"<{zotero_link}|Open in Zotero>"
        }

        slack_resp = requests.post(SLACK_WEBHOOK, json=message, timeout=15)
        if not slack_resp.ok:
            print(f"Slack webhook failed ({slack_resp.status_code}): {slack_resp.text[:300]}")
        print(f"Posted: {title}")

    # Save newest item key
    save_last(items[0]["key"])


if __name__ == "__main__":
    main()
