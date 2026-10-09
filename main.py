import hashlib
import io
import json
import os
import re
from typing import Any, Dict

from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import httpx
from starlette.middleware.sessions import SessionMiddleware

# Zagruzkayem peremennye okruzheniya iz .env fayla
load_dotenv()

app = FastAPI()

SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key-12345")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Konfiguraciya klyuchey cherez os.environ
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

DA_CLIENT_ID = os.environ.get("DA_CLIENT_ID", "21741")
DA_CLIENT_SECRET = os.environ.get("DA_CLIENT_SECRET", "")
DA_REDIRECT_URI = "http://127.0.0.1:8000/auth/donationalerts/callback"

# FREEKASSA CONFIG
FK_MERCHANT_ID = os.environ.get("FK_MERCHANT_ID", "21741")
FK_SECRET1 = os.environ.get("sk-1", os.environ.get("FK_SECRET1", ""))
FK_SECRET2 = os.environ.get("sk-2", os.environ.get("FK_SECRET2", ""))

oauth = OAuth()

# Google OAuth
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# GitHub OAuth
oauth.register(
    name="github",
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    access_token_url="https://github.com/login/oauth/access_token",
    access_token_params=None,
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
)

# ==========================================
# 1. BAZA DANNYKH (bd.txt)
# ==========================================
DB_FILE = "bd.txt"


class TextDatabase:

  def __init__(self, filepath: str = DB_FILE):
    self.filepath = filepath
    if not os.path.exists(self.filepath):
      with open(self.filepath, "w", encoding="utf-8") as f:
        f.write("")

  def load_all(self) -> Dict[str, Dict[str, Any]]:
    users = {}
    if not os.path.exists(self.filepath):
      return users

    with open(self.filepath, "r", encoding="utf-8") as f:
      content = f.read()

    pattern = (
        r"(\S+@\S+)\s*\[\s*rate:\s*(\S+)\s*files:\s*(\d+)\s*helped:\s*(\S+)\s*\]"
    )
    matches = re.findall(pattern, content)

    for email, rate, files, helped in matches:
      users[email] = {
          "rate": rate,
          "files": int(files),
          "helped": helped.lower() == "true",
      }
    return users

  def get_user(self, email: str) -> Dict[str, Any]:
    users = self.load_all()
    if email not in users:
      self.save_user(email, rate="free", files=0, helped=False)
      return {"rate": "free", "files": 0, "helped": False}
    return users[email]

  def save_user(self, email: str, rate: str, files: int, helped: bool):
    users = self.load_all()
    users[email] = {"rate": rate, "files": files, "helped": helped}
    self._write_all(users)

  def _write_all(self, users: Dict[str, Dict[str, Any]]):
    with open(self.filepath, "w", encoding="utf-8") as f:
      for email, data in users.items():
        f.write(f"{email} [\n")
        f.write(f"rate: {data['rate']}\n")
        f.write(f"files: {data['files']}\n")
        f.write(f"helped: {'true' if data['helped'] else 'false'}\n")
        f.write("]\n\n")


db = TextDatabase()

# ==========================================
# 2. GOOGLE DRIVE INTEGRATION
# ==========================================
SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRETS_FILE = "client_secret.json"
GOOGLE_DRIVE_FOLDER_ID = "1rLqz7WIMqq4piCmuMbs3v4jTGk1IY9KM"


def get_drive_service():
  creds = None
  token_json_str = os.environ.get("GOOGLE_TOKEN_JSON")

  if token_json_str:
    try:
      token_data = json.loads(token_json_str)
      creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except Exception as e:
      print(f"Oshibka parsina GOOGLE_TOKEN_JSON iz .env: {e}")

  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      try:
        creds.refresh(GoogleRequest())
      except Exception as e:
        print(f"Oshibka obnovleniya tokena: {e}")
        creds = None

  if not creds:
    print("Ne udalos initsializirovat Google Credentials!")
    return None

  try:
    return build("drive", "v3", credentials=creds)
  except Exception as e:
    print(f"Oshibka zapuska Google Drive API: {e}")
    return None


