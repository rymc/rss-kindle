(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;
  var controls = doc.querySelector(".page-turn-controls, .page-turn-rails");
  var streamControls = controls && controls.getAttribute("data-page-mode") === "stream";
  var sideControls = controls && controls.classList.contains("page-turn-rails");
  var turnButtons = controls ? controls.querySelectorAll("[data-page-turn]") : [];
  var readingProgress = doc.querySelector("[data-reading-progress]");
  var readingProgressFill = readingProgress && readingProgress.querySelector("span");
  var articleEndCue = doc.querySelector("[data-article-end-cue]");
  var articleAdvanceForm = doc.querySelector("[data-article-advance-form]");
  var articleNextPath = articleAdvanceForm && articleAdvanceForm.querySelector("[data-article-next-path]");
  var readingProgressFrame = 0;
  var pageViewport = 0;
  var pageMaximum = 0;
  var pageStep = 240;
  var streamList = streamControls && doc.querySelector("[data-paged-stream]");
  var streamStatus = streamControls && doc.querySelector("[data-stream-page-status]");
  var streamCards = streamList ? streamList.querySelectorAll("[data-entry-card]") : [];
  var streamPages = [];
  var streamPageIndex = 0;
  var streamPageSize = 3;
  var streamGap = 10;
  var streamOffset = streamList ? parseInt(streamList.getAttribute("data-stream-offset"), 10) || 0 : 0;
  root.className += " js";

  function findWithAttribute(node, attribute) {
    while (node && node !== doc) {
      if (node.getAttribute && node.hasAttribute(attribute)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function submitNormally(form, url) {
    form.action = url;
    form.submit();
  }

  function removeCard(button) {
    var card = findWithAttribute(button, "data-entry-card");
    if (!card || !card.parentNode) return;
    var list = card.parentNode;
    list.removeChild(card);
    if (!list.querySelector("[data-entry-card]")) {
      var empty = doc.createElement("p");
      empty.className = "empty-state";
      empty.textContent = list.getAttribute("data-empty-message") || "No items in this view.";
      list.parentNode.replaceChild(empty, list);
    }
  }

  function finishQuickAction(button, action, actionUrl) {
    var card = findWithAttribute(button, "data-entry-card");
    var list = card && card.parentNode;
    if (action === "read") {
      if (button.form.hasAttribute("data-keep-after-read")) {
        button.parentNode.removeChild(button);
      } else {
        removeCard(button);
      }
      return;
    }

    if (action === "unstar" && list && list.hasAttribute("data-starred-view")) {
      removeCard(button);
      return;
    }

    var active = action === "star";
    var nextAction = active ? "unstar" : "star";
    var nextUrl = actionUrl.replace(/\/(?:un)?star$/, "/" + nextAction);
    button.classList.toggle("is-active", active);
    button.setAttribute("data-quick-action", nextAction);
    button.setAttribute("formaction", nextUrl);
    button.setAttribute("aria-label", button.getAttribute("aria-label").replace(/^(Star|Unstar)/, active ? "Unstar" : "Star"));
    button.textContent = button.classList.contains("article-star-button")
      ? (active ? "★ Unstar" : "☆ Star")
      : (active ? "★" : "☆");
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

  function showStreamPage() {
    var index;
    var firstArticleIndex = 0;
    var visiblePage = streamPages[streamPageIndex] || [];
    for (index = 0; index < streamCards.length; index += 1) {
      streamCards[index].classList.remove("is-stream-page-visible");
    }
    for (index = 0; index < visiblePage.length; index += 1) {
      visiblePage[index].classList.add("is-stream-page-visible");
    }
    if (streamStatus && visiblePage.length) {
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
        + " · " + articleRange + "/" + knownTotal + (adjacentUrl(1) ? "+" : "");
      streamStatus.setAttribute(
        "aria-label",
        "Page " + currentPage + " of " + knownPages + (hasMore ? " or more" : "")
          + ", articles " + articleRange + " of " + knownTotal + (hasMore ? " or more" : "")
      );
    }
    window.scrollTo(0, 0);
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
    if (!controls || controls.hidden) return;
    var current = window.pageYOffset || root.scrollTop || 0;
    var index;
    var direction;
    for (index = 0; index < turnButtons.length; index += 1) {
      direction = parseInt(turnButtons[index].getAttribute("data-page-turn"), 10);
      if (streamControls) {
        turnButtons[index].disabled = direction > 0
          ? streamPageIndex >= streamPages.length - 1 && !adjacentUrl(direction)
          : streamPageIndex <= 0 && !adjacentUrl(direction);
      } else {
        turnButtons[index].disabled = direction > 0
          ? current >= pageMaximum - 1 && !adjacentUrl(direction)
          : current <= 0 && !adjacentUrl(direction);
      }
    }
  }

  function updateArticleEndCue() {
    if (!articleEndCue || streamControls) return;
    var current = window.pageYOffset || root.scrollTop || 0;
    articleEndCue.hidden = current < pageMaximum - 1;
  }

  function updatePageControls() {
    if (!controls) return;
    if (streamControls) {
      var hasStreamPages = streamPages.length > 1;
      var hasAdjacentStreamPage = Boolean(adjacentUrl(-1) || adjacentUrl(1));
      controls.hidden = !(hasStreamPages || hasAdjacentStreamPage);
      root.classList.toggle("has-page-turn-rails", !controls.hidden && sideControls);
      updateTurnButtons();
      return;
    }
    measurePage();
    var hasAdjacentPage = Boolean(adjacentUrl(-1) || adjacentUrl(1));
    var hasOverflow = pageMaximum > 24;
    controls.hidden = !(hasOverflow || hasAdjacentPage);
    root.classList.toggle("has-page-turns", !controls.hidden && !sideControls);
    root.classList.toggle("has-page-turn-rails", !controls.hidden && sideControls);
    updateTurnButtons();
    updateArticleEndCue();
  }

  function updateReadingProgress() {
    if (!readingProgressFill) return;
    var current = window.pageYOffset || root.scrollTop || 0;
    var progress = pageMaximum ? Math.min(1, current / pageMaximum) : 1;
    readingProgressFill.style.transform = "scaleX(" + progress + ")";
    readingProgress.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
    updateArticleEndCue();
    readingProgressFrame = 0;
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
    if (!readingProgressFill || readingProgressFrame) return;
    if (window.requestAnimationFrame) {
      readingProgressFrame = window.requestAnimationFrame(updateReadingProgress);
    } else {
      updateReadingProgress();
    }
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
    var current = window.pageYOffset || root.scrollTop || 0;
    var nextUrl = adjacentUrl(direction);
    var target;
    if (nextUrl && ((direction > 0 && current >= pageMaximum - 1) || (direction < 0 && current <= 0))) {
      if (direction > 0) {
        advanceToArticle(nextUrl);
      } else {
        window.location.href = nextUrl;
      }
      return;
    }
    target = Math.max(0, Math.min(pageMaximum, current + (direction * pageStep)));
    window.scrollTo(0, target);
    window.setTimeout(function () {
      updateTurnButtons();
      updateArticleEndCue();
    }, 0);
  }

  if (streamControls) {
    buildStreamPages(window.location.hash === "#end");
    showStreamPage();
  }
  updatePageControls();
  updateReadingProgress();
  window.addEventListener("resize", function () {
    if (streamControls) {
      buildStreamPages(false);
      showStreamPage();
    }
    updatePageControls();
    scheduleReadingProgress();
  }, false);
  window.addEventListener("scroll", scheduleReadingProgress, false);

  doc.addEventListener("keydown", function (event) {
    var target = event.target;
    var tagName = target && target.tagName ? target.tagName.toLowerCase() : "";
    if (tagName === "input" || tagName === "textarea" || tagName === "select") return;
    if (event.key === "PageDown" || event.key === "ArrowRight") {
      event.preventDefault();
      pageTurn(1);
    } else if (event.key === "PageUp" || event.key === "ArrowLeft") {
      event.preventDefault();
      pageTurn(-1);
    }
  }, false);

  doc.addEventListener("click", function (event) {
    var homeLink = findWithAttribute(event.target, "data-home-link");
    if (homeLink && homeLink.blur) homeLink.blur();

    var turnButton = findWithAttribute(event.target, "data-page-turn");
    if (turnButton) {
      event.preventDefault();
      if (turnButton.blur) turnButton.blur();
      pageTurn(parseInt(turnButton.getAttribute("data-page-turn"), 10));
      return;
    }

    var actionButton = findWithAttribute(event.target, "data-quick-action");
    if (!actionButton || !window.fetch || !window.FormData) return;

    var form = actionButton.form;
    var action = actionButton.getAttribute("data-quick-action");
    var url = actionButton.getAttribute("formaction") || form.action;
    event.preventDefault();
    actionButton.disabled = true;

    window.fetch(url, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: {"X-RSS-Kindle-Action": "1"}
    }).then(function (response) {
      if (!response.ok) {
        submitNormally(form, url);
        return;
      }
      finishQuickAction(actionButton, action, url);
      actionButton.disabled = false;
      if (streamControls) {
        buildStreamPages(false);
        showStreamPage();
      }
      updatePageControls();
    }, function () {
      submitNormally(form, url);
    });
  }, false);
}());
