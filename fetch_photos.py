#!/usr/bin/env python3
"""
fetch_photos.py <slide_data.json>

Downloads each person's photo_url (leadership + technology_team) to
./photos/<slug>.jpg and adds a "photo_path" field pointing at it, in place,
back into slide_data.json. Run this on your pipeline machine (has real
internet) between extract_slide_data.py and build_ppt.js — this sandbox's
network is locked to package registries, so it can't fetch these itself.

If a download fails (dead link, timeout, non-image response), photo_path is
just left unset — the renderer falls back to an initials avatar for that
person. Never blocks the rest of the deck on one bad photo URL.
"""
import sys, os, re, json
import requests  # pip install requests --break-system-packages

OUT_DIR = "photos"


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "person").lower()).strip("-") or "person"


def download(url, dest):
    try:
        r = requests.get(url, timeout=10, verify=False,  # corporate MITM proxy — see project notes
                          headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "image" not in ctype:
            return False
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f"  skip ({e})", file=sys.stderr)
        return False


def main():
    if len(sys.argv) != 2:
        print("usage: fetch_photos.py <slide_data.json>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    d = json.load(open(path))
    os.makedirs(OUT_DIR, exist_ok=True)

    for group in ("leadership", "technology_team"):
        for p in d.get(group, []):
            url = p.get("photo_url")
            if not url:
                continue
            dest = os.path.join(OUT_DIR, slug(p.get("name")) + ".jpg")
            print(f"fetching {p.get('name')} -> {dest}")
            if download(url, dest):
                p["photo_path"] = dest

    json.dump(d, open(path, "w"), indent=1)
    print(f"updated {path}")


if __name__ == "__main__":
    main()
