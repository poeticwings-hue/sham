// ── State ──
var currentBookId = null;
var books = [];
var currentBookStructure = [];
var tashkeelVisible = true;
var currentSlidesData = [];   // [{number, html}] for the open book, cached for instant tashkeel toggling
var renderToken = 0;          // bumped whenever we (re)start rendering, so stale async work can bail out
var CHUNK_SIZE = 40;          // slides rendered per progressive batch (keeps huge books from freezing the tab)

// Arabic diacritics (tashkeel) range — matches the same characters the
// server strips for search normalization, so toggling is predictable.
var TASHKEEL_REGEX = /[\u064B-\u065F\u0670]/g;

// ── DOM Elements ──
var bookTree = document.getElementById('book-tree');
var bookContent = document.getElementById('book-content');
var welcome = document.getElementById('welcome');
var reader = document.getElementById('reader');
var searchModal = document.getElementById('search-modal');
var searchInput = document.getElementById('search-input');
var searchResults = document.getElementById('search-results');
var aiPanel = document.getElementById('ai-panel');
var aiMessages = document.getElementById('ai-messages');
var aiInput = document.getElementById('ai-input');
var fontSelector = document.getElementById('font-selector');

// ── New DOM Elements for TOC Search ──
var tocSearchInput = document.getElementById('toc-search-input');
var tocSearchClear = document.getElementById('toc-search-clear');

// ── Additional State ──
var currentSlideNumber = null;  // Track current slide for session restoration
var currentActiveHeadingId = null;  // Track active heading for highlighting
var currentSearchQuery = null;  // Track current search query for highlighting in pages
var lastSearchResults = [];  // Store last search results for persistence

// Arabic diacritics (tashkeel) range — matches the same characters the
// server strips for search normalization, so toggling is predictable.
var TASHKEEL_REGEX = /[\u064B-\u065F\u0670]/g;

// ── Session State Keys ──
var SESSION_STATE_KEY = 'libraryReaderState';

// ── Init ──
document.addEventListener('DOMContentLoaded', function() {
    loadBooks();
    setupEventListeners();
    setupFontSwitcher();
    setupTashkeelToggle();
    setupTOCSearch();
    restoreSessionState();
});

// ── Event Listeners ──
function setupEventListeners() {
    document.getElementById('btn-refresh').addEventListener('click', function() { refreshLibrary(false); });
    document.getElementById('btn-refresh-full').addEventListener('click', function() {
        if (confirm('سيُعاد فهرسة جميع الكتب من جديد، وقد يستغرق ذلك وقتاً أطول من التحديث العادي. هل تريد الاستمرار؟')) {
            refreshLibrary(true);
        }
    });
    document.getElementById('btn-search-toggle').addEventListener('click', function() { toggleModal(searchModal, true); });
    document.getElementById('close-search').addEventListener('click', function() { toggleModal(searchModal, false); });
    document.getElementById('btn-ai-toggle').addEventListener('click', function() { toggleAI(true); });
    document.getElementById('close-ai').addEventListener('click', function() { toggleAI(false); });
    document.getElementById('btn-send-ai').addEventListener('click', sendAIQuestion);

    // Search button
    var searchButton = document.getElementById('btn-search-execute');
    if (searchButton) {
        searchButton.addEventListener('click', handleSearch);
    }

    // Allow Enter key to trigger search
    searchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            handleSearch();
        }
    });

    searchInput.addEventListener('input', debounce(handleSearch, 400));

    searchModal.addEventListener('click', function(e) {
        if (e.target === searchModal) toggleModal(searchModal, false);
    });

    aiInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendAIQuestion();
        }
    });
}

// ── Font Switcher ──
function setupFontSwitcher() {
    fontSelector.addEventListener('change', function(e) {
        var font = e.target.value;
        document.documentElement.style.setProperty('--font-body', '"' + font + '", serif');
        document.documentElement.style.setProperty('--font-heading', '"' + font + '", serif');
    });
}

