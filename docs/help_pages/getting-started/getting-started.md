# Getting Started

Get your first validation running in under 5 minutes.

---

## 1. Sign in and choose your workspace

Use the organization switcher in the header to select which workspace you're working in. Each organization is a separate workspace with its own workflows and team members.

## 2. Create a workflow

1. Go to **Workflows** in the sidebar
2. Click **New Workflow**
3. Fill in the basics:
   - **Name**: Something descriptive like "JSON Config Validation"
   - **Project**: Choose or create a project to organize related workflows
   - **Allowed file types**: Select the formats this workflow will accept

## 3. Add a validation step

1. From the workflow detail page, click **Add Step**
2. Select a validator:
   - **JSON Schema** — Validates structure against a schema
   - **XML Schema** — Validates XML against an XSD
   - **Basic** — Custom rules using CEL expressions
   - **AI** — Natural language validation rules
3. Configure the step settings
4. Click **Save**

## 4. Review the default assertions

Each validator comes with default assertions that always run. Click **View rules** on your step to see what's included. You can add custom assertions for stricter checks.

## 5. Activate and test

1. Toggle the workflow status to **Active**
2. Click **Launch** to open the submission dialog
3. Upload a sample file or paste content
4. Click **Run** to start validation

Watch the results appear as each step completes. If something fails, the findings will tell you exactly what went wrong.

---

## Tips

- **Start simple**: One validator, one rule. Add complexity once the basics work.
- **Test before sharing**: Run sample data through your workflow before making it available to your team.
- **Archive, don't delete**: Archiving preserves run history while hiding the workflow from daily use.
