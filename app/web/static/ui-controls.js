/* TGhost shared control primitives (T50).
 *
 * Reusable dropdown (select enhancement) and dialog helpers consumed by
 * Overview, Studio-adjacent web surfaces and Settings. Every control keeps
 * an accessible name, full keyboard operation, focus return and viewport fit:
 *
 * Dropdown (`select[data-ui-dropdown]`):
 * - Trigger button carries `aria-haspopup="listbox"`, `aria-expanded` and
 *   `aria-controls`; the menu uses `role="listbox"` with an `aria-label`.
 * - ArrowDown/ArrowUp/Home/End open the menu; arrows move, Enter/Space
 *   choose, Escape closes and returns focus to the trigger, Tab closes.
 * - Single-character type-ahead jumps to a matching option.
 * - Outside pointerdown closes without moving focus.
 * - The menu opens upward (`.opens-up`) when space below is insufficient.
 * - The native select stays in the form so submissions, auth and CSRF are
 *   unchanged; the custom trigger degrades gracefully without JavaScript.
 *
 * Dialog (`dialog[data-ui-dialog]`):
 * - Openers use `data-ui-dialog-open="<id>"`; closers use
 *   `data-ui-dialog-close` inside the dialog. Focus returns to the opener.
 * - Native Escape handling is preserved; optional backdrop dismissal is
 *   opt-in via `data-ui-dialog-backdrop="close"`.
 * - Viewport-safe sizing lives in `style.css` (`.ui-dialog` rules).
 */