// ── Tashkeel Toggle ──
// Re-renders the already-fetched slide HTML through a single cheap regex
// pass instead of walking the DOM tree node-by-node, which is what made
// this slow (or simply not work, since it wasn't wired to anything) on
// long pages before. The reader's scroll position is preserved
// proportionally so toggling doesn't jump the user back to the top.
function setupTashkeelToggle() {
    var btn = document.getElementById('btn-tashkeel');
    if (!btn) return;

    btn.addEventListener('click', function() {
        tashkeelVisible = !tashkeelVisible;
        btn.classList.toggle('active-toggle', !tashkeelVisible);
        btn.textContent = tashkeelVisible ? '◌ التشكيل' : '◌ بدون تشكيل';

        if (!currentSlidesData.length) return;

        // Save the exact scroll position before re-rendering
        var scrollTop = reader.scrollTop;
        var scrollLeft = reader.scrollLeft;
        
        // Get the first visible slide's offset
        var slides = bookContent.querySelectorAll('.slide-page');
        var firstVisibleSlide = null;
        var firstVisibleSlideOffset = null;
        
        for (var i = 0; i < slides.length; i++) {
            var slideTop = slides[i].offsetTop;
            var slideBottom = slideTop + slides[i].offsetHeight;
            
            // Check if this slide is visible in the viewport
            if (slideBottom > scrollTop && slideTop < scrollTop + reader.clientHeight) {
                firstVisibleSlide = slides[i];
                firstVisibleSlideOffset = slideTop - scrollTop;
                break;
            }
        }

        renderToken++;
        renderAllSlides(renderToken, null);

        // After rendering, restore the scroll position
        requestAnimationFrame(function() {
            var newSlides = bookContent.querySelectorAll('.slide-page');
            if (firstVisibleSlide && newSlides.length > 0) {
                // Find the corresponding slide in the new DOM
                var slideNumber = firstVisibleSlide.dataset.slideNumber;
                var targetSlide = bookContent.querySelector('.slide-page[data-slide-number="' + slideNumber + '"]');
                if (targetSlide) {
                    // Scroll to the same relative position within the slide
                    var newTargetOffset = targetSlide.offsetTop + (firstVisibleSlideOffset || 0);
                    reader.scrollTop = newTargetOffset;
                }
            }
            
            // Re-apply search highlights if there's an active search
            if (currentSearchQuery) {
                setTimeout(function() {
                    highlightSearchTermsInPage(currentSearchQuery);
                }, 100);
            }
        });
    });
}

function stripTashkeel(html) {
    return html.replace(TASHKEEL_REGEX, '');
}

function buildSlideHtml(slide) {
    var html = tashkeelVisible ? slide.html : stripTashkeel(slide.html);
    return '<div class="slide-page" data-slide-number="' + slide.number + '">' + html + '</div>';
}

// ── Library Loading ──
function loadBooks() {
    fetch('/api/books')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            books = data;
            renderBookTree();
        })
        .catch(function(err) {
            console.error('Failed to load books:', err);
            bookTree.innerHTML = '<div class="empty-msg">تعذر تحميل المكتبة. تأكد من تشغيل الخادم.</div>';
        });
}

function refreshLibrary(forceFull) {
    var btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    document.getElementById('btn-refresh-full').disabled = true;
    btn.classList.add('active-toggle');
    btn.innerHTML = '<span class="spinner"></span> جارٍ التحديث...';

    var url = '/api/refresh' + (forceFull ? '?full=1' : '');

    fetch(url, { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.status === 'error') {
                alert('حدث خطأ أثناء تحديث المكتبة:\n' + data.message);
                return;
            }
            if (data.missing_dir) {
                alert('لم يتم العثور على مجلد "books" بجانب البرنامج. أضف كتبك إليه ثم أعد التحديث.');
                return;
            }
            loadBooks();
            var msg = 'تم تحديث المكتبة (' + data.total + ' ملف).\n' +
                'جديد: ' + data.added + '   معدَّل: ' + data.updated +
                '   بلا تغيير: ' + data.unchanged + '   محذوف: ' + data.removed;
            if (data.errors > 0) msg += '\nتعذرت معالجة ' + data.errors + ' ملف(ات) — راجع نافذة التشغيل لتفاصيل الخطأ.';
            alert(msg);
        })
        .catch(function(err) {
            console.error('Refresh error:', err);
            alert('تعذر الاتصال بالخادم. تأكد من تشغيله.');
        })
        .finally(function() {
            btn.disabled = false;
            document.getElementById('btn-refresh-full').disabled = false;
            btn.classList.remove('active-toggle');
            btn.textContent = 'تحديث المكتبة';
        });
}

