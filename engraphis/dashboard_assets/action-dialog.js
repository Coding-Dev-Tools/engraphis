/* Engraphis action dialog.

   One accessible, CSP-safe modal replaces the browser's native confirm() and prompt()
   boxes. Native boxes cannot be styled or themed, block the page's event loop, and are
   suppressed outright by some embedded browsers, which made destructive confirmations
   silently fail. The markup lives in index.html (#action-dialog); this module only
   fills it with text nodes and form controls, so untrusted names and titles stay inert.

   EngraphisDialog.open(spec) resolves to an object of field values, or null when the
   person cancels. confirm(spec) resolves to a boolean and prompt(spec) to a string or
   null. Only one dialog is open at a time; opening another cancels the first. */
(function () {
  'use strict';

  let pending = null;
  const byId = id => document.getElementById(id);
  /* Workspace names and memory titles are untrusted and appear inside the prose. Bidi
     embedding, override and isolate controls, and the implicit direction marks, could
     reorder that prose on screen, for example to make one action read as another, so the
     dialog never displays them. */
  const BIDI_CONTROLS = /[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;
  const shown = value => String(value == null ? '' : value).replace(BIDI_CONTROLS, '');

  function paragraphs(message) {
    const values = Array.isArray(message) ? message : [message];
    return values.filter(value => value != null && shown(value).trim()).map(value => {
      const paragraph = document.createElement('p');
      paragraph.textContent = shown(value);
      return paragraph;
    });
  }

  function fieldControl(field, index) {
    const id = `action-dialog-field-${field.name || index}`;
    const label = document.createElement('label');
    label.htmlFor = id;
    const caption = document.createElement('span');
    caption.textContent = shown(field.label || field.name || 'Value');
    label.append(caption);
    let control;
    if (field.type === 'select') {
      control = document.createElement('select');
      (field.options || []).forEach(choice => {
        const item = document.createElement('option');
        item.value = String(choice.value);
        item.textContent = shown(choice.label == null ? choice.value : choice.label);
        if (String(choice.value) === String(field.value)) item.selected = true;
        control.append(item);
      });
    } else if (field.type === 'textarea') {
      control = document.createElement('textarea');
      control.rows = field.rows || 4;
      control.value = field.value == null ? '' : String(field.value);
    } else {
      control = document.createElement('input');
      control.type = field.type === 'password' ? 'password' : 'text';
      control.value = field.value == null ? '' : String(field.value);
      control.autocomplete = 'off';
      control.spellcheck = field.type !== 'password' && field.spellcheck !== false;
    }
    control.id = id;
    control.dataset.fieldName = field.name || String(index);
    if (field.maxLength) control.maxLength = field.maxLength;
    if (field.placeholder) control.placeholder = field.placeholder;
    if (field.required) {
      control.required = true;
      control.setAttribute('aria-required', 'true');
    }
    label.append(control);
    if (field.hint) {
      const hint = document.createElement('small');
      hint.className = 'project-help';
      hint.textContent = shown(field.hint);
      label.append(hint);
    }
    return label;
  }

  function settle(value) {
    const current = pending;
    if (!current) return;
    pending = null;
    current.cleanup();
    const dialog = byId('action-dialog');
    if (dialog && dialog.open) dialog.close();
    byId('action-dialog-fields').replaceChildren();
    if (current.returnFocus && current.returnFocus.isConnected
      && typeof current.returnFocus.focus === 'function') {
      current.returnFocus.focus();
    }
    current.resolve(value);
  }

  function open(spec = {}) {
    const dialog = byId('action-dialog');
    const form = byId('action-dialog-form');
    if (!dialog || !form || typeof dialog.showModal !== 'function') return Promise.resolve(null);
    if (pending) settle(null);
    const returnFocus = document.activeElement;
    const title = byId('action-dialog-title');
    const message = byId('action-dialog-message');
    const fields = byId('action-dialog-fields');
    const error = byId('action-dialog-error');
    const submit = byId('action-dialog-submit');
    const cancel = byId('action-dialog-cancel');
    title.textContent = shown(spec.title || 'Confirm');
    message.replaceChildren(...paragraphs(spec.message));
    fields.replaceChildren(...(spec.fields || []).map(fieldControl));
    error.textContent = '';
    error.hidden = true;
    submit.textContent = shown(spec.confirmLabel || 'Continue');
    submit.className = spec.danger ? 'danger-button' : 'primary-button';
    cancel.textContent = shown(spec.cancelLabel || 'Cancel');
    dialog.classList.toggle('danger', Boolean(spec.danger));

    return new Promise(resolve => {
      const onSubmit = event => {
        event.preventDefault();
        const values = {};
        let invalid = null;
        fields.querySelectorAll('[data-field-name]').forEach(control => {
          const value = control.tagName === 'SELECT' ? control.value : control.value.trim();
          values[control.dataset.fieldName] = value;
          if (control.required && !value && !invalid) invalid = control;
        });
        if (invalid) {
          error.textContent = 'Complete the required field before continuing.';
          error.hidden = false;
          invalid.setAttribute('aria-invalid', 'true');
          invalid.focus();
          return;
        }
        settle(values);
      };
      const onCancel = event => {
        if (event) event.preventDefault();
        settle(null);
      };
      const onClose = () => {
        // A previous close event can arrive after its continuation opens a new dialog.
        if (!dialog.open) settle(null);
      };
      form.addEventListener('submit', onSubmit);
      cancel.addEventListener('click', onCancel);
      dialog.addEventListener('cancel', onCancel);
      dialog.addEventListener('close', onClose);
      pending = {
        resolve,
        returnFocus,
        cleanup: () => {
          form.removeEventListener('submit', onSubmit);
          cancel.removeEventListener('click', onCancel);
          dialog.removeEventListener('cancel', onCancel);
          dialog.removeEventListener('close', onClose);
        },
      };
      dialog.showModal();
      const firstField = fields.querySelector('input, textarea, select');
      if (firstField) {
        firstField.focus();
        if (typeof firstField.select === 'function' && firstField.tagName !== 'SELECT') firstField.select();
      } else if (spec.danger) cancel.focus();
      else submit.focus();
    });
  }

  async function confirm(spec = {}) {
    return (await open({ ...spec, fields: [] })) !== null;
  }

  async function prompt(spec = {}) {
    const result = await open({
      ...spec,
      fields: [{
        name: 'value', label: spec.label, value: spec.value, type: spec.multiline ? 'textarea' : 'text',
        required: spec.required !== false, maxLength: spec.maxLength, placeholder: spec.placeholder,
        hint: spec.hint,
      }],
    });
    return result === null ? null : result.value;
  }

  window.EngraphisDialog = Object.freeze({ open, confirm, prompt });
})();
