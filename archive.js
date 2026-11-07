(function () {
  const details = document.querySelectorAll("#archive details");

  details.forEach(function (detail) {
    detail.addEventListener("toggle", function (event) {
      if (detail.open) {
        detail.querySelector("summary").scrollIntoView();
      }
    });
  });
})();
