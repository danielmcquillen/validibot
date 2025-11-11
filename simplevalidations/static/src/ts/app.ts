import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';
import { initWorkflowLaunchForms } from './submitContentForm';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
  initWorkflowLaunchForms(root);
}
