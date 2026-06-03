import traceback
import contextlib
import hashlib
import music21
from music21 import converter, pitch, key, interval, stream, meter, expressions, environment, note, chord, harmony
import tempfile
import os
import time
import subprocess
from flask import Flask, request, render_template, send_from_directory, abort, url_for, redirect, jsonify
from werkzeug.utils import secure_filename
import glob
import math # For math.inf
import io
import zipfile
from xml.etree import ElementTree as ET
from collections import defaultdict
import re
from pathlib import Path
from supabase import create_client, Client
import sys
import logging
import pdfplumber
import shutil
import json
import requests
from bs4 import BeautifulSoup
import re

# This forces every error to show up in your Railway Deploy Logs
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

# --- NEW LOGGING SETUP START ---
# Define the path for your log file. It will be in the same directory as app.py
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_errors.log')

# Configure basic logging to write to the file
logging.basicConfig(
    filename=log_file_path,
    level=logging.ERROR, # Log messages with severity ERROR and higher
    format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
)
# --- NEW LOGGING SETUP END ---

AUDIVERIS_CMD_FAST = r"C:\audiveris_fast_install\bin\audiveris.bat"
AUDIVERIS_CMD_FULL = "/app/Audiveris/bin/Audiveris"


@contextlib.contextmanager
def temporarily_set_cwd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)

def cleanup_stale_cache(output_folder, max_age_hours=1):
    """Deletes temporary MXL folders that have been abandoned by users."""
    if not os.path.exists(output_folder):
        return
        
    current_time = time.time()
    for item in os.listdir(output_folder):
        item_path = os.path.join(output_folder, item)
        
        # Only check directories
        if os.path.isdir(item_path):
            folder_age = current_time - os.path.getmtime(item_path)
            
            if folder_age > (max_age_hours * 3600):
                try:
                    shutil.rmtree(item_path)
                    print(f"🧹 Garbage Collector: Removed abandoned cache {item}")
                except Exception as e:
                    print(f"⚠️ Failed to remove stale cache {item}: {e}")

