(() => {
  const makeSkeleton = () => {
    const wrapper = document.createElement('div');
    wrapper.className = 'loading-shelf';
    wrapper.setAttribute('aria-hidden', 'true');
    for (let index = 0; index < 5; index += 1) {
      wrapper.append(document.createElement('span'));
    }
    return wrapper;
  };

  document.querySelectorAll('[data-suggestions]').forEach(input => {
    const panel = document.getElementById(input.getAttribute('aria-controls'));
    let timer;
    let request;
    function hide() {
      panel.hidden = true;
      panel.replaceChildren();
    }
    input.addEventListener('input', () => {
      clearTimeout(timer);
      request?.abort();
      const term = input.value.trim();
      hide();
      if (term.length < 2) return;
      timer = setTimeout(async () => {
        request = new AbortController();
        const url = new URL(input.dataset.suggestions, location.href);
        url.searchParams.set('q', term);
        try {
          const response = await fetch(url, {signal: request.signal});
          if (!response.ok) return;
          const {results} = await response.json();
          if (input.value.trim() !== term || document.activeElement !== input) return;
          for (const result of results) {
            const link = document.createElement('a');
            link.href = result.url;
            link.textContent = result.title;
            const extra = document.createElement('small');
            extra.textContent = [result.author, result.library].filter(Boolean).join(' · ');
            link.append(extra);
            panel.append(link);
          }
          panel.hidden = !results.length;
        } catch (error) {
          if (error.name !== 'AbortError') hide();
        }
      }, 240);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') hide();
      if (event.key === 'ArrowDown' && !panel.hidden) {
        event.preventDefault();
        panel.querySelector('a')?.focus();
      }
    });
    panel.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        input.focus();
        hide();
      }
    });
    input.closest('form').addEventListener('focusout', event => {
      if (!input.closest('form').contains(event.relatedTarget)) hide();
    });
  });

  const filters = document.querySelector('[data-catalogue-form]');
  if (filters) {
    const target = document.getElementById('catalogue-results');
    const status = document.getElementById('catalogue-status');
    let current;
    let original = target.innerHTML;
    async function load(url, push = true) {
      current?.abort();
      const operation = new AbortController();
      current = operation;
      status.textContent = 'Loading catalogue…';
      target.setAttribute('aria-busy', 'true');
      target.replaceChildren(makeSkeleton());
      try {
        const response = await fetch(url, {
          headers: {'X-Libraverse-Fragment': 'catalogue'}, signal: operation.signal,
        });
        if (!response.ok) throw new Error();
        const markup = await response.text();
        if (operation.signal.aborted) return;
        target.innerHTML = markup;
        original = markup;
        if (push && url.href !== location.href) history.pushState({}, '', url);
        status.textContent = '';
      } catch (error) {
        if (error.name === 'AbortError' || operation.signal.aborted) return;
        target.innerHTML = original;
        status.textContent = 'Could not refresh the catalogue. Apply the filters again to retry.';
      } finally {
        if (!operation.signal.aborted) target.removeAttribute('aria-busy');
      }
    }
    function apply() {
      const url = new URL(filters.action);
      url.search = new URLSearchParams(new FormData(filters)).toString();
      load(url);
    }
    filters.addEventListener('submit', event => {
      event.preventDefault();
      apply();
    });
    filters.addEventListener('change', event => {
      if (event.target.matches('select')) apply();
    });
    target.addEventListener('click', event => {
      const link = event.target.closest('.pagination a');
      if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        load(new URL(link.href));
      }
    });
    addEventListener('popstate', () => {
      const url = new URL(location.href);
      for (const field of filters.elements) {
        if (field.name) field.value = url.searchParams.get(field.name) || (field.name === 'sort' ? 'title' : '');
      }
      load(url, false);
    });
  }

  const dialog = document.getElementById('book-quick-view');
  const quickContent = dialog?.querySelector('[data-quick-content]');
  let preview;
  dialog?.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
  dialog?.addEventListener('close', () => preview?.abort());
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-quick-url]');
    if (event.target.closest('[data-print]')) window.print();
    if (!button || !dialog) return;
    preview?.abort();
    const operation = new AbortController();
    preview = operation;
    quickContent.replaceChildren(makeSkeleton());
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(button.dataset.quickUrl, {signal: operation.signal});
      if (!response.ok) throw new Error();
      const documentFragment = new DOMParser().parseFromString(await response.text(), 'text/html');
      const card = documentFragment.querySelector('[data-book-preview]');
      if (!card) throw new Error();
      if (!operation.signal.aborted) quickContent.replaceChildren(card);
    } catch (error) {
      if (operation.signal.aborted) return;
      const link = document.createElement('a');
      link.href = button.dataset.quickUrl;
      link.textContent = 'Could not load the preview. Open book details.';
      quickContent.replaceChildren(link);
    }
  });

  async function refreshReservation(panel) {
    const response = await fetch(panel.dataset.refreshUrl, {
      headers: {'X-Libraverse-Fragment': 'reservation'},
    });
    if (!response.ok) throw new Error();
    panel.innerHTML = await response.text();
  }
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-reservation-form]');
    if (!form || !event.submitter) return;
    event.preventDefault();
    const button = event.submitter;
    const data = new FormData(form);
    data.set(button.name, button.value);
    const panel = form.closest('[data-reservation-panel]');
    const status = panel.parentElement.querySelector('[data-reservation-message]');
    button.disabled = true;
    status.textContent = 'Updating reservation…';
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: data, headers: {'X-Libraverse-Fragment': 'reservation'},
      });
      if (response.redirected) {
        location.assign(response.url);
        return;
      }
      const payload = await response.json();
      status.textContent = payload.message || payload.error;
      if (response.ok) {
        try {
          await refreshReservation(panel);
          panel.querySelector('button')?.focus();
        } catch {
          status.textContent += ' Refresh the page to see the latest status.';
        }
      }
    } catch {
      status.textContent = 'Could not update the reservation. Please try again.';
    } finally {
      button.disabled = false;
    }
  });
  let lastRefresh = 0;
  addEventListener('focus', () => {
    if (Date.now() - lastRefresh < 15000) return;
    lastRefresh = Date.now();
    document.querySelectorAll('[data-reservation-panel]').forEach(panel => {
      if (!panel.querySelector('button:disabled') && !panel.contains(document.activeElement)) {
        refreshReservation(panel).catch(() => {});
      }
    });
  });
})();
