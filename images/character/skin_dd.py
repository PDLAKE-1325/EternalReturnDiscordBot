import os
import io
import re
import pickle
from tqdm import tqdm

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ===============================
# 설정
# ===============================
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# 🔴 여기에 CharactER 폴더 ID
CHARACTER_FOLDER_ID = "1m__ubKg-KY7TqnqbFqwHi1DVxrdBeTEW"

DOWNLOAD_DIR = "Mini_Files"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ===============================
# Google Drive 인증
# ===============================
def get_service():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)

service = get_service()

# ===============================
# 유틸
# ===============================
def extract_prefix_number(name: str):
    m = re.match(r"^(\d+)\.", name)
    return int(m.group(1)) if m else None

def list_folders(parent_id):
    q = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'"
    res = service.files().list(
        q=q,
        fields="files(id, name)",
        pageSize=1000
    ).execute()
    return res.get("files", [])

def list_mini_files(parent_id):
    q = f"'{parent_id}' in parents and name contains '_Mini'"
    res = service.files().list(
        q=q,
        fields="files(id, name)",
        pageSize=1000
    ).execute()
    return res.get("files", [])

def download_file(file_id, save_path):
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(save_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

# ===============================
# 메인
# ===============================
def main():
    print("▶ CharactER 폴더 순회 시작")

    character_folders = list_folders(CHARACTER_FOLDER_ID)

    for char in character_folders:
        print(f"\n📂 캐릭터: {char['name']}")

        subfolders = list_folders(char["id"])

        # 06 이상 폴더만
        target_folders = []
        for f in subfolders:
            num = extract_prefix_number(f["name"])
            if num is not None and num >= 6:
                target_folders.append(f)

        if not target_folders:
            print("  ⏭️ 06+ 폴더 없음")
            continue

        target_folders.sort(key=lambda x: extract_prefix_number(x["name"]))

        for folder in target_folders:
            print(f"  ▶ 진입: {folder['name']}")

            mini_files = list_mini_files(folder["id"])
            if not mini_files:
                print("     └ _Mini 파일 없음")
                continue

            for f in tqdm(mini_files, desc="     ⬇ 다운로드"):
                # 🔹 원본 파일명 그대로 저장 (중복 시 덮어씀)
                save_path = os.path.join(DOWNLOAD_DIR, f["name"])
                download_file(f["id"], save_path)

    print("\n✅ 완료: 모든 _Mini 이미지 다운로드 끝")

# ===============================
if __name__ == "__main__":
    main()
