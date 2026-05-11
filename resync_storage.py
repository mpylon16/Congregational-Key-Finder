import os
import io
import music21
from batch_processor import (
    supabase, 
    analyse_musicxml_summary, 
    make_json_safe
)

# Configuration matching your batch processor
BUCKET_NAME = 'mxl-library'
OUTPUT_DIR = "./output"

def resync_missing_songs():
    print(f"🔄 Starting re-sync for bucket: {BUCKET_NAME}")
    
    # 1. List all files in the bucket
    files = supabase.storage.from_(BUCKET_NAME).list()
    
    if not files:
        print("📭 No files found in bucket.")
        return

    for file_info in files:
        mxl_filename = file_info['name']
        if not mxl_filename.endswith('.mxl'):
            continue
            
        # The filename in your setup is the hash (e.g., "abc123...mxl")
        pdf_hash = mxl_filename.replace('.mxl', '')
        
        # 2. Check if this hash is already in the database
        res = supabase.table('songs').select("pdf_hash").eq("pdf_hash", pdf_hash).execute()
        
        if len(res.data) > 0:
            print(f"⏩ Skipping {mxl_filename}, already in database.")
            continue

        print(f"🔍 Processing missing file: {mxl_filename}")
        
        try:
            # 3. Download the file from storage to analyze it
            file_data = supabase.storage.from_(BUCKET_NAME).download(mxl_filename)
            
            # Setup a local directory for analysis (mirroring your batch logic)
            cached_output_dir = os.path.join(OUTPUT_DIR, pdf_hash)
            os.makedirs(cached_output_dir, exist_ok=True)
            local_path = os.path.join(cached_output_dir, mxl_filename)
            
            with open(local_path, 'wb') as f:
                f.write(file_data)

            # 4. Run your existing analysis
            # Note: We use the hash as the 'name' to help the glob pattern in your function
            summary = analyse_musicxml_summary(
                output_dir=cached_output_dir,
                name=pdf_hash,
                prefer_transpose_keys=False
            )

            # 5. Insert into Database
            mxl_url = supabase.storage.from_(BUCKET_NAME).get_public_url(mxl_filename)
            db_summary = make_json_safe(summary)
                                      
            supabase.table('songs').upsert({
                "pdf_hash": pdf_hash,
                "title": summary.get("title", "Unknown Title"),
                "ccli_number": summary.get("ccli_number", "N/A"),
                "mxl_url": mxl_url,
                "analysis_results": db_summary 
            }).execute()
            
            print(f"✅ Successfully added {summary.get('title')} to the database.")

        except Exception as e:
            print(f"❌ Failed to sync {mxl_filename}: {e}")

if __name__ == "__main__":
    resync_missing_songs()