def make_json_safe(data):
    """Recursively converts all music21 objects in a dict/list to strings."""
    if isinstance(data, dict):
        return {k: make_json_safe(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [make_json_safe(v) for v in data]
    # If it's a music21 object, it will have a 'name' or 'fullName' attribute
    elif hasattr(data, 'classes') or 'music21' in str(type(data)):
        return str(data)
    return data

def clean_author_metadata(text):
    if not text:
        return ""
    
    # 1. Strip common layout/credit boilerplate strings (case-insensitive)
    patterns_to_strip = [
        r"(?i)\bwords\s+and\s+music\s+by\b",
        r"(?i)\bwords\s+by\b",
        r"(?i)\bmusic\s+by\b",
        r"(?i)\bwords\s+and\b",
        r"(?i)\bmusic\s+and\b"
    ]
    
    cleaned = text
    for pattern in patterns_to_strip:
        cleaned = re.sub(pattern, "", cleaned)
    
    # 2. Replace any leftover slashes or punctuation noise with spaces
    cleaned = cleaned.replace('/', ' ').replace(':', ' ')
    
    # 3. Clean up duplicate words or phrases sitting right next to each other
    words = cleaned.split()
    deduped_words = []
    for word in words:
        if not deduped_words or word.lower() != deduped_words[-1].lower():
            deduped_words.append(word)
            
    cleaned = " ".join(deduped_words)
    
    # 4. Final strip of whitespace or dangling connectors
    cleaned = re.sub(r'^\s*[,\s\-\/]+\s*', '', cleaned) 
    cleaned = re.sub(r'\s*[,\s\-\/]+\s*$', '', cleaned) 
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()     
    
    return cleaned


def extract_metadata_from_pdf(pdf_path):
    metadata = {
        "title": None,
        "author": None,
        "ccli_number": None,
        "year": None
    }
    
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return metadata
            
        first_page = pdf.pages[0]
        width, height = first_page.width, first_page.height
        
        # 1. TARGET THE TOP ZONE FOR HEADERS (Top 30%)
        top_box = (0, 0, width, height * 0.3)
        top_page_segment = first_page.within_bbox(top_box)
        
        # Extract word structures to isolate and discard music fonts/glyphs safely
        segment_words = top_page_segment.extract_words(extra_attrs=["size"])
        clean_words = [
            w for w in segment_words 
            if not any(0xE000 <= ord(char) <= 0xF8FF for char in w["text"])
        ]
        
        if not clean_words:
            return metadata

        # --- 2. TITLE DETECTION (Looking for the largest standard text) ---
        max_size = max(w["size"] for w in clean_words)
        title_words = [w for w in clean_words if w["size"] >= max_size - 0.5]
        title_words.sort(key=lambda w: (w["top"], w["x0"]))
        metadata["title"] = " ".join([w["text"] for w in title_words])

        # --- 3. RECONSTRUCT CLEAN STRIPPED HEADER TEXT STREAM ---
        # Sort words naturally by line layout to build a clean text stream for regex execution
        clean_words.sort(key=lambda w: (w["top"], w["x0"]))
        
        # Group tokens roughly into text lines to preserve structural match conditions
        lines = []
        current_line = []
        last_top = -1
        
        for w in clean_words:
            # Tolerates tiny vertical offsets inline (within 3 points)
            if last_top == -1 or abs(w["top"] - last_top) <= 3:
                current_line.append(w["text"])
            else:
                lines.append(" ".join(current_line))
                current_line = [w["text"]]
            last_top = w["top"]
            
        if current_line:
            lines.append(" ".join(current_line))
            
        header_text_stream = "\n".join(lines)

        # --- 4. EXECUTING SAVED REGEX STRATEGY ON CLEAN TEXT ---
        # Pull Year
        year_match = re.search(r'©\s*(\d{4})', header_text_stream)
        if year_match:
            metadata["year"] = year_match.group(1)

        # Run Split Author Searches
        words_by = re.search(r'Words (?:and Music )?by\s*(.*?)(?=\n|Music by|©|CCLI|$)', header_text_stream, re.IGNORECASE)
        music_by = re.search(r'Music by\s*(.*?)(?=\n|Words by|©|CCLI|$)', header_text_stream, re.IGNORECASE)
        
        raw_author = ""
        if words_by and music_by:
            w = words_by.group(1).strip().strip(',')
            m = music_by.group(1).strip().strip(',')
            if w == m:
                raw_author = w
            else:
                raw_author = f"{w} and {m}"
        elif words_by:
            raw_author = words_by.group(1).strip()
        elif music_by:
            raw_author = music_by.group(1).strip()
            
        if not raw_author and "Public Domain" in header_text_stream:
            raw_author = "Public Domain"

        # Apply your exact cleanup formatter function to fix residual duplicate layout text
        if raw_author:
            metadata["author"] = clean_author_metadata(raw_author)

        # --- 5. GLOBAL FOOTER FALLBACKS (For CCLI and Year if missing above) ---
        full_page_text = first_page.extract_text() or ""
        
        ccli_match = re.search(r'(?:CCLI\s*(?:Song)?\s*(?:#|No\.?)?\s*)(\d+)', full_page_text, re.IGNORECASE)
        if ccli_match:
            metadata["ccli_number"] = ccli_match.group(1)
            
        if metadata["year"] == None:
            fallback_year = re.search(r'(?:©|copyright|kbd\s+)\s*([12]\d{3})', full_page_text, re.IGNORECASE)
            if fallback_year:
                metadata["year"] = fallback_year.group(1)

    return metadata
       
def extract_metadata_from_musicxml(xml_text):
    """
    Extracts CCLI Song number and title from MusicXML text.
    Looks in <rights>, <credit-words>, <work-title>, <movement-title>.
    """
    root = ET.fromstring(xml_text)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"Error parsing XML content string: {e}")
        return {"ccli_number": None, "title": "Error Parsing XML"} # Return an error state
    
    print(f"The root element of your XML content is: {root.tag}") # Corrected print statement

    # Try to find CCLI Song number from <rights> or <credit-words>
    ccli_number = None    # Identify the default namespace from the root element
    namespace_uri = None
    if '}' in root.tag: # Checks if the root tag has a namespace (e.g., '{uri}tag_name')
        namespace_uri = root.tag.split('}')[0].strip('{')
    namespaces = {'mx': namespace_uri} if namespace_uri else {} # Use 'mx' as a prefix for XPath

    for tag in ['rights', 'credit-words']:
        print(f"\n--- Searching for <{tag}> elements ---")
        # Construct XPath query with namespace prefix if a namespace exists
        xpath_query = f".//mx:{tag}" if namespace_uri else f".//{tag}"
        
        elements_found = root.findall(xpath_query, namespaces=namespaces)

        if not elements_found:
            print(f"No <{tag}> elements found.")
            continue # Move to the next tag if none found

        for i, el in enumerate(elements_found):
            print(f"  Processing <{tag}> element #{i+1}:")
            if el.text is not None:
                text = el.text.strip()
                print(f"    Raw text from element: '{el.text}'") # Print raw text for debugging
                print(f"    Stripped text being checked: '{text}'")

                # Primary: look for "CCLI Song 1234567"
                match = re.search(r"\bCCLI(?:\s+Song)?\s+(\d{5,8})\b", text, re.IGNORECASE)
                if match:
                    ccli_number = match.group(1)
                    print(f"    SUCCESS: CCLI number found: {ccli_number}")
                    break # Breaks out of the inner loop (over 'el' elements)

                # Fallback: if text is short, just extract the first 5–8 digit number
                lines = text.splitlines()
                if len(lines) <= 3:
                    fallback_match = re.search(r"\b(\d{5,8})\b", lines[0])
                    if fallback_match:
                        print(f"    SUCCESS: Using fallback CCLI extraction from line 1: {fallback_match.group(1)}")
                        ccli_number = fallback_match.group(1)
                        break # Breaks out of the inner loop
                else:
                    print(f"    Text too long for fallback ({len(lines)} lines).")
            else:
                print(f"    Element has no text content (el.text is None).")

        if ccli_number:
            print(f"Found CCLI number: {ccli_number}. Breaking outer loop.")
            break # Breaks out of the outer loop (over 'tag')

    if ccli_number:
        print(f"Final CCLI: {ccli_number}")
    else:
        print("Final CCLI: Not found")

    # Try to find a title
    title = None
    title_tags = [
        './/work-title',
        './/movement-title',
        './/credit-words',
    ]
    for tag in title_tags:
        for el in root.findall(tag):
            if el.text and len(el.text.strip()) > 3:
                title = el.text.strip()
                break
        if title:
            break

    return {
        'ccli_number': ccli_number,
        'title': title
    }

# --- 2. UPGRADED WEB SCRAPER ---
def fetch_first_line_by_ccli(ccli_number):
    # Normalize the input: convert to string, remove whitespace, and lowercase
    ccli_str = str(ccli_number).strip().lower() if ccli_number is not None else ""
    
    # Check for invalid inputs: empty, "unknown", or "none"
    if not ccli_str or ccli_str in ["unknown", "none"]:
        return None
        
    ccli_clean = ccli_str
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
            return None

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
        
    return None

def get_raw_mxl_range(mxl_path):
    """Quickly scans an MXL file to find the absolute highest and lowest MIDI pitches."""
    try:
        score = converter.parse(mxl_path)
        pitches = [p.midi for p in score.pitches if p.midi is not None]
        if not pitches:
            return 48, 72 # Fallback: C3 to C5 if parsing fails
            
        return min(pitches), max(pitches)
    except Exception as e:
        print(f"⚠️ Quick peek failed: {e}")
        return 48, 72 # Fallback

def get_file_hash(pdf_path):
    with open(pdf_path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

def get_safe_scratch_dir():
    # Use /tmp, which is the standard, writable temp space on Railway/Linux
    scratch_dir = "/tmp/m21_scratch"

    try:
        os.makedirs(scratch_dir, exist_ok=True)
        # Test write access
        test_file = os.path.join(scratch_dir, 'test.tmp')
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
        print(f"✅ Scratch dir is writable: {scratch_dir}")
        return scratch_dir
    except Exception as e:
        print(f"❌ Failed to use scratch dir: {scratch_dir} — {e}")
        # Final fallback to whatever the OS suggests
        fallback = os.path.join(tempfile.gettempdir(), "m21_scratch_fallback")
        os.makedirs(fallback, exist_ok=True)
        return fallback

# Set music21's scratch directory
safe_working_dir = get_safe_scratch_dir()
us = environment.UserSettings()
# Ensure music21 actually uses this path
us['directoryScratch'] = safe_working_dir

# --- Configuration ---
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'output'

# --- Ensure necessary directories exist on startup ---
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- Flask App Initialization ---
app = Flask(__name__)

# Supabase Configuration
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Safety check: avoid crashing if variables are missing
if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None
    print("⚠️ WARNING: Supabase credentials missing from environment!")

# Ensure the app knows EXACTLY where it lives on the Linux server
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(basedir, 'output')

# Make sure these folders actually exist so the app doesn't crash on the first upload
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# --- Music Analysis Constants (Tunable) ---

# 1. Pitch Boundary Definitions (MIDI)
G3 = 55
A3 = 57
B3 = 59
C4 = 60
C5 = 72
D5 = 74
E5 = 76

# 2. Scoring Weight Multipliers
# These control how the 0-100 score is calculated
AVG_PENALTY_MULTIPLIER = 12  # Weight of overall stamina/tessitura
PEAK_PENALTY_MULTIPLIER = 5   # Weight of "deal-breaker" high/low notes

# 3. Key Preference Settings
MAX_ACCIDENTALS = 3
FRIENDLY_KEYS = {'C', 'G', 'D', 'A', 'E', 'F', 'Bb', 'Eb'}
TRANSPOSE_INSTRUMENT_KEYS = {'Bb', 'Eb', 'Ab'}

# 4. Global Comfort Thresholds 
# Centralized for use in logic, slugs, and HTML explanations
AVG_PENALTY_MULTIPLIER = 12
PEAK_PENALTY_MULTIPLIER = 5

COMFORT_THRESHOLDS = [
    {"min": 98, "label": "✅ Perfect Fit", "slug": "perfect", "desc": "Right in the Ideal Tessitura for a congregation."},
    {"min": 85, "label": "🎵 Very Singable", "slug": "very-singable", "desc": "Mostly Comfortable with a few manageable high or low spots."},
    {"min": 65, "label": "⚠️ Challenging", "slug": "challenging", "desc": "Frequent visits to the Max Singing Range; may tire some voices."},
    {"min": 40, "label": "❌ Uncomfortable", "slug": "uncomfortable", "desc": "Significant strain. Contains several notes that are difficult for a crowd."},
    {"min": 0,  "label": "🚫 Not Suitable", "slug": "unsuitable", "desc": "Contains notes beyond the Max Singing Range."}
]

# --- Helper Functions for Music Analysis ---

##def is_singable(low_midi, high_midi):
##    """
##    Checks if an entire pitch range (lowest to highest MIDI) falls within the
##    defined COMFORTABLE_RANGE.
##    """
##    return COMFORTABLE_RANGE[0] <= low_midi and high_midi <= COMFORTABLE_RANGE[1]
##
##def range_singability(low_midi, high_midi):
##    """
##    Performs a singability check, providing detailed comfort feedback
##    aligned with predefined categories and colors.
##
##    Args:
##        song_midi_data: A list of (midi_note, duration) tuples for the song.
##
##    Returns:
##        A dictionary containing singability feedback.
##    """


#--------Calculate comfort score----------

# Define MIDI boundaries
G3 = pitch.Pitch('G3').midi  # 55
A3 = pitch.Pitch('A3').midi  # 57
B3 = pitch.Pitch('B3').midi  # 59
C4 = pitch.Pitch('C4').midi  # 60
C5 = pitch.Pitch('C5').midi  # 72
D5 = pitch.Pitch('D5').midi  # 74
E5 = pitch.Pitch('E5').midi  # 76

def get_note_comfort_category(midi_note):
    """
    Returns a color string based on the comfort of a single MIDI note,
    matching your defined categories and colors.

    MIDI values for reference:
    C4 = 60, D4 = 62, E4 = 64, F4 = 65, G4 = 67, A4 = 69, B4 = 71
    C5 = 72, D5 = 74, E5 = 76, F5 = 77, G5 = 79, A5 = 81, B5 = 83

    C3 = 48, D3 = 50, E3 = 52, F3 = 53, G3 = 55, A3 = 57, B3 = 59
    """
    # 🎯 Ideal Tessitura: C4–C5
    if C4 <= midi_note <= C5:
        return "ideal"
    # ✅ Comfortable Range: A3–D5 (excluding ideal tessitura which is handled above)
    elif (A3 <= midi_note < C4) or (C5 < midi_note <= D5): 
        return "comfortable for most"
    # ⚠️ Stretch Zone: G3–E5 (excluding comfortable range which is handled above)
    elif (G3 <= midi_note < A3) or (D5 < midi_note <= E5): 
        return "a stretch for some"
    # ❌ Out of Range: below G3 or above E5
    else:
        return "out of range"

def get_note_comfort_color(midi_note):
    """
    Returns a color string based on the comfort category of a single MIDI note.
    """
    category = get_note_comfort_category(midi_note)
    if category == "ideal":
        return "green"
    elif category == "comfortable for most":
        return "#50C878"
    elif category == "a stretch for some":
        return "#FF8503"
    else: # out_of_range
        return "red"

def crop_and_clean_stream(parsed_score, min_midi, max_midi):
    """
    Permanently removes out-of-bounds notes from a music21 stream.
    Replaces them with rests to maintain measure timing integrity.
    """
    # .notes filters for both Note and Chord objects
    for el in parsed_score.recurse().notes:
        
        # Handle Single Notes
        if isinstance(el, note.Note):
            if el.pitch.midi < min_midi or el.pitch.midi > max_midi:
                # Create a rest of the exact same length
                r = note.Rest()
                r.duration = el.duration
                # Safely swap the note for the rest in the score
                el.activeSite.replace(el, r)
                
        # Handle Chords
        elif isinstance(el, chord.Chord):
            # Figure out which pitches are actually valid
            valid_pitches = [p for p in el.pitches if min_midi <= p.midi <= max_midi]
            
            if not valid_pitches:
                # Every note in the chord was out of bounds; replace with a rest
                r = note.Rest()
                r.duration = el.duration
                el.activeSite.replace(el, r)
            elif len(valid_pitches) < len(el.pitches):
                # Some notes were valid, some were not. Rebuild the chord.
                el.pitches = valid_pitches
                
    return parsed_score

# --- 2. LOGIC FUNCTIONS ---
def get_comfort_metadata(score):
    for threshold in COMFORT_THRESHOLDS:
        if score >= threshold["min"]:
            return threshold
    return COMFORT_THRESHOLDS[-1]

def calculate_comfort_score(notes_info):
    if not notes_info:
        return 0

    total_penalty = 0
    total_duration = 0
    max_penalty_seen = 0 

    for note in notes_info:
        midi = note['midi']
        dur = note['duration']
        total_duration += dur
        
        current_penalty = 0
        if C4 <= midi <= C5:
            current_penalty = 0
        elif A3 <= midi <= B3 or C5 < midi <= D5:
            current_penalty = 1 * dur
        elif G3 <= midi < A3 or D5 < midi <= E5:
            base = 3
            current_penalty = base * dur
            if dur >= 1.9: current_penalty += base * 0.5
        else:
            base = 10
            current_penalty = base * dur
            if dur >= 2: current_penalty += base
        
        total_penalty += current_penalty
        density = current_penalty / dur if dur > 0 else 0
        if density > max_penalty_seen:
            max_penalty_seen = density

    avg_penalty = total_penalty / total_duration if total_duration > 0 else 0
    
    # Calculate 0-100 score using global multipliers
    comfort_index = 100 - (avg_penalty * AVG_PENALTY_MULTIPLIER) - (max_penalty_seen * PEAK_PENALTY_MULTIPLIER)
    return max(0, min(100, round(comfort_index, 1)))

def comfort_category(score):
    return get_comfort_metadata(score)["label"]

def comfort_category_slug(score):
    return get_comfort_metadata(score)["slug"]

def ensure_music21_key(k):
    """
    Try to convert to a music21.key.Key object. Return None if not possible.
    """
    if isinstance(k, music21.key.Key):
        return k

    elif isinstance(k, music21.key.KeySignature):
        try:
            result = k.asKey()
            print("Was KeySignature, converted to key")
            return result
        except Exception as e:
            print(f"Exception during KeySignature conversion: {e}")
            return None

    elif isinstance(k, str):
        print("Key object is string")
        try:
            result = music21.key.Key(k)
            print("Converted string to music21 key")
            return result
        except Exception as e:
            print(f"Exception during string-to-key conversion: {e}")
            return None

    print(f"Final return None — unknown type: {type(k)} = {k}")
    return None

def calculate_band_friction(target_key, team_profile=None):
    """Calculates a friction penalty (0-25 points) based on active instruments."""
    if not team_profile:
        team_profile = {'guitar': True, 'keyboard': True} # Default band setup

    clean_tonic = target_key.tonic.name.replace('-', '♭').replace('#', '♯')
    mode = target_key.mode 

    instrument_friction = {
        'guitar': {
            'C': 0, 'C#': 8, 'D♭': 8, 'D': 0, 'D#': 9, 'E♭': 9, 'E': 0, 'F': 4, 
            'F#': 8, 'G♭': 8, 'G': 0, 'G#': 9, 'A♭': 9, 'A': 0, 'A#': 5, 'B♭': 5, 'B': 7
        },
        'keyboard': {
            'C': 0, 'C#': 3, 'D♭': 3, 'D': 0, 'D#': 3, 'E♭': 2, 'E': 1, 'F': 0, 
            'F#': 6, 'G♭': 6, 'G': 0, 'G#': 4, 'A♭': 3, 'A': 1, 'A#': 2, 'B♭': 1, 'B': 5
        },
        'bb_horns': { 
            'C': 2, 'C#': 8, 'D♭': 3, 'D': 6, 'D#': 1, 'E♭': 1, 'E': 8, 'F': 0, 
            'F#': 9, 'G♭': 9, 'G': 3, 'G#': 7, 'A♭': 2, 'A': 8, 'A#': 0, 'B♭': 0, 'B': 8
        },
        'eb_horns': { 
            'C': 1, 'C#': 7, 'D♭': 6, 'D': 2, 'D#': 0, 'E♭': 0, 'E': 6, 'F': 2, 
            'F#': 8, 'G♭': 8, 'G': 0, 'G#': 5, 'A♭': 5, 'A': 6, 'A#': 1, 'B♭': 1, 'B': 8
        },
        'strings': { 
            'C': 0, 'C#': 8, 'D♭': 8, 'D': 0, 'D#': 5, 'E♭': 4, 'E': 2, 'F': 0, 
            'F#': 7, 'G♭': 7, 'G': 0, 'G#': 8, 'A♭': 7, 'A': 0, 'A#': 4, 'B♭': 3, 'B': 6
        }
    }
    
    total_friction = 0
    active_count = 0

    for instrument, active in team_profile.items():
        if not active: continue
        active_count += 1
        
        if mode == 'minor':
            if instrument == 'guitar':
                base_friction = instrument_friction['guitar'].get(clean_tonic, 5)
                friction = max(0, base_friction - 2) # Minor open chords are a bit easier
            else:
                try:
                    relative_major = target_key.relative.tonic.name.replace('-', '♭').replace('#', '♯')
                    friction = instrument_friction[instrument].get(relative_major, 5)
                except Exception:
                    friction = 5
        else:
            friction = instrument_friction[instrument].get(clean_tonic, 5)
            
        total_friction += friction

    if active_count == 0: return 0
    return round((total_friction / active_count) * 2.5)

def get_key_analysis_info(original_key, all_notes_info, prefer_transpose_keys=False):
    key_analysis_info = []
    original_key_obj = ensure_music21_key(original_key)
    if not isinstance(original_key_obj, music21.key.Key): return []

    for i in range(-11, 12):
        try:
            shifted_notes_info = [
                {'midi': n['midi'] + i, 'duration': n['duration']}
                for n in all_notes_info if 'midi' in n and 'duration' in n and n['duration'] > 0
            ]
            if not shifted_notes_info: continue

            comfort_score = calculate_comfort_score(shifted_notes_info)
            try: transposed_key = original_key_obj.transpose(i)
            except Exception: continue

            # New Ranking Logic using the Instrument Matrix
            instrument_penalty = calculate_band_friction(transposed_key)
            distance_penalty = abs(i) * 0.5
            final_score = round(max(0, comfort_score - instrument_penalty - distance_penalty), 1)

            # Gather and Clean Metadata (Fixing the Flat symbols here!)
            lowest = min(n['midi'] for n in shifted_notes_info)
            highest = max(n['midi'] for n in shifted_notes_info)
            clean_low = pitch.Pitch(lowest).nameWithOctave.replace('-', '♭').replace('#', '♯')
            clean_high = pitch.Pitch(highest).nameWithOctave.replace('-', '♭').replace('#', '♯')
            clean_key_name = f"{transposed_key.tonic.name.replace('-', '♭').replace('#', '♯')} {transposed_key.mode}"

            key_analysis_info.append({
                'shift': i,
                'key': clean_key_name, # Exporting clean string for the HTML template
                'music21_key': transposed_key, # Kept for deduplication math
                'comfort_score': comfort_score, 
                'final_score': final_score,     
                'low_midi': lowest, 'high_midi': highest,
                'low_comfort': get_note_comfort_category(lowest),
                'high_comfort': get_note_comfort_category(highest),
                'low_color': get_note_comfort_color(lowest),
                'high_color': get_note_comfort_color(highest),
                'range_low': clean_low,
                'range_high': clean_high,
                'comfort_label': comfort_category(comfort_score),
                'comfort_slug': comfort_category_slug(comfort_score),
            })
        except Exception as e:
            print(f"❌ Error on shift {i}: {e}")
            continue

    return key_analysis_info

# --- Flask Routes ---

# --- MAIN UPLOAD ROUTE ---
@app.route('/', methods=['GET', 'POST'])
def upload_file():
    # 1. Run the Garbage Collector to keep your Railway disk clean
    cleanup_stale_cache(app.config.get('OUTPUT_FOLDER', '/app/output'))

    if request.method == 'POST':
        file = request.files['file']
        prefer_transpose_keys = 'transpose_keys' in request.form

        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # 2. Extract raw text from PDF
            pdf_metadata = extract_metadata_from_pdf(filepath)
            name, _ = os.path.splitext(filename)
            pdf_hash = get_file_hash(filepath)

            # ---------------------------------------------------------
            # 3. SUPABASE DEDUPLICATION CHECK (Save Railway Credits!)
            # ---------------------------------------------------------
            if supabase:
                try:
                    ccli = pdf_metadata.get("ccli_number")
                    existing_song = None

                    # Check A: Match by CCLI (The gold standard)
                    if ccli:
                        response = supabase.table('songs').select('pdf_hash').eq('ccli_number', ccli).limit(1).execute()
                        if response.data:
                            existing_song = response.data[0]

                    # Check B: Match by exact PDF Hash (Fallback if CCLI was missing)
                    if not existing_song:
                        response = supabase.table('songs').select('pdf_hash').eq('pdf_hash', pdf_hash).limit(1).execute()
                        if response.data:
                            existing_song = response.data[0]

                    # If we found it, skip Audiveris completely!
                    if existing_song:
                        print(f"♻️ Song already exists in DB! Redirecting to hash: {existing_song['pdf_hash']}")
                        
                        # Clean up the PDF we just saved to the upload folder since we don't need it
                        if os.path.exists(filepath):
                            os.remove(filepath)
                            
                        # Redirect instantly to the results page using the EXISTING hash
                        return redirect(url_for('get_song', pdf_hash=existing_song['pdf_hash']))

                except Exception as db_err:
                    print(f"⚠️ Supabase check failed, continuing with upload pipeline: {db_err}")
            # ---------------------------------------------------------

            # 4. If no match was found, proceed to Staging Area
            cached_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], pdf_hash)
            os.makedirs(cached_output_dir, exist_ok=True)

            # 5. Run Audiveris
            if not any(fname.endswith('.mxl') for fname in os.listdir(cached_output_dir)):
                try:
                    print(f"⏱️ Starting Audiveris for hash: {pdf_hash}")
                    start_time = time.time()

                    classpath_sep = os.pathsep  # Automatically picks ; for Windows and : for Linux
                    # We use the variable here to make the classpath 'Universal'
                    cp_value = f"/app/Audiveris/lib/*{classpath_sep}/app/Audiveris/res"
                    # 1. Environment Setup
                    # We force the HOME and TESSDATA paths at the process level
                    env = os.environ.copy()
                    env["HOME"] = "/app/audiveris_home"
                    env["TESSDATA_PREFIX"] = "/usr/share/tesseract-ocr/5/tessdata"
                    
                    # 2. Direct Java Command
                    # By calling 'java' directly, we can force the 'res' folder into the classpath
                    # and bypass the problematic bash start-script.
                    subprocess_args = [
                        'java',
                        '-Xmx3g',                          # High memory for OCR
                        '-Duser.home=/app/audiveris_home',  # Fix for the config path
                        '-cp', cp_value, # Load code AND resources
                        'Audiveris',                       # The Main Class name
                        '-batch',
                        '-transcribe',
                        '-export',
                        '-output', cached_output_dir,
                        '-option', 'org.audiveris.omr.sheet.ProcessingSwitches.smallHeads=true',
                        '-option', 'org.audiveris.omr.sheet.stem.BeamLinker.allowSmallHeadOnStandardBeam=true',
                        # --- New Silencing Options ---
                        '-option', 'org.audiveris.omr.util.LogLevel.level=WARNING',
                        '-option', 'org.audiveris.omr.text.TextBuilder.level=SEVERE', 
                        '-option', 'org.audiveris.omr.step.PageRhythm.level=WARNING',
                        filepath
                    ]

                    print("Running Audiveris (Direct Java Call):", ' '.join(subprocess_args))
                    
                    result = subprocess.run(
                        subprocess_args, 
                        stdout=subprocess.DEVNULL, # This silences the thousands of lines of logs
                        stderr=subprocess.PIPE,    # But keeps the errors if it crashes
                        text=True, 
                        check=False,
                        env=env  # Pass the explicit environment lie
                    )

                    duration = time.time() - start_time
                    
                    # Log the actual output even if it fails, so we can see what's happening
                    if result.stdout:
                        print("--- Audiveris STDOUT ---")
                        print(result.stdout)
                    if result.stderr:
                        print("--- Audiveris STDERR ---")
                        print(result.stderr)

                    if result.returncode != 0:
                        return f"<p>Audiveris processing failed (Error Code: {result.returncode}). Check server logs.</p><pre>{result.stdout}\n{result.stderr}</pre>", 500

                    print(f"✅ Audiveris finished in {duration:.2f} seconds")
                    time.sleep(1.0) # Give Linux a full second to finalize the file writes
                    print("📂 Contents of output directory:", os.listdir(cached_output_dir))
                    pass
                except Exception as e:
                    return f"<p>Unexpected error during Audiveris processing: {e}</p>", 500
            else:
                print(f"✅ Using cached MXL files for {filename} (hash: {pdf_hash})")

            # ---------------------------------------------------------
            # 6. FETCH FIRST LINE FROM WEB & RENDER FORM
            # ---------------------------------------------------------
            ccli_number = pdf_metadata.get("ccli_number")
            first_line_candidate = fetch_first_line_by_ccli(ccli_number)

            # ---------------------------------------------------------
            # 7. QUICK PEEK & RENDER FORM
            # ---------------------------------------------------------
            # Find the MXL file Audiveris just created (or the cached one)
            mxl_files = [f for f in os.listdir(cached_output_dir) if f.endswith('.mxl')]
            if mxl_files:
                local_mxl_path = os.path.join(cached_output_dir, mxl_files[0])
                raw_min_midi, raw_max_midi = get_raw_mxl_range(local_mxl_path)
            else:
                raw_min_midi, raw_max_midi = 48, 72 # Safe fallback

            # Determine if the file naturally overflows the standard 48-84 workspace
            has_out_of_bounds_notes = (raw_min_midi < 48 or raw_max_midi > 84)

            # Calculate the smart default position for the sliders on page-load
            default_slider_min = max(48, raw_min_midi)
            default_slider_max = min(84, raw_max_midi)

            # We pass explicit elements rather than the raw dictionary to match our 
            # new standalone template setup and clear out old `metadata.get()` dependencies
            return render_template(
                'review_song.html',
                title=pdf_metadata.get('title') or name,
                author=pdf_metadata.get('author'),
                ccli=ccli_number,
                year=pdf_metadata.get('year'),
                first_line=first_line_candidate,  # Injected directly into the form
                raw_min_midi=raw_min_midi, # <-- Pass the true lowest note
                raw_max_midi=raw_max_midi, # <-- Pass the true highest note
                slider_min_start=default_slider_min, # <-- Injected starting low slider position
                slider_max_start=default_slider_max, # <-- Injected starting high slider position
                pdf_hash=pdf_hash,
                name=name,
                prefer_transpose_keys=prefer_transpose_keys
            )

        return f"<p>Please upload a valid PDF file.</p><p><a href='{url_for('upload_file')}'>Try Again</a></p>"

    return render_template('upload.html')