PLANS = {
    "free": {"name": "Bazovyy", "bytes": 1 * 1024 * 1024 * 1024, "label": "1 GB"},
    "izuchu": {
        "name": "Izuchu",
        "bytes": 15 * 1024 * 1024 * 1024,
        "label": "15 GB",
    },
}


def get_file_visibility_mode(permissions: list) -> str:
  for perm in permissions:
    if perm.get("type") == "anyone":
      if perm.get("allowFileDiscovery"):
        return "public"
      return "link"
  return "restricted"


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
  user = request.session.get("user")

  if not user:
    return """
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>CloudIX — Vkhod</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen p-4">
            <div class="bg-slate-900 p-8 rounded-2xl border border-slate-800 shadow-2xl text-center max-w-md w-full space-y-4">
                <div class="w-16 h-16 bg-blue-600 rounded-2xl mx-auto flex items-center justify-center text-2xl font-bold mb-4 shadow-lg shadow-blue-500/30">☁️</div>
                <h1 class="text-2xl font-bold mb-1">CloudIX</h1>
                <p class="text-slate-400 text-sm mb-6">Khranilishche faylov v Google Drive.</p>
                
                <a href="/login/google" class="w-full inline-block bg-white text-slate-950 font-semibold py-3 px-4 rounded-xl hover:bg-slate-200 transition shadow-md">
                    Voyti cherez Google
                </a>
                <a href="/login/github" class="w-full inline-block bg-slate-800 text-white font-semibold py-3 px-4 rounded-xl hover:bg-slate-700 transition border border-slate-700 shadow-md">
                    Voyti cherez GitHub
                </a>
                <a href="/login/donationalerts" class="w-full inline-block bg-orange-600 text-white font-semibold py-3 px-4 rounded-xl hover:bg-orange-500 transition shadow-md">
                    Voyti cherez DonationAlerts
                </a>
            </div>
        </body>
        </html>
        """

  user_email = user.get("email", "unknown@user")
  provider = request.session.get("provider", "OAuth")

  user_db_data = db.get_user(user_email)
  plan_key = user_db_data.get("rate", "free")

  current_plan = PLANS.get(plan_key, PLANS["free"])
  max_storage = current_plan["bytes"]
  limit_label = current_plan["label"]

  status_param = request.query_params.get("status")
  status_notice = ""
  if status_param == "success":
    status_notice = '<div class="bg-emerald-500/20 border border-emerald-500/50 text-emerald-300 p-4 rounded-xl text-xs mb-4"><b>Oplata uspeshno proviedena!</b> Tarif obnovlen.</div>'
  elif status_param == "fail":
    status_notice = '<div class="bg-red-500/20 border border-red-500/50 text-red-300 p-4 rounded-xl text-xs mb-4"><b>Oshibka oplaty ili otmena.</b> Poprobuyte snova.</div>'

  drive_service = get_drive_service()
  user_files = []
  total_used_bytes = 0
  error_notice = ""

  if drive_service:
    try:
      query = f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and appProperties has {{ key='owner' and value='{user_email}' }} and trashed = false"
      results = (
          drive_service.files()
          .list(q=query, fields="files(id, name, size, permissions)")
          .execute()
      )
      items = results.get("files", [])

      for item in items:
        file_size = int(item.get("size", 0))
        total_used_bytes += file_size
        size_str = (
            f"{round(file_size / (1024 * 1024), 2)} MB"
            if file_size > 1024 * 1024
            else f"{round(file_size / 1024, 1)} KB"
        )
        visibility = get_file_visibility_mode(item.get("permissions", []))

        user_files.append({
            "id": item["id"],
            "name": item["name"],
            "size": size_str,
            "visibility": visibility,
        })

      db.save_user(
          user_email,
          rate=plan_key,
          files=len(user_files),
          helped=user_db_data["helped"],
      )
    except Exception as e:
      error_notice = f'<div class="bg-red-500/20 border border-red-500/50 text-red-300 p-4 rounded-xl text-xs mb-4"><b>Oshibka Google Drive API:</b> {str(e)}</div>'
  else:
    error_notice = '<div class="bg-amber-500/20 border border-amber-500/50 text-amber-300 p-4 rounded-xl text-xs mb-4">Google Drive API ne nastroyen.</div>'

  used_mb = round(total_used_bytes / (1024 * 1024), 2)
  percent = (
      min(round((total_used_bytes / max_storage) * 100), 100)
      if max_storage > 0
      else 0
  )

  files_html = ""
  base_url = str(request.base_url).rstrip("/")

  for f in user_files:
    download_url = f"{base_url}/download/{f['id']}"

    if f["visibility"] == "public":
      badge = '<span class="bg-emerald-500/20 text-emerald-400 text-[10px] px-2 py-0.5 rounded-full font-bold">🌐 Publichnyy</span>'
    elif f["visibility"] == "link":
      badge = '<span class="bg-amber-500/20 text-amber-400 text-[10px] px-2 py-0.5 rounded-full font-bold">🔗 Po ssylke</span>'
    else:
      badge = '<span class="bg-slate-700 text-slate-300 text-[10px] px-2 py-0.5 rounded-full font-bold">🔒 Privatnyy</span>'

    files_html += f"""
        <div class="bg-slate-800/50 border border-slate-700/50 p-4 rounded-xl mb-3 hover:bg-slate-800/80 transition space-y-3">
            <div class="flex items-center justify-between">
                <div class="truncate mr-2">
                    <div class="flex items-center space-x-2">
                        <span class="font-medium text-sm text-slate-200">{f['name']}</span>
                        {badge}
                    </div>
                    <span class="text-xs text-slate-400">Razmer: {f['size']}</span>
                </div>
                <div class="flex items-center space-x-2 shrink-0">
                    <a href="/download/{f['id']}" class="bg-blue-600 hover:bg-blue-500 text-white text-xs px-3 py-1.5 rounded-lg transition font-medium">Skachat</a>
                    <a href="/delete/{f['id']}" class="bg-red-500/20 hover:bg-red-500/30 text-red-400 text-xs px-3 py-1.5 rounded-lg transition font-medium">Udalit</a>
                </div>
            </div>
            
            <div class="flex items-center justify-between pt-2 border-t border-slate-700/40 text-xs gap-2 flex-wrap">
                <form action="/visibility/{f['id']}" method="post" class="flex items-center space-x-2">
                    <label class="text-slate-400 text-[11px]">Dostup:</label>
                    <select name="mode" onchange="this.form.submit()" class="bg-slate-900 border border-slate-700 text-slate-200 rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-blue-500">
                        <option value="restricted" {"selected" if f['visibility'] == 'restricted' else ""}>🔒 Privatnyy</option>
                        <option value="link" {"selected" if f['visibility'] == 'link' else ""}>🔗 Po ssylke</option>
                        <option value="public" {"selected" if f['visibility'] == 'public' else ""}>🌐 Publichnyy</option>
                    </select>
                </form>
                
                <button onclick="navigator.clipboard.writeText('{download_url}'); alert('Ssylka skopirovana!');" class="text-slate-400 hover:text-blue-400 text-[11px] underline flex items-center gap-1">
                    📋 Kopirovat pryamuyu ssylku
                </button>
            </div>
        </div>
        """

  if not user_files and not error_notice:
    files_html = '<div class="text-center text-slate-500 py-8 text-sm">U vas poka net zagruzhennykh faylov v Google Drive</div>'

  plan_badge = (
      f'<span class="bg-emerald-500/20 text-emerald-400 text-xs px-2.5 py-1'
      f' rounded-full font-bold ml-2">TARIF: {current_plan["name"].upper()}'
      f" ({limit_label})</span>"
      if plan_key != "free"
      else (
          '<span class="bg-slate-800 text-slate-400 text-xs px-2.5 py-1'
          ' rounded-full font-bold ml-2">BAZOVYY (1 GB)</span>'
      )
  )

  user_avatar = user.get("picture") or "https://via.placeholder.com/48"
  user_name = user.get("name") or user_email.split("@")[0]

  return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>CloudIX — Panel upravleniya</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-950 text-slate-100 min-h-screen py-10 px-4">
        <div class="max-w-2xl mx-auto space-y-6">
            {status_notice}
            {error_notice}

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl flex items-center justify-between">
                <div class="flex items-center space-x-4">
                    <img src="{user_avatar}" class="w-12 h-12 rounded-full border-2 border-blue-500">
                    <div>
                        <div class="flex items-center flex-wrap gap-y-1">
                            <h2 class="font-bold text-lg">{user_name}</h2>
                            {plan_badge}
                        </div>
                        <p class="text-xs text-slate-400 mt-1">{user_email} (vkhod: {provider})</p>
                    </div>
                </div>
                <a href="/logout" class="bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs px-4 py-2 rounded-xl transition font-medium">Vyyti</a>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl space-y-4">
                <h3 class="font-semibold text-sm text-slate-300 uppercase tracking-wider">Vybor Tarifa / Pokupka</h3>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <form action="/select-plan" method="post" class="bg-slate-800/60 p-4 rounded-xl border border-slate-700 flex flex-col justify-between">
                        <input type="hidden" name="plan" value="free">
                        <div>
                            <div class="font-bold text-base text-slate-200">Bazovyy (Free)</div>
                            <div class="text-xs text-slate-400 mt-1">1 GB diskovogo prostranstva</div>
                        </div>
                        <button type="submit" {"disabled" if plan_key == "free" else ""} class="mt-4 w-full text-xs py-2 px-3 rounded-lg font-semibold {"bg-slate-700 text-slate-500 cursor-not-allowed" if plan_key == "free" else "bg-blue-600 hover:bg-blue-500 text-white transition"}">
                            {"Tekushchiy tarif" if plan_key == "free" else "Vybrakh Bazovyy"}
                        </button>
                    </form>

                    <form action="/select-plan" method="post" class="bg-slate-800/60 p-4 rounded-xl border border-emerald-500/30 flex flex-col justify-between">
                        <input type="hidden" name="plan" value="izuchu">
                        <div>
                            <div class="font-bold text-base text-emerald-400 flex items-center justify-between">
                                Izuchu
                                <span class="text-[10px] bg-emerald-500/20 text-emerald-300 px-2 py-0.5 rounded-full font-normal">PRO</span>
                            </div>
                            <div class="text-xs text-slate-400 mt-1">15 GB diskovogo prostranstva (Kupit)</div>
                        </div>
                        <button type="submit" {"disabled" if plan_key == "izuchu" else ""} class="mt-4 w-full text-xs py-2 px-3 rounded-lg font-semibold {"bg-slate-700 text-slate-500 cursor-not-allowed" if plan_key == "izuchu" else "bg-emerald-600 hover:bg-emerald-500 text-white transition shadow-lg shadow-emerald-500/20"}">
                            {"Tekushchiy tarif" if plan_key == "izuchu" else "Oplatit (FreeKassa)"}
                        </button>
                    </form>
                </div>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl space-y-3">
                <div class="flex justify-between text-sm">
                    <span class="text-slate-400">Ispolzovano mesta (Google Drive)</span>
                    <span class="font-semibold">{used_mb} MB iz {limit_label} ({percent}%)</span>
                </div>
                <div class="w-full bg-slate-800 h-3 rounded-full overflow-hidden">
                    <div class="bg-blue-600 h-full transition-all duration-500" style="width: {percent}%;"></div>
                </div>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl">
                <h3 class="font-semibold mb-4 text-sm text-slate-300 uppercase tracking-wider">Zagruzit fayl v Google Drive</h3>
                <form action="/upload" method="post" enctype="multipart/form-data" class="space-y-4">
                    <div class="border-2 border-dashed border-slate-700 hover:border-blue-500 rounded-xl p-6 text-center transition cursor-pointer">
                        <input type="file" name="file" required class="w-full text-sm text-slate-400 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-semibold file:bg-blue-600 file:text-white hover:file:bg-blue-500 cursor-pointer">
                    </div>
                    <button type="submit" class="w-full bg-blue-600 hover:bg-blue-500 font-semibold py-3 rounded-xl transition shadow-lg shadow-blue-500/20">Zagruzit v Google Drive</button>
                </form>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl shadow-xl">
                <h3 class="font-semibold mb-4 text-sm text-slate-300 uppercase tracking-wider">Vashi fayly</h3>
                <div class="space-y-2">
                    {files_html}
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# Obrabotka POST-zaprosa ot FreeKassa pri perenapravlenii polzovatelya na URL uspeha/neudachi
@app.post("/")
async def homepage_post(request: Request):
  status_param = request.query_params.get("status", "success")
  return RedirectResponse(url=f"/?status={status_param}", status_code=303)


