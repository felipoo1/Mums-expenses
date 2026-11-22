# Mum's Monthly Spend

A simple phone-friendly app for Mum's monthly expenditure, reconciled to:
S Susan (Mum) · F Family food · T Transport · G Groceries · U Utilities · M Medical.
X marks anything that is not Mum's spending. Money coming in is never counted.
$500 cash for groceries is added to G every month.

How a month works:
1. **Upload**: bank or card statements, then utility bills (PDF or photos). Claude reads them.
2. **Tag**: confirm or change each line's letter. Done lines move to step 3.
3. **Reconcile**: type each document's printed total, tick every line. Stamp turns green when everything agrees.
4. **Year**: month-by-month totals for each category.

Runs on Streamlit Community Cloud. Data is stored in Supabase (tables starting with `mum_`).

Streamlit secrets needed:
```
SUPABASE_DB_URL = "postgresql://..."
ANTHROPIC_API_KEY = "sk-ant-..."
```
Optional: `APP_PIN = "1234"` adds a PIN screen.
GitHub Actions secret needed for the keep-awake job: `SUPABASE_DB_URL`.
