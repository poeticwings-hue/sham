import re
import sqlite3
import configparser
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from bs4 import BeautifulSoup, NavigableString
import requests

app = Flask(__name__)

# ── Tunables ──
PAGE_TOLERANCE = 2

# ── Paths ──
BASE_DIR = Path(__file__).resolve().parent

# ── Config ──
config = configparser.ConfigParser()
config.read(BASE_DIR / 'config.ini', encoding='utf-8')
BOOKS_DIR = (BASE_DIR / config.get('settings', 'books_dir', fallback='./books')).resolve()
GEMINI_KEY = config.get('settings', 'gemini_api_key', fallback='')
GEMINI_MODEL = config.get('settings', 'gemini_model', fallback='gemini-1.5-flash')
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

DB_PATH = (BASE_DIR / 'library.db').resolve()
HTML_SUFFIXES = ('.html', '.htm')


# ═══════════════════════════════════════════════════════════════
#  ARABIC NORMALIZATION (SEARCH ONLY)
# ═══════════════════════════════════════════════════════════════

def normalize_arabic(text):
    text = str(text)
    text = re.sub(r'[\u064B-\u065F\u0670\u0640]', '', text)
    text = re.sub(r'[\u0622\u0623\u0625]', '\u0627', text)
    text = text.replace('\u0649', '\u064A')
    text = re.sub(r'[\u200B-\u200F\u202A-\u202E\uFEFF]', '', text)
    text = re.sub(r'[:\.،؛؟!()\[\]«»]', '', text)
    return text.strip()

def normalize_part(part):
    """Normalize part name for comparison (remove Arabic prefixes, leading zeros)."""
    if not part:
        return None
    # Remove Arabic prefixes (e.g., "الجزء", "ج")
    part = re.sub(r'[\u0627\u0644\u062c\u0632\u0621\u062c]', '', str(part))
    # Remove leading zeros
    part = part.lstrip('0')
    return part or '0'  # Handle empty string

