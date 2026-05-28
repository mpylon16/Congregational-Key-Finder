import os
import re
import time
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from dotenv import load_dotenv

# LOAD THE .ENV FILE FIRST
load_dotenv() 

# NOW FETCH THE VARIABLES SAFELY
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Check if they actually loaded
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in .env file.")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- 2. UPGRADED WEB SCRAPER ---
def fetch_first_line_by_ccli(ccli_number):
    if not ccli_number or str(ccli_number).strip().lower() == "unknown":
        return "Unknown"
        
    ccli_clean = str(ccli_number).replace(" ", "").strip()
    search_url = f"https://wordtoworship.com/search/node/{ccli_clean}"
    
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://wordtoworship.com/search/songs',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })
    
    try:
        response = session.get(search_url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # DEBUG: Print the title of the page to see if we landed where we think we did
        print(f"DEBUG: Page Title: {soup.title.string if soup.title else 'No Title'}")
        
        # DEBUG: Print the first 500 characters of the page to see what's on it
        print(f"DEBUG: Page Snippet: {response.text[:500]}")

        # DEBUG: Print the first 5 links on the page to help us identify the correct one
        # This will show up in your terminal and tell us exactly how to find the song link
        links = soup.find_all('a')
        print(f"DEBUG: Found {len(links)} links on the page. Checking for /song/ links...")
        
        # Look for the link more broadly
        song_link = None
        for link in links:
            if link.get('href') and '/song/' in link.get('href'):
                song_link = link
                print(f"DEBUG: Found song link: {song_link['href']}")
                break
        
        if not song_link:
            print(f"DEBUG: No link containing '/song/' found in search results.")
            return "Unknown"

        if song_link['href'].startswith('http'):
            song_url = song_link['href']
        else:
            song_url = "https://wordtoworship.com" + song_link['href']
        # Step 3: Fetch the song page
        print(f"DEBUG: Navigating to {song_url}")
        song_resp = session.get(song_url, timeout=10)
        song_soup = BeautifulSoup(song_resp.text, 'html.parser')
        
        # Step 4: Robust Lyric Extraction
        lyrics_text = None
        
        # The lyrics are inside a field named 'field-name-body'
        # We target that specific class, then grab the 'field-item' inside it.
        lyrics_container = song_soup.find('div', class_='field-name-body')
        
        if lyrics_container:
            # Get the text content, preserving line breaks
            lyrics_text = lyrics_container.get_text(separator='\n')
            
            # Split into lines and filter
            lines = [l.strip() for l in lyrics_text.split('\n') if l.strip()]
            
            for line in lines:
                # Ignore the "Lyrics:" label and structural headers
                if not re.match(r'^(Lyrics:|Verse|Chorus|Bridge|Ending)\s*\d*', line, re.IGNORECASE):
                    return line
        else:
            print("DEBUG: Could not locate 'field-name-body' class.")
            
    except Exception as e:
        print(f"⚠️ Web scraping error: {e}")
        
    return "Unknown"

# --- 3. MAIN RUNNER SCRIPT ---
def main():
    print("🚀 Fetching records from Supabase...")
    
    # Notice we are now explicitly requesting the ccli_number column too
    try:
        response = supabase.table("songs").select("id, title, first_line, ccli_number").execute()
        songs = response.data
    except Exception as e:
        print(f"❌ Failed to connect to Supabase: {e}")
        return

    # Filter for songs missing a first line, but THAT HAVE a valid CCLI number to search with
    target_songs = []
    for s in songs:
        needs_lyric = not s.get("first_line") or s["first_line"].strip() in ["", "Unknown", "Unknown First Line"]
        has_ccli = s.get("ccli_number") and str(s["ccli_number"]).strip().lower() != "unknown"
        
        if needs_lyric and has_ccli:
            target_songs.append(s)

    total_targets = len(target_songs)

    if total_targets == 0:
        print("✅ No eligible songs found requiring updates (either already have lyrics or missing CCLI).")
        return

    print(f"📋 Found {total_targets} songs ready for web lookup.\n" + "-"*40)

    updated_count = 0
    
    # We use enumerate HERE on the main loop to track our progress accurately
    for i, song in enumerate(target_songs, 1):
        song_id = song["id"]
        ccli = song["ccli_number"]
        
        # Now the print statement shows the clean [1/110] counter right at the start of the lookup
        print(f"[{i}/{total_targets}] 🔍 Searching CCLI {ccli} for: '{song['title']}'...")
        
        first_line = fetch_first_line_by_ccli(ccli)
        
        if first_line and first_line != "Unknown":
            print(f"✨ Found lyric: \"{first_line}\"")
            
            # Push back to Supabase
            try:
                supabase.table("songs").update({"first_line": first_line}).eq("id", song_id).execute()
                print(f"💾 Updated DB for ID {song_id}")
                updated_count += 1
            except Exception as e:
                print(f"❌ DB Update failed for ID {song_id}: {e}")
        else:
            print("⚠️ No lyric found on Word to Worship for this CCLI.")
            
        print("-" * 40)
        
        # Polite delay to prevent getting IP banned by the web host
        time.sleep(1.5) 

    print(f"\n🎉 Script complete! Successfully updated {updated_count} profiles out of {total_targets} attempts.")

if __name__ == "__main__":
    main()