@app.route('/commit_song', methods=['POST'])
def commit_song():
    # 1. Grab data passed from the review_song.html form
    pdf_hash = request.form.get('pdf_hash')
    name = request.form.get('name')
    prefer_transpose_keys = request.form.get('prefer_transpose_keys') == 'True'
    
    cached_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], pdf_hash)
    
    # Rebuild metadata dictionary with the user's manual corrections!
    user_metadata = {
        "title": request.form.get('title') or None,
        "author": request.form.get('author') or None,
        "ccli_number": request.form.get('ccli_number') or None,
        "year": request.form.get('year') or None,
        "first_line": request.form.get('first_line') or None
    }

    # 2. Handle the Cropping Limits (Updated for Dual Slider)
    min_midi_val = request.form.get('min_midi')
    max_midi_val = request.form.get('max_midi')
    
    # Safely cast to integer, defaulting to wide bounds if empty
    min_midi = int(min_midi_val) if min_midi_val else 0
    max_midi = int(max_midi_val) if max_midi_val else 127

    mxl_files = [f for f in os.listdir(cached_output_dir) if f.endswith('.mxl')]
    if not mxl_files:
        return "Error: MXL file lost from cache.", 500
    
    local_mxl_path = os.path.join(cached_output_dir, mxl_files[0])

    try:
        # 1. Unconditionally sanitize and parse the file to handle OMR errors
        if str(local_mxl_path).endswith('.mxl'):
            raw_xml_string = inject_divisions_and_time_if_missing(local_mxl_path)
            score = converter.parse(raw_xml_string, format='musicxml')
        else:
            score = converter.parse(local_mxl_path)

        # 2. Apply optional pitch range cropping if limits are set
        if min_midi > 0 or max_midi < 127:
            print(f"✂️ Cropping MXL file to range {min_midi} - {max_midi}")
            score = crop_and_clean_stream(score, min_midi, max_midi)

        # 3. Always overwrite the disk cache with a clean, validated version
        # Use 'mxl' for compressed archives to keep file extensions aligned
        if str(local_mxl_path).endswith('.mxl'):
            score.write('mxl', fp=local_mxl_path)
        else:
            score.write('musicxml', fp=local_mxl_path)

        # 4. Run the Analysis on the clean file
        summary = analyse_musicxml_summary(
            output_dir=cached_output_dir,
            name=name,
            prefer_transpose_keys=prefer_transpose_keys,
            pdf_metadata=user_metadata # Pass the clean form data, NOT the raw PDF text
        )

        # 🛑 SAFETY GATE: Verify the analysis actually succeeded
        if not summary or "original_key_info" not in summary or "comfort_score" not in summary["original_key_info"]:
            logging.error(f"Analysis payload is invalid or incomplete for hash {pdf_hash}.")
            return f"<p>Analysis failed: The MusicXML file generated by Audiveris was malformed or could not be parsed by music21.</p>", 422
        
        # 5. Database Save
        if supabase:
            storage_path = f"{pdf_hash}.mxl"
            with open(local_mxl_path, 'rb') as f:
                supabase.storage.from_('mxl-library').upload(
                    path=storage_path, 
                    file=f, 
                    file_options={"upsert": "true", "content-type": "application/vnd.recordare.musicxml+xml"}
                )
            
            mxl_url = supabase.storage.from_('mxl-library').get_public_url(storage_path)
            db_summary = make_json_safe(summary)
            orig_info = summary.get("original_key_info", {})
                                                    
            supabase.table('songs').upsert({
                "pdf_hash": pdf_hash,
                "title": user_metadata.get("title", "Unknown Title"),
                "ccli_number": user_metadata.get("ccli_number"),
                "author": user_metadata.get("author"),
                "year": user_metadata.get("year"),
                "first_line": user_metadata.get("first_line"),
                "original_key": orig_info.get("name", "Unknown"),
                "lowest_note": orig_info.get("range_low", "Unknown"),
                "highest_note": orig_info.get("range_high", "Unknown"),
                "mxl_url": mxl_url,
                "analysis_results": db_summary 
            }, on_conflict="pdf_hash").execute()
            print(f"🚀 Cloud Save Successful for {pdf_hash}")

        # Define the fallback clearly on its own line to prevent unclosed parenthesis issues
        final_mxl_url = mxl_url if 'mxl_url' in locals() else None

        # 6. Render the final result
        return render_template("analysis_results.html",
            pdf_hash=pdf_hash,                                       
            original_key=summary["original_key_info"],
            mxl_url=final_mxl_url,
            recommended=summary["recommended"],
            other_keys=summary["other_keys"],
            skipped=summary["skipped"],
            warnings=summary["warnings"],
            ccli_link=summary.get("ccli_url"),
            title=user_metadata.get("title"),
            author=user_metadata.get("author"),
            year=user_metadata.get("year"),
            ccli_no=user_metadata.get("ccli_number"),
            first_line=user_metadata.get("first_line"), # <-- ADDED HERE
            meta=get_comfort_metadata(summary["original_key_info"]["comfort_score"]),
            thresholds=COMFORT_THRESHOLDS
        )

    except Exception as e:
        # Crash reporting / cleanup logic
        logging.error("--- FATAL ERROR DURING MUSICXML ANALYSIS ---")
        logging.error(f"Exception Type: {type(e)}")
        logging.error(f"Exception Message: {e}")
        logging.exception("Full Traceback Details:")
        logging.error("--- END FATAL ERROR ---")

        print("Error:", e)

        if 'cached_output_dir' in locals() and os.path.exists(cached_output_dir):
            try:
                shutil.rmtree(cached_output_dir, ignore_errors=True)
                print(f"🧹 Cleaned up failed directory: {cached_output_dir}")
            except Exception as cleanup_error:
                logging.warning(f"Cleanup failed for {cached_output_dir}: {cleanup_error}")

        return f"<p>Analysis failed during commit: {e}</p>", 500
                