# ==========================================
# 3. SMENA TARIFA I PEREKHOD NA FREEKASSA
# ==========================================
@app.post("/select-plan")
async def select_plan(request: Request, plan: str = Form(...)):
  user = request.session.get("user")
  if not user:
    raise HTTPException(status_code=401, detail="Avtorizuytes")

  user_email = user.get("email", "unknown@user")

  if plan == "free":
    user_db_data = db.get_user(user_email)
    db.save_user(
        user_email,
        rate="free",
        files=user_db_data["files"],
        helped=user_db_data["helped"],
    )
    return RedirectResponse(url="/", status_code=303)

  elif plan == "izuchu":
    amount = "100.00"  # Tsena tarifa v valyute
    currency = "RUB"
    order_id = user_email.replace("@", "_at_")

    # Podpis 1 (sk-1): md5(merchant_id:amount:secret1:currency:order_id)
    sign_str = f"{FK_MERCHANT_ID}:{amount}:{FK_SECRET1}:{currency}:{order_id}"
    sign = hashlib.md5(sign_str.encode()).hexdigest()

    fk_url = (
        f"https://pay.freekassa.ru/?"
        f"m={FK_MERCHANT_ID}&"
        f"oa={amount}&"
        f"currency={currency}&"
        f"o={order_id}&"
        f"s={sign}"
    )
    return RedirectResponse(url=fk_url, status_code=303)

  return RedirectResponse(url="/", status_code=303)