# ═══════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            slide_count INTEGER DEFAULT 0,
            mtime REAL,
            size INTEGER,
            shamela_id TEXT,
            folder TEXT
        );
        CREATE TABLE IF NOT EXISTS slides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            slide_number INTEGER NOT NULL,
            html_content TEXT NOT NULL,
            plain_text TEXT
        );
        CREATE TABLE IF NOT EXISTS headings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            slide_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            text TEXT NOT NULL,
            anchor_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'inline'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            normalized_text,
            book_id UNINDEXED,
            slide_id UNINDEXED,
            heading_text UNINDEXED
        );
        CREATE INDEX IF NOT EXISTS idx_slides_book ON slides(book_id);
        CREATE INDEX IF NOT EXISTS idx_slides_book_num ON slides(book_id, slide_number);
        CREATE INDEX IF NOT EXISTS idx_headings_book ON headings(book_id);
    """)
    conn.commit()
    conn.close()


def clear_library():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM headings")
    c.execute("DELETE FROM slides")
    c.execute("DELETE FROM books")
    c.execute("DELETE FROM search_index")
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#  HTML PARSING
# ═══════════════════════════════════════════════════════════════

_P_OPEN_RE = re.compile(r'<p\b[^>]*>', re.IGNORECASE)
_P_CLOSE_RE = re.compile(r'</p\s*>', re.IGNORECASE)


def fix_legacy_paragraph_breaks(raw_html):
    raw_html = _P_OPEN_RE.sub('', raw_html)
    raw_html = _P_CLOSE_RE.sub('<br>', raw_html)
    return raw_html


KNOWN_SLIDE_SELECTORS = [
    'div.PageText', 'div.pagetext',
    'div.slide', 'div.page', 'div.Page', 'div.bk', 'div.nass', 'div.mtn',
    '.slide', '.page', '.chapter', 'section', 'article',
]


def find_slide_containers(body):
    for selector in KNOWN_SLIDE_SELECTORS:
        containers = body.select(selector)
        if len(containers) > 1:
            total_text = sum(len(c.get_text(strip=True)) for c in containers)
            if total_text > 200:
                return containers

    body_text_len = len(body.get_text(strip=True)) or 1
    class_groups = {}
    for div in body.find_all('div'):
        classes = div.get('class')
        if not classes:
            continue
        key = ' '.join(classes)
        class_groups.setdefault(key, []).append(div)

    best_containers, best_coverage = None, 0
    for els in class_groups.values():
        if len(els) < 2:
            continue
        total_text = sum(len(e.get_text(strip=True)) for e in els)
        coverage = total_text / body_text_len
        if coverage > best_coverage:
            best_coverage = coverage
            best_containers = els

    if best_containers and best_coverage > 0.5:
        return best_containers
    return None


_HEADING_TAG_RE = re.compile(r'^h[1-6]$')


def extract_headings(container, slide_idx):
    results = []
    for hi, h in enumerate(container.find_all(_HEADING_TAG_RE)):
        if not h.get('id'):
            h['id'] = f"anchor-{slide_idx}-h{hi}"
        text = h.get_text(strip=True)
        if text:
            results.append((int(h.name[1]), text, h['id']))
    for mi, m in enumerate(container.find_all(attrs={'data-type': 'title'})):
        if not m.get('id'):
            m['id'] = f"anchor-{slide_idx}-t{mi}"
        text = m.get_text(strip=True)
        if text:
            results.append((2, text, m['id']))
    return results


def process_containers(containers):
    slides = []
    for idx, container in enumerate(containers, 1):
        headings = extract_headings(container, idx)
        html = str(container)
        text = container.get_text(separator=' ', strip=True)
        slides.append({'number': idx, 'html': html, 'text': text, 'headings': headings})
    return slides


def parse_book(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        raw_html = f.read()

    raw_html = fix_legacy_paragraph_breaks(raw_html)
    soup = BeautifulSoup(raw_html, 'lxml')

    title = ''
    if soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        h1 = soup.find('h1')
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        title = filepath.stem

    for tag in soup.find_all(['script', 'style']):
        tag.decompose()

    body = soup.find('body') or soup
    slides = []
    containers = find_slide_containers(body)
    if containers:
        slides = process_containers(containers)

    if not slides:
        direct_children = [c for c in body.children
                           if not isinstance(c, NavigableString)
                           and getattr(c, 'name', None) in ('div', 'section', 'article')
                           and len(c.get_text(strip=True)) > 100]
        if direct_children:
            slides = process_containers(direct_children)

    if not slides:
        headings = extract_headings(body, 1)
        slides = [{
            'number': 1,
            'html': str(body),
            'text': body.get_text(separator=' ', strip=True),
            'headings': headings,
        }]

    unique_slides = []
    seen_hashes = set()
    for slide in slides:
        text_key = slide['text'].replace(' ', '').replace('\n', '')[:300]
        if text_key not in seen_hashes and len(slide['text'].strip()) > 20:
            seen_hashes.add(text_key)
            unique_slides.append(slide)

    for i, slide in enumerate(unique_slides, 1):
        slide['number'] = i

    return {'title': title, 'slides': unique_slides}


# ═══════════════════════════════════════════════════════════════
#  LIBRARY SCANNER
# ═══════════════════════════════════════════════════════════════

def scan_library(force_full=False):
    if force_full:
        clear_library()

    if not BOOKS_DIR.exists():
        return {'total': 0, 'added': 0, 'updated': 0, 'removed': 0,
                'unchanged': 0, 'errors': 0, 'missing_dir': True}

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    c = conn.cursor()

    c.execute("SELECT id, path, mtime, size FROM books")
    existing = {row[1]: {'id': row[0], 'mtime': row[2], 'size': row[3]} for row in c.fetchall()}

    html_files = [p for p in BOOKS_DIR.rglob('*')
                  if p.is_file() and p.suffix.lower() in HTML_SUFFIXES]

    seen_paths = set()
    stats = {'added': 0, 'updated': 0, 'removed': 0, 'unchanged': 0, 'errors': 0}

    for filepath in html_files:
        path_str = str(filepath)
        seen_paths.add(path_str)
        try:
            st = filepath.stat()
            mtime, size = st.st_mtime, st.st_size
            prev = existing.get(path_str)
            folder = str(filepath.parent.name)

            if prev and prev['mtime'] == mtime and prev['size'] == size:
                stats['unchanged'] += 1
                continue

            data = parse_book(filepath)

            if prev:
                book_id = prev['id']
                c.execute("DELETE FROM headings WHERE book_id=?", (book_id,))
                c.execute("DELETE FROM slides WHERE book_id=?", (book_id,))
                c.execute("DELETE FROM search_index WHERE book_id=?", (book_id,))
                c.execute(
                    "UPDATE books SET title=?, filename=?, slide_count=?, mtime=?, size=?, folder=? WHERE id=?",
                    (data['title'], filepath.name, len(data['slides']), mtime, size, folder, book_id)
                )
                stats['updated'] += 1
            else:
                c.execute(
                    "INSERT INTO books (title, filename, path, slide_count, mtime, size, folder) VALUES (?,?,?,?,?,?,?)",
                    (data['title'], filepath.name, path_str, len(data['slides']), mtime, size, folder)
                )
                book_id = c.lastrowid
                stats['added'] += 1

            heading_rows = []
            search_rows = []
            for slide in data['slides']:
                c.execute(
                    "INSERT INTO slides (book_id, slide_number, html_content, plain_text) VALUES (?,?,?,?)",
                    (book_id, slide['number'], slide['html'], slide['text'])
                )
                slide_id = c.lastrowid

                for level, htext, anchor in slide.get('headings', []):
                    heading_rows.append((book_id, slide_id, level, htext, anchor))
                    norm = normalize_arabic(htext)
                    if norm:
                        search_rows.append((norm, book_id, slide_id, htext))

                if slide['text']:
                    norm_body = normalize_arabic(slide['text'])
                    if norm_body:
                        search_rows.append((norm_body, book_id, slide_id, ''))

            if heading_rows:
                c.executemany(
                    "INSERT INTO headings (book_id, slide_id, level, text, anchor_id, source) VALUES (?,?,?,?,?,'inline')",
                    heading_rows
                )
            if search_rows:
                c.executemany(
                    "INSERT INTO search_index (normalized_text, book_id, slide_id, heading_text) VALUES (?,?,?,?)",
                    search_rows
                )

        except Exception as e:
            print(f"Error parsing {filepath}: {e}")
            stats['errors'] += 1

    removed_paths = set(existing.keys()) - seen_paths
    for path_str in removed_paths:
        book_id = existing[path_str]['id']
        c.execute("DELETE FROM headings WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM slides WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM search_index WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM books WHERE id=?", (book_id,))
        stats['removed'] += 1

    conn.commit()
    conn.close()

    stats['total'] = len(html_files)
    stats['missing_dir'] = False
    return stats


# ═══════════════════════════════════════════════════════════════
#  SHAMELA .DB DETECTION & VALIDATION
# ═══════════════════════════════════════════════════════════════

def validate_shamela_db(db_path):
    try:
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0].lower() for row in c.fetchall()}
        if not {'page', 'title'}.issubset(tables):
            return False, "Missing tables (page, title)"
        c.execute("PRAGMA table_info(page)")
        page_cols = {row[1].lower() for row in c.fetchall()}
        if not {'id', 'part', 'page'}.issubset(page_cols):
            return False, "Missing columns in page table"
        c.execute("PRAGMA table_info(title)")
        title_cols = {row[1].lower() for row in c.fetchall()}
        if not {'id', 'page', 'parent'}.issubset(title_cols):
            return False, "Missing columns in title table"
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)


def find_shamela_db(folder_path):
    """Find a valid Shamela .db file in the given folder.
    Tries common names like 2864.db, metadata.db, or any .db file."""
    if not folder_path or not folder_path.exists():
        return None
    
    # Try specific patterns first
    common_names = ['metadata.db', 'shamela.db', 'toc.db']
    for name in common_names:
        db_path = folder_path / name
        if db_path.exists():
            is_valid, _ = validate_shamela_db(db_path)
            if is_valid:
                return db_path
    
    # Try any .db file
    for db_path in folder_path.glob("*.db"):
        is_valid, _ = validate_shamela_db(db_path)
        if is_valid:
            return db_path
    return None


def get_book_folder(book_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT path FROM books WHERE id=?", (book_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return Path(row[0]).parent
    return None


def get_book_id_by_path(filepath):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM books WHERE path=?", (str(filepath),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def is_single_volume_book(book_id):
    """Determine if a book is single-volume based on its location.
    Standalone HTM in books/ = single-volume
    HTM in subfolder = part of multi-part book"""
    book_folder = get_book_folder(book_id)
    if not book_folder:
        return True  # Default to single-volume if we can't determine
    
    book_path = None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT path FROM books WHERE id=?", (book_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        book_path = Path(row[0])
    
    if not book_path:
        return True
    
    # If book is directly in books/ folder (not in subfolder), it's single-volume
    if book_path.parent.name == 'books' or str(book_path.parent) == str(BOOKS_DIR):
        return True
    
    # If book is in a subfolder, it's part of a multi-part book
    return False


def is_single_volume_db(db_path):
    """Check if .db file represents a single-volume book (all parts are NULL)."""
    try:
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute("SELECT DISTINCT part FROM page WHERE part IS NOT NULL AND part != ''")
        parts = [row[0] for row in c.fetchall()]
        conn.close()
        
        # If no non-NULL/empty parts found, it's single-volume
        if not parts:
            return True
        return False
    except Exception:
        return False


def detect_local_part_name(book_id):
    """Detect which part this local file represents.
    Returns part name string (e.g., '1', '2', '001', 'المقدمة', 'الكتاب') or None."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT filename, path FROM books WHERE id=?", (book_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None

    filename, path = row

    # Try filename pattern: 001.htm -> part "1" or "001"
    m = re.match(r'^(\d+)\.(?:htm|html)$', filename, re.I)
    if m:
        part_num = m.group(1)
        # Normalize to string without leading zeros for matching
        return part_num

    # Try filename without extension for non-numeric parts (e.g., المقدمة.htm -> المقدمة)
    m = re.match(r'^([^.]+)\.(?:htm|html)$', filename, re.I)
    if m:
        part_name = m.group(1)
        # If it's a known part name (like المقدمة, الكتاب, etc.), use it as-is
        # Otherwise try to extract numeric part from it
        if part_name.isdigit():
            return part_name
        # Check if it contains a number
        num_match = re.search(r'(\d+)', part_name)
        if num_match:
            return num_match.group(1)
        # Return the filename without extension as the part identifier
        return part_name

    # Try PartName span in first slide
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT html_content FROM slides WHERE book_id=? ORDER BY slide_number LIMIT 3", (book_id,))
    for (html,) in c.fetchall():
        # Look for جـ N or الجزء N or ج 1 or جزء 1
        pm = re.search(r'ج[ـ]?\s*(\d+)', html)
        if pm:
            conn.close()
            return pm.group(1)
        pm = re.search(r'الجزء\s*(?:ال)?\s*(\d+)', html)
        if pm:
            conn.close()
            return pm.group(1)
    conn.close()

    return None


# ═══════════════════════════════════════════════════════════════
#  SHAMELA.WS FETCHING
# ═══════════════════════════════════════════════════════════════

SHAMELA_TOC_SELECTOR = "div.s-nav-head + ul > li"
SHAMELA_BOOK_ID_RE = re.compile(r'(?:shamela\.ws/book/)?(\d+)(?:/(\d+))?')
SHAMELA_PAGE_HREF_RE = re.compile(r'/book/\d+/(\d+)')

# Matches both single and double quotes for class attribute
LOCAL_PAGENUMBER_SPAN_RE = re.compile(r"<span\s+class\s*=\s*['\"]PageNumber['\"]>\s*\(\s*ص\s*:\s*(\d+)\s*\)\s*</span>", re.IGNORECASE)


def extract_shamela_book_id(raw):
    raw = (raw or '').strip()
    match = SHAMELA_BOOK_ID_RE.search(raw)
    if match:
        return (match.group(1), match.group(2))
    return (None, None)


def fetch_shamela_toc(shamela_id):
    book_id, part_id = extract_shamela_book_id(shamela_id)
    if not book_id:
        return []

    url = f"https://shamela.ws/book/{book_id}"
    try:
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'ar,en;q=0.8',
        }, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, 'lxml')
    top_items = soup.select(SHAMELA_TOC_SELECTOR)
    entries = []

    def walk(items, level):
        for li in items:
            link = li.find('a', href=True)
            if not link:
                continue
            href = link.get('href', '')
            page_match = SHAMELA_PAGE_HREF_RE.search(href)
            if not page_match:
                continue
            text = link.get_text(strip=True)
            if text:
                entries.append({
                    'level': level,
                    'text': text,
                    'abs_page': int(page_match.group(1))
                })
            sub_ul = li.find('ul', recursive=False)
            if sub_ul:
                walk(sub_ul.find_all('li', recursive=False), level + 1)

    if top_items:
        walk(top_items, 1)
    else:
        for link in soup.find_all('a', href=True):
            href = link['href']
            if f'/book/{book_id}/' not in href:
                continue
            page_match = SHAMELA_PAGE_HREF_RE.search(href)
            text = link.get_text(strip=True)
            if page_match and text:
                entries.append({
                    'level': 1,
                    'text': text,
                    'abs_page': int(page_match.group(1))
                })

    return entries


