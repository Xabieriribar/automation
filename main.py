import os
import json
import logging
import csv
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Tuple
import re
import base64

from fastapi import FastAPI, Form, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import requests
from requests.auth import HTTPBasicAuth
import pytz

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from pydantic import BaseModel, Field

# PDF Generation library imports
from fpdf import FPDF

# Setup highly informative and clean logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("GarageWebhook")

# Load environment variables
load_dotenv()


def get_configured_gemini_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    placeholder_values = {
        "your_gemini_api_key_here",
        "YOUR_GEMINI_API_KEY",
        "your-api-key",
    }
    if not api_key or api_key in placeholder_values:
        return ""
    return api_key


app = FastAPI(
    title="Garage Twilio-Google Drive Webhook",
    description="Middleware connecting Twilio WhatsApp to Google Drive and Gemini AI services",
    version="1.5.0"
)

# Enable CORS for Chrome Extension requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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
        
    logger.info(f"Downloading image/audio asset from Twilio CDN URL: {media_url}")
    response = requests.get(media_url, auth=auth, timeout=30)
    response.raise_for_status()
    return response.content

def upload_file_to_folder(drive_service, folder_id: str, file_content: bytes, filename: str, mimetype: str = "image/jpeg") -> str:
    """
    Streams file bytes directly to a specific Google Drive folder.
    Includes support for Google Shared Drives.
    """
    media = MediaIoBaseUpload(
        BytesIO(file_content),
        mimetype=mimetype,
        resumable=True
    )
    
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    
    logger.info(f"Uploading file '{filename}' ({mimetype}) to Google Drive subfolder '{folder_id}'")
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
# --- Features: Gemini AI & Twilio Integrations ---
# =====================================================================

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

def extract_swiss_plate(text: str) -> Optional[str]:
    """
    Tries to locate and clean a Swiss license plate from text.
    Swiss plates: 2 capital letters (Canton code) followed by 1 to 6 digits.
    """
    pattern = re.compile(
        r'\b(VD|GE|ZH|VS|FR|NE|JU|BE|UR|SZ|OW|NW|GL|ZG|SO|BS|BL|SH|AR|AI|SG|GR|AG|TG|TI)\s*([0-9]{1,6})\b',
        re.IGNORECASE
    )
    match = pattern.search(text)
    if match:
        canton = match.group(1).upper()
        number = match.group(2)
        return f"{canton} {number}"
    return None

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

def analyze_mechanic_message_with_gemini(text: str) -> Optional[dict]:
    """
    Calls the Google Gemini API to analyze the mechanic's message, extracting the license plate,
    checking if work/budget validation is requested, finding a potential customer phone,
    and summarizing the anomaly/problem detected.
    """
    api_key = get_configured_gemini_api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not configured in .env. Skipping Gemini AI analysis.")
        return None
        
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    prompt = f"""
    Tu es un assistant IA expert intégré au système d'un garage automobile. Ton rôle est d'analyser le message saisi par un mécanicien (contenant une photo de diagnostic) et d'en extraire des informations structurées.
    
    Message du mécanicien : "{text}"
    
    Analyse ce texte pour remplir l'objet JSON ci-dessous.
    Règles d'analyse :
    1. "is_validation_request" (boolean) : Doit être true si le message indique la détection d'une anomalie, d'une pièce usée, d'un problème sur le véhicule, d'un devis ou d'un budget qui nécessite de demander l'accord/la validation du client pour effectuer des travaux supplémentaires. Par exemple, si le mécanicien mentionne des plaquettes de frein usées, une fuite, ou écrit des mots comme "devis", "budget", "à changer", "à réparer".
    2. "license_plate" (string) : Extrais la plaque d'immatriculation suisse ou française (ex: "VD 123456", "GE 987654", "AA-123-AA"). Nettoie-la (en majuscules, espaces normaux). Si aucune plaque n'est détectable, laisse une chaîne vide "".
    3. "client_phone" (string ou null) : Si un numéro de téléphone mobile (suisse ou international, ex: 0791234567, +4179...) is présent dans le texte pour désigner le client, extrais-le. Sinon, retourne null.
    4. "detected_anomaly" (string ou null) : Résume brièvement en français la pièce ou le problème à l'origine de la demande (ex: "plaquettes de frein usées", "pneu arrière lisse", "fuite de liquide de refroidissement"). Max 4-5 mots. Si aucun problème n'est identifiable, retourne null.

    Renvoie UNIQUEMENT un objet JSON valide (sans formatage Markdown ```json, sans texte avant ni après).
    Exemple de réponse attendue :
    {{
        "is_validation_request": true,
        "license_plate": "VD 123456",
        "client_phone": "0791234567",
        "detected_anomaly": "plaquettes de frein usées"
    }}
    """
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    try:
        logger.info(f"Calling Gemini API ({model}) for message semantic analysis...")
        response = requests.post(url, headers=headers, json=payload, timeout=12)
        if response.status_code == 200:
            result = response.json()
            text_response = result['candidates'][0]['content']['parts'][0]['text']
            parsed_data = json.loads(text_response.strip())
            logger.info(f"Gemini AI successfully parsed structured data: {parsed_data}")
            return parsed_data
        else:
            logger.error(f"Gemini API returned an error status: {response.status_code}. Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to analyze message with Gemini: {e}")
        return None

def transcribe_and_summarize_audio_with_gemini(audio_bytes: bytes, mime_type: str) -> Optional[str]:
    """
    Calls the Google Gemini API using direct REST multimodal capabilities (inlineData) to transcribe
    and format a mechanic's workshop voice memo in French.
    """
    api_key = get_configured_gemini_api_key()
    if not api_key:
        logger.error("GEMINI_API_KEY is not configured in .env. Cannot process audio memo.")
        return None
        
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    # Base64 encode the audio binary payload
    base64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    
    prompt = (
        "Tu es un secrétaire d'atelier automobile expert en Suisse. Écoute cet enregistrement audio d'un mécanicien. "
        "Supprime les bruits de fond du garage, corrige la grammaire et structure proprement le texte en deux sections précises :\n"
        "- 'Véhicule' : Indique la plaque d'immatriculation suisse détectée (ex: VD 123456).\n"
        "- 'Détails des travaux' : Liste claire et professionnelle des réparations effectuées ou à prévoir. Conserve le jargon technique mécanique."
    )
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64_audio
                        }
                    }
                ]
            }
        ]
    }
    
    try:
        logger.info(f"Uploading audio payload ({len(audio_bytes)} bytes) directly to Gemini API ({model})...")
        response = requests.post(url, headers=headers, json=payload, timeout=40)
        if response.status_code == 200:
            result = response.json()
            text_response = result['candidates'][0]['content']['parts'][0]['text']
            logger.info("Gemini multimodal audio processing succeeded.")
            return text_response.strip()
        else:
            logger.error(f"Gemini API returned an error status for audio transcription: {response.status_code}. Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to transcribe workshop audio via Gemini: {e}")
        return None

def analyze_delivery_note_with_gemini(image_bytes: bytes, mime_type: str) -> Optional[str]:
    """
    Calls the Google Gemini API using direct REST multimodal capabilities (inlineData) to analyze
    and structure a delivery note or parts purchase invoice image in French.
    """
    api_key = get_configured_gemini_api_key()
    if not api_key:
        logger.error("GEMINI_API_KEY is not configured in .env. Cannot process delivery note.")
        return None
        
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    # Base64 encode the image payload
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    prompt = (
        "Tu es un assistant administratif d'atelier automobile en Suisse. Analyse cette image qui est un bon de livraison ou une facture de pièces de rechange. "
        "Extrais les informations suivantes avec une précision absolue et formate-les proprement en français :\n"
        "- 'Fournisseur' : Nom de l'entreprise qui vend les pièces (ex: Derendinger, Technomag, etc.).\n"
        "- 'Détails des pièces' : Liste claire des articles/pièces commandés avec les quantités si visibles.\n"
        "- 'Montant Total' : Prix total en CHF (si affiché, sinon mentionne 'Non spécifié').\n"
        "- 'Plaque associée' : La plaque d'immatriculation suisse concernée (ex: VD 123456)."
    )
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64_image
                        }
                    }
                ]
            }
        ]
    }
    
    try:
        logger.info(f"Sending delivery note image ({len(image_bytes)} bytes) directly to Gemini API ({model}) for vision analysis...")
        response = requests.post(url, headers=headers, json=payload, timeout=40)
        if response.status_code == 200:
            result = response.json()
            text_response = result['candidates'][0]['content']['parts'][0]['text']
            logger.info("Gemini vision analysis succeeded for delivery note.")
            return text_response.strip()
        else:
            logger.error(f"Gemini API returned error for vision analysis: {response.status_code}. Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to analyze delivery note with Gemini: {e}")
        return None