# ==========================================
# FREEKASSA CALLBACK (URL OPOVESHCHENIYA)
# ==========================================
@app.post("/pay/callback")
async def pay_callback(
    MERCHANT_ID: str = Form(...),
    AMOUNT: str = Form(...),
    intid: str = Form(...),
    MERCHANT_ORDER_ID: str = Form(...),
    SIGN: str = Form(...),
):
  # Proverka podpisi secret 2 (sk-2): md5(MERCHANT_ID:AMOUNT:secret2:MERCHANT_ORDER_ID)
  expected_sign_str = f"{MERCHANT_ID}:{AMOUNT}:{FK_SECRET2}:{MERCHANT_ORDER_ID}"
  expected_sign = hashlib.md5(expected_sign_str.encode()).hexdigest()

  if SIGN.lower() != expected_sign.lower():
    raise HTTPException(
        status_code=400, detail="Oshibka podpisi FreeKassa (SIGN)"
    )

  # Vosstanavlivaem email polzovatelya iz order_id
  user_email = MERCHANT_ORDER_ID.replace("_at_", "@")

  # Obnovlyayem tarif v bd.txt
  user_db_data = db.get_user(user_email)
  db.save_user(
      user_email,
      rate="izuchu",
      files=user_db_data["files"],
      helped=user_db_data["helped"],
  )

  # FreeKassa trebuet strogo otvet "YES"
  return HTMLResponse(content="YES", status_code=200)


