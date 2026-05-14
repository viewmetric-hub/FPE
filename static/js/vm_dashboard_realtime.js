/**
 * Shared helpers: highlight "Real time" labels in red on Kaiadmin dashboards.
 */
(function (global) {
  'use strict';

  function vmEscapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function vmBracketize(label) {
    if (!label) return '';
    return ' (' + label + ')';
  }

  function vmIsRealtimeBracketLabel(label) {
    if (label == null) return false;
    return /^real\s*time$/i.test(String(label).trim());
  }

  var VM_REALTIME_COLOR = '#dc3545';

  function vmSetBracketLabel(el, rawLabel) {
    if (!el) return;
    el.textContent = vmBracketize(rawLabel);
    if (vmIsRealtimeBracketLabel(rawLabel)) {
      el.style.color = VM_REALTIME_COLOR;
      el.style.fontWeight = '700';
      el.classList.add('text-danger', 'fw-bold');
    } else {
      el.style.color = '';
      el.style.fontWeight = '';
      el.classList.remove('text-danger', 'fw-bold');
    }
  }

  function vmWrapDayLabelStrong(label) {
    var s = label == null ? '' : String(label);
    if (vmIsRealtimeBracketLabel(s)) {
      return (
        '<strong style="color:' +
        VM_REALTIME_COLOR +
        ';font-weight:700">' +
        vmEscapeHtml(s.trim()) +
        '</strong>'
      );
    }
    return '<strong>' + vmEscapeHtml(s) + '</strong>';
  }

  global.vmEscapeHtml = vmEscapeHtml;
  global.vmBracketize = vmBracketize;
  global.vmIsRealtimeBracketLabel = vmIsRealtimeBracketLabel;
  global.vmSetBracketLabel = vmSetBracketLabel;
  global.vmWrapDayLabelStrong = vmWrapDayLabelStrong;
})(typeof window !== 'undefined' ? window : globalThis);
