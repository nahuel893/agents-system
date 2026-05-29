import json
import time
import urllib.request
import urllib.error

# Config
API_TOKEN = "ol_api_pB0Gy2EHCP4w6uXWexL2M9ZSPqNGilacvN2kcJ"
BASE_URL = "https://servidor-net.tail65a83a.ts.net"

def make_request(path, data=None):
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    req_body = json.dumps(data).encode("utf-8") if data is not None else None
    
    max_retries = 6
    backoff = 3
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                res_body = res.read().decode("utf-8")
                return json.loads(res_body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited (429) on {path}. Retrying in {backoff} seconds (Attempt {attempt+1}/{max_retries})...")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"HTTP Error {e.code} for {path}: {e.read().decode('utf-8')}")
            raise e
        except Exception as e:
            print(f"Connection Error for {path}: {e}")
            raise e
            
    raise Exception(f"Failed after {max_retries} retries due to rate limiting on {path}")

def delete_document(doc_id, title):
    print(f"Deleting duplicate document: '{title}' (ID: {doc_id})...")
    res = make_request("/api/documents.delete", {"id": doc_id})
    print(f"Deleted doc '{title}' status: {res.get('ok')}")
    time.sleep(1.0)  # Pace deletions to avoid 429s

def main():
    print("--- Listing Collections ---")
    collections_res = make_request("/api/collections.list", {})
    collections = collections_res.get("data", [])
    
    col_id = None
    for col in collections:
        if col['name'] == "Agent Platform":
            col_id = col['id']
            break
            
    if not col_id:
        print("Collection 'Agent Platform' not found!")
        return

    print(f"Listing all documents in collection: {col_id}")
    
    # Paginate through all documents to get the full list
    all_docs = []
    limit = 100
    offset = 0
    while True:
        res = make_request("/api/documents.list", {
            "collectionId": col_id,
            "limit": limit,
            "offset": offset
        })
        docs = res.get("data", [])
        if not docs:
            break
        all_docs.extend(docs)
        if len(docs) < limit:
            break
        offset += limit
        time.sleep(0.5)

    print(f"Total documents found in collection: {len(all_docs)}")

    # Group documents by (title, parentDocumentId)
    groups = {}
    for doc in all_docs:
        key = (doc['title'], doc.get('parentDocumentId'))
        if key not in groups:
            groups[key] = []
        groups[key].append(doc)

    # Find duplicates and delete older ones
    duplicates_to_delete = []
    for key, docs_in_group in groups.items():
        if len(docs_in_group) > 1:
            title, parent = key
            # Sort by createdAt descending (newest first)
            docs_in_group.sort(key=lambda d: d.get('createdAt', ''), reverse=True)
            
            # Keep the newest one (index 0)
            kept_doc = docs_in_group[0]
            print(f"Group '{title}' (parent: {parent}) has {len(docs_in_group)} copies. Keeping newest: ID {kept_doc['id']} created at {kept_doc['createdAt']}")
            
            # Mark others for deletion
            for dup in docs_in_group[1:]:
                duplicates_to_delete.append(dup)

    print(f"\nFound {len(duplicates_to_delete)} duplicate documents to delete.")
    
    # Execute deletions
    deleted_count = 0
    for dup in duplicates_to_delete:
        try:
            delete_document(dup['id'], dup['title'])
            deleted_count += 1
        except Exception as e:
            print(f"Failed to delete document {dup['id']} ({dup['title']}): {e}")
            
    print(f"\nCleanup complete. Successfully deleted {deleted_count}/{len(duplicates_to_delete)} duplicate documents.")

if __name__ == "__main__":
    main()
