"""
Canvas course lister + DigitalOcean Spaces uploader.
No API token required — log in once via the browser.
"""

from playwright.sync_api import sync_playwright
from pathlib import Path
from urllib.parse import urlparse
import re
import boto3
import os
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
CANVAS_URL = "https://umsystem.instructure.com"
PROFILE_DIR = Path("./canvas_profile")
DOWNLOAD_DIR = Path("./downloads")
HEADLESS = False

# DigitalOcean Spaces (set in .env)
SPACES_KEY = os.getenv("SPACES_KEY")
SPACES_SECRET = os.getenv("SPACES_SECRET")
SPACES_REGION = os.getenv("SPACES_REGION", "sfo3")
SPACES_BUCKET = os.getenv("SPACES_BUCKET")
SPACES_ENDPOINT = f"https://{SPACES_REGION}.digitaloceanspaces.com"

PROFILE_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── Spaces client ───────────────────────────────────────────────────────────

def get_spaces_client():
    session = boto3.session.Session()
    return session.client(
        "s3",
        region_name=SPACES_REGION,
        endpoint_url=SPACES_ENDPOINT,
        aws_access_key_id=SPACES_KEY,
        aws_secret_access_key=SPACES_SECRET,
    )

def upload_to_spaces(local_path: Path, remote_key: str):
    """Upload a file to Spaces. Returns True on success."""
    try:
        client = get_spaces_client()
        client.upload_file(str(local_path), SPACES_BUCKET, remote_key)
        print(f"  Uploaded: {remote_key}")
        return True
    except Exception as e:
        print(f"  Upload failed for {remote_key}: {e}")
        return False

def upload_folder(local_folder: Path, remote_prefix: str, skip_existing=True):
    """Upload every file in a folder to Spaces under the given prefix.
    If skip_existing is True, skip files that are already in the bucket."""
    client = get_spaces_client()
    count = 0
    skipped = 0
    for path in local_folder.rglob("*"):
        if path.is_file():
            rel = path.relative_to(local_folder)
            key = f"{remote_prefix}/{rel.as_posix()}"
            if skip_existing:
                try:
                    client.head_object(Bucket=SPACES_BUCKET, Key=key)
                    skipped += 1
                    continue
                except Exception:
                    pass  # not found, upload it
            if upload_to_spaces(path, key):
                count += 1
    print(f"Uploaded {count} files, skipped {skipped} existing under {remote_prefix}/")
    return count

# ── Canvas helpers ──────────────────────────────────────────────────────────

def is_course_root(href: str) -> bool:
    if not href:
        return False
    clean = href.split("?")[0].rstrip("/")
    return bool(re.search(r"/courses/\d+$", clean))

def base_url(page) -> str:
    parsed = urlparse(page.url)
    return f"{parsed.scheme}://{parsed.netloc}"

def get_courses(page) -> dict:
    try:
        raw = page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('.ic-DashboardCard').forEach(card => {
                    const a = card.querySelector('a');
                    const title = card.querySelector('.ic-DashboardCard__header-title');
                    if (a) {
                        results.push({
                            href: a.getAttribute('href') || '',
                            name: (title ? title.textContent : a.textContent) || ''
                        });
                    }
                });
                if (results.length === 0) {
                    document.querySelectorAll('a[href*="/courses/"]').forEach(a => {
                        results.push({
                            href: a.getAttribute('href') || '',
                            name: a.textContent || ''
                        });
                    });
                }
                return results;
            }
        """)
    except Exception as e:
        return {}

    courses = {}
    base = base_url(page)
    for item in raw:
        href = item.get("href", "")
        name = " ".join(item.get("name", "").split())
        if href.startswith("/"):
            href = base + href
        if not is_course_root(href):
            continue
        if not name or name.lower() in ("courses", "all courses", "dashboard"):
            continue
        if href not in courses:
            courses[href] = name
    return courses

def safe_name(name: str) -> str:
    """Make a filesystem-safe folder name."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()[:80]