// ── Tree Rendering ──
function renderBookTree() {
    bookTree.innerHTML = '';
    if (books.length === 0) {
        bookTree.innerHTML = '<div class="empty-msg">لا توجد كتب في المجلد. أضف ملفات HTML إلى مجلد books ثم اضغط "تحديث المكتبة".</div>';
        return;
    }

    books.forEach(function(book) {
        var node = createTreeNode(book);
        bookTree.appendChild(node);
    });
    
    // Try to restore session state after books are loaded
    setTimeout(applyPendingSessionState, 200);
}

function createTreeNode(book) {
    var node = document.createElement('div');
    node.className = 'tree-node';
    node.dataset.bookId = book.id;

    var toggle = document.createElement('div');
    toggle.className = 'tree-toggle';

    var arrow = document.createElement('span');
    arrow.className = 'tree-arrow';
    arrow.textContent = '◄';

    var label = document.createElement('span');
    label.className = 'tree-label';
    label.textContent = book.title;

    var dbIndicator = document.createElement('span');
    dbIndicator.className = 'db-indicator';
    if (book.has_db) {
        dbIndicator.textContent = '📚';
        dbIndicator.title = 'قاعدة بيانات مرتبطة';
    } else {
        dbIndicator.textContent = '❌';
        dbIndicator.title = 'لا توجد قاعدة بيانات';
        dbIndicator.style.opacity = '0.5';
    }

    toggle.appendChild(arrow);
    toggle.appendChild(label);
    toggle.appendChild(dbIndicator);
    node.appendChild(toggle);

    var children = document.createElement('div');
    children.className = 'tree-children';
    node.appendChild(children);

    var loaded = false;
    var expanded = false;

    toggle.addEventListener('click', function() {
        if (!loaded) {
            loadBookStructure(book.id, children).then(function() {
                loaded = true;
                expanded = true;
                children.classList.add('open');
                arrow.classList.add('expanded');
                // After loading structure, try to apply pending session state
                setTimeout(applyPendingSessionState, 100);
            });
        } else {
            expanded = !expanded;
            children.classList.toggle('open', expanded);
            arrow.classList.toggle('expanded', expanded);
        }

        if (!node.classList.contains('active')) {
            document.querySelectorAll('.tree-node.active').forEach(function(n) { n.classList.remove('active'); });
            node.classList.add('active');
            loadBookContent(book.id);
        }
    });

    return node;
}

function loadBookStructure(bookId, container) {
    return fetch('/api/books/' + bookId + '/structure')
        .then(function(res) { return res.json(); })
        .then(function(headings) {
            currentBookStructure = headings;
            renderStructureContainer(bookId, container, headings);
        })
        .catch(function(err) {
            renderStructureContainer(bookId, container, []);
        });
}

function renderStructureContainer(bookId, container, headings) {
    container.innerHTML = '';

    var actionRow = document.createElement('div');
    actionRow.className = 'tree-actions';

    var importRow = document.createElement('div');
    importRow.className = 'tree-import-toc';
    importRow.textContent = '⤓ استيراد الفهرس من الشاملة';
    importRow.title = 'يجلب فهرس الفصول/الأبواب الكامل من shamela.ws ويربطه بصفحات هذا الكتاب';
    importRow.addEventListener('click', function(e) {
        e.stopPropagation();
        runShamelaImport(bookId, container);
    });
    actionRow.appendChild(importRow);

    // Find the book data to check has_db
    var bookData = books.find(function(b) { return b.id === bookId; });
    if (!bookData || !bookData.has_db) {
        var linkDbRow = document.createElement('div');
        linkDbRow.className = 'tree-link-db';
        linkDbRow.textContent = '📚 ربط قاعدة بيانات';
        linkDbRow.title = 'رفع ملف .db يدوياً لربطه بهذا الكتاب';
        linkDbRow.addEventListener('click', function(e) {
            e.stopPropagation();
            uploadDbFile(bookId, container);
        });
        actionRow.appendChild(linkDbRow);
    }

    container.appendChild(actionRow);

    if (!headings || headings.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'empty-msg';
        empty.style.padding = '8px';
        empty.style.fontSize = '0.85rem';
        empty.textContent = 'لا يوجد فهرس لهذا الكتاب';
        container.appendChild(empty);
        return;
    }

    headings.forEach(function(h) {
        var div = document.createElement('div');
        div.className = 'tree-heading level-' + h.level;
        div.dataset.headingId = h.id;  // Add heading ID for tracking
        div.textContent = h.text;
        div.addEventListener('click', function(e) {
            e.stopPropagation();
            navigateToHeading(bookId, h);
            setActiveHeading(h.id);  // Track active heading
        });
        container.appendChild(div);
    });
}

