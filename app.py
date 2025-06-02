import traceback
import contextlib
import hashlib
import music21
from music21 import converter, pitch, key as m21key, interval, stream, meter, expressions, environment, note
import tempfile
import os
import subprocess
from flask import Flask, request, render_template, send_from_directory, abort
from werkzeug.utils import secure_filename
import glob
import math # For math.inf
import io
import zipfile
from xml.etree import ElementTree as ET
import time

AUDIVERIS_CMD_FAST = r"C:\audiveris_fast_install\bin\audiveris.bat"
AUDIVERIS_CMD_FULL = r"C:\audiveris_full_install\bin\audiveris.bat"


@contextlib.contextmanager
def temporarily_set_cwd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)

import hashlib

def get_file_hash(pdf_path):
    with open(pdf_path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

# Use a safe temp folder outside protected areas like Documents or AppData
def get_safe_scratch_dir():
    # Try C:/temp/m21_scratch first (Windows), then fall back to system temp
    base_temp = os.environ.get("TEMP", "C:/temp")
    scratch_dir = os.path.join(base_temp, "m21_scratch")

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
        print(f"❌ Failed to use preferred scratch dir: {scratch_dir} — {e}")
        # Fall back to system temp
        fallback = os.path.join(tempfile.gettempdir(), "m21_scratch_fallback")
        os.makedirs(fallback, exist_ok=True)
        print(f"⚠️  Falling back to system temp dir: {fallback}")
        return fallback

# Set music21's scratch directory
safe_working_dir = get_safe_scratch_dir()
us = environment.UserSettings()
us['directoryScratch'] = safe_working_dir

# --- Configuration ---
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'output'

# --- Ensure necessary directories exist on startup ---
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- Flask App Initialization ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

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

def is_singable(low_midi, high_midi):
    """
    Checks if an entire pitch range (lowest to highest MIDI) falls within the
    defined COMFORTABLE_RANGE.
    """
    return COMFORTABLE_RANGE[0] <= low_midi and high_midi <= COMFORTABLE_RANGE[1]

#--------Calculate comfort score----------

# Define MIDI boundaries
G3 = pitch.Pitch('G3').midi  # 55
A3 = pitch.Pitch('A3').midi  # 57
B3 = pitch.Pitch('B3').midi  # 59
C4 = pitch.Pitch('C4').midi  # 60
C5 = pitch.Pitch('C5').midi  # 72
D5 = pitch.Pitch('D5').midi  # 74
E5 = pitch.Pitch('E5').midi  # 76

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
            score += 3 * dur  # edge of range
        else:
            score += 10 * dur  # well outside range
    return round(score, 1)

def comfort_category(score):
    if score == 0:
        return "✅ Perfect fit"
    elif score <= 20:
        return "🎵 Very singable"
    elif score <= 80:
        return "⚠️ Singable but may challenge some"
    elif score <= 200:
        return "❌ Uncomfortable for most"
    else:
        return "🚫 Not suitable for congregational singing"

def comfort_category_slug(score):
    if score == 0:
        return "perfect"
    elif score <= 20:
        return "very-singable"
    elif score <= 80:
        return "challenging"
    elif score <= 200:
        return "uncomfortable"
    else:
        return "unsuitable"

def get_suitable_keys(original_key, all_notes_info, prefer_transpose_keys=False):
    """
    Finds and scores potential transposed keys based on comfort, key complexity,
    and preference for instrument keys.
    Returns a sorted list of all candidates and a filtered list of truly singable options.
    """
    suitable_options = []
    
    # Iterate through possible transpositions (e.g., -12 to +12 semitones = full octave up/down)
    for i in range(-12, 13):
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
            transposed_key = original_key.transpose(i)
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

            suitable_options.append({
                'shift': i,
                'key': transposed_key,
                'comfort_score': round(comfort_score, 1),
                'final_score': round(final_score, 1),
                'low': lowest,
                'high': highest,
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
    suitable_options.sort(key=lambda k: k['final_score'])

    # Filter singable options (if needed)
    truly_singable = [k for k in suitable_options if k['comfort_score'] <= 80]

    return suitable_options, truly_singable_options

# --- Flask Routes ---

@app.route('/generate_pdf/<hash>', methods=['GET'])
def generate_pdf_for_download(hash):
    shift = request.args.get("key", type=int, default=0)

    cache_dir = os.path.join(app.config['OUTPUT_FOLDER'], hash)
    uploaded_pdf = glob.glob(os.path.join(app.config['UPLOAD_FOLDER'], f"{hash}*.pdf"))
    if not uploaded_pdf:
        return f"<p>Error: No original PDF found for hash {hash}</p>", 404
    pdf_path = uploaded_pdf[0]

    full_output_dir = os.path.join(cache_dir, "full_export")
    os.makedirs(full_output_dir, exist_ok=True)

    # Run full Audiveris export to get better quality MXL
    subprocess_args = [
        AUDIVERIS_CMD_FULL,
        "-batch",
        "-transcribe",
        "-export",
        "-output", full_output_dir,
        "-option", "org.audiveris.omr.sheet.Partitioner.smallHeads=true",
        pdf_path
    ]

    try:
        result = subprocess.run(subprocess_args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return f"<p>Audiveris full run failed. Logs below:</p><pre>{result.stderr}</pre>", 500
    except Exception as e:
        return f"<p>Unexpected error during Audiveris full run: {e}</p>", 500

    mxl_files = glob.glob(os.path.join(full_output_dir, "*.mxl"))
    if not mxl_files:
        return f"<p>No MXL files found in {full_output_dir}</p>", 500

    from music21 import converter, stream

    combined_score = stream.Score()

    for mxl in sorted(mxl_files):
        try:
            score = converter.parse(mxl)
            if score.parts:
                for part in score.parts:
                    transposed = part.transpose(shift)
                    if transposed is not None:
                        combined_score.append(transposed)
                    else:
                        print(f"⚠️ Transpose returned None for a part in {mxl}")
            else:
                # No parts found — maybe a flat score
                print(f"⚠️ No parts found in {mxl}, appending full score")
                transposed_score = score.transpose(shift)
                if transposed_score is not None:
                    combined_score.append(transposed_score)
                else:
                    print(f"⚠️ Transpose returned None for full score in {mxl}")
        except Exception as e:
            print(f"⚠️ Error processing {mxl}: {e}")

    # Try to infer a readable key name
    from music21 import key
    try:
        inferred_key = combined_score.analyze('key')
        key_label = f"{inferred_key.tonic.name.replace('#','s').replace('-','b')}_{inferred_key.mode}"
    except Exception:
        key_label = f"shift{shift}"

    output_pdf_path = os.path.join(full_output_dir, f"lead_sheet_{key_label}.pdf")
    try:
        combined_score.write('lily.pdf', fp=output_pdf_path)
    except Exception as e:
        return f"<p>Error generating PDF with Lilypond: {e}</p>", 500

    return send_file(output_pdf_path, as_attachment=True)


@app.route('/', methods=['GET', 'POST'])
def upload_file():
    recommended = None  # placeholder to avoid NameError
    if request.method == 'POST':
        file = request.files['file']
        prefer_transpose_keys = 'transpose_keys' in request.form

        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            name, _ = os.path.splitext(filename)
            pdf_hash = get_file_hash(filepath)

            cached_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], pdf_hash)
            os.makedirs(cached_output_dir, exist_ok=True)

            # Run Audiveris only if MXL files aren't already cached
            if not any(fname.endswith('.mxl') for fname in os.listdir(cached_output_dir)):
                try:
                    print(f"⏱️ Starting Audiveris for hash: {pdf_hash}")
                    start_time = time.time()

                    subprocess_args = [
                        AUDIVERIS_CMD_FAST,
                        '-batch',
                        '-transcribe',
                        '-export',
                        '-output', cached_output_dir,
                        '-option', 'org.audiveris.omr.sheet.Partitioner.smallHeads=true',
                        '-option', 'audiveris.log.level=WARNING',
                        '-threads', '4',
                        filepath
                    ]
                    print("Running Audiveris command:", ' '.join(subprocess_args))
                    result = subprocess.run(subprocess_args, capture_output=True, text=True, check=False)

                    duration = time.time() - start_time
                    print(f"✅ Audiveris finished in {duration:.2f} seconds")
                    print("📂 Contents of output directory:", os.listdir(cached_output_dir))

                    if result.returncode != 0:
                        return f"<p>Audiveris processing failed (Error Code: {result.returncode}). Check server logs.</p><pre>{result.stdout}\n{result.stderr}</pre>", 500

                except Exception as e:
                    return f"<p>Unexpected error during Audiveris processing: {e}</p>", 500
            else:
                print(f"✅ Using cached MXL files for {filename} (hash: {pdf_hash})")

            try:
                summary = analyse_musicxml_summary(
                    output_dir=cached_output_dir,
                    name=name,
                    prefer_transpose_keys=prefer_transpose_keys
                )
                print("✅ Recommended key object:", recommended)

                return render_template("analysis_results.html",
                    pdf_hash=pdf_hash,
                    recommended=summary["recommended"],
                    other_singable=summary["other_singable"],
                    all_keys=summary["all_keys"],
                    skipped=summary["skipped"],
                    warnings=summary["warnings"],
                    pitch_range=summary["pitch_range"],
                    original_key=summary["original_key"],
                    original_key_is_singable=summary["original_key_is_singable"],
                    original_key_comment=summary["original_key_comment"]
                )

            except Exception as e:
                return f"<p>MusicXML analysis failed: {e}</p>", 500

        return "<p>Please upload a valid PDF file.</p><p><a href='/'>Try Again</a></p>"

    return render_template('upload.html')



def inject_time_signature_from_previous(mxl_path, fallback_time_signature):
    with zipfile.ZipFile(mxl_path, 'r') as zip_ref:
        xml_file_name = [n for n in zip_ref.namelist() if n.endswith('.xml')][0]
        xml_bytes = zip_ref.read(xml_file_name)

    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml_bytes)
    first_measure = root.find('.//part/measure')
    xml_modified = False

    if first_measure is not None:
        attributes = first_measure.find('attributes')
        has_time = attributes.find('time') if attributes is not None else None
        if attributes is not None and has_time is None:
            time_elem = ET.Element('time')
            if not isinstance(fallback_time_signature, meter.TimeSignature):
                # Try to convert string fallback like "4/4" to Meter object
                fallback_time_signature = meter.TimeSignature(fallback_time_signature)
            ET.SubElement(time_elem, 'beats').text = str(fallback_time_signature.numerator)
            ET.SubElement(time_elem, 'beat-type').text = str(fallback_time_signature.denominator)
            attributes.insert(1, time_elem)
            xml_modified = True

    return ET.tostring(root, encoding='utf-8') if xml_modified else None

def analyse_musicxml_summary(output_dir, name, prefer_transpose_keys=False):
    """
    Quickly analyzes MusicXML output to determine:
    - Original key
    - Pitch range
    - Recommended key and other singable keys
    - Comfort score
    - Whether original key is singable
    - If not, how far out of range it is
    - Any skipped movements or warnings
    """
    from music21 import converter, pitch, stream, note
    import os, glob, tempfile

    pattern = os.path.join(output_dir, f"{name}.mvt*.mxl")
    mxl_files = sorted(glob.glob(pattern))

    all_notes_info = []
    skipped = []
    warnings = []

    for path in mxl_files:
        try:
            # Inject fallback time signature if needed
            injected_bytes = inject_time_signature_from_previous(path, '4/4')
            if injected_bytes:
                patched_path = os.path.join(tempfile.gettempdir(), f"{os.path.basename(path)}_patched.xml")
                with open(patched_path, 'wb') as f:
                    f.write(injected_bytes)
                parse_target = patched_path
            else:
                parse_target = path

            score = converter.parse(parse_target)
            notes, _, part_warnings = extract_vocal_note_info(score, source_name=os.path.basename(path))
            all_notes_info.extend(notes)
            warnings.extend([f"{os.path.basename(path)}: {w}" for w in part_warnings])

        except Exception as e:
            skipped.append(os.path.basename(path))
            warnings.append(f"{os.path.basename(path)} failed: {e}")
            continue

    if not all_notes_info:
        return {
            "recommended": None,
            "other_singable": [],
            "all_keys": [],
            "original_key": "Unknown",
            "pitch_range": ("?", "?"),
            "skipped": skipped,
            "warnings": warnings,
            "original_key_is_singable": False,
            "original_key_comment": "No valid vocal notes found."
        }

    # Build dummy stream for key analysis
    dummy_stream = stream.Part()
    for n in all_notes_info:
        p = pitch.Pitch(midi=n['midi'])
        n_obj = note.Note(p)
        n_obj.duration.quarterLength = n['duration']
        dummy_stream.append(n_obj)

    try:
        key_estimate = dummy_stream.analyze('key')
        original_key_name = f"{key_estimate.tonic.name} {key_estimate.mode}"
    except Exception as e:
        key_estimate = None
        original_key_name = "Unknown"
        warnings.append(f"Key analysis failed: {e}")

    # Evaluate all keys
    all_keys, truly_singable = get_suitable_keys(key_estimate, all_notes_info, prefer_transpose_keys)
    recommended = all_keys[0] if all_keys else None
    other_singable = [k for k in truly_singable if k != recommended]

    # Pitch range of original (untransposed)
    midi_values = [n['midi'] for n in all_notes_info]
    original_low = min(midi_values)
    original_high = max(midi_values)
    low = pitch.Pitch(original_low).nameWithOctave
    high = pitch.Pitch(original_high).nameWithOctave

    original_low_out = max(0, COMFORTABLE_RANGE[0] - original_low)
    original_high_out = max(0, original_high - COMFORTABLE_RANGE[1])

    # Find the original key object (shift == 0) for singability check
    original_key_info = next((k for k in all_keys if k['shift'] == 0), None)
    if original_key_info:
        original_key_is_singable = is_singable(original_key_info["low"], original_key_info["high"])
        delta_low = max(0, COMFORTABLE_RANGE[0] - original_key_info["low"])
        delta_high = max(0, original_key_info["high"] - COMFORTABLE_RANGE[1])
        range_comment = ""
        if delta_low or delta_high:
            parts = []
            if delta_low > 0:
                parts.append(f"Lowest note ({pitch.Pitch(original_low).nameWithOctave}) is {delta_low} semitone{'s' if delta_low > 1 else ''} lower than the comfortable range for a mixed congregation")
            if delta_high > 0:
                parts.append(f"Highest note ({pitch.Pitch(original_high).nameWithOctave}) is {delta_high} semitone{'s' if delta_high > 1 else ''} higher than the comfortable range for a mixed congregation")
            range_comment = ", ".join(parts) + "."
    else:
        original_key_is_singable = False
        range_comment = ""

    # Recommended pitch out-of-range info and display names
    if recommended:

        # 2. Add the string representations of the range to the 'recommended' dict
        recommended['range_low'] = pitch.Pitch(recommended['low']).nameWithOctave
        recommended['range_high'] = pitch.Pitch(recommended['high']).nameWithOctave

        # 3. Calculate out-of-range values and add them to the 'recommended' dict
        #    If value is 0 (in range), set to None so {% if %} condition is false in template
        recommended_low_out = max(0, COMFORTABLE_RANGE[0] - recommended['low'])
        recommended['low_out'] = recommended_low_out if recommended_low_out > 0 else None

        recommended_high_out = max(0, recommended['high'] - COMFORTABLE_RANGE[1])
        recommended['high_out'] = recommended_high_out if recommended_high_out > 0 else None

    # If 'recommended' is None (no suitable keys found), the template's {% if recommended %}
    # will handle it, so no 'else' block needed here for setting these keys.

    return {
        "recommended": recommended, # This 'recommended' dict now contains range_low, range_high, low_out, high_out
        "other_singable": other_singable,
        "all_keys": all_keys,
        "original_key": original_key_name,
        "pitch_range": (low, high), # This is for the ORIGINAL range
        "skipped": skipped,
        "warnings": warnings,
        "original_key_is_singable": original_key_is_singable,
        "original_key_comment": range_comment,
        "original_low_out": original_low_out, # This is for the ORIGINAL range
        "original_high_out": original_high_out, # This is for the ORIGINAL range
        # Removed recommended_low_out and recommended_high_out from the top level
        # because they are now part of the 'recommended' dictionary itself.
    }


def extract_vocal_note_info(score, fallback_time_signature='4/4', source_name='unknown.mxl'):
    from music21 import stream, meter, expressions
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

@app.route('/download/<folder>/<filename>')
def download_file(folder, filename):
    full_dir = os.path.join(app.config['OUTPUT_FOLDER'], folder)
    full_path = os.path.join(full_dir, filename)
    if not os.path.isfile(full_path):
        abort(404, description="File not found.")
    return send_from_directory(directory=full_dir, path=filename, as_attachment=True)



if __name__ == '__main__':
    app.run(debug=True)