def generate_devis_with_gemini(webpage_text: str, margin_percentage: float) -> Optional[str]:
    """
    Calls the Google Gemini API to analyze raw parts catalog cart text, extract articles,
    apply a sales markup, and generate a professional Swiss repair estimate (devis) in French.
    Includes regional specifications for the Vaud/Lausanne automotive market (VAT 8.1%,
    CO Art. 375 alignment, chronological groupings, and automated service fees).
    """
    api_key = get_configured_gemini_api_key()
    if not api_key:
        logger.error("GEMINI_API_KEY is not configured in .env. Cannot generate devis.")
        return None
        
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    prompt = f"""
    Tu es un secrétaire d'atelier automobile d'élite dans le canton de Vaud (région de Lausanne, Suisse). Tu reçois le texte brut extrait d'une page de panier d'achat de pièces de rechange (Derendinger, Technomag, Oscaro, etc.).
    
    Ton travail est de générer un Devis de réparation automobile suisse professionnel en français, d'une rigueur absolue.
    
    Taux horaire de main-d'œuvre du garage à appliquer : 170 CHF HT / heure.
    
    Règles strictes de construction du Devis :
    
    1. REGROUPEMENT CHRONOLOGIQUE (Méthode Sandwich) :
       - Identifie les pièces du panier.
       - Pour chaque groupe de pièces cohérent (ex: Freins avant, Amortisseurs, Vidange/Filtres, etc.), crée une section dédiée.
       - Au début de cette section, estime de manière réaliste et ajoute la main-d'œuvre correspondante en heures décimales (ex: "Main-d'œuvre - Remplacement des freins avant : 1.2 h à 170 CHF HT/h = 204.00 CHF HT").
       - Juste en dessous de cette ligne de main-d'œuvre, liste les pièces associées trouvées dans le panier d'achat, en leur appliquant une marge bénéficiaire de {margin_percentage}% sur le prix brut HT du panier.
       
    2. FRAIS AUTOMATISÉS (Frais Annexes et Consommables) :
       Ajoute systématiquement en fin de devis une section "Frais Annexes et Consommables" comprenant :
       - "Petites fournitures" : Forfait de 15.00 CHF HT.
       - "Contribution recyclage et élimination des déchets" : Forfait de 12.00 CHF HT (si des freins, disques, amortisseurs, filtres, huiles ou liquides sont inclus dans les travaux).
       
    3. RÉSUMÉ FINANCIER DÉTAILLÉ :
       Calcule et affiche clairement la synthèse financière à la fin (en CHF) :
       - Total Main-d'œuvre HT
       - Total Pièces HT
       - Total Frais Annexes HT
       - TOTAL BRUT HT
       - Montant TVA (exactement 8.1% du TOTAL BRUT HT)
       - TOTAL À PAYER (TTC) (Somme du TOTAL BRUT HT + Montant TVA)
       
    4. MENTIONS LÉGALES OBLIGATOIRES (Vaud/Lausanne) :
       Ajoute la section suivante en pied de page :
       "Conditions et Mentions Légales :
       Ce devis est valable pour une durée de 30 jours à compter de sa date d'émission. Conformément à l'Article 375 du Code des Obligations Suisse (CO), une tolérance empirique de 10% sur le montant total estimé hors taxes est admise en cas de travaux supplémentaires imprévus nécessaires à la sécurité du véhicule."
       
    Texte brut extrait du panier d'achat :
    "{webpage_text}"
    
    Retourne uniquement le texte clair, propre et rédigé du Devis, structuré avec soin, sans aucun formatage Markdown additionnel. Les calculs mathématiques doivent être impeccables.
    """
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    
    try:
        logger.info(f"Calling Gemini API ({model}) to generate legal Swiss devis...")
        response = requests.post(url, headers=headers, json=payload, timeout=25)
        if response.status_code == 200:
            result = response.json()
            text_response = result['candidates'][0]['content']['parts'][0]['text']
            logger.info("Gemini successfully generated the Vaudois/Swiss legal devis.")
            return text_response.strip()
        else:
            logger.error(f"Gemini API returned error for devis generation: {response.status_code}. Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to generate devis with Gemini: {e}")
        return None

# =====================================================================
# --- PDF Generation: Swiss Atelier Layout Class & Helper ---
# =====================================================================

class SwissDevisPDF(FPDF):
    """
    Custom PDF class structured to match high-end A4 Swiss corporate standards.
    """
    def header(self):
        # Top brand banner
        self.set_fill_color(15, 23, 42) # Slate 900
        self.rect(0, 0, 210, 8, 'F')
        
        self.set_y(15)
        # Title of the PDF
        self.set_font('helvetica', 'B', 15)
        self.set_text_color(15, 23, 42) # Slate 900
        self.cell(0, 10, "DEVIS DE RÉPARATION AUTOMOBILE", ln=True, align='L')
        
        # Horizontal thin rule
        self.set_draw_color(226, 232, 240) # Slate 200
        self.set_line_width(0.5)
        self.line(10, 26, 200, 26)
        
        # Company Info Header Card (Right aligned)
        self.set_y(12)
        self.set_font('helvetica', 'B', 9)
        self.set_text_color(51, 65, 85) # Slate 700
        self.cell(0, 4, "Garage Automobile de Lausanne", ln=True, align='R')
        self.set_font('helvetica', '', 8)
        self.set_text_color(100, 116, 139) # Slate 500
        self.cell(0, 4, "Rue de la Gare 12, 1000 Lausanne", ln=True, align='R')
        self.cell(0, 4, "IDE/TVA: CHE-123.456.789 TVA", ln=True, align='R')
        
        self.set_y(32) # Reset writing pointer beneath headers
        
    def footer(self):
        # Keep legal footer 3 cm above the bottom of A4
        self.set_y(-28)
        self.set_font('helvetica', 'I', 7)
        self.set_text_color(100, 116, 139) # Slate 500
        
        # Fine separator rule above bottom text
        self.set_draw_color(241, 245, 249) # Slate 100
        self.set_line_width(0.5)
        self.line(10, 265, 200, 265)
        
        # Official Swiss legal compliance text
        legal_text = (
            "Conditions et Mentions Légales :\n"
            "Ce devis est valable pour une durée de 30 jours à compter de sa date d'émission. "
            "Conformément à l'Article 375 du Code des Obligations Suisse (CO), une tolérance empirique de 10% "
            "sur le montant total estimé hors taxes est admise en cas de travaux supplémentaires imprévus "
            "nécessaires à la sécurité du véhicule."
        )
        self.multi_cell(0, 3.5, legal_text, align='C')

