(function () {
  document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
    const inputId = toggle.getAttribute("aria-controls");
    const input = inputId ? document.getElementById(inputId) : null;
    if (!input) return;

    const syncVisibility = () => {
      const isVisible = input.type === "text";
      toggle.setAttribute("aria-pressed", String(isVisible));
      toggle.setAttribute(
        "aria-label",
        isVisible ? "Hide password" : "Show password",
      );
    };

    syncVisibility();
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      input.type = input.type === "password" ? "text" : "password";
      syncVisibility();
    });

    new MutationObserver(syncVisibility).observe(input, {
      attributes: true,
      attributeFilter: ["type"],
    });
  });
})();
