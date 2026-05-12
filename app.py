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

def extract_metadata_from_pdf(pdf_path):
    metadata = {
        "ccli_number": "Unknown",
        "author": "Unknown",
        "year": "Unknown"
    }
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page = pdf.pages[0]
            width, height = first_page.width, first_page.height
            
            # Extract header text (top 30%) for Year and Author
            header_box = (0, 0, width, height * 0.3)
            header_text = first_page.within_bbox(header_box).extract_text() or ""
            
            # Extract full page text for CCLI (usually at the bottom)
            full_text = first_page.extract_text() or ""

            # 1. CCLI Extraction
            ccli_match = re.search(r'CCLI\s+Song\s+#?\s*(\d{5,8})', full_text, re.IGNORECASE)
            if ccli_match:
                metadata["ccli_number"] = ccli_match.group(1)

            # 2. Year Extraction
            year_match = re.search(r'(?:©|Words:|Music:)\s*.*?(\d{4})', header_text)
            if year_match:
                metadata["year"] = year_match.group(1)

            # 3. Author Extraction
            words_by = re.search(r'Words (?:and Music )?by\s*(.*?)(?=\n|Music by|©|CCLI|$)', header_text, re.IGNORECASE)
            music_by = re.search(r'Music by\s*(.*?)(?=\n|Words by|©|CCLI|$)', header_text, re.IGNORECASE)
            
            if words_by and music_by:
                w, m = words_by.group(1).strip().strip(','), music_by.group(1).strip().strip(',')
                metadata["author"] = w if w == m else f"Words: {w} / Music: {m}"
            elif words_by:
                metadata["author"] = words_by.group(1).strip()
            elif music_by:
                metadata["author"] = music_by.group(1).strip()
            elif "Public Domain" in header_text:
                metadata["author"] = "Public Domain"

            return metadata
    except Exception as e:
        print(f"⚠️ Metadata extraction failed: {e}")
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
# Comfortable vocal range for most congregations (C4 to D5)
# C4 = MIDI 60, D5 = MIDI 74
COMFORTABLE_RANGE = (pitch.Pitch('C4').midi, pitch.Pitch('D5').midi)

# "Sweet Spot" within the comfortable range, notes here are extra comfortable
# Example: E4 (64) to B4 (71)
SWEET_SPOT_RANGE = (pitch.Pitch('E4').midi, pitch.Pitch('B4').midi)

# Penalties for notes outside the comfortable range
OUT_OF_RANGE_PENALTY_PER_SEMITONE = 5 # Penalty for each semitone outside the comfortable range
SWEET_SPOT_BONUS_PER_SEMITONE = 1     # Bonus (score reduction) for each semitone inside the sweet spot

# Threshold for notes considered "harsh outliers" (very difficult to sing)
HARSH_OUTLIER_THRESHOLD = 8 # Notes more than this many semitones outside range get extra penalty

# Maximum number of sharps/flats considered "friendly" for congregational singing
MAX_ACCIDENTALS = 3

# List of keys considered "friendly" (e.g., C, G, D, A, E, F, Bb, Eb)
FRIENDLY_KEYS = {'C', 'G', 'D', 'A', 'E', 'F', 'Bb', 'Eb'}

# List of keys that are common for transposing instruments (Bb, Eb, Ab)
TRANSPOSE_INSTRUMENT_KEYS = {'Bb', 'Eb', 'Ab'}

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

def calculate_comfort_score(notes_info):
    """
    Calculates comfort score based on note durations and pitch zones.
    Lower is better. 0 = ideal tessitura only.
    """
    if not notes_info:
        print("⚠️ Empty note list in calculate_comfort_score")
        return float('inf')

    score = 0
    for note in notes_info:
        midi = note['midi']
        dur = note['duration']
        if C4 <= midi <= C5:
            continue  # ideal tessitura
        elif A3 <= midi <= B3 or C5 < midi <= D5:
            score += 1 * dur  # slightly outside ideal
        elif G3 <= midi < A3 or D5 < midi <= E5:
            base_penalty = 3
            score += base_penalty * dur  # edge of range
            # Add an extra penalty for sustained notes in the stretch zone
            if dur >= 1.9: # If note is half-note or longer
                score += base_penalty * 0.5 # Add half the base penalty again
        else:
            base_penalty = 10
            score += base_penalty * dur
            # Add a more significant extra penalty for sustained notes out of range
            if dur >= 2: # If note is half-note or longer
                score += base_penalty # Add full base penalty again
    return round(score, 1)