# ==========================================
# 4. NASTROYKA VIDIMOSTI FAYLA
# ==========================================
@app.post("/visibility/{file_id}")
async def change_file_visibility(
    request: Request, file_id: str, mode: str = Form(...)
):
  user = request.session.get("user")
  if not user:
    raise HTTPException(status_code=401, detail="Avtorizuytes")

  user_email = user.get("email", "unknown@user")
  drive_service = get_drive_service()
  if not drive_service:
    raise HTTPException(status_code=500, detail="Google Drive ne nastroen")

  try:
    file_info = (
        drive_service.files()
        .get(fileId=file_id, fields="appProperties, permissions")
        .execute()
    )
    file_owner = file_info.get("appProperties", {}).get("owner")

    if file_owner != user_email:
      raise HTTPException(
          status_code=403,
          detail="Tolko vladлец fayla mozhet menyat rezhim dostupa!",
      )

    permissions = file_info.get("permissions", [])
    for perm in permissions:
      if perm.get("type") == "anyone":
        drive_service.permissions().delete(
            fileId=file_id, permissionId=perm["id"]
        ).execute()

    if mode == "link":
      drive_service.permissions().create(
          fileId=file_id,
          body={
              "type": "anyone",
              "role": "reader",
              "allowFileDiscovery": False,
          },
      ).execute()
    elif mode == "public":
      drive_service.permissions().create(
          fileId=file_id,
          body={"type": "anyone", "role": "reader", "allowFileDiscovery": True},
      ).execute()

  except Exception as e:
    raise HTTPException(
        status_code=500, detail=f"Oshibka izmeneniya dostupa fayla: {e}"
    )

  return RedirectResponse(url="/", status_code=303)