@app.route('/search_songs', methods=['GET'])
def search_songs():
    query = request.args.get('q', '')
    if len(query) < 2:
        return jsonify([])

    try:
        # Search title, author, or CCLI number
        # ILIKE is case-insensitive search
        res = supabase.table('songs').select("*").or_(
            f"title.ilike.%{query}%,author.ilike.%{query}%,ccli_number.ilike.%{query}%,first_line.ilike.%{query}%"
        ).limit(10).execute()
        
        return jsonify(res.data)
    except Exception as e:
        print(f"Search error: {e}")
        return jsonify([]), 500

@app.route('/get_song/<pdf_hash>', methods=['GET'])
def get_song(pdf_hash):
    # Fetch pre-analyzed data for a selected song
    res = supabase.table('songs').select("*").eq('pdf_hash', pdf_hash).single().execute()
    song = res.data
    
    if not song:
        return "Song not found", 404
        
    # Safely pull the analysis dictionary, fallback to an empty dict if NULL
    summary = song.get('analysis_results') or {}
    
    # Safely fetch original_key_info block or fallback to an empty dict
    original_key_info = summary.get("original_key_info") or {}
    
    # Safely pull the comfort score
    comfort_score = original_key_info.get("comfort_score")
    
    # Only try to fetch metadata if comfort_score is actually present
    meta = get_comfort_metadata(comfort_score) if comfort_score is not None else None
    
    # Return the same analysis template we use for new uploads
    return render_template("analysis_results.html",
        is_database_pull=True, # Tells the HTML to hide the top original key block
        pdf_hash=song.get('pdf_hash'),
        original_key=original_key_info,
        mxl_url=song.get("mxl_url"), # Enables download button
        recommended=summary.get("recommended", []),
        other_keys=summary.get("other_keys", []),
        skipped=summary.get("skipped", []),
        warnings=summary.get("warnings", []),
        ccli_link=summary.get("ccli_url"),
        title=song.get("title"),
        author=song.get("author"),
        year=song.get("year"),
        ccli_no=song.get("ccli_number"),
        meta=meta, # Safely computed above
        thresholds=COMFORT_THRESHOLDS
    )

