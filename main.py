import os
import json
import logging
from datetime import datetime
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, Form, Response, status
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import requests
from requests.auth import HTTPBasicAuth
import pytz

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Setup highly informative and clean logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("GarageWebhook")

# Load environment variables
load_dotenv()

app = FastAPI(
    title="Garage Twilio-Google Drive Webhook",
    description="Hyper-minimalist middleware connecting Twilio WhatsApp to Google Drive",
    version="1.0.0"
)

# Optional: Add CORS support just in case
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global cached drive service client
_drive_service = None

def get_drive_service():
    """
    Initializes and caches the Google Drive client.
    Supports credentials specified via a file path or a raw JSON string.
    """
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    google_creds_raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not google_creds_raw:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not configured.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    google_creds_raw = google_creds_raw.strip()

    try:
        if google_creds_raw.startswith("{"):
            # Authenticate via raw JSON string
            creds_info = json.loads(google_creds_raw)
            credentials = service_account.Credentials.from_service_account_info(
                creds_info, scopes=scopes
            )
            logger.info("Google Service Account authenticated successfully from RAW JSON string.")
        else:
            # Authenticate via JSON file path
            if not os.path.exists(google_creds_raw):
                raise FileNotFoundError(f"Service account file not found at path: {google_creds_raw}")
            credentials = service_account.Credentials.from_service_account_file(
                google_creds_raw, scopes=scopes
            )
            logger.info(f"Google Service Account authenticated successfully from file: {google_creds_raw}")

        # Disable API discovery caching to avoid permission warnings on read-only filesystems
        _drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return _drive_service
    except Exception as e:
        logger.error(f"Failed to authenticate with Google: {e}")
        raise

def get_or_create_subfolder(drive_service, parent_id: str, folder_name: str) -> str:
    """
    Searches for a subfolder matching the cleaned license plate in the parent directory.
    If it exists, returns its ID. Otherwise, programmatically creates it.
    """
    # Escape single quotes in the folder name to prevent Google Drive query breakdown
    escaped_name = folder_name.replace("'", "\\'")
    
    query = (
        f"'{parent_id}' in parents and "
        f"name = '{escaped_name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    
    logger.info(f"Querying Google Drive for folder: '{folder_name}' under parent: '{parent_id}'")
    results = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1
    ).execute()
    
    files = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
        logger.info(f"Folder matching '{folder_name}' already exists. ID: {folder_id}")
        return folder_id
        
    # If the folder doesn't exist, create it
    logger.info(f"Folder matching '{folder_name}' does not exist. Programmatically creating a new folder...")
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    
    new_folder = drive_service.files().create(
        body=folder_metadata,
        fields="id"
    ).execute()
    
    new_folder_id = new_folder.get("id")
    logger.info(f"Successfully created subfolder '{folder_name}' with ID: {new_folder_id}")
    return new_folder_id

def download_twilio_media(media_url: str) -> bytes:
    """
    Downloads media from Twilio. Supports HTTP Basic Authentication
    if secure media downloads are enabled in the Twilio console.
    """
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    auth = None
    if twilio_sid and twilio_auth_token:
        # Strip credentials in case copy-paste added extra spaces
        auth = HTTPBasicAuth(twilio_sid.strip(), twilio_auth_token.strip())
        logger.info("Configured Basic Authentication for Twilio secure media download.")
        
    logger.info(f"Downloading image asset from Twilio CDN URL: {media_url}")
    response = requests.get(media_url, auth=auth, timeout=30)
    response.raise_for_status()
    return response.content

def upload_file_to_folder(drive_service, folder_id: str, file_content: bytes, filename: str) -> str:
    """
    Streams file bytes directly to a specific Google Drive folder.
    """
    media = MediaIoBaseUpload(
        BytesIO(file_content),
        mimetype="image/jpeg",
        resumable=True
    )
    
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    
    logger.info(f"Uploading file '{filename}' to Google Drive subfolder '{folder_id}'")
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    
    file_id = uploaded_file.get("id")
    logger.info(f"Upload complete. Google Drive File ID: {file_id}")
    return file_id

