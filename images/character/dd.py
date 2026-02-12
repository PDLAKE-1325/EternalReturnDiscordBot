import os
import io
from tqdm import tqdm

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# 저장 위치
DOWNLOAD_DIR = './Mini_Files'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as f:
            pickle.dump(creds, f)
    return build('drive', 'v3', credentials=creds)

service = get_service()

CHARACTER_FOLDER_ID = "1m__ubKg-KY7TqnqbFqwHi1DVxrdBeTEW"

def list_folders(parent_id, name=None):
    q = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'"
    if name:
        q += f" and name='{name}'"
    results = service.files().list(
        q=q,
        fields="files(id, name)",
        pageSize=1000
    ).execute()
    return results.get('files', [])

def list_files(parent_id):
    q = f"'{parent_id}' in parents and name contains '_Mini'"
    results = service.files().list(
        q=q,
        fields="files(id, name, size)",
        pageSize=1000
    ).execute()
    return results.get('files', [])

def download_file(file_id, filename):
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(os.path.join(DOWNLOAD_DIR, filename), 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

# 1️⃣ CharactER 아래 모든 폴더 순회
level1_folders = list_folders(CHARACTER_FOLDER_ID)

for folder in level1_folders:
    # 2️⃣ 각 폴더 안에서 "02. Default" 폴더 찾기
    default_folders = list_folders(folder['id'], name="02. Default")
    for df in default_folders:
        # 3️⃣ "_Mini" 파일만 다운로드
        files = list_files(df['id'])
        for f in tqdm(files, desc=df['name']):
            download_file(f['id'], f['name'])

print("✅ 완료: _Mini 파일만 다운로드됨")
