(() => {
  document.documentElement.classList.add('motion-ready');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const format = new Intl.NumberFormat('en-US');

  function animateNumber(element) {
    const finalValue = Number(element.dataset.count || 0);
    if (!Number.isFinite(finalValue) || reducedMotion) {
      element.textContent = format.format(finalValue);
      return;
    }

    const duration = 850;
    const start = performance.now();
    const easeOut = (value) => 1 - Math.pow(1 - value, 4);
    element.textContent = '0';

    function frame(now) {
      const progress = Math.min((now - start) / duration, 1);
      element.textContent = format.format(Math.round(finalValue * easeOut(progress)));
      if (progress < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  document.querySelectorAll('[data-count]').forEach(animateNumber);

  const revealItems = document.querySelectorAll('[data-reveal]');
  if (reducedMotion || !('IntersectionObserver' in window)) {
    revealItems.forEach((item) => item.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    revealItems.forEach((item) => observer.observe(item));
  }

  document.querySelectorAll('[data-auto-submit]').forEach((node) => {
    const selects = node instanceof HTMLSelectElement ? [node] : [...node.querySelectorAll('select')];
    selects.forEach((select) => {
      select.addEventListener('change', () => {
        document.body.classList.add('is-navigating');
        select.form.requestSubmit();
      });
    });
  });

  // Compact filter popovers dismiss without changing the applied values.
  document.querySelectorAll('.date-popover, .explorer-filter-popover, .channel-add-popover').forEach((popover) => {
    document.addEventListener('click', (event) => {
      if (!popover.contains(event.target)) popover.removeAttribute('open');
    });
    popover.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        popover.removeAttribute('open');
        popover.querySelector('summary')?.focus();
      }
    });
  });

  // Overview date range (T46): inclusive UTC From/To scope cards and graph.
  // Invalid ranges stay unapplied and the last applied range is kept; an
  // empty range means available history. The server re-validates and answers
  // 422 for anything that slips through, so no partial range ever applies.
  document.querySelectorAll('[data-date-range-form]').forEach((form) => {
    const fromInput = form.querySelector('input[name="from"]');
    const toInput = form.querySelector('input[name="to"]');
    const error = form.querySelector('[data-date-error]');
    const lastFrom = form.dataset.lastFrom || '';
    const lastTo = form.dataset.lastTo || '';
    const showError = (message) => {
      if (!error) return;
      error.textContent = message;
      error.hidden = false;
    };
    const clearError = () => {
      if (!error) return;
      error.textContent = '';
      error.hidden = true;
    };
    const isRealDate = (value) => {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
      const year = Number(value.slice(0, 4));
      if (year < 1970 || year > 2100) return false;
      const date = new Date(`${value}T00:00:00Z`);
      return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
    };
    form.addEventListener('submit', (event) => {
      // Channel auto-submit bypasses Apply only when the dates are already valid.
      const from = (fromInput?.value || '').trim();
      const to = (toInput?.value || '').trim();
      if (!from && !to) {
        clearError();
        return;
      }
      let message = '';
      if (from && !isRealDate(from)) message = 'From must be a real calendar date (YYYY-MM-DD, 1970–2100).';
      else if (to && !isRealDate(to)) message = 'To must be a real calendar date (YYYY-MM-DD, 1970–2100).';
      else if (from && to && from > to) message = 'From must not be after To. The last applied range is kept.';
      if (message) {
        event.preventDefault();
        if (fromInput) fromInput.value = lastFrom;
        if (toInput) toInput.value = lastTo;
        showError(message);
      } else {
        clearError();
      }
    });
  });

  document.querySelectorAll('.refresh-form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button');
      button?.classList.add('is-busy');
      if (button) button.disabled = true;
    });
  });

  function activateSaveConfirmation(saveConfirmation) {
    if (!saveConfirmation) return;
    const section = saveConfirmation.dataset.section;
    const target = section ? document.getElementById(section) : null;
    const savedButton = target?.querySelector('.form-actions button[type="submit"]');
    const originalButtonText = savedButton?.textContent;
    let dismissed = false;

    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      saveConfirmation.classList.add('is-leaving');
      window.setTimeout(() => saveConfirmation.remove(), reducedMotion ? 0 : 360);
    };

    requestAnimationFrame(() => {
      saveConfirmation.classList.add('is-visible');
      if (savedButton) {
        savedButton.textContent = 'Saved';
        savedButton.classList.add('is-saved');
      }
    });

    window.setTimeout(() => {
      if (savedButton && originalButtonText) {
        savedButton.textContent = originalButtonText;
        savedButton.classList.remove('is-saved');
      }
    }, reducedMotion ? 0 : 1900);
    window.setTimeout(dismiss, reducedMotion ? 4000 : 4200);
    saveConfirmation.querySelector('.save-confirmation-close')?.addEventListener('click', dismiss);
  }

  activateSaveConfirmation(document.querySelector('[data-save-confirmation]'));

  // Settings saves (T45): POST the form, accept the canonical T51 JSON
  // response and merge it in place. The panel node stays mounted, so there
  // is no remount blink and scroll/focus never move. Edits are preserved on
  // validation, conflict and network failures.
  const dirtyForms = new Set();

  function settingsFeedback(form) {
    return form.querySelector('[data-save-feedback]');
  }

  function markDirty(form) {
    const section = form.dataset.settingsSave;
    if (!section || form.dataset.submitting === 'true') return;
    dirtyForms.add(section);
    const note = settingsFeedback(form);
    if (note) {
      note.textContent = 'Unsaved changes';
      note.classList.add('is-dirty');
    }
  }

  function markClean(form) {
    dirtyForms.delete(form.dataset.settingsSave);
    const note = settingsFeedback(form);
    if (note) {
      note.textContent = '';
      note.classList.remove('is-dirty');
    }
  }

  function showSettingsNotice(message) {
    const current = document.querySelector('[data-save-confirmation]');
    current?.remove();
    const notice = document.createElement('aside');
    notice.className = 'save-confirmation';
    notice.dataset.saveConfirmation = '';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    const copy = document.createElement('span');
    copy.className = 'save-confirmation-copy';
    const strong = document.createElement('strong');
    strong.textContent = 'Saved';
    const text = document.createElement('span');
    text.textContent = message;
    copy.append(strong, text);
    const close = document.createElement('button');
    close.className = 'save-confirmation-close';
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss confirmation');
    close.textContent = '×';
    const progress = document.createElement('span');
    progress.className = 'save-confirmation-progress';
    progress.setAttribute('aria-hidden', 'true');
    const icon = document.createElement('span');
    icon.className = 'save-confirmation-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = '✓';
    notice.append(icon, copy, close, progress);
    document.getElementById('main-content')?.prepend(notice);
    activateSaveConfirmation(notice);
  }

  function provenanceLabel(entry) {
    if (!entry || typeof entry !== 'object') return { text: 'default', saved: false };
    if (entry.source === 'db') {
      const when = typeof entry.updated_at === 'string' && entry.updated_at ? entry.updated_at.slice(0, 16) : '';
      return { text: when ? `Saved ${when} UTC` : 'Saved', saved: true };
    }
    if (entry.source === 'env') return { text: 'from .env', saved: false };
    return { text: 'default', saved: false };
  }

  function mergeProvenance(form, fields) {
    if (!fields || typeof fields !== 'object') return;
    form.querySelectorAll('[data-provenance-for]').forEach((slot) => {
      const key = slot.dataset.provenanceFor;
      const label = provenanceLabel(fields[key]);
      const tag = slot.querySelector('.source-tag');
      if (tag) {
        tag.textContent = label.text;
        tag.classList.toggle('saved', label.saved);
      }
      slot.querySelectorAll('.link-btn[form]').forEach((reset) => {
        reset.hidden = !label.saved;
      });
    });
  }

  function mergeSetupState(payload) {
    const setup = payload.setup;
    if (!setup || typeof setup !== 'object') return;
    document.querySelectorAll('[data-setup-readiness]').forEach((pill) => {
      pill.classList.toggle('ok', setup.ready === true);
      const blockers = Array.isArray(setup.blockers) ? setup.blockers : [];
      const label = setup.ready === true ? 'Ready' : (blockers[0]?.message || 'Not ready');
      pill.textContent = label;
    });
    const restart = payload.restart_required === true;
    const notices = [...document.querySelectorAll('[data-restart-notice]')];
    let banner = notices.find((node) => !node.hasAttribute('data-idle')) || null;
    let idle = notices.find((node) => node.hasAttribute('data-idle')) || null;
    const telegramPanel = document.getElementById('connection');
    if (restart && !banner && telegramPanel) {
      banner = document.createElement('div');
      banner.className = 'banner warn restart-banner';
      banner.dataset.restartNotice = '';
      banner.setAttribute('role', 'status');
      banner.textContent = 'Connection saved. Restart the collector to use the updated credentials.';
      const connectionForm = telegramPanel.querySelector('form[data-settings-save]');
      if (connectionForm) connectionForm.before(banner);
      else telegramPanel.prepend(banner);
    }
    if (!restart && !idle && telegramPanel) {
      idle = document.createElement('p');
      idle.className = 'panel-note restart-hint';
      idle.dataset.restartNotice = '';
      idle.dataset.idle = '';
      idle.textContent = 'Connection changes take effect after the collector restarts.';
      const connectionForm = telegramPanel.querySelector('form[data-settings-save]');
      if (connectionForm) connectionForm.before(idle);
      else telegramPanel.prepend(idle);
    }
    if (banner) banner.hidden = !restart;
    if (idle) idle.hidden = restart;
    const connection = payload.connection;
    if (connection && typeof connection === 'object') {
      const statusLine = document.querySelector('[data-connection-status]');
      const apiId = document.querySelector('[data-connection-api-id]');
      if (apiId) apiId.textContent = connection.api_id != null ? String(connection.api_id) : '—';
      const sessionPill = statusLine?.querySelector('.pill');
      if (sessionPill && typeof connection.has_session === 'boolean') {
        sessionPill.classList.toggle('ok', connection.has_session);
        const icon = sessionPill.querySelector('i');
        sessionPill.textContent = connection.has_session ? 'Session set' : 'Not set';
        if (icon) sessionPill.prepend(icon);
      }
    }
    const keyEntry = payload.fields?.['studio.openrouter_api_key'];
    if (keyEntry && typeof keyEntry.set === 'boolean') {
      document.querySelectorAll('[data-key-status]').forEach((node) => {
        node.textContent = '';
        const pill = document.createElement('span');
        pill.className = keyEntry.set ? 'pill ok' : 'pill';
        pill.textContent = keyEntry.set ? 'Set' : 'Not set';
        node.append(pill);
      });
    }
    const researchEnabled = payload.fields?.['research.enabled'];
    if (researchEnabled && typeof researchEnabled.value === 'boolean') {
      document.querySelectorAll('[data-switch-text]').forEach((node) => {
        node.textContent = researchEnabled.value ? 'Enabled' : 'Disabled';
      });
    }
  }

  function clearFieldErrors(form) {
    form.querySelectorAll('.field-error[data-settings-error]').forEach((node) => node.remove());
    form.querySelectorAll('[aria-invalid="true"]').forEach((node) => node.removeAttribute('aria-invalid'));
    form.querySelectorAll('.field.has-error').forEach((node) => {
      if (!node.querySelector('.field-error')) node.classList.remove('has-error');
    });
  }

  function showFieldErrors(form, errors) {
    let firstInvalid = null;
    Object.entries(errors).forEach(([field, message]) => {
      const input = form.querySelector(`[name="${field}"]`);
      if (!(input instanceof HTMLElement)) return;
      input.setAttribute('aria-invalid', 'true');
      const control = input.closest('.field-control') || input.parentElement;
      const error = document.createElement('p');
      error.className = 'field-error';
      error.dataset.settingsError = '';
      error.textContent = String(message);
      control?.append(error);
      input.closest('.field')?.classList.add('has-error');
      firstInvalid ??= input;
    });
    firstInvalid?.focus({ preventScroll: true });
  }

  function showPanelError(panel, message) {
    panel.querySelector('[data-settings-save-error]')?.remove();
    const banner = document.createElement('div');
    banner.className = 'banner warn';
    banner.dataset.settingsSaveError = '';
    banner.setAttribute('role', 'alert');
    banner.textContent = message;
    panel.prepend(banner);
  }

  const settingsStack = document.querySelector('.settings-stack');
  if (settingsStack) {
    settingsStack.addEventListener('input', (event) => {
      const form = event.target.closest?.('form[data-settings-save]');
      if (form instanceof HTMLFormElement) markDirty(form);
    });

    settingsStack.addEventListener('change', (event) => {
      const form = event.target.closest?.('form[data-settings-save]');
      if (form instanceof HTMLFormElement) markDirty(form);
    });

    settingsStack.addEventListener('submit', async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
      if (!form.hasAttribute('data-settings-save')) return;

      const action = new URL(form.action, window.location.href);
      if (action.origin !== window.location.origin || !action.pathname.startsWith('/settings/')) return;

      event.preventDefault();
      const panel = form.closest('.panel');
      if (!panel?.id || form.dataset.submitting === 'true') return;

      form.dataset.submitting = 'true';
      const submitter = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
      if (submitter) {
        submitter.disabled = true;
        submitter.classList.add('is-busy');
      }
      panel.querySelector('[data-settings-save-error]')?.remove();

      try {
        const response = await fetch(action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        if (!response.ok || !payload || payload.ok !== true) {
          const errors = payload && typeof payload.errors === 'object' ? payload.errors : null;
          clearFieldErrors(form);
          if (errors && Object.keys(errors).length > 0) {
            showFieldErrors(form, errors);
          } else {
            const message = payload?.error?.message || 'Could not save these settings. Check the connection and try again.';
            showPanelError(panel, message);
          }
          return;
        }

        clearFieldErrors(form);
        mergeProvenance(form, payload.fields);
        mergeSetupState(payload);
        // Secrets are write-only: blank keeps the stored value, so clear the
        // inputs after a save instead of echoing anything back.
        form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
        const clearKey = form.querySelector('input[name="clear_openrouter_api_key"]');
        if (clearKey instanceof HTMLInputElement) clearKey.checked = false;
        markClean(form);
        showSettingsNotice(payload.notice || 'Settings saved.');
      } catch {
        showPanelError(panel, 'Could not save these settings. Check the connection and try again.');
      } finally {
        form.dataset.submitting = 'false';
        if (submitter) {
          submitter.disabled = false;
          submitter.classList.remove('is-busy');
        }
      }
    });

    // Reset buttons post their hidden form to /settings/*/reset. They merge
    // through the same canonical response, and refresh the field input from
    // the server value because a reset changes what the user sees.
    const RESET_FIELD_NAMES = {
      'collection.poll_minutes': 'poll_minutes',
      'collection.track_days': 'track_days',
      'collection.backfill_limit': 'backfill_limit',
      'studio.model': 'model',
      'research.enabled': 'research_enabled',
      'research.blocked_domains': 'blocked_domains',
    };
    settingsStack.addEventListener('submit', (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
      if (form.hasAttribute('data-settings-save')) return;
      const action = new URL(form.action, window.location.href);
      if (!action.pathname.endsWith('/reset')) return;
      event.preventDefault();
      const opener = document.querySelector(`form[data-settings-save] [form="${form.id}"]`);
      const owner = opener?.closest('form[data-settings-save]');
      if (!(owner instanceof HTMLFormElement) || owner.dataset.submitting === 'true') return;
      owner.dataset.submitting = 'true';
      fetch(action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      }).then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (response.ok && payload?.ok === true) {
          owner.closest('.panel')?.querySelector('[data-settings-save-error]')?.remove();
          mergeProvenance(owner, payload.fields);
          mergeSetupState(payload);
          const fields = payload.fields || {};
          Object.entries(RESET_FIELD_NAMES).forEach(([key, name]) => {
            const entry = fields[key];
            const input = owner.querySelector(`[name="${name}"]`);
            if (!entry || !(input instanceof HTMLElement) || !('value' in entry)) return;
            if (input instanceof HTMLInputElement && input.type === 'checkbox') {
              input.checked = entry.value === true;
            } else if (input instanceof HTMLElement && 'value' in input) {
              input.value = String(entry.value ?? '');
            }
          });
          markClean(owner);
          showSettingsNotice(payload.notice || 'Setting reset.');
        } else {
          const panel = owner.closest('.panel');
          if (panel) showPanelError(panel, payload?.error?.message || 'Could not reset this setting.');
        }
      }).catch(() => {
        const panel = owner.closest('.panel');
        if (panel) showPanelError(panel, 'Could not reset this setting. Check the connection and try again.');
      }).finally(() => {
        owner.dataset.submitting = 'false';
      });
    });
  }

  // Unsaved guard: leaving Configuration for Agent logs (or the page) with
  // unsaved edits asks first; saved work is never at risk.
  document.querySelectorAll('[data-settings-view-tabs] a').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (dirtyForms.size === 0) return;
      const stay = !window.confirm('Leave unsaved settings? Your unsaved edits will be lost. Saved settings are unchanged.');
      if (stay) event.preventDefault();
      else dirtyForms.clear();
    });
  });
  window.addEventListener('beforeunload', (event) => {
    if (dirtyForms.size === 0) return;
    event.preventDefault();
  });

  // Section navigation: arrows/Home/End move between section links without
  // changing the page; Enter follows the link natively.
  document.querySelectorAll('[data-settings-section-nav]').forEach((nav) => {
    const links = () => [...nav.querySelectorAll('a')];
    const panels = [...document.querySelectorAll('.settings-stack > section')];
    function selectSection() {
      const requested = location.hash.slice(1);
      const chosen = panels.find((panel) => panel.id === requested) || panels[0];
      if (!chosen) return;
      panels.forEach((panel) => { panel.hidden = panel !== chosen; });
      links().forEach((link) => {
        if (link.getAttribute('href') === `#${chosen.id}`) link.setAttribute('aria-current', 'true');
        else link.removeAttribute('aria-current');
      });
    }
    nav.addEventListener('click', (event) => {
      const link = event.target.closest('a');
      if (!link || !nav.contains(link)) return;
      event.preventDefault();
      history.replaceState(null, '', link.getAttribute('href'));
      selectSection();
    });
    window.addEventListener('hashchange', selectSection);
    selectSection();

    nav.addEventListener('keydown', (event) => {
      const items = links();
      const current = items.indexOf(document.activeElement);
      if (current === -1) return;
      let next = -1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % items.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = items.length - 1;
      if (next === -1) return;
      event.preventDefault();
      items[next].focus();
    });
  });

  // Export actions stay attached to their opener, with native keyboard use.
  document.addEventListener('click', (event) => {
    document.querySelectorAll('.header-export[open]').forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.header-export[open]').forEach((menu) => {
      menu.open = false;
      menu.querySelector('summary')?.focus();
    });
  });

  // Agent logs: the list, filters and pagination are server-rendered over all
  // stored results. This only checks storage availability so an outage reads
  // as unavailable instead of empty.
  const logsAvailability = document.querySelector('[data-logs-availability]');
  if (logsAvailability) {
    fetch(window.location.href, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        const unavailable = !response.ok || payload?.available === false;
        if (unavailable) {
          const message = payload?.error?.message || 'Agent log storage is unavailable. Saved results cannot be listed right now; try again later.';
          logsAvailability.textContent = message;
          logsAvailability.hidden = false;
        } else {
          logsAvailability.hidden = true;
        }
      })
      .catch(() => { /* Keep the server-rendered list; do not invent an outage. */ });
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-delete-dialog-open]');
    if (button instanceof HTMLButtonElement) {
      const dialog = document.getElementById(button.dataset.deleteDialogOpen || '');
      if (!(dialog instanceof HTMLDialogElement)) return;
      dialog.showModal();
      dialog.querySelector('input[name="confirmation"]')?.focus();
      return;
    }

    const closeButton = event.target.closest('[data-delete-dialog-close]');
    if (closeButton instanceof HTMLButtonElement) {
      closeButton.closest('dialog')?.close();
      return;
    }

    if (event.target instanceof HTMLDialogElement && event.target.matches('.delete-channel-dialog')) {
      event.target.close();
    }
  });

  // Modal isolation (T47): Tab and Shift+Tab cycle inside the topmost open
  // modal dialog instead of leaking to the page behind it. Escape and focus
  // return stay with each dialog's own handling.
  const DIALOG_FOCUSABLE = "[autofocus], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || event.defaultPrevented) return;
    const open = [...document.querySelectorAll('dialog[open]')];
    if (open.length === 0) return;
    const dialog = open[open.length - 1];
    const items = [...dialog.querySelectorAll(DIALOG_FOCUSABLE)].filter(
      (node) => node instanceof HTMLElement && node.closest('[hidden]') === null,
    );
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  });

  // Charts — read data from data-chart attributes to keep CSP script-src 'self'.
  function parseChartData(element) {
    const raw = element.getAttribute('data-chart') || element.dataset.chart;
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  // Overview chart (T46): one selected metric in its KPI accent, Day/Week/Month
  // buckets summed per bucket, Daily posts or Cumulative-in-window modes.
  // Monotone interpolation passes through exact values without implying
  // negative values or invented daily growth. Keyboard point buttons and an
  // exact text summary expose every value; no data-table disclosure exists.
  const trendCanvas = document.getElementById('trendChart');
  if (trendCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(trendCanvas);
    if (data && Array.isArray(data.labels)) {
      const channelRoot = document.querySelector('[data-channel-metrics]');
      const channelMetrics = channelRoot ? JSON.parse(channelRoot.dataset.channelMetrics) : null;
      const channelKeys = ['subscribers','views24','views48','views72','er','err'];
      const isChannelMetric = () => channelKeys.includes(state.metric);
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const chartColors = {
        views: styles.getPropertyValue('--cyan').trim() || '#19d2ec',
        reactions: styles.getPropertyValue('--coral').trim() || '#ff716a',
        comments: styles.getPropertyValue('--teal').trim() || '#24d4b8',
        shares: styles.getPropertyValue('--blue').trim() || '#5b91ff',
        subscribers: '#82aaff', views24: '#19d2ec', views48: '#36bfd8', views72: '#60abbf', er: '#ff9c93', err: '#6ee7dd',
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      const chartFills = {
        views: 'rgba(26, 211, 238, .10)',
        reactions: 'rgba(255, 113, 106, .10)',
        comments: 'rgba(36, 212, 184, .10)',
        shares: 'rgba(91, 145, 255, .10)',
      };
      const chartMetricNames = { subscribers: 'Subscribers', views24: 'Typical 24-hour views', views48: 'Typical 48-hour views', views72: 'Typical 72-hour views', er: 'ER', err: 'ERR', views: 'Views', reactions: 'Reactions', comments: 'Comments', shares: 'Shares' };
      const scopeLabel = trendCanvas.dataset.chartScope || '';
      const observationScope = trendCanvas.dataset.observationScope || 'Selected dates';
      const number = (value) => value == null ? 'Unavailable' : ['er','err'].includes(state.metric) ? `${Number(value).toFixed(state.metric === 'er' ? 2 : 1)}%` : format.format(Number(value) || 0);
      const dayLabel = (iso) => {
        const date = new Date(`${iso}T00:00:00Z`);
        if (Number.isNaN(date.getTime())) return iso;
        return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(date);
      };
      const monthLabel = (key) => {
        const date = new Date(`${key}-01T00:00:00Z`);
        if (Number.isNaN(date.getTime())) return key;
        return new Intl.DateTimeFormat('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' }).format(date);
      };

      const state = { metric: 'views', mode: 'daily', period: 'day', point: 0 };
      const metricSelect = document.getElementById('chartMetric');
      if (metricSelect && metricSelect instanceof HTMLSelectElement) {
        state.metric = chartMetricNames[metricSelect.value] ? metricSelect.value : 'views';
      }

      function bucketed() {
        if (isChannelMetric()) {
          const series = channelMetrics?.series || { labels: [] };
          const buckets = new Map();
          series.labels.forEach((label,index) => {
            if (state.period !== 'day' && series.observed?.[index] === false) return;
            let key=label, text=dayLabel(label);
            if (state.period === 'month') {key=label.slice(0,7);text=monthLabel(key);}
            else if (state.period === 'week') {const date=new Date(`${label}T00:00:00Z`);date.setUTCDate(date.getUTCDate()-((date.getUTCDay()+6)%7));key=date.toISOString().slice(0,10);text=`Week of ${dayLabel(key)}`;}
            const raw=series[state.metric]?.[index];
            buckets.set(key,{label:text,value:raw == null ? null : Number(raw)});
          });
          return {labels:[...buckets.values()].map(p=>p.label),values:[...buckets.values()].map(p=>p.value)};
        }
        const daily = (Array.isArray(data[state.metric]) ? data[state.metric] : []).map((v) => Math.max(0, Number(v) || 0));
        const running = [];
        let total = 0;
        daily.forEach((v) => { total += v; running.push(total); });
        const buckets = new Map();
        data.labels.forEach((label, index) => {
          let key = label;
          let text = label;
          if (state.period === 'month') {
            key = String(label).slice(0, 7);
            text = monthLabel(key);
          } else if (state.period === 'week') {
            const date = new Date(`${label}T00:00:00Z`);
            if (!Number.isNaN(date.getTime())) {
              date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7));
              key = date.toISOString().slice(0, 10);
              text = `Week of ${dayLabel(key)}`;
            }
          }
          const entry = buckets.get(key) || { label: text, total: 0, last: index };
          entry.total += daily[index] || 0;
          entry.last = index;
          buckets.set(key, entry);
        });
        const labels = [];
        const values = [];
        buckets.forEach((entry) => {
          labels.push(entry.label);
          values.push(state.mode === 'cumulative' ? running[entry.last] : entry.total);
        });
        return { labels, values };
      }

      const summary = document.querySelector('[data-chart-summary]');
      const readout = document.querySelector('[data-chart-readout]');
      const prevButton = document.querySelector('[data-chart-prev]');
      const nextButton = document.querySelector('[data-chart-next]');
      let trendChart = null;
      if (data.labels.length > 0 || channelMetrics?.series?.labels?.length > 0) {
        trendChart = new window.Chart(trendCanvas, {
          type: 'line',
          data: { labels: [], datasets: [{ label: chartMetricNames[state.metric], data: [], borderColor: chartColors[state.metric], backgroundColor: chartFills[state.metric], fill: true, cubicInterpolationMode: 'monotone', borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 }] },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            animation: motionOK ? { duration: 1400, easing: 'easeOutQuart' } : false,
            plugins: {
              legend: { position: 'top', align: 'start', labels: { color: chartColors.muted, boxWidth: 24, boxHeight: 2, padding: 24, usePointStyle: false, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
              tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12, displayColors: true },
            },
            scales: {
              x: { ticks: { color: chartColors.muted, maxTicksLimit: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
              y: { beginAtZero: true, suggestedMin: 0, ticks: { color: chartColors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: chartColors.grid, borderDash: [4, 5] }, border: { display: false } },
            },
          },
        });
      }

      function currentPoints() {
        const { labels, values } = bucketed();
        return labels.map((label, index) => ({ label, value: values[index] }));
      }

      function renderChart() {
        const points = currentPoints();
        state.point = points.length === 0 ? 0 : Math.min(state.point, points.length - 1);
        const metricName = chartMetricNames[state.metric];
        const modeSuffix = !isChannelMetric() && state.mode === 'cumulative' ? ' (cumulative in window)' : '';
        const legacyMode = document.querySelector('[data-legacy-chart-mode]');
        if (legacyMode) legacyMode.hidden = isChannelMetric();
        const title = document.querySelector('[data-trend-title]');
        if (title) title.textContent = isChannelMetric() ? 'Channel trends' : 'Post performance';
        const scope = document.querySelector('[data-trend-scope]');
        if (scope) scope.textContent = isChannelMetric() ? `Recorded observations · ${observationScope} (UTC). Current channel cards stay current.` : `Latest known totals grouped by publication date · UTC. ${scopeLabel}.`;
        document.querySelectorAll('[data-channel-metric]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.channelMetric === state.metric)));
        if (/^views(24|48|72)$/.test(state.metric)) setHorizon(state.metric.slice(5));
        if (trendChart) {
          trendChart.options.scales.y.beginAtZero = !isChannelMetric();
          trendChart.options.scales.y.suggestedMin = isChannelMetric() ? undefined : 0;
          trendChart.options.scales.y.ticks.callback = value => number(value);
          trendChart.options.plugins.tooltip.callbacks = { label: context => `${chartMetricNames[state.metric]}: ${number(context.parsed.y)}` };
          trendChart.data.labels = points.map((p) => p.label);
          trendChart.data.datasets[0].label = `${metricName}${modeSuffix}`;
          trendChart.data.datasets[0].data = points.map((p) => p.value);
          trendChart.data.datasets[0].borderColor = chartColors[state.metric];
          trendChart.data.datasets[0].backgroundColor = chartFills[state.metric] || `${chartColors[state.metric]}18`;
          trendChart.update(motionOK ? undefined : 'none');
        }
        if (summary) {
          if (points.length === 0) {
            summary.textContent = isChannelMetric() ? `${metricName} · ${observationScope}: no observations in this window.` : `${metricName} · ${scopeLabel}: no posts in this window.`;
          } else if (isChannelMetric()) {
            const latest = points[points.length - 1];
            summary.textContent = `${metricName} · recorded observations: ${points.length} points, latest ${number(latest.value)} on ${latest.label}.`;
          } else {
            const total = points.reduce((sum, p) => sum + p.value, 0);
            const shown = state.mode === 'cumulative'
              ? points[points.length - 1].value
              : total;
            let peak = points[0];
            points.forEach((p) => { if (p.value > peak.value) peak = p; });
            const peakText = state.mode === 'cumulative'
              ? `ending at ${number(shown)}`
              : `peak ${number(peak.value)} on ${peak.label}`;
            summary.textContent = `${metricName} · ${scopeLabel}: ${points.length} ${points.length === 1 ? 'point' : 'points'}, ${number(shown)} total ${state.metric}${modeSuffix}; ${peakText}.`;
          }
        }
        renderReadout();
      }

      function renderReadout() {
        const points = currentPoints();
        if (!readout) return;
        if (points.length === 0) {
          readout.textContent = 'No chart points in this window.';
          trendCanvas.setAttribute('aria-label', isChannelMetric() ? 'Empty channel chart. No observations in this window.' : 'Empty performance chart. No posts in this window.');
          return;
        }
        const point = points[state.point];
        const metricName = chartMetricNames[state.metric];
        const modeSuffix = !isChannelMetric() && state.mode === 'cumulative' ? ', cumulative in window' : '';
        readout.textContent = `${point.label}: ${number(point.value)} ${metricName}${modeSuffix} (point ${state.point + 1} of ${points.length}).`;
        trendCanvas.setAttribute('aria-label', `Performance chart, ${metricName}. Current point: ${readout.textContent}`);
        if (trendChart) {
          try {
            trendChart.setActiveElements([{ datasetIndex: 0, index: state.point }]);
            trendChart.update(motionOK ? undefined : 'none');
          } catch { /* Highlighting is decorative; the readout stays exact. */ }
        }
      }

      function markActive(selector, attr, value) {
        document.querySelectorAll(selector).forEach((item) => {
          const selected = item.getAttribute(attr) === value;
          item.classList.toggle('active', selected);
          item.setAttribute('aria-pressed', String(selected));
        });
      }

      function cardValue(key,value) {
        return value == null ? '—' : ['er','err'].includes(key) ? `${Number(value).toFixed(key==='er'?2:1)}%` : format.format(Math.round(value));
      }
      function setHorizon(age) {
        const key=`views${age}`, value=channelMetrics?.current?.[key];
        document.querySelectorAll('[data-views-horizon]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.viewsHorizon===String(age))));
        const valueButton=document.querySelector('.typical-stat-value');
        if (valueButton) {valueButton.dataset.channelMetric=key;valueButton.setAttribute('aria-label',`Explore Typical ${age}-hour views`);valueButton.setAttribute('aria-pressed',String(state.metric===key));}
        const field=document.querySelector('[data-typical-value]');if(field)field.textContent=cardValue(key,value);
        const detail=document.querySelector('[data-typical-detail]');if(detail)detail.textContent=`At ${age} hours · avg per post`;
        const coverage=channelMetrics?.coverage?.[key];
        const coverageField=document.querySelector('[data-typical-coverage]');
        if(coverageField)coverageField.textContent=coverage ? `${coverage.measured} / ${coverage.eligible} eligible posts measured · last 30 days` : 'No fixed-age observations yet';
        const delta=document.querySelector('.typical-stat-value [data-cm-delta]');if(delta){delta.dataset.cmDelta=key;renderDelta(delta);}
      }
      function renderDelta(field) {
        const key=field.dataset.cmDelta, value=channelMetrics?.current?.[key], before=channelMetrics?.previous?.[key];
        if(value == null || before == null) {field.textContent='History building';return;}
        const change=value-before, sign=change>=0?'+':'';
        const text=['er','err'].includes(key) ? `${sign}${change.toFixed(key==='er'?2:1)} pp` : key==='subscribers' ? `${sign}${format.format(change)}` : before>0 ? `${sign}${(change/before*100).toFixed(1)}%` : null;
        field.textContent=text ? `${text} vs 30 days ago` : 'History building';
      }
      document.querySelectorAll('[data-cm-delta]').forEach(renderDelta);
      setHorizon('24');
      function selectMetric(key) { if(!metricSelect || ![...metricSelect.options].some(o=>o.value===key))return;metricSelect.value=key;metricSelect.dispatchEvent(new Event('change',{bubbles:true})); }
      document.querySelectorAll('[data-channel-metric]').forEach(button=>button.addEventListener('click',()=>selectMetric(button.dataset.channelMetric)));
      document.querySelectorAll('[data-views-horizon]').forEach(button=>button.addEventListener('click',()=>selectMetric(`views${button.dataset.viewsHorizon}`)));
      const definitions=document.querySelector('[data-metric-definitions]');
      document.querySelector('[data-metric-definitions-open]')?.addEventListener('click',()=>definitions?.showModal());
      document.querySelector('[data-metric-definitions-close]')?.addEventListener('click',()=>definitions?.close());
      definitions?.addEventListener('click',event=>{if(event.target===definitions)definitions.close();});
      if (metricSelect) {
        metricSelect.addEventListener('change', () => {
          if (chartMetricNames[metricSelect.value]) {
            state.metric = metricSelect.value;
            state.point = 0;
            renderChart();
          }
        });
      }
      document.getElementById('chartMode')?.addEventListener('change', (event) => {
        state.mode = event.target.value === 'cumulative' ? 'cumulative' : 'daily';
        state.point = 0;
        renderChart();
      });
      document.getElementById('chartPeriod')?.addEventListener('change', (event) => {
        state.period = ['week', 'month'].includes(event.target.value) ? event.target.value : 'day';
        state.point = 0;
        renderChart();
      });
      if (prevButton) {
        prevButton.addEventListener('click', () => {
          const total = currentPoints().length;
          if (total === 0) return;
          state.point = (state.point - 1 + total) % total;
          renderReadout();
        });
      }
      if (nextButton) {
        nextButton.addEventListener('click', () => {
          const total = currentPoints().length;
          if (total === 0) return;
          state.point = (state.point + 1) % total;
          renderReadout();
        });
      }
      renderChart();
    }
  }

  // Post Explorer (T52): server-backed search, one sort, channel subset and
  // metric ranges over /api/explorer. Filters narrow the Overview channel
  // scope and never transfer Studio context. A sequence guard drops delayed
  // responses so rapid filter changes cannot overwrite newer results.
  const EXPLORER_PAGE_SIZE = 20;
  const EXPLORER_METRICS = ['views', 'reactions', 'comments', 'shares'];
  const PREFILL_STORAGE_KEY = 'tg-studio:prefill';
  const PREFILL_MAX_TEXT = 4000;

  function explorerError(message) {
    const node = document.querySelector('[data-explorer-error]');
    if (!node) return;
    if (!message) {
      node.textContent = '';
      node.hidden = true;
      return;
    }
    node.textContent = message;
    node.hidden = false;
  }

  const explorerForm = document.querySelector('[data-explorer-form]');
  if (explorerForm) {
    const rowsBody = document.querySelector('[data-explorer-rows]');
    const pagesNav = document.querySelector('[data-explorer-pages]');
    const chipsBox = document.querySelector('[data-explorer-chips]');
    const countNode = document.querySelector('[data-explorer-count]');
    const searchInput = explorerForm.querySelector('input[name="q"]');
    const sortSelect = explorerForm.querySelector('select[name="sort"]');
    let explorerSequence = 0;

    let currentPage = 1;
    let totalPages = 1;

    function readExplorerState(page) {
      const q = (searchInput?.value || '').trim();
      if (q.length > 200) return { error: 'Search must be at most 200 characters.' };
      const channels = [...explorerForm.querySelectorAll('input[name="channels"]:checked')]
        .map((box) => box.value)
        .filter((value) => /^\d+$/.test(value));
      const sort = sortSelect?.value || 'date';
      if (!['date', 'views', 'reactions', 'comments', 'shares'].includes(sort)) {
        return { error: 'Sort must be newest, views, reactions, comments or shares.' };
      }
      const bounds = {};
      for (const metric of EXPLORER_METRICS) {
        const rawMin = (explorerForm.querySelector(`input[name="min_${metric}"]`)?.value || '').trim();
        const rawMax = (explorerForm.querySelector(`input[name="max_${metric}"]`)?.value || '').trim();
        const low = rawMin === '' ? null : Number(rawMin);
        const high = rawMax === '' ? null : Number(rawMax);
        if ((rawMin !== '' && (!Number.isInteger(low) || low < 0)) || (rawMax !== '' && (!Number.isInteger(high) || high < 0))) {
          return { error: `Invalid ${metric} range: bounds must be whole numbers from 0 up.` };
        }
        if (low !== null && high !== null && low > high) {
          return { error: `Invalid ${metric} range: min must not exceed max.` };
        }
        if (low !== null) bounds[`min_${metric}`] = low;
        if (high !== null) bounds[`max_${metric}`] = high;
      }
      return { q, channels, sort, bounds, page: Math.max(1, page || 1) };
    }

    function explorerQuery(state) {
      const params = new URLSearchParams();
      if (state.q) params.set('q', state.q);
      if (state.channels.length > 0) params.set('channels', state.channels.join(','));
      params.set('sort', state.sort);
      Object.entries(state.bounds).forEach(([key, value]) => params.set(key, String(value)));
      params.set('page', String(state.page));
      params.set('page_size', String(EXPLORER_PAGE_SIZE));
      return params.toString();
    }

    function textCell(text, className) {
      const cell = document.createElement('td');
      cell.className = className;
      cell.textContent = text;
      return cell;
    }

    function metricCell(row, metric) {
      const cell = document.createElement('td');
      cell.className = 'number-cell';
      cell.setAttribute('aria-label', `${metric}: ${format.format(Number(row[metric]) || 0)}`);
      cell.textContent = format.format(Number(row[metric]) || 0);
      if (metric === 'comments' && row.collected_comments !== undefined) {
        const archived = document.createElement('span');
        archived.className = 'archived-count';
        archived.textContent = `${format.format(Number(row.collected_comments) || 0)} archived`;
        cell.append(' ', archived);
      }
      return cell;
    }

    function renderExplorerRows(rows) {
      if (!rowsBody) return;
      rowsBody.textContent = '';
      readerIds = rows.map((row) => Number(row.id)).filter((id) => Number.isInteger(id));
      if (rows.length === 0) {
        const empty = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 5;
        cell.className = 'empty-state';
        cell.textContent = 'No posts match these filters. Adjust or clear them to recover.';
        empty.append(cell);
        rowsBody.append(empty);
        return;
      }
      const showChannel = Boolean(document.querySelector('.explorer-table .col-channel'));
      rows.forEach((row) => {
        const tr = document.createElement('tr');
        const excerpt = (row.text || '(media post)').slice(0, 140);
        const postCell = document.createElement('td');
        postCell.className = 'post-text';
        const openButton = document.createElement('button');
        openButton.type = 'button';
        openButton.className = 'link-btn explorer-open';
        openButton.textContent = excerpt;
        openButton.setAttribute('aria-label', `Read post ${row.message_id || row.id}`);
        openButton.addEventListener('click', () => openReader(Number(row.id), openButton));
        postCell.append(openButton);
        const metadata = document.createElement('span');
        metadata.className = 'post-meta';
        metadata.textContent = `${row.identifier || row.channel_identifier || ''} · ${String(row.posted_at || '').slice(0,16)} UTC`;
        postCell.append(metadata);
        tr.append(postCell);
        EXPLORER_METRICS.forEach((metric) => tr.append(metricCell(row, metric)));
        rowsBody.append(tr);
      });
    }

    function renderChips(state) {
      if (!chipsBox) return;
      chipsBox.textContent = '';
      const chips = [];
      if (state.q) chips.push({ label: `Search: ${state.q}`, clear: () => { if (searchInput) searchInput.value = ''; } });
      if (state.channels.length > 0) {
        chips.push({
          label: `Channels: ${state.channels.length} selected`,
          clear: () => explorerForm.querySelectorAll('input[name="channels"]:checked').forEach((box) => { box.checked = false; }),
        });
      }
      if (state.sort !== 'date') {
        chips.push({ label: `Sort: ${state.sort}`, clear: () => { if (sortSelect) { sortSelect.value = 'date'; sortSelect.dispatchEvent(new Event('ui-select-sync')); } } });
      }
      Object.entries(state.bounds).forEach(([key, value]) => {
        chips.push({
          label: `${key.replace('_', ' ')}: ${value}`,
          clear: () => {
            const input = explorerForm.querySelector(`input[name="${key}"]`);
            if (input) input.value = '';
          },
        });
      });
      chips.forEach((chip) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'chip';
        button.textContent = `${chip.label} ×`;
        button.setAttribute('aria-label', `Remove filter ${chip.label}`);
        button.addEventListener('click', () => {
          chip.clear();
          loadExplorer(1, true);
          explorerForm.querySelector('[type="submit"]')?.focus();
        });
        chipsBox.append(button);
      });
    }

    function renderPages(page, pages, total) {
      currentPage = page;
      totalPages = pages;
      if (countNode) {
        countNode.textContent = `${format.format(total)} ${total === 1 ? 'post' : 'posts'} · page ${page} of ${pages}`;
      }
      if (!pagesNav) return;
      pagesNav.textContent = '';
      pagesNav.setAttribute('aria-label', 'Post pages');
      const addButton = (label, target, attrs) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost small';
        button.textContent = label;
        Object.entries(attrs || {}).forEach(([key, value]) => button.setAttribute(key, value));
        button.addEventListener('click', () => loadExplorer(target, true));
        pagesNav.append(button);
      };
      if (page > 1) addButton('← Previous', page - 1, { rel: 'prev' });
      const info = document.createElement('span');
      info.textContent = `Page ${page} of ${pages} · ${format.format(total)} posts`;
      info.setAttribute('aria-live', 'polite');
      pagesNav.append(info);
      if (page < pages) addButton('Next →', page + 1, { rel: 'next' });
    }

    async function loadExplorer(page, moveFocus) {
      const state = readExplorerState(page);
      if (state.error) {
        explorerError(state.error);
        return;
      }
      explorerError(null);
      const sequence = ++explorerSequence;
      let payload = null;
      try {
        const response = await fetch(`/api/explorer?${explorerQuery(state)}`, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
        });
        payload = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(payload?.error?.message || 'Post Explorer is unavailable right now.');
        }
      } catch (error) {
        if (sequence !== explorerSequence) return;
        explorerError(error instanceof Error ? error.message : 'Could not load the Post Explorer. Check the connection and try again.');
        return;
      }
      if (sequence !== explorerSequence || !payload) return;
      renderExplorerRows(payload.rows || []);
      renderChips(state);
      renderPages(payload.page || 1, payload.total_pages || 1, payload.total || 0);
      if (moveFocus && countNode) {
        countNode.setAttribute('tabindex', '-1');
        countNode.focus({ preventScroll: true });
      }
    }

    explorerForm.addEventListener('submit', (event) => {
      event.preventDefault();
      loadExplorer(1, true);
      explorerForm.querySelector('details')?.removeAttribute('open');
    });
    let searchTimer;
    searchInput?.addEventListener('input', () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => loadExplorer(1, true), 250);
    });
    explorerForm.querySelector('[name=sort]')?.addEventListener('change', () => loadExplorer(1, true));
    explorerForm.querySelector('[data-explorer-clear]')?.addEventListener('click', () => {
      explorerForm.reset();
      sortSelect?.dispatchEvent(new Event('ui-select-sync'));
      explorerError(null);
      loadExplorer(1, false);
      searchInput?.focus();
    });
    loadExplorer(1, false);
  }

  // Post reader (T52): full safe Telegram formatting, discussion, latest
  // metrics with separately labelled collection snapshots, validated source
  // link, previous/next, Escape/focus return and list restoration. Closing
  // keeps the Explorer list, scroll and focus where they were.
  const readerDialog = document.querySelector('[data-reader]');
  const readerBody = document.querySelector('[data-reader-body]');
  const readerError = document.querySelector('[data-reader-error]');
  const readerFullLink = document.querySelector('[data-reader-full]');
  let readerOpener = null;
  let readerIds = [];
  let readerIndex = -1;
  let readerSequence = 0;

  function readerShowError(message) {
    if (!readerError) return;
    if (!message) {
      readerError.textContent = '';
      readerError.hidden = true;
      return;
    }
    readerError.textContent = message;
    readerError.hidden = false;
  }

  function readerPromptText(detail) {
    const excerpt = String(detail.text || '(media post)').slice(0, 2000);
    return `Explore this post from ${detail.channel_identifier || 'the channel'} (published ${String(detail.posted_at || '').slice(0, 10)}):\n\n${excerpt}`;
  }

  function renderReaderDetail(detail) {
    if (!readerBody) return;
    if (readerDialog) {
      if (detail.channel_id) readerDialog.dataset.channelId = String(detail.channel_id);
      else delete readerDialog.dataset.channelId;
    }
    readerBody.textContent = '';
    const heading = document.createElement('p');
    heading.className = 'reader-post-meta';
    heading.textContent = `Post ${detail.message_id} · ${detail.channel_title || detail.channel_identifier || ''} · published ${String(detail.posted_at || '').slice(0, 16)} UTC`;
    readerBody.append(heading);
    // formatted_html is rendered server-side from stored text plus entities
    // through the sanitized Telegram converter, so it is safe to inject.
    // Plain excerpts elsewhere always use textContent, never innerHTML.
    const body = document.createElement('div');
    body.className = 'detail-post-body';
    body.setAttribute('role', 'article');
    if (detail.formatted_html) {
      body.innerHTML = detail.formatted_html;
    } else {
      body.textContent = detail.text || '(media post)';
    }
    readerBody.append(body);
    const metrics = document.createElement('ul');
    metrics.className = 'reader-metrics';
    ['views', 'reactions', 'comments', 'shares'].forEach((metric) => {
      const item = document.createElement('li');
      item.textContent = `${metric}: ${format.format(Number(detail.metrics?.[metric]) || 0)}`;
      metrics.append(item);
    });
    readerBody.append(metrics);
    if (detail.source_url) {
      const source = document.createElement('p');
      source.className = 'reader-source';
      const link = document.createElement('a');
      link.href = detail.source_url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open original post in Telegram';
      source.append(link);
      readerBody.append(source);
    }
    const snapTitle = document.createElement('h3');
    snapTitle.textContent = 'Collection snapshots';
    readerBody.append(snapTitle);
    const snapNote = document.createElement('p');
    snapNote.className = 'scope-note';
    snapNote.textContent = 'Snapshot timestamps are collection times; the Overview chart uses publication dates.';
    readerBody.append(snapNote);
    const snaps = document.createElement('ul');
    snaps.className = 'reader-snapshots';
    (detail.snapshots || []).forEach((snap) => {
      const item = document.createElement('li');
      item.textContent = `Collected ${String(snap.taken_at || '').slice(0, 16)} UTC — ${format.format(snap.views || 0)} views, ${format.format(snap.reactions || 0)} reactions, ${format.format(snap.comments || 0)} comments, ${format.format(snap.shares || 0)} shares`;
      snaps.append(item);
    });
    if ((detail.snapshots || []).length === 0) {
      const item = document.createElement('li');
      item.textContent = 'No snapshots recorded.';
      snaps.append(item);
    }
    readerBody.append(snaps);
    const discussTitle = document.createElement('h3');
    discussTitle.textContent = 'Discussion';
    readerBody.append(discussTitle);
    const discussion = document.createElement('ul');
    discussion.className = 'reader-discussion';
    (detail.discussion || []).forEach((comment) => {
      const item = document.createElement('li');
      const head = document.createElement('p');
      head.className = 'reader-comment-head';
      head.textContent = `${comment.sender_name || 'Unknown'} · ${String(comment.posted_at || '').slice(0, 16)} UTC`;
      const text = document.createElement('p');
      text.textContent = comment.text || '(empty comment)';
      item.append(head, text);
      discussion.append(item);
    });
    if ((detail.discussion || []).length === 0) {
      const item = document.createElement('li');
      item.textContent = 'No comments collected for this post.';
      discussion.append(item);
    }
    readerBody.append(discussion);
    if (readerFullLink) readerFullLink.href = `/post/${encodeURIComponent(String(detail.id))}`;
    const title = document.getElementById('readerTitle');
    if (title) title.textContent = `Post ${detail.message_id} from ${detail.channel_identifier || 'the channel'}`;
  }

  async function openReader(postId, opener) {
    if (!readerDialog || !Number.isInteger(postId) || postId <= 0) return;
    readerOpener = opener instanceof HTMLElement ? opener : null;
    const sequence = ++readerSequence;
    readerShowError(null);
    const known = readerIds.indexOf(postId);
    readerIndex = known === -1 ? -1 : known;
    try {
      const response = await fetch(`/api/posts/${encodeURIComponent(String(postId))}`, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.error?.message || 'This post could not be read.');
      }
      if (sequence !== readerSequence) return;
      renderReaderDetail(payload);
      readerBody.scrollTop = 0;
      updateReaderNav();
      if (typeof readerDialog.showModal === 'function' && !readerDialog.open) {
        readerDialog.showModal();
      }
      readerDialog.querySelector('[data-reader-close]')?.focus();
    } catch (error) {
      if (sequence !== readerSequence) return;
      readerShowError(error instanceof Error ? error.message : 'This post could not be read. Try again or open the full page.');
    }
  }

  function updateReaderNav() {
    const prev = readerDialog?.querySelector('[data-reader-prev]');
    const next = readerDialog?.querySelector('[data-reader-next]');
    if (prev) prev.disabled = readerIds.length === 0 || readerIndex <= 0;
    if (next) next.disabled = readerIds.length === 0 || readerIndex < 0 || readerIndex >= readerIds.length - 1;
  }

  function stepReader(delta) {
    if (readerIds.length === 0 || readerIndex < 0) return;
    const next = readerIndex + delta;
    if (next < 0 || next >= readerIds.length) return;
    openReader(readerIds[next], readerOpener);
  }

  if (readerDialog) {
    readerDialog.querySelector('[data-reader-close]')?.addEventListener('click', () => readerDialog.close());
    readerDialog.querySelector('[data-reader-prev]')?.addEventListener('click', () => stepReader(-1));
    readerDialog.querySelector('[data-reader-next]')?.addEventListener('click', () => stepReader(1));
    readerDialog.querySelector('[data-reader-explore]')?.addEventListener('click', () => {
      const title = document.getElementById('readerTitle')?.textContent || 'a post';
      const bodyText = readerBody?.querySelector('.detail-post-body')?.textContent || '';
      const full = readerFullLink?.getAttribute('href') || '';
      const idMatch = full.match(/\/post\/(\d+)/);
      const payload = {
        channel_id: Number(readerDialog.dataset.channelId || 0) || undefined,
        post_id: idMatch ? Number(idMatch[1]) : undefined,
        text: `Explore ${title}:\n\n${bodyText.slice(0, 2000)}`,
      };
      if (!payload.channel_id || !payload.text.trim()) {
        readerShowError('Studio handoff needs the post’s channel. Open the full page and try again.');
        return;
      }
      if (payload.text.length > PREFILL_MAX_TEXT) payload.text = payload.text.slice(0, PREFILL_MAX_TEXT);
      try {
        window.sessionStorage.setItem(PREFILL_STORAGE_KEY, JSON.stringify(payload));
      } catch {
        readerShowError('Browser storage is unavailable, so the post reference could not be handed to Studio.');
        return;
      }
      window.location.href = '/studio';
    });
    // Escape closes natively; always return focus to the opener so list
    // position and keyboard context are restored.
    readerDialog.addEventListener('close', () => {
      readerShowError(null);
      if (readerOpener && document.contains(readerOpener)) readerOpener.focus();
      readerOpener = null;
    });
  }

  // Standalone post page handoff: stores an authorized post/channel
  // reference and navigates to Studio, which prefills the composer without
  // sending. Raw channel content never travels in the URL.
  document.querySelectorAll('[data-explore-post]').forEach((button) => {
    button.addEventListener('click', () => {
      const channelId = Number(button.getAttribute('data-channel-id') || 0);
      const postId = Number(button.getAttribute('data-post-id') || 0);
      const label = button.getAttribute('data-channel-label') || 'the channel';
      const article = document.querySelector('.detail-post-body');
      const excerpt = (article?.textContent || '').trim().slice(0, 2000);
      const errorNode = document.querySelector('[data-explore-error]');
      const fail = (message) => {
        if (!errorNode) return;
        errorNode.textContent = message;
        errorNode.hidden = false;
      };
      if (!Number.isInteger(channelId) || channelId <= 0 || !Number.isInteger(postId) || postId <= 0 || !excerpt) {
        fail('Studio handoff needs this post’s channel and text. Reload and try again.');
        return;
      }
      const payload = {
        channel_id: channelId,
        post_id: postId,
        text: `Explore this post from ${label}:\n\n${excerpt}`.slice(0, PREFILL_MAX_TEXT),
      };
      try {
        window.sessionStorage.setItem(PREFILL_STORAGE_KEY, JSON.stringify(payload));
      } catch {
        fail('Browser storage is unavailable, so the post reference could not be handed to Studio.');
        return;
      }
      window.location.href = '/studio';
    });
  });
  const postCanvas = document.getElementById('postChart');
  if (postCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(postCanvas);
    if (data && Array.isArray(data.labels)) {
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const colors = {
        reach: styles.getPropertyValue('--cyan').trim(),
        reactions: styles.getPropertyValue('--coral').trim(),
        comments: styles.getPropertyValue('--teal').trim(),
        shares: styles.getPropertyValue('--blue').trim(),
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      new window.Chart(postCanvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [
            { label: 'Reach / Views', data: data.views, borderColor: colors.reach, backgroundColor: 'rgba(25, 210, 236, .10)', fill: true, tension: 0.36, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 },
            { label: 'Reactions', data: data.reactions, borderColor: colors.reactions, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Comments', data: data.comments, borderColor: colors.comments, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Shares', data: data.shares, borderColor: colors.shares, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          animation: motionOK ? { duration: 1300, easing: 'easeOutQuart' } : false,
          plugins: {
            legend: { position: 'top', align: 'start', labels: { color: colors.muted, boxWidth: 24, boxHeight: 2, padding: 24, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
            tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12 },
          },
          scales: {
            x: { ticks: { color: colors.muted, maxTicksLimit: 10, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
            y: { beginAtZero: true, ticks: { color: colors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: colors.grid, borderDash: [4, 5] }, border: { display: false } },
          },
        },
      });
    }
  }
})();
