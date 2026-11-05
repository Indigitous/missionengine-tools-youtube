"""
Link Airtable rows to Supabase rows by matching the "url" column.

For every Airtable record, find the Supabase row with the same url
and write that row's id into an Airtable field (the foreign key).

Install:  pip install requests
Run:      python sync_urls.py
"""

import time
import requests

# ---------- SETTINGS (fill these in) ----------

SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_KEY = "YOUR-SUPABASE-SERVICE-OR-ANON-KEY"
SUPABASE_TABLE = "urls"          # table name in Supabase
SUPABASE_ID_COLUMN = "id"        # primary key column in Supabase
SUPABASE_URL_COLUMN = "url"      # url column in Supabase

AIRTABLE_TOKEN = "YOUR-AIRTABLE-PERSONAL-ACCESS-TOKEN"
AIRTABLE_BASE_ID = "appXXXXXXXXXXXXXX"
AIRTABLE_TABLE = "YOUR-TABLE-NAME"   # table name or tblXXXX id
AIRTABLE_URL_FIELD = "url"           # url field in Airtable
AIRTABLE_FK_FIELD = "supabase_id"    # field where the Supabase id will go

ONLY_FILL_EMPTY = True   # True = skip rows that already have a supabase_id
DRY_RUN = False          # True = print what would change, but don't update

# ----------------------------------------------


def clean_url(url):
    """
    Make urls comparable so these all match each other:
      http://site.com/page
      https://site.com/page/
      https://www.site.com/page
      www.site.com/page
      site.com/page
    """
    if not url:
        return ""

    url = str(url).strip().lower()

    # Remove http:// or https://
    if url.startswith("https://"):
        url = url[len("https://"):]
    elif url.startswith("http://"):
        url = url[len("http://"):]

    # Remove www.
    if url.startswith("www."):
        url = url[len("www."):]

    # Remove trailing slash
    url = url.rstrip("/")

    return url


def get_supabase_urls():
    """Return a dict of {clean_url: supabase_id} for every row in Supabase."""
    endpoint = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

    lookup = {}
    page_size = 1000
    start = 0

    while True:
        params = {
            "select": f"{SUPABASE_ID_COLUMN},{SUPABASE_URL_COLUMN}",
            "offset": start,
            "limit": page_size,
        }
        response = requests.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        rows = response.json()

        for row in rows:
            key = clean_url(row.get(SUPABASE_URL_COLUMN))
            if key:
                lookup[key] = row[SUPABASE_ID_COLUMN]

        if len(rows) < page_size:
            break
        start += page_size

    print(f"Loaded {len(lookup)} urls from Supabase")
    return lookup


def get_airtable_records():
    """Return a list of all records in the Airtable table."""
    endpoint = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

    records = []
    offset = None

    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset

        response = requests.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.2)  # stay under Airtable's 5 requests/second limit

    print(f"Loaded {len(records)} records from Airtable")
    return records


def update_airtable(updates):
    """Send updates to Airtable, 10 records at a time (Airtable's limit)."""
    endpoint = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }

    for i in range(0, len(updates), 10):
        batch = updates[i:i + 10]
        response = requests.patch(
            endpoint,
            headers=headers,
            json={"records": batch, "typecast": True},
        )
        if response.status_code != 200:
            print("Error updating batch:", response.text)
        else:
            print(f"Updated records {i + 1} to {i + len(batch)}")
        time.sleep(0.2)


def main():
    supabase_lookup = get_supabase_urls()
    airtable_records = get_airtable_records()

    updates = []
    not_found = []

    for record in airtable_records:
        fields = record.get("fields", {})
        url = clean_url(fields.get(AIRTABLE_URL_FIELD))

        if not url:
            continue
        if ONLY_FILL_EMPTY and fields.get(AIRTABLE_FK_FIELD):
            continue

        supabase_id = supabase_lookup.get(url)
        if supabase_id is None:
            not_found.append(fields.get(AIRTABLE_URL_FIELD))
            continue

        # Skip if it's already set to the right value
        if str(fields.get(AIRTABLE_FK_FIELD)) == str(supabase_id):
            continue

        updates.append({
            "id": record["id"],
            "fields": {AIRTABLE_FK_FIELD: supabase_id},
        })

    print(f"\n{len(updates)} records to update")
    print(f"{len(not_found)} Airtable urls had no match in Supabase")

    if DRY_RUN:
        for u in updates[:20]:
            print("Would update:", u)
        print("\nDRY_RUN is on, nothing was changed.")
    else:
        update_airtable(updates)

    if not_found:
        print("\nUnmatched urls (first 20):")
        for url in not_found[:20]:
            print("  ", url)

    print("\nDone.")


if __name__ == "__main__":
    main()