// Real <h1-6>/data-type="title" headings carry an anchor inside the page
// itself; headings imported from shamela.ws don't (we only know which
// page they start on), so they fall back to scrolling to that whole page.
function navigateToHeading(bookId, h) {
    var target = h.anchor ? { type: 'anchor', value: h.anchor } : { type: 'slide', value: h.slide_number };
    
    // Track current slide number for session restoration
    if (target.type === 'slide') {
        currentSlideNumber = target.value;
    }
    
    if (currentBookId === bookId) {
        if (target.type === 'anchor') scrollToAnchorWhenReady(target.value);
        else scrollToSlideWhenReady(target.value);
    } else {
        loadBookContent(bookId, target);
    }
    
    saveSessionState();
}

// Modified scroll functions to ensure they scroll to the top of the element
function scrollToAnchorWhenReady(anchorId, attemptsLeft) {
    attemptsLeft = attemptsLeft === undefined ? 40 : attemptsLeft;
    var el = document.getElementById(anchorId);
    if (el) {
        // Scroll to the element with block: 'start' to ensure it's at the top
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    if (attemptsLeft <= 0) return;
    setTimeout(function() { scrollToAnchorWhenReady(anchorId, attemptsLeft - 1); }, 50);
}

function scrollToSlideWhenReady(slideNumber, attemptsLeft) {
    attemptsLeft = attemptsLeft === undefined ? 40 : attemptsLeft;
    var el = bookContent.querySelector('[data-slide-number="' + slideNumber + '"]');
    if (el) {
        // Scroll to the slide with block: 'start' to ensure it's at the top
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    if (attemptsLeft <= 0) return;
    setTimeout(function() { scrollToSlideWhenReady(slideNumber, attemptsLeft - 1); }, 50);
}


// ── Manual DB Upload ──
function uploadDbFile(bookId, container) {
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = '.db';
    input.onchange = function(e) {
        var file = e.target.files[0];
        if (!file) return;

        var form = new FormData();
        form.append('db_file', file);

        var linkRow = container.querySelector('.tree-link-db');
        if (linkRow) {
            linkRow.textContent = 'جارٍ الرفع...';
            linkRow.classList.add('loading');
        }

        fetch('/api/books/' + bookId + '/link_db', {
            method: 'POST',
            body: form
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.status === 'ok') {
                alert('تم ربط قاعدة البيانات بنجاح');
                loadBooks(); // Refresh to update indicators
            } else {
                alert('خطأ: ' + (data.message || 'فشل في ربط القاعدة'));
            }
        })
        .catch(function(err) {
            alert('تعذر الاتصال بالخادم');
        })
        .finally(function() {
            if (linkRow) {
                linkRow.textContent = '🗃️ ربط قاعدة بيانات';
                linkRow.classList.remove('loading');
            }
        });
    };
    input.click();
}

function runShamelaImport(bookId, container) {
    var input = prompt('أدخل رقم الكتاب على موقع الشاملة (shamela.ws) أو رابط الكتاب كاملاً:\nمثال: 2864 أو https://shamela.ws/book/2864');
    if (!input || !input.trim()) return;

    var importRow = container.querySelector('.tree-import-toc');
    if (importRow) {
        importRow.textContent = 'جارٍ الاستيراد...';
        importRow.classList.add('loading');
    }

    fetch('/api/books/' + bookId + '/import_toc?debug=1', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ shamela_id: input.trim() })
    })
    .then(function(res) {
        return res.json().then(function(data) { return { ok: res.ok, data: data }; });
    })
    .then(function(result) {
        if (!result.ok || result.data.status === 'error') {
            alert('تعذر الاستيراد:\n' + (result.data.message || 'خطأ غير معروف'));
            if (importRow) { importRow.textContent = '⤓ استيراد الفهرس من الشاملة'; importRow.classList.remove('loading'); }
            return;
        }
        var d = result.data;
        alert('تم الاستيراد: ' + d.matched + ' من ' + d.total + ' عنصراً تم ربطه بنجاح بصفحات الكتاب' +
              (d.unmatched > 0 ? ('\n(' + d.unmatched + ' عنصراً لم يُطابَق - قد يكون ترقيم الصفحات مختلفاً قليلاً)') : '.'));
        loadBookStructure(bookId, container);
    })
    .catch(function(err) {
        alert('تعذر الاتصال بالخادم أو بموقع الشاملة.');
        if (importRow) { importRow.textContent = '⤓ استيراد الفهرس من الشاملة'; importRow.classList.remove('loading'); }
    });
}

