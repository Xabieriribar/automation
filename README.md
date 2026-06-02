# Garage Automation - Cart-to-Invoice-Lines MVP

This repository contains a FastAPI backend deployed on Render and a Chrome extension under `chrome_extension/`.

## MVP Workflow

1. The garage user logs into a parts supplier manually.
2. The user prepares the cart manually.
3. The user clicks the Chrome extension popup on the active cart tab.
4. The extension extracts visible page text with `activeTab` + `scripting`.
5. The backend converts the text into structured lines.
6. The backend applies margin only to parts, calculates totals and Swiss VAT at 8.1%, and returns warnings instead of crashing.
7. The user reviews lines, totals, warnings, text, CSV, PDF, and optional connector status.

This is not a supplier automation agent. It does not automate login, navigate supplier sites, store supplier credentials, perform checkout, or purchase parts.

## Architecture

```text
visible cart text -> structured part/labour/fee lines -> deterministic totals -> export adapters
```

AI usage is limited to extracting candidate part lines as JSON. Totals, VAT, margin, labour, fees, CSV, and PDF are calculated by backend code.

## API

`POST /api/generate-devis` accepts:

```json
{
  "webpage_text": "visible supplier cart text",
  "license_plate": "VD 123456",
  "margin_percentage": 20,
  "client_name": "Client optional",
  "vehicle_label": "Vehicle optional",
  "operation_type": "Freins avant",
  "labor_hours": 1.5,
  "hourly_rate": 145,
  "fee_label": "Recyclage",
  "fee_amount_ht": 12,
  "export_target": "text",
  "bexio_dry_run": true
}
```

The response contains `devis`, `csv`, `pdf_base64`, `parts`, `labor`, `fees`, `totals`, `warnings`, and `exports`.

Pricing rules:

- Default VAT is 8.1%.
- Margin applies only to parts.
- Labour is optional. Blank labour inputs create no labour line.
- Fees are optional. Blank fee inputs create no fee line.
- The generic text, CSV, and PDF exports always remain available even if accounting connectors are unavailable.

Gemini is optional. If `GEMINI_API_KEY` is configured, Gemini extracts strict JSON candidate parts only. If it is missing or unavailable, the endpoint uses local regex/text fallback extraction and returns warnings.

## Export Targets

- `text`: copyable text plus CSV/PDF.
- `csv`: generic semicolon CSV with generated lines.
- `pdf`: fpdf2 generated A4 PDF.
- `bexio_draft`: server-side bexio draft invoice dry-run payload. Live creation is disabled unless backend env IDs, `BEXIO_ACCESS_TOKEN`, `ACCOUNTING_CONNECTOR_TOKEN`, matching `X-Connector-Token`, and `bexio_dry_run=false` are all present.
- `winbiz_import`: gated Winbiz DocumentImport WDX/CSV generation. It requires `WINBIZ_ADDRESS_CODE`, `WINBIZ_COLLECTIVE_ACCOUNT`, and `WINBIZ_SALES_ACCOUNT` because those are installation-specific.
- `cresus_import`: placeholder only. Official docs found do not confirm a stable invoice/devis line import API or file format for this MVP.

## Research Links

- bexio API docs: https://docs.bexio.com/
- bexio sales documents researched: `POST /2.0/kb_invoice`, `POST /2.0/kb_offer`, `POST /2.0/kb_invoice/{invoice_id}/issue`, and draft/cancel actions.
- Winbiz DocumentImport format: https://helpcenter.winbiz.ch/hc/de/articles/10681203852828-Format-f%C3%BCr-den-Dokumentenimport
- Winbiz accounting entries import: https://helpcenter.winbiz.ch/hc/en-us/articles/115001603574-EntriesImport-file
- Crésus data import: https://www.epsitec.ch/support/import-d
- Chrome MV3 `activeTab`: https://developer.chrome.com/docs/extensions/develop/concepts/activeTab
- FastAPI security first steps: https://fastapi.tiangolo.com/tutorial/security/first-steps/
- FastAPI CORS docs: https://fastapi.tiangolo.com/tutorial/cors/
- fpdf2 tutorial: https://py-pdf.github.io/fpdf2/Tutorial.html

## Local Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the API:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For local extension testing, temporarily point `API_ENDPOINT` in `chrome_extension/popup.js` to:

```text
http://localhost:8000/api/generate-devis
```

Then load `chrome_extension/` as an unpacked extension in Chrome.

## Render Deployment

`render.yaml` is a single Python web service:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health endpoint: `/health`

Required and optional environment variables are documented in `.env.example`. Google Drive and Twilio variables are still used by the existing WhatsApp/Drive webhook flow.

## Security Notes

- Supplier authentication stays manual in the browser.
- No supplier credentials, cookies, passwords, or checkout data are stored.
- The content script reads `document.body.innerText` only.
- The extension keeps `activeTab` and user-triggered extraction. It does not request broad supplier host permissions.
- Accounting API secrets are backend environment variables only and are never exposed in frontend JavaScript.
- Popup rendering uses text nodes and `textContent`, not HTML injection.
- bexio live creation is opt-in, dry-run first, and gated by a backend connector token header.

## Limitations

- Supplier pages vary. Low-confidence extraction returns warnings and still produces reviewable output.
- bexio live drafts require garage-specific bexio IDs for contacts, accounts, taxes, units, language, currency, payment type, bank account, and the private connector token.
- Winbiz WDX/CSV import requires garage-specific address/account codes. Without those, generic CSV remains available.
- Crésus invoice/devis import is not implemented because official docs found here do not provide a clear invoice-line import/API format.
- This is not a CRM, DMS, full SaaS dashboard, autonomous browser agent, or accounting replacement.

