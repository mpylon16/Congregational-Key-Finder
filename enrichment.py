import os
import re
import pdfplumber
from batch_processor import supabase, get_file_hash

def enrich_existing_database():
    PROCESSED_DIR = "./pending_pdfs/processed"
    
    # 1. Fetch all songs from Supabase
    print("Fetching songs from Supabase...")
    res = supabase.table('songs').select("*").execute()
    songs = res.data
    print(f"Found {len(songs)} songs in database.")

    # 2. Map local files by their hash for instant lookup
    print("Indexing local processed PDFs...")
    local_files = {}
    if os.path.exists(PROCESSED_DIR):
        for f in os.listdir(PROCESSED_DIR):
            if f.lower().endswith('.pdf'):
                path = os.path.join(PROCESSED_DIR, f)
                file_hash = get_file_hash(path)
                local_files[file_hash] = path

    # 3. Process each song
    for song in songs:
        pdf_hash = song['pdf_hash']
        title = song['title']
        updates = {}

        # Look for the file using the hash
        target_pdf = local_files.get(pdf_hash)

        if target_pdf:
            print(f"📄 Processing: {title}")
            try:
                with pdfplumber.open(target_pdf) as pdf:
                    # Extract text from the top 30% of the first page (where credits live)
                    first_page = pdf.pages[0]
                    width, height = first_page.width, first_page.height
                    top_box = (0, 0, width, height * 0.3)
                    header_text = first_page.within_bbox(top_box).extract_text() or ""
                    
                    # 1. Improved Year Extraction
                    year_match = re.search(r'©\s*(\d{4})', header_text)
                    if year_match:
                        updates["year"] = year_match.group(1)

                    # 2. Split Author Extraction (Words & Music)
                    # We look for "Words by", "Music by", and "Words and Music by" separately
                    words_by = re.search(r'Words (?:and Music )?by\s*(.*?)(?=\n|Music by|©|CCLI|$)', header_text, re.IGNORECASE)
                    music_by = re.search(r'Music by\s*(.*?)(?=\n|Words by|©|CCLI|$)', header_text, re.IGNORECASE)
                    
                    final_author = ""
                    if words_by and music_by:
                        w = words_by.group(1).strip().strip(',')
                        m = music_by.group(1).strip().strip(',')
                        if w == m:
                            final_author = w
                        else:
                            final_author = f"Words: {w} / Music: {m}"
                    elif words_by:
                        final_author = words_by.group(1).strip()
                    elif music_by:
                        final_author = music_by.group(1).strip()
                    
                    # Cleanup "Public Domain" mentions
                    if not final_author and "Public Domain" in header_text:
                        final_author = "Public Domain"

                    if final_author:
                        # Clean up trailing periods or extra newlines
                        updates["author"] = final_author.replace('\n', ' ').strip()

                    # CCLI: If it was missing before
                    if not song.get('ccli_number'):
                        ccli_match = re.search(r'CCLI\s+Song\s+#?\s*(\d{5,8})', text, re.IGNORECASE)
                        if ccli_match:
                            updates["ccli_number"] = ccli_match.group(1)

            except Exception as e:
                print(f"⚠️ Error reading {target_pdf}: {e}")
        else:
            print(f"❓ PDF not found in processed folder for: {title}")

        # 4. Extract Key/Range from the existing JSON blob if columns are empty
        analysis = song.get('analysis_results', {})
        if analysis:
            orig = analysis.get('original_key_info', {})
            if not song.get('original_key'): updates['original_key'] = orig.get('name')
            if not song.get('lowest_note'): updates['lowest_note'] = orig.get('range_low')
            if not song.get('highest_note'): updates['highest_note'] = orig.get('range_high')

        # 5. Push updates
        if updates:
            supabase.table('songs').update(updates).eq('pdf_hash', pdf_hash).execute()
            print(f"✅ Updated metadata for {title}")

if __name__ == "__main__":
    enrich_existing_database()