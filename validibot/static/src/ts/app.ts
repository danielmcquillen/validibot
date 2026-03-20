import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';
import { initWorkflowForms } from './workflowForm';
import { initWorkflowLaunch } from './workflowLaunch';
import { initTemplateVariableEditor } from './templateVariableEditor';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
  initWorkflowForms(root);
  initWorkflowLaunch(root);
  initTemplateVariableEditor(root);
}