def create_devis_pdf(devis_text: str, plate: str) -> bytes:
    """
    Parses the generated Gemini estimate text and produces a high-fidelity, beautifully styled PDF.
    Guarantees robust horizontal pointer mapping for the fpdf2 library.
    """
    # Sanitize characters outside the latin-1 encoding range of Helvetica core font
    sanitized_text = devis_text.replace("œ", "oe").replace("Œ", "Oe").replace("’", "'").replace("…", "...")
    
    pdf = SwissDevisPDF()
    pdf.set_auto_page_break(auto=True, margin=35)
    pdf.add_page()
    
    # Metadata info card (Light blue glass effect)
    pdf.set_fill_color(248, 250, 252) # Light slate 50
    pdf.set_draw_color(226, 232, 240) # Slate 200
    pdf.rect(10, 32, 190, 24, style='DF')
    
    # Write metadata texts using explicit position control
    pdf.set_y(35)
    pdf.set_font('helvetica', 'B', 10)
    pdf.set_text_color(51, 65, 85) # Slate 700
    pdf.set_x(12)
    pdf.cell(90, 5, f"VÉHICULE : {plate.upper()}")
    
    swiss_tz = pytz.timezone("Europe/Zurich")
    now_swiss = datetime.now(swiss_tz)
    date_str = now_swiss.strftime("%d.%m.%Y à %H:%M")
    pdf.set_x(102)
    pdf.cell(95, 5, f"DATE D'ÉMISSION : {date_str}", align='R')
    pdf.ln(5)
    pdf.set_x(10)
    
    pdf.set_font('helvetica', '', 9)
    pdf.set_x(12)
    pdf.cell(90, 5, "STATUT : Devis estimatif officiel")
    pdf.set_x(102)
    pdf.cell(95, 5, "LIEU : Lausanne, Suisse", align='R')
    pdf.ln(5)
    
    pdf.ln(12)
    pdf.set_x(10)
    
    # Render devis lines
    pdf.set_font('helvetica', '', 10)
    pdf.set_text_color(15, 23, 42) # Slate 900
    
    # Parse devis lines
    for line in sanitized_text.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            pdf.ln(3)
            pdf.set_x(10)
            continue
            
        # Clean redundant text blocks re-emitted by Gemini since they are rendered natively in headers/footers
        if "DEVIS" in line_stripped.upper() and ("REPARATION" in line_stripped.upper() or "AUTOMOBILE" in line_stripped.upper()):
            continue
        if "CONDITIONS ET MENTIONS LÉGALES" in line_stripped.upper() or "ARTICLE 375" in line_stripped:
            continue
        if "CE DEVIS EST VALABLE" in line_stripped.upper() or "TOLÉRANCE EMPIRIQUE" in line_stripped.upper():
            continue
        if "GARAGE AUTOMOBILE DE LAUSANNE" in line_stripped.upper():
            continue
            
        # Structure parsing for titles, listings, and sums
        if line_stripped.startswith("###") or line_stripped.startswith("##"):
            cleaned_title = re.sub(r'^[#\s]+', '', line_stripped)
            pdf.ln(4)
            pdf.set_x(10)
            pdf.set_font('helvetica', 'B', 11)
            pdf.set_text_color(37, 99, 235) # Blue 600
            pdf.cell(0, 6, cleaned_title)
            pdf.ln(6)
            pdf.set_x(10)
            pdf.set_font('helvetica', '', 10)
            pdf.set_text_color(15, 23, 42)
            pdf.ln(2)
            pdf.set_x(10)
        elif line_stripped.startswith("-") or line_stripped.startswith("*"):
            cleaned_item = re.sub(r'^[-*\s]+', '', line_stripped)
            pdf.set_font('helvetica', '', 9.5)
            pdf.set_x(10)
            pdf.multi_cell(0, 5, f"  - {cleaned_item}")
            pdf.set_font('helvetica', '', 10)
            pdf.set_x(10)
        elif any(total_kw in line_stripped.upper() for total_kw in ["TOTAL", "TVA", "À PAYER"]):
            pdf.ln(2)
            pdf.set_x(10)
            pdf.set_font('helvetica', 'B', 10.5)
            pdf.set_text_color(15, 23, 42)
            
            # Subtle grey highlight bar for totals
            _, y = pdf.get_x(), pdf.get_y()
            pdf.set_fill_color(241, 245, 249) # Light Slate 100
            pdf.rect(10, y, 190, 7, 'F')
            pdf.set_xy(10, y)
            
            pdf.cell(0, 7, f"  {line_stripped}")
            pdf.ln(7)
            pdf.set_x(10)
            pdf.set_font('helvetica', '', 10)
            pdf.ln(2)
            pdf.set_x(10)
        else:
            pdf.set_x(10)
            pdf.multi_cell(0, 5.5, line_stripped)
            pdf.set_x(10)
            
    # Output raw document bytes in memory
    return pdf.output()

# =====================================================================
# --- Twilio Features helper functions ---
# =====================================================================

def send_validation_buttons(client_number: str, plate: str, from_number: str, detected_anomaly: Optional[str] = None) -> bool:
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
    
    anomaly_part = f" ({detected_anomaly})" if detected_anomaly else ""
    body = f"Bonjour, le garage a détecté une anomalie{anomaly_part} sur votre véhicule {plate}. Veuillez valider ou refuser les réparations supplémentaires via les boutons ci-dessous :"
    
    if content_sid:
        # Use Twilio Content API template
        content_variables = json.dumps({"1": plate, "2": detected_anomaly or "une anomalie"})
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

# =====================================================================
# --- Chrome Extension /api/generate-devis API Endpoint ---
# =====================================================================

class DevisRequest(BaseModel):
    webpage_text: str = Field(default="", max_length=100000)
    license_plate: str = Field(default="", max_length=64)
    margin_percentage: Optional[float] = Field(default=20.0, ge=0, le=200)
    client_name: Optional[str] = Field(default=None, max_length=120)
    vehicle_label: Optional[str] = Field(default=None, max_length=160)
    operation_type: Optional[str] = Field(default=None, max_length=160)
    labor_hours: Optional[float] = Field(default=None, ge=0, le=100)
    hourly_rate: Optional[float] = Field(default=None, ge=0, le=1000)
    fee_label: Optional[str] = Field(default=None, max_length=120)
    fee_amount_ht: Optional[float] = Field(default=None, ge=0, le=10000)
    export_target: str = Field(default="text", max_length=32)
    bexio_dry_run: bool = True


class PartLine(BaseModel):
    reference: str = ""
    description: str
    quantity: float = 1.0
    unit_price_ht: float
    total_ht: float
    brand: str = ""
    supplier: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class LaborLine(BaseModel):
    description: str
    hours: float
    hourly_rate: float
    total_ht: float


class FeeLine(BaseModel):
    description: str
    amount_ht: float


class DevisData(BaseModel):
    devis_number: str
    date: str
    client_name: str
    vehicle_label: str
    license_plate: str
    operation_type: str
    margin_percentage: float
    parts: List[PartLine]
    labor: List[LaborLine]
    fees: List[FeeLine]
    totals: Dict[str, float]
    warnings: List[str]


class DevisResponse(BaseModel):
    devis: str
    csv: str
    pdf_base64: str
    plate: str
    parts: List[PartLine]
    labor: List[LaborLine]
    fees: List[FeeLine]
    totals: Dict[str, float]
    warnings: List[str]
    exports: Dict[str, Any]


