import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';
import { initWorkflowLaunch } from './workflowLaunch';
import { initTemplateVariableEditor } from './templateVariableEditor';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
  initWorkflowLaunch(root);
  initTemplateVariableEditor(root);
}