def comfort_category(score):
    if score == 0:
        return "✅ Perfect fit"
    elif score <= 35:
        return "🎵 Very singable"
    elif score <= 120:
        return "⚠️ Singable but may challenge some"
    elif score <= 250:
        return "❌ Uncomfortable for most"
    else:
        return "🚫 Not suitable for congregational singing"

def comfort_category_slug(score):
    if score == 0:
        return "perfect"
    elif score <= 35:
        return "very-singable"
    elif score <= 120:
        return "challenging"
    elif score <= 250:
        return "uncomfortable"
    else:
        return "unsuitable"

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


def get_key_analysis_info(original_key, all_notes_info, prefer_transpose_keys=False):
    """
    Finds and scores potential transposed keys based on comfort, key complexity,
    and preference for instrument keys.
    Returns a sorted list of all candidates and a filtered list of truly singable options.
    """
    key_analysis_info = []
##    print(f"all_notes_info={all_notes_info}")
    warnings = []
    # Iterate through possible transpositions (e.g., -12 to +12 semitones = full octave up/down)
    for i in range(-11, 12):
        try:
            shifted_notes_info = [
                {'midi': n['midi'] + i, 'duration': n['duration']}
                for n in all_notes_info
                if 'midi' in n and 'duration' in n and isinstance(n['duration'], (int, float)) and n['duration'] > 0
            ]

            if not shifted_notes_info:
                print(f"⚠️ Skipping shift {i}: no valid notes after filtering")
                continue

            comfort_score = calculate_comfort_score(shifted_notes_info)
            original_key = ensure_music21_key(original_key)
            if not isinstance(original_key, music21.key.Key):
                print(f"⚠️ Cannot transpose shift {i}: final_key is not a valid music21 Key object. Skipping.")
                continue
            try:
                transposed_key = original_key.transpose(i)
            except Exception as e:
                print(f"❌ Transposition failed for shift {i}: {e}")
                continue

            key_tonic_name = transposed_key.tonic.name

            accidental_penalty = abs(transposed_key.sharps)
            if accidental_penalty > MAX_ACCIDENTALS:
                accidental_penalty *= 5

            key_preference_penalty = 0
            if prefer_transpose_keys:
                if key_tonic_name not in TRANSPOSE_INSTRUMENT_KEYS:
                    key_preference_penalty = 10
            else:
                if key_tonic_name not in FRIENDLY_KEYS:
                    key_preference_penalty = 3

            final_score = comfort_score + (accidental_penalty * 2) + key_preference_penalty + abs(i)

            lowest = min(n['midi'] for n in shifted_notes_info)
            highest = max(n['midi'] for n in shifted_notes_info)
            low_comfort = get_note_comfort_category(lowest)
            high_comfort = get_note_comfort_category(highest)
            low_color = get_note_comfort_color(lowest)
            high_color = get_note_comfort_color(highest)

            key_analysis_info.append({
                'shift': i,
                'key': transposed_key,
                'comfort_score': round(comfort_score, 1),
                'final_score': round(final_score, 1),
                'low': lowest,
                'high': highest,
                'low_comfort': low_comfort,
                'high_comfort': high_comfort,
                'low_color': low_color,
                'high_color': high_color,
                'range_low': pitch.Pitch(lowest).nameWithOctave,
                'range_high': pitch.Pitch(highest).nameWithOctave,
                'comfort_label': comfort_category(comfort_score),
                'comfort_slug': comfort_category_slug(comfort_score),
            })

        except ZeroDivisionError:
            print(f"❌ ZeroDivisionError on shift {i}")
            continue
        except Exception as e:
            print(f"❌ Unexpected error on shift {i}: {e}")
            continue
 
    # Sort all candidates by their final_score (lowest score is best)
    key_analysis_info.sort(key=lambda k: k['final_score'])

    return key_analysis_info

# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        file = request.files['file']
        prefer_transpose_keys = 'transpose_keys' in request.form

        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            pdf_metadata = extract_metadata_from_pdf(filepath)
            name, _ = os.path.splitext(filename)

            pdf_hash = get_file_hash(filepath)

            cached_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], pdf_hash)
            os.makedirs(cached_output_dir, exist_ok=True)

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
                        '-Xmx4g',                          # High memory for OCR
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

                except Exception as e:
                    return f"<p>Unexpected error during Audiveris processing: {e}</p>", 500
            else:
                print(f"✅ Using cached MXL files for {filename} (hash: {pdf_hash})")

            try:
                summary = analyse_musicxml_summary(
                    output_dir=cached_output_dir,
                    name=name,
                    prefer_transpose_keys=prefer_transpose_keys,
                    pdf_metadata=pdf_metadata
                )

                if supabase: # Only try if the client was actually created
                    try:
                        # Debug: List all buckets to see what the app actually sees
                        buckets = supabase.storage.list_buckets()
                        print(f"DEBUG: Visible buckets: {[b.name for b in buckets]}")

                        mxl_files = [f for f in os.listdir(cached_output_dir) if f.endswith('.mxl')]
                        if mxl_files:
                            mxl_filename = mxl_files[0]
                            local_mxl_path = os.path.join(cached_output_dir, mxl_filename)
                            storage_path = f"{pdf_hash}.mxl"

                            with open(local_mxl_path, 'rb') as f:
                                supabase.storage.from_('mxl-library').upload(
                                    path=storage_path, 
                                    file=f, 
                                    file_options={
                                        "upsert": "true",
                                        "content-type": "application/vnd.recordare.musicxml+xml" # The official MXL type
                                    }
                                )
                            
                            mxl_url = supabase.storage.from_('mxl-library').get_public_url(storage_path)
                            # Fix: Create a "database-friendly" copy of the summary
                            db_summary = make_json_safe(summary)

                            # Flatten analysis for easy searching/filtering
                            orig_info = summary.get("original_key_info", {})
                                                      
                            supabase.table('songs').upsert({
                                "pdf_hash": pdf_hash,
                                "title": summary.get("title", "Unknown Title"),
                                "ccli_number": pdf_metadata.get("ccli_number", "Unknown"), # From the NEW pdf_metadata
                                "author": pdf_metadata.get("author", "Unknown"),           # NEW COLUMN
                                "year": pdf_metadata.get("year", "Unknown"),               # NEW COLUMN
                                "original_key": orig_info.get("name", "Unknown"),          # NEW COLUMN
                                "lowest_note": orig_info.get("range_low", "Unknown"),      # NEW COLUMN
                                "highest_note": orig_info.get("range_high", "Unknown"),
                                "mxl_url": mxl_url,
                                "analysis_results": db_summary 
                            }, on_conflict="pdf_hash").execute()  # <--- Added on_conflict="pdf_hash"
                            print(f"🚀 Cloud Save Successful for {pdf_hash}")
                    except Exception as cloud_err:
                        # This prints to the Railway logs but DOES NOT trigger a 500 error for the user                     
                        print(f"⚠️ Cloud Save background error: {cloud_err}")
                        
                return render_template("analysis_results.html",
                    pdf_hash=pdf_hash,                                       
                    original_key=summary["original_key_info"],
                    recommended=summary["recommended"],
                    other_keys=summary["other_keys"],
                    skipped=summary["skipped"],
                    warnings=summary["warnings"],
                    ccli_link=summary.get("ccli_url"),
                    title=summary.get("title"),
                    author=pdf_metadata.get("author"),
                    year=pdf_metadata.get("year"),
                    ccli_no=summary["ccli_number"]
                )

            except Exception as e:
                logging.error("--- FATAL ERROR DURING MUSICXML ANALYSIS ---")
                logging.error(f"Exception Type: {type(e)}")
                logging.error(f"Exception Message: {e}")
                logging.exception("Full Traceback Details:")
                logging.error("--- END FATAL ERROR ---")

                print("Error:", e)

                if 'cached_output_dir' in locals() and os.path.exists(cached_output_dir):
                    try:
                        # We use ignore_errors=True so that if a single temp file 
                        # is 'locked', the whole app doesn't crash.
                        shutil.rmtree(cached_output_dir, ignore_errors=True)
                        print(f"🧹 Cleaned up failed directory: {cached_output_dir}")
                    except Exception as cleanup_error:
                        # Log it, but don't stop the user from seeing the original error
                        logging.warning(f"Cleanup failed for {cached_output_dir}: {cleanup_error}")

                return f"<p>MusicXML analysis failed: {e}</p>", 500

        return f"<p>Please upload a valid PDF file.</p><p><a href='{url_for('upload_file')}'>Try Again</a></p>"

    return render_template('upload.html')