# ==========================================
# 5. ZAGRUZKA V GOOGLE DRIVE
# ==========================================
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
  user = request.session.get("user")
  if not user:
    return RedirectResponse(url="/", status_code=303)

  user_email = user.get("email", "unknown@user")
  drive_service = get_drive_service()
  if not drive_service:
    raise HTTPException(
        status_code=500, detail="Google Drive API ne nastroen!"
    )

  contents = await file.read()

  file_metadata = {
      "name": file.filename,
      "parents": [GOOGLE_DRIVE_FOLDER_ID],
      "appProperties": {"owner": user_email},
  }

  media = MediaIoBaseUpload(
      io.BytesIO(contents), mimetype=file.content_type, resumable=True
  )

  try:
    drive_service.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
  except Exception as e:
    raise HTTPException(
        status_code=500, detail=f"Oshibka zagruzki v Google Drive: {e}"
    )

  user_db_data = db.get_user(user_email)
  db.save_user(
      user_email,
      rate=user_db_data["rate"],
      files=user_db_data["files"] + 1,
      helped=user_db_data["helped"],
  )

  return RedirectResponse(url="/", status_code=303)


# ==========================================
# 6. SKACHIVANIYE
# ==========================================
@app.get("/download/{file_id}")
async def download_file(request: Request, file_id: str):
  user = request.session.get("user")
  user_email = user.get("email", "unknown@user") if user else None

  drive_service = get_drive_service()
  if not drive_service:
    raise HTTPException(status_code=500, detail="Google Drive ne nastroen")

  try:
    file_info = (
        drive_service.files()
        .get(fileId=file_id, fields="name, appProperties, permissions")
        .execute()
    )
    file_owner = file_info.get("appProperties", {}).get("owner")
    permissions = file_info.get("permissions", [])

    is_anyone_allowed = any(p.get("type") == "anyone" for p in permissions)

    if not is_anyone_allowed and (not user_email or file_owner != user_email):
      raise HTTPException(
          status_code=403,
          detail="Dostup zapreshchen! Fayl imeet status 'Privatnyy'.",
      )

    request_drive = drive_service.files().get_media(fileId=file_id)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request_drive)

    done = False
    while not done:
      _, done = downloader.next_chunk()

    file_stream.seek(0)
    return StreamingResponse(
        file_stream,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={file_info['name']}"
        },
    )
  except Exception as e:
    raise HTTPException(
        status_code=500, detail=f"Oshibka skachivaniya fayla: {e}"
    )


# ==========================================
# 7. UDALENIYE
# ==========================================
@app.get("/delete/{file_id}")
async def delete_file(request: Request, file_id: str):
  user = request.session.get("user")
  if not user:
    raise HTTPException(status_code=401, detail="Avtorizuytes")

  user_email = user.get("email", "unknown@user")
  drive_service = get_drive_service()
  if not drive_service:
    raise HTTPException(status_code=500, detail="Google Drive ne nastroen")

  try:
    file_info = (
        drive_service.files()
        .get(fileId=file_id, fields="appProperties")
        .execute()
    )
    file_owner = file_info.get("appProperties", {}).get("owner")

    if file_owner != user_email:
      raise HTTPException(
          status_code=403,
          detail="Oshibka dostupa! Udalit fayl mozhet tolko vladлец.",
      )

    drive_service.files().delete(fileId=file_id).execute()

    user_db_data = db.get_user(user_email)
    new_count = max(0, user_db_data["files"] - 1)
    db.save_user(
        user_email,
        rate=user_db_data["rate"],
        files=new_count,
        helped=user_db_data["helped"],
    )
  except Exception as e:
    raise HTTPException(
        status_code=500, detail=f"Oshibka udaleniya fayla: {e}"
    )

  return RedirectResponse(url="/", status_code=303)