// ── Book Content Loading ──
// jumpTarget (optional): { type: 'anchor', value: anchorId } or { type: 'slide', value: slideNumber }
function loadBookContent(bookId, jumpTarget) {
    currentBookId = bookId;
    
    // Track slide number if jumpTarget is provided
    if (jumpTarget && jumpTarget.type === 'slide') {
        currentSlideNumber = jumpTarget.value;
    }
    
    welcome.classList.add('hidden');
    bookContent.classList.remove('hidden');
    bookContent.innerHTML = '<div class="loading-msg">جارٍ تحميل الكتاب...</div>';
    
    // Clear active heading when loading a new book
    if (currentActiveHeadingId) {
        var prevHeading = bookTree.querySelector('.tree-heading[data-heading-id="' + currentActiveHeadingId + '"]');
        if (prevHeading) {
            prevHeading.classList.remove('active-heading');
        }
        currentActiveHeadingId = null;
    }

    renderToken++;
    var myToken = renderToken;
    
    saveSessionState();

    fetch('/api/books/' + bookId + '/content')
        .then(function(res) { return res.json(); })
        .then(function(slides) {
            if (myToken !== renderToken) return; // a newer load started meanwhile
            currentSlidesData = slides || [];
            renderAllSlides(myToken, jumpTarget);
            
            // Add scroll listener to save position
            reader.addEventListener('scroll', debounce(function() {
                saveSessionState();
            }, 500));
        })
        .catch(function(err) {
            console.error('Error loading book:', err);
            bookContent.innerHTML = '<div class="error-msg">تعذر تحميل الكتاب</div>';
        });
}

// Renders the first batch of slides immediately (instant first paint even
// for very large books), then appends the rest in small batches via
// setTimeout so the browser stays responsive instead of freezing on one
// giant DOM insertion.
function renderAllSlides(token, jumpTarget) {
    var slides = currentSlidesData;
    if (!slides.length) {
        bookContent.innerHTML = '<div class="empty-msg">لا يوجد محتوى في هذا الكتاب</div>';
        return;
    }

    var firstChunkSize = CHUNK_SIZE;
    if (jumpTarget && jumpTarget.type === 'slide') {
        firstChunkSize = Math.max(firstChunkSize, jumpTarget.value + 3);
    }

    var firstChunk = slides.slice(0, firstChunkSize);
    bookContent.innerHTML = firstChunk.map(buildSlideHtml).join('');

    if (jumpTarget) {
        if (jumpTarget.type === 'anchor') {
            scrollToAnchorWhenReady(jumpTarget.value);
        } else if (jumpTarget.type === 'slide') {
            scrollToSlideWhenReady(jumpTarget.value);
        }
    }

    if (slides.length > firstChunkSize) {
        appendRemainingChunks(slides, firstChunkSize, token);
    }
}

function appendRemainingChunks(slides, startIndex, token) {
    if (token !== renderToken) return; // cancelled — user switched books or toggled tashkeel again
    var end = Math.min(startIndex + CHUNK_SIZE, slides.length);
    var html = slides.slice(startIndex, end).map(buildSlideHtml).join('');
    bookContent.insertAdjacentHTML('beforeend', html);
    if (end < slides.length) {
        setTimeout(function() { appendRemainingChunks(slides, end, token); }, 0);
    }
}

// ── Search ──
function handleSearch() {
    var query = searchInput.value.trim();
    if (query.length < 2) {
        searchResults.innerHTML = '';
        currentSearchQuery = null;
        lastSearchResults = [];
        clearSearchHighlights();
        return;
    }

    var scopeEl = document.querySelector('input[name="search-scope"]:checked');
    var scope = scopeEl ? scopeEl.value : 'all';

    searchResults.innerHTML = '<div class="loading-msg" style="padding:16px">جارٍ البحث...</div>';

    fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: query, book_id: currentBookId, scope: scope })
    })
    .then(function(res) {
        if (!res.ok) throw new Error('Search failed: ' + res.status);
        return res.json();
    })
    .then(function(results) {
        currentSearchQuery = query;
        lastSearchResults = results || [];
        renderSearchResults(results, query);
    })
    .catch(function(err) {
        console.error('Search error:', err);
        searchResults.innerHTML = '<div class="error-msg" style="padding:12px">خطأ في البحث: ' + escapeHtml(err.message) + '</div>';
        currentSearchQuery = null;
        lastSearchResults = [];
    });
}