(() => {
  "use strict";

  var openMenu = null;

  function closeMenu(restoreFocus) {
    if (!openMenu) return;
    var state = openMenu;
    openMenu = null;
    state.wrapper.classList.remove("is-open", "opens-up");
    state.trigger.setAttribute("aria-expanded", "false");
    state.menu.hidden = true;
    if (restoreFocus) state.trigger.focus();
  }

  function viewportTop() {
    var header = document.querySelector(".topbar");
    if (header && ["fixed", "sticky"].includes(getComputedStyle(header).position)) {
      return Math.max(16, header.getBoundingClientRect().bottom + 8);
    }
    return 16;
  }

  function viewportBottom() {
    var rail = document.querySelector(".sidebar");
    var bottom = window.innerHeight - 8;
    if (rail && getComputedStyle(rail).position === "fixed") {
      var rect = rail.getBoundingClientRect();
      if (rect.top > window.innerHeight / 2) bottom = Math.min(bottom, rect.top - 8);
    }
    return bottom;
  }

  function fitMenu(state) {
    var rect = state.wrapper.getBoundingClientRect();
    state.menu.style.maxHeight = "";
    var needed = state.menu.getBoundingClientRect().height;
    var below = viewportBottom() - rect.bottom - 8;
    var above = rect.top - viewportTop();
    var opensUp = below < needed && above > below;
    state.wrapper.classList.toggle("opens-up", opensUp);
    state.menu.style.maxHeight = Math.min(needed, Math.max(80, opensUp ? above : below)) + "px";
  }

  function fitPopover(popover) {
    var summary = popover.querySelector("summary");
    var body = summary.nextElementSibling;
    if (!(body instanceof HTMLElement)) return;
    body.style.maxHeight = "";
    body.style.translate = "";
    var rect = summary.getBoundingClientRect();
    var below = viewportBottom() - rect.bottom - 8;
    var above = rect.top - viewportTop();
    var needed = body.getBoundingClientRect().height;
    var opensUp = below < needed && above > below;
    popover.classList.toggle("opens-up", opensUp);
    body.style.maxHeight = Math.min(needed, Math.max(80, opensUp ? above : below)) + "px";
    var bounds = body.getBoundingClientRect();
    var shift = Math.max(16 - bounds.left, 0) - Math.max(bounds.right - window.innerWidth + 16, 0);
    body.style.translate = shift + "px 0";
  }

  function openMenuAt(state, index) {
    closeMenu(false);
    document.querySelectorAll("details[data-ui-popover][open]").forEach(function (popover) { popover.open = false; });
    state.menu.hidden = false;
    state.wrapper.classList.add("is-open");
    fitMenu(state);
    state.trigger.setAttribute("aria-expanded", "true");
    openMenu = state;
    var options = state.options;
    if (!options.length) {
      state.trigger.focus();
      return;
    }
    var at = Math.max(0, Math.min(options.length - 1, index));
    options[at].focus();
  }

  function moveFocus(state, delta) {
    var options = state.options;
    var focused = options.indexOf(document.activeElement);
    if (focused === -1) return;
    var next = (focused + delta + options.length) % options.length;
    options[next].focus();
  }

  function jumpToChar(state, ch) {
    var lowered = ch.toLowerCase();
    var found = state.options.findIndex(function (option) {
      return (option.textContent || "").toLowerCase().indexOf(lowered) === 0;
    });
    if (found >= 0) state.options[found].focus();
  }

  function syncTrigger(state) {
    var selected = state.select.selectedOptions[0];
    var label = selected ? selected.textContent : "No options";
    state.value.textContent = label;
    state.trigger.title = label;
    var name = state.label + ": " + label;
    state.trigger.setAttribute("aria-label", name);
    state.options.forEach(function (option, index) {
      option.setAttribute(
        "aria-selected",
        String(index === state.select.selectedIndex)
      );
    });
  }

  function buildOption(state, nativeOption, optionIndex) {
    var option = document.createElement("div");
    option.className = "ui-select-option";
    option.id = state.menu.id + "-" + optionIndex;
    option.setAttribute("role", "option");
    option.tabIndex = -1;
    option.textContent = nativeOption.textContent;
    option.setAttribute(
      "aria-selected",
      String(optionIndex === state.select.selectedIndex)
    );
    if (nativeOption.disabled) option.setAttribute("aria-disabled", "true");
    option.addEventListener("click", function () {
      if (nativeOption.disabled) return;
      state.select.value = nativeOption.value;
      state.select.dispatchEvent(new Event("change", { bubbles: true }));
      syncTrigger(state);
      closeMenu(true);
    });
    return option;
  }

  function enhanceSelect(select) {
    if (!select || select.dataset.uiDropdownReady === "true") return null;
    select.dataset.uiDropdownReady = "true";

    var fieldLabel = select.getAttribute("aria-label");
    if (!fieldLabel) {
      var hostLabel = select.closest("label");
      fieldLabel =
        hostLabel && hostLabel.querySelector("span")
          ? hostLabel.querySelector("span").textContent.trim()
          : "Options";
    }

    var wrapper = document.createElement("div");
    wrapper.className = "ui-select";
    var trigger = document.createElement("button");
    trigger.className = "ui-select-trigger";
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    var value = document.createElement("span");
    value.className = "ui-select-value";
    var chevron = document.createElement("span");
    chevron.className = "ui-select-chevron ui-chevron";
    chevron.setAttribute("aria-hidden", "true");
    trigger.append(value, chevron);

    var menu = document.createElement("div");
    menu.className = "ui-select-menu";
    var uid = select.id || select.name || "options";
    menu.id = "ui-select-" + String(uid).replace(/[^a-zA-Z0-9_-]+/g, "-");
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", fieldLabel);
    menu.hidden = true;
    trigger.setAttribute("aria-controls", menu.id);

    var state = {
      select: select,
      wrapper: wrapper,
      trigger: trigger,
      menu: menu,
      value: value,
      options: [],
      label: fieldLabel,
    };

    Array.prototype.forEach.call(select.options, function (native, i) {
      var option = buildOption(state, native, i);
      state.options.push(option);
      menu.append(option);
    });

    function onTriggerClick() {
      if (openMenu && openMenu.wrapper === wrapper) closeMenu(false);
      else openMenuAt(state, select.selectedIndex);
    }

    function onTriggerKey(event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        openMenuAt(state, select.selectedIndex);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openMenuAt(state, select.options.length - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        openMenuAt(state, 0);
      } else if (event.key === "End") {
        event.preventDefault();
        openMenuAt(state, select.options.length - 1);
      }
    }

    function onMenuKey(event) {
      var focused = state.options.indexOf(document.activeElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveFocus(state, 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveFocus(state, -1);
      } else if (event.key === "Home") {
        event.preventDefault();
        if (state.options.length) state.options[0].focus();
      } else if (event.key === "End") {
        event.preventDefault();
        if (state.options.length) state.options[state.options.length - 1].focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (focused >= 0) state.options[focused].click();
      } else if (event.key === "Tab") {
        closeMenu(false);
      } else if (
        event.key.length === 1 &&
        !event.altKey &&
        !event.ctrlKey &&
        !event.metaKey
      ) {
        jumpToChar(state, event.key);
      }
    }

    trigger.addEventListener("click", onTriggerClick);
    trigger.addEventListener("keydown", onTriggerKey);
    menu.addEventListener("keydown", onMenuKey);
    select.addEventListener("tg:options", function () {
      closeMenu(false);
      menu.replaceChildren();
      state.options = [];
      Array.prototype.forEach.call(select.options, function (native, i) {
        var option = buildOption(state, native, i);
        state.options.push(option);
        menu.appendChild(option);
      });
      syncTrigger(state);
    });
    select.addEventListener("change", function () {
      syncTrigger(state);
    });
    select.addEventListener("ui-select-sync", function () {
      syncTrigger(state);
    });

    select.classList.add("ui-native-select");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.parentNode.insertBefore(wrapper, select.nextSibling);
    wrapper.append(trigger, menu);
    syncTrigger(state);

    state.destroy = function () {
      trigger.removeEventListener("click", onTriggerClick);
      trigger.removeEventListener("keydown", onTriggerKey);
      menu.removeEventListener("keydown", onMenuKey);
      if (openMenu && openMenu.wrapper === wrapper) closeMenu(false);
      wrapper.remove();
      select.classList.remove("ui-native-select");
      select.removeAttribute("aria-hidden");
      select.tabIndex = 0;
      delete select.dataset.uiDropdownReady;
    };
    return state;
  }

  function firstFocusable(dialog) {
    return dialog.querySelector(
      "[autofocus], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"
    );
  }

  function openDialog(dialog, opener) {
    if (!(dialog instanceof HTMLDialogElement)) return false;
    if (opener) dialog.dataset.uiDialogOpener = "true";
    dialog.dataset.uiDialogOpenerRef = opener ? "set" : "";
    if (opener && opener.id) dialog.dataset.uiDialogOpenerId = opener.id;
    dialog._uiOpener = opener || null;
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    var target = firstFocusable(dialog);
    if (target) target.focus();
    return true;
  }

  function closeDialog(dialog, restoreFocus) {
    if (!(dialog instanceof HTMLDialogElement)) return false;
    var opener = dialog._uiOpener || null;
    if (!opener && dialog.dataset.uiDialogOpenerId) {
      opener = document.getElementById(dialog.dataset.uiDialogOpenerId);
    }
    if (dialog.open) dialog.close();
    else dialog.removeAttribute("open");
    if (restoreFocus !== false && opener && document.contains(opener)) {
      opener.focus();
    }
    return true;
  }

  function wireDialogs(root) {
    var scope = root || document;
    scope.querySelectorAll("dialog[data-ui-dialog]").forEach(function (dialog) {
      if (dialog.dataset.uiDialogReady === "true") return;
      dialog.dataset.uiDialogReady = "true";
      dialog.addEventListener("close", function () {
        var opener = dialog._uiOpener || null;
        if (!opener && dialog.dataset.uiDialogOpenerId) {
          opener = document.getElementById(dialog.dataset.uiDialogOpenerId);
        }
        if (opener && document.contains(opener)) opener.focus();
      });
      if (dialog.dataset.uiDialogBackdrop === "close") {
        dialog.addEventListener("click", function (event) {
          if (event.target === dialog) closeDialog(dialog, true);
        });
      }
    });
  }

  function onDocumentClick(event) {
    var statusCloser = event.target.closest("[data-sync-dismiss]");
    if (statusCloser) {
      statusCloser.closest(".sync-feedback").remove();
      var url = new URL(window.location.href);
      url.searchParams.delete("msg");
      window.history.replaceState(window.history.state, "", url);
      document.querySelector(".refresh-form button[type=submit]").focus();
      return;
    }
    var opener = event.target.closest("[data-ui-dialog-open]");
    if (opener instanceof HTMLElement) {
      var dialog = document.getElementById(opener.dataset.uiDialogOpen || "");
      if (dialog instanceof HTMLDialogElement) {
        event.preventDefault();
        openDialog(dialog, opener);
      }
      return;
    }
    var closer = event.target.closest("[data-ui-dialog-close]");
    if (closer instanceof HTMLElement) {
      var host = closer.closest("dialog");
      if (host instanceof HTMLDialogElement) closeDialog(host, true);
      return;
    }
    if (openMenu && !openMenu.wrapper.contains(event.target)) closeMenu(false);
  }

  function onDocumentKey(event) {
    if (event.key === "Escape") {
      document.querySelectorAll("details[data-ui-popover][open]").forEach(function (popover) {
        event.preventDefault();
        popover.open = false;
        popover.querySelector("summary").focus();
      });
    }
    if (event.key === "Escape" && openMenu) {
      event.preventDefault();
      closeMenu(true);
    }
  }

  var wired = false;
  function initAll(root) {
    var scope = root || document;
    scope
      .querySelectorAll("select[data-ui-dropdown]")
      .forEach(function (select) {
        enhanceSelect(select);
      });
    wireDialogs(scope);
    if (!wired) {
      wired = true;
      document.addEventListener("click", onDocumentClick);
      document.addEventListener("keydown", onDocumentKey);
      window.addEventListener("resize", function () {
        if (openMenu) fitMenu(openMenu);
        document.querySelectorAll("details[data-ui-popover][open]").forEach(fitPopover);
      });
      document.addEventListener("toggle", function (event) {
        var popover = event.target;
        if (!(popover instanceof HTMLDetailsElement) || !popover.matches("[data-ui-popover]") || !popover.open) return;
        closeMenu(false);
        fitPopover(popover);
        document.querySelectorAll("details[data-ui-popover][open]").forEach(function (other) { if (other !== popover) other.open = false; });
      }, true);
      document.addEventListener("ui-popover-refit", function (event) {
        var popover = event.target;
        if (popover instanceof HTMLDetailsElement && popover.matches("[data-ui-popover]") && popover.open) fitPopover(popover);
      }, true);
      document.addEventListener("pointerdown", function (event) {
        document.querySelectorAll("details[data-ui-popover][open]").forEach(function (popover) { if (!popover.contains(event.target)) popover.open = false; });
        if (openMenu && !openMenu.wrapper.contains(event.target)) {
          closeMenu(false);
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function onReady() {
      document.removeEventListener("DOMContentLoaded", onReady);
      initAll(document);
    });
  } else {
    initAll(document);
  }

  window.TgUI = {
    enhanceSelect: enhanceSelect,
    closeMenu: closeMenu,
    openDialog: openDialog,
    closeDialog: closeDialog,
    initAll: initAll,
  };
})();