def extract_xml_from_mxl(path):
    with zipfile.ZipFile(path, 'r') as z:
        # Find the first .xml file inside the .mxl archive that ISN'T metadata
        for name in z.namelist():
            name_lower = name.lower()
            # Match either extension while ignoring the container/metadata files
            if name_lower.endswith(('.xml', '.musicxml')) and "container.xml" not in name_lower and "meta-inf" not in name_lower:
                return z.read(name).decode("utf-8")
    raise ValueError(f"No valid music .xml file found inside {path}")

def inject_divisions_and_time_if_missing(current_path, previous_path=None):
    # Load the XML content
    xml_data = extract_xml_from_mxl(current_path)
    xml = xml_data.decode("utf-8") if isinstance(xml_data, bytes) else xml_data

    # --- FIX 1: Zero/Missing Divisions ---
    if re.search(r'<divisions>\s*0\s*</divisions>', xml):
        xml = re.sub(r'<divisions>\s*0\s*</divisions>', '<divisions>1</divisions>', xml)
    elif "<divisions>" not in xml:
        xml = re.sub(r"<attributes>", "<attributes><divisions>1</divisions>", xml, count=1)

    # --- FIX 2: Empty/Missing Time Signatures ---
    xml = xml.replace("<beats></beats>", "<beats>4</beats>")
    xml = xml.replace("<beat-type></beat-type>", "<beat-type>4</beat-type>")
    if "<time" not in xml:
        fallback_time = "<time><beats>4</beats><beat-type>4</beat-type></time>"
        xml = re.sub(r"<attributes>", f"<attributes>{fallback_time}", xml, count=1)

    # --- FIX 3: Rest Injection for Empty Measures (The "Come Lord Jesus" Fix) ---
    # This regex finds measures that contain <attributes> but NO <note> tags
    # and inserts a 1-beat rest so music21 doesn't divide by zero.
    
    def add_rest_to_empty(match):
        measure_open = match.group(1)   # e.g., <measure number="22">
        attributes = match.group(2)     # The <attributes> block
        measure_close = match.group(3)  # </measure>
        
        # If there is no <note> tag between attributes and the end of the measure:
        dummy_note = '<note><rest/><duration>1</duration><voice>1</voice><type>quarter</type></note>'
        print(f"🛠️ Injected dummy rest into empty measure.")
        return f"{measure_open}{attributes}{dummy_note}{measure_close}"

    # Pattern: Look for <measure>...<attributes>... but NO <note> before </measure>
    empty_measure_pattern = r'(<measure[^>]*>)\s*(<attributes>.*?</attributes>)(?!\s*<note)\s*(</measure>)'
    xml = re.sub(empty_measure_pattern, add_rest_to_empty, xml, flags=re.DOTALL)

    # --- FIX 4: Grace-Note-Only Measures ---
    # Measures 21 and 30 in your file only have <grace/> notes (0 duration).
    # We must ensure at least one note with duration exists.
    grace_only_pattern = r'(<measure[^>]*>.*?<grace/>.*?(?<!<duration>))(?=\s*</measure>)'
    xml = re.sub(grace_only_pattern, r'\1<note><rest/><duration>1</duration><type>quarter</type></note>', xml, flags=re.DOTALL)

    return xml.encode("utf-8")

