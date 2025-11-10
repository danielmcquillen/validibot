import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
}