## Testing

Run:

```bash
python test_devis_mvp.py
```

Covered checks:

- fallback parsing
- margin calculation
- VAT totals
- CSV generation
- PDF smoke test
- `/api/generate-devis` response shape

---

# Twilio WhatsApp to Google Drive Webhook Middleware

A production-ready, hyper-minimalist, stateless FastAPI webhook that connects the Twilio WhatsApp API directly to the Google Drive API. This middleware is custom-designed for Swiss automotive garages to automate mechanic image uploads using a strict **"One Input, One Output"** workflow.

---

## 🚀 Core Features

*   **Zero UI Middleware**: Extremely fast, lightweight, and stateless. Designed to run seamlessly on Vercel, Render, or any VPS.
*   **Automatic Subfolder Routing**: Scans the parent folder `Garage_Dossiers` for subfolders matching the vehicle's Swiss license plate (e.g., `VD 123456`). If the subfolder doesn't exist, it creates it programmatically.
*   **A_TRAITER_SANS_PLAQUE Fallback**: Safely defaults images with no license plate caption to a dedicated processing folder.
*   **Smart Timezone Naming**: Automatically converts image timestamps to Swiss local time (`Europe/Zurich`) formatted as `photo_YYYYMMDD_HHMMSS.jpg`.
*   **Secure Twilio Media Support**: Integrates basic authentication automatically if your Twilio account has *Secure Media* downloads enabled.
*   **Valid TwiML Responses**: Sends clean, standard-compliant XML replies to Twilio, giving instant confirmation to the mechanic's WhatsApp.

---

## 🛠️ Google Cloud & Drive Setup (Critical First Step)

For the Service Account to access Google Drive, you **must** follow these three steps:

1.  **Create a Service Account**:
    *   Go to the [Google Cloud Console](https://console.cloud.google.com/).
    *   Navigate to **IAM & Admin > Service Accounts** and create a new account.
    *   Generate and download a new private key in **JSON** format.
2.  **Enable the Google Drive API**:
    *   Go to **APIs & Services > Library**.
    *   Search for **Google Drive API** and click **Enable**.
3.  **Share the Parent Folder**:
    *   Create a folder in your Google Drive named `Garage_Dossiers` (or whatever you prefer).
    *   Open your downloaded Service Account JSON key and find the `"client_email"` address (e.g., `my-service-account@project-id.iam.gserviceaccount.com`).
    *   **CRITICAL**: Share your Google Drive parent folder (`Garage_Dossiers`) with that exact Service Account email address, giving it **Editor** permissions. If you skip this, the webhook will return `404 Not Found` or access errors.
    *   Copy the ID of the parent folder from the URL bar (e.g., `https://drive.google.com/drive/folders/PARENT_FOLDER_ID`) and put it in your `.env`.

---

## ⚙️ Environment Variables

Create a `.env` file in the root of the project (inspired by `.env.example`):

```bash
# FastAPI Server Config
PORT=8000

# Twilio Credentials (Only required if Twilio "Secure Media" is enabled)
TWILIO_ACCOUNT_SID=ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
TWILIO_AUTH_TOKEN=your_twilio_auth_token_here

# Google Drive Parent Folder ID
GOOGLE_DRIVE_PARENT_FOLDER_ID=your_parent_folder_id_from_url_here

# Google Service Account JSON
# You can provide EITHER:
# A) The path to your service account credentials file:
GOOGLE_SERVICE_ACCOUNT_JSON=./credentials.json
# B) OR the raw JSON string directly (perfect for Vercel/Render env configuration):
# GOOGLE_SERVICE_ACCOUNT_JSON={"type": "service_account", ...}
```

---

## 📦 Local Setup and Installation

### 1. Install Dependencies

Ensure you have Python 3.9+ installed, then run:

```bash
pip install -r requirements.txt
```

### 2. Run the App

Start the server using `uvicorn`:

```bash
python main.py
```

The application will launch on `http://localhost:8000`. You can inspect the interactive OpenAPI documentation at `http://localhost:8000/docs`.

### 3. Exposing for Twilio testing (ngrok)

Since Twilio needs a public URL to send webhooks, expose your local server using `ngrok`:

```bash
ngrok http 8000
```

Copy the forwarding HTTPS URL (e.g., `https://xxxx-xx-xx-xx.ngrok-free.app`) and configure it in the next step.

---

## 💬 Twilio Console Configuration

1.  Log in to your [Twilio Console](https://www.twilio.com/console).
2.  Navigate to **Messaging > Try it out > Send a WhatsApp message** or your active WhatsApp sender settings.
3.  Under the Sandbox/WhatsApp Sender configuration, locate the **"A webhook URL when a message comes in"** field.
4.  Paste your ngrok URL or production URL with the endpoint path:
    ```text
    https://<your-subdomain>.ngrok-free.app/webhook/whatsapp
    ```
5.  Set the HTTP method to **POST** and save your changes.

---

## 🧪 How to Test

1.  Send a WhatsApp photo to your Twilio number.
2.  Add a caption with a Swiss license plate, e.g., `VD 123456`.
3.  You will receive a WhatsApp confirmation:
    > ✅ Photo enregistrée dans le dossier VD 123456
4.  Check your Google Drive! A new folder `VD 123456` will be created inside your parent folder, containing the uploaded photo named like `photo_20260531_144546.jpg`.
5.  Try sending a photo *without* a caption. You will receive:
    > ✅ Photo enregistrée dans le dossier A_TRAITER_SANS_PLAQUE
