# Where the FUCK is my stock

A web app for furniture-hire/event logistics. Upload multiple PDF quotations or ZIPs of PDFs to get:

- **Furniture booking clashes:** side-by-side job names, event dates, delivery/collection dates and times, quantities and combined units needed.
- **Furniture stock needed:** maximum simultaneous demand across every overlapping job, on-hand stock, shortfall, and which jobs need the pieces.
- **Delivery & collection calendar** and **booking details to confirm**.
- A downloadable PDF report, CSV package, and blank inventory-count template.

**No finance/discount checks.** Assesses inventory within each supplier. It does *not* automatically assume a shared stock pool between suppliers.

## Run locally

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL displayed by Streamlit (typically http://localhost:8501).

## Put it online

Upload this directory to a GitHub repository, then deploy `app.py` using [Streamlit Community Cloud](https://share.streamlit.io/) or a hosting provider that supports Python web apps. Public links and hosting are **not** created automatically by downloading the project.

## Count the stock

Download the **stock-count template** after the first run, fill `supplier,item_name,available_qty` with the actual available stock, and upload that CSV alongside your quotations next time. Without the CSV, the application displays peak demand but **cannot calculate a verified shortage**.

## Assumptions and limits

- Furniture is unavailable from delivery until collection **plus the turnaround buffer** (default 4 hours).
- 'Midnight' collection is interpreted as midnight **after** the stated event date, and flagged for confirmation.
- Quotes are not proof of confirmed bookings. Check which quotes are operationally confirmed.
- Structured/printable quotation PDFs work best. Scans require the optional OCR switch and `tesseract-ocr` installed on the server.
- The parser is tuned to the uploaded example quotation's tabular layout; other supplier layouts may need a custom parser.
- Stock item naming should be consistent; aliases can be implemented in the backend if suppliers use different product descriptions.
- User-uploaded files are processed on the server in temporary directories and deleted after the report is created. Results remain in the active Streamlit session/cache; review hosting policies before uploading sensitive business documents.
