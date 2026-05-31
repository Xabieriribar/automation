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
