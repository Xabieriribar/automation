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
    Supports:
    1. OAuth 2.0 User Credentials (via refresh token, client ID, and client secret) - Recommended for personal @gmail.com accounts to bypass quota limitations.
    2. Google Service Account credentials (raw JSON string or file path) - Recommended for Workspace accounts with Shared Drives.
    """
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    scopes = ["https://www.googleapis.com/auth/drive"]

    # 1. Try OAuth 2.0 User Credentials (highly recommended for personal accounts with quota issues)
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if refresh_token and client_id and client_secret:
        try:
            from google.oauth2.credentials import Credentials
            credentials = Credentials(
                token=None,
                refresh_token=refresh_token.strip(),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id.strip(),
                client_secret=client_secret.strip(),
                scopes=scopes
            )
            logger.info("Google OAuth 2.0 User Credentials authenticated successfully! Acting on behalf of personal account.")
            _drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            return _drive_service
        except Exception as e:
            logger.error(f"Failed to authenticate with OAuth 2.0 User Credentials: {e}")
            raise

    # 2. Fallback to Google Service Account
    google_creds_raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not google_creds_raw:
        raise ValueError(
            "Neither Google OAuth credentials (GOOGLE_REFRESH_TOKEN) nor "
            "Service Account credentials (GOOGLE_SERVICE_ACCOUNT_JSON) are configured."
        )

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
        logger.error(f"Failed to authenticate with Google Service Account: {e}")
        raise

def get_or_create_subfolder(drive_service, parent_id: str, folder_name: str) -> str:
    """
    Searches for a subfolder matching the cleaned license plate in the parent directory.
    If it exists, returns its ID. Otherwise, programmatically creates it.
    Includes support for Google Shared Drives.
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
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
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
        fields="id",
        supportsAllDrives=True
    ).execute()
    
    new_folder_id = new_folder.get("id")
    logger.info(f"Successfully created subfolder '{folder_name}' with ID: {new_folder_id}")
    return new_folder_id

def download_twilio_media(media_url: str) -> bytes:
    """
    Downloads media from Twilio using HTTP Basic Authentication with Twilio credentials.
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    
    # Strip whitespace in case of copy-paste/env loading trailing spaces
    if account_sid:
        account_sid = account_sid.strip()
    if auth_token:
        auth_token = auth_token.strip()
        
    # We must explicitly use HTTP Basic Authentication using Twilio credentials
    auth = None
    if account_sid and auth_token:
        auth = (account_sid, auth_token)
        logger.info("Configured HTTP Basic Authentication using Twilio credentials.")
    else:
        logger.warning("Twilio credentials not fully set. Proceeding without auth (Secure Media must be disabled).")
        
    logger.info(f"Downloading image asset from Twilio CDN URL: {media_url}")
    response = requests.get(media_url, auth=auth, timeout=30)
    response.raise_for_status()
    return response.content

def upload_file_to_folder(drive_service, folder_id: str, file_content: bytes, filename: str) -> str:
    """
    Streams file bytes directly to a specific Google Drive folder.
    Includes support for Google Shared Drives.
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
        fields="id",
        supportsAllDrives=True
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

# =====================================================================
# --- Feature: Validation Client Instantanée (Approbation Budget 1-Clic) ---
# =====================================================================
import re

try:
    from twilio.rest import Client as TwilioClient
    HAS_TWILIO_SDK = True
except ImportError:
    HAS_TWILIO_SDK = False
    logger.warning("Twilio SDK is not installed. Falling back to direct REST API calls via requests.")

# In-memory session store for pending client validations
# Keys: client_number (e.g. "whatsapp:+41791234567")
# Values: {"plate": "VD 123456", "garage_number": "whatsapp:+41797654321"}
PENDING_VALIDATIONS = {}

def format_to_e164(phone_number: str) -> str:
    """
    Cleans and formats a phone number to E.164.
    If it starts with 0, we assume it's a Swiss number (+41) since timezone is Europe/Zurich.
    """
    cleaned = re.sub(r'[^\d+]', '', phone_number)
    if cleaned.startswith('+'):
        return cleaned
    if cleaned.startswith('0'):
        return '+41' + cleaned[1:]
    return cleaned