// Clear search highlights from the current page
function clearSearchHighlights() {
    var highlights = bookContent.querySelectorAll('.search-highlight');
    highlights.forEach(function(h) {
        var parent = h.parentNode;
        parent.replaceChild(document.createTextNode(h.textContent), h);
        parent.normalize();
    });
}

// Highlight search terms in the current page
function highlightSearchTermsInPage(query) {
    if (!query || !bookContent || !currentBookId) return;
    
    clearSearchHighlights();
    
    var slides = bookContent.querySelectorAll('.slide-page');
    slides.forEach(function(slide) {
        var text = slide.textContent;
        if (text.includes(query)) {
            var html = slide.innerHTML;
            slide.innerHTML = highlightSearchTermsInHtml(html, query);
        }
    });
}

// Highlight search terms in HTML content
function highlightSearchTermsInHtml(html, query) {
    if (!query) return html;
    
    var queryLower = query.toLowerCase();
    var result = html;
    var lastIndex = 0;
    var matchIndex;
    var textLower = html.toLowerCase();
    
    while ((matchIndex = textLower.indexOf(queryLower, lastIndex)) !== -1) {
        // Only highlight if not already highlighted
        var before = result.substring(0, matchIndex);
        var after = result.substring(matchIndex);
        
        // Check if this match is already inside a search-highlight span
        var beforeLower = before.toLowerCase();
        var spanStartInBefore = beforeLower.lastIndexOf('<span class="search-highlight"');
        var spanEndInBefore = beforeLower.lastIndexOf('</span>');
        
        if (spanStartInBefore === -1 || spanEndInBefore > spanStartInBefore) {
            // Not inside a highlight span, so highlight this match
            var matchEnd = matchIndex + query.length;
            result = before + '<span class="search-highlight">' + 
                     html.substring(matchIndex, matchEnd) + '</span>' + 
                     after.substring(query.length);
            lastIndex = matchIndex + query.length + 34; // Skip past the added span tags
            textLower = result.toLowerCase();
        } else {
            lastIndex = matchIndex + 1;
        }
    }
    
    return result;
}

function renderSearchResults(results, query) {
    if (!results || results.length === 0) {
        searchResults.innerHTML = '<div class="empty-msg" style="padding:16px">لا توجد نتائج</div>';
        return;
    }

    // Store the current search query for highlighting
    var searchQuery = query || searchInput.value.trim();

    searchResults.innerHTML = '';
    results.forEach(function(r) {
        var item = document.createElement('div');
        item.className = 'search-result-item';
        
        // Show book title with matching text
        var titleHtml = '<div class="result-title">' + escapeHtml(r.book_title) + '</div>';
        
        // Show page info and heading
        var metaHtml = '<div class="result-meta">صفحة ' + r.slide_number + 
                      (r.heading ? ' — ' + escapeHtml(r.heading) : '') + '</div>';
        
        // Show snippet with matching text highlighted
        var snippetHtml = '<div class="result-snippet">' + 
                          highlightSearchTerms(r.snippet || '', searchQuery) + '</div>';
        
        item.innerHTML = titleHtml + metaHtml + snippetHtml;
        
        item.addEventListener('click', function() {
            // Don't close modal if we're already in the same book
            // This allows users to click multiple results without reopening search
            
            if (currentBookId === r.book_id) {
                scrollToSlideWhenReady(r.slide_number);
                // Highlight the search term in the page
                setTimeout(function() {
                    highlightSearchTermsInPage(searchQuery);
                }, 500);
            } else {
                document.querySelectorAll('.tree-node.active').forEach(function(n) { n.classList.remove('active'); });
                var node = bookTree.querySelector('.tree-node[data-book-id="' + r.book_id + '"]');
                if (node) node.classList.add('active');
                loadBookContent(r.book_id, { type: 'slide', value: r.slide_number });
                // Highlight the search term in the page after loading
                setTimeout(function() {
                    highlightSearchTermsInPage(searchQuery);
                }, 1000);
            }
        });
        searchResults.appendChild(item);
    });
}

