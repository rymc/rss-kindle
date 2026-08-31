from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("viewport_height", [506, 535, 593, 738, 767, 796, 800])
def test_article_pages_show_complete_non_repeated_lines_with_book_rules(
    viewport_height: int,
):
    paragraphs = []
    for section in range(1, 8):
        paragraphs.append(f"<h2>Section {section}</h2>")
        for paragraph in range(1, 5):
            sentence = (
                f"Section {section}, paragraph {paragraph}. "
                "Each rendered line must remain complete and easy to follow. "
            )
            paragraphs.append(f"<p>{sentence * 7}</p>")

    html = f"""
      <!doctype html>
      <html>
        <head><meta charset="utf-8"></head>
        <body class="article-page">
          <main class="content">
            <article class="article-view">
              <header class="page-header article-header-compact">
                <h2>Pagination specimen</h2>
              </header>
              <div class="article-body">{''.join(paragraphs)}</div>
            </article>
          </main>
          <nav class="page-turn-rails page-turn-rails-article"
               data-page-mode="article" data-page-previous-url=""
               data-page-next-url="" hidden>
            <button type="button" data-page-turn="-1">Previous page</button>
            <button type="button" data-page-turn="1">Next page</button>
          </nav>
        </body>
      </html>
    """

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:  # pragma: no cover - host dependency
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            page = browser.new_page(
                viewport={"width": 600, "height": viewport_height}
            )
            page.set_content(html)
            page.add_style_tag(path=BASE_DIR / "app/static/style.css")
            page.add_script_tag(path=BASE_DIR / "app/static/reader.js")
            page.wait_for_function(
                "Number(document.documentElement.dataset.articlePageCount) > 3"
            )

            result = page.evaluate(
                """async () => {
                  const article = document.querySelector('.article-view');
                  const nextButton = document.querySelector('[data-page-turn="1"]');
                  const pageCount = Number(
                    document.documentElement.getAttribute('data-article-page-count')
                  );
                  const firstBottomMask = document.querySelector(
                    '.article-page-mask-bottom:not([hidden])'
                  );
                  const firstMaskRect = firstBottomMask?.getBoundingClientRect();
                  const maskBlocksPointer = Boolean(firstBottomMask && firstMaskRect
                    && document.elementFromPoint(
                      Math.round(firstMaskRect.left + firstMaskRect.width / 2),
                      Math.round(firstMaskRect.top + 2)
                    ) === firstBottomMask);

                  function blockKind(element) {
                    const tag = (element?.tagName || '').toLowerCase();
                    if (/^h[1-6]$/.test(tag)) return 'heading';
                    if (['p', 'li', 'blockquote', 'pre', 'dt', 'dd',
                         'figcaption', 'td', 'th'].includes(tag)) return 'paragraph';
                    return 'other';
                  }

                  function blockFor(node) {
                    let element = node.parentNode;
                    const fallback = element;
                    while (element && element !== article) {
                      if (blockKind(element) !== 'other') return element;
                      element = element.parentNode;
                    }
                    return fallback;
                  }

                  function readPage() {
                    const groups = [];
                    function groupFor(element) {
                      let group = groups.find((candidate) => candidate.element === element);
                      if (!group) {
                        group = {
                          element,
                          kind: blockKind(element),
                          order: groups.length,
                          rects: []
                        };
                        groups.push(group);
                      }
                      return group;
                    }
                    function visit(node) {
                      if (node.nodeType === Node.TEXT_NODE && /\\S/.test(node.nodeValue || '')) {
                        const range = document.createRange();
                        range.selectNodeContents(node);
                        const group = groupFor(blockFor(node));
                        for (const rect of range.getClientRects()) {
                          if (rect.width > 0 && rect.height > 0) {
                            group.rects.push({top: rect.top, bottom: rect.bottom});
                          }
                        }
                      } else if (node.nodeType === Node.ELEMENT_NODE) {
                        for (const child of node.childNodes) visit(child);
                      }
                    }
                    visit(article);

                    const topMask = document.querySelector(
                      '.article-page-mask-top:not([hidden])'
                    );
                    const bottomMask = document.querySelector(
                      '.article-page-mask-bottom:not([hidden])'
                    );
                    const topEdge = topMask ? topMask.getBoundingClientRect().bottom : 6;
                    const bottomEdge = bottomMask
                      ? bottomMask.getBoundingClientRect().top
                      : document.documentElement.clientHeight;
                    const visible = [];
                    let partialLines = 0;

                    for (const group of groups) {
                      group.rects.sort((first, second) => first.top - second.top);
                      const lines = [];
                      for (const rect of group.rects) {
                        const line = lines.at(-1);
                        if (line && Math.abs(line.top - rect.top) <= 2) {
                          line.bottom = Math.max(line.bottom, rect.bottom);
                        } else {
                          lines.push({...rect});
                        }
                      }
                      lines.forEach((line, lineIndex) => {
                        const intersects = line.bottom > topEdge + 0.5
                          && line.top < bottomEdge - 0.5;
                        if (!intersects) return;
                        if (line.top < topEdge - 0.5 || line.bottom > bottomEdge + 0.5) {
                          partialLines += 1;
                        }
                        visible.push({
                          id: `${group.order}:${lineIndex}`,
                          group: group.order,
                          kind: group.kind,
                          line: lineIndex,
                          total: lines.length,
                          top: line.top
                        });
                      });
                    }
                    visible.sort((first, second) => first.top - second.top);
                    const last = visible.at(-1);
                    const lastGroupLines = last
                      ? visible.filter((line) => line.group === last.group)
                      : [];
                    const remaining = last ? last.total - last.line - 1 : 0;
                    const ruleViolation = Boolean(last && (
                      last.kind === 'heading'
                      || (last.kind === 'paragraph' && remaining > 0 && (
                        (lastGroupLines.length === 1 && lastGroupLines[0].line === 0)
                        || remaining === 1
                      ))
                    ));
                    return {
                      ids: visible.map((line) => line.id),
                      firstVisibleTop: visible.length ? visible[0].top : null,
                      partialLines,
                      ruleViolation,
                      topMaskHeight: topMask ? topMask.getBoundingClientRect().height : 0
                    };
                  }

                  const pages = [];
                  for (let index = 0; index < pageCount; index += 1) {
                    pages.push(readPage());
                    if (index < pageCount - 1) {
                      nextButton.click();
                      await new Promise((resolve) => requestAnimationFrame(
                        () => requestAnimationFrame(resolve)
                      ));
                    }
                  }
                  let repeatedLines = 0;
                  for (let index = 1; index < pages.length; index += 1) {
                    repeatedLines += pages[index].ids.filter(
                      (line) => pages[index - 1].ids.includes(line)
                    ).length;
                  }
                  const articleBody = article.querySelector('.article-body');
                  const articleParagraphs = articleBody.querySelectorAll('p');
                  const bodyStyle = getComputedStyle(articleBody);
                  const paragraphStyle = getComputedStyle(articleParagraphs[0]);
                  const indentedStyle = getComputedStyle(articleParagraphs[1]);
                  const sectionHeadingStyle = getComputedStyle(
                    article.querySelector('.article-body h2')
                  );
                  return {
                    pageCount,
                    maskBlocksPointer,
                    partialLines: pages.reduce(
                      (total, current) => total + current.partialLines, 0
                    ),
                    repeatedLines,
                    ruleViolations: pages.filter((current) => current.ruleViolation).length,
                    maximumTopMask: Math.max(
                      ...pages.map((current) => current.topMaskHeight)
                    ),
                    finalPageFirstLine: pages.at(-1).firstVisibleTop,
                    typography: {
                      textAlign: bodyStyle.textAlign,
                      hyphens: bodyStyle.hyphens || bodyStyle.webkitHyphens,
                      lineHeightRatio: parseFloat(bodyStyle.lineHeight)
                        / parseFloat(bodyStyle.fontSize),
                      paragraphMargin: parseFloat(paragraphStyle.marginBottom),
                      paragraphIndent: parseFloat(indentedStyle.textIndent),
                      sectionHeadingAlign: sectionHeadingStyle.textAlign
                    }
                  };
                }"""
            )
            page.set_viewport_size(
                {"width": 600, "height": viewport_height + 1}
            )
            page.wait_for_function(
                "document.documentElement.dataset.articlePageIndex === "
                "document.documentElement.dataset.articlePageCount"
            )
            result["keptFinalPageAfterResize"] = True
        finally:
            browser.close()

    assert result["pageCount"] > 3
    assert result["partialLines"] == 0
    assert result["repeatedLines"] == 0
    assert result["ruleViolations"] == 0
    assert result["maskBlocksPointer"] is True
    assert result["maximumTopMask"] <= 10
    assert result["finalPageFirstLine"] <= 12
    assert result["keptFinalPageAfterResize"] is True
    assert result["typography"]["textAlign"] == "justify"
    assert result["typography"]["hyphens"] == "auto"
    assert result["typography"]["lineHeightRatio"] <= 1.5
    assert result["typography"]["paragraphMargin"] == 0
    assert result["typography"]["paragraphIndent"] > 0
    assert result["typography"]["sectionHeadingAlign"] == "left"