def inject_into_attributes(attrs_content, divisions_tag, time_tag):
    # Only inject if the tags are missing or broken
    if "<divisions>0</divisions>" in attrs_content or "<divisions>" not in attrs_content:
        attrs_content = re.sub(r"<divisions>0</divisions>", "", attrs_content)
        attrs_content = divisions_tag + attrs_content

    if "<time>" not in attrs_content:
        attrs_content += time_tag

    return f"<attributes>{attrs_content}</attributes>"

def simplify_enharmonic(key_obj):
    """Return the enharmonic spelling with the fewest accidentals."""
    enharmonic_tonic = key_obj.tonic.getEnharmonic()
    enharmonic_key = key.Key(enharmonic_tonic.name, key_obj.mode)
    options = [key_obj, enharmonic_key]
    print(f"Original: {key_obj}")        # Db major
    print(f"Enharmonic: {enharmonic_key}")    # C# major
    return min(options, key=lambda k: abs(k.sharps))

def extract_written_chords(score):
    """Returns a list of ChordSymbol objects (written chord names)."""
    try:
        return [h for h in score.recurse().getElementsByClass(music21.harmony.ChordSymbol)]
    except Exception:
        return []

def analyse_musicxml_summary(output_dir, name, prefer_transpose_keys=False, pdf_metadata=None):
    print("🎬 Running analyse_musicxml_summary()")
