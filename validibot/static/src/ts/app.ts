import { initCatalogFilters } from './catalogFilter';
import { initAssertionForms } from './assertionForm';
import { initWorkflowForms } from './workflowForm';
import { initWorkflowLaunch } from './workflowLaunch';
import { initTemplateVariableEditor } from './templateVariableEditor';
import { initSignalMapping } from './signalMapping';
import { initTabularSchemas } from './tabularSchema';
import { initRecaptcha } from './recaptcha';
import { initResizableColumns } from './resizableColumns';

export function initAppFeatures(root: ParentNode | Document = document): void {
  initCatalogFilters(root);
  initAssertionForms(root);
  initWorkflowForms(root);
  initWorkflowLaunch(root);
  initTemplateVariableEditor(root);
  initSignalMapping(root);
  initTabularSchemas(root);
  initResizableColumns(root);
  initRecaptcha();
}