# ═══════════════════════════════════════════════════════════════
#  SYNTHETIC ANCHOR INJECTION
# ═══════════════════════════════════════════════════════════════

def inject_synthetic_anchor(html_content, heading_text, anchor_id):
    if not html_content or not heading_text:
        return html_content
    try:
        soup = BeautifulSoup(f'<div id="temp-wrapper">{html_content}</div>', 'lxml')
        wrapper = soup.find('div', id='temp-wrapper')
        norm_heading = normalize_arabic(heading_text)
        if not norm_heading:
            return html_content

        injected = False
        
        # First, try to find exact or partial match in text nodes
        for text_node in wrapper.find_all(string=True):
            if text_node.parent.name in ('script', 'style'):
                continue
            node_text = str(text_node).strip()
            if not node_text:
                continue
            norm_node = normalize_arabic(node_text)
            # Check if heading is contained in this node (exact or as substring)
            if norm_heading in norm_node or norm_node in norm_heading:
                span = soup.new_tag('span', id=anchor_id, style='display:none')
                text_node.parent.insert_before(span)
                injected = True
                break

        if not injected:
            # Try in element text content
            for elem in wrapper.find_all(['p', 'span', 'div', 'font', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                if elem.name in ('script', 'style'):
                    continue
                elem_text = elem.get_text(strip=True)
                if not elem_text:
                    continue
                norm_elem = normalize_arabic(elem_text)
                if norm_heading in norm_elem or norm_elem in norm_heading:
                    span = soup.new_tag('span', id=anchor_id, style='display:none')
                    elem.insert_before(span)
                    injected = True
                    break

        if not injected:
            # Try partial matching with word boundaries
            heading_words = norm_heading.split()
            if len(heading_words) > 0:
                for text_node in wrapper.find_all(string=True):
                    if text_node.parent.name in ('script', 'style'):
                        continue
                    node_text = str(text_node).strip()
                    if not node_text:
                        continue
                    norm_node = normalize_arabic(node_text)
                    node_words = norm_node.split()
                    # Check if first few words match
                    for i in range(min(3, len(heading_words))):
                        if heading_words[i] in node_words:
                            span = soup.new_tag('span', id=anchor_id, style='display:none')
                            text_node.parent.insert_before(span)
                            injected = True
                            break
                    if injected:
                        break

        if not injected:
            # Fallback: insert at the beginning of PageText or after PageHead
            page_text = wrapper.find('div', class_='PageText')
            if page_text:
                page_head = page_text.find('div', class_='PageHead')
                if page_head:
                    span = soup.new_tag('span', id=anchor_id, style='display:none')
                    page_head.insert_after(span)
                else:
                    span = soup.new_tag('span', id=anchor_id, style='display:none')
                    page_text.insert(0, span)
            else:
                span = soup.new_tag('span', id=anchor_id, style='display:none')
                wrapper.insert(0, span)

        return ''.join(str(child) for child in wrapper.children)
    except Exception as e:
        print(f"[WARN] Failed to inject anchor for '{heading_text[:40]}...': {e}")
        return html_content


# ═══════════════════════════════════════════════════════════════
#  TOC IMPORT (PER-PART, DB-BASED)
# ═══════════════════════════════════════════════════════════════

def import_toc_for_book(book_id, shamela_id):
    """Import TOC for a single part or single-volume book using local .db as page map.

    Flow:
    1. Determine if single-volume or multi-part book
    2. Find .db in parent folder
    3. Fetch Shamela TOC (whole book)
    4. Build mapping from absolute page numbers to (part, sequential_page) from .db
    5. For single-volume: import ALL TOC entries
       For multi-part: filter TOC entries to only those belonging to THIS part
    6. Map sequential page numbers to local slide page numbers
    7. Match each entry to local slides by printed page number
    8. Insert headings for this part only
    """
    # ── Step 1: Determine book type and local part ──
    book_folder = get_book_folder(book_id)
    if not book_folder:
        return {'status': 'error', 'message': 'تعذر تحديد مجلد الكتاب'}

    # Check if this is a single-volume book (standalone in books/ folder)
    is_single_volume = is_single_volume_book(book_id)

    if is_single_volume:
        # For single-volume books, we don't need a part number
        local_part = None
        print(f"[INFO] Book {book_id} detected as single-volume")
    else:
        # For multi-part books, detect the part from filename or content
        local_part = detect_local_part_name(book_id)
        if not local_part:
            return {'status': 'error', 'message': 'تعذر تحديد رقم الجزء من اسم الملف'}
        print(f"[INFO] Local file detected as part: '{local_part}'")

    # ── Step 2: Find .db ──
    db_path = find_shamela_db(book_folder)
    if not db_path:
        return {
            'status': 'error',
            'message': 'لم يتم العثور على قاعدة بيانات Shamela في مجلد الكتاب. '
                      'يرجى وضع ملف .db في نفس المجلد أو الضغط على "ربط قاعدة بيانات".'
        }

    is_valid, error = validate_shamela_db(db_path)
    if not is_valid:
        return {'status': 'error', 'message': f'قاعدة البيانات غير صالحة: {error}'}

    # ── Step 3: Build comprehensive mapping from .db ──
    s_conn = sqlite3.connect(str(db_path))
    s_c = s_conn.cursor()

    # Get all page entries: id, part, page (sequential), number
    s_c.execute("SELECT id, part, page, number FROM page WHERE page IS NOT NULL ORDER BY id")
    page_rows = s_c.fetchall()

    # Get all title entries: id, page (ref to page.id), parent
    s_c.execute("SELECT id, page, parent FROM title ORDER BY id")
    title_rows = s_c.fetchall()

    s_conn.close()

    # Build mapping: absolute_page_id -> (part, sequential_page)
    abs_page_map = {}
    for page_id, part, seq_page, number in page_rows:
        abs_page_map[page_id] = (str(part) if part else None, seq_page)

    # Build title info: title_id -> (page_id, parent_id)
    title_info = {}
    for title_id, page_id, parent_id in title_rows:
        title_info[title_id] = (page_id, parent_id)

    print(f"[INFO] .db has {len(abs_page_map)} pages and {len(title_info)} titles")

    # ── Step 4: Fetch Shamela TOC ──
    book_id_input, _ = extract_shamela_book_id(shamela_id)
    if not book_id_input:
        return {'status': 'error', 'message': 'معرف الشاملة غير صالح'}

    toc_entries = fetch_shamela_toc(book_id_input)
    if not toc_entries:
        return {'status': 'error', 'message': 'لم يتم العثور على فهرس في الشاملة'}

    print(f"[INFO] Shamela TOC has {len(toc_entries)} entries")

    # ── Step 5: Filter TOC entries ──
    # For single-volume: import ALL entries
    # For multi-part: filter to THIS part only
    part_entries = []
    unmatched_pages = []

    for entry in toc_entries:
        abs_page_id = entry['abs_page']

        if abs_page_id not in abs_page_map:
            unmatched_pages.append(entry)
            continue

        db_part, seq_page = abs_page_map[abs_page_id]

        if is_single_volume:
            # Single-volume: import ALL entries (no part filtering)
            part_entries.append({
                'level': entry['level'],
                'text': entry['text'],
                'abs_page_id': abs_page_id,
                'seq_page': seq_page,
            })
        else:
            # Multi-part: filter to THIS part only
            # Use normalize_part for consistent comparison
            db_part_norm = normalize_part(db_part)
            local_part_norm = normalize_part(local_part)

            if db_part_norm == local_part_norm:
                part_entries.append({
                    'level': entry['level'],
                    'text': entry['text'],
                    'abs_page_id': abs_page_id,
                    'seq_page': seq_page,
                })

    if is_single_volume:
        print(f"[INFO] Single-volume: importing all {len(part_entries)} TOC entries")
    else:
        print(f"[INFO] Filtered to {len(part_entries)} entries for part '{local_part}'")

    if not part_entries:
        if is_single_volume:
            return {
                'status': 'error',
                'message': 'لا توجد عناوين في الفهرس. قد يكون ترقيم الصفحات مختلفاً.'
            }
        else:
            return {
                'status': 'error',
                'message': f'لا توجد عناوين للجزء {local_part} في الفهرس. '
                          f'تأكد من رقم الجزء في اسم الملف أو محتوى الكتاب.'
            }

    # ── Step 6: Get local slides with page numbers ──
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, slide_number, html_content FROM slides WHERE book_id=? ORDER BY slide_number", (book_id,))
    slides = []
    for sid, snum, html in c.fetchall():
        m = LOCAL_PAGENUMBER_SPAN_RE.search(html[:800])
        page_num = int(m.group(1)) if m else None
        slides.append({'id': sid, 'num': snum, 'page': page_num, 'html': html})
    conn.close()

    slides_with_page = [s for s in slides if s['page'] is not None]
    page_lookup = {s['page']: s for s in slides_with_page}

    print(f"[INFO] Local file has {len(slides)} slides, {len(slides_with_page)} with page numbers")
    if slides_with_page:
        print(f"[INFO] Local page range: {slides_with_page[0]['page']} - {slides_with_page[-1]['page']}")

    # ── Step 7: Match and insert ──
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Clear old imported headings for THIS part only
    c.execute("DELETE FROM headings WHERE book_id=? AND source='imported'", (book_id,))

    stats = {'inserted': 0, 'exact': 0, 'synthetic': 0, 'unmatched': 0}

    for entry in part_entries:
        # The seq_page from .db is the sequential page number within the part
        # This should match the page numbers in the local HTM file
        target_seq_page = entry['seq_page']

        if target_seq_page is None:
            stats['unmatched'] += 1
            continue

        matched = False

        # Try exact match first
        if target_seq_page in page_lookup:
            c.execute(
                "INSERT INTO headings (book_id, slide_id, level, text, anchor_id, source) VALUES (?,?,?,?,?,'imported')",
                (book_id, page_lookup[target_seq_page]['id'], entry['level'], entry['text'], '')
            )
            stats['inserted'] += 1
            stats['exact'] += 1
            matched = True
        else:
            # Try with tolerance
            for delta in range(-PAGE_TOLERANCE, PAGE_TOLERANCE + 1):
                pg = target_seq_page + delta
                if pg in page_lookup:
                    c.execute(
                        "INSERT INTO headings (book_id, slide_id, level, text, anchor_id, source) VALUES (?,?,?,?,?,'imported')",
                        (book_id, page_lookup[pg]['id'], entry['level'], entry['text'], '')
                    )
                    stats['inserted'] += 1
                    stats['exact'] += 1
                    matched = True
                    break

        if not matched:
            # Nearest slide fallback
            best = None
            best_diff = float('inf')
            for s in slides_with_page:
                diff = abs(s['page'] - target_seq_page) if s['page'] else 999
                if diff < best_diff:
                    best_diff = diff
                    best = s

            if best and best_diff <= 5:
                aid = f"synth-{best['id']}-{stats['synthetic']}"
                new_html = inject_synthetic_anchor(best['html'], entry['text'], aid)
                if new_html != best['html']:
                    c.execute("UPDATE slides SET html_content=? WHERE id=?", (new_html, best['id']))
                c.execute(
                    "INSERT INTO headings (book_id, slide_id, level, text, anchor_id, source) VALUES (?,?,?,?,?,'imported')",
                    (book_id, best['id'], entry['level'], entry['text'], aid)
                )
                stats['inserted'] += 1
                stats['synthetic'] += 1
            else:
                stats['unmatched'] += 1

    c.execute("UPDATE books SET shamela_id=? WHERE id=?", (str(book_id_input), book_id))
    conn.commit()
    conn.close()

    return {
        'status': 'ok',
        'source': 'db',
        'matched': stats['inserted'],
        'exact': stats['exact'],
        'synthetic': stats['synthetic'],
        'unmatched': stats['unmatched'] + len(unmatched_pages),
        'total': len(toc_entries),
        'part': local_part,
        'part_entries': len(part_entries),
    }


# ═══════════════════════════════════════════════════════════════
#  FTS5 HELPERS
# ═══════════════════════════════════════════════════════════════

def build_fts_match(normalized_query, prefix=True):
    words = re.findall(r'\S+', normalized_query)
    if not words:
        return None
    parts = []
    for i, w in enumerate(words):
        w_escaped = w.replace('"', '""')
        if prefix and i == len(words) - 1:
            parts.append(f'"{w_escaped}"*')
        else:
            parts.append(f'"{w_escaped}"')
    return ' '.join(parts)


def escape_like(s):
    return s.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')


# ═══════════════════════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/refresh', methods=['POST'])
def refresh():
    try:
        force_full = request.args.get('full', '').lower() in ('1', 'true', 'yes')
        stats = scan_library(force_full=force_full)
        return jsonify({'status': 'ok', **stats})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/books')
def get_books():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, filename, slide_count, shamela_id, path, folder FROM books ORDER BY title")
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        folder = Path(r[5]).parent if r[5] else None
        has_db = bool(folder and find_shamela_db(folder))
        result.append({
            'id': r[0], 'title': r[1], 'filename': r[2],
            'slide_count': r[3], 'shamela_id': r[4],
            'folder': r[6], 'has_db': has_db,
        })
    return jsonify(result)


@app.route('/api/books/<int:book_id>/import_toc', methods=['POST'])
def import_toc(book_id):
    data = request.get_json() or {}
    raw_input = data.get('shamela_id') or ''
    if not raw_input.strip():
        return jsonify({
            'status': 'error',
            'message': 'يرجى إدخال رقم الكتاب على الشاملة أو رابط الكتاب (مثال: 2864 أو https://shamela.ws/book/2864).'
        }), 400
    result = import_toc_for_book(book_id, raw_input)
    if result.get('status') == 'error':
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/books/<int:book_id>/link_db', methods=['POST'])
def link_db(book_id):
    if 'db_file' not in request.files:
        return jsonify({'status': 'error', 'message': 'لم يتم رفع أي ملف'}), 400

    file = request.files['db_file']
    book_folder = get_book_folder(book_id)
    if not book_folder:
        return jsonify({'status': 'error', 'message': 'تعذر تحديد مجلد الكتاب'}), 400

    # Save with original filename or as metadata.db
    filename = file.filename or "metadata.db"
    target = book_folder / filename
    file.save(target)

    is_valid, error = validate_shamela_db(target)
    if not is_valid:
        target.unlink()
        return jsonify({'status': 'error', 'message': error}), 400

    return jsonify({'status': 'ok', 'path': str(target), 'filename': filename})


@app.route('/api/books/<int:book_id>/structure')
def get_structure(book_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT h.id, h.slide_id, h.level, h.text, h.anchor_id, sl.slide_number
                 FROM headings h JOIN slides sl ON h.slide_id = sl.id
                 WHERE h.book_id = ? ORDER BY sl.slide_number, h.id""", (book_id,))
    rows = c.fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'slide_id': r[1], 'level': r[2], 'text': r[3],
                      'anchor': r[4], 'slide_number': r[5]} for r in rows])


@app.route('/api/books/<int:book_id>/content')
def get_content(book_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT slide_number, html_content FROM slides WHERE book_id = ? ORDER BY slide_number", (book_id,))
    rows = c.fetchall()
    conn.close()
    return jsonify([{'number': r[0], 'html': r[1]} for r in rows])


@app.route('/api/search', methods=['POST'])
def search():
    data = request.get_json() or {}
    query = (data.get('query') or '').strip()
    book_id = data.get('book_id')
    scope = data.get('scope', 'all')

    if len(query) < 2:
        return jsonify([])

    norm_query = normalize_arabic(query)
    match_expr = build_fts_match(norm_query)
    if not match_expr:
        return jsonify([])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = []
    try:
        sql = """
            SELECT s.book_id, b.title, s.slide_id, sl.slide_number, s.heading_text, s.normalized_text
            FROM search_index s
            JOIN books b ON s.book_id = b.id
            JOIN slides sl ON s.slide_id = sl.id
            WHERE s.normalized_text MATCH ?
        """
        params = [match_expr]
        if scope == 'current' and book_id:
            sql += " AND s.book_id = ?"
            params.append(book_id)
        sql += " ORDER BY rank LIMIT 50"
        c.execute(sql, params)
        rows = c.fetchall()
    except sqlite3.OperationalError:
        like_query = '%' + escape_like(norm_query) + '%'
        sql = """
            SELECT s.book_id, b.title, s.slide_id, sl.slide_number, s.heading_text, s.normalized_text
            FROM search_index s
            JOIN books b ON s.book_id = b.id
            JOIN slides sl ON s.slide_id = sl.id
            WHERE s.normalized_text LIKE ? ESCAPE '\\'
        """
        params = [like_query]
        if scope == 'current' and book_id:
            sql += " AND s.book_id = ?"
            params.append(book_id)
        sql += " LIMIT 50"
        c.execute(sql, params)
        rows = c.fetchall()

    conn.close()

    seen = set()
    results = []
    for r in rows:
        key = (r[0], r[2])
        if key in seen:
            continue
        seen.add(key)
        text = r[5] if r[5] else ''
        snippet = text[:200] + '...' if len(text) > 200 else text
        results.append({
            'book_id': r[0], 'book_title': r[1], 'slide_id': r[2],
            'slide_number': r[3], 'heading': r[4] if r[4] else 'نص عام',
            'snippet': snippet
        })

    return jsonify(results)


@app.route('/api/ask', methods=['POST'])
def ask():
    data = request.get_json() or {}
    question = (data.get('question') or '').strip()
    if not question or not GEMINI_KEY or GEMINI_KEY == 'YOUR_GEMINI_API_KEY_HERE':
        return jsonify({'answer': 'يرجى إدخال سؤال والتأكد من ضبط مفتاح API في config.ini.', 'sources': []})

    norm_q = normalize_arabic(question)
    match_expr = build_fts_match(norm_q, prefix=False)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = []
    if match_expr:
        try:
            c.execute("""
                SELECT s.book_id, b.title, s.slide_id, sl.slide_number, s.heading_text, s.normalized_text
                FROM search_index s
                JOIN books b ON s.book_id = b.id
                JOIN slides sl ON s.slide_id = sl.id
                WHERE s.normalized_text MATCH ?
                ORDER BY rank LIMIT 10
            """, (match_expr,))
            rows = c.fetchall()
        except sqlite3.OperationalError:
            like_q = '%' + escape_like(norm_q) + '%'
            c.execute("""
                SELECT s.book_id, b.title, s.slide_id, sl.slide_number, s.heading_text, s.normalized_text
                FROM search_index s
                JOIN books b ON s.book_id = b.id
                JOIN slides sl ON s.slide_id = sl.id
                WHERE s.normalized_text LIKE ? ESCAPE '\\'
                LIMIT 10
            """, (like_q,))
            rows = c.fetchall()

    conn.close()

    passages = []
    for r in rows:
        book_title = r[1]
        heading = r[4] if r[4] else f"صفحة {r[3]}"
        text = r[5] if r[5] else ''
        passages.append(f"[المصدر: {book_title} - {heading}]\n{text[:800]}")

    context = "\n\n".join(passages) if passages else "لم يتم العثور على نصوص ذات صلة في المكتبة."

    system_prompt = (
        "أنت مساعد بحثي متخصص في النصوص الإسلامية. "
        "أجب بناءً فقط على المقتطفات المقدمة أدناه. "
        "استشهد باسم الكتاب والقسم لكل ادعاء. "
        "إذا لم تحتوِ المقتطفات على إجابة، قل بوضوح أن المعلومة غير موجودة في المكتبة."
    )
    full_prompt = f"{system_prompt}\n\nالمقتطفات من المكتبة:\n{context}\n\nالسؤال: {question}\n\nالإجابة:"

    try:
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}
        }
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        resp.raise_for_status()
        gemini_data = resp.json()
        answer = gemini_data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        answer = f"حدث خطأ أثناء الاتصال بالذكاء الاصطناعي: {str(e)}"

    return jsonify({'answer': answer, 'sources': passages})


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()
    print("Starting server at http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