#    from music21 import converter, pitch, stream, note

    pattern = os.path.join(output_dir, f"{name}*.mxl")
    mxl_files = sorted(glob.glob(pattern))

    all_notes_info = []
    skipped = []
    warnings = []

    previous_path = None
    summary = {}

    # Pre-populate from PDF metadata (most reliable source for CCLI)
    if pdf_metadata:
        summary["ccli_number"] = pdf_metadata.get("ccli_number")
    
    # Title defaults to filename immediately as baseline
    try:
        songname, _, _ = name.split("-")
    except ValueError:
        songname = name
    summary["title"] = songname.replace('_', ' ')

    for path in mxl_files:
        current_path = path
        try:
            injected_bytes = inject_divisions_and_time_if_missing(current_path, previous_path)

            if injected_bytes:
                patched_path = os.path.join(tempfile.gettempdir(), f"{os.path.basename(current_path)}_patched.xml")
                with open(patched_path, "wb") as f:
                    f.write(injected_bytes)
                parse_target = patched_path
            else:
                parse_target = current_path

            # --- FIX START: Bypassing music21 container.xml bug ---
            try:
                target_str = str(parse_target)
                if target_str.lower().endswith('.mxl'):
                    print(f"📦 Extracting raw XML for analysis using extract_xml_from_mxl: {target_str}")
                    raw_xml_string = extract_xml_from_mxl(target_str)
                    score = converter.parse(raw_xml_string, format='musicxml')
                else:
                    score = converter.parse(target_str)
            except Exception as parse_error:
                logging.error(f"❌ Failed to parse score in analysis: {parse_error}")
                raise parse_error
            # --- FIX END ---
            print(f"🔍 Parsing MXL file: {os.path.basename(parse_target)}")
            print(f"🔍 Score has {len(score.parts)} parts")
                        
            for part in score.parts:
                print(f"🧩 Part: name='{part.partName}', id='{part.id}'")

            notes, _, part_warnings = extract_vocal_note_info(score, source_name=os.path.basename(path))
            print(f"✅ {os.path.basename(path)}: extracted {len(notes)} vocal notes")
            if part_warnings:
                for w in part_warnings:
                    print(f"⚠️ Warning in {os.path.basename(path)}: {w}")
            all_notes_info.extend(notes)
            warnings.extend([f"{os.path.basename(path)}: {w}" for w in part_warnings])

            # Try to get title from XML metadata (overrides filename if found)
            # Try to get CCLI from XML metadata only if not already found from PDF
            needs_ccli = not summary.get("ccli_number")
            needs_title = summary["title"] == songname.replace('_', ' ')  # still on filename fallback

            if needs_ccli or needs_title:
                try:
                    xml_content = extract_xml_from_mxl(path)
                    print("Extracting metadata from XML")
                    metadata = extract_metadata_from_musicxml(xml_content)

                    if needs_ccli and metadata.get("ccli_number"):
                        summary["ccli_number"] = metadata.get("ccli_number")
                        print(f"CCLI from XML: {summary['ccli_number']}")

                    if needs_title and metadata.get("title"):
                        summary["title"] = metadata.get("title")
                        print(f"Title from XML: {summary['title']}")

                except Exception as e:
                    print(f"⚠️ Failed to extract metadata from {path}: {e}")
                    traceback.print_exc()

        except Exception as e:
            skipped.append(os.path.basename(path))
            warnings.append(f"{os.path.basename(path)} failed: {e}")
            print(f"❌ Failed to analyze {os.path.basename(path)}: {e}")
            traceback.print_exc()
            continue
        previous_path = current_path

    # Final fallbacks
    if not summary.get("ccli_number"):
        summary["ccli_number"] = "Not found"
    # Title already has filename fallback set at top, so no further action needed

    print(f"Final title: {summary['title']}")
    print(f"Final CCLI: {summary['ccli_number']}")

    if not all_notes_info:
        return {
            "recommended": None,
            "other_keys": [],
            "all_keys_analysis": [],
            "original_key_info": {
                "name": "Unknown",
                "range_low": "?",
                "range_high": "?",
                "low_out": None,
                "high_out": None
            },
            "skipped": skipped,
            "warnings": warnings,
            "title": summary["title"],
            "ccli_number": summary["ccli_number"],
        }

    # Build dummy stream for key analysis
    dummy_stream = stream.Part()
    for n in all_notes_info:
        p = pitch.Pitch(midi=n['midi'])
        n_obj = note.Note(p)
        n_obj.duration.quarterLength = n['duration']
        dummy_stream.append(n_obj)

    chords = extract_written_chords(score)
    ks_objects = score.parts[0].recurse().getElementsByClass(key.KeySignature)
    
    # Declare fallbacks
    declared_key = ks_objects[0].asKey() if ks_objects else None
    print(f"Declared key type: {type(declared_key)} | Value: {declared_key}")
    estimated_key = dummy_stream.analyze('key')
    print(f"Estimated key:{estimated_key}")

    try:        
        # 1. FIXED: Clean filename parsing to isolate the key safely from extensions
        file_key = None
        if "-" in name:
            try:
                parts = name.split("-")
                raw_key_part = parts[-1] # Grabs the last segment, e.g., "C.pdf"
                clean_key_string = os.path.splitext(raw_key_part)[0].strip() # Strips ".pdf" to leave "C"
                file_key = ensure_music21_key(clean_key_string)
            except Exception as e:
                print(f"⚠️ Filename key parsing skipped: {e}")

        # 2. PRIORITY LOGIC LADDER
        if file_key:
            # Trust the filename explicit value first
            final_key = file_key
            print(f"🔑 Using Key designated by Filename: {final_key}")
        elif declared_key:
            # Fall back to structural key signatures on the stave lines
            final_key = declared_key
            
            # Check if the weight analysis suggests a relative minor shift
            relative_minor = declared_key.relative
            if estimated_key == relative_minor and chords:
                first_chord = chords[0] if len(chords) > 0 else None
                last_chord = chords[-1] if len(chords) > 0 else None
                
                # Only switch to relative minor if it strictly starts or ends on that minor root
                if (first_chord and first_chord.root().name == relative_minor.tonic.name and first_chord.quality == 'minor') or \
                (last_chord and last_chord.root().name == relative_minor.tonic.name and last_chord.quality == 'minor'):
                    final_key = relative_minor
                    print(f"🎼 Switched to structural Relative Minor based on layout: {final_key}")
        else:
            # Ultimate fallback when working with open-ended pitch collections
            final_key = estimated_key
            print(f"📊 Using mathematical estimated pitch key: {final_key}")

        # Generate warnings if structural dissonance exists between estimates
        key_warning = ""
        if declared_key and estimated_key.tonic.name != declared_key.tonic.name:
            if not file_key: # Only warn if we didn't explicitly clear it via filename
                key_warning = f"⚠️ Estimated key from notes ({estimated_key.tonic.name} {estimated_key.mode}) differs from stave key signature ({declared_key.tonic.name} {declared_key.mode})."

        if final_key:
            final_key = simplify_enharmonic(final_key)
            original_key_name = f"{final_key.tonic.name} {final_key.mode}"
        else:
            original_key_name = "Unknown"

    except Exception as e:
        final_key = None
        original_key_name = "Unknown"
        key_warning = f"⚠️ Key analysis failed: {e}"
        warnings.append(key_warning)

    print(f"Just before all_keys_analysis, final _key: {type(final_key)} = {final_key}")
    all_keys_analysis = get_key_analysis_info(final_key, all_notes_info, prefer_transpose_keys)
    bad_entries = [k for k in all_keys_analysis if not isinstance(k, dict) or 'key' not in k]
    print("🛠  Number of malformed entries:", len(bad_entries))

    # 1. Find the original key details (shift == 0) from the generated analysis list
    original_key_info = next((k for k in all_keys_analysis if k['shift'] == 0), None)
    
    if original_key_info:
        # 1. Pull the note strings from the dictionary
        low = original_key_info["range_low"]   # e.g., "Ab3"
        high = original_key_info["range_high"] # e.g., "Eb5"

        # 2. Pull raw MIDI integers directly from the data structure, bypassing string parsing entirely
        original_low = original_key_info["low_midi"]
        original_high = original_key_info["high_midi"]

        # 3. Pull everything else normally
        original_low_comfort = original_key_info.get("low_comfort") or get_note_comfort_category(original_low)
        original_high_comfort = original_key_info.get("high_comfort") or get_note_comfort_category(original_high)
        original_low_color = original_key_info.get("low_color") or get_note_comfort_color(original_low)
        original_high_color = original_key_info.get("high_color") or get_note_comfort_color(original_high)
        orig_comfort_score = original_key_info["comfort_score"]
        orig_comfort_label = original_key_info["comfort_label"]
    else:
        # Fallback values if shift 0 wasn't caught
        midi_values = [n['midi'] for n in all_notes_info]
        original_low = min(midi_values) if midi_values else 60
        original_high = max(midi_values) if midi_values else 72
        low = pitch.Pitch(original_low).nameWithOctave
        high = pitch.Pitch(original_high).nameWithOctave
        original_low_comfort = get_note_comfort_category(original_low)
        original_high_comfort = get_note_comfort_category(original_high)
        original_low_color = get_note_comfort_color(original_low)
        original_high_color = get_note_comfort_color(original_high)
        orig_comfort_score = 0.0
        orig_comfort_label = "⚠️ Unknown"
        
# 2. Run the full matrix through your pitch-class deduplicator (sorted by final_score descending)
    deduped_keys = deduplicate_by_key(all_keys_analysis)

    # 3. Stamp an absolute recommendation rank (1 to 12) based on the balanced final_score
    for index, k in enumerate(deduped_keys):
        k['recommendation_rank'] = index + 1

    # 4. Establish the top recommended choice
    recommended = deduped_keys[0] if deduped_keys else None
    
    if recommended:
        rec_low_midi = recommended['low_midi']
        rec_high_midi = recommended['high_midi']

        recommended['low_comfort'] = get_note_comfort_category(rec_low_midi)
        recommended['high_comfort'] = get_note_comfort_category(rec_high_midi)
        recommended['low_color'] = get_note_comfort_color(rec_low_midi)
        recommended['high_color'] = get_note_comfort_color(rec_high_midi)

    # 5. Keep all remaining 11 options for the lower categories
    other_keys = [k for k in deduped_keys if k['shift'] != recommended['shift']]

    summary["recommended"] = recommended
    summary["other_keys"] = other_keys          
    summary["original_key_info"] = {
        "name": original_key_name,
        "range_low": low,
        "range_high": high,
        "low_comfort": original_low_comfort,
        "high_comfort": original_high_comfort,
        "low_color": original_low_color,
        "high_color": original_high_color,
        "comfort_score": orig_comfort_score,  # Raw vocal comfort score
        "comfort_label": orig_comfort_label
    }
    summary["skipped"] = skipped
    summary["warnings"] = warnings
    if summary.get("ccli_number") and summary["ccli_number"] != "Not found":
        summary["ccli_url"] = f"https://songselect.ccli.com/Songs/{summary['ccli_number']}"

    return summary