@app.route('/search_songs', methods=['GET'])
def search_songs():
    query = request.args.get('q', '')
    if len(query) < 2:
        return jsonify([])

    try:
        # Search title, author, or CCLI number
        # ILIKE is case-insensitive search
        res = supabase.table('songs').select("*").or_(
            f"title.ilike.%{query}%,author.ilike.%{query}%,ccli_number.ilike.%{query}%"
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
        
    summary = song['analysis_results']
    
    # Return the same analysis template we use for new uploads
    return render_template("analysis_results.html",
        pdf_hash=song['pdf_hash'],
        original_key=summary["original_key_info"],
        recommended=summary["recommended"],
        other_keys=summary["other_keys"],
        skipped=summary["skipped"],
        warnings=summary["warnings"],
        ccli_link=summary.get("ccli_url"),
        title=song.get("title"),
        author=pdf_metadata.get("author"),
        year=pdf_metadata.get("year"),
        ccli_no=song.get("ccli_number")
    )

def extract_xml_from_mxl(path):
    with zipfile.ZipFile(path, 'r') as z:
        # Find the first .xml file inside the .mxl archive
        for name in z.namelist():
            if name.endswith('.xml'):
                return z.read(name).decode("utf-8")
    raise ValueError(f"No .xml file found inside {path}")

def inject_divisions_and_time_if_missing(current_path, previous_path=None):
    # Load the XML content
    xml_data = extract_xml_from_mxl(current_path)
    xml = xml_data.decode("utf-8") if isinstance(xml_data, bytes) else xml_data

    # --- FIX 1: Zero/Missing Divisions ---
    if "<divisions>0</divisions>" in xml:
        xml = xml.replace("<divisions>0</divisions>", "<divisions>1</divisions>")
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
    from music21 import converter, pitch, stream, note

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

            score = converter.parse(parse_target)
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
    first_chord = chords[0] if chords else None
    print(f"First chord:{first_chord}")
    last_chord = chords[-1] if chords else None
    print(f"last chord:{last_chord}")
    ks_objects = score.parts[0].recurse().getElementsByClass(key.KeySignature)
    for ks in ks_objects:
        print(f"Found KeySignature: {ks.sharps} sharps — {ks.asKey().name}")
    try:
        songname_part, _, file_key = name.split("-")
        file_key = ensure_music21_key(file_key) if file_key else None
        print(f"songname is {songname_part}, file_key is {file_key}")
        print(f"file_key key type: {type(file_key)} | Value: {file_key}")
            
        declared_key = ks_objects[0].asKey() if ks_objects else None
        print(f"Declared key type: {type(declared_key)} | Value: {declared_key}")

        estimated_key = dummy_stream.analyze('key')
        print(f"Estimated key:{estimated_key}")

        key_warning = ""
        if file_key:
            final_key = file_key
        elif declared_key:
            final_key = declared_key
            relative_minor = declared_key.relative
            print(f"Relative minor:{relative_minor}")

            if estimated_key == relative_minor:
                if chords:
                    first_chord = chords[0] if len(chords) > 0 else None
                    last_chord = chords[-1] if len(chords) > 0 else None

                    try:
                        if (
                            (first_chord and first_chord.root().name == relative_minor.tonic.name and first_chord.quality == 'minor') or
                            (last_chord and last_chord.root().name == relative_minor.tonic.name and last_chord.quality == 'minor')
                        ):
                            final_key = relative_minor
                            final_key = ensure_music21_key(final_key)
                    except Exception as e:
                        warnings.append(f"Chord comparison failed: {e}")

            elif (
                estimated_key.tonic.name != declared_key.tonic.name or 
                estimated_key.mode != declared_key.mode
            ):
                key_warning = (
                    f"⚠️ Estimated key from notes ({estimated_key.tonic.name} {estimated_key.mode}) disagrees with declared key signature ({declared_key.tonic.name} {declared_key.mode})."
                )
            print(f"final_key:{final_key}")
            final_key = ensure_music21_key(final_key)
        else:
            final_key = estimated_key
            final_key = ensure_music21_key(final_key)
            key_warning = "⚠️ No key signature found on the stave; using estimated key."

        if final_key:
            final_key = simplify_enharmonic(final_key)

        original_key_name = f"{final_key.tonic.name} {final_key.mode}" if final_key else "Unknown"

    except Exception as e:
        final_key = None
        original_key_name = "Unknown"
        key_warning = f"⚠️ Key analysis failed: {e}"
        warnings.append(key_warning)

    print(f"Just before all_keys_analysis, final _key: {type(final_key)} = {final_key}")
    all_keys_analysis = get_key_analysis_info(final_key, all_notes_info, prefer_transpose_keys)
    bad_entries = [k for k in all_keys_analysis if not isinstance(k, dict) or 'key' not in k]
    print("🛠  Number of malformed entries:", len(bad_entries))

    original_key_info = next((k for k in all_keys_analysis if k['shift'] == 0), None)
    if original_key_info:
        original_low = original_key_info["low"]
        original_high = original_key_info["high"]
        low = original_key_info["range_low"]
        high = original_key_info["range_high"]
        original_low_comfort = original_key_info["low_comfort"]
        original_high_comfort = original_key_info["high_comfort"]
        original_low_color = original_key_info["low_color"]
        original_high_color = original_key_info["high_color"]
    else:
        midi_values = [n['midi'] for n in all_notes_info]
        original_low = min(midi_values)
        original_high = max(midi_values)
        low = pitch.Pitch(original_low).nameWithOctave
        high = pitch.Pitch(original_high).nameWithOctave
        original_low_comfort = get_note_comfort_category(original_low)
        original_high_comfort = get_note_comfort_category(original_high)
        original_low_color = get_note_comfort_color(original_low)
        original_high_color = get_note_comfort_color(original_high)
        
    deduped_keys = deduplicate_by_key(all_keys_analysis)

    recommended = deduped_keys[0] if deduped_keys else None
    recommended['low_comfort'] = get_note_comfort_category(recommended['low'])
    recommended['high_comfort'] = get_note_comfort_category(recommended['high'])
    recommended['low_color'] = get_note_comfort_color(recommended['low'])
    recommended['high_color'] = get_note_comfort_color(recommended['high'])

    other_keys = [k for k in deduped_keys if k['shift'] != recommended['shift']]

    summary["all_keys_analysis"] = all_keys_analysis
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
        "comfort_score": original_key_info["comfort_score"],
        "comfort_label": original_key_info["comfort_label"]
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
##    print("🔍 Type of all_keys_analysis:", type(all_keys_analysis))
##    print("🔍 First few entries:", all_keys_analysis[:3])

    best_versions = {}
    for k in all_keys_analysis:
        if not isinstance(k, dict):
            print(f"⚠️ Skipping invalid item: {k} (type: {type(k)})")
            continue

        key_id = (k['key'].tonic.name, k['key'].mode)
        if key_id not in best_versions or k['comfort_score'] < best_versions[key_id]['comfort_score']:
            best_versions[key_id] = k
    return list(best_versions.values())


@app.route('/download/<folder>/<filename>')
def download_file(folder, filename):
    full_dir = os.path.join(app.config['OUTPUT_FOLDER'], folder)
    full_path = os.path.join(full_dir, filename)
    if not os.path.isfile(full_path):
        abort(404, description="File not found.")
    return send_from_directory(directory=full_dir, path=filename, as_attachment=True)



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