def build_twiml_response(message: str) -> Response:
    """
    Generates a syntactically correct TwiML XML response.
    Twilio consumes this XML to send an automatic WhatsApp message back to the sender.
    """
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{message}</Message>
</Response>"""
    return Response(content=xml_content, media_type="application/xml")

@app.on_event("startup")
def startup_event():
    """
    Performs critical environment checks and pre-warms the Google Drive credentials.
    Allows errors to log clearly at boot time rather than on the first request.
    """
    logger.info("Initializing Twilio-Google Drive Webhook Application...")
    
    parent_folder_id = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID")
    if not parent_folder_id:
        logger.error("WARNING: GOOGLE_DRIVE_PARENT_FOLDER_ID is missing from your configuration.")
    else:
        logger.info(f"Configured Parent Drive ID: '{parent_folder_id}'")
        
    try:
        get_drive_service()
        logger.info("Google Drive service successfully initialized and authenticated!")
    except Exception as e:
        logger.error(f"WARNING: Google Drive initialization failed at startup: {e}")

@app.get("/health", tags=["Monitoring"])
async def health_check():
    """
    Basic health check endpoint for monitoring systems (UptimeRobot, Render, etc.)
    """
    status_info = {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
    try:
        get_drive_service()
        status_info["google_drive"] = "connected"
    except Exception:
        status_info["google_drive"] = "error"
    return status_info

@app.post("/webhook/whatsapp", tags=["Webhooks"])
async def webhook_whatsapp(
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    NumMedia: Optional[int] = Form(None),
    From: Optional[str] = Form(None)
):
    """
    Twilio Webhook endpoint. Handles incoming WhatsApp media messages.
    """
    logger.info(f"Incoming request from {From or 'Unknown Sender'}. Attachments detected (NumMedia): {NumMedia or 0}")
    
    # 1. Extract and sanitize the plate string (Caption)
    license_plate = "A_TRAITER_SANS_PLAQUE"
    if Body:
        cleaned_body = Body.strip()
        if cleaned_body:
            license_plate = cleaned_body.upper()
            
    logger.info(f"Extracted and sanitized folder target name: '{license_plate}'")
    
    # 2. Safety check: Ensure media URL actually exists
    if not MediaUrl0:
        logger.warning(f"No media attached to request by {From or 'unknown'}. Rejecting transaction.")
        return build_twiml_response(
            "❌ Erreur: Aucune photo détectée. Veuillez envoyer une photo avec le numéro de plaque en description."
        )
        
    try:
        # 3. Retrieve configurations
        parent_folder_id = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID")
        if not parent_folder_id:
            logger.critical("GOOGLE_DRIVE_PARENT_FOLDER_ID is not configured in .env file.")
            raise ValueError("GOOGLE_DRIVE_PARENT_FOLDER_ID environment variable is missing.")
            
        # 4. Get authenticated drive service
        drive_service = get_drive_service()
        
        # 5. Resolve folder path (create or fetch matching subfolder)
        folder_id = get_or_create_subfolder(drive_service, parent_folder_id, license_plate)
        
        # 6. Stream and download image from Twilio's CDN
        file_bytes = download_twilio_media(MediaUrl0)
        
        # 7. Generate beautiful localized Swiss time timestamp for naming
        swiss_tz = pytz.timezone("Europe/Zurich")
        now_swiss = datetime.now(swiss_tz)
        filename = now_swiss.strftime("photo_%Y%m%d_%H%M%S.jpg")
        
        # 8. Upload direct to Google Drive folder
        upload_file_to_folder(drive_service, folder_id, file_bytes, filename)
        
        # 9. Return beautiful, compliant TwiML XML to confirm
        logger.info(f"Transaction successfully processed for '{license_plate}'. Returning Twilio success response.")
        return build_twiml_response(f"✅ Photo enregistrée dans le dossier {license_plate}")
        
    except Exception as e:
        logger.exception("An unhandled exception occurred during the media synchronization process:")
        # Always reply with an automated error notice to the mechanic
        return build_twiml_response("❌ Erreur: Impossible d'enregistrer la photo. Réessayez.")

if __name__ == "__main__":
    import uvicorn
    # Pick port from environment or default to 8000
    server_port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting server in direct execution mode on port {server_port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=server_port, reload=True)
