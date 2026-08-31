(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;
  if (!root || !doc.querySelector || !doc.addEventListener) return;
  var controls = doc.querySelector(".page-turn-controls, .page-turn-rails");
  var streamControls = controls && controls.getAttribute("data-page-mode") === "stream";
  var sideControls = controls && hasClass(controls, "page-turn-rails");
  var turnButtons = controls ? controls.querySelectorAll("[data-page-turn]") : [];
  var readingProgress = doc.querySelector("[data-reading-progress]");
  var readingProgressFill = readingProgress && readingProgress.querySelector("span");
  var articleEndCue = doc.querySelector("[data-article-end-cue]");
  var articleAdvanceForm = doc.querySelector("[data-article-advance-form]");
  var articleNextPath = articleAdvanceForm && articleAdvanceForm.querySelector("[data-article-next-path]");
  var readingProgressTimer = 0;
  var resizeTimer = 0;
  var lastProgressPercent = -1;
  var pageViewport = 0;
  var pageMaximum = 0;
  var pageStep = 240;
  var streamList = streamControls && doc.querySelector("[data-paged-stream]");
  var streamStatus = streamControls && doc.querySelector("[data-stream-page-status]");
  var streamCards = streamList ? streamList.querySelectorAll("[data-entry-card]") : [];
  var streamPages = [];
  var streamVisibleCards = [];
  var streamPageIndex = 0;
  var streamPageSize = 3;
  var streamGap = 10;
  var streamOffset = streamList ? parseInt(streamList.getAttribute("data-stream-offset"), 10) || 0 : 0;
  setClass(root, "js", true);

  function hasClass(node, className) {
    if (!node) return false;
    if (node.classList) return node.classList.contains(className);
    return (" " + (node.className || "") + " ").indexOf(" " + className + " ") !== -1;
  }

  function setClass(node, className, enabled) {
    if (!node) return;
    if (node.classList) {
      if (enabled) {
        node.classList.add(className);
      } else {
        node.classList.remove(className);
      }
      return;
    }
    var names = (" " + (node.className || "") + " ").replace(/\s+/g, " ");
    var token = " " + className + " ";
    if (enabled && names.indexOf(token) === -1) {
      names += className + " ";
    } else if (!enabled) {
      names = names.replace(token, " ");
    }
    node.className = names.replace(/^\s+|\s+$/g, "");
  }

  function containsItem(items, item) {
    var index;
    for (index = 0; index < items.length; index += 1) {
      if (items[index] === item) return true;
    }
    return false;
  }

  function isHidden(node) {
    return !node || node.getAttribute("hidden") !== null;
  }

  function setHidden(node, hidden) {
    if (!node) return;
    if (hidden) {
      node.setAttribute("hidden", "hidden");
    } else {
      node.removeAttribute("hidden");
    }
  }

  function supportsNativeDetails() {
    if (!doc.body || !doc.createElement) return false;
    var details = doc.createElement("details");
    var summary = doc.createElement("summary");
    var content = doc.createElement("div");
    summary.appendChild(doc.createTextNode("Menu"));
    content.appendChild(doc.createTextNode("Items"));
    details.appendChild(summary);
    details.appendChild(content);
    details.style.position = "absolute";
    details.style.left = "-9999px";
    details.style.visibility = "hidden";
    doc.body.appendChild(details);
    var closedHeight = details.offsetHeight;
    details.setAttribute("open", "open");
    var supported = details.offsetHeight > closedHeight;
    doc.body.removeChild(details);
    return supported;
  }

  function setupReaderMenu() {
    var menu = doc.querySelector(".reader-menu");
    if (!menu) return;
    if (supportsNativeDetails()) {
      setClass(root, "has-native-details", true);
      return;
    }

    var summary = menu.querySelector(".reader-menu-summary");
    var panel = menu.querySelector(".reader-menu-panel");
    if (!summary || !panel) return;
    summary.setAttribute("role", "button");
    summary.setAttribute("tabindex", "0");
    summary.setAttribute("aria-expanded", "false");
    setHidden(panel, true);

    function toggleMenu(event) {
      var opening = isHidden(panel);
      if (event && event.preventDefault) event.preventDefault();
      if (opening) {
        menu.setAttribute("open", "open");
      } else {
        menu.removeAttribute("open");
      }
      setHidden(panel, !opening);
      summary.setAttribute("aria-expanded", opening ? "true" : "false");
    }

    summary.addEventListener("click", toggleMenu, false);
    summary.addEventListener("keydown", function (event) {
      var keyCode = event.keyCode || event.which || 0;
      if (event.key === "Enter" || event.key === " " || keyCode === 13 || keyCode === 32) {
        toggleMenu(event);
      }
    }, false);
  }

  function findWithAttribute(node, attribute) {
    while (node && node !== doc) {
      if (node.getAttribute && node.hasAttribute(attribute)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function submitNormally(form, url, action) {
    if (form.getAttribute("data-state-form") !== null) {
      var fallbackAction = form.querySelector("[data-state-action-fallback]");
      if (!fallbackAction) {
        fallbackAction = doc.createElement("input");
        fallbackAction.type = "hidden";
        fallbackAction.name = "state_action";
        fallbackAction.setAttribute("data-state-action-fallback", "");
        form.appendChild(fallbackAction);
      }
      fallbackAction.value = action;
    }
    form.setAttribute("action", url);
    form.submit();
  }

  function isInteractiveTarget(node) {
    while (node && node !== doc) {
      var tagName = node.tagName ? node.tagName.toLowerCase() : "";
      if (tagName === "a" || tagName === "button" || tagName === "summary"
          || tagName === "input" || tagName === "textarea" || tagName === "select"
          || node.isContentEditable) return true;
      node = node.parentNode;
    }
    return false;
  }

  function canReturnThroughHistory(link) {
    if (!link || !doc.referrer || !window.history || window.history.length < 2) return false;
    var referrer = doc.createElement("a");
    var destination = doc.createElement("a");
    referrer.href = doc.referrer;
    destination.href = link.href;
    return referrer.protocol === destination.protocol
      && referrer.host === destination.host
      && referrer.pathname === destination.pathname
      && referrer.search === destination.search;
  }

  function isPlainClick(event) {
    return !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
      && (typeof event.button === "undefined" || event.button === 0);
  }

  function rememberArticlePosition(link) {
    if (!link || !window.history || !window.history.replaceState) return;
    var card = findWithAttribute(link, "data-entry-card");
    if (!card || !card.id) return;
    try {
      window.history.replaceState(
        null,
        doc.title,
        window.location.pathname + window.location.search + "#" + card.id
      );
    } catch (error) {
      // The href still carries the same anchor when History API updates are unavailable.
    }
  }

  function removeCard(button) {
    var card = findWithAttribute(button, "data-entry-card");
    if (!card || !card.parentNode) return false;
    var list = card.parentNode;
    list.removeChild(card);
    if (!list.querySelector("[data-entry-card]")) {
      var empty = doc.createElement("p");
      empty.className = "empty-state";
      empty.textContent = list.getAttribute("data-empty-message") || "No items in this view.";
      list.parentNode.replaceChild(empty, list);
    }
    return true;
  }

  function finishReadAction(button, action, actionUrl) {
    var card = findWithAttribute(button, "data-entry-card");
    var list = card && card.parentNode;
    if (!card || !list) return false;

    var isRead = action === "read";
    if (isRead && !list.hasAttribute("data-preserve-read-items")) {
      return removeCard(button);
    }

    var nextAction = isRead ? "unread" : "read";
    var nextUrl = actionUrl.replace(/\/(?:un)?read$/, "/" + nextAction);
    var readState = card.querySelector("[data-read-state]");
    var readStateSeparator = card.querySelector("[data-read-state-separator]");
    setClass(card, "is-read", isRead);
    setClass(button, "is-active", isRead);
    setHidden(readState, !isRead);
    setHidden(readStateSeparator, !isRead);
    button.setAttribute("data-quick-action", nextAction);
    button.setAttribute("value", nextAction);
    if (button.form && button.form.getAttribute("data-state-form") === null) {
      button.form.setAttribute("action", nextUrl);
    }
    button.setAttribute(
      "aria-label",
      button.getAttribute("aria-label").replace(/ as (?:un)?read$/, " as " + nextAction)
    );
    button.setAttribute("title", "Mark as " + nextAction);
    button.textContent = isRead ? "↶" : "✓";
    return false;
  }

  function finishQuickAction(button, action, actionUrl) {
    var card = findWithAttribute(button, "data-entry-card");
    var list = card && card.parentNode;
    if (action === "read" || action === "unread") {
      return finishReadAction(button, action, actionUrl);
    }

    if (action === "unstar" && list && list.hasAttribute("data-starred-view")) {
      return removeCard(button);
    }

    var active = action === "star";
    var nextAction = active ? "unstar" : "star";
    var nextUrl = actionUrl.replace(/\/(?:un)?star$/, "/" + nextAction);
    setClass(button, "is-active", active);
    button.setAttribute("data-quick-action", nextAction);
    button.setAttribute("value", nextAction);
    if (button.form && button.form.getAttribute("data-state-form") === null) {
      button.form.setAttribute("action", nextUrl);
    }
    button.setAttribute("aria-label", button.getAttribute("aria-label").replace(/^(Star|Unstar)/, active ? "Unstar" : "Star"));
    button.textContent = hasClass(button, "article-star-button")
      ? (active ? "★ Unstar" : "☆ Star")
      : (active ? "★" : "☆");
    return false;
  }

  function pageReserve(viewport) {
    if (!controls) return 24;
    if (sideControls) return 24;
    return Math.min(viewport - 240, controls.offsetHeight + 24);
  }

  function buildStreamPages(showLastPage) {
    if (!streamList) return;
    streamCards = streamList.querySelectorAll("[data-entry-card]");
    streamPages = [];
    var current = window.pageYOffset || root.scrollTop || 0;
    var listTop = streamList.getBoundingClientRect().top + current;
    var available = Math.max(240, (root.clientHeight || window.innerHeight) - listTop - 32);
    var cardHeight = Math.max(72, Math.floor((available - (streamGap * (streamPageSize - 1))) / streamPageSize));
    var page = [];
    var index;
    var card;
    for (index = 0; index < streamCards.length; index += 1) {
      card = streamCards[index];
      card.style.height = cardHeight + "px";
      if (page.length >= streamPageSize) {
        streamPages.push(page);
        page = [];
      }
      page.push(card);
    }
    if (page.length) streamPages.push(page);
    streamPageIndex = showLastPage
      ? Math.max(0, streamPages.length - 1)
      : Math.min(streamPageIndex, Math.max(0, streamPages.length - 1));
  }

  function restoreStreamPageFromAnchor() {
    if (!streamList || window.location.hash.indexOf("#entry-") !== 0) return;
    var target = doc.getElementById(window.location.hash.slice(1));
    var pageIndex;
    if (!target) return;
    for (pageIndex = 0; pageIndex < streamPages.length; pageIndex += 1) {
      if (containsItem(streamPages[pageIndex], target)) {
        streamPageIndex = pageIndex;
        return;
      }
    }
  }

  function showStreamPage() {
    var index;
    var firstArticleIndex = 0;
    var visiblePage = streamPages[streamPageIndex] || [];
    for (index = 0; index < streamVisibleCards.length; index += 1) {
      if (!containsItem(visiblePage, streamVisibleCards[index])) {
        setClass(streamVisibleCards[index], "is-stream-page-visible", false);
      }
    }
    for (index = 0; index < visiblePage.length; index += 1) {
      if (!containsItem(streamVisibleCards, visiblePage[index])) {
        setClass(visiblePage[index], "is-stream-page-visible", true);
      }
    }
    streamVisibleCards = visiblePage;
    if (streamStatus && visiblePage.length) {
      setHidden(streamStatus, false);
      for (index = 0; index < streamPageIndex; index += 1) {
        firstArticleIndex += streamPages[index].length;
      }
      var firstArticle = streamOffset + firstArticleIndex + 1;
      var lastArticle = firstArticle + visiblePage.length - 1;
      var articleRange = firstArticle === lastArticle
        ? String(firstArticle)
        : firstArticle + "–" + lastArticle;
      var knownTotal = streamOffset + streamCards.length;
      var currentPage = Math.floor(streamOffset / streamPageSize) + streamPageIndex + 1;
      var knownPages = Math.ceil(knownTotal / streamPageSize);
      var hasMore = Boolean(adjacentUrl(1));
      streamStatus.textContent = "Page " + currentPage + "/" + knownPages + (hasMore ? "+" : "")
        + " · " + articleRange + "/" + knownTotal + (hasMore ? "+" : "");
      streamStatus.setAttribute(
        "aria-label",
        "Page " + currentPage + " of " + knownPages + (hasMore ? " or more" : "")
          + ", articles " + articleRange + " of " + knownTotal + (hasMore ? " or more" : "")
      );
    } else if (streamStatus) {
      streamStatus.textContent = "";
      streamStatus.removeAttribute("aria-label");
      setHidden(streamStatus, true);
    }
    if ((window.pageYOffset || root.scrollTop || 0) !== 0) window.scrollTo(0, 0);
  }

  function measurePage() {
    pageViewport = root.clientHeight || window.innerHeight;
    pageMaximum = Math.max(0, root.scrollHeight - pageViewport);
    pageStep = Math.max(240, pageViewport - pageReserve(pageViewport));
  }

  function adjacentUrl(direction) {
    if (!controls) return "";
    return controls.getAttribute(direction > 0 ? "data-page-next-url" : "data-page-previous-url") || "";
  }

  function updateTurnButtons() {
    if (!controls || isHidden(controls)) return;
    var current = window.pageYOffset || root.scrollTop || 0;
    var index;
    var direction;
    for (index = 0; index < turnButtons.length; index += 1) {
      direction = parseInt(turnButtons[index].getAttribute("data-page-turn"), 10);
      var disabled;
      if (streamControls) {
        disabled = direction > 0
          ? streamPageIndex >= streamPages.length - 1 && !adjacentUrl(direction)
          : streamPageIndex <= 0 && !adjacentUrl(direction);
      } else {
        disabled = direction > 0
          ? current >= pageMaximum - 1 && !adjacentUrl(direction)
          : current <= 0 && !adjacentUrl(direction);
      }
      if (turnButtons[index].disabled !== disabled) turnButtons[index].disabled = disabled;
    }
  }

  function updateArticleEndCue() {
    if (!articleEndCue || streamControls) return;
    var current = window.pageYOffset || root.scrollTop || 0;
    var hidden = current < pageMaximum - 1;
    if (isHidden(articleEndCue) !== hidden) setHidden(articleEndCue, hidden);
  }

  function updatePageControls() {
    if (!controls) return;
    if (streamControls) {
      var hasStreamPages = streamPages.length > 1;
      var hasAdjacentStreamPage = Boolean(adjacentUrl(-1) || adjacentUrl(1));
      var hideStreamControls = !(hasStreamPages || hasAdjacentStreamPage);
      setHidden(controls, hideStreamControls);
      setClass(root, "has-page-turn-rails", !hideStreamControls && sideControls);
      setClass(root, "has-page-turn-gutter", false);
      updateTurnButtons();
      return;
    }
    measurePage();
    var hasAdjacentPage = Boolean(adjacentUrl(-1) || adjacentUrl(1));
    var hasOverflow = pageMaximum > 24;
    var hidePageControls = !(hasOverflow || hasAdjacentPage);
    setHidden(controls, hidePageControls);
    setClass(root, "has-page-turns", !hidePageControls && !sideControls);
    setClass(root, "has-page-turn-rails", !hidePageControls && sideControls);
    // Side rails narrow the article and can add wrapped lines.
    if (!hidePageControls) measurePage();
    updateTurnButtons();
    updateArticleEndCue();
  }

  function restoreArticleEnd() {
    if (streamControls || window.location.hash !== "#end") return;
    measurePage();
    if ((window.pageYOffset || root.scrollTop || 0) !== pageMaximum) {
      window.scrollTo(0, pageMaximum);
    }
    updateTurnButtons();
  }

  function updateReadingProgress() {
    if (!readingProgressFill) return;
    if (readingProgressTimer) {
      window.clearTimeout(readingProgressTimer);
      readingProgressTimer = 0;
    }
    var current = window.pageYOffset || root.scrollTop || 0;
    var progress = pageMaximum ? Math.min(1, current / pageMaximum) : 1;
    var progressPercent = Math.round(progress * 100);
    if (progressPercent !== lastProgressPercent) {
      var progressTransform = "scaleX(" + (progressPercent / 100) + ")";
      readingProgressFill.style.webkitTransform = progressTransform;
      readingProgressFill.style.transform = progressTransform;
      readingProgress.setAttribute("aria-valuenow", String(progressPercent));
      lastProgressPercent = progressPercent;
    }
    updateArticleEndCue();
    readingProgressTimer = 0;
  }

  function advanceToArticle(nextUrl) {
    if (!articleAdvanceForm || !articleNextPath) {
      window.location.href = nextUrl;
      return;
    }
    articleNextPath.value = nextUrl;
    articleAdvanceForm.submit();
  }

  function scheduleReadingProgress() {
    if (!readingProgressFill) return;
    if (readingProgressTimer) window.clearTimeout(readingProgressTimer);
    readingProgressTimer = window.setTimeout(updateReadingProgress, 120);
  }

  function pageTurn(direction) {
    if (streamControls) {
      var streamUrl = adjacentUrl(direction);
      if (direction > 0 && streamPageIndex < streamPages.length - 1) {
        streamPageIndex += 1;
        showStreamPage();
        updateTurnButtons();
        return;
      }
      if (direction < 0 && streamPageIndex > 0) {
        streamPageIndex -= 1;
        showStreamPage();
        updateTurnButtons();
        return;
      }
      if (streamUrl) {
        window.location.href = direction < 0
          ? streamUrl.replace(/#.*$/, "") + "#end"
          : streamUrl.replace(/#.*$/, "");
      }
      return;
    }
    measurePage();
    var current = window.pageYOffset || root.scrollTop || 0;
    var nextUrl = adjacentUrl(direction);
    var target;
    if (nextUrl && ((direction > 0 && current >= pageMaximum - 1) || (direction < 0 && current <= 0))) {
      if (direction > 0) {
        advanceToArticle(nextUrl);
      } else {
        window.location.href = nextUrl.replace(/#.*$/, "") + "#end";
      }
      return;
    }
    target = Math.max(0, Math.min(pageMaximum, current + (direction * pageStep)));
    window.scrollTo(0, target);
    window.setTimeout(function () {
      updateTurnButtons();
      updateReadingProgress();
    }, 0);
  }

  setupReaderMenu();
  if (streamControls) {
    buildStreamPages(window.location.hash === "#end");
    restoreStreamPageFromAnchor();
    showStreamPage();
  }
  updatePageControls();
  restoreArticleEnd();
  updateReadingProgress();
  if (controls || readingProgressFill) {
    window.addEventListener("resize", function () {
      if (resizeTimer) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(function () {
        if (streamControls) {
          buildStreamPages(false);
          showStreamPage();
        }
        updatePageControls();
        scheduleReadingProgress();
        resizeTimer = 0;
      }, 120);
    }, false);
  }
  if (readingProgressFill) {
    window.addEventListener("scroll", scheduleReadingProgress, false);
  }

  doc.addEventListener("keydown", function (event) {
    if (!controls) return;
    var target = event.target;
    var keyCode = event.keyCode || event.which || 0;
    var interactiveTarget = isInteractiveTarget(target);
    var pageDown = event.key === "PageDown" || keyCode === 34;
    var pageUp = event.key === "PageUp" || keyCode === 33;
    var arrowRight = event.key === "ArrowRight" || keyCode === 39;
    var arrowLeft = event.key === "ArrowLeft" || keyCode === 37;
    if (pageDown || (arrowRight && !interactiveTarget)) {
      event.preventDefault();
      pageTurn(1);
    } else if (pageUp || (arrowLeft && !interactiveTarget)) {
      event.preventDefault();
      pageTurn(-1);
    }
  }, false);

  doc.addEventListener("click", function (event) {
    var plainClick = isPlainClick(event);
    var articleLink = findWithAttribute(event.target, "data-open-article");
    if (plainClick && articleLink) rememberArticlePosition(articleLink);

    var closeLink = findWithAttribute(event.target, "data-close-article");
    if (closeLink) {
      if (plainClick && canReturnThroughHistory(closeLink)) {
        event.preventDefault();
        window.history.back();
        return;
      }
    }

    var turnButton = findWithAttribute(event.target, "data-page-turn");
    if (turnButton) {
      event.preventDefault();
      if (event.detail && turnButton.blur) turnButton.blur();
      pageTurn(parseInt(turnButton.getAttribute("data-page-turn"), 10));
      return;
    }

    var actionButton = findWithAttribute(event.target, "data-quick-action");
    if (!actionButton || !window.fetch || !window.FormData) return;

    var form = actionButton.form;
    if (!form) return;
    var action = actionButton.getAttribute("data-quick-action");
    var url = form.getAttribute("action") || form.action;
    event.preventDefault();
    actionButton.disabled = true;

    var actionData = new FormData(form);
    if (form.getAttribute("data-state-form") !== null) {
      actionData.append("state_action", action);
    }
    window.fetch(url, {
      method: "POST",
      body: actionData,
      credentials: "same-origin",
      headers: {"X-RSS-Kindle-Action": "1"}
    }).then(function (response) {
      if (!response.ok) {
        submitNormally(form, url, action);
        return;
      }
      var streamLayoutChanged = finishQuickAction(actionButton, action, url);
      actionButton.disabled = false;
      if (streamControls && streamLayoutChanged) {
        buildStreamPages(false);
        showStreamPage();
      }
      updatePageControls();
    }, function () {
      submitNormally(form, url, action);
    });
  }, false);
}());
