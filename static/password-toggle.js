document.addEventListener("DOMContentLoaded", () => {
  const eyeOpen = `
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
      <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="12" cy="12" r="3" stroke-width="2"/>
    </svg>`;
  const eyeClosed = `
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
      <path d="m3 3 18 18M10.6 10.7a2 2 0 0 0 2.7 2.7M9.9 4.3A10.7 10.7 0 0 1 12 4c6 0 9.5 8 9.5 8a16 16 0 0 1-2.1 3M6.2 6.2C3.8 8 2.5 12 2.5 12S6 20 12 20a9.8 9.8 0 0 0 3-.5"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;

  document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;

    button.innerHTML = eyeOpen;
    button.addEventListener("click", () => {
      const willShow = input.type === "password";
      input.type = willShow ? "text" : "password";
      button.innerHTML = willShow ? eyeClosed : eyeOpen;
      button.setAttribute(
        "aria-label",
        willShow ? "Hide password" : "Show password",
      );
      button.setAttribute("aria-pressed", String(willShow));
      input.focus();
    });
  });
});