MONEY_QUANT = Decimal("0.01")
DEFAULT_TVA_RATE = 0.081
DEFAULT_HOURLY_RATE = 145.0
DEFAULT_OPERATION_TYPE = "Travaux atelier"
SUPPORTED_EXPORT_TARGETS = {"text", "csv", "pdf", "bexio_draft", "winbiz_import", "cresus_import"}
KNOWN_SUPPLIERS = [
    "Derendinger",
    "Technomag",
    "Oscaro",
    "Autodoc",
    "ESA",
    "Mister-Auto",
    "Krautli",
    "Auto-Doc",
]
KNOWN_BRANDS = [
    "ATE",
    "Bosch",
    "Brembo",
    "TRW",
    "Sachs",
    "Valeo",
    "Mann",
    "Mahle",
    "Meyle",
    "Febi",
    "SKF",
    "NGK",
    "Denso",
    "Textar",
    "Ferodo",
    "Bilstein",
]
PRICE_RE = re.compile(
    r"(?:CHF|SFR|Fr\.?)\s*([0-9][0-9'’\s]*(?:[,.][0-9]{1,2})?)|"
    r"([0-9][0-9'’\s]*(?:[,.][0-9]{1,2})?)\s*(?:CHF|SFR|Fr\.?)",
    re.IGNORECASE,
)
REFERENCE_RE = re.compile(r"\b(?=[A-Z0-9][A-Z0-9\-/.]*\d)[A-Z0-9][A-Z0-9\-/.]{4,}\b", re.IGNORECASE)
NOISE_PRICE_WORDS = (
    "total",
    "sous-total",
    "subtotal",
    "tva",
    "vat",
    "rabais",
    "remise",
    "coupon",
    "livraison",
    "transport",
    "expedition",
    "port",
    "paiement",
)


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace("'", "").replace("’", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _clean_label(value: Optional[str], default: str) -> str:
    if not value:
        return default
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or default


def _format_chf(value: float) -> str:
    return f"CHF {_money(value):,.2f}".replace(",", "'")


def _format_qty(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _detect_supplier(text: str) -> str:
    lower_text = text.lower()
    for supplier in KNOWN_SUPPLIERS:
        if supplier.lower() in lower_text:
            return supplier
    return ""


def _parse_money_values(line: str) -> List[float]:
    if not re.search(r"(CHF|SFR|Fr\.?)", line, flags=re.IGNORECASE):
        return []
    values = []
    for match in PRICE_RE.finditer(line):
        value = _to_float(match.group(1) or match.group(2), 0.0)
        if value > 0:
            values.append(_money(value))
    return values


def _is_noise_price_line(line: str) -> bool:
    lower_line = line.lower()
    return any(word in lower_line for word in NOISE_PRICE_WORDS)


def _extract_quantity(context: str) -> float:
    patterns = [
        r"(?:quantite|quantité|qty|qte|qté)\s*[:x]?\s*([0-9]+(?:[,.][0-9]+)?)",
        r"\bx\s*([0-9]+(?:[,.][0-9]+)?)\b",
        r"\b([0-9]+(?:[,.][0-9]+)?)\s*x\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, context, flags=re.IGNORECASE)
        if match:
            quantity = _to_float(match.group(1), 1.0)
            if quantity > 0:
                return quantity
    return 1.0


def _extract_reference(context: str) -> str:
    skip_words = {"CHF", "SFR", "TOTAL", "PRIX", "QUANTITE", "QTE", "ARTICLE"}
    for candidate in REFERENCE_RE.findall(context.upper()):
        normalized = candidate.strip(".,;:")
        if normalized in skip_words:
            continue
        if re.fullmatch(r"\d+[,.]\d{1,2}", normalized):
            continue
        if normalized.isdigit() and len(normalized) < 5:
            continue
        return normalized
    return ""


def _extract_brand(context: str) -> str:
    brand_match = re.search(r"(?:marque|brand)\s*[:\-]?\s*([A-Za-z0-9\-]{2,30})", context, flags=re.IGNORECASE)
    if brand_match:
        return _clean_label(brand_match.group(1), "")
    lower_context = context.lower()
    for brand in KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(brand.lower())}\b", lower_context):
            return brand
    return ""


def _extract_description(context_lines: List[str]) -> str:
    for raw_line in reversed(context_lines):
        line = re.sub(r"\s+", " ", raw_line).strip(" -:\t")
        lower_line = line.lower()
        if not line or len(line) < 4:
            continue
        if _parse_money_values(line) or _is_noise_price_line(line):
            continue
        if re.search(r"^(quantite|quantité|qty|qte|qté)\b", lower_line):
            continue
        if re.search(r"^(reference|ref\.?|marque|brand)\b", lower_line):
            continue
        if lower_line in {"prix", "prix ht", "quantite", "quantité", "qte", "qté", "article", "panier"}:
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]", line):
            continue
        return line[:140]
    return "Piece extraite du panier"


def _coerce_part_line(raw: Dict[str, Any], supplier: str = "") -> Optional[PartLine]:
    description = _clean_label(raw.get("description"), "Piece extraite du panier")
    quantity = _to_float(raw.get("quantity"), 1.0)
    if quantity <= 0:
        quantity = 1.0
    unit_price = _to_float(raw.get("unit_price_ht"), 0.0)
    total = _to_float(raw.get("total_ht"), 0.0)
    if unit_price <= 0 and total > 0:
        unit_price = total / quantity
    if unit_price <= 0:
        return None
    confidence = _to_float(raw.get("confidence"), 0.5)
    confidence = min(1.0, max(0.0, confidence))
    return PartLine(
        reference=_clean_label(raw.get("reference"), ""),
        description=description,
        quantity=quantity,
        unit_price_ht=_money(unit_price),
        total_ht=_money(unit_price * quantity),
        brand=_clean_label(raw.get("brand"), ""),
        supplier=_clean_label(raw.get("supplier"), supplier),
        confidence=confidence,
    )


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def extract_parts_with_gemini(webpage_text: str, supplier: str) -> Tuple[List[PartLine], List[str]]:
    api_key = get_configured_gemini_api_key()
    if not api_key:
        return [], ["Gemini non configure: extraction locale par heuristiques."]

    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = f"""
Tu extrais uniquement des lignes de pieces depuis un texte visible de panier fournisseur automobile.
Retourne du JSON strict uniquement, sans Markdown, au schema:
{{
  "parts": [
    {{
      "reference": "string",
      "description": "string",
      "quantity": number,
      "unit_price_ht": number,
      "total_ht": number,
      "brand": "string",
      "supplier": "string",
      "confidence": number
    }}
  ],
  "warnings": ["string"]
}}

Regles:
- N'extrais que les pieces candidates presentes dans le texte.
- N'ajoute pas de main-d'oeuvre, TVA, marge, frais, conditions ou total final.
- Les prix doivent etre des nombres CHF HT visibles ou deduits du contexte direct.
- Ignore les totaux panier, TVA, rabais, livraison, frais de port et moyens de paiement.
- Si une information manque, utilise une chaine vide et une confiance plus basse.

Texte fournisseur:
{webpage_text[:12000]}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=20)
        if response.status_code != 200:
            logger.warning("Gemini extraction failed with HTTP %s.", response.status_code)
            return [], ["Gemini indisponible: extraction locale par heuristiques."]
        result = response.json()
        text_response = result["candidates"][0]["content"]["parts"][0]["text"]
        data = _extract_json_object(text_response)
        if not data:
            return [], ["Gemini a renvoye une reponse non JSON: extraction locale utilisee."]
        parts = []
        for raw_part in data.get("parts", []):
            if isinstance(raw_part, dict):
                part = _coerce_part_line(raw_part, supplier=supplier)
                if part:
                    parts.append(part)
        warnings = [str(item)[:180] for item in data.get("warnings", []) if item]
        return parts, warnings
    except Exception as exc:
        logger.warning("Gemini extraction failed: %s", exc)
        return [], ["Gemini indisponible: extraction locale par heuristiques."]


def extract_parts_with_fallback(webpage_text: str, supplier: str) -> Tuple[List[PartLine], List[str]]:
    warnings = []
    if not webpage_text.strip():
        return [], ["Aucun texte exploitable n'a ete extrait de l'onglet actif."]

    normalized = webpage_text.replace("\xa0", " ")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    parts: List[PartLine] = []
    seen = set()

    for index, line in enumerate(lines):
        prices = _parse_money_values(line)
        if not prices or _is_noise_price_line(line):
            continue

        context_lines = lines[max(0, index - 6):index + 1]
        context = " ".join(context_lines)
        quantity = _extract_quantity(context)
        unit_price = min(prices) if len(prices) > 1 else prices[0]
        description = _extract_description(context_lines[:-1] or context_lines)
        reference = _extract_reference(context)
        brand = _extract_brand(context)
        confidence = 0.62 if description != "Piece extraite du panier" else 0.42

        key = (reference.lower(), description.lower(), quantity, unit_price)
        if key in seen:
            continue
        seen.add(key)

        part = _coerce_part_line(
            {
                "reference": reference,
                "description": description,
                "quantity": quantity,
                "unit_price_ht": unit_price,
                "brand": brand,
                "supplier": supplier,
                "confidence": confidence,
            },
            supplier=supplier,
        )
        if part:
            parts.append(part)

    if not parts:
        warnings.append("Aucune piece n'a ete detectee automatiquement. Verifiez que l'onglet actif affiche bien le panier fournisseur.")
    elif sum(part.confidence for part in parts) / len(parts) < 0.55:
        warnings.append("Confiance d'extraction faible: controlez les pieces et les prix avant d'envoyer le devis.")

    return parts[:40], warnings


def extract_parts_from_text(webpage_text: str) -> Tuple[List[PartLine], List[str]]:
    supplier = _detect_supplier(webpage_text)
    warnings: List[str] = []
    parts: List[PartLine] = []

    if get_configured_gemini_api_key():
        parts, gemini_warnings = extract_parts_with_gemini(webpage_text, supplier)
        warnings.extend(gemini_warnings)

    if not parts:
        fallback_parts, fallback_warnings = extract_parts_with_fallback(webpage_text, supplier)
        parts = fallback_parts
        warnings.extend(fallback_warnings)

    if parts and sum(part.confidence for part in parts) / len(parts) < 0.55:
        warnings.append("Certaines lignes ont une confiance faible: confirmez le resume avant validation client.")

    return parts, list(dict.fromkeys(warnings))


def apply_margin_to_parts(parts: List[PartLine], margin_percentage: float) -> List[PartLine]:
    multiplier = 1 + (margin_percentage / 100.0)
    priced_parts = []
    for part in parts:
        unit_price = _money(part.unit_price_ht * multiplier)
        priced_parts.append(
            PartLine(
                reference=part.reference,
                description=part.description,
                quantity=part.quantity,
                unit_price_ht=unit_price,
                total_ht=_money(unit_price * part.quantity),
                brand=part.brand,
                supplier=part.supplier,
                confidence=part.confidence,
            )
        )
    return priced_parts


def build_devis_data(request: DevisRequest) -> DevisData:
    swiss_tz = pytz.timezone("Europe/Zurich")
    now_swiss = datetime.now(swiss_tz)
    warnings: List[str] = []

    plate = extract_swiss_plate(request.license_plate) or _clean_label(request.license_plate, "A_TRAITER_SANS_PLAQUE").upper()
    operation_type = _clean_label(request.operation_type, DEFAULT_OPERATION_TYPE)
    margin_percentage = float(request.margin_percentage if request.margin_percentage is not None else 20.0)
    if request.export_target not in SUPPORTED_EXPORT_TARGETS:
        warnings.append(f"Export cible inconnu '{request.export_target}': les exports texte, CSV et PDF restent disponibles.")

    raw_parts, extraction_warnings = extract_parts_from_text(request.webpage_text)
    warnings.extend(extraction_warnings)
    parts = apply_margin_to_parts(raw_parts, margin_percentage)

    labor: List[LaborLine] = []
    labor_hours = float(request.labor_hours or 0)
    if labor_hours > 0:
        hourly_rate = float(request.hourly_rate if request.hourly_rate is not None else DEFAULT_HOURLY_RATE)
        if request.hourly_rate is None:
            warnings.append(f"Taux horaire absent: le taux par defaut de {_format_chf(DEFAULT_HOURLY_RATE)} HT/h a ete utilise.")
        labor_total = _money(labor_hours * hourly_rate)
        labor.append(
            LaborLine(
                description=f"Main-d'oeuvre - {operation_type}",
                hours=_money(labor_hours),
                hourly_rate=_money(hourly_rate),
                total_ht=labor_total,
            )
        )
    elif request.hourly_rate:
        warnings.append("Taux horaire ignore: ajoutez des heures de main-d'oeuvre pour creer une ligne MO.")

    fees: List[FeeLine] = []
    fee_amount = float(request.fee_amount_ht or 0)
    if fee_amount > 0:
        fees.append(
            FeeLine(
                description=_clean_label(request.fee_label, "Frais annexes"),
                amount_ht=_money(fee_amount),
            )
        )
    elif request.fee_label:
        warnings.append("Libelle de frais ignore: ajoutez un montant HT pour creer une ligne de frais.")

    total_labor_ht = _money(sum(item.total_ht for item in labor))
    total_parts_ht = _money(sum(item.total_ht for item in parts))
    total_fees_ht = _money(sum(item.amount_ht for item in fees))
    total_ht = _money(total_labor_ht + total_parts_ht + total_fees_ht)
    tva_amount = _money(total_ht * DEFAULT_TVA_RATE)
    total_ttc = _money(total_ht + tva_amount)

    totals = {
        "total_labor_ht": total_labor_ht,
        "total_parts_ht": total_parts_ht,
        "total_fees_ht": total_fees_ht,
        "total_ht": total_ht,
        "tva_rate": DEFAULT_TVA_RATE,
        "tva_amount": tva_amount,
        "total_ttc": total_ttc,
    }

    return DevisData(
        devis_number=now_swiss.strftime("DEVIS-%Y%m%d-%H%M%S"),
        date=now_swiss.strftime("%d.%m.%Y"),
        client_name=_clean_label(request.client_name, "Client a renseigner"),
        vehicle_label=_clean_label(request.vehicle_label, "Vehicule a preciser"),
        license_plate=plate,
        operation_type=operation_type,
        margin_percentage=_money(margin_percentage),
        parts=parts,
        labor=labor,
        fees=fees,
        totals=totals,
        warnings=list(dict.fromkeys(warnings)),
    )


def build_devis_text(data: DevisData) -> str:
    lines = [
        "DEVIS DE REPARATION AUTOMOBILE",
        "",
        "Garage Automobile de Lausanne",
        "Adresse: Garage a completer, Canton de Vaud",
        "IDE/TVA: CHE-XXX.XXX.XXX TVA",
        "",
        f"No devis: {data.devis_number}",
        f"Date: {data.date}",
        f"Client: {data.client_name}",
        f"Vehicule: {data.vehicle_label}",
        f"Plaque: {data.license_plate}",
        f"Operation: {data.operation_type}",
        "",
        "1. Main-d'oeuvre",
    ]
    if data.labor:
        for line in data.labor:
            lines.append(
                f"- {line.description}: {_format_qty(line.hours)} h x {_format_chf(line.hourly_rate)} HT/h = {_format_chf(line.total_ht)} HT"
            )
    else:
        lines.append("- Aucune main-d'oeuvre ajoutee.")

    lines.extend(["", "2. Pieces"])
    if data.parts:
        lines.append(f"Prix pieces HT avec marge appliquee de {_format_qty(data.margin_percentage)}%.")
        for part in data.parts:
            reference = f"Ref. {part.reference} - " if part.reference else ""
            brand = f" ({part.brand})" if part.brand else ""
            supplier = f" - {part.supplier}" if part.supplier else ""
            lines.append(
                f"- {reference}{part.description}{brand}{supplier}: "
                f"{_format_qty(part.quantity)} x {_format_chf(part.unit_price_ht)} HT = {_format_chf(part.total_ht)} HT"
            )
    else:
        lines.append("- Aucune piece n'a pu etre extraite automatiquement du panier.")

    lines.extend(["", "3. Frais annexes et consommables"])
    if data.fees:
        for fee in data.fees:
            lines.append(f"- {fee.description}: {_format_chf(fee.amount_ht)} HT")
    else:
        lines.append("- Aucun frais annexe ajoute.")

    totals = data.totals
    lines.extend(
        [
            "",
            "Resume financier",
            f"- Total main-d'oeuvre HT: {_format_chf(totals['total_labor_ht'])}",
            f"- Total pieces HT: {_format_chf(totals['total_parts_ht'])}",
            f"- Total frais HT: {_format_chf(totals['total_fees_ht'])}",
            f"- Total HT: {_format_chf(totals['total_ht'])}",
            f"- TVA 8.1%: {_format_chf(totals['tva_amount'])}",
            f"- Total TTC: {_format_chf(totals['total_ttc'])}",
            "",
            "Conditions",
            "- Devis valable 30 jours a compter de la date d'emission.",
            "- Tout travail supplementaire depassant 10% du montant HT necessite l'accord du client.",
            "- Paiement du a la restitution du vehicule.",
        ]
    )

    if data.warnings:
        lines.extend(["", "Points a verifier"])
        for warning in data.warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def iter_invoice_export_lines(data: DevisData) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    position = 1

    for labor in data.labor:
        rows.append(
            {
                "position": position,
                "type": "labour",
                "reference": "",
                "description": labor.description,
                "quantity": labor.hours,
                "unit": "h",
                "unit_price_ht": labor.hourly_rate,
                "total_ht": labor.total_ht,
                "vat_rate": DEFAULT_TVA_RATE,
            }
        )
        position += 1

    for part in data.parts:
        rows.append(
            {
                "position": position,
                "type": "part",
                "reference": part.reference,
                "description": part.description,
                "quantity": part.quantity,
                "unit": "pc",
                "unit_price_ht": part.unit_price_ht,
                "total_ht": part.total_ht,
                "vat_rate": DEFAULT_TVA_RATE,
            }
        )
        position += 1

    for fee in data.fees:
        rows.append(
            {
                "position": position,
                "type": "fee",
                "reference": "",
                "description": fee.description,
                "quantity": 1,
                "unit": "pc",
                "unit_price_ht": fee.amount_ht,
                "total_ht": fee.amount_ht,
                "vat_rate": DEFAULT_TVA_RATE,
            }
        )
        position += 1

    return rows


def build_invoice_csv(data: DevisData) -> str:
    output = StringIO()
    fieldnames = [
        "position",
        "type",
        "reference",
        "description",
        "quantity",
        "unit",
        "unit_price_ht",
        "total_ht",
        "vat_rate",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
    writer.writeheader()
    for row in iter_invoice_export_lines(data):
        formatted = dict(row)
        formatted["quantity"] = _format_qty(float(row["quantity"]))
        formatted["unit_price_ht"] = f"{_money(row['unit_price_ht']):.2f}"
        formatted["total_ht"] = f"{_money(row['total_ht']):.2f}"
        formatted["vat_rate"] = "8.1"
        writer.writerow(formatted)
    return output.getvalue()


def _env_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_float(name: str) -> Optional[float]:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def build_bexio_draft_payload(data: DevisData) -> Tuple[Dict[str, Any], List[str]]:
    today = datetime.strptime(data.date, "%d.%m.%Y")
    missing = []
    required_ids = {
        "contact_id": _env_int("BEXIO_CONTACT_ID"),
        "user_id": _env_int("BEXIO_USER_ID"),
        "bank_account_id": _env_int("BEXIO_BANK_ACCOUNT_ID"),
        "currency_id": _env_int("BEXIO_CURRENCY_ID"),
        "payment_type_id": _env_int("BEXIO_PAYMENT_TYPE_ID"),
        "language_id": _env_int("BEXIO_LANGUAGE_ID"),
        "account_id": _env_int("BEXIO_ACCOUNT_ID"),
        "tax_id": _env_int("BEXIO_TAX_ID"),
        "unit_id": _env_int("BEXIO_UNIT_ID"),
    }
    for env_name, value in {
        "BEXIO_CONTACT_ID": required_ids["contact_id"],
        "BEXIO_USER_ID": required_ids["user_id"],
        "BEXIO_BANK_ACCOUNT_ID": required_ids["bank_account_id"],
        "BEXIO_CURRENCY_ID": required_ids["currency_id"],
        "BEXIO_PAYMENT_TYPE_ID": required_ids["payment_type_id"],
        "BEXIO_LANGUAGE_ID": required_ids["language_id"],
        "BEXIO_ACCOUNT_ID": required_ids["account_id"],
        "BEXIO_TAX_ID": required_ids["tax_id"],
        "BEXIO_UNIT_ID": required_ids["unit_id"],
    }.items():
        if value is None:
            missing.append(env_name)

    positions = []
    for row in iter_invoice_export_lines(data):
        positions.append(
            {
                "type": "KbPositionCustom",
                "amount": f"{float(row['quantity']):.6f}",
                "unit_id": required_ids["unit_id"],
                "account_id": required_ids["account_id"],
                "tax_id": required_ids["tax_id"],
                "text": row["description"],
                "unit_price": f"{_money(row['unit_price_ht']):.6f}",
                "discount_in_percent": "0.000000",
            }
        )

    payload = {
        "title": f"Garage {data.license_plate}",
        "contact_id": required_ids["contact_id"],
        "user_id": required_ids["user_id"],
        "bank_account_id": required_ids["bank_account_id"],
        "language_id": required_ids["language_id"],
        "currency_id": required_ids["currency_id"],
        "payment_type_id": required_ids["payment_type_id"],
        "header": f"Devis {data.devis_number} - {data.vehicle_label}",
        "footer": "Devis genere depuis le panier fournisseur. Merci de verifier avant emission.",
        "mwst_type": 0,
        "mwst_is_net": True,
        "show_position_taxes": True,
        "is_valid_from": today.strftime("%Y-%m-%d"),
        "is_valid_until": (today + timedelta(days=30)).strftime("%Y-%m-%d"),
        "positions": positions,
    }
    return payload, missing


def build_bexio_export(data: DevisData, request: DevisRequest, connector_token: Optional[str] = None) -> Dict[str, Any]:
    payload, missing = build_bexio_draft_payload(data)
    status_payload: Dict[str, Any] = {
        "available": not missing,
        "dry_run": True,
        "message": "Dry-run bexio draft payload generated server-side.",
        "missing_config": missing,
    }

    if request.export_target == "bexio_draft":
        status_payload["payload"] = payload

    if missing:
        status_payload["message"] = "bexio draft export needs installation-specific IDs before live creation."
        return status_payload

    if request.bexio_dry_run or request.export_target != "bexio_draft":
        return status_payload

    expected_connector_token = os.getenv("ACCOUNTING_CONNECTOR_TOKEN", "").strip()
    if not expected_connector_token or connector_token != expected_connector_token:
        status_payload["available"] = False
        status_payload["dry_run"] = True
        status_payload["message"] = "Live bexio creation requires ACCOUNTING_CONNECTOR_TOKEN and X-Connector-Token; dry-run payload only."
        return status_payload

    access_token = os.getenv("BEXIO_ACCESS_TOKEN", "").strip()
    if not access_token or access_token == "your_bexio_oauth_access_token_here":
        status_payload["available"] = False
        status_payload["message"] = "BEXIO_ACCESS_TOKEN is missing; live bexio draft creation skipped."
        status_payload["missing_config"] = ["BEXIO_ACCESS_TOKEN"]
        return status_payload

    try:
        response = requests.post(
            "https://api.bexio.com/2.0/kb_invoice",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if response.status_code != 201:
            status_payload["available"] = False
            status_payload["message"] = f"bexio returned HTTP {response.status_code}; draft creation skipped in response."
            status_payload["error"] = response.text[:500]
            return status_payload
        status_payload["dry_run"] = False
        status_payload["message"] = "bexio draft invoice created."
        status_payload["response"] = response.json()
        return status_payload
    except Exception as exc:
        logger.warning("bexio draft creation failed: %s", exc)
        status_payload["available"] = False
        status_payload["message"] = "bexio draft creation failed; local exports remain available."
        status_payload["error"] = str(exc)
        return status_payload


def build_winbiz_export(data: DevisData, request: DevisRequest) -> Dict[str, Any]:
    required = {
        "WINBIZ_ADDRESS_CODE": os.getenv("WINBIZ_ADDRESS_CODE"),
        "WINBIZ_COLLECTIVE_ACCOUNT": os.getenv("WINBIZ_COLLECTIVE_ACCOUNT"),
        "WINBIZ_SALES_ACCOUNT": os.getenv("WINBIZ_SALES_ACCOUNT"),
    }
    missing = [name for name, value in required.items() if not value]
    status_payload: Dict[str, Any] = {
        "available": False,
        "format": "Winbiz DocumentImport semicolon CSV/WDX",
        "missing_config": missing,
        "message": "Winbiz import is documented, but customer/account codes are installation-specific.",
    }
    if missing or request.export_target != "winbiz_import":
        return status_payload

    rows = []
    doc_type = os.getenv("WINBIZ_DOCUMENT_TYPE", "10")
    vat_rate = _env_float("WINBIZ_VAT_RATE") or 8.1
    for row in iter_invoice_export_lines(data):
        fields = [""] * 155
        fields[0] = "<NEW>"
        fields[1] = doc_type
        fields[2] = datetime.strptime(data.date, "%d.%m.%Y").strftime("%Y%m%d")
        fields[4] = data.devis_number
        fields[5] = f"{data.totals['total_ht']:.2f}"
        fields[10] = required["WINBIZ_COLLECTIVE_ACCOUNT"] or ""
        fields[19] = required["WINBIZ_ADDRESS_CODE"] or ""
        fields[47] = str(row["position"])
        fields[48] = "0"
        fields[50] = str(row["description"])[:250]
        fields[52] = f"{float(row['quantity']):.4f}"
        fields[53] = f"{_money(row['unit_price_ht']):.4f}"
        fields[54] = row["unit"]
        fields[56] = f"{_money(row['total_ht']):.2f}"
        fields[59] = required["WINBIZ_SALES_ACCOUNT"] or ""
        fields[60] = f"{vat_rate:.2f}"
        fields[61] = "2"
        fields[63] = "2"
        rows.append(";".join(str(value).replace(";", ",") for value in fields))

    status_payload["available"] = True
    status_payload["message"] = "Winbiz import file generated from configured Winbiz codes; test in Winbiz before production use."
    status_payload["filename"] = f"{data.devis_number}_winbiz.wdx"
    status_payload["content"] = "\r\n".join(rows) + "\r\n"
    status_payload["missing_config"] = []
    return status_payload


def build_cresus_export(data: DevisData, request: DevisRequest) -> Dict[str, Any]:
    return {
        "available": False,
        "message": "Crésus official docs found for assisted/list imports, but no clear invoice/devis line import API format was confirmed.",
        "format": None,
    }


def build_export_status(
    data: DevisData,
    request: DevisRequest,
    csv_text: str,
    pdf_base64: str,
    connector_token: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "text": {
            "available": True,
            "filename": f"{data.devis_number}.txt",
        },
        "csv": {
            "available": True,
            "filename": f"{data.devis_number}.csv",
            "delimiter": ";",
            "rows": len(iter_invoice_export_lines(data)),
        },
        "pdf": {
            "available": bool(pdf_base64),
            "filename": f"{data.devis_number}.pdf",
        },
        "bexio": build_bexio_export(data, request, connector_token),
        "winbiz": build_winbiz_export(data, request),
        "cresus": build_cresus_export(data, request),
    }


def _pdf_safe(value: Any) -> str:
    text = str(value)
    replacements = {
        "œ": "oe",
        "Œ": "Oe",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
        "\xa0": " ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _fit_pdf_text(pdf: FPDF, text: str, width: float) -> str:
    safe = _pdf_safe(text)
    if pdf.get_string_width(safe) <= width - 2:
        return safe
    while safe and pdf.get_string_width(safe + "...") > width - 2:
        safe = safe[:-1]
    return safe.rstrip() + "..."


def _pdf_section_title(pdf: FPDF, title: str):
    if pdf.get_y() > 252:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(30, 64, 175)
    pdf.cell(0, 7, _pdf_safe(title), ln=True)
    pdf.set_text_color(15, 23, 42)


def _pdf_table_header(pdf: FPDF, labels: List[str], widths: List[float]):
    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(241, 245, 249)
    pdf.set_draw_color(203, 213, 225)
    for label, width in zip(labels, widths):
        pdf.cell(width, 7, _pdf_safe(label), border=1, fill=True)
    pdf.ln()


def _pdf_table_row(pdf: FPDF, values: List[str], widths: List[float], aligns: Optional[List[str]] = None):
    if pdf.get_y() > 260:
        pdf.add_page()
    pdf.set_font("helvetica", "", 8)
    pdf.set_draw_color(226, 232, 240)
    aligns = aligns or ["L"] * len(values)
    for value, width, align in zip(values, widths, aligns):
        pdf.cell(width, 7, _fit_pdf_text(pdf, value, width), border=1, align=align)
    pdf.ln()


def create_structured_devis_pdf(data: DevisData) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()

    pdf.set_fill_color(15, 23, 42)
    pdf.rect(0, 0, 210, 10, "F")
    pdf.set_y(16)
    pdf.set_font("helvetica", "B", 16)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(0, 8, "DEVIS DE REPARATION AUTOMOBILE", ln=True)

    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(0, 5, "Garage Automobile de Lausanne - Garage a completer, Canton de Vaud", ln=True)
    pdf.cell(0, 5, "IDE/TVA: CHE-XXX.XXX.XXX TVA", ln=True)

    pdf.ln(4)
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(12, pdf.get_y(), 186, 32, "DF")
    y = pdf.get_y() + 3
    pdf.set_xy(15, y)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(30, 5, "No devis:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(65, 5, _pdf_safe(data.devis_number))
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(25, 5, "Date:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(55, 5, _pdf_safe(data.date), ln=True)

    pdf.set_x(15)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(30, 5, "Client:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(65, 5, _fit_pdf_text(pdf, data.client_name, 65))
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(25, 5, "Plaque:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(55, 5, _pdf_safe(data.license_plate), ln=True)

    pdf.set_x(15)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(30, 5, "Vehicule:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(65, 5, _fit_pdf_text(pdf, data.vehicle_label, 65))
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(25, 5, "Operation:")
    pdf.set_font("helvetica", "", 9)
    pdf.cell(55, 5, _fit_pdf_text(pdf, data.operation_type, 55), ln=True)
    pdf.ln(12)

    _pdf_section_title(pdf, "1. Main-d'oeuvre")
    labor_widths = [98, 24, 35, 28]
    _pdf_table_header(pdf, ["Description", "Heures", "Taux HT", "Total HT"], labor_widths)
    if data.labor:
        for labor in data.labor:
            _pdf_table_row(
                pdf,
                [labor.description, _format_qty(labor.hours), _format_chf(labor.hourly_rate), _format_chf(labor.total_ht)],
                labor_widths,
                ["L", "R", "R", "R"],
            )
    else:
        _pdf_table_row(pdf, ["Aucune main-d'oeuvre ajoutee", "-", "-", "-"], labor_widths)

    _pdf_section_title(pdf, "2. Pieces")
    part_widths = [26, 64, 20, 20, 25, 25]
    _pdf_table_header(pdf, ["Reference", "Designation", "Marque", "Qte", "Prix HT", "Total HT"], part_widths)
    if data.parts:
        for part in data.parts:
            _pdf_table_row(
                pdf,
                [
                    part.reference or "-",
                    part.description,
                    part.brand or "-",
                    _format_qty(part.quantity),
                    _format_chf(part.unit_price_ht),
                    _format_chf(part.total_ht),
                ],
                part_widths,
                ["L", "L", "L", "R", "R", "R"],
            )
    else:
        _pdf_table_row(pdf, ["-", "Aucune piece detectee automatiquement", "-", "-", "-", "-"], part_widths)

    _pdf_section_title(pdf, "3. Frais annexes et consommables")
    fee_widths = [145, 40]
    _pdf_table_header(pdf, ["Description", "Montant HT"], fee_widths)
    if data.fees:
        for fee in data.fees:
            _pdf_table_row(pdf, [fee.description, _format_chf(fee.amount_ht)], fee_widths, ["L", "R"])
    else:
        _pdf_table_row(pdf, ["Aucun frais annexe ajoute", "-"], fee_widths)

    _pdf_section_title(pdf, "Resume financier")
    pdf.set_font("helvetica", "", 9)
    summary_rows = [
        ("Total main-d'oeuvre HT", data.totals["total_labor_ht"]),
        ("Total pieces HT", data.totals["total_parts_ht"]),
        ("Total frais HT", data.totals["total_fees_ht"]),
        ("Total HT", data.totals["total_ht"]),
        ("TVA 8.1%", data.totals["tva_amount"]),
        ("Total TTC", data.totals["total_ttc"]),
    ]
    for label, amount in summary_rows:
        pdf.set_x(112)
        pdf.set_font("helvetica", "B" if label == "Total TTC" else "", 9)
        pdf.cell(55, 6, _pdf_safe(label), border=1)
        pdf.cell(31, 6, _format_chf(amount), border=1, align="R", ln=True)

    pdf.ln(6)
    pdf.set_font("helvetica", "I", 8)
    pdf.set_text_color(71, 85, 105)
    legal = (
        "Devis valable 30 jours. Tout travail supplementaire depassant 10% du montant HT "
        "necessite l'accord du client. Paiement du a la restitution du vehicule."
    )
    pdf.multi_cell(0, 4.5, _pdf_safe(legal))

    output = pdf.output(dest="S")
    if isinstance(output, str):
        return output.encode("latin-1")
    return bytes(output)

@app.post("/api/generate-devis", response_model=DevisResponse, tags=["Devis"])
async def api_generate_devis(request: DevisRequest, connector_token: Optional[str] = Header(default=None, alias="X-Connector-Token")):
    """
    Endpoint called by the Chrome Extension after the user clicks the popup on
    the currently active supplier/cart tab. It extracts candidate parts, applies
    deterministic garage pricing, and returns review data plus a PDF.
    """
    logger.info(f"Received devis generation request for plate: '{request.license_plate}', margin: {request.margin_percentage}%")

    data = build_devis_data(request)
    devis_text = build_devis_text(data)
    csv_text = build_invoice_csv(data)
    pdf_base64 = ""
    try:
        pdf_bytes = create_structured_devis_pdf(data)
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    except Exception:
        logger.exception("PDF generation failed for devis %s", data.devis_number)
        data.warnings.append("Le devis texte a ete genere, mais le PDF n'a pas pu etre cree.")

    exports = build_export_status(data, request, csv_text, pdf_base64, connector_token)
    target_to_export = {
        "bexio_draft": "bexio",
        "winbiz_import": "winbiz",
        "cresus_import": "cresus",
    }
    export_key = target_to_export.get(request.export_target)
    if export_key and not exports[export_key].get("available"):
        data.warnings.append(exports[export_key].get("message", "Export cible indisponible."))

    return DevisResponse(
        devis=devis_text,
        csv=csv_text,
        pdf_base64=pdf_base64,
        plate=data.license_plate,
        parts=data.parts,
        labor=data.labor,
        fees=data.fees,
        totals=data.totals,
        warnings=data.warnings,
        exports=exports,
    )

# =====================================================================
# --- Twilio Incoming WhatsApp Webhook Router ---
# =====================================================================

@app.post("/webhook/whatsapp", tags=["Webhooks"])
async def webhook_whatsapp(
    Body: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    NumMedia: Optional[int] = Form(None),
    From: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    ButtonText: Optional[str] = Form(None),
    ButtonPayload: Optional[str] = Form(None),
    MediaContentType0: Optional[str] = Form(None)
):
    """
    Twilio Webhook endpoint. Handles incoming WhatsApp media messages and client interactive button responses.
    Features:
    1. "La Dictée Atelier" - Direct voice-to-text transcribing and structured documentation into Drive.
    2. "La Capture des Bons de Livraison" - Direct delivery note/invoice image processing via Gemini Vision.
    3. "Validation Client Instantanée" - AI-based photo upload and instant validation buttons.
    """
    logger.info(f"Incoming request from {From or 'Unknown Sender'} to {To or 'Unknown Recipient'}. Body: '{Body or ''}', MediaContentType0: '{MediaContentType0 or ''}'")
    
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

    # Detect delivery note trigger keywords in Body text
    is_bon_livraison = False
    if MediaContentType0 and MediaContentType0.startswith("image/"):
        if Body:
            body_lower = Body.lower()
            if any(kw in body_lower for kw in ["bon", "livraison", "facture", "achat"]):
                is_bon_livraison = True

    # 1. Feature: "La Dictée Atelier" (Voice-to-Text Workshop Transcription)
    if MediaContentType0 and MediaContentType0.startswith("audio/"):
        logger.info("Voice message detected! Starting 'La Dictée Atelier' workflow.")
        
        if not MediaUrl0:
            logger.warning("Audio content type detected but no media URL is present. Rejecting request.")
            return build_twiml_response("❌ Erreur: Fichier audio introuvable ou non lisible.")
            
        try:
            # Download audio from Twilio CDN
            audio_bytes = download_twilio_media(MediaUrl0)
            
            # Save temporarily to /tmp/audio_input.ogg as required by prompt
            os.makedirs("/tmp", exist_ok=True)
            audio_path = "/tmp/audio_input.ogg"
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
            logger.info(f"Successfully saved voice memo temporarily to: {audio_path}")
            
            # Process with Google Gemini multimodal capabilities
            logger.info("Transcribing and structuring workshop voice memo via Gemini AI...")
            transcription = transcribe_and_summarize_audio_with_gemini(audio_bytes, MediaContentType0)
            
            if not transcription:
                logger.error("Failed to transcribe workshop audio via Gemini.")
                return build_twiml_response("❌ Erreur: Impossible de transcrire la note vocale de l'atelier.")
                
            logger.info(f"Gemini voice transcription results:\n{transcription}")
            
            # Extract Swiss license plate from transcription or text body
            plate = extract_swiss_plate(transcription)
            if not plate and Body:
                plate = extract_swiss_plate(Body)
            if not plate:
                plate = "A_TRAITER_SANS_PLAQUE"
                
            logger.info(f"Resolved license plate for the voice memo: '{plate}'")
            
            # Get authenticated Google Drive client
            drive_service = get_drive_service()
            
            parent_folder_id = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID")
            if not parent_folder_id:
                logger.critical("GOOGLE_DRIVE_PARENT_FOLDER_ID is missing from .env configuration.")
                raise ValueError("GOOGLE_DRIVE_PARENT_FOLDER_ID variable is missing.")
                
            # Create or resolve Google Drive subfolder for the vehicle
            folder_id = get_or_create_subfolder(drive_service, parent_folder_id, plate)
            
            # Upload transcription as works summary text file
            file_content_bytes = transcription.encode("utf-8")
            upload_file_to_folder(
                drive_service=drive_service,
                folder_id=folder_id,
                file_content=file_content_bytes,
                filename="travaux_atelier.txt",
                mimetype="text/plain"
            )
            
            logger.info(f"Workshop note uploaded successfully to folder '{plate}' on Google Drive.")
            
            # Send French WhatsApp confirmation back to the mechanic
            return build_twiml_response(f"📝 Note d'atelier enregistrée avec succès dans le dossier {plate}.")
            
        except Exception as e:
            logger.exception("An error occurred during 'La Dictée Atelier' processing:")
            return build_twiml_response("❌ Erreur: Échec du traitement de la note vocale d'atelier.")

    # 2. Feature: "La Capture des Bons de Livraison" (Delivery Notes & Parts Capture)
    elif MediaContentType0 and MediaContentType0.startswith("image/") and is_bon_livraison:
        logger.info("Delivery note image detected! Starting 'La Capture des Bons de Livraison' workflow.")
        
        if not MediaUrl0:
            logger.warning("Delivery note image type detected but no media URL is present. Rejecting request.")
            return build_twiml_response("❌ Erreur: Image du bon de livraison introuvable.")
            
        try:
            # Download delivery note image from Twilio CDN
            file_bytes = download_twilio_media(MediaUrl0)
            
            # Save temporarily to /tmp/delivery_note.jpg as required by prompt
            os.makedirs("/tmp", exist_ok=True)
            image_path = "/tmp/delivery_note.jpg"
            with open(image_path, "wb") as f:
                f.write(file_bytes)
            logger.info(f"Successfully saved delivery note image temporarily to: {image_path}")
            
            # Process with Google Gemini multimodal capabilities (vision)
            logger.info("Analyse du bon de livraison par Gemini...")
            extraction = analyze_delivery_note_with_gemini(file_bytes, MediaContentType0)
            
            if not extraction:
                logger.error("Failed to analyze delivery note via Gemini Vision.")
                return build_twiml_response("❌ Erreur: Impossible d'analyser le bon de livraison.")
                
            logger.info(f"Gemini Vision extraction results:\n{extraction}")
            
            # Extract Swiss license plate from Gemini output or message body
            plate = extract_swiss_plate(extraction)
            if not plate and Body:
                plate = extract_swiss_plate(Body)
            if not plate:
                plate = "A_TRAITER_SANS_PLAQUE"
                
            logger.info(f"Resolved license plate for delivery note: '{plate}'")
            
            # Get authenticated Google Drive client
            drive_service = get_drive_service()
            
            parent_folder_id = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID")
            if not parent_folder_id:
                logger.critical("GOOGLE_DRIVE_PARENT_FOLDER_ID is missing from .env configuration.")
                raise ValueError("GOOGLE_DRIVE_PARENT_FOLDER_ID variable is missing.")
                
            # Create or resolve Google Drive subfolder for the vehicle
            folder_id = get_or_create_subfolder(drive_service, parent_folder_id, plate)
            
            # Generate local timestamp for Switzerland
            swiss_tz = pytz.timezone("Europe/Zurich")
            now_swiss = datetime.now(swiss_tz)
            timestamp = now_swiss.strftime("%Y%m%d_%H%M%S")
            
            image_filename = f"bon_livraison_{timestamp}.jpg"
            text_filename = f"pieces_fournisseur_{timestamp}.txt"
            
            # 1. Upload original delivery note image file to Drive
            upload_file_to_folder(
                drive_service=drive_service,
                folder_id=folder_id,
                file_content=file_bytes,
                filename=image_filename,
                mimetype="image/jpeg"
            )
            
            # 2. Upload structured summary text file to Drive
            text_content_bytes = extraction.encode("utf-8")
            upload_file_to_folder(
                drive_service=drive_service,
                folder_id=folder_id,
                file_content=text_content_bytes,
                filename=text_filename,
                mimetype="text/plain"
            )
            
            logger.info(f"Delivery note and parts summary uploaded successfully to folder '{plate}' on Google Drive.")
            
            # Send French WhatsApp confirmation back to the mechanic
            return build_twiml_response(f"🧾 Bon de livraison et récapitulatif des pièces enregistrés avec succès dans le dossier {plate}.")
            
        except Exception as e:
            logger.exception("An error occurred during 'La Capture des Bons de Livraison' processing:")
            return build_twiml_response("❌ Erreur: Échec du traitement de la capture du bon de livraison.")

    # 3. Workflow: Photo Upload & Instant Client Validation (Standard Photo/Validation Workflow)
    else:
        # Extract and sanitize the plate string (Caption)
        license_plate = "A_TRAITER_SANS_PLAQUE"
        is_devis = False
        client_number = None
        detected_anomaly = None
        
        if Body:
            # 3a. Try Gemini Analysis first if API key is configured
            gemini_result = None
            if get_configured_gemini_api_key():
                logger.info("GEMINI_API_KEY found. Analyzing message using Gemini intermediate intelligence layer...")
                gemini_result = analyze_mechanic_message_with_gemini(Body)
                
            if gemini_result:
                is_devis = gemini_result.get("is_validation_request", False)
                license_plate = gemini_result.get("license_plate", "A_TRAITER_SANS_PLAQUE") or "A_TRAITER_SANS_PLAQUE"
                client_number = gemini_result.get("client_phone")
                detected_anomaly = gemini_result.get("detected_anomaly")
            else:
                # 3b. Rule-based Fallback (Regex + keywords)
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
                
        logger.info(f"Extracted and sanitized folder target name: '{license_plate}', is_devis: {is_devis}, client_number: {client_number}, detected_anomaly: '{detected_anomaly}'")
        
        # Safety check for photo: Ensure media URL actually exists
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
            upload_file_to_folder(drive_service, folder_id, file_bytes, filename, mimetype="image/jpeg")
            
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
                    
                    logger.info(f"Triggering instant customer validation for {client_key} on plate {license_plate} (anomaly: '{detected_anomaly}')")
                    # We send from the same Twilio number (To parameter of current request)
                    validation_sent = send_validation_buttons(
                        client_number=formatted_client,
                        plate=license_plate,
                        from_number=To or os.environ.get("TWILIO_NUMBER", ""),
                        detected_anomaly=detected_anomaly
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
