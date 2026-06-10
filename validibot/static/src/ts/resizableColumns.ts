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
const FALLBACK_DEFAULT_LEFT_PCT = 58;
const MIN_RATIO = 10;
const MAX_RATIO = 90;

function createHandle(): HTMLElement {
  const handle = document.createElement('div');
  handle.className = 'resizable-handle';
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-label', 'Resize columns');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('aria-valuemin', '10');
  handle.setAttribute('aria-valuemax', '90');
  handle.setAttribute('tabindex', '0');
  handle.title = 'Drag to resize';
  return handle;
}

function getStoredRatio(key: string): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + key);
    if (raw) {
      const val = parseFloat(raw);
      if (val > MIN_RATIO && val < MAX_RATIO) {
        return val;
      }
    }
  } catch {
    // localStorage unavailable
  }
  return null;
}

function getDefaultRatio(container: HTMLElement): number {
  const configured = parseFloat(container.dataset.resizableDefault ?? '');
  if (configured > MIN_RATIO && configured < MAX_RATIO) {
    return configured;
  }
  return FALLBACK_DEFAULT_LEFT_PCT;
}

function getHandleOuterWidth(handle: HTMLElement): number {
  const style = window.getComputedStyle(handle);
  const marginLeft = parseFloat(style.marginLeft) || 0;
  const marginRight = parseFloat(style.marginRight) || 0;
  return handle.getBoundingClientRect().width + marginLeft + marginRight;
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

  const panels = Array.from(
    container.querySelectorAll<HTMLElement>(':scope > .resizable-panel'),
  );
  if (panels.length !== 2) {
    return;
  }
  const [left, right] = panels;
  const key = container.dataset.resizableKey || 'default';
  const defaultRatio = getDefaultRatio(container);

  // Templates render the handle so the affordance is visible immediately.
  // Keep dynamic creation as a fallback for other resizable layouts.
  const existingHandle = container.querySelector<HTMLElement>(
    ':scope > .resizable-handle',
  );
  const handle = existingHandle ?? createHandle();
  if (!existingHandle) {
    container.insertBefore(handle, right);
  }
  container.dataset.resizableInit = 'true';

  let currentRatio = getStoredRatio(key) ?? defaultRatio;
  applyRatio(currentRatio);

  function applyRatio(pct: number): void {
    const containerWidth = container.getBoundingClientRect().width;
    const availableWidth = Math.max(
      0,
      containerWidth - getHandleOuterWidth(handle),
    );
    const minRatio = Math.min(50, (MIN_PANEL_PX / availableWidth) * 100);
    currentRatio = Math.max(minRatio, Math.min(100 - minRatio, pct));
    const leftWidth = availableWidth * (currentRatio / 100);
    const rightWidth = availableWidth - leftWidth;

    left.style.flexBasis = `${leftWidth}px`;
    right.style.flexBasis = `${rightWidth}px`;
    left.style.flexGrow = '0';
    right.style.flexGrow = '0';
    left.style.flexShrink = '0';
    right.style.flexShrink = '0';
    // Ensure min widths are respected
    left.style.minWidth = `${MIN_PANEL_PX}px`;
    right.style.minWidth = `${MIN_PANEL_PX}px`;
    handle.setAttribute('aria-valuenow', currentRatio.toFixed(1));
  }

  function onDrag(clientX: number): void {
    const rect = container.getBoundingClientRect();
    const handleWidth = getHandleOuterWidth(handle);
    const availableWidth = rect.width - handleWidth;
    const leftWidth = clientX - rect.left - handleWidth / 2;
    applyRatio((leftWidth / availableWidth) * 100);
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
      storeRatio(key, currentRatio);
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
      storeRatio(key, currentRatio);
    };

    document.addEventListener('touchmove', onTouchMove);
    document.addEventListener('touchend', onTouchEnd);
  });

  // ── Double-click to reset ───────────────────────────────────────

  handle.addEventListener('dblclick', () => {
    applyRatio(defaultRatio);
    storeRatio(key, currentRatio);
  });

  // ── Keyboard (arrow keys for accessibility) ─────────────────────

  handle.addEventListener('keydown', (e: KeyboardEvent) => {
    const rect = container.getBoundingClientRect();
    const availableWidth = rect.width - getHandleOuterWidth(handle);
    const minRatio = Math.min(50, (MIN_PANEL_PX / availableWidth) * 100);
    const step = 2;

    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      applyRatio(Math.max(minRatio, currentRatio - step));
      storeRatio(key, currentRatio);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      applyRatio(Math.min(100 - minRatio, currentRatio + step));
      storeRatio(key, currentRatio);
    }
  });

  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(() => {
      if (!container.isConnected) {
        observer.disconnect();
        return;
      }
      applyRatio(currentRatio);
    });
    observer.observe(container);
  }
}

export function initResizableColumns(
  root: ParentNode | Document = document,
): void {
  const containers = new Set<HTMLElement>();
  if (root instanceof HTMLElement && root.matches('[data-resizable-columns]')) {
    containers.add(root);
  }
  root.querySelectorAll<HTMLElement>('[data-resizable-columns]').forEach(
    (container) => {
      containers.add(container);
    },
  );
  containers.forEach(initResizable);
}
