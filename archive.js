(function () {
  const input = document.getElementById("archive-search");
  const countEl = document.getElementById("archive-result-count");
  if (!input || !countEl) return;

  const articles = document.querySelectorAll("article[data-search]");
  const total = articles.length;
  const details = document.querySelectorAll("#archive details");

  details.forEach(function (detail) {
    detail.addEventListener("toggle", function (event) {
      if (detail.open) {
        detail.querySelector("summary").scrollIntoView();
      }
    });
  });

  input.addEventListener("input", function () {
    const query = input.value.toLowerCase().trim();
    let visible = 0;

    for (let i = 0; i < articles.length; i++) {
      const match =
        !query || articles[i].getAttribute("data-search").indexOf(query) !== -1;
      articles[i].hidden = !match;
      if (match) visible++;
    }

    for (let i = 0; i < details.length; i++) {
      const span = details[i].querySelector(".month-count");
      if (!span) continue;
      const monthVisible = details[i].querySelectorAll(
        "article:not([hidden])",
      ).length;
      span.textContent = query ? monthVisible : span.getAttribute("data-total");
    }

    countEl.textContent = query
      ? "Showing " + visible + " of " + total + " posts"
      : "";
  });
})();
