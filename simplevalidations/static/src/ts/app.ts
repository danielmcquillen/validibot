import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';
import { initWorkflowLaunch } from './workflowLaunch';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
  initWorkflowLaunch(root);
}
