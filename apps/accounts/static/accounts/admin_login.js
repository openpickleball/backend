// OPEN's admin login (issue #101). The page takes no password itself:
// this script signs in through allauth's headless browser client, with
// the password and then a code from the authenticator app, or, in code
// mode, confirms a code for a signed-in session. On success it opens the
// page the server chose (data-next, already checked to stay on this
// site). Messages are fixed text and never repeat what was typed.

const MESSAGES = {
  credentials: "The email or password is wrong.",
  code: "The code is wrong. Try the next code from your app.",
  verify: "Verify your email address first, from the email OPEN sent you.",
  throttled: "Too many attempts. Wait a few minutes, then try again.",
  failed: "Signing in did not work. Reload the page and try again.",
};

const form = document.getElementById("open-admin-login");
const errorBox = document.getElementById("open-admin-error");
const codeRow = document.getElementById("open-admin-code-row");
const codeInput = document.getElementById("open-admin-code");

const base = form.dataset.base;
const next = form.dataset.next;
let mode = form.dataset.mode;

function cookie(name) {
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) {
      return decodeURIComponent(rest.join("="));
    }
  }
  return "";
}

function csrfToken() {
  // The cookie holds the current token (a login rotates it); the form's
  // hidden field is the fallback.
  return (
    cookie(form.dataset.csrfCookie) ||
    form.querySelector("input[name=csrfmiddlewaretoken]").value
  );
}

function showError(key) {
  errorBox.textContent = MESSAGES[key];
  errorBox.hidden = false;
}

async function send(endpoint, body) {
  const response = await fetch(`${base}/${endpoint}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    },
    body: JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  return { status: response.status, payload };
}

function hasErrorCode(payload, code) {
  const errors = payload?.errors ?? [];
  return errors.some((error) => error.code === code);
}

function pendingFlow(payload) {
  const flows = payload?.data?.flows ?? [];
  const pending = flows.find((flow) => flow.is_pending);
  return pending ? pending.id : null;
}

function showCodeStep() {
  mode = "mfa";
  for (const id of ["open-admin-credentials", "open-admin-password-row"]) {
    const row = document.getElementById(id);
    if (row) {
      row.hidden = true;
    }
  }
  codeRow.hidden = false;
  codeInput.required = true;
  codeInput.focus();
}

function done() {
  window.location.assign(next);
}

async function logIn() {
  const email = document.getElementById("open-admin-email").value;
  const password = document.getElementById("open-admin-password").value;
  const { status, payload } = await send("auth/login", { email, password });
  if (status === 200 || status === 409) {
    // Signed in (or already): the server decides what the session may see.
    done();
  } else if (status === 401 && pendingFlow(payload) === "mfa_authenticate") {
    showCodeStep();
  } else if (status === 401 && pendingFlow(payload) === "verify_email") {
    showError("verify");
  } else if (status === 400 && hasErrorCode(payload, "too_many_login_attempts")) {
    // allauth's lockout after failed attempts answers 400, like a wrong
    // password, with its own code. OPEN's login view turns it into a 429
    // (#100); this branch stays in case it does not.
    showError("throttled");
  } else if (status === 400) {
    showError("credentials");
  } else if (status === 429) {
    showError("throttled");
  } else {
    showError("failed");
  }
}

async function confirmCode(endpoint) {
  const code = codeInput.value.trim();
  const { status, payload } = await send(endpoint, { code });
  if (status === 200) {
    done();
  } else if (status === 400 && hasErrorCode(payload, "too_many_login_attempts")) {
    // allauth's lockout after failed codes answers 400, like a wrong
    // code, with its own code. OPEN's views turn it into a 429 (#100);
    // this branch stays in case they do not.
    showError("throttled");
  } else if (status === 400) {
    codeInput.value = "";
    showError("code");
  } else if (status === 429) {
    showError("throttled");
  } else {
    showError("failed");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  const button = form.querySelector("input[type=submit]");
  button.disabled = true;
  try {
    if (mode === "login") {
      await logIn();
    } else if (mode === "mfa") {
      await confirmCode("auth/2fa/authenticate");
    } else {
      await confirmCode("auth/2fa/reauthenticate");
    }
  } catch {
    showError("failed");
  } finally {
    button.disabled = false;
  }
});
