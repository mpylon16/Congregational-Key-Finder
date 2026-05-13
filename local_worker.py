import os
import hashlib
import subprocess
import time
from dotenv import load_dotenv
from supabase import create_client

# 1. Setup
load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
supabase = create_client(url, key)

PDF_SOURCE_DIR = "./pending_pdfs"
OUTPUT_DIR = "./output"
BUCKET_NAME = "mxl-library"
TABLE_NAME = "scores"

# Ensure folders exist
os.makedirs(PDF_SOURCE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_pdf_hash(filepath):
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def is_already_processed(pdf_hash):
    """Check Supabase to see if this hash exists."""
    res = supabase.table(TABLE_NAME).select("id").eq("pdf_hash", pdf_hash).execute()
    return len(res.data) > 0

def process_files():
    pdfs = [f for f in os.listdir(PDF_SOURCE_DIR) if f.lower().endswith('.pdf')]
    print(f"Found {len(pdfs)} PDFs in {PDF_SOURCE_DIR}")

    for pdf_name in pdfs:
        path_to_pdf = os.path.join(PDF_SOURCE_DIR, pdf_name)
        pdf_hash = get_pdf_hash(path_to_pdf)

        # Skip if done
        if is_already_processed(pdf_hash):
            print(f"⏩ Skipping {pdf_name} (Already in Supabase)")
            continue

        print(f"🚀 Processing: {pdf_name}...")
        
        # 2. Run Audiveris (Adjust path to your local Java/Audiveris location if needed)
        # Using the command format that worked in your logs
        out_path = os.path.join(OUTPUT_DIR, pdf_hash)
        os.makedirs(out_path, exist_ok=True)
        
        cmd = [
            "java", "-Xmx4g", 
            "-cp", "C:/path/to/Audiveris/lib/*;C:/path/to/Audiveris/res", # FIX THIS PATH TO LOCAL
            "Audiveris", "-batch", "-transcribe", "-export", 
            "-output", out_path, 
            path_to_pdf
        ]
        
        try:
            subprocess.run(cmd, check=True)
            
            # 3. Find the resulting MXL
            mxl_files = [f for f in os.listdir(out_path) if f.endswith('.mxl')]
            if not mxl_files:
                print(f"❌ No MXL generated for {pdf_name}")
                continue
            
            mxl_filename = mxl_files[0]
            local_mxl_path = os.path.join(out_path, mxl_filename)
            storage_path = f"{pdf_hash}.mxl"

            # 4. Upload to Storage
            print(f"📤 Uploading {mxl_filename} to Supabase...")
            with open(local_mxl_path, 'rb') as f:
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=storage_path, 
                    file=f,
                    file_options={"upsert": "true", "content-type": "application/vnd.recordare.musicxml+xml"}
                )

            # 5. Update Database (Adjust columns to match your table)
            mxl_url = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
            supabase.table(TABLE_NAME).insert({
                "title": pdf_name.replace(".pdf", ""),
                "pdf_hash": pdf_hash,
                "mxl_url": mxl_url,
                "status": "completed",
                "created_at": "now()"
            }).execute()
            
            print(f"✅ Successfully finished {pdf_name}")

        except Exception as e:
            print(f"💥 Failed to process {pdf_name}: {e}")

if __name__ == "__main__":
    process_files()