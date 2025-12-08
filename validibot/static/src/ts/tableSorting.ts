/**
 * Client-side table sorting functionality.
 *
 * Usage:
 * Add `data-sortable-table` attribute to any table element.
 * Add `data-sortable="true"` to any <th> element that should be sortable.
 *
 * Example:
 * <table class="table" data-sortable-table>
 *   <thead>
 *     <tr>
 *       <th data-sortable="true">Name</th>
 *       <th data-sortable="true">Age</th>
 *       <th>Actions</th>
 *     </tr>
 *   </thead>
 *   <tbody>...</tbody>
 * </table>
 */

type SortDirection = 'asc' | 'desc' | '';

interface SortableHeader {
    element: HTMLTableCellElement;
    columnIndex: number;
    direction: SortDirection;
}

const SORT_ICONS = {
    unsorted: 'bi bi-arrow-down-up',
    ascending: 'bi bi-sort-alpha-down',
    descending: 'bi bi-sort-alpha-up',
} as const;

/**
 * Initialize sorting for a single table element.
 */
function initializeTableSorting(table: HTMLTableElement): void {
    const headers = Array.from(
        table.querySelectorAll<HTMLTableCellElement>('thead th[data-sortable="true"]'),
    );

    if (headers.length === 0) {
        return;
    }

    const sortableHeaders: SortableHeader[] = headers.map((element, index) => {
        // Get the actual column index in case some columns are not sortable
        const headerRow = element.parentElement as HTMLTableRowElement;
        const columnIndex = Array.from(headerRow.cells).indexOf(element);

        return {
            element,
            columnIndex,
            direction: '' as SortDirection,
        };
    });

    // Add visual indicators and click handlers
    sortableHeaders.forEach((header) => {
        const { element } = header;

        // Make header visually interactive
        element.style.cursor = 'pointer';
        element.style.userSelect = 'none';

        // Add sort icon
        const iconSpan = document.createElement('i');
        iconSpan.className = `${SORT_ICONS.unsorted} small ms-1`;
        iconSpan.setAttribute('aria-hidden', 'true');
        element.appendChild(iconSpan);

        // Add click handler
        element.addEventListener('click', () => handleHeaderClick(table, sortableHeaders, header));
    });
}

/**
 * Handle click on a sortable header.
 */
function handleHeaderClick(
    table: HTMLTableElement,
    allHeaders: SortableHeader[],
    clickedHeader: SortableHeader,
): void {
    const tbody = table.querySelector('tbody');
    if (!tbody) {
        return;
    }

    // Determine new sort direction
    const newDirection: SortDirection = clickedHeader.direction === 'asc' ? 'desc' : 'asc';

    // Reset all headers except the clicked one
    allHeaders.forEach((header) => {
        if (header !== clickedHeader) {
            header.direction = '';
            updateHeaderIcon(header.element, '');
        }
    });

    // Update clicked header
    clickedHeader.direction = newDirection;
    updateHeaderIcon(clickedHeader.element, newDirection);

    // Sort the rows
    sortTableRows(tbody, clickedHeader.columnIndex, newDirection);
}

/**
 * Update the sort icon for a header based on its direction.
 */
function updateHeaderIcon(headerElement: HTMLTableCellElement, direction: SortDirection): void {
    const icon = headerElement.querySelector('i');
    if (!icon) {
        return;
    }

    headerElement.setAttribute('data-sort-direction', direction);

    switch (direction) {
        case 'asc':
            icon.className = `${SORT_ICONS.ascending} small ms-1`;
            break;
        case 'desc':
            icon.className = `${SORT_ICONS.descending} small ms-1`;
            break;
        default:
            icon.className = `${SORT_ICONS.unsorted} small ms-1`;
            break;
    }
}

/**
 * Sort table rows by the specified column.
 */
function sortTableRows(
    tbody: HTMLTableSectionElement,
    columnIndex: number,
    direction: SortDirection,
): void {
    // Get all rows that have the expected number of cells (skip empty state rows)
    const rows = Array.from(tbody.querySelectorAll<HTMLTableRowElement>('tr')).filter(
        (row) => row.cells.length > columnIndex,
    );

    if (rows.length === 0) {
        return;
    }

    // Sort rows
    rows.sort((rowA, rowB) => {
        const cellA = rowA.cells[columnIndex];
        const cellB = rowB.cells[columnIndex];

        if (!cellA || !cellB) {
            return 0;
        }

        const textA = getCellSortValue(cellA);
        const textB = getCellSortValue(cellB);

        const comparison = textA.localeCompare(textB, undefined, { numeric: true });

        return direction === 'asc' ? comparison : -comparison;
    });

    // Re-append rows in sorted order
    rows.forEach((row) => tbody.appendChild(row));
}

/**
 * Extract the sortable value from a table cell.
 * Prefers data-sort-value attribute, falls back to text content.
 */
function getCellSortValue(cell: HTMLTableCellElement): string {
    // Check for explicit sort value
    const sortValue = cell.getAttribute('data-sort-value');
    if (sortValue !== null) {
        return sortValue;
    }

    // Fall back to text content
    return cell.textContent?.trim() || '';
}

/**
 * Initialize all sortable tables in the given root element.
 */
export function initTableSorting(root: ParentNode = document): void {
    const tables = root.querySelectorAll<HTMLTableElement>('table[data-sortable-table]');
    tables.forEach((table) => {
        // Skip if already initialized
        if (table.dataset.sortingInitialized === 'true') {
            return;
        }

        initializeTableSorting(table);
        table.dataset.sortingInitialized = 'true';
    });
}