# ==========================================
# 8. OAUTH AVTORIZACIYA
# ==========================================
@app.get("/login/google")
async def login_google(request: Request):
  return await oauth.google.authorize_redirect(
      request, request.url_for("auth_google")
  )


@app.get("/auth/callback")
async def auth_google(request: Request):
  token = await oauth.google.authorize_access_token(request)
  request.session["user"] = dict(token.get("userinfo"))
  request.session["provider"] = "google"
  return RedirectResponse(url="/")


@app.get("/login/github")
async def login_github(request: Request):
  redirect_uri = request.url_for("auth_github")
  return await oauth.github.authorize_redirect(request, redirect_uri)


@app.get("/auth/github/callback")
async def auth_github(request: Request):
  token = await oauth.github.authorize_access_token(request)
  resp = await oauth.github.get("user", token=token)
  profile = resp.json()

  email = profile.get("email")
  if not email:
    emails_resp = await oauth.github.get("user/emails", token=token)
    emails = emails_resp.json()
    primary_email = next(
        (e["email"] for e in emails if e.get("primary")), None
    )
    email = primary_email or (
        emails[0]["email"] if emails else f"{profile['login']}@github.com"
    )

  request.session["user"] = {
      "name": profile.get("name") or profile.get("login"),
      "email": email,
      "picture": profile.get("avatar_url"),
  }
  request.session["provider"] = "github"
  return RedirectResponse(url="/")


# --- DONATIONALERTS OAUTH ---
@app.get("/login/donationalerts")
async def login_donationalerts():
  auth_url = (
      f"https://www.donationalerts.com/oauth/authorize"
      f"?client_id={DA_CLIENT_ID}"
      f"&redirect_uri={DA_REDIRECT_URI}"
      f"&response_type=code"
      f"&scope=oauth-user-show"
  )
  return RedirectResponse(auth_url)


@app.get("/auth/donationalerts/callback")
async def auth_donationalerts(
    request: Request, code: str = None, error: str = None
):
  if error or not code:
    raise HTTPException(
        status_code=400, detail=f"Oshibka avtorizacii DonationAlerts: {error}"
    )

  async with httpx.AsyncClient() as client:
    token_resp = await client.post(
        "https://www.donationalerts.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": DA_CLIENT_ID,
            "client_secret": DA_CLIENT_SECRET,
            "redirect_uri": DA_REDIRECT_URI,
            "code": code,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if token_resp.status_code != 200:
      raise HTTPException(
          status_code=token_resp.status_code,
          detail=f"Oshibka polucheniya tokena DA: {token_resp.text}",
      )

    token_data = token_resp.json()
    access_token = token_data.get("access_token")

    user_resp = await client.get(
        "https://www.donationalerts.com/api/v1/user/oauth",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    if user_resp.status_code != 200:
      raise HTTPException(
          status_code=user_resp.status_code, detail="Oshibka profilya DA"
      )

    profile = user_resp.json().get("data", {})

  email = (
      profile.get("email")
      or f"{profile.get('code', 'user')}@donationalerts.local"
  )

  request.session["user"] = {
      "name": profile.get("name"),
      "email": email,
      "picture": profile.get("avatar"),
  }
  request.session["provider"] = "donationalerts"
  return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
  request.session.clear()
  return RedirectResponse(url="/")


# ==========================================
# ROUT DLYA OTKRYTIYA FK-VERIFY.HTML
# ==========================================
@app.get("/fk-verify.html")
async def get_fk_verify():
  file_path = os.path.join(os.path.dirname(__file__), "fk-verify.html")

  if not os.path.exists(file_path):
    raise HTTPException(
        status_code=404,
        detail="Fayl fk-verify.html ne nayden v papke proekta!",
    )

  return FileResponse(file_path, media_type="text/html")


if __name__ == "__main__":
  import uvicorn

  port = int(os.environ.get("PORT", 8000))
  uvicorn.run(app, host="0.0.0.0", port=port)