def send_whatsapp_message(to_number: str, body_content: str, from_number: str, content_sid: Optional[str] = None, content_variables: Optional[str] = None) -> Optional[str]:
    """
    Sends a WhatsApp message using either the Twilio SDK (if available) or direct REST API requests.
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    
    if not account_sid or not auth_token:
        logger.error("Twilio credentials missing in environment variables. Cannot send WhatsApp message.")
        return None
        
    formatted_to = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
    formatted_from = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
    
    # Try using Twilio SDK if available
    if HAS_TWILIO_SDK:
        try:
            client = TwilioClient(account_sid, auth_token)
            if content_sid:
                message = client.messages.create(
                    to=formatted_to,
                    from_=formatted_from,
                    content_sid=content_sid,
                    content_variables=content_variables
                )
            else:
                message = client.messages.create(
                    to=formatted_to,
                    from_=formatted_from,
                    body=body_content
                )
            logger.info(f"WhatsApp message successfully sent via SDK. Message SID: {message.sid}")
            return message.sid
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message via SDK: {e}. Trying REST API fallback.")
            
    # Fallback to direct HTTP request using requests
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {
        "To": formatted_to,
        "From": formatted_from,
    }
    
    if content_sid:
        data["ContentSid"] = content_sid
        if content_variables:
            data["ContentVariables"] = content_variables
    else:
        data["Body"] = body_content
        
    try:
        response = requests.post(url, data=data, auth=HTTPBasicAuth(account_sid, auth_token), timeout=15)
        if response.status_code in [200, 201]:
            res_json = response.json()
            logger.info(f"WhatsApp message successfully sent via REST API. Message SID: {res_json.get('sid')}")
            return res_json.get('sid')
        else:
            logger.error(f"Failed to send WhatsApp message via REST API. Status: {response.status_code}, Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error during REST API fallback call: {e}")
        return None

def send_validation_buttons(client_number: str, plate: str, from_number: str) -> bool:
    """
    Sends interactive buttons (Quick Replies) or fallback interactive instructions to the client.
    """
    target_number = client_number
    if not target_number:
        target_number = os.getenv("DEBUG_CLIENT_NUMBER")
        if not target_number:
            logger.error("No client number provided and DEBUG_CLIENT_NUMBER is not set.")
            return False
            
    target_number = format_to_e164(target_number)
    content_sid = os.getenv("TWILIO_CONTENT_SID")
    
    body = f"Bonjour, le garage a détecté une anomalie sur votre véhicule {plate}. Veuillez valider ou refuser les réparations supplémentaires via les boutons ci-dessous :"
    
    if content_sid:
        # Use Twilio Content API template
        content_variables = json.dumps({"1": plate})
        logger.info(f"Sending interactive WhatsApp buttons via Template {content_sid} to client {target_number}")
        sid = send_whatsapp_message(
            to_number=target_number,
            body_content="",
            from_number=from_number,
            content_sid=content_sid,
            content_variables=content_variables
        )
        return sid is not None
    else:
        # Fallback to interactive text message
        fallback_body = (
            f"{body}\n\n"
            f"👉 Répondez directement par :\n"
            f"1️⃣ *Autoriser les travaux*\n"
            f"2️⃣ *Refuser*"
        )
        logger.info(f"Sending fallback WhatsApp interactive text to client {target_number}")
        sid = send_whatsapp_message(
            to_number=target_number,
            body_content=fallback_body,
            from_number=from_number
        )
        return sid is not None

@app.post("/webhook/whatsapp", tags=["Webhooks"])
async def webhook_whatsapp(
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    NumMedia: Optional[int] = Form(None),
    From: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    ButtonText: Optional[str] = Form(None),
    ButtonPayload: Optional[str] = Form(None)
):
    """
    Twilio Webhook endpoint. Handles incoming WhatsApp media messages and client interactive button responses.
    """
    logger.info(f"Incoming request from {From or 'Unknown Sender'} to {To or 'Unknown Recipient'}. Body: '{Body or ''}'")
    
    # 0. Check if the message is a client interactive response to a budget request
    client_response = None
    if ButtonText:
        client_response = ButtonText.strip()
    elif Body:
        # Check if the Body matches one of the button texts
        body_cleaned = Body.strip().lower()
        if "autoriser les travaux" in body_cleaned or body_cleaned == "refuser":
            client_response = Body.strip()
            
    if client_response and From:
        logger.info(f"Client interactive response detected: '{client_response}' from {From}")
        # Look up in our in-memory session store
        session = PENDING_VALIDATIONS.get(From)
        if session:
            plate = session.get("plate")
            garage_number = session.get("garage_number")
            
            # Send WhatsApp confirmation to the garage manager
            if "autoriser" in client_response.lower():
                manager_message = f"✅ Le client a AUTORISÉ les travaux pour le véhicule {plate}."
            else:
                manager_message = f"❌ Le client a REFUSÉ les travaux pour le véhicule {plate}."
                
            # Send the message to the manager from the Twilio number
            logger.info(f"Relaying client decision to manager {garage_number}")
            send_whatsapp_message(
                to_number=garage_number,
                body_content=manager_message,
                from_number=To or os.environ.get("TWILIO_NUMBER", "")
            )
            
            # Clean up the session
            PENDING_VALIDATIONS.pop(From, None)
            
            # Respond to the client to confirm we received their choice
            client_reply = "Merci pour votre réponse. Elle a bien été transmise au garage."
            return build_twiml_response(client_reply)
        else:
            logger.warning(f"Received client response '{client_response}' from {From} but no pending session found.")
            return build_twiml_response("Merci pour votre réponse. Aucune demande en attente n'a été trouvée pour ce numéro.")

    # 1. Extract and sanitize the plate string (Caption)
    license_plate = "A_TRAITER_SANS_PLAQUE"
    is_devis = False
    client_number = None
    
    if Body:
        # Extract potential phone number
        phone_matches = re.findall(r'(\+?[0-9][0-9\s\-\.]{7,15}[0-9])', Body)
        temp_body = Body
        for match in phone_matches:
            # Clean and validate if it looks like a phone number
            cleaned_phone = re.sub(r'[\s\-\.]', '', match)
            if len(cleaned_phone) >= 9 and len(cleaned_phone) <= 15 and (cleaned_phone.startswith('+') or cleaned_phone.startswith('0')):
                client_number = cleaned_phone
                # Remove this phone number from the plate string
                temp_body = temp_body.replace(match, "")
                break
                
        # Detect and clean keywords "devis" or "budget"
        body_lower = temp_body.lower()
        if "devis" in body_lower or "budget" in body_lower:
            is_devis = True
            for kw in ["devis", "budget"]:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(kw), re.IGNORECASE)
                temp_body = pattern.sub("", temp_body)
                
        # Clean remaining characters as the license plate
        temp_plate = temp_body.strip()
        temp_plate = " ".join(temp_plate.split()).upper()
        if temp_plate:
            license_plate = temp_plate
            
    logger.info(f"Extracted and sanitized folder target name: '{license_plate}', is_devis: {is_devis}, client_number: {client_number}")
    
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
        
        # 8b. If it was a devis/budget request, send interactive validation message to client
        validation_sent = False
        if is_devis and From:
            target_client_number = client_number or os.getenv("DEBUG_CLIENT_NUMBER")
            if target_client_number:
                formatted_client = format_to_e164(target_client_number)
                client_key = formatted_client if formatted_client.startswith("whatsapp:") else f"whatsapp:{formatted_client}"
                
                # Store in session (both From and To numbers are tracked to handle reply)
                PENDING_VALIDATIONS[client_key] = {
                    "plate": license_plate,
                    "garage_number": From
                }
                
                logger.info(f"Triggering instant customer validation for {client_key} on plate {license_plate}")
                # We send from the same Twilio number (To parameter of current request)
                validation_sent = send_validation_buttons(
                    client_number=formatted_client,
                    plate=license_plate,
                    from_number=To or os.environ.get("TWILIO_NUMBER", "")
                )
        
        # 9. Return beautiful, compliant TwiML XML to confirm
        logger.info(f"Transaction successfully processed for '{license_plate}'. Returning Twilio success response.")
        if is_devis:
            if validation_sent:
                return build_twiml_response(f"✅ Photo enregistrée et demande de validation envoyée au client pour le véhicule {license_plate}.")
            else:
                return build_twiml_response(f"✅ Photo enregistrée pour le véhicule {license_plate}, mais l'envoi de la validation client a échoué (vérifiez le numéro client et la configuration).")
                
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
