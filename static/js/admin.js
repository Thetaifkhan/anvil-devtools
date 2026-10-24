/* Admin dashboard interactivity.
 *
 * Two responsibilities:
 *   1. The /admin/login form POSTs an access token and redirects on success.
 *   2. The /admin/support/<id> form updates the submission via fetch.
 *
 * Both forms add duplicate-submit protection and surface friendly errors.
 */
  // CSRF: the server embeds a per-session token in a meta tag; echo it back
  // in a custom header on every state-changing request.
  function csrfHeaders() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    if (meta && meta.getAttribute('content')) h['X-CSRF-Token'] = meta.getAttribute('content');
    return h;
  }

(function () {
  'use strict';

  // ── Admin login form ────────────────────────────────────────────────────
  var loginForm = document.getElementById('admin-login-form');
  if (loginForm) {
    var loginBtn = document.getElementById('admin-login-submit');
    var loginStatus = document.getElementById('admin-login-status');
    var tokenField = document.getElementById('admin-access-token');

    function setLoginLoading(loading) {
      if (!loginBtn) return;
      loginBtn.disabled = loading;
      loginBtn.classList.toggle('loading', loading);
      var label = loginBtn.querySelector('.btn-label');
      if (label) {
        label.textContent = loading
          ? (loginBtn.dataset.loadingText || 'Verifying…')
          : 'Verify and sign in';
      }
    }

    function setLoginStatus(text, kind) {
      if (!loginStatus) return;
      loginStatus.textContent = text || '';
      loginStatus.classList.remove('error', 'ok');
      if (kind) loginStatus.classList.add(kind);
    }

    loginForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (loginBtn && loginBtn.disabled) return;
      var token = (tokenField && tokenField.value || '').trim();
      if (!token) {
        setLoginStatus('Please paste an access token.', 'error');
        return;
      }
      setLoginLoading(true);
      setLoginStatus('Verifying…', '');

      fetch('/admin/login', {
        method: 'POST',
        headers: csrfHeaders(),
        body: JSON.stringify({ access_token: token }),
        credentials: 'same-origin',
      }).then(function (resp) {
        return resp.json().then(function (body) {
          return { status: resp.status, body: body };
        });
      }).then(function (result) {
        var body = result.body || {};
        if (body.ok && body.redirect) {
          setLoginStatus('Signed in. Redirecting…', 'ok');
          window.location.href = body.redirect;
        } else if (result.status === 429) {
          setLoginStatus(body.error || 'Too many login attempts.', 'error');
        } else if (result.status === 401) {
          setLoginStatus(body.error || 'Could not verify that token.', 'error');
        } else if (result.status === 403) {
          setLoginStatus(body.error || 'Your account is not configured as an admin.', 'error');
        } else {
          setLoginStatus(body.error || 'Login failed. Please try again.', 'error');
        }
      }).catch(function (err) {
        setLoginStatus('Network error. Please try again.', 'error');
        console.error('admin login failed:', err);
      }).finally(function () {
        setLoginLoading(false);
      });
    });
  }

  // ── Support update form (admin/support_detail) ─────────────────────────
  var updateForm = document.getElementById('support-update-form');
  if (updateForm) {
    var status = updateForm.querySelector('[name=status]');
    var priority = updateForm.querySelector('[name=priority]');
    var notes = updateForm.querySelector('[name=admin_notes]');
    var submitBtn = updateForm.querySelector('button[type=submit]');
    var statusSpan = updateForm.querySelector('.contact-status');
    var endpoint = updateForm.dataset.endpoint;

    function setUpdateLoading(loading) {
      if (!submitBtn) return;
      submitBtn.disabled = loading;
      submitBtn.classList.toggle('loading', loading);
      var label = submitBtn.querySelector('.btn-label') || submitBtn;
      label.textContent = loading ? 'Saving…' : 'Save changes';
    }

    updateForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (submitBtn && submitBtn.disabled) return;
      var payload = {};
      if (status) payload.status = status.value;
      if (priority) payload.priority = priority.value;
      if (notes) payload.admin_notes = notes.value;

      setUpdateLoading(true);
      if (statusSpan) statusSpan.textContent = 'Saving…';

      fetch(endpoint, {
        method: 'POST',
        headers: csrfHeaders(),
        body: JSON.stringify(payload),
        credentials: 'same-origin',
      }).then(function (resp) {
        return resp.json().then(function (body) {
          return { status: resp.status, body: body };
        });
      }).then(function (result) {
        var body = result.body || {};
        if (body.ok) {
          if (statusSpan) {
            statusSpan.textContent = 'Saved.';
            statusSpan.classList.add('ok');
            setTimeout(function () {
              statusSpan.textContent = '';
              statusSpan.classList.remove('ok');
            }, 2000);
          }
        } else {
          if (statusSpan) {
            statusSpan.textContent = body.error || 'Save failed.';
            statusSpan.classList.add('error');
          }
        }
      }).catch(function (err) {
        if (statusSpan) {
          statusSpan.textContent = 'Network error.';
          statusSpan.classList.add('error');
        }
        console.error('admin update failed:', err);
      }).finally(function () {
        setUpdateLoading(false);
      });
    });
  }

  // ── Confirm-on-submit for queue retry buttons ──────────────────────────
  document.querySelectorAll('[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      var msg = form.dataset.confirm || 'Are you sure?';
      if (!window.confirm(msg)) {
        event.preventDefault();
      }
    });
  });
})();
