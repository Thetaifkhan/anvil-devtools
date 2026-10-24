/* Contact form: client-side validation, duplicate-submit protection,
 * idempotency key, reference-code display, accessibility.
 *
 * The form never blocks on Supabase — the server queues the submission
 * in the durable outbox and returns 202 with a reference code. If the
 * network fails mid-submit, the same idempotency key is reused on the
 * next attempt so the server treats it as a duplicate (no second row).
 */
(function () {
  'use strict';

  var form = document.getElementById('contact-form');
  if (!form) return;

  var submitBtn = document.getElementById('contact-submit');
  var statusEl = document.getElementById('contact-status');
  var resultEl = document.getElementById('contact-result');
  var refEl = document.getElementById('result-reference');
  var metaEl = document.getElementById('result-meta');
  var resetBtn = document.getElementById('result-reset');
  var pageInput = document.getElementById('contact-page');

  // Pre-fill the page URL with the URL the user came from (Referrer) —
  // we trust it because we only ever store it as a length-bounded TEXT
  // and never as a clickable link in the admin UI.
  if (pageInput && !pageInput.value) {
    var ref = document.referrer || window.location.href;
    if (ref && ref.length <= 2048) pageInput.value = ref;
  }

  // Generate an idempotency key per page load. If the user double-clicks
  // submit, both clicks send the same key; if they retry after a network
  // error, we reuse the same key so no duplicate row is created.
  var idempotencyKey = generateIdempotencyKey();
  // Hidden input — read by the server side.
  var keyInput = document.createElement('input');
  keyInput.type = 'hidden';
  keyInput.name = 'idempotency_key';
  keyInput.value = idempotencyKey;
  form.appendChild(keyInput);

  // Character counters
  document.querySelectorAll('[data-counter-for]').forEach(function (counter) {
    var fieldName = counter.getAttribute('data-counter-for');
    var field = document.getElementById('contact-' + fieldName);
    if (!field) return;
    var countEl = counter.querySelector('[data-count]');
    function update() {
      var len = (field.value || '').length;
      if (countEl) countEl.textContent = String(len);
      var max = parseInt(field.getAttribute('maxlength') || '0', 10);
      if (max && len > max * 0.9) {
        counter.style.color = len >= max ? 'var(--bad)' : 'var(--warn)';
      } else {
        counter.style.color = '';
      }
    }
    field.addEventListener('input', update);
    update();
  });

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('error', 'ok');
    if (kind) statusEl.classList.add(kind);
  }

  function setFieldError(field, message) {
    var errEl = document.querySelector('[data-error-for="' + field + '"]');
    var input = document.getElementById('contact-' + field);
    if (errEl) errEl.textContent = message || '';
    if (input) {
      var wrap = input.closest('.field');
      if (wrap) wrap.classList.toggle('invalid', !!message);
    }
  }

  function clearErrors() {
    document.querySelectorAll('[data-error-for]').forEach(function (el) {
      el.textContent = '';
    });
    document.querySelectorAll('.field.invalid').forEach(function (el) {
      el.classList.remove('invalid');
    });
  }

  function validate() {
    var ok = true;
    clearErrors();
    var category = form.querySelector('#contact-category').value;
    if (!category) {
      setFieldError('category', 'Please pick a category.');
      ok = false;
    }
    var subject = form.querySelector('#contact-subject').value.trim();
    if (!subject) {
      setFieldError('subject', 'Subject is required.');
      ok = false;
    } else if (subject.length > 200) {
      setFieldError('subject', 'Subject must be at most 200 characters.');
      ok = false;
    }
    var message = form.querySelector('#contact-message').value.trim();
    if (!message) {
      setFieldError('message', 'Message is required.');
      ok = false;
    } else if (message.length < 10) {
      setFieldError('message', 'Please include at least 10 characters of detail.');
      ok = false;
    }
    var email = form.querySelector('#contact-email').value.trim();
    if (email && (!email.includes('@') || !email.includes('.') || email.includes(' '))) {
      setFieldError('email', 'That email address looks invalid.');
      ok = false;
    }
    return ok;
  }

  function setLoading(loading) {
    if (!submitBtn) return;
    submitBtn.classList.toggle('loading', loading);
    submitBtn.disabled = loading;
    var label = submitBtn.querySelector('.btn-label');
    if (label) {
      label.textContent = loading
        ? (submitBtn.dataset.loadingText || 'Submitting…')
        : 'Send submission';
    }
  }

  function showResult(referenceCode, alreadyQueued, deliveryStatus, serverMessage) {
    if (!resultEl || !refEl) return;
    form.hidden = true;
    resultEl.hidden = false;
    refEl.textContent = referenceCode || '—';

    // Honest delivery-status display. The server explicitly tells us
    // whether the submission was durably queued or only best-effort,
    // and we surface that distinction to the user — never claiming
    // "saved" when the queue is volatile.
    if (metaEl) {
      var meta = '';
      if (alreadyQueued) {
        meta = 'Your previous submission was already accepted — no duplicate was created. ';
      } else if (serverMessage) {
        meta = serverMessage + ' ';
      } else {
        meta = 'Queued for delivery. ';
      }
      if (deliveryStatus === 'queued_durable') {
        meta += 'Status: durably queued — will be delivered to the database even if this server restarts.';
      } else if (deliveryStatus === 'queued_volatile') {
        meta += 'Status: queued locally (best-effort) — an operator must drain the queue before the next deploy.';
      } else if (deliveryStatus === 'delivered') {
        meta += 'Status: delivered to the database.';
      }
      metaEl.textContent = meta;
    }
    resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function showError(message) {
    setStatus(message || 'Something went wrong. Please try again.', 'error');
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      form.reset();
      form.hidden = false;
      if (resultEl) resultEl.hidden = true;
      // New idempotency key for the next submission.
      idempotencyKey = generateIdempotencyKey();
      keyInput.value = idempotencyKey;
      if (pageInput) pageInput.value = document.referrer || window.location.href;
      setStatus('', '');
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (submitBtn && submitBtn.disabled) return; // duplicate-submit protection
    if (!validate()) return;

    var payload = {
      category: form.querySelector('#contact-category').value,
      subject: form.querySelector('#contact-subject').value.trim(),
      message: form.querySelector('#contact-message').value.trim(),
      name: form.querySelector('#contact-name').value.trim(),
      email: form.querySelector('#contact-email').value.trim(),
      tool_slug: form.querySelector('#contact-tool').value,
      page_url: form.querySelector('#contact-page').value,
      idempotency_key: idempotencyKey,
      consent: form.querySelector('#contact-consent').checked ? 'yes' : '',
      browser_context: collectBrowserContext(),
    };

    setLoading(true);
    setStatus('Submitting…', '');

    fetch('/api/contact', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(payload),
      credentials: 'same-origin',
    }).then(function (resp) {
      return resp.json().then(function (body) {
        return { status: resp.status, body: body };
      });
    }).then(function (result) {
      var body = result.body || {};
      if (body.ok && body.reference_code) {
        setStatus('Accepted', 'ok');
        // Pass the delivery_status so the result card can honestly tell
        // the user whether the submission is durable or best-effort.
        showResult(body.reference_code, body.already_queued, body.delivery_status, body.message);
      } else if (result.status === 429) {
        showError(body.error || 'Too many submissions. Please wait and try again.');
      } else if (result.status === 503) {
        showError(body.error || 'We cannot accept submissions right now. Please email us directly.');
      } else {
        showError(body.error || 'Submission failed. Please try again.');
      }
    }).catch(function (err) {
      // Network error — DO NOT reset the idempotency key. On retry the
      // server uses the same key and detects the duplicate (or this is
      // the first attempt and it goes through normally).
      showError('Network error. Click "Send submission" again to retry — your text is preserved.');
      console.error('contact submit failed:', err);
    }).finally(function () {
      setLoading(false);
    });
  });

  function collectBrowserContext() {
    var ctx = {};
    try {
      ctx.user_agent = navigator.userAgent.slice(0, 200);
    } catch (e) {}
    try {
      ctx.viewport = { width: window.innerWidth, height: window.innerHeight };
    } catch (e) {}
    try {
      ctx.language = (navigator.language || '').slice(0, 30);
    } catch (e) {}
    try {
      ctx.timezone_offset_minutes = new Date().getTimezoneOffset();
    } catch (e) {}
    try {
      ctx.reduced_motion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) {}
    return ctx;
  }

  function generateIdempotencyKey() {
    // Crypto.randomUUID is widely supported in modern browsers and falls
    // back to a Math.random-based UUID on very old browsers.
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }
})();