def extract_vocal_note_info(score, fallback_time_signature='4/4', source_name='unknown.mxl'):
    warnings = []

    combined_vocal_part = stream.Part()
    combined_vocal_part.id = 'CombinedVoice'
    combined_vocal_part.partName = 'Combined Voice'
    last_known_time_signature = None

    # === Select Vocal Part ===
    movement_vocal_stream = None
    if score.parts:
        for part in score.parts:
            part_name_lower = str(part.partName).lower() if part.partName else ""
            if any(term in part_name_lower for term in ["voice", "lead", "soprano", "alto", "tenor", "bass"]):
                movement_vocal_stream = part
                break
        if movement_vocal_stream is None:
            movement_vocal_stream = score.parts[0]
            warnings.append(f"No labeled vocal part found in {source_name}; defaulted to first part.")
    else:
        movement_vocal_stream = score

    # === Time Signature ===
    ts = movement_vocal_stream.recurse().getElementsByClass(meter.TimeSignature).first()
    if ts:
        last_known_time_signature = ts
    else:
        fallback_ts = last_known_time_signature or meter.TimeSignature(fallback_time_signature)
        movement_vocal_stream.insert(0, fallback_ts)
        warnings.append(f"Inserted fallback time signature into {source_name}")

    # === Combine Measures ===
    for m in movement_vocal_stream.getElementsByClass('Measure'):
        combined_vocal_part.append(m)

    # === Note Filtering ===
    filtered_vocal_part = stream.Part()
    filtered_vocal_part.id = 'FilteredVoice'
    filtered_vocal_part.partName = 'Filtered Vocal Part'
    all_notes_info = []

    non_vocal_note_types = {'cue', 'grace', 'unpitched'}
    non_vocal_notehead_sizes = {'cue', 'grace', 'small'}

    for element in combined_vocal_part.flatten().notesAndRests:
        is_vocal_note = False
        current_lyrics = []
        current_note_type = getattr(element, 'type', None)
        current_notehead_size = getattr(getattr(element, 'notehead', None), 'size', None)

        # === Grace Note Check ===
        if element.isNote:
            if hasattr(element, 'expressions') and any('Grace' in str(type(exp)) for exp in element.expressions):
                continue

        # === Lyric Detection ===
        if element.isNote:
            if element.lyrics:
                current_lyrics = [ly.text for ly in element.lyrics]
        elif element.isChord:
            for note_in_chord in element.notes:
                if note_in_chord.lyrics:
                    current_lyrics.extend([ly.text for ly in note_in_chord.lyrics])

        # === Vocal Note Identification ===
        if element.isRest:
            continue
        elif current_lyrics:
            is_vocal_note = True
        elif element.isNote or element.isChord:
            if current_note_type not in non_vocal_note_types and current_notehead_size not in non_vocal_notehead_sizes:
                is_vocal_note = True

        if is_vocal_note:
            if element.isNote and element.pitch and element.pitch.midi is not None and element.duration.quarterLength > 0:
                all_notes_info.append({'midi': element.pitch.midi, 'duration': element.duration.quarterLength})
                filtered_vocal_part.append(element)
            elif element.isChord and element.duration.quarterLength > 0:
                for note_in_chord in element.notes:
                    note_type = getattr(note_in_chord, 'type', None)
                    note_size = getattr(getattr(note_in_chord, 'notehead', None), 'size', None)
                    is_note_in_chord_vocal = bool(note_in_chord.lyrics) or (
                        note_type not in non_vocal_note_types and note_size not in non_vocal_notehead_sizes
                    )
                    if is_note_in_chord_vocal and note_in_chord.pitch and note_in_chord.pitch.midi is not None:
                        all_notes_info.append({'midi': note_in_chord.pitch.midi, 'duration': element.duration.quarterLength})
                filtered_vocal_part.append(element)

    return all_notes_info, filtered_vocal_part, warnings

def deduplicate_by_key(all_keys_analysis):
    best_versions = {}
    
    for k in all_keys_analysis:
        if not isinstance(k, dict) or 'key' not in k:
            continue
            
        # 1. Cleanly handle both fresh music21 objects and legacy string objects
        if hasattr(k['key'], 'tonic') and hasattr(k['key'], 'mode'):
            # Convert to a standardized music21 Key object if needed
            key_obj = k['key']
        else:
            clean_str = " ".join(str(k['key']).split()).replace('♭', '-').replace('♯', '#')
            try:
                key_obj = music21.key.Key(clean_str)
            except Exception:
                key_obj = None

        # 2. Extract the strict pitch class bucket (0 to 11) + mode
        if key_obj:
            bucket_id = f"{key_obj.tonic.pitchClass}_{key_obj.mode}"
        else:
            # Absolute fallback if string parsing fails
            bucket_id = str(k['key'])[:3].strip()

        # 3. Choose the octave variant with the highest FINAL balanced score
        if bucket_id not in best_versions or k['final_score'] > best_versions[bucket_id]['final_score']:
            best_versions[bucket_id] = k
            
    # 4. Sort the options cleanly by final_score descending (Fixes Point ix)
    sorted_options = sorted(best_versions.values(), key=lambda x: x['final_score'], reverse=True)
    
    # Return exactly the top 12 unique musical keys
    return sorted_options[:12]

@app.route('/download/<folder>/<filename>')
def download_file(folder, filename):
    full_dir = os.path.join(app.config['OUTPUT_FOLDER'], folder)
    full_path = os.path.join(full_dir, filename)
    if not os.path.isfile(full_path):
        abort(404, description="File not found.")
    return send_from_directory(directory=full_dir, path=filename, as_attachment=True)

@app.route('/api/reanalyze-score', methods=['POST'])
def reanalyze_score():
    if 'file' not in request.files or 'pdf_hash' not in request.form:
        return jsonify({"error": "Missing file or song identifier"}), 400

    file = request.files['file']
    pdf_hash = request.form['pdf_hash']
    
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    # 1. Save the newly edited file locally to a temp directory
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, file.filename)
    file.save(temp_path)

    try:
        # 2. Extract and parse using your bulletproof bypass
        if file.filename.endswith('.mxl'):
            raw_xml_string = extract_xml_from_mxl(temp_path)
            score = converter.parse(raw_xml_string, format='musicxml')
        else:
            score = converter.parse(temp_path)

        # 3. Extract vocal notes
        notes, _, _ = extract_vocal_note_info(score, source_name=file.filename)
        if not notes:
            return jsonify({"error": "Could not extract vocal notes from this file."}), 400

        # 4. Standard App Analysis Logic
        ks_objects = score.parts[0].recurse().getElementsByClass(key.KeySignature)
        detected_key = ks_objects[0].asKey() if ks_objects else score.analyze('key')
        detected_key = simplify_enharmonic(ensure_music21_key(detected_key))
        
        all_keys_analysis = get_key_analysis_info(detected_key, notes, prefer_transpose_keys=False)
        original_key_info = next((k for k in all_keys_analysis if k['shift'] == 0), None)

        midi_values = [n['midi'] for n in notes]
        low_midi = min(midi_values) if midi_values else 60
        high_midi = max(midi_values) if midi_values else 72
        
        comfort_score = original_key_info["comfort_score"] if original_key_info else calculate_comfort_score(notes)

        # Deduplicate and rank
        deduped_keys = deduplicate_by_key(all_keys_analysis)
        for index, k in enumerate(deduped_keys):
            k['recommendation_rank'] = index + 1
            
        recommended = deduped_keys[0] if deduped_keys else None
        if recommended:
            rec_low_midi = pitch.Pitch(recommended['range_low']).midi
            rec_high_midi = pitch.Pitch(recommended['range_high']).midi
            recommended['low_comfort'] = get_note_comfort_category(rec_low_midi)
            recommended['high_comfort'] = get_note_comfort_category(rec_high_midi)
            recommended['low_color'] = get_note_comfort_color(rec_low_midi)
            recommended['high_color'] = get_note_comfort_color(rec_high_midi)

        other_keys = [k for k in deduped_keys if k['shift'] != recommended['shift']] if recommended else []

        # 5. Package summary payload
        summary = {
            "title": file.filename, 
            "all_keys_analysis": all_keys_analysis,
            "recommended": recommended,
            "other_keys": other_keys,
            "original_key_info": {
                "name": f"{detected_key.tonic.name} {detected_key.mode}",
                "range_low": pitch.Pitch(low_midi).nameWithOctave,
                "range_high": pitch.Pitch(high_midi).nameWithOctave,
                "comfort_score": comfort_score,
                "comfort_label": comfort_category(comfort_score)
            }
        }
        db_summary = make_json_safe(summary)

        # 6. OVERWRITE THE MXL IN THE STORAGE BUCKET
        # This keeps the cloud storage file matched with your edits
        storage_path = f"{pdf_hash}.mxl"
        with open(temp_path, 'rb') as f:
            supabase.storage.from_('mxl-library').upload(
                path=storage_path, 
                file=f, 
                file_options={
                    "upsert": "true",
                    "content-type": "application/vnd.recordare.musicxml+xml"
                }
            )

        # 7. UPDATE THE DATABASE MATRIX
        supabase.table('songs').update({
            "analysis_results": db_summary,
            "original_key": summary["original_key_info"]["name"],
            "lowest_note": summary["original_key_info"]["range_low"],
            "highest_note": summary["original_key_info"]["range_high"]
        }).eq("pdf_hash", pdf_hash).execute()

        return jsonify({"message": "Successfully updated cloud storage and re-analyzed score data!"}), 200

    except Exception as e:
        print(f"❌ Re-analysis route failure: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(temp_dir)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