def download_course(page, course_url: str, course_name: str):
    """Download syllabus, files, and pages for one course into downloads/."""
    folder = DOWNLOAD_DIR / safe_name(course_name)
    folder.mkdir(parents=True, exist_ok=True)
    print(f"\n--- Downloading: {course_name} ---")

    # Syllabus (skip HTML — PDFs only)
    # Files tab — try new UI, then switch to old if needed
    try:
        page.goto(f"{course_url}/files", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        # If we see the new Files UI, switch to old
        switch_btn = page.locator("text=Switch to Old Files Page")
        if switch_btn.count() > 0:
            print("  Switching to old Files page...")
            switch_btn.first.click()
            page.wait_for_timeout(3000)

        snippet = page.evaluate("() => document.body.innerText.slice(0, 200)")
        print(f"  Snippet: {snippet[:150]}")

        file_links = page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (!href || !text) return;
                    if ((href.includes('/files/') || href.includes('/download')) && !href.includes('/folder') && !seen.has(href)) {
                        seen.add(href);
                        out.push({href, text});
                    }
                });
                return out;
            }
        """)
        # Also collect folder links
        folder_links = page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && href.includes('/folder/') && !seen.has(href)) {
                        seen.add(href);
                        out.push({href, text});
                    }
                });
                return out;
            }
        """)
        print(f"  Found {len(file_links)} root file links, {len(folder_links)} folders")

        # Enter each folder and grab PDFs inside
        for fl in folder_links:
            try:
                fhref = fl["href"]
                if fhref.startswith("/"):
                    fhref = base_url(page) + fhref
                print(f"  Entering folder: {fl['text']}")
                page.goto(fhref, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
                inner = page.evaluate("""
                    () => {
                        const out = [];
                        const seen = new Set();
                        document.querySelectorAll('a').forEach(a => {
                            const href = a.getAttribute('href') || '';
                            const text = (a.textContent || '').trim();
                            if (href && text && (href.includes('/files/') || href.includes('/download')) && !href.includes('/folder') && !seen.has(href)) {
                                seen.add(href);
                                out.push({href, text});
                            }
                        });
                        return out;
                    }
                """)
                file_links.extend(inner)
                print(f"    +{len(inner)} files inside")
            except Exception as e:
                print(f"    Folder skip: {e}")
                continue

    except Exception as e:
        print(f"  Files page error: {e}")
        file_links = []

    files_dir = folder / "files"
    files_dir.mkdir(exist_ok=True)
    saved = 0
    for item in file_links:
        try:
            href = item["href"]
            name = item["text"]
            lower = name.lower()
            if not lower.endswith(".pdf"):
                continue
            if href.startswith("/"):
                href = base_url(page) + href
            if "/files/" in href and "/download" not in href:
                href = href.rstrip("/") + "/download"
            safe = safe_name(name) or "file.pdf"
            dest = files_dir / safe
            if dest.exists():
                print(f"  Already have: {safe}")
                saved += 1
                continue
            # Start listening for download before navigating
            with page.expect_download(timeout=30000) as dl_info:
                try:
                    page.goto(href)
                except Exception as nav_err:
                    # Playwright raises when a download starts — that's expected
                    if "Download is starting" not in str(nav_err):
                        raise
            download = dl_info.value
            download.save_as(str(dest))
            print(f"  File: {safe}")
            saved += 1
        except Exception as e:
            print(f"  Skip {item.get('text','?')}: {e}")
            continue
    print(f"  Saved {saved} PDFs")

    # Upload the course folder to Spaces
    remote_prefix = f"courses/{safe_name(course_name)}"
    upload_folder(folder, remote_prefix)
    return folder


def try_extract(page, label=""):
    print(f"  Checking for courses{label}...")
    courses = get_courses(page)
    print(f"  Found {len(courses)} so far")
    return courses


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("Starting Playwright...")
    with sync_playwright() as p:
        print("Launching browser...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(10000)
        print("Browser ready.")

        print(f"Opening {CANVAS_URL}")
        page.goto(CANVAS_URL, wait_until="domcontentloaded")
        print("Page opened.")

        if not HEADLESS:
            print("\nLog in to Canvas in the browser window.")
            print("When the dashboard is visible, press Enter here...")
            input()
            print("Enter received, continuing...")

        print(f"Current URL: {page.url}")

        courses = try_extract(page, " (first check)")

        if not courses:
            print("Nothing yet — waiting 3s...")
            page.wait_for_timeout(3000)
            courses = try_extract(page, " (after wait)")

        if not courses:
            print("Still empty — reloading page...")
            try:
                page.reload(wait_until="domcontentloaded")
                print("Reload done.")
                page.wait_for_timeout(2000)
                print(f"URL after reload: {page.url}")
            except Exception as e:
                print(f"Reload error: {e}")
            courses = try_extract(page, " (after reload)")

        if not courses:
            print("Trying /courses page...")
            try:
                page.goto(f"{base_url(page)}/courses", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                print(f"URL: {page.url}")
            except Exception as e:
                print(f"Navigation error: {e}")
            courses = try_extract(page, " (/courses)")

        if courses:
            print(f"\n{len(courses)} courses found:\n")
            for url, name in courses.items():
                print(f"  {name}")
                print(f"    {url}\n")
            # Save course list locally for later upload / categorization
            list_path = DOWNLOAD_DIR / "courses.txt"
            with open(list_path, "w", encoding="utf-8") as f:
                for url, name in courses.items():
                    f.write(f"{name}\t{url}\n")
            print(f"Course list saved to {list_path}")
            upload_to_spaces(list_path, "courses.txt")

            # Download materials for each course
            print("\nStarting course downloads...")
            for url, name in courses.items():
                try:
                    download_course(page, url, name)
                except Exception as e:
                    print(f"Failed on {name}: {e}")
            print("\nAll courses processed.")
        else:
            print("\nNo courses found.")
            try:
                print("Page title:", page.title())
            except Exception:
                pass

        print("Session saved in ./canvas_profile")
        if not HEADLESS:
            input("Press Enter to close...")
        context.close()
        print("Done.")

if __name__ == "__main__":
    main()

