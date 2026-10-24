(function () {
  const form = document.getElementById("login-form");
  const email = document.getElementById("email");
  const password = document.getElementById("password");

  const submitButton = document.querySelector("[data-submit-button]");
  const submitLabel = document.querySelector("[data-submit-label]");
  const spinner = document.querySelector(".auth-spinner");
  const status = document.querySelector("[data-submit-status]");

  if (
    !form
    || !email
    || !password
    || !submitButton
    || !submitLabel
    || !spinner
    || !status
  ) return;

  const emailError = document.getElementById("email-error");
  const passwordError = document.getElementById("password-error");
  const emailPattern = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  let submitting = false;
  let attemptedSubmit = false;

  function setFieldError(input, messageElement, message) {
    messageElement.textContent = message;
    if (message) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function validateEmail() {
    const value = email.value.trim();
    if (!value) return "Enter your email address.";
    if (value.length > 254 || !emailPattern.test(value)) {
      return "Enter a valid email address.";
    }
    return "";
  }

  function validatePassword() {
    return password.value ? "" : "Enter your password.";
  }

  email.addEventListener("input", () => {
    if (attemptedSubmit) {
      setFieldError(email, emailError, validateEmail());
    }
  });

  password.addEventListener("input", () => {
    if (attemptedSubmit) {
      setFieldError(password, passwordError, validatePassword());
    }
  });

  form.addEventListener("submit", (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }

    attemptedSubmit = true;
    const emailMessage = validateEmail();
    const passwordMessage = validatePassword();
    setFieldError(email, emailError, emailMessage);
    setFieldError(password, passwordError, passwordMessage);
    if (emailMessage || passwordMessage) {
      event.preventDefault();
      (emailMessage ? email : password).focus();
      return;
    }

    submitting = true;
    submitButton.disabled = true;
    submitLabel.textContent = "Signing in";
    spinner.hidden = false;
    status.textContent = "Signing you in. Please wait.";
    status.hidden = false;
  });

  window.addEventListener("pageshow", () => {
    submitting = false;
    submitButton.disabled = false;
    submitLabel.textContent = "Sign in";
    spinner.hidden = true;
    status.textContent = "";
    status.hidden = true;
  });
})();
