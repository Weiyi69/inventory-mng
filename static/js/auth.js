(function () {
  const cookieValue = (name) => {
    const prefix = `${name}=`;
    const cookie = document.cookie
      .split("; ")
      .find((item) => item.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  };

  async function refreshSession() {
    const csrfToken = cookieValue("csrf_refresh_token");
    if (!csrfToken) {
      window.location.assign("/login");
      return;
    }
    try {
      const response = await fetch("/auth/refresh", {
        method: "POST",
        headers: { "X-CSRF-TOKEN": csrfToken }
      });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok) {
        throw new Error(`Session refresh failed (${response.status}).`);
      }
      const result = await response.json();
      const csrfMeta = document.querySelector('meta[name="csrf-token"]');
      if (csrfMeta && result.csrf_token) csrfMeta.content = result.csrf_token;
      document.querySelectorAll('input[name="csrf_token"]').forEach((input) => {
        input.value = result.csrf_token;
      });
    } catch (error) {
      console.error(error);
    }
  }

  const refreshWithTabLock = () => {
    if (navigator.locks) {
      navigator.locks.request("retailflow-jwt-refresh", refreshSession);
    } else {
      refreshSession();
    }
  };

  window.setInterval(refreshWithTabLock, 10 * 60 * 1000);
})();
