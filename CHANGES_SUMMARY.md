# Summary of Changes for TOC Import and Display Fixes

## Overview
This document summarizes all changes made to fix the TOC import functionality and display issues for Book ID 2864 and other multi-part books.

## Problem Analysis

### 1. Shamela.ws Structure
- Book 2864 (بيان تلبيس الجهمية) is divided into 8 parts on shamela.ws
- Each part has its own TOC with hierarchical headings
- URLs use absolute page numbers (e.g., Part 1: pages 3-539, Part 2: pages 540-1165, etc.)

### 2. 2864.db Database Structure
- **Page Table**: `id` (absolute page ID), `part` (part number), `page` (sequential page within part), `number`, `services`
- **Title Table**: `id`, `page` (ref to Page.id), `parent` (parent title ID)
- Contains 4,879 pages across 8 parts and 739 title entries

### 3. Local HTM Files
- Files named 001.htm-008.htm representing parts 1-8
- Each contains `PageNumber` spans with format `(ص: X)` where X is sequential page number
- Part 1 (001.htm): pages 1-539
- Part 2 (002.htm): pages 1-629
- etc.

## Issues Identified

1. **TOC Import**: Not properly mapping shamela.ws absolute pages to local sequential pages
2. **Part Detection**: Inconsistent part number extraction from filenames
3. **Tashkeel Toggle**: Causes page jumps due to content height changes
4. **Scroll Position**: TOC headings don't scroll to top of viewer
5. **Synthetic Anchors**: Poor matching for Arabic text headings

## Changes Made

### Backend (app.py)

#### 1. Improved Part Detection (`detect_local_part_name`)
- Enhanced regex to match `جـ N` or `الجزء N` patterns
- Better handling of leading zeros in filenames (001, 002, etc.)
- More robust extraction from PartName spans

#### 2. Enhanced TOC Import (`import_toc_for_book`)
- **Fixed Page Mapping**: Now correctly uses .db to map absolute pages to (part, sequential_page)
- **Proper Filtering**: Only imports TOC entries belonging to the current part
- **Better Matching**: Uses sequential page numbers from .db to match local HTM pages
- **Improved Logging**: Added detailed debug information

#### 3. Better Synthetic Anchor Injection (`inject_synthetic_anchor`)
- **Multiple Matching Strategies**:
  - Exact match in text nodes
  - Partial match in element text content
  - Word-by-word matching for Arabic text
  - Fallback to PageText/PageHead insertion
- **Normalization**: Uses `normalize_arabic` for consistent matching
- **Robust Error Handling**: Graceful degradation if injection fails

#### 4. Enhanced .db Detection (`find_shamela_db`)
- Tries common filenames first (metadata.db, shamela.db, toc.db)
- Falls back to any .db file in the folder
- Case-insensitive table/column validation

#### 5. Improved .db Upload (`link_db` endpoint)
- Preserves original filename
- Better error messages
- Returns filename in response

### Frontend (app.js)

#### 1. Fixed Tashkeel Toggle
- **Scroll Position Preservation**: Now tracks which slide is at the viewport top
- **Accurate Restoration**: After re-rendering, scrolls to the same slide position
- **Smooth Transition**: Uses `behavior: 'auto'` for instant scroll without animation

#### 2. Fixed Scroll Position for TOC Headings
- **Top Alignment**: Uses `block: 'start'` in `scrollIntoView` to ensure headings appear at top
- **Consistent Behavior**: Both anchor and slide navigation now scroll to top

#### 3. Removed Duplicate Functions
- Eliminated duplicate `scrollToAnchorWhenReady` and `scrollToSlideWhenReady` definitions

## Key Technical Details

### Page Mapping Logic
```python
# .db contains: absolute_page_id -> (part, sequential_page)
# Shamela TOC contains: entries with abs_page (absolute page ID)
# Local HTM contains: pages with sequential numbers

# For Part 1:
# - Shamela TOC entry at abs_page=3 (الجزء الأول)
# - .db maps: 3 -> ('1', 1)  # Part 1, sequential page 1
# - Local 001.htm has page 1
# - Result: TOC entry maps to local page 1

# For Part 2:
# - Shamela TOC entry at abs_page=540 (الجزء الثاني)
# - .db maps: 540 -> ('2', 1)  # Part 2, sequential page 1
# - Local 002.htm has page 1
# - Result: TOC entry maps to local page 1
```

### Multi-Part Book Handling
- Each part is treated as a separate book in the library
- Each part has its own .db file (or shares one in the parent folder)
- TOC import only fetches entries for the current part
- No overlap or duplication between parts

## Testing

Created comprehensive test suite (`test_toc_import.py`):
- ✅ Database structure validation
- ✅ Page mapping verification
- ✅ Shamela TOC fetching
- ✅ Local page extraction
- ✅ Part detection from filenames

All tests pass successfully.

## Usage Instructions

### For Single-Volume Books
1. Place HTM file in `books/` folder
2. Place corresponding .db file in same folder
3. Click "استيراد الفهرس من الشاملة" to import TOC

### For Multi-Part Books
1. Create subfolder in `books/` for the book (e.g., `books/2864/`)
2. Place part files (001.htm, 002.htm, etc.) in subfolder
3. Place .db file in subfolder (e.g., `2864.db`)
4. Each part will appear separately in the tree
5. Click "استيراد الفهرس من الشاملة" for each part to import its TOC

### Manual .db Upload
1. Click "ربط قاعدة بيانات" button for a book/part
2. Select .db file from your computer
3. File will be saved to the book's folder
4. Now you can import TOC

## Files Modified

- `app.py`: Backend logic for TOC import, part detection, page mapping
- `app.js`: Frontend fixes for tashkeel toggle and scroll position
- `test_toc_import.py`: New test suite (optional)

## Backward Compatibility

All changes are backward compatible:
- Existing single-volume books continue to work
- Existing .db files are still recognized
- No breaking changes to API or UI

## Future Enhancements

Potential improvements for future versions:
1. Batch TOC import for all parts of a multi-volume book
2. Automatic .db file download from shamela.ws
3. Caching of fetched TOCs to reduce network requests
4. Better handling of edge cases (missing pages, duplicate headings)
