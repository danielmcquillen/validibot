/**
 * Resizable two-column layout with a vertical drag handle.
 *
 * Usage: add `data-resizable-columns` to a container with exactly two
 * child panels. The module inserts a thin drag handle between them
 * and lets users resize by dragging. The ratio is persisted to
 * localStorage under a key derived from `data-resizable-key`.
 *
 * ```html
 * <div data-resizable-columns data-resizable-key="signal-mapping">
 *   <div class="resizable-panel">...left...</div>
 *   <div class="resizable-panel">...right...</div>
 * </div>
 * ```
 *
 * On narrow viewports (< 992px / Bootstrap lg), the layout stacks
 * vertically and the drag handle is hidden automatically via CSS.
 */

const STORAGE_PREFIX = 'validibot:resizable:';
const MIN_PANEL_PX = 280;
const DEFAULT_LEFT_PCT = 58;

function getStoredRatio(key: string): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + key);
    if (raw) {
      const val = parseFloat(raw);
      if (val > 10 && val < 90) {
        return val;
      }
    }
  } catch {
    // localStorage unavailable
  }
  return null;
}

function storeRatio(key: string, pct: number): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, pct.toFixed(1));
  } catch {
    // localStorage unavailable
  }
}

function initResizable(container: HTMLElement): void {
  if (container.dataset.resizableInit === 'true') {
    return;
  }
  container.dataset.resizableInit = 'true';

  const panels = Array.from(
    container.querySelectorAll<HTMLElement>(':scope > .resizable-panel'),
  );
  if (panels.length !== 2) {
    return;
  }
  const [left, right] = panels;
  const key = container.dataset.resizableKey || 'default';

  // Insert drag handle between the two panels
  const handle = document.createElement('div');
  handle.className = 'resizable-handle';
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('tabindex', '0');
  handle.title = 'Drag to resize';
  container.insertBefore(handle, right);

  // Apply initial ratio
  const initial = getStoredRatio(key) ?? DEFAULT_LEFT_PCT;
  applyRatio(initial);

  function applyRatio(pct: number): void {
    left.style.flexBasis = `${pct}%`;
    right.style.flexBasis = `${100 - pct}%`;
    left.style.flexGrow = '0';
    right.style.flexGrow = '0';
    left.style.flexShrink = '0';
    right.style.flexShrink = '0';
    // Ensure min widths are respected
    left.style.minWidth = `${MIN_PANEL_PX}px`;
    right.style.minWidth = `${MIN_PANEL_PX}px`;
  }

  function onDrag(clientX: number): void {
    const rect = container.getBoundingClientRect();
    const offsetX = clientX - rect.left;
    const totalWidth = rect.width;
    let pct = (offsetX / totalWidth) * 100;

    // Enforce min widths as percentage
    const minPct = (MIN_PANEL_PX / totalWidth) * 100;
    pct = Math.max(minPct, Math.min(100 - minPct, pct));

    applyRatio(pct);
  }

  // ── Mouse drag ──────────────────────────────────────────────────

  handle.addEventListener('mousedown', (e: MouseEvent) => {
    e.preventDefault();
    container.classList.add('resizable-dragging');

    const onMouseMove = (ev: MouseEvent) => {
      onDrag(ev.clientX);
    };

    const onMouseUp = () => {
      container.classList.remove('resizable-dragging');
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      // Persist final ratio
      const rect = container.getBoundingClientRect();
      const leftWidth = left.getBoundingClientRect().width;
      storeRatio(key, (leftWidth / rect.width) * 100);
    };

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });

  // ── Touch drag ──────────────────────────────────────────────────

  handle.addEventListener('touchstart', (e: TouchEvent) => {
    e.preventDefault();
    container.classList.add('resizable-dragging');

    const onTouchMove = (ev: TouchEvent) => {
      if (ev.touches.length > 0) {
        onDrag(ev.touches[0].clientX);
      }
    };

    const onTouchEnd = () => {
      container.classList.remove('resizable-dragging');
      document.removeEventListener('touchmove', onTouchMove);
      document.removeEventListener('touchend', onTouchEnd);
      const rect = container.getBoundingClientRect();
      const leftWidth = left.getBoundingClientRect().width;
      storeRatio(key, (leftWidth / rect.width) * 100);
    };

    document.addEventListener('touchmove', onTouchMove);
    document.addEventListener('touchend', onTouchEnd);
  });

  // ── Double-click to reset ───────────────────────────────────────

  handle.addEventListener('dblclick', () => {
    applyRatio(DEFAULT_LEFT_PCT);
    storeRatio(key, DEFAULT_LEFT_PCT);
  });

  // ── Keyboard (arrow keys for accessibility) ─────────────────────

  handle.addEventListener('keydown', (e: KeyboardEvent) => {
    const rect = container.getBoundingClientRect();
    const leftWidth = left.getBoundingClientRect().width;
    const currentPct = (leftWidth / rect.width) * 100;
    const step = 2;

    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const newPct = Math.max(
        (MIN_PANEL_PX / rect.width) * 100,
        currentPct - step,
      );
      applyRatio(newPct);
      storeRatio(key, newPct);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      const maxPct = 100 - (MIN_PANEL_PX / rect.width) * 100;
      const newPct = Math.min(maxPct, currentPct + step);
      applyRatio(newPct);
      storeRatio(key, newPct);
    }
  });
}

export function initResizableColumns(
  root: ParentNode | Document = document,
): void {
  root
    .querySelectorAll<HTMLElement>('[data-resizable-columns]')
    .forEach(initResizable);
}