// ── AI Chat ──
function toggleAI(show) {
    aiPanel.classList.toggle('hidden', !show);
}

function sendAIQuestion() {
    var question = aiInput.value.trim();
    if (!question) return;

    addAIMessage(question, 'user');
    aiInput.value = '';

    var btn = document.getElementById('btn-send-ai');
    btn.textContent = 'جارٍ التفكير...';
    btn.disabled = true;

    fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: question })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        addAIMessage(data.answer, 'bot', data.sources);
    })
    .catch(function(err) {
        addAIMessage('حدث خطأ في الاتصال بالمساعد الذكي.', 'bot');
    })
    .finally(function() {
        btn.textContent = 'إرسال';
        btn.disabled = false;
    });
}

function addAIMessage(text, type, sources) {
    sources = sources || [];
    var msg = document.createElement('div');
    msg.className = 'ai-message ' + type;

    var html = escapeHtml(text).replace(/\n/g, '<br>');

    if (sources.length > 0 && type === 'bot') {
        html += '<div style="margin-top:10px;border-top:1px dashed #d4c9b8;padding-top:8px;font-size:0.85rem;color:#8b6914">المصادر:</div>';
        sources.forEach(function(src, idx) {
            var clean = src.split('\n')[0].replace('[المصدر:', '').replace(']', '').trim();
            html += '<div class="source-citation" data-source="' + escapeHtml(src) + '">' + (idx + 1) + '. ' + escapeHtml(clean) + '</div>';
        });
    }

    msg.innerHTML = html;
    aiMessages.appendChild(msg);
    aiMessages.scrollTop = aiMessages.scrollHeight;
}

// ── TOC Search ──
function setupTOCSearch() {
    if (!tocSearchInput || !tocSearchClear) return;

    var tocSearchTimeout;

    tocSearchInput.addEventListener('input', function() {
        clearTimeout(tocSearchTimeout);
        tocSearchTimeout = setTimeout(filterTOCBySearch, 300);
        tocSearchClear.classList.toggle('hidden', tocSearchInput.value === '');
    });

    tocSearchClear.addEventListener('click', function() {
        tocSearchInput.value = '';
        filterTOCBySearch();
        tocSearchClear.classList.add('hidden');
        tocSearchInput.focus();
    });

    tocSearchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            tocSearchInput.value = '';
            filterTOCBySearch();
            tocSearchClear.classList.add('hidden');
        }
    });
}

function filterTOCBySearch() {
    var query = tocSearchInput.value.trim().toLowerCase();
    if (!query) {
        // Reset all visibility
        var allNodes = bookTree.querySelectorAll('.tree-node, .tree-heading');
        allNodes.forEach(function(node) {
            node.style.display = '';
        });
        return;
    }

    var hasMatches = false;
    var allNodes = bookTree.querySelectorAll('.tree-node, .tree-heading');
    
    allNodes.forEach(function(node) {
        var text = node.textContent.toLowerCase();
        if (text.includes(query)) {
            node.style.display = '';
            // Show parent nodes
            var parentNode = node.closest('.tree-node');
            if (parentNode) {
                parentNode.style.display = '';
                var children = parentNode.querySelector('.tree-children');
                if (children) {
                    children.classList.add('open');
                    var toggle = parentNode.querySelector('.tree-arrow');
                    if (toggle) toggle.classList.add('expanded');
                }
            }
            hasMatches = true;
        } else {
            node.style.display = 'none';
        }
    });

    if (!hasMatches) {
        allNodes.forEach(function(node) {
            node.style.display = '';
        });
    }
}

// ── Session Restoration ──
function saveSessionState() {
    if (!window.localStorage) return;
    
    var state = {
        lastBookId: currentBookId,
        lastSlideNumber: currentSlideNumber || null,
        lastScrollPosition: reader.scrollTop || 0,
        lastActiveHeadingId: currentActiveHeadingId || null,
        tashkeelVisible: tashkeelVisible,
        timestamp: Date.now()
    };
    
    try {
        localStorage.setItem(SESSION_STATE_KEY, JSON.stringify(state));
    } catch (e) {
        console.warn('Failed to save session state:', e);
    }
}

function restoreSessionState() {
    if (!window.localStorage) return;
    
    try {
        var savedState = localStorage.getItem(SESSION_STATE_KEY);
        if (!savedState) return;
        
        var state = JSON.parse(savedState);
        
        // Only restore if recent (within last 24 hours)
        if (Date.now() - state.timestamp > 24 * 60 * 60 * 1000) return;
        
        // Restore tashkeel state
        if (state.tashkeelVisible !== undefined) {
            tashkeelVisible = state.tashkeelVisible;
            var btn = document.getElementById('btn-tashkeel');
            if (btn) {
                btn.classList.toggle('active-toggle', !tashkeelVisible);
                btn.textContent = tashkeelVisible ? '◌ التشكيل' : '◌ بدون تشكيل';
            }
        }
        
        // Store for later use (books need to load first)
        window.pendingSessionState = state;
        
    } catch (e) {
        console.warn('Failed to restore session state:', e);
    }
}

function applyPendingSessionState() {
    if (!window.pendingSessionState) return;
    
    var state = window.pendingSessionState;
    delete window.pendingSessionState;
    
    // Find the book node
    if (state.lastBookId) {
        var bookNode = bookTree.querySelector('.tree-node[data-book-id="' + state.lastBookId + '"]');
        if (bookNode) {
            // Expand and activate the book
            var toggle = bookNode.querySelector('.tree-toggle');
            if (toggle) {
                toggle.click();  // This will expand and load structure
                
                // After a brief delay, try to restore heading
                setTimeout(function() {
                    if (state.lastActiveHeadingId) {
                        var headingNode = bookTree.querySelector('.tree-heading[data-heading-id="' + state.lastActiveHeadingId + '"]');
                        if (headingNode) {
                            headingNode.classList.add('active-heading');
                            currentActiveHeadingId = state.lastActiveHeadingId;
                        }
                    }
                    
                    // Load book content if we have a slide number
                    if (state.lastSlideNumber && currentBookId === state.lastBookId) {
                        loadBookContent(state.lastBookId, { type: 'slide', value: state.lastSlideNumber });
                        
                        // Restore scroll position after content loads
                        setTimeout(function() {
                            if (state.lastScrollPosition) {
                                reader.scrollTop = state.lastScrollPosition;
                            }
                        }, 500);
                    } else if (state.lastBookId === currentBookId) {
                        loadBookContent(state.lastBookId);
                    }
                }, 500);
            }
        }
    }
}

// ── Search Term Highlighting ──
function highlightSearchTerms(text, query) {
    if (!query) return escapeHtml(text);
    
    var queryLower = query.toLowerCase();
    var textLower = text.toLowerCase();
    var result = '';
    var lastIndex = 0;
    var matchIndex;
    
    // Simple highlighting - find all occurrences
    while ((matchIndex = textLower.indexOf(queryLower, lastIndex)) !== -1) {
        result += escapeHtml(text.substring(lastIndex, matchIndex));
        result += '<span class="search-highlight">' + escapeHtml(text.substring(matchIndex, matchIndex + query.length)) + '</span>';
        lastIndex = matchIndex + query.length;
    }
    result += escapeHtml(text.substring(lastIndex));
    
    return result;
}

// ── Active Heading Tracking ──
function setActiveHeading(headingId) {
    // Remove previous active heading
    if (currentActiveHeadingId) {
        var prevHeading = bookTree.querySelector('.tree-heading[data-heading-id="' + currentActiveHeadingId + '"]');
        if (prevHeading) {
            prevHeading.classList.remove('active-heading');
        }
    }
    
    // Set new active heading
    currentActiveHeadingId = headingId;
    if (headingId) {
        var headingNode = bookTree.querySelector('.tree-heading[data-heading-id="' + headingId + '"]');
        if (headingNode) {
            headingNode.classList.add('active-heading');
            // Scroll heading into view in sidebar
            headingNode.scrollIntoView({ behavior: 'auto', block: 'nearest' });
        }
    }
    
    saveSessionState();
}

// ── Utilities ──
function toggleModal(modal, show) {
    modal.classList.toggle('hidden', !show);
    if (show) {
        searchInput.focus();
        searchInput.value = '';
        searchResults.innerHTML = '';
    }
}

function debounce(fn, delay) {
    var timer;
    return function() {
        var args = arguments;
        clearTimeout(timer);
        timer = setTimeout(function() { fn.apply(null, args); }, delay);
    };
}